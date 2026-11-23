import os
import threading
import time
from pathlib import Path

import pytest
from psycopg import Connection, OperationalError
from psycopg.errors import InsufficientPrivilege

from app.core.config import Settings
from app.persistence import migrate
from app.persistence.postgres import build_conninfo


def test_missing_password_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_MIGRATOR_PASSWORD", "")
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_DB", "enterprise")
    assert migrate.main() == 1
    assert "AGENT_MIGRATOR_PASSWORD" in capsys.readouterr().err


def test_connection_failure_hides_the_password(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_MIGRATOR_PASSWORD", "day47-secret-value")
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_PORT", "1")
    monkeypatch.setenv("POSTGRES_DB", "enterprise")
    monkeypatch.setenv("AGENT_MIGRATOR_USER", "agent_migrator")
    assert migrate.main() == 1
    captured = capsys.readouterr()
    assert "day47-secret-value" not in captured.err
    assert "day47-secret-value" not in captured.out


def test_command_settings_do_not_use_the_app_role(monkeypatch):
    monkeypatch.setenv("AGENT_MIGRATOR_USER", "agent_app")
    monkeypatch.setenv("AGENT_MIGRATOR_PASSWORD", "unused")
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_DB", "enterprise")
    with pytest.raises(RuntimeError, match="agent_migrator"):
        migrate.apply_migrations(migrate._command_settings())


def test_prepare_database_in_prod_does_not_apply_sql(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("prod executed a migration")

    monkeypatch.setattr(migrate, "apply_migrations", forbidden)
    seen: list[str] = []
    monkeypatch.setattr(migrate, "assert_schema_ready", lambda _settings: seen.append("check"))
    migrate.prepare_database(Settings(
        app_env="prod",
        enterprise_infra_enabled=True,
        postgres_host="127.0.0.1",
        postgres_db="enterprise",
        agent_app_password="app-secret",
        agent_migrator_password="",
        redis_host="127.0.0.1",
        redis_password="redis-secret",
        agent_internal_token="internal-token-value",
        java_base_url="http://127.0.0.1:8080",
    ))
    assert seen == ["check"]


def test_failed_migration_closes_its_connection(monkeypatch):
    closed = {"value": False}

    class FakeConnection:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError("lock failed")

        def close(self):
            closed["value"] = True

        def rollback(self):
            return None

    monkeypatch.setattr(migrate.Connection, "connect", lambda *_args, **_kwargs: FakeConnection())
    with pytest.raises(RuntimeError, match="lock failed"):
        migrate.apply_migrations(Settings(
            app_env="dev",
            postgres_host="127.0.0.1",
            postgres_db="enterprise",
            agent_app_user="agent_app",
            agent_migrator_user="agent_migrator",
            agent_migrator_password="unused",
        ))
    assert closed["value"] is True


def test_repeat_and_lock_on_real_postgres():
    env = _root_env()
    if not env.get("AGENT_MIGRATOR_PASSWORD") or not env.get("AGENT_APP_PASSWORD") or not env.get("POSTGRES_DB"):
        pytest.skip("仓库根目录 .env 未提供迁移或业务口令，跳过真实数据库测试")
    settings = _settings(env)
    try:
        first = migrate.apply_migrations(settings)
        second = migrate.apply_migrations(settings)
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL 不可用，跳过真实数据库测试: {exc.__class__.__name__}")
    assert second == 0
    before = _snapshot(env)
    _hold_lock_and_wait(env, settings)
    after = _snapshot(env)
    assert before["versions"] == after["versions"]
    assert before["memories"] == after["memories"]
    assert before["audits"] == after["audits"]
    assert before["checkpoints"] == after["checkpoints"]
    assert before["blobs"] == after["blobs"]
    assert first >= 0
    migrate.assert_schema_ready(settings)
    with _connect(env, "agent_app", env["AGENT_APP_PASSWORD"]) as connection:
        user = connection.execute(
            "select current_user, (select rolsuper from pg_roles where rolname = current_user)"
        ).fetchone()
        business = connection.execute(
            "select has_schema_privilege(current_user, 'business', 'USAGE')"
        ).fetchone()
        try:
            connection.execute("create table agent.day47_ddl_probe(id int)")
            raise AssertionError("agent_app executed DDL")
        except Exception as exc:
            connection.rollback()
            assert exc.__class__.__name__ == "InsufficientPrivilege"
        try:
            connection.execute("delete from agent.memory_audit")
            raise AssertionError("agent_app deleted audit rows")
        except Exception as exc:
            connection.rollback()
            assert exc.__class__.__name__ == "InsufficientPrivilege"
        for statement in (
            "insert into agent.schema_migrations (version, name) values ('999', 'blocked')",
            "update agent.schema_migrations set name = name where version = '999'",
            "delete from agent.schema_migrations where version = '999'",
        ):
            try:
                connection.execute(statement)
                raise AssertionError("agent_app wrote migration history")
            except InsufficientPrivilege as exc:
                connection.rollback()
                assert exc.sqlstate == "42501"
    assert user[0] == "agent_app"
    assert user[1] is False
    assert business[0] is False
    assert migrate.main() == 0


def _hold_lock_and_wait(env: dict[str, str], settings: Settings) -> None:
    started = threading.Event()
    release = threading.Event()
    holder: dict[str, Connection] = {}

    def hold() -> None:
        connection = _connect(env, "agent_migrator", env["AGENT_MIGRATOR_PASSWORD"])
        holder["connection"] = connection
        connection.execute("select pg_advisory_lock(%s)", (migrate._LOCK_KEY,))
        connection.commit()
        started.set()
        release.wait(15)
        connection.close()

    finished: dict[str, float] = {}

    def run() -> None:
        migrate.apply_migrations(settings)
        finished["at"] = time.monotonic()

    holder_thread = threading.Thread(target=hold)
    worker = threading.Thread(target=run)
    holder_thread.start()
    assert started.wait(10)
    worker.start()
    time.sleep(0.4)
    assert worker.is_alive()
    released_at = time.monotonic()
    release.set()
    try:
        worker.join(20)
        holder_thread.join(5)
    finally:
        release.set()
        connection = holder.get("connection")
        if connection is not None and not connection.closed:
            connection.close()
    assert "at" in finished
    assert finished["at"] >= released_at


def _snapshot(env: dict[str, str]) -> dict[str, object]:
    with _connect(env, "agent_migrator", env["AGENT_MIGRATOR_PASSWORD"]) as connection:
        versions = connection.execute(
            "select version from agent.schema_migrations order by version"
        ).fetchall()
        memories = connection.execute("select count(*) from agent.memory_item").fetchone()
        audits = connection.execute("select count(*) from agent.memory_audit").fetchone()
        checkpoints = connection.execute("select count(*) from agent.checkpoints").fetchone()
        blobs = connection.execute("select count(*) from agent.checkpoint_blobs").fetchone()
        forced = connection.execute(
            """
            select c.relname, c.relrowsecurity, c.relforcerowsecurity
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'agent'
              and c.relname in ('memory_item', 'memory_audit')
            order by c.relname
            """
        ).fetchall()
    assert [(row[0], row[1], row[2]) for row in forced] == [
        ("memory_audit", True, True),
        ("memory_item", True, True),
    ]
    return {
        "versions": [row[0] for row in versions],
        "memories": memories[0],
        "audits": audits[0],
        "checkpoints": checkpoints[0],
        "blobs": blobs[0],
    }


def _settings(env: dict[str, str]) -> Settings:
    return Settings(
        app_env="dev",
        enterprise_infra_enabled=False,
        postgres_host=env.get("POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(env.get("POSTGRES_PORT", "5432")),
        postgres_db=env["POSTGRES_DB"],
        agent_app_user="agent_app",
        agent_app_password=env["AGENT_APP_PASSWORD"],
        agent_migrator_user="agent_migrator",
        agent_migrator_password=env["AGENT_MIGRATOR_PASSWORD"],
        agent_internal_token="",
    )


def _connect(env: dict[str, str], user: str, password: str) -> Connection:
    return Connection.connect(
        build_conninfo(env.get("POSTGRES_HOST", "127.0.0.1"), int(env.get("POSTGRES_PORT", "5432")), env["POSTGRES_DB"], user, password),
        options="-c search_path=agent",
    )


def _root_env() -> dict[str, str]:
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    os.environ.setdefault("POSTGRES_HOST", values.get("POSTGRES_HOST", "127.0.0.1"))
    os.environ.setdefault("POSTGRES_PORT", values.get("POSTGRES_PORT", "5432"))
    os.environ.setdefault("POSTGRES_DB", values.get("POSTGRES_DB", ""))
    os.environ.setdefault("AGENT_MIGRATOR_PASSWORD", values.get("AGENT_MIGRATOR_PASSWORD", ""))
    os.environ.setdefault("AGENT_MIGRATOR_USER", "agent_migrator")
    return values
