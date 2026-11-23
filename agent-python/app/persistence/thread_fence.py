"""Atomic fencing counters for one tenant and one Knowledge QA thread.

The counter lives in agent.thread_run. A caller borrows the existing agent_app
pool for a single transaction, then returns the connection. This module does
not lock a thread and does not write checkpoints.
"""

from __future__ import annotations

from uuid import UUID

from psycopg_pool import ConnectionPool

from app.persistence.postgres import tenant_transaction

_UPSERT = """
insert into agent.thread_run (tenant_id, thread_id, fence)
values (%s, %s, 1)
on conflict (tenant_id, thread_id)
do update set fence = agent.thread_run.fence + 1
returning fence
"""

_READ = """
select fence
from agent.thread_run
where tenant_id = %s and thread_id = %s
"""


class FenceError(Exception):
    """Base error for fencing reads and writes."""


class FenceValidationError(FenceError):
    """The tenant, thread, or expected fence is not usable."""


class FenceContextError(FenceError):
    """The caller did not supply a tenant for the transaction."""


class FenceNotFound(FenceError):
    """This tenant has no fence for the thread."""


class FenceMismatch(FenceError):
    """The expected fence is not the current fence."""


class FenceStoreError(FenceError):
    """PostgreSQL could not finish the transaction."""


class ThreadFenceStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def next_fence(self, tenant_id: UUID | None, thread_id: str | None) -> int:
        """Allocate the next fence and return it only after the transaction commits."""
        tenant, thread = _identity(tenant_id, thread_id)
        try:
            with tenant_transaction(self._pool, str(tenant)) as connection:
                row = connection.execute(_UPSERT, (tenant, thread)).fetchone()
                if row is None or row[0] is None:
                    raise FenceStoreError("数据库事务失败")
                fence = int(row[0])
        except FenceError:
            raise
        except Exception as exc:
            raise _store_error(exc) from None
        return fence

    def check_fence(self, tenant_id: UUID | None, thread_id: str | None, expected_fence: object) -> int:
        """Return the fence when it is current for this tenant and thread."""
        tenant, thread = _identity(tenant_id, thread_id)
        expected = _expected(expected_fence)
        try:
            with tenant_transaction(self._pool, str(tenant)) as connection:
                row = connection.execute(_READ, (tenant, thread)).fetchone()
        except FenceError:
            raise
        except Exception as exc:
            raise _store_error(exc) from None
        if row is None:
            raise FenceNotFound("fence 不存在")
        current = int(row[0])
        if current != expected:
            raise FenceMismatch("fence 不匹配")
        return current


def _identity(tenant_id: UUID | None, thread_id: str | None) -> tuple[UUID, str]:
    if tenant_id is None:
        raise FenceContextError("租户上下文缺失")
    if type(tenant_id) is not UUID:
        raise FenceValidationError("tenant_id 不合法")
    if type(thread_id) is not str:
        raise FenceValidationError("thread_id 不合法")
    prefix = f"kq:{tenant_id}:"
    if not thread_id.startswith(prefix):
        raise FenceValidationError("thread_id 不合法")
    try:
        conversation_id = UUID(thread_id[len(prefix):])
    except ValueError:
        raise FenceValidationError("thread_id 不合法") from None
    canonical = f"kq:{tenant_id}:{conversation_id}"
    if thread_id != canonical:
        raise FenceValidationError("thread_id 不合法")
    return tenant_id, canonical


def _expected(value: object) -> int:
    if type(value) is not int or value < 1:
        raise FenceValidationError("fence 不合法")
    return value


def _store_error(exc: Exception) -> FenceStoreError:
    return FenceStoreError(f"数据库事务失败: {type(exc).__name__}")
