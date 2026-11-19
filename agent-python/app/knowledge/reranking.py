"""Rerank an already retrieved candidate list.

The service only scores the objects it is given. It does not search again,
does not write those scores back onto the original hits, and does not run
inside a database transaction. HybridRerankRetriever asks HybridRetriever for
a wider candidate pool, then keeps the reranked top-k.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from time import perf_counter
from uuid import UUID

from app.core.config import RERANKER_MAX_CANDIDATES
from app.knowledge.hybrid import HybridRetrievalHit, HybridRetriever
from app.knowledge.reranker import Reranker

DEFAULT_RERANK_CANDIDATES = RERANKER_MAX_CANDIDATES


class RerankValidationError(ValueError):
    """The rerank request itself is not acceptable."""


@dataclass(frozen=True)
class RerankedHit:
    document_id: UUID
    chunk_id: UUID
    chunk_index: int
    content: str
    char_start: int | None
    char_end: int | None
    vector_rank: int | None
    keyword_rank: int | None
    rrf_score: float
    similarity: float | None
    rerank_score: float


@dataclass(frozen=True)
class RerankTiming:
    embedding_seconds: float | None
    vector_seconds: float
    keyword_seconds: float
    fusion_seconds: float
    rerank_seconds: float
    total_seconds: float


class RerankService:
    def __init__(self, reranker: Reranker | object, *, candidate_limit: int = DEFAULT_RERANK_CANDIDATES) -> None:
        self._reranker = reranker
        self._candidate_limit = _require_limit(candidate_limit)

    def rerank(
        self,
        query: str,
        candidates: Sequence[HybridRetrievalHit],
        *,
        top_k: int = 5,
    ) -> list[RerankedHit]:
        limit = _require_top_k(top_k)
        if limit > self._candidate_limit:
            raise RerankValidationError("top_k 不能大于候选数量")
        if not isinstance(query, str) or not query.strip():
            raise RerankValidationError("查询内容不能为空")
        if not isinstance(candidates, Sequence) or isinstance(candidates, str):
            raise RerankValidationError("候选必须是列表")
        pooled = _dedupe(list(candidates))
        if not pooled:
            return []
        window = pooled[: self._candidate_limit]
        if any(not item.content.strip() for item in window):
            raise RerankValidationError("候选内容不能为空")
        scores = self._reranker.score(query.strip(), [item.content for item in window])  # type: ignore[attr-defined]
        by_index = _scores_by_index(scores, len(window))
        ranked = [
            _copy_hit(item, by_index[index])
            for index, item in enumerate(window)
        ]
        ranked.sort(key=lambda item: (-item.rerank_score, str(item.chunk_id)))
        return ranked[:limit]


class HybridRerankRetriever:
    """Retrieve with hybrid search, then rerank that pool. HybridRetriever stays unchanged."""

    def __init__(
        self,
        hybrid_retriever: HybridRetriever | object,
        rerank_service: RerankService,
        *,
        candidate_k: int = DEFAULT_RERANK_CANDIDATES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._hybrid = hybrid_retriever
        self._rerank = rerank_service
        self._candidate_k = _require_limit(candidate_k)
        self._clock = clock or perf_counter
        self.last_timing: RerankTiming | None = None
        self.last_candidates: tuple[HybridRetrievalHit, ...] = ()

    def search(
        self,
        tenant_id: UUID,
        query: str,
        *,
        top_k: int = 5,
        document_id: UUID | None = None,
    ) -> list[RerankedHit]:
        limit = _require_top_k(top_k)
        if limit > self._candidate_k:
            raise RerankValidationError("top_k 不能大于候选数量")
        started = self._clock()
        candidates = list(
            self._hybrid.search(  # type: ignore[attr-defined]
                tenant_id,
                query,
                top_k=self._candidate_k,
                document_id=document_id,
            )
        )
        self.last_candidates = tuple(candidates)
        hybrid_timing = getattr(self._hybrid, "last_timing", None)
        rerank_started = self._clock()
        reranked = self._rerank.rerank(query, candidates, top_k=limit)
        rerank_seconds = self._clock() - rerank_started
        self.last_timing = RerankTiming(
            embedding_seconds=_optional_seconds(hybrid_timing, "embedding_seconds"),
            vector_seconds=_required_seconds(hybrid_timing, "vector_seconds"),
            keyword_seconds=_required_seconds(hybrid_timing, "keyword_seconds"),
            fusion_seconds=_required_seconds(hybrid_timing, "fusion_seconds"),
            rerank_seconds=rerank_seconds,
            total_seconds=self._clock() - started,
        )
        return reranked


def _copy_hit(hit: HybridRetrievalHit, score: float) -> RerankedHit:
    return RerankedHit(
        document_id=hit.document_id,
        chunk_id=hit.chunk_id,
        chunk_index=hit.chunk_index,
        content=hit.content,
        char_start=hit.char_start,
        char_end=hit.char_end,
        vector_rank=hit.vector_rank,
        keyword_rank=hit.keyword_rank,
        rrf_score=hit.rrf_score,
        similarity=hit.similarity,
        rerank_score=score,
    )


def _dedupe(candidates: list[object]) -> list[HybridRetrievalHit]:
    seen: set[UUID] = set()
    unique: list[HybridRetrievalHit] = []
    for item in candidates:
        if not isinstance(item, HybridRetrievalHit):
            raise RerankValidationError("候选必须是 HybridRetrievalHit")
        if item.chunk_id in seen:
            continue
        seen.add(item.chunk_id)
        unique.append(item)
    return unique


def _scores_by_index(scores: object, expected: int) -> dict[int, float]:
    if not isinstance(scores, list) or len(scores) != expected:
        raise RerankValidationError("Rerank 分数数量不正确")
    found: dict[int, float] = {}
    for item in scores:
        index = getattr(item, "index", None)
        score = getattr(item, "score", None)
        if type(index) is not int or not 0 <= index < expected or index in found:
            raise RerankValidationError("Rerank 分数索引不正确")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise RerankValidationError("Rerank 分数不正确")
        found[index] = float(score)
    if len(found) != expected:
        raise RerankValidationError("Rerank 分数数量不正确")
    return found


def _optional_seconds(timing: object, name: str) -> float | None:
    if timing is None:
        return None
    value = getattr(timing, name, None)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(float(value), 0.0)


def _required_seconds(timing: object, name: str) -> float:
    value = _optional_seconds(timing, name)
    if value is None:
        raise RerankValidationError("缺少检索耗时")
    return value


def _require_top_k(top_k: object) -> int:
    if type(top_k) is not int or not 1 <= top_k <= RERANKER_MAX_CANDIDATES:
        raise RerankValidationError(f"top_k 必须是 1 到 {RERANKER_MAX_CANDIDATES} 之间的整数")
    return top_k


def _require_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= RERANKER_MAX_CANDIDATES:
        raise RerankValidationError(f"候选数量必须是 1 到 {RERANKER_MAX_CANDIDATES} 之间的整数")
    return value
