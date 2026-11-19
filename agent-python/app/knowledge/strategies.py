"""Route a trusted tenant query to vector, hybrid, or hybrid rerank search.

This module does not embed text, fuse ranks, or call a chat model. Reranker
runtime failures can fall back to the hybrid candidates already retrieved for
the same request. Validation errors and retrieval errors are not converted
into a result.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from uuid import UUID

from app.core.config import RAG_FAILURE_POLICIES, RAG_RETRIEVAL_STRATEGIES, RERANKER_MAX_CANDIDATES
from app.knowledge.reranker import RerankAPIError, RerankResponseError

logger = logging.getLogger(__name__)
_DEGRADABLE = (RerankAPIError, RerankResponseError)


class StrategyValidationError(ValueError):
    """The requested retrieval strategy or its result cannot be used."""


@dataclass(frozen=True)
class RetrievedPassage:
    document_id: UUID
    chunk_id: UUID
    chunk_index: int
    content: str
    char_start: int | None = None
    char_end: int | None = None
    similarity: float | None = None
    embedding_model: str | None = None
    vector_rank: int | None = None
    keyword_rank: int | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None


@dataclass(frozen=True)
class SearchOutcome:
    passages: tuple[RetrievedPassage, ...]
    requested_strategy: str
    effective_strategy: str
    candidate_count: int
    degraded: bool
    degradation_reason: str | None
    retrieval_seconds: float
    rerank_seconds: float | None


class RetrievalStrategies:
    def __init__(
        self,
        *,
        vector: object,
        hybrid: object | None = None,
        hybrid_rerank: object | None = None,
        failure_policy: str = "strict",
        candidate_k: int = RERANKER_MAX_CANDIDATES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if failure_policy not in RAG_FAILURE_POLICIES:
            raise StrategyValidationError("failure_policy 必须是 strict 或 fallback")
        if type(candidate_k) is not int or not 1 <= candidate_k <= RERANKER_MAX_CANDIDATES:
            raise StrategyValidationError("candidate_k 必须是 1 到 20 之间的整数")
        self._vector = vector
        self._hybrid = hybrid
        self._hybrid_rerank = hybrid_rerank
        self._failure_policy = failure_policy
        self._candidate_k = candidate_k
        self._clock = clock or perf_counter

    def search(
        self,
        tenant_id: UUID,
        query: str,
        *,
        top_k: int,
        strategy: str,
        document_id: UUID | None = None,
    ) -> SearchOutcome:
        if strategy not in RAG_RETRIEVAL_STRATEGIES:
            raise StrategyValidationError("检索策略必须是 vector、hybrid 或 hybrid_rerank")
        if not isinstance(tenant_id, UUID):
            raise StrategyValidationError("tenant_id 必须是 UUID")
        if strategy == "vector":
            return self._vector_search(tenant_id, query, top_k, document_id)
        if strategy == "hybrid":
            return self._hybrid_search(tenant_id, query, top_k, document_id)
        return self._rerank_search(tenant_id, query, top_k, document_id)

    def _vector_search(self, tenant_id: UUID, query: str, top_k: int, document_id: UUID | None) -> SearchOutcome:
        started = self._clock()
        hits = self._vector.search(tenant_id, query, top_k=top_k, document_id=document_id)  # type: ignore[attr-defined]
        elapsed = self._clock() - started
        passages = tuple(to_passage(hit) for hit in list(hits))
        return SearchOutcome(passages, "vector", "vector", len(passages), False, None, elapsed, None)

    def _hybrid_search(self, tenant_id: UUID, query: str, top_k: int, document_id: UUID | None) -> SearchOutcome:
        hybrid = self._require_hybrid()
        started = self._clock()
        hits = hybrid.search(tenant_id, query, top_k=self._candidate_k, document_id=document_id)  # type: ignore[attr-defined]
        elapsed = self._clock() - started
        pool = tuple(to_passage(hit) for hit in list(hits))
        timing = getattr(hybrid, "last_timing", None)
        return SearchOutcome(
            pool[:top_k],
            "hybrid",
            "hybrid",
            len(pool),
            False,
            None,
            _retrieval_seconds(timing, elapsed),
            None,
        )

    def _rerank_search(self, tenant_id: UUID, query: str, top_k: int, document_id: UUID | None) -> SearchOutcome:
        reranker = self._hybrid_rerank
        if reranker is None:
            raise StrategyValidationError("未配置 Reranker，不能使用 hybrid_rerank")
        started = self._clock()
        try:
            hits = reranker.search(tenant_id, query, top_k=top_k, document_id=document_id)  # type: ignore[attr-defined]
        except _DEGRADABLE as exc:
            if self._failure_policy != "fallback":
                raise
            return self._fallback(reranker, top_k, started, exc)
        elapsed = self._clock() - started
        passages = tuple(to_passage(hit) for hit in list(hits))
        candidates = tuple(getattr(reranker, "last_candidates", ()) or ())
        timing = getattr(reranker, "last_timing", None)
        return SearchOutcome(
            passages,
            "hybrid_rerank",
            "hybrid_rerank",
            len(candidates) if candidates else len(passages),
            False,
            None,
            _component_seconds(timing, "retrieval", elapsed),
            _component_seconds(timing, "rerank", None),
        )

    def _fallback(self, reranker: object, top_k: int, started: float, exc: Exception) -> SearchOutcome:
        candidates = tuple(getattr(reranker, "last_candidates", ()) or ())
        if not candidates:
            raise exc
        reason = _safe_reason(exc)
        logger.warning("Reranker 降级为 Hybrid：%s", reason)
        pool = tuple(to_passage(hit) for hit in candidates)
        hybrid = self._hybrid
        timing = getattr(hybrid, "last_timing", None) if hybrid is not None else None
        finished = self._clock()
        retrieval_seconds = _retrieval_seconds(timing, finished - started)
        return SearchOutcome(
            pool[:top_k],
            "hybrid_rerank",
            "hybrid",
            len(pool),
            True,
            reason,
            retrieval_seconds,
            max(finished - started - retrieval_seconds, 0.0),
        )

    def _require_hybrid(self) -> object:
        if self._hybrid is None:
            raise StrategyValidationError("未配置 Hybrid 检索")
        return self._hybrid


def to_passage(hit: object) -> RetrievedPassage:
    document_id = getattr(hit, "document_id", None)
    chunk_id = getattr(hit, "chunk_id", None)
    chunk_index = getattr(hit, "chunk_index", None)
    content = getattr(hit, "content", None)
    if not isinstance(document_id, UUID) or not isinstance(chunk_id, UUID):
        raise StrategyValidationError("检索结果标识不正确")
    if type(chunk_index) is not int or not isinstance(content, str):
        raise StrategyValidationError("检索结果格式不正确")
    return RetrievedPassage(
        document_id=document_id,
        chunk_id=chunk_id,
        chunk_index=chunk_index,
        content=content,
        char_start=_optional_int(getattr(hit, "char_start", None)),
        char_end=_optional_int(getattr(hit, "char_end", None)),
        similarity=_optional_float(getattr(hit, "similarity", None)),
        embedding_model=_optional_text(getattr(hit, "embedding_model", None)),
        vector_rank=_optional_int(getattr(hit, "vector_rank", None)),
        keyword_rank=_optional_int(getattr(hit, "keyword_rank", None)),
        rrf_score=_optional_float(getattr(hit, "rrf_score", None)),
        rerank_score=_optional_float(getattr(hit, "rerank_score", None)),
    )


def _retrieval_seconds(timing: object, fallback: float) -> float:
    if timing is None:
        return max(fallback, 0.0)
    embedding = _optional_float(getattr(timing, "embedding_seconds", None)) or 0.0
    vector = _optional_float(getattr(timing, "vector_seconds", None))
    keyword = _optional_float(getattr(timing, "keyword_seconds", None))
    fusion = _optional_float(getattr(timing, "fusion_seconds", None))
    if vector is None or keyword is None or fusion is None:
        return max(fallback, 0.0)
    return embedding + vector + keyword + fusion


def _component_seconds(timing: object, kind: str, fallback: float | None) -> float | None:
    if timing is None:
        return fallback
    if kind == "rerank":
        value = _optional_float(getattr(timing, "rerank_seconds", None))
        return 0.0 if value is None else value
    embedding = _optional_float(getattr(timing, "embedding_seconds", None)) or 0.0
    vector = _optional_float(getattr(timing, "vector_seconds", None))
    keyword = _optional_float(getattr(timing, "keyword_seconds", None))
    fusion = _optional_float(getattr(timing, "fusion_seconds", None))
    if vector is None or keyword is None or fusion is None:
        return fallback
    return embedding + vector + keyword + fusion


def _safe_reason(exc: Exception) -> str:
    text = f"{exc.__class__.__name__}: {exc}".replace("\n", " ").strip()
    if len(text) > 200:
        text = text[:200]
    return text


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise StrategyValidationError("检索结果格式不正确")
    return value


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StrategyValidationError("检索结果分数不正确")
    return float(value)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise StrategyValidationError("检索结果格式不正确")
    return value
