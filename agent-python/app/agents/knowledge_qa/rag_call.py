"""Call a RAG ask implementation without forcing test doubles to grow new parameters."""

from __future__ import annotations

import inspect


def ask_rag(
    rag_service: object,
    tenant_id: object,
    question: object,
    *,
    top_k: object,
    strategy: object,
    memories: tuple[str, ...],
) -> object:
    ask = rag_service.ask  # type: ignore[attr-defined]
    arguments: dict[str, object] = {"top_k": top_k, "strategy": strategy}
    if _accepts_memories(ask):
        arguments["memories"] = memories
    return ask(tenant_id, question, **arguments)


def _accepts_memories(ask: object) -> bool:
    try:
        signature = inspect.signature(ask)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if "memories" in signature.parameters:
        return True
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
