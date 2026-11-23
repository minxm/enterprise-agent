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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID

from app.persistence.thread_fence import FenceContextError, _expected, _identity

_CURRENT: contextvars.ContextVar[FenceContext | None] = contextvars.ContextVar(
    "agent_thread_fence",
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


def current_fence() -> FenceContext:
    """Return the fence bound to this task."""
    context = _CURRENT.get()
    if context is None:
        raise FenceContextError("执行上下文缺失")
    return context


def bind_current_context(function: Callable[..., T]) -> Callable[..., T]:
    """Capture the current context for a worker started with threading.Thread.

    Call this in the parent thread. The returned function runs `function`
    inside that copy, including the fence scope that is active now.
    """
    captured = contextvars.copy_context()

    def bound(*args: object, **kwargs: object) -> T:
        return captured.run(function, *args, **kwargs)

    return bound
