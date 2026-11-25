import asyncio
import contextvars
import threading
from uuid import uuid4

import pytest
from psycopg import OperationalError

from app.persistence.fence_context import (
    bind_current_context,
    current_fence,
    fence_scope,
    resolve_checkpoint_tenant,
    tenant_scope,
)
from app.persistence.fenced_saver import (
    FenceIntegrityError,
    FencedPostgresSaver,
    FencingViolation,
)
from app.persistence.thread_fence import FenceContextError, FenceStoreError, FenceValidationError
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.types import INTERRUPT


class _Cursor:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.params: list[object] = []
        self.rowcount = 1
        self.update_error: Exception | None = None
        self.checkpoint_error: Exception | None = None
        self._row: dict[str, str] | None = None
        self._guc = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        self.statements.append(statement)
        self.params.append(params)
        if "set_config" in statement:
            self._guc = str(params[0])
            self._row = {"set_config": self._guc}
            return self
        if "current_setting" in statement:
            self._row = {"current_setting": self._guc}
            return self
        if "update agent.thread_run" in statement:
            if self.update_error is not None:
                raise self.update_error
            return self
        if "INSERT INTO checkpoints" in statement and self.checkpoint_error is not None:
            raise self.checkpoint_error
        return self

    def executemany(self, statement, _params):
        self.statements.append(statement)
        if "checkpoint_writes" in statement and self.checkpoint_error is not None:
            raise self.checkpoint_error
        return self

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self.cursor_obj = cursor
        self.committed = False
        self.rolled_back = False
        self.depth = 0

    def transaction(self):
        return self

    def cursor(self, **_kwargs):
        return self.cursor_obj

    def __enter__(self):
        self.depth += 1
        return self

    def __exit__(self, exc_type, *_args):
        self.depth -= 1
        if exc_type is None:
            self.committed = True
        else:
            self.rolled_back = True
        return False


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection_obj = connection

    def connection(self):
        return self.connection_obj


def _saver(cursor: _Cursor, monkeypatch) -> tuple[FencedPostgresSaver, _Connection]:
    connection = _Connection(cursor)
    monkeypatch.setattr(
        "app.persistence.fenced_saver.get_connection",
        lambda _conn: _Pool(connection).connection(),
    )
    return FencedPostgresSaver(object()), connection


def _thread(tenant) -> str:
    return f"kq:{tenant}:{uuid4()}"


def _config(thread_id: str, checkpoint_id: str | None = None) -> dict:
    configurable = {"thread_id": thread_id, "checkpoint_ns": ""}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def _checkpoint(checkpoint_id: str, values: dict | None = None) -> tuple[dict, dict]:
    channel_values = values or {"answer": "ok", "payload": {"n": 1}}
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


def test_matching_fence_writes_checkpoint_sql_and_returns_the_parent_config(monkeypatch):
    tenant = uuid4()
    thread_id = _thread(tenant)
    cursor = _Cursor()
    saver, connection = _saver(cursor, monkeypatch)
    checkpoint, versions = _checkpoint("cp-1")
    original = checkpoint["channel_values"]["payload"]

    def parent_put(*_args, **_kwargs):
        raise AssertionError("parent put")

    monkeypatch.setattr(PostgresSaver, "put", parent_put)
    with fence_scope(tenant, thread_id, 1):
        saved = saver.put(
            _config(thread_id, "client-fence-ignored"),
            checkpoint,
            {"source": "loop", "step": 2, "fence": 99},
            versions,
        )

    assert saved == {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
            "checkpoint_id": "cp-1",
        }
    }
    assert connection.committed is True
    assert connection.rolled_back is False
    assert checkpoint["channel_values"]["payload"] is original
    update_at = next(i for i, sql in enumerate(cursor.statements) if "update agent.thread_run" in sql)
    insert_at = next(i for i, sql in enumerate(cursor.statements) if "INSERT INTO checkpoints" in sql)
    assert update_at < insert_at
    assert saver.UPSERT_CHECKPOINT_BLOBS_SQL in cursor.statements
    assert cursor.params[update_at] == (tenant, thread_id, 1)


def test_stale_fence_does_not_write_a_checkpoint(monkeypatch):
    tenant = uuid4()
    thread_id = _thread(tenant)
    cursor = _Cursor()
    cursor.rowcount = 0
    saver, connection = _saver(cursor, monkeypatch)
    checkpoint, versions = _checkpoint("cp-stale")

    with fence_scope(tenant, thread_id, 1):
        with pytest.raises(FencingViolation):
            saver.put(_config(thread_id), checkpoint, {"source": "loop", "step": 1}, versions)

    assert connection.rolled_back is True
    assert connection.committed is False
    assert not any("INSERT INTO checkpoints" in sql for sql in cursor.statements)


def test_matching_and_stale_fences_control_put_writes(monkeypatch):
    tenant = uuid4()
    thread_id = _thread(tenant)
    cursor = _Cursor()
    saver, connection = _saver(cursor, monkeypatch)
    config = _config(thread_id, "cp-1")

    def parent_put_writes(*_args, **_kwargs):
        raise AssertionError("parent put_writes")

    monkeypatch.setattr(PostgresSaver, "put_writes", parent_put_writes)
    with fence_scope(tenant, thread_id, 4):
        assert saver.put_writes(config, [("messages", "hello")], "task-1", "path") is None
        assert saver.put_writes(config, [(INTERRUPT, {"value": 1})], "task-2") is None
    assert saver.INSERT_CHECKPOINT_WRITES_SQL in cursor.statements
    assert saver.UPSERT_CHECKPOINT_WRITES_SQL in cursor.statements
    assert connection.committed is True

    cursor.statements.clear()
    cursor.rowcount = 0
    connection.committed = False
    with fence_scope(tenant, thread_id, 3):
        with pytest.raises(FencingViolation):
            saver.put_writes(config, [("messages", "late")], "task-3")
    assert cursor.statements
    assert not any("checkpoint_writes" in sql for sql in cursor.statements)
    assert connection.rolled_back is True


def test_database_errors_stay_database_errors(monkeypatch):
    tenant = uuid4()
    thread_id = _thread(tenant)
    cursor = _Cursor()
    cursor.update_error = OperationalError("db unavailable")
    saver, connection = _saver(cursor, monkeypatch)
    checkpoint, versions = _checkpoint("cp-db")

    with fence_scope(tenant, thread_id, 1):
        with pytest.raises(OperationalError):
            saver.put(_config(thread_id), checkpoint, {"source": "loop", "step": 1}, versions)

    assert connection.rolled_back is True
    assert not any("INSERT INTO checkpoints" in sql for sql in cursor.statements)


def test_checkpoint_sql_failure_rolls_the_fence_update_back(monkeypatch):
    tenant = uuid4()
    thread_id = _thread(tenant)
    cursor = _Cursor()
    cursor.checkpoint_error = RuntimeError("checkpoint failed")
    saver, connection = _saver(cursor, monkeypatch)
    checkpoint, versions = _checkpoint("cp-boom")

    with fence_scope(tenant, thread_id, 1):
        with pytest.raises(RuntimeError, match="checkpoint failed"):
            saver.put(_config(thread_id), checkpoint, {"source": "loop", "step": 1}, versions)

    assert any("update agent.thread_run" in sql for sql in cursor.statements)
    assert connection.rolled_back is True
    assert connection.committed is False


def test_missing_or_foreign_context_refuses_before_sql(monkeypatch):
    tenant = uuid4()
    thread_id = _thread(tenant)
    other = _thread(tenant)
    cursor = _Cursor()
    saver, _connection = _saver(cursor, monkeypatch)
    checkpoint, versions = _checkpoint("cp-none")

    def unexpected(_conn):
        raise AssertionError("connection borrowed")

    monkeypatch.setattr("app.persistence.fenced_saver.get_connection", unexpected)
    with pytest.raises(FenceContextError):
        saver.put(_config(thread_id), checkpoint, {"source": "loop", "step": 1}, versions)
    with fence_scope(tenant, thread_id, 1):
        with pytest.raises(FenceContextError):
            saver.put(_config(other), checkpoint, {"source": "loop", "step": 1}, versions)
    with pytest.raises(FenceValidationError):
        with fence_scope(tenant, other, True):
            pass
    assert cursor.statements == []


def test_too_many_fence_rows_are_an_integrity_error(monkeypatch):
    tenant = uuid4()
    thread_id = _thread(tenant)
    cursor = _Cursor()
    cursor.rowcount = 2
    saver, connection = _saver(cursor, monkeypatch)
    checkpoint, versions = _checkpoint("cp-many")

    with fence_scope(tenant, thread_id, 1):
        with pytest.raises(FenceIntegrityError):
            saver.put(_config(thread_id), checkpoint, {"source": "loop", "step": 1}, versions)

    assert connection.rolled_back is True
    assert not any("INSERT INTO checkpoints" in sql for sql in cursor.statements)


def test_a_failed_guc_is_not_a_fencing_violation(monkeypatch):
    tenant = uuid4()
    thread_id = _thread(tenant)
    cursor = _Cursor()
    saver, connection = _saver(cursor, monkeypatch)
    cursor._guc = "other"
    checkpoint, versions = _checkpoint("cp-guc")

    def execute(statement, params=None):
        cursor.statements.append(statement)
        if "set_config" in statement:
            cursor._row = {"set_config": "other"}
            return cursor
        if "current_setting" in statement:
            cursor._row = {"current_setting": "other"}
            return cursor
        raise AssertionError(statement)

    cursor.execute = execute
    with fence_scope(tenant, thread_id, 1):
        with pytest.raises(FenceStoreError):
            saver.put(_config(thread_id), checkpoint, {"source": "loop", "step": 1}, versions)
    assert connection.rolled_back is True


def test_concurrent_contexts_do_not_trade_fences():
    tenant = uuid4()
    thread_id = _thread(tenant)

    async def read(fence: int, found: list[int]) -> None:
        with fence_scope(tenant, thread_id, fence):
            await asyncio.sleep(0.02)
            found.append(current_fence().fence)

    found: list[int] = []

    async def both() -> None:
        await asyncio.gather(read(1, found), read(2, found))

    asyncio.run(both())
    assert sorted(found) == [1, 2]

    seen: dict[str, int | None] = {}

    def read_sync() -> None:
        try:
            seen["value"] = current_fence().fence
        except FenceContextError:
            seen["value"] = None

    with fence_scope(tenant, thread_id, 7):
        captured = contextvars.copy_context()
        bare = threading.Thread(target=read_sync)
        bare.start()
        bare.join()
        assert seen["value"] is None
        copied = threading.Thread(target=bind_current_context(read_sync))
        copied.start()
        copied.join()
        assert seen["value"] == 7

        def produce() -> None:
            def run() -> None:
                seen["sse"] = current_fence().fence

            captured.run(run)

        worker = threading.Thread(target=produce)
        worker.start()
        worker.join()
    assert seen["sse"] == 7

    async def through_worker() -> int:
        with fence_scope(tenant, thread_id, 8):
            return await asyncio.to_thread(lambda: current_fence().fence)

    assert asyncio.run(through_worker()) == 8


def test_tenant_scope_restores_and_does_not_cross_a_bare_thread():
    tenant = uuid4()
    other = uuid4()
    with tenant_scope(tenant):
        with tenant_scope(other):
            assert resolve_checkpoint_tenant() == other
        assert resolve_checkpoint_tenant() == tenant
    with pytest.raises(FenceContextError, match="租户上下文缺失"):
        resolve_checkpoint_tenant()

    seen: dict[str, object] = {}

    def read() -> None:
        try:
            seen["tenant"] = resolve_checkpoint_tenant()
        except FenceContextError:
            seen["tenant"] = None

    with tenant_scope(tenant):
        bare = threading.Thread(target=read)
        bare.start()
        bare.join()
        assert seen["tenant"] is None
        copied = threading.Thread(target=bind_current_context(read))
        copied.start()
        copied.join()
        assert seen["tenant"] == tenant

    async def through_worker() -> object:
        with tenant_scope(tenant):
            return await asyncio.to_thread(resolve_checkpoint_tenant)

    assert asyncio.run(through_worker()) == tenant


def test_checkpoint_reads_without_a_tenant_do_not_borrow_a_connection(monkeypatch):
    tenant = uuid4()
    thread_id = _thread(tenant)
    saver, _connection = _saver(_Cursor(), monkeypatch)

    def unexpected(_conn):
        raise AssertionError("connection borrowed")

    monkeypatch.setattr("app.persistence.fenced_saver.get_connection", unexpected)
    with pytest.raises(FenceContextError, match="租户上下文缺失"):
        saver.get_tuple(_config(thread_id))
    with pytest.raises(FenceContextError, match="租户上下文缺失"):
        list(saver.list(_config(thread_id)))
    with pytest.raises(FenceContextError, match="租户上下文缺失"):
        saver.delete_thread(thread_id)
    with pytest.raises(FenceContextError, match="租户上下文缺失"):
        saver.prune([thread_id], strategy="delete")
    with pytest.raises(FenceContextError, match="租户上下文缺失"):
        saver.get_delta_channel_history(config=_config(thread_id), channels=["messages"])
    with pytest.raises(RuntimeError, match="Checkpointer DDL"):
        saver.setup()


def test_get_tuple_and_list_keep_the_tenant_transaction_open(monkeypatch):
    tenant = uuid4()
    thread_id = _thread(tenant)
    cursor = _Cursor()
    answers = [{"set_config": str(tenant)}, {"current_setting": str(tenant)}, None]

    def fetchone():
        if answers:
            return answers.pop(0)
        return {"current_setting": str(tenant)}

    cursor.fetchone = fetchone
    saver, connection = _saver(cursor, monkeypatch)
    depths: list[int] = []
    original = cursor.execute

    def execute(statement, params=None):
        depths.append(connection.depth)
        return original(statement, params)

    cursor.execute = execute
    with tenant_scope(tenant):
        assert saver.get_tuple(_config(thread_id)) is None
    assert depths and all(item > 0 for item in depths)
    assert connection.depth == 0

    cursor.fetchall = lambda: [{"checkpoint": {"v": 4}, "parent_checkpoint_id": None}]
    saver._load_checkpoint_tuple = lambda _value: connection.depth
    with tenant_scope(tenant):
        loaded = list(saver.list(_config(thread_id), limit=1))
    assert loaded == [2]
    assert connection.depth == 0


def test_delete_and_prune_require_the_current_tenant(monkeypatch):
    tenant = uuid4()
    thread_id = _thread(tenant)
    foreign = _thread(uuid4())
    cursor = _Cursor()
    saver, connection = _saver(cursor, monkeypatch)
    with tenant_scope(tenant):
        with pytest.raises(FenceContextError, match="不一致"):
            saver.delete_thread(foreign)
        with pytest.raises(FenceContextError, match="不一致"):
            saver.prune([foreign], strategy="delete")
    assert cursor.statements == []
    with tenant_scope(tenant):
        saver.delete_thread(thread_id)
        saver.prune([thread_id], strategy="delete")
    assert any(sql.startswith("DELETE FROM checkpoints") for sql in cursor.statements)
    assert any(sql.startswith("DELETE FROM checkpoint_blobs") for sql in cursor.statements)
    assert any(sql.startswith("DELETE FROM checkpoint_writes") for sql in cursor.statements)
    assert connection.depth == 0
    with tenant_scope(tenant):
        with pytest.raises(FenceContextError, match="尚未纳入租户事务"):
            saver.get_delta_channel_history(config=_config(thread_id), channels=["messages"])


def test_a_different_tenant_scope_cannot_use_another_fence(monkeypatch):
    tenant = uuid4()
    thread_id = _thread(tenant)
    cursor = _Cursor()
    saver, _connection = _saver(cursor, monkeypatch)
    checkpoint, versions = _checkpoint("cp-scope")
    with tenant_scope(uuid4()):
        with fence_scope(tenant, thread_id, 1):
            with pytest.raises(FenceContextError, match="租户上下文不一致"):
                saver.put(_config(thread_id), checkpoint, {"source": "loop", "step": 1}, versions)
    assert cursor.statements == []

