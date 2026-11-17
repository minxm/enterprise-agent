"""Answer a question from tenant-scoped knowledge passages.

Retrieval and chat stay behind their existing clients. This service does not
embed text, open a database connection, or trust a tenant id supplied as text.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.core.config import EMBEDDING_MODEL_NAME
from app.knowledge.prompts import INSUFFICIENT_KNOWLEDGE, SYSTEM_PROMPT, build_user_prompt, limit_passages
from app.knowledge.retrieval import MAX_TOP_K, RetrievalHit
from app.models.chat import ChatConfigurationError, ChatResponseError


class RAGValidationError(ValueError):
    """The question request itself is not acceptable."""


@dataclass(frozen=True)
class RAGSource:
    document_id: UUID
    chunk_id: UUID
    chunk_index: int
    content: str
    similarity: float
    embedding_model: str
    char_start: int | None
    char_end: int | None


@dataclass(frozen=True)
class RAGAnswer:
    answer: str
    sources: tuple[RAGSource, ...]
    retrieval_count: int
    model: str


class RAGService:
    def __init__(self, retriever: object, chat_client: object) -> None:
        self._retriever = retriever
        self._chat_client = chat_client

    def ask(self, tenant_id: UUID, question: str, *, top_k: int = 5) -> RAGAnswer:
        tenant = _require_tenant(tenant_id)
        normalized = _require_question(question)
        limit = _require_top_k(top_k)
        model_name = _require_model_name(self._chat_client)
        hits = self._retriever.search(tenant, normalized, top_k=limit)  # type: ignore[attr-defined]
        passages = tuple(hits)
        if not passages:
            return RAGAnswer(
                answer=INSUFFICIENT_KNOWLEDGE,
                sources=(),
                retrieval_count=0,
                model=model_name,
            )
        selected = tuple(limit_passages(passages))
        user_prompt = build_user_prompt(normalized, selected)
        raw_answer = self._chat_client.complete(system=SYSTEM_PROMPT, user=user_prompt)  # type: ignore[attr-defined]
        return RAGAnswer(
            answer=_require_answer(raw_answer),
            sources=tuple(_source(hit) for hit in selected),
            retrieval_count=len(passages),
            model=model_name,
        )


def _source(hit: RetrievalHit) -> RAGSource:
    return RAGSource(
        document_id=hit.document_id,
        chunk_id=hit.chunk_id,
        chunk_index=hit.chunk_index,
        content=hit.content,
        similarity=hit.similarity,
        embedding_model=hit.embedding_model,
        char_start=hit.char_start,
        char_end=hit.char_end,
    )


def _require_tenant(tenant_id: object) -> UUID:
    if not isinstance(tenant_id, UUID):
        raise RAGValidationError("tenant_id 必须是 UUID")
    return tenant_id


def _require_question(question: object) -> str:
    if not isinstance(question, str) or not question.strip():
        raise RAGValidationError("问题不能为空")
    return question.strip()


def _require_top_k(top_k: object) -> int:
    if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
        raise RAGValidationError(f"top_k 必须是 1 到 {MAX_TOP_K} 之间的整数")
    return top_k


def _require_model_name(chat_client: object) -> str:
    model_name = getattr(chat_client, "model_name", "")
    if not isinstance(model_name, str) or not model_name.strip():
        raise ChatConfigurationError("CHAT_MODEL 不能为空")
    if model_name == EMBEDDING_MODEL_NAME:
        raise ChatConfigurationError("CHAT_MODEL 不能使用 Embedding 模型")
    return model_name


def _require_answer(answer: object) -> str:
    if not isinstance(answer, str) or not answer.strip():
        raise ChatResponseError("模型输出为空")
    return answer.strip()
