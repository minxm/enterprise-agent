"""Run-scoped preference text for one graph execution.

This object is passed as LangGraph runtime context. It is not checkpointed
state. A run that has already received it keeps that snapshot until it
finishes. Deleting, archiving, or expiring a memory does not cancel the run
and does not rewrite the checkpoint. The next run recalls again.

The context intentionally has no tenant, actor, permission, token, or ticket.
"""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.runtime import get_runtime


@dataclass(frozen=True)
class RunContext:
    memories: tuple[str, ...] = ()


def current_memories() -> tuple[str, ...]:
    """Read this run's preference text. Outside a graph run the result is empty."""
    try:
        runtime = get_runtime(RunContext)
    except Exception:
        return ()
    context = getattr(runtime, "context", None)
    if type(context) is not RunContext:
        return ()
    memories = context.memories
    if type(memories) is not tuple or any(type(item) is not str for item in memories):
        return ()
    return memories
