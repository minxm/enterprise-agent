"""Apply agent-schema SQL migrations with the migrator role.

Business connections stay on agent_app. This module opens its own connection.
The production application checks versions and does not call apply_migrations.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from psycopg import Connection, OperationalError
from psycopg.errors import InsufficientPrivilege

from app.core.config import Settings
from app.persistence.postgres import _public_error, build_agent_conninfo, reject_superuser

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_LOCK_KEY = 16160016016


def apply_migrations(settings: Settings, directory: Path | None = None) -> int:
    if (
        not settings.postgres_host
        or not settings.postgres_db
        or not settings.agent_migrator_password
    ):
        raise RuntimeError(
            "数据库迁移配置不完整：需要 POSTGRES_HOST、POSTGRES_DB 和 AGENT_MIGRATOR_PASSWORD"
        )
    if settings.agent_migrator_user == settings.agent_app_user:
        raise RuntimeError("迁移必须使用 agent_migrator，不能复用业务连接")
    conninfo = (
        f"host={settings.postgres_host} port={settings.postgres_port} "
        f"dbname={settings.postgres_db} user={settings.agent_migrator_user} "
        f"password={settings.agent_migrator_password} connect_timeout=3"
    )
    try:
        connection = Connection.connect(conninfo, options="-c search_path=agent")
    except OperationalError as exc:
        raise _public_error("数据库迁移连接失败", exc, settings.agent_migrator_password) from exc
    try:
        reject_superuser(connection)
        _require_migrator(connection, settings.agent_migrator_user)
        _lock(connection)
        try:
            _ensure_migration_table(connection)
            applied = _applied_versions(connection)
            executed = 0
            for version, name, path in _migration_files(directory or MIGRATIONS_DIR):
                if version in applied:
                    continue
                script = path.read_text(encoding="utf-8")
                with connection.transaction():
                    for statement in _split_sql(script):
                        connection.execute(statement)
                    connection.execute(
                        "insert into agent.schema_migrations (version, name) values (%s, %s)",
                        (version, name),
                    )
                executed += 1
            return executed
        finally:
            _unlock(connection)
    finally:
        connection.close()


def prepare_database(settings: Settings) -> None:
    """开发环境沿用启动时迁移。生产环境只核对已有版本，不执行 SQL 脚本。"""
    if settings.app_env == "prod":
        assert_schema_ready(settings)
        return
    apply_migrations(settings)


def assert_schema_ready(settings: Settings) -> None:
    if settings.agent_app_user != "agent_app":
        raise RuntimeError("业务连接必须使用 agent_app")
    if not settings.postgres_host or not settings.postgres_db or not settings.agent_app_password:
        raise RuntimeError("数据库未完成迁移")
    try:
        connection = Connection.connect(
            build_agent_conninfo(settings),
            options="-c search_path=agent",
        )
    except OperationalError as exc:
        raise _public_error("数据库未完成迁移", exc, settings.agent_app_password) from exc
    try:
        reject_superuser(connection)
        row = connection.execute("select current_user").fetchone()
        current_user = row[0] if row else ""
        if current_user != "agent_app":
            raise RuntimeError("业务连接必须使用 agent_app")
        try:
            rows = connection.execute("select version from agent.schema_migrations").fetchall()
        except InsufficientPrivilege:
            raise RuntimeError("数据库未完成迁移") from None
        applied = {item[0] for item in rows}
        expected = {version for version, _name, _path in _migration_files(MIGRATIONS_DIR)}
        if not expected or not expected.issubset(applied):
            raise RuntimeError("数据库未完成迁移")
        logger.info("agent schema 兼容，未执行迁移")
    finally:
        connection.close()


def _require_migrator(connection: Connection, expected_user: str) -> None:
    row = connection.execute("select current_user").fetchone()
    current_user = row[0] if row else ""
    connection.rollback()
    if current_user != expected_user or current_user == "agent_app":
        raise RuntimeError("迁移必须使用 agent_migrator，不能复用业务连接")


def _lock(connection: Connection) -> None:
    connection.execute("select pg_advisory_lock(%s)", (_LOCK_KEY,)).fetchone()
    connection.commit()


def _unlock(connection: Connection) -> None:
    try:
        connection.rollback()
        connection.execute("select pg_advisory_unlock(%s)", (_LOCK_KEY,)).fetchone()
        connection.commit()
    except Exception:
        try:
            connection.rollback()
        except Exception:
            return


def _ensure_migration_table(connection: Connection) -> None:
    with connection.transaction():
        connection.execute(
            """
            create table if not exists agent.schema_migrations (
                version text primary key,
                name text not null,
                applied_at timestamptz not null default now()
            )
            """
        )
        connection.execute("revoke all on table agent.schema_migrations from agent_app")
        connection.execute("revoke all on table agent.schema_migrations from public")
        connection.execute("grant select on table agent.schema_migrations to agent_app")


def _applied_versions(connection: Connection) -> set[str]:
    rows = connection.execute("select version from agent.schema_migrations").fetchall()
    connection.rollback()
    return {row[0] for row in rows}


def _migration_files(directory: Path) -> list[tuple[str, str, Path]]:
    if not directory.is_dir():
        raise RuntimeError("迁移目录不存在")
    found: list[tuple[str, str, Path]] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.sql")):
        stem = path.stem
        version, separator, name = stem.partition("_")
        if not separator or not version.isdigit() or not name:
            raise RuntimeError("迁移文件名必须是 {version}_{name}.sql")
        if version in seen:
            raise RuntimeError("迁移版本号重复")
        seen.add(version)
        found.append((version, name, path))
    found.sort(key=lambda item: int(item[0]))
    return found


def _split_sql(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_string = False
    dollar_tag: str | None = None
    index = 0
    length = len(script)
    while index < length:
        if dollar_tag is not None:
            if script.startswith(dollar_tag, index):
                current.append(dollar_tag)
                index += len(dollar_tag)
                dollar_tag = None
                continue
            current.append(script[index])
            index += 1
            continue
        char = script[index]
        if char == "'":
            current.append(char)
            if in_string and index + 1 < length and script[index + 1] == "'":
                current.append("'")
                index += 2
                continue
            in_string = not in_string
            index += 1
            continue
        if not in_string:
            tag = _opening_dollar_tag(script, index)
            if tag is not None:
                current.append(tag)
                index += len(tag)
                dollar_tag = tag
                continue
        if char == ";" and not in_string:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            index += 1
            continue
        if (
            not in_string
            and char == "-"
            and index + 1 < length
            and script[index + 1] == "-"
        ):
            newline = script.find("\n", index)
            if newline < 0:
                break
            index = newline
            continue
        current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _opening_dollar_tag(script: str, index: int) -> str | None:
    if index >= len(script) or script[index] != "$":
        return None
    if index + 1 < len(script) and script[index + 1] == "$":
        return "$$"
    end = index + 1
    while end < len(script) and (script[end].isalnum() or script[end] == "_"):
        end += 1
    if end > index + 1 and end < len(script) and script[end] == "$":
        return script[index : end + 1]
    return None


def main() -> int:
    try:
        executed = apply_migrations(_command_settings())
    except Exception as exc:
        print(_safe_text(exc), file=sys.stderr)
        return 1
    if executed == 0:
        print("agent schema 没有待执行的迁移")
    else:
        print(f"agent schema 迁移完成，新执行 {executed} 个版本")
    return 0


def _command_settings() -> Settings:
    port_text = os.environ.get("POSTGRES_PORT", "5432").strip() or "5432"
    try:
        port = int(port_text)
    except ValueError:
        port = 0
    return Settings(
        app_env="dev",
        enterprise_infra_enabled=False,
        postgres_host=os.environ.get("POSTGRES_HOST", "").strip(),
        postgres_port=port,
        postgres_db=os.environ.get("POSTGRES_DB", "").strip(),
        agent_app_user="agent_app",
        agent_app_password="unused",
        agent_migrator_user=os.environ.get("AGENT_MIGRATOR_USER", "agent_migrator").strip() or "agent_migrator",
        agent_migrator_password=os.environ.get("AGENT_MIGRATOR_PASSWORD", "").strip(),
        agent_internal_token="",
    )


def _safe_text(exc: Exception) -> str:
    text = str(exc)
    for name in (
        "AGENT_MIGRATOR_PASSWORD",
        "AGENT_APP_PASSWORD",
        "BUSINESS_MIGRATOR_PASSWORD",
        "BUSINESS_APP_PASSWORD",
        "POSTGRES_PASSWORD",
    ):
        secret = os.environ.get(name, "")
        if secret:
            text = text.replace(secret, "***")
    return text


if __name__ == "__main__":
    raise SystemExit(main())
