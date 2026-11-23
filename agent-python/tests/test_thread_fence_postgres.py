import threading
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import Connection, OperationalError
from psycopg.errors import InsufficientPrivilege
from psycopg_pool import ConnectionPool

from app.core.config import Settings
from app.persistence.migrate import apply_migrations
from app.persistence.postgres import build_conninfo, tenant_transaction
from app.persistence.thread_fence import (
    FenceMismatch,
    FenceNotFound,
    ThreadFenceStore,
)


def test_fence_allocation_is_atomic_isolated_and_privileged(fence_db):
    store = ThreadFenceStore(fence_db.pool)
    tenant_a, tenant_b = uuid4(), uuid4()
    conversation_id = uuid4()
    thread_a = f"kq:{tenant_a}:{conversation_id}"
    thread_b = f"kq:{tenant_b}:{conversation_id}"
    other_thread = f"kq:{tenant_a}:{uuid4()}"
    fence_db.tenants.extend([tenant_a, tenant_b])
    before = fence_db.before

    first = store.next_fence(tenant_a, thread_a)
    second = store.next_fence(tenant_a, thread_a)
    other = store.next_fence(tenant_a, other_thread)
    foreign = store.next_fence(tenant_b, thread_b)

    assert first == 1
    assert second == 2
    assert other == 1
    assert foreign == 1
    assert store.check_fence(tenant_a, thread_a, 2) == 2
    with pytest.raises(FenceMismatch):
        store.check_fence(tenant_a, thread_a, 1)
    with pytest.raises(FenceNotFound):
        store.check_fence(tenant_a, f"kq:{tenant_a}:{uuid4()}", 1)

    concurrent = _allocate_together(fence_db.pool, tenant_a, f"kq:{tenant_a}:{uuid4()}", 8)
    assert sorted(concurrent) == list(range(1, 17))
    assert len(set(concurrent)) == 16

    _assert_rollback_discards_the_increment(fence_db, tenant_a)
    _assert_tenant_isolation(fence_db, tenant_a, tenant_b, thread_a, thread_b)
    _assert_no_context_and_no_ddl(fence_db, thread_a)
    _assert_migration_role(fence_db)

    after = _counts(fence_db.env)
    assert "006" not in before["versions"] or before["versions"] == after["versions"]
    assert "006" in after["versions"]
    assert [row for row in before["versions"] if row != "006"] == [row for row in after["versions"] if row != "006"]
    for name in ("checkpoints", "blobs", "writes", "memories", "audits", "documents", "chunks"):
        assert before[name] == after[name]


def _allocate_together(pool: ConnectionPool, tenant_id: UUID, thread_id: str, per_connection: int) -> list[int]:
    barrier = threading.Barrier(2)
    found: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        local = ThreadFenceStore(pool)
        try:
            barrier.wait(10)
            values = [local.next_fence(tenant_id, thread_id) for _ in range(per_connection)]
        except BaseException as exc:
            with lock:
                errors.append(exc)
            return
        with lock:
            found.extend(values)

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    if errors:
        raise errors[0]
    return found


def _assert_rollback_discards_the_increment(fence_db, tenant_id: UUID) -> None:
    thread_id = f"kq:{tenant_id}:{uuid4()}"
    with pytest.raises(RuntimeError, match="abort fence"):
        with tenant_transaction(fence_db.pool, str(tenant_id)) as connection:
            connection.execute(
                """
                insert into agent.thread_run (tenant_id, thread_id, fence)
                values (%s, %s, 1)
                """,
                (tenant_id, thread_id),
            )
            raise RuntimeError("abort fence")
    assert ThreadFenceStore(fence_db.pool).next_fence(tenant_id, thread_id) == 1


def _assert_tenant_isolation(fence_db, tenant_a: UUID, tenant_b: UUID, thread_a: str, thread_b: str) -> None:
    with tenant_transaction(fence_db.pool, str(tenant_a)) as connection:
        visible = connection.execute(
            "select thread_id, fence from agent.thread_run where thread_id = %s",
            (thread_b,),
        ).fetchall()
        updated = connection.execute(
            "update agent.thread_run set fence = fence + 10 where thread_id = %s",
            (thread_b,),
        )
        assert visible == []
        assert updated.rowcount == 0
    assert ThreadFenceStore(fence_db.pool).check_fence(tenant_b, thread_b, 1) == 1
    with tenant_transaction(fence_db.pool, str(tenant_b)) as connection:
        hidden = connection.execute(
            "select fence from agent.thread_run where thread_id = %s",
            (thread_a,),
        ).fetchall()
        assert hidden == []


def _assert_no_context_and_no_ddl(fence_db, thread_id: str) -> None:
    fresh = uuid4()
    with _connect(fence_db.env, "agent_app", fence_db.env["AGENT_APP_PASSWORD"]) as connection:
        assert connection.execute("select current_user").fetchone()[0] == "agent_app"
        with connection.transaction():
            assert connection.execute("select count(*) from agent.thread_run").fetchone()[0] == 0
            updated = connection.execute("update agent.thread_run set fence = 99")
            assert updated.rowcount == 0
        with pytest.raises(InsufficientPrivilege):
            with connection.transaction():
                connection.execute(
                    """
                    insert into agent.thread_run (tenant_id, thread_id, fence)
                    values (%s, %s, 1)
                    """,
                    (fresh, f"kq:{fresh}:{uuid4()}"),
                )
        with pytest.raises(InsufficientPrivilege) as denied:
            with connection.transaction():
                connection.execute("create table agent.day52_ddl_probe(id int)")
        assert denied.value.sqlstate == "42501"
        with pytest.raises(InsufficientPrivilege):
            with connection.transaction():
                connection.execute("alter table agent.thread_run add column extra text")
        with pytest.raises(InsufficientPrivilege):
            with connection.transaction():
                connection.execute("delete from agent.thread_run")
        with pytest.raises(InsufficientPrivilege):
            with connection.transaction():
                connection.execute(
                    "insert into agent.schema_migrations (version, name) values ('999', 'day52-probe')"
                )
        assert connection.execute("select thread_id from agent.thread_run").fetchall() == []
        tenant_id = UUID(thread_id.split(":")[1])
        assert ThreadFenceStore(fence_db.pool).check_fence(tenant_id, thread_id, 2) == 2
        privileges = connection.execute(
            """
            select
                has_table_privilege(current_user, 'agent.thread_run', 'SELECT'),
                has_table_privilege(current_user, 'agent.thread_run', 'INSERT'),
                has_table_privilege(current_user, 'agent.thread_run', 'UPDATE'),
                has_table_privilege(current_user, 'agent.thread_run', 'DELETE'),
                has_table_privilege(current_user, 'agent.thread_run', 'TRUNCATE'),
                has_schema_privilege(current_user, 'agent', 'CREATE')
            """
        ).fetchone()
        assert privileges == (True, True, True, False, False, False)


def _assert_migration_role(fence_db) -> None:
    with _connect(fence_db.env, "agent_migrator", fence_db.env["AGENT_MIGRATOR_PASSWORD"]) as connection:
        user = connection.execute(
            "select current_user, (select rolsuper from pg_roles where rolname = current_user)"
        ).fetchone()
        assert user == ("agent_migrator", False)
        forced = connection.execute(
            """
            select c.relrowsecurity, c.relforcerowsecurity, c.relowner::regrole::text
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'agent' and c.relname = 'thread_run'
            """
        ).fetchone()
        assert forced == (True, True, "agent_migrator")
        policies = connection.execute(
            """
            select polname
            from pg_policy
            where polrelid = 'agent.thread_run'::regclass
            order by polname
            """
        ).fetchall()
        assert [row[0] for row in policies] == ["tenant_isolation", "thread_run_maintainer"]
    assert apply_migrations(_settings(fence_db.env)) == 0


def _counts(env: dict[str, str]) -> dict[str, object]:
    with _connect(env, "agent_migrator", env["AGENT_MIGRATOR_PASSWORD"]) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "select version from agent.schema_migrations order by version"
            ).fetchall()
        ]
        checkpoints = connection.execute("select count(*) from agent.checkpoints").fetchone()[0]
        blobs = connection.execute("select count(*) from agent.checkpoint_blobs").fetchone()[0]
        writes = connection.execute("select count(*) from agent.checkpoint_writes").fetchone()[0]
        memories = connection.execute("select count(*) from agent.memory_item").fetchone()[0]
        audits = connection.execute("select count(*) from agent.memory_audit").fetchone()[0]
    documents, chunks = _knowledge_counts(env)
    return {
        "versions": versions,
        "checkpoints": checkpoints,
        "blobs": blobs,
        "writes": writes,
        "memories": memories,
        "audits": audits,
        "documents": documents,
        "chunks": chunks,
    }


def _knowledge_counts(env: dict[str, str]) -> tuple[int, int]:
    user = env.get("POSTGRES_USER", "postgres")
    password = env.get("POSTGRES_PASSWORD", "")
    if not password:
        return (-1, -1)
    with _connect(env, user, password) as connection:
        documents = connection.execute("select count(*) from agent.knowledge_document").fetchone()[0]
        chunks = connection.execute("select count(*) from agent.knowledge_chunk").fetchone()[0]
    return documents, chunks


@pytest.fixture
def fence_db():
    env = _root_env()
    if not env.get("AGENT_APP_PASSWORD") or not env.get("AGENT_MIGRATOR_PASSWORD") or not env.get("POSTGRES_DB"):
        pytest.skip("仓库根目录 .env 未提供数据库口令，跳过真实数据库测试")
    settings = _settings(env)
    try:
        before = _counts(env)
        apply_migrations(settings)
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL 不可用，跳过真实数据库测试: {exc.__class__.__name__}")
    pool = ConnectionPool(
        build_conninfo(
            settings.postgres_host,
            settings.postgres_port,
            settings.postgres_db,
            settings.agent_app_user,
            settings.agent_app_password,
        ),
        min_size=2,
        max_size=4,
        timeout=10,
        kwargs={"options": "-c search_path=agent"},
        open=True,
    )
    database = type("FenceDatabase", (), {"pool": pool, "env": env, "tenants": [], "before": before})()
    try:
        yield database
    finally:
        pool.close()
        if database.tenants:
            with _connect(env, "agent_migrator", env["AGENT_MIGRATOR_PASSWORD"]) as connection:
                connection.execute(
                    "delete from agent.thread_run where tenant_id = any(%s)",
                    (database.tenants,),
                )
                connection.commit()


def _settings(env: dict[str, str]) -> Settings:
    return Settings(
        postgres_host=env.get("POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(env.get("POSTGRES_PORT", "5432")),
        postgres_db=env["POSTGRES_DB"],
        agent_app_user="agent_app",
        agent_app_password=env["AGENT_APP_PASSWORD"],
        agent_migrator_user="agent_migrator",
        agent_migrator_password=env["AGENT_MIGRATOR_PASSWORD"],
    )


def _connect(env: dict[str, str], user: str, password: str) -> Connection:
    return Connection.connect(
        build_conninfo(
            env.get("POSTGRES_HOST", "127.0.0.1"),
            int(env.get("POSTGRES_PORT", "5432")),
            env["POSTGRES_DB"],
            user,
            password,
        ),
        autocommit=True,
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
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values
