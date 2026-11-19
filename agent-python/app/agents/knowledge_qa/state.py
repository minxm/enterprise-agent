"""State carried between Knowledge QA graph nodes.

The state holds the current request and its result. It does not hold API keys,
database connections, or model clients. tenant_id is accepted only after the
caller has already resolved a server-side UUID.
"""

from __future__ import annotations

from typing import TypedDict

# The latest checkpoint keeps this many completed turns. Older turns are dropped.
# Each turn stores the question, answer, status, and source ids, not the full
# retrieval payload.
MAX_CONVERSATION_TURNS = 20


class ConversationTurn(TypedDict):
    question: str
    answer: str
    status: str
    source_ids: list[str]


class KnowledgeQAState(TypedDict, total=False):
    tenant_id: str
    question: str
    top_k: int
    retrieval_strategy: str
    requested_intent: str | None
    intent: str
    rag_result: dict[str, object] | None
    answer: str
    sources: list[dict[str, object]]
    retrieval_count: int
    requested_strategy: str | None
    effective_strategy: str | None
    degraded: bool
    degradation_reason: str | None
    status: str
    error_code: str | None
    error_message: str | None
    request_id: str | None
    turns: list[ConversationTurn]
