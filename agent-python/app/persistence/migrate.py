"""Apply agent-schema SQL migrations with the migrator role.

Business connections stay on agent_app. This module opens its own connection.
"""

from __future__ import annotations

from pathlib import Path

from psycopg import Connection, OperationalError

from app.core.config import Settings
from app.persistence.postgres import _public_error

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_LOCK_KEY = 16160016016


def apply_migrations(settings: Settings, directory: Path | None = None) -> None:
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
        _require_migrator(connection, settings.agent_migrator_user)
        _lock(connection)
        try:
            _ensure_migration_table(connection)
            applied = _applied_versions(connection)
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
        finally:
            _unlock(connection)
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
    index = 0
    while index < len(script):
        char = script[index]
        if char == "'":
            current.append(char)
            if in_string and index + 1 < len(script) and script[index + 1] == "'":
                current.append("'")
                index += 2
                continue
            in_string = not in_string
        elif char == ";" and not in_string:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        elif (
            not in_string
            and char == "-"
            and index + 1 < len(script)
            and script[index + 1] == "-"
        ):
            newline = script.find("\n", index)
            if newline < 0:
                break
            index = newline
        else:
            current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements
