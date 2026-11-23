from uuid import UUID, uuid4

import pytest
from psycopg import OperationalError

from app.persistence.thread_fence import (
    FenceContextError,
    FenceMismatch,
    FenceNotFound,
    FenceStoreError,
    FenceValidationError,
    ThreadFenceStore,
)


class _Cursor:
    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args):
        return self

    def fetchone(self):
        return (self._tenant_id,)


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, *, row=None, error: Exception | None = None, fail_on_exit: bool = False) -> None:
        self.row = row
        self.error = error
        self.fail_on_exit = fail_on_exit
        self.statements: list[str] = []
        self.tenant_id = ""

    def cursor(self):
        return _Cursor(self.tenant_id)

    def execute(self, statement, _params=None):
        if self.error is not None:
            raise self.error
        self.statements.append(statement)
        return _Result(self.row)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_args):
        if exc_type is None and self.fail_on_exit:
            raise OperationalError("commit failed password=secret")
        return False


class _CountingConnection(_Connection):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[tuple[str, str], int] = {}

    def execute(self, statement, params=None):
        if "on conflict" in statement:
            key = (str(params[0]), params[1])
            self.values[key] = self.values.get(key, 0) + 1
            return _Result((self.values[key],))
        if statement.strip().startswith("select fence"):
            key = (str(params[0]), params[1])
            if key not in self.values:
                return _Result(None)
            return _Result((self.values[key],))
        return super().execute(statement, params)


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection_obj = connection

    def connection(self):
        return self.connection_obj


def _store(connection: _Connection) -> ThreadFenceStore:
    return ThreadFenceStore(_Pool(connection))  # type: ignore[arg-type]


def _thread(tenant_id: UUID, conversation_id: UUID | None = None) -> str:
    return f"kq:{tenant_id}:{conversation_id or uuid4()}"


def test_next_fence_returns_the_database_value_after_commit():
    tenant_id = uuid4()
    connection = _Connection(row=(1,))
    connection.tenant_id = str(tenant_id)

    fence = _store(connection).next_fence(tenant_id, _thread(tenant_id))

    assert fence == 1
    assert "on conflict (tenant_id, thread_id)" in connection.statements[0]
    assert "agent.thread_run.fence + 1" in connection.statements[0]


def test_check_fence_accepts_only_the_current_value():
    tenant_id = uuid4()
    connection = _Connection(row=(2,))
    connection.tenant_id = str(tenant_id)
    store = _store(connection)
    thread_id = _thread(tenant_id)

    assert store.check_fence(tenant_id, thread_id, 2) == 2
    connection.row = None
    with pytest.raises(FenceNotFound):
        store.check_fence(tenant_id, thread_id, 2)
    connection.row = (3,)
    with pytest.raises(FenceMismatch):
        store.check_fence(tenant_id, thread_id, 2)


def test_threads_and_tenants_are_separate_parameters():
    first = uuid4()
    second = uuid4()
    conversation_id = uuid4()
    connection = _Connection(row=(4,))
    connection.tenant_id = str(first)
    store = _store(connection)

    store.next_fence(first, _thread(first, conversation_id))
    connection.tenant_id = str(second)
    store.next_fence(second, _thread(second, conversation_id))

    assert connection.statements
    assert _thread(first, conversation_id) != _thread(second, conversation_id)


def test_invalid_identity_and_fence_are_rejected_before_sql():
    tenant_id = uuid4()
    connection = _Connection(row=(1,))
    store = _store(connection)
    with pytest.raises(FenceContextError):
        store.next_fence(None, _thread(tenant_id))
    with pytest.raises(FenceValidationError):
        store.next_fence("not-a-uuid", _thread(tenant_id))  # type: ignore[arg-type]
    with pytest.raises(FenceValidationError):
        store.next_fence(tenant_id, "kq:other")
    with pytest.raises(FenceValidationError):
        store.next_fence(tenant_id, f"KQ:{tenant_id}:{uuid4()}")
    with pytest.raises(FenceValidationError):
        store.check_fence(tenant_id, _thread(tenant_id), True)  # type: ignore[arg-type]
    with pytest.raises(FenceValidationError):
        store.check_fence(tenant_id, _thread(tenant_id), 0)
    assert connection.statements == []


def test_same_thread_increments_and_other_threads_do_not():
    tenant_id = uuid4()
    other = uuid4()
    conversation_id = uuid4()
    connection = _CountingConnection()
    connection.tenant_id = str(tenant_id)
    store = ThreadFenceStore(_Pool(connection))  # type: ignore[arg-type]

    assert store.next_fence(tenant_id, _thread(tenant_id, conversation_id)) == 1
    assert store.next_fence(tenant_id, _thread(tenant_id, conversation_id)) == 2
    assert store.next_fence(tenant_id, _thread(tenant_id)) == 1
    connection.tenant_id = str(other)
    assert store.next_fence(other, _thread(other, conversation_id)) == 1
    assert store.check_fence(tenant_id, _thread(tenant_id, conversation_id), 2) == 2


def test_database_errors_are_not_reported_as_a_fence_mismatch():
    tenant_id = uuid4()
    connection = _Connection(error=OperationalError("password=secret host=db"))
    connection.tenant_id = str(tenant_id)
    store = _store(connection)

    with pytest.raises(FenceStoreError) as caught:
        store.next_fence(tenant_id, _thread(tenant_id))
    with pytest.raises(FenceStoreError) as checked:
        store.check_fence(tenant_id, _thread(tenant_id), 1)

    assert "secret" not in str(caught.value)
    assert "secret" not in str(checked.value)
    assert "host=db" not in str(caught.value)
    assert type(caught.value) is not FenceMismatch


def test_a_failed_commit_does_not_return_a_fence():
    tenant_id = uuid4()
    connection = _Connection(row=(7,), fail_on_exit=True)
    connection.tenant_id = str(tenant_id)

    with pytest.raises(FenceStoreError) as caught:
        _store(connection).next_fence(tenant_id, _thread(tenant_id))

    assert "7" not in str(caught.value)
    assert "secret" not in str(caught.value)
