import threading
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import Connection, OperationalError
from psycopg.errors import NotNullViolation
from psycopg_pool import ConnectionPool
from app.core.config import Settings
from app.persistence.checkpoint import CheckpointResources
from app.persistence.fence_context import fence_scope, tenant_scope
from app.persistence.fenced_saver import FencedPostgresSaver, FencingViolation
from app.persistence.migrate import apply_migrations
from app.persistence.postgres import build_conninfo, reject_superuser, tenant_transaction
from app.persistence.thread_fence import FenceContextError, FenceValidationError, ThreadFenceStore


def test_current_fence_can_be_saved_and_read(fenced_db):
    saver, store, tenant, thread_id = _begin(fenced_db)
    assert store.next_fence(tenant, thread_id) == 1
    checkpoint, versions = _checkpoint("cp-current", {"answer": "ok", "payload": {"n": 1}})

    with fence_scope(tenant, thread_id, 1):
        saved = saver.put(
            _config(thread_id),
            checkpoint,
            {"source": "loop", "step": 2},
            versions,
        )
        saver.put_writes(saved, [("messages", "pending")], "task-1", "tasks")

    with tenant_scope(tenant):
        loaded = saver.get_tuple(_config(thread_id))
        listed = list(saver.list(_config(thread_id)))
    assert saved["configurable"]["checkpoint_id"] == "cp-current"
    assert loaded is not None
    assert loaded.checkpoint["channel_values"]["answer"] == "ok"
    assert loaded.checkpoint["channel_values"]["payload"] == {"n": 1}
    assert loaded.metadata["source"] == "loop"
    assert loaded.metadata["step"] == 2
    assert loaded.pending_writes == [("task-1", "messages", "pending")]
    assert [item.checkpoint["id"] for item in listed] == ["cp-current"]
    assert _counts(fenced_db, thread_id) == (1, 1, 1)
    _assert_guc_does_not_leak(fenced_db)


def test_stale_fence_cannot_put_a_checkpoint(fenced_db):
    saver, store, tenant, thread_id = _begin(fenced_db)
    assert store.next_fence(tenant, thread_id) == 1
    assert store.next_fence(tenant, thread_id) == 2
    checkpoint, versions = _checkpoint("cp-stale")

    with fence_scope(tenant, thread_id, 1):
        with pytest.raises(FencingViolation):
            saver.put(_config(thread_id), checkpoint, {"source": "loop", "step": 1}, versions)

    with tenant_scope(tenant):
        assert saver.get_tuple(_config(thread_id)) is None
    assert _counts(fenced_db, thread_id) == (0, 0, 0)
    assert store.check_fence(tenant, thread_id, 2) == 2


def test_stale_fence_cannot_put_writes(fenced_db):
    saver, store, tenant, thread_id = _begin(fenced_db)
    assert store.next_fence(tenant, thread_id) == 1
    checkpoint, versions = _checkpoint("cp-writes")
    with fence_scope(tenant, thread_id, 1):
        saved = saver.put(_config(thread_id), checkpoint, {"source": "loop", "step": 1}, versions)
    assert store.next_fence(tenant, thread_id) == 2

    with fence_scope(tenant, thread_id, 1):
        with pytest.raises(FencingViolation):
            saver.put_writes(saved, [("messages", "late")], "task-late")

    assert _counts(fenced_db, thread_id) == (1, 1, 0)
    with tenant_scope(tenant):
        loaded = saver.get_tuple(saved)
    assert loaded is not None
    assert loaded.pending_writes == []


def test_checkpoint_sql_failure_rolls_back_the_same_transaction(fenced_db):
    saver, store, tenant, thread_id = _begin(fenced_db)
    assert store.next_fence(tenant, thread_id) == 1
    broken, versions = _checkpoint(None, {"payload": {"n": 1}})

    with fence_scope(tenant, thread_id, 1):
        with pytest.raises(NotNullViolation) as caught:
            saver.put(_config(thread_id), broken, {"source": "loop", "step": 1}, versions)
    assert caught.value.sqlstate == "23502"
    assert _counts(fenced_db, thread_id) == (0, 0, 0)

    checkpoint, versions = _checkpoint("cp-after-rollback", {"payload": {"n": 2}})
    with fence_scope(tenant, thread_id, 1):
        saver.put(_config(thread_id), checkpoint, {"source": "loop", "step": 2}, versions)
    with tenant_scope(tenant):
        loaded = saver.get_tuple(_config(thread_id))
    assert loaded is not None
    assert loaded.checkpoint["channel_values"]["payload"] == {"n": 2}
    assert _counts(fenced_db, thread_id) == (1, 1, 0)


def test_two_savers_accept_only_the_current_fence(fenced_db):
    _saver, store, tenant, thread_id = _begin(fenced_db)
    saver_a = FencedPostgresSaver(fenced_db.resources.pool)
    saver_b = FencedPostgresSaver(fenced_db.second.pool)
    assert store.next_fence(tenant, thread_id) == 1
    assert store.next_fence(tenant, thread_id) == 2
    denied: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def write(saver: FencedPostgresSaver, fence: int, checkpoint_id: str, label: str) -> None:
        checkpoint, versions = _checkpoint(checkpoint_id, {"answer": label})
        try:
            barrier.wait(5)
            with fence_scope(tenant, thread_id, fence):
                saver.put(
                    _config(thread_id),
                    checkpoint,
                    {"source": "loop", "step": fence},
                    versions,
                )
        except FencingViolation:
            denied.append(label)
        except BaseException as exc:
            errors.append(exc)

    workers = [
        threading.Thread(target=write, args=(saver_a, 1, "cp-saver-a", "a")),
        threading.Thread(target=write, args=(saver_b, 2, "cp-saver-b", "b")),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
    assert errors == []
    assert denied == ["a"]
    with tenant_scope(tenant):
        assert saver_b.get_tuple(_config(thread_id)).checkpoint["id"] == "cp-saver-b"
    assert _counts(fenced_db, thread_id) == (1, 0, 0)


def test_rls_blocks_the_other_tenant_and_a_missing_guc(fenced_db):
    saver, store, tenant_a, thread_a = _begin(fenced_db)
    tenant_b = uuid4()
    conversation_id = uuid4()
    thread_b = f"kq:{tenant_b}:{conversation_id}"
    fenced_db.tenants.append(tenant_b)
    fenced_db.threads.append(thread_b)
    assert store.next_fence(tenant_b, thread_b) == 1
    with tenant_transaction(fenced_db.store_pool, str(tenant_a)) as connection:
        updated = connection.execute(
            "update agent.thread_run set fence = fence + 5 where thread_id = %s",
            (thread_b,),
        )
        visible = connection.execute(
            "select fence from agent.thread_run where thread_id = %s",
            (thread_b,),
        ).fetchall()
    assert updated.rowcount == 0
    assert visible == []
    assert store.check_fence(tenant_b, thread_b, 1) == 1
    with fenced_db.resources.pool.connection() as connection:
        bare = connection.execute(
            "update agent.thread_run set fence = fence + 5 where thread_id = %s",
            (thread_b,),
        )
        assert bare.rowcount == 0
    assert store.check_fence(tenant_b, thread_b, 1) == 1
    with pytest.raises(FenceValidationError):
        with fence_scope(tenant_a, thread_b, 1):
            pass
    checkpoint, versions = _checkpoint("cp-no-context")
    with pytest.raises(FenceContextError):
        saver.put(_config(thread_a), checkpoint, {"source": "loop", "step": 1}, versions)
    with tenant_scope(tenant_a):
        assert saver.get_tuple(_config(thread_a)) is None


@pytest.fixture(scope="module")
def fenced_db():
    env = _root_env()
    if not env.get("AGENT_APP_PASSWORD") or not env.get("AGENT_MIGRATOR_PASSWORD") or not env.get("POSTGRES_DB"):
        pytest.skip("仓库根目录 .env 未提供数据库口令，跳过真实数据库测试")
    settings = _settings(env)
    try:
        apply_migrations(settings)
        resources = _resources(settings)
        second = _resources(settings)
        resources.open()
        second.open()
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
    database = type(
        "FencedDatabase",
        (),
        {
            "resources": resources,
            "second": second,
            "store_pool": store_pool,
            "env": env,
            "threads": [],
            "tenants": [],
        },
    )()
    with resources.pool.connection() as connection:
        reject_superuser(connection)
        role = connection.execute(
            "select current_user, (select rolsuper from pg_roles where rolname = current_user)"
        ).fetchone()
        assert (role["current_user"], role["rolsuper"]) == ("agent_app", False)
    try:
        yield database
    finally:
        _cleanup(database)
        store_pool.close()
        resources.close()
        second.close()


def _begin(fenced_db):
    tenant = uuid4()
    thread_id = f"kq:{tenant}:{uuid4()}"
    fenced_db.tenants.append(tenant)
    fenced_db.threads.append(thread_id)
    saver = FencedPostgresSaver(fenced_db.resources.pool)
    store = ThreadFenceStore(fenced_db.store_pool)
    return saver, store, tenant, thread_id


def _counts(fenced_db, thread_id: str) -> tuple[int, int, int]:
    with fenced_db.resources.pool.connection() as connection:
        with connection.transaction():
            connection.execute("select set_config('app.tenant_id', %s, true)", (_tenant_of(thread_id),))
            return (
                _scalar(connection, "select count(*) from agent.checkpoints where thread_id = %s", (thread_id,)),
                _scalar(connection, "select count(*) from agent.checkpoint_blobs where thread_id = %s", (thread_id,)),
                _scalar(connection, "select count(*) from agent.checkpoint_writes where thread_id = %s", (thread_id,)),
            )


def _assert_guc_does_not_leak(fenced_db) -> None:
    with fenced_db.resources.pool.connection() as connection:
        current = _scalar(connection, "select current_setting('app.tenant_id', true)")
        visible = _scalar(connection, "select count(*) from agent.thread_run")
    assert current in (None, "")
    assert visible == 0


def _cleanup(fenced_db) -> None:
    threads = fenced_db.threads
    if threads and fenced_db.resources.pool is not None:
        with fenced_db.resources.pool.connection() as connection:
            for thread_id in threads:
                with connection.transaction():
                    connection.execute(
                        "select set_config('app.tenant_id', %s, true)",
                        (_tenant_of(thread_id),),
                    )
                    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                        connection.execute(
                            f"delete from agent.{table} where thread_id = %s",
                            (thread_id,),
                        )
    if fenced_db.tenants:
        with _connect(
            fenced_db.env,
            "agent_migrator",
            fenced_db.env["AGENT_MIGRATOR_PASSWORD"],
        ) as connection:
            connection.execute(
                "delete from agent.thread_run where tenant_id = any(%s)",
                (fenced_db.tenants,),
            )


def _resources(settings: Settings) -> CheckpointResources:
    return CheckpointResources(
        build_conninfo(
            settings.postgres_host,
            settings.postgres_port,
            settings.postgres_db,
            settings.agent_app_user,
            settings.agent_app_password,
        ),
        settings.agent_app_password,
    )


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


def _checkpoint(checkpoint_id: str | None, values: dict | None = None) -> tuple[dict, dict]:
    channel_values = values or {"answer": "ok", "payload": {"kept": True}}
    versions = {
        key: f"00000000000000000000000000000001.{index:016d}"
        for index, key in enumerate(channel_values, start=1)
    }
    checkpoint = {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-11-23T08:00:00.000000+00:00",
        "channel_values": channel_values,
        "channel_versions": versions,
        "versions_seen": {},
        "updated_channels": list(channel_values),
    }
    return checkpoint, versions


def _tenant_of(thread_id: str) -> str:
    _prefix, tenant, _conversation = thread_id.split(":")
    return tenant


def _scalar(connection, sql: str, params=None):
    row = connection.execute(sql, params).fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


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
