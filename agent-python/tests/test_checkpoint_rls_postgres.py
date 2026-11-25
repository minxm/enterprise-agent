"""Checkpoint RLS against the local PostgreSQL, connected as agent_app.

Cleanup deletes only the tenants created here. Historical checkpoint threads
stay in place. Observation of one existing thread id is test-only: the product
path never parses a tenant out of a thread id.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import Connection, OperationalError
from psycopg.errors import InsufficientPrivilege
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.core.config import Settings
from app.persistence.checkpoint import CheckpointResources
from app.persistence.fence_context import fence_scope, tenant_scope
from app.persistence.fenced_saver import FencedPostgresSaver, FencingViolation
from app.persistence.migrate import apply_migrations
from app.persistence.postgres import build_conninfo, reject_superuser
from app.persistence.thread_fence import ThreadFenceStore

TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def test_checkpoint_tables_force_rls_for_agent_app(rls_db):
    status = _as_migrator(
        rls_db,
        """
        select c.relname, c.relrowsecurity, c.relforcerowsecurity, r.rolname as owner
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        join pg_roles r on r.oid = c.relowner
        where n.nspname = 'agent' and c.relname = any(%s)
        order by c.relname
        """,
        (list(TABLES),),
    )
    assert [tuple(row) for row in status] == [
        ("checkpoint_blobs", True, True, "agent_migrator"),
        ("checkpoint_writes", True, True, "agent_migrator"),
        ("checkpoints", True, True, "agent_migrator"),
    ]
    policies = _as_migrator(
        rls_db,
        """
        select tablename, policyname
        from pg_policies
        where schemaname = 'agent' and tablename = any(%s)
        order by tablename, policyname
        """,
        (list(TABLES),),
    )
    assert [tuple(row) for row in policies] == [
        ("checkpoint_blobs", "checkpoint_maintainer"),
        ("checkpoint_blobs", "checkpoint_tenant"),
        ("checkpoint_writes", "checkpoint_maintainer"),
        ("checkpoint_writes", "checkpoint_tenant"),
        ("checkpoints", "checkpoint_maintainer"),
        ("checkpoints", "checkpoint_tenant"),
    ]
    roles = _as_migrator(
        rls_db,
        """
        select rolname, rolsuper, rolbypassrls
        from pg_roles
        where rolname = any(%s)
        order by rolname
        """,
        (["agent_app", "agent_migrator"],),
    )
    assert [tuple(row) for row in roles] == [
        ("agent_app", False, False),
        ("agent_migrator", False, False),
    ]
    assert apply_migrations(rls_db.settings) == 0


def test_missing_guc_hides_every_checkpoint(rls_db):
    with rls_db.resources.pool.connection() as connection:
        visible = _scalar(connection, "select count(*) from agent.checkpoints")
        blobs = _scalar(connection, "select count(*) from agent.checkpoint_blobs")
        writes = _scalar(connection, "select count(*) from agent.checkpoint_writes")
    assert (visible, blobs, writes) == (0, 0, 0)


def test_tenant_cannot_read_or_change_another_tenants_rows(rls_db):
    owner = _write(rls_db, "cp-owner")
    other = _write(rls_db, "cp-other")
    with tenant_scope(other.tenant):
        hidden = rls_db.saver.get_tuple(_config(owner.thread_id))
        own = rls_db.saver.get_tuple(_config(other.thread_id))
    assert hidden is None
    assert own is not None
    assert own.checkpoint["id"] == "cp-other"
    assert own.checkpoint["channel_values"]["answer"] == "cp-other"
    assert own.config["configurable"]["checkpoint_ns"] == ""
    assert _counts(rls_db, other.tenant, owner.thread_id) == (0, 0, 0)
    assert _counts(rls_db, owner.tenant, owner.thread_id) == (1, 1, 1)
    with rls_db.resources.pool.connection() as connection:
        with connection.transaction():
            connection.execute("select set_config('app.tenant_id', %s, true)", (str(other.tenant),))
            updated = connection.execute(
                "update agent.checkpoints set metadata = metadata || %s where thread_id = %s",
                (Jsonb({"probe": "other"}), owner.thread_id),
            )
            deleted = connection.execute(
                "delete from agent.checkpoint_blobs where thread_id = %s",
                (owner.thread_id,),
            )
            deleted_writes = connection.execute(
                "delete from agent.checkpoint_writes where thread_id = %s",
                (owner.thread_id,),
            )
            assert (updated.rowcount, deleted.rowcount, deleted_writes.rowcount) == (0, 0, 0)
            with pytest.raises(InsufficientPrivilege):
                with connection.transaction():
                    connection.execute(
                        """
                        insert into agent.checkpoints
                            (thread_id, checkpoint_ns, checkpoint_id, checkpoint)
                        values (%s, '', %s, %s)
                        """,
                        (owner.thread_id, "cp-forged", Jsonb({"v": 1})),
                    )
    assert _counts(rls_db, owner.tenant, owner.thread_id) == (1, 1, 1)
    with tenant_scope(owner.tenant):
        restored = rls_db.saver.get_tuple(_config(owner.thread_id))
    assert restored is not None
    assert restored.checkpoint["channel_values"]["answer"] == "cp-owner"


def test_same_conversation_uuid_stays_isolated(rls_db):
    conversation = uuid4()
    left = _write(rls_db, "left", conversation)
    right = _write(rls_db, "right", conversation)
    assert left.thread_id != right.thread_id
    assert left.thread_id.endswith(str(conversation))
    with tenant_scope(left.tenant):
        assert rls_db.saver.get_tuple(_config(right.thread_id)) is None
        assert rls_db.saver.get_tuple(_config(left.thread_id)) is not None
    assert _counts(rls_db, left.tenant, right.thread_id) == (0, 0, 0)


def test_stale_fence_does_not_leave_partial_rows(rls_db):
    written = _write(rls_db, "cp-current-rls")
    before = _counts(rls_db, written.tenant, written.thread_id)
    assert rls_db.store.next_fence(written.tenant, written.thread_id) == 2
    checkpoint = {
        "v": 1,
        "id": "cp-stale-rls",
        "ts": "2026-11-25T08:00:00.000000+00:00",
        "channel_values": {"answer": "stale"},
        "channel_versions": {"answer": "00000000000000000000000000000001.0000000000000001"},
        "versions_seen": {},
        "updated_channels": ["answer"],
    }
    versions = {"answer": "00000000000000000000000000000001.0000000000000001"}
    with fence_scope(written.tenant, written.thread_id, 1):
        with pytest.raises(FencingViolation):
            rls_db.saver.put(_config(written.thread_id), checkpoint, {"source": "loop", "step": 1}, versions)
    assert _counts(rls_db, written.tenant, written.thread_id) == before
    with rls_db.resources.pool.connection() as connection:
        with connection.transaction():
            connection.execute("select set_config('app.tenant_id', %s, true)", (str(written.tenant),))
            stale = _scalar(
                connection,
                "select count(*) from agent.checkpoints where thread_id = %s and checkpoint_id = %s",
                (written.thread_id, "cp-stale-rls"),
            )
    assert stale == 0


def test_noncanonical_thread_id_is_rejected(rls_db):
    tenant = uuid4()
    with rls_db.resources.pool.connection() as connection:
        with pytest.raises(InsufficientPrivilege):
            with connection.transaction():
                connection.execute("select set_config('app.tenant_id', %s, true)", (str(tenant),))
                connection.execute(
                    """
                    insert into agent.checkpoints
                        (thread_id, checkpoint_ns, checkpoint_id, checkpoint)
                    values ('not-a-thread', '', 'cp-bad', %s)
                    """,
                    (Jsonb({"v": 1}),),
                )


def test_historical_checkpoint_is_readable_with_its_own_tenant(rls_db):
    assert rls_db.historical_thread
    with tenant_scope(rls_db.historical_tenant):
        loaded = rls_db.saver.get_tuple(_config(rls_db.historical_thread))
    assert loaded is not None
    assert loaded.config["configurable"]["checkpoint_ns"] == ""
    assert _counts(rls_db, rls_db.historical_tenant, rls_db.historical_thread)[0] >= 1


def test_agent_app_cannot_disable_rls_or_drop_the_policy(rls_db):
    with rls_db.resources.pool.connection() as connection:
        with pytest.raises(InsufficientPrivilege):
            connection.execute("alter table agent.checkpoints disable row level security")
        with pytest.raises(InsufficientPrivilege):
            connection.execute("drop policy checkpoint_tenant on agent.checkpoints")
    status = _as_migrator(
        rls_db,
        """
        select relrowsecurity, relforcerowsecurity
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'agent' and c.relname = 'checkpoints'
        """,
    )
    assert [tuple(row) for row in status] == [(True, True)]
    names = _as_migrator(
        rls_db,
        """
        select policyname from pg_policies
        where schemaname = 'agent' and tablename = 'checkpoints'
        order by policyname
        """,
    )
    assert [row[0] for row in names] == ["checkpoint_maintainer", "checkpoint_tenant"]


def test_historical_counts_are_unchanged(rls_db):
    _cleanup(rls_db)
    rls_db.threads.clear()
    assert _table_counts(rls_db) == rls_db.baseline


@pytest.fixture(scope="module")
def rls_db():
    env = _root_env()
    if not env.get("AGENT_APP_PASSWORD") or not env.get("AGENT_MIGRATOR_PASSWORD") or not env.get("POSTGRES_DB"):
        pytest.skip("仓库根目录 .env 未提供数据库口令，跳过真实数据库测试")
    settings = _settings(env)
    resources = None
    store_pool = None
    try:
        apply_migrations(settings)
        resources = CheckpointResources(
            build_conninfo(
                settings.postgres_host,
                settings.postgres_port,
                settings.postgres_db,
                settings.agent_app_user,
                settings.agent_app_password,
            ),
            settings.agent_app_password,
        )
        resources.open()
        store_pool = ConnectionPool(
            resources.conninfo,
            min_size=1,
            max_size=2,
            timeout=10,
            kwargs={"options": "-c search_path=agent"},
            open=True,
        )
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL 不可用，跳过真实数据库测试: {exc.__class__.__name__}")
    except RuntimeError as exc:
        if "连接" in str(exc) or "PostgreSQL" in str(exc) or "超级用户" in str(exc):
            pytest.skip(f"PostgreSQL 不可用，跳过真实数据库测试: {exc.__class__.__name__}")
        raise
    with resources.pool.connection() as connection:
        reject_superuser(connection)
        role = connection.execute("select current_user as user").fetchone()
        assert role["user"] == "agent_app"
    database = type(
        "CheckpointRls",
        (),
        {
            "settings": settings,
            "env": env,
            "resources": resources,
            "store_pool": store_pool,
            "saver": FencedPostgresSaver(resources.pool),
            "store": ThreadFenceStore(store_pool),
            "threads": [],
            "baseline": None,
            "historical_thread": None,
            "historical_tenant": None,
        },
    )()
    database.baseline = _table_counts(database)
    historical = _as_migrator(
        database,
        """
        select thread_id
        from agent.checkpoints
        where checkpoint_ns = ''
          and thread_id ~ '^kq:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        limit 1
        """,
    )
    if historical:
        database.historical_thread = historical[0][0]
        database.historical_tenant = UUID(database.historical_thread.split(":")[1])
    try:
        yield database
    finally:
        _cleanup(database)
        store_pool.close()
        resources.close()


class _Written:
    def __init__(self, tenant: UUID, thread_id: str) -> None:
        self.tenant = tenant
        self.thread_id = thread_id


def _write(rls_db, answer: str, conversation: UUID | None = None) -> _Written:
    tenant = uuid4()
    thread_id = f"kq:{tenant}:{conversation or uuid4()}"
    rls_db.threads.append((tenant, thread_id))
    fence = rls_db.store.next_fence(tenant, thread_id)
    versions = {
        "answer": "00000000000000000000000000000001.0000000000000001",
        "payload": "00000000000000000000000000000001.0000000000000002",
    }
    checkpoint = {
        "v": 1,
        "id": answer,
        "ts": "2026-11-25T08:00:00.000000+00:00",
        "channel_values": {"answer": answer, "payload": {"n": 1}},
        "channel_versions": versions,
        "versions_seen": {},
        "updated_channels": ["answer", "payload"],
    }
    with fence_scope(tenant, thread_id, fence):
        saved = rls_db.saver.put(
            _config(thread_id),
            checkpoint,
            {"source": "loop", "step": 1},
            versions,
        )
        rls_db.saver.put_writes(saved, [("messages", answer)], "task-rls")
    return _Written(tenant, thread_id)


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


def _counts(rls_db, tenant: UUID, thread_id: str) -> tuple[int, int, int]:
    with rls_db.resources.pool.connection() as connection:
        with connection.transaction():
            connection.execute("select set_config('app.tenant_id', %s, true)", (str(tenant),))
            return tuple(
                _scalar(connection, f"select count(*) from agent.{table} where thread_id = %s", (thread_id,))
                for table in TABLES
            )


def _scalar(connection, sql: str, params=None) -> int:
    row = connection.execute(sql, params).fetchone()
    value = next(iter(row.values())) if isinstance(row, dict) else row[0]
    return int(value)


def _table_counts(rls_db) -> tuple[int, int, int]:
    rows = _as_migrator(
        rls_db,
        """
        select
            (select count(*) from agent.checkpoints),
            (select count(*) from agent.checkpoint_blobs),
            (select count(*) from agent.checkpoint_writes)
        """,
    )
    return tuple(int(value) for value in rows[0])


def _as_migrator(rls_db, sql: str, params=None) -> list[tuple]:
    with Connection.connect(
        build_conninfo(
            rls_db.settings.postgres_host,
            rls_db.settings.postgres_port,
            rls_db.settings.postgres_db,
            rls_db.settings.agent_migrator_user,
            rls_db.settings.agent_migrator_password,
        ),
        autocommit=True,
        options="-c search_path=agent",
    ) as connection:
        reject_superuser(connection)
        return list(connection.execute(sql, params).fetchall())


def _cleanup(rls_db) -> None:
    if rls_db.resources.pool is None:
        return
    with rls_db.resources.pool.connection() as connection:
        for tenant, thread_id in rls_db.threads:
            with connection.transaction():
                connection.execute("select set_config('app.tenant_id', %s, true)", (str(tenant),))
                for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    connection.execute(f"delete from agent.{table} where thread_id = %s", (thread_id,))
    if not rls_db.threads:
        return
    tenants = [tenant for tenant, _thread in rls_db.threads]
    with Connection.connect(
        build_conninfo(
            rls_db.settings.postgres_host,
            rls_db.settings.postgres_port,
            rls_db.settings.postgres_db,
            rls_db.settings.agent_migrator_user,
            rls_db.settings.agent_migrator_password,
        ),
        autocommit=True,
        options="-c search_path=agent",
    ) as connection:
        connection.execute("delete from agent.thread_run where tenant_id = any(%s)", (tenants,))


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


def _root_env() -> dict[str, str]:
    path = Path(__file__).resolve().parents[2] / ".env"
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values
