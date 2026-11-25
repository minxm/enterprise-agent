"""Trusted fence identity for one graph run.

The values live in a ContextVar, so two concurrent tasks do not share them.
A raw threading.Thread starts with an empty context. Copy the context in the
parent thread and run the worker inside that copy. asyncio.to_thread already
copies the caller's context. The SSE worker does the same with
contextvars.copy_context() before it starts the thread.

Nothing here reads the user question, the request body, or graph state.
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID

from app.persistence.thread_fence import FenceContextError, _expected, _identity

EXECUTION_TOKEN_KEY = "__agent_execution_token"

_CURRENT: contextvars.ContextVar[FenceContext | None] = contextvars.ContextVar(
    "agent_thread_fence",
    default=None,
)
_TENANT: contextvars.ContextVar[UUID | None] = contextvars.ContextVar(
    "agent_tenant",
    default=None,
)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class FenceContext:
    tenant_id: UUID
    thread_id: str
    fence: int


@contextmanager
def fence_scope(tenant_id: UUID | None, thread_id: str | None, fence: object) -> Iterator[FenceContext]:
    """Bind one validated fence for the current task and restore the previous one."""
    tenant, thread = _identity(tenant_id, thread_id)
    context = FenceContext(tenant, thread, _expected(fence))
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)


@contextmanager
def tenant_scope(tenant_id: UUID | None) -> Iterator[UUID]:
    """Bind the trusted tenant for checkpoint reads without allocating a fence.

    The value comes from the caller. It is not parsed out of a thread id.
    """
    if type(tenant_id) is not UUID:
        raise FenceContextError("租户上下文缺失")
    token = _TENANT.set(tenant_id)
    try:
        yield tenant_id
    finally:
        _TENANT.reset(token)


def scoped_tenant() -> UUID | None:
    """Return the tenant-only context when one is active."""
    return _TENANT.get()


def resolve_checkpoint_tenant(token: object = None) -> UUID:
    """Return the trusted tenant from the scope, the fence, or the execution token.

    A thread id is never accepted as the source of the tenant. Disagreeing
    sources fail closed instead of picking one.
    """
    found: list[UUID] = []
    scoped = _TENANT.get()
    if scoped is not None:
        found.append(scoped)
    current = _CURRENT.get()
    if current is not None:
        found.append(current.tenant_id)
    registered = _lookup_execution(token)
    if registered is not None:
        found.append(registered.tenant_id)
    if not found:
        raise FenceContextError("租户上下文缺失")
    if any(item != found[0] for item in found):
        raise FenceContextError("租户上下文不一致")
    return found[0]


def current_fence() -> FenceContext:
    """Return the fence bound to this task."""
    context = _CURRENT.get()
    if context is None:
        raise FenceContextError("执行上下文缺失")
    return context


_EXECUTIONS: dict[str, FenceContext] = {}
_EXECUTIONS_GUARD = threading.Lock()


def register_execution(token: str, tenant_id: UUID | None, thread_id: str | None, fence: object) -> None:
    """Remember the fence for checkpoint writes that run outside the graph thread.

    The token is minted by the service and placed on the runnable config. It is
    not a fence, and it is not accepted unless this process registered it.
    """
    if type(token) is not str or not token:
        raise FenceContextError("执行令牌不合法")
    tenant, thread = _identity(tenant_id, thread_id)
    context = FenceContext(tenant, thread, _expected(fence))
    with _EXECUTIONS_GUARD:
        _EXECUTIONS[token] = context


def unregister_execution(token: str) -> None:
    with _EXECUTIONS_GUARD:
        _EXECUTIONS.pop(token, None)


def resolve_execution_fence(token: object) -> FenceContext:
    """Use the task fence, or the server registry when the worker has none.

    A registry hit never replaces a different fence already bound to the task.
    """
    current = _CURRENT.get()
    registered = _lookup_execution(token)
    if current is not None and registered is not None and current != registered:
        raise FenceContextError("执行上下文不一致")
    if current is not None:
        return current
    if registered is not None:
        return registered
    raise FenceContextError("执行上下文缺失")


def _lookup_execution(token: object) -> FenceContext | None:
    if type(token) is not str or not token:
        return None
    with _EXECUTIONS_GUARD:
        return _EXECUTIONS.get(token)


def bind_current_context(function: Callable[..., T]) -> Callable[..., T]:
    """Capture the current context for a worker started with threading.Thread.

    Call this in the parent thread. The returned function runs `function`
    inside that copy, including the fence scope that is active now.
    """
    captured = contextvars.copy_context()

    def bound(*args: object, **kwargs: object) -> T:
        return captured.run(function, *args, **kwargs)

    return bound
