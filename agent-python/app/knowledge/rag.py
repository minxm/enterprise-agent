"""Answer a question from tenant-scoped knowledge passages.

Retrieval and chat stay behind their existing clients. This service does not
embed text, open a database connection, or trust a tenant id supplied as text.
The default path still calls the injected retriever directly. Hybrid and
rerank are used only when a strategy catalog is supplied and selected.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from uuid import UUID

from app.core.config import EMBEDDING_MODEL_NAME, RAG_RETRIEVAL_STRATEGIES
from app.knowledge.prompts import INSUFFICIENT_KNOWLEDGE, SYSTEM_PROMPT, build_user_prompt, limit_passages
from app.knowledge.retrieval import MAX_TOP_K
from app.knowledge.strategies import RetrievalStrategies, RetrievedPassage, SearchOutcome, StrategyValidationError, to_passage
from app.models.chat import ChatConfigurationError, ChatResponseError


class RAGValidationError(ValueError):
    """The question request itself is not acceptable."""


_OMITTED = object()


@dataclass(frozen=True)
class RAGSource:
    document_id: UUID
    chunk_id: UUID
    chunk_index: int
    content: str
    similarity: float | None
    embedding_model: str | None
    char_start: int | None
    char_end: int | None
    vector_rank: int | None = None
    keyword_rank: int | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None


@dataclass(frozen=True)
class RAGAnswer:
    answer: str
    sources: tuple[RAGSource, ...]
    retrieval_count: int
    model: str
    retrieval_strategy: str = "vector"
    effective_strategy: str = "vector"
    degraded: bool = False
    degradation_reason: str | None = None
    candidate_count: int = 0
    context_count: int = 0


@dataclass(frozen=True)
class RAGTiming:
    retrieval_seconds: float
    rerank_seconds: float | None
    chat_seconds: float
    total_seconds: float
    candidate_count: int
    context_count: int
    retrieved_ids: tuple[str, ...]


class RAGService:
    def __init__(
        self,
        retriever: object,
        chat_client: object,
        *,
        strategies: RetrievalStrategies | None = None,
        default_strategy: str = "vector",
        default_top_k: int = 5,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if default_strategy not in RAG_RETRIEVAL_STRATEGIES:
            raise RAGValidationError("默认检索策略必须是 vector、hybrid 或 hybrid_rerank")
        self._retriever = retriever
        self._chat_client = chat_client
        self._strategies = strategies
        self._default_strategy = default_strategy
        self._default_top_k = _require_top_k(default_top_k)
        self._clock = clock or perf_counter
        self.last_timing: RAGTiming | None = None

    def ask(
        self,
        tenant_id: UUID,
        question: str,
        *,
        top_k: int | object = _OMITTED,
        strategy: str | None = None,
        memories: tuple[str, ...] = (),
    ) -> RAGAnswer:
        tenant, normalized, limit, selected_strategy = normalize_rag_request(
            tenant_id,
            question,
            top_k,
            strategy,
            default_top_k=self._default_top_k,
            default_strategy=self._default_strategy,
        )
        model_name = _require_model_name(self._chat_client)
        started = self._clock()
        outcome = self._retrieve(tenant, normalized, limit, selected_strategy)
        retrieved_ids = tuple(str(item.chunk_id) for item in outcome.passages)
        if not outcome.passages:
            self.last_timing = _timing(outcome, 0.0, self._clock() - started, 0, retrieved_ids)
            return _empty_answer(model_name, outcome)
        selected = tuple(limit_passages(outcome.passages))
        user_prompt = build_user_prompt(normalized, selected, memories=_preference_lines(memories))
        chat_started = self._clock()
        raw_answer = self._chat_client.complete(system=SYSTEM_PROMPT, user=user_prompt)  # type: ignore[attr-defined]
        chat_seconds = self._clock() - chat_started
        self.last_timing = _timing(outcome, chat_seconds, self._clock() - started, len(selected), retrieved_ids)
        return RAGAnswer(
            answer=_require_answer(raw_answer),
            sources=tuple(_source(hit) for hit in selected),
            retrieval_count=len(outcome.passages),
            model=model_name,
            retrieval_strategy=outcome.requested_strategy,
            effective_strategy=outcome.effective_strategy,
            degraded=outcome.degraded,
            degradation_reason=outcome.degradation_reason,
            candidate_count=outcome.candidate_count,
            context_count=len(selected),
        )

    def _retrieve(self, tenant: UUID, question: str, top_k: int, strategy: str) -> SearchOutcome:
        if self._strategies is None:
            if strategy != "vector":
                raise RAGValidationError("未配置该检索策略")
            started = self._clock()
            hits = self._retriever.search(tenant, question, top_k=top_k)  # type: ignore[attr-defined]
            passages = tuple(to_passage(hit) for hit in list(hits))
            return SearchOutcome(
                passages,
                "vector",
                "vector",
                len(passages),
                False,
                None,
                self._clock() - started,
                None,
            )
        try:
            return self._strategies.search(tenant, question, top_k=top_k, strategy=strategy)
        except StrategyValidationError as exc:
            raise RAGValidationError(str(exc)) from exc


def _source(hit: RetrievedPassage) -> RAGSource:
    return RAGSource(
        document_id=hit.document_id,
        chunk_id=hit.chunk_id,
        chunk_index=hit.chunk_index,
        content=hit.content,
        similarity=hit.similarity,
        embedding_model=hit.embedding_model,
        char_start=hit.char_start,
        char_end=hit.char_end,
        vector_rank=hit.vector_rank,
        keyword_rank=hit.keyword_rank,
        rrf_score=hit.rrf_score,
        rerank_score=hit.rerank_score,
    )


def _empty_answer(model_name: str, outcome: SearchOutcome) -> RAGAnswer:
    return RAGAnswer(
        answer=INSUFFICIENT_KNOWLEDGE,
        sources=(),
        retrieval_count=0,
        model=model_name,
        retrieval_strategy=outcome.requested_strategy,
        effective_strategy=outcome.effective_strategy,
        degraded=outcome.degraded,
        degradation_reason=outcome.degradation_reason,
        candidate_count=outcome.candidate_count,
        context_count=0,
    )


def _timing(
    outcome: SearchOutcome,
    chat_seconds: float,
    total_seconds: float,
    context_count: int,
    retrieved_ids: tuple[str, ...],
) -> RAGTiming:
    return RAGTiming(
        retrieval_seconds=outcome.retrieval_seconds,
        rerank_seconds=outcome.rerank_seconds,
        chat_seconds=chat_seconds,
        total_seconds=total_seconds,
        candidate_count=outcome.candidate_count,
        context_count=context_count,
        retrieved_ids=retrieved_ids,
    )


def normalize_rag_request(
    tenant_id: object,
    question: object,
    top_k: object,
    strategy: object | None,
    *,
    default_top_k: int,
    default_strategy: str,
) -> tuple[UUID, str, int, str]:
    """Validate a RAG request with the same rules used by RAGService.ask."""
    tenant = _require_tenant(tenant_id)
    normalized = _require_question(question)
    limit = _require_top_k(default_top_k if top_k is _OMITTED else top_k)
    selected = default_strategy if strategy is None else _require_strategy(strategy)
    return tenant, normalized, limit, selected


def _require_tenant(tenant_id: object) -> UUID:
    if not isinstance(tenant_id, UUID):
        raise RAGValidationError("tenant_id 必须是 UUID")
    return tenant_id


def _require_question(question: object) -> str:
    if not isinstance(question, str) or not question.strip():
        raise RAGValidationError("问题不能为空")
    return question.strip()


def _require_strategy(strategy: object) -> str:
    if not isinstance(strategy, str) or strategy not in RAG_RETRIEVAL_STRATEGIES:
        raise RAGValidationError("检索策略必须是 vector、hybrid 或 hybrid_rerank")
    return strategy


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


def _preference_lines(memories: object) -> tuple[str, ...]:
    if type(memories) is not tuple:
        return ()
    return tuple(item for item in memories if type(item) is str and item.strip())


def _require_answer(answer: object) -> str:
    if not isinstance(answer, str) or not answer.strip():
        raise ChatResponseError("模型输出为空")
    return answer.strip()
