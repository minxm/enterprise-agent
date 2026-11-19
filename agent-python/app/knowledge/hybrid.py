"""Fuse vector search and keyword search with reciprocal rank fusion.

The two searches run one after the other. Each uses its own transaction, so
they do not share a database connection. A failure in either search is raised
and no fused list is returned.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from uuid import UUID

from app.knowledge.keyword_retrieval import KeywordHit, KeywordRetriever
from app.knowledge.retrieval import MAX_TOP_K, RetrievalHit, VectorRetriever

DEFAULT_RRF_K = 60
DEFAULT_CANDIDATE_K = MAX_TOP_K


class HybridValidationError(ValueError):
    """The hybrid request itself is not acceptable."""


@dataclass(frozen=True)
class HybridRetrievalHit:
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


@dataclass(frozen=True)
class HybridTiming:
    embedding_seconds: float | None
    vector_seconds: float
    keyword_seconds: float
    fusion_seconds: float
    total_seconds: float


class HybridRetriever:
    def __init__(
        self,
        vector_retriever: VectorRetriever | object,
        keyword_retriever: KeywordRetriever | object,
        *,
        rrf_k: int = DEFAULT_RRF_K,
        embedding_client: object | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._vector = vector_retriever
        self._keyword = keyword_retriever
        self._rrf_k = _require_rrf_k(rrf_k)
        self._embedding_client = embedding_client
        self._clock = clock or perf_counter
        self.last_timing: HybridTiming | None = None

    def search(
        self,
        tenant_id: UUID,
        query: str,
        *,
        top_k: int = 5,
        document_id: UUID | None = None,
        vector_k: int | None = None,
        keyword_k: int | None = None,
    ) -> list[HybridRetrievalHit]:
        limit = _require_top_k(top_k)
        vector_limit = _require_candidate(vector_k, limit, "vector_k")
        keyword_limit = _require_candidate(keyword_k, limit, "keyword_k")
        started = self._clock()
        if self._embedding_client is not None:
            self._embedding_client.last_query_seconds = None  # type: ignore[attr-defined]
        vector_started = self._clock()
        vector_hits = self._vector.search(  # type: ignore[attr-defined]
            tenant_id,
            query,
            top_k=vector_limit,
            document_id=document_id,
        )
        vector_wall = self._clock() - vector_started
        embedding_seconds = _embedding_seconds(self._embedding_client)
        if embedding_seconds is None:
            vector_seconds = vector_wall
        else:
            vector_seconds = max(vector_wall - embedding_seconds, 0.0)
        keyword_started = self._clock()
        keyword_hits = self._keyword.search(  # type: ignore[attr-defined]
            tenant_id,
            query,
            top_k=keyword_limit,
            document_id=document_id,
        )
        keyword_seconds = self._clock() - keyword_started
        fusion_started = self._clock()
        fused = fuse_ranks(
            list(vector_hits),
            list(keyword_hits),
            top_k=limit,
            rrf_k=self._rrf_k,
        )
        fusion_seconds = self._clock() - fusion_started
        self.last_timing = HybridTiming(
            embedding_seconds=embedding_seconds,
            vector_seconds=vector_seconds,
            keyword_seconds=keyword_seconds,
            fusion_seconds=fusion_seconds,
            total_seconds=self._clock() - started,
        )
        return fused


def fuse_ranks(
    vector_hits: list[RetrievalHit] | list[object],
    keyword_hits: list[KeywordHit] | list[object],
    *,
    top_k: int,
    rrf_k: int = DEFAULT_RRF_K,
) -> list[HybridRetrievalHit]:
    limit = _require_top_k(top_k)
    constant = _require_rrf_k(rrf_k)
    pooled: dict[UUID, _Accumulator] = {}
    for rank, hit in enumerate(_dedupe(vector_hits), start=1):
        item = pooled.setdefault(hit.chunk_id, _Accumulator(hit))
        if item.vector_rank is None:
            item.vector_rank = rank
            item.rrf_score += 1.0 / (constant + rank)
            if item.similarity is None:
                item.similarity = _similarity(hit)
    for rank, hit in enumerate(_dedupe(keyword_hits), start=1):
        item = pooled.setdefault(hit.chunk_id, _Accumulator(hit))
        if item.keyword_rank is None:
            item.keyword_rank = rank
            item.rrf_score += 1.0 / (constant + rank)
    ranked = sorted(pooled.values(), key=lambda item: (-item.rrf_score, str(item.chunk_id)))
    return [item.to_hit() for item in ranked[:limit]]


class _Accumulator:
    def __init__(self, hit: object) -> None:
        self.document_id = _uuid_attr(hit, "document_id")
        self.chunk_id = _uuid_attr(hit, "chunk_id")
        self.chunk_index = _int_attr(hit, "chunk_index")
        self.content = _text_attr(hit, "content")
        self.char_start = _optional_int_attr(hit, "char_start")
        self.char_end = _optional_int_attr(hit, "char_end")
        self.vector_rank: int | None = None
        self.keyword_rank: int | None = None
        self.rrf_score = 0.0
        self.similarity: float | None = None

    def to_hit(self) -> HybridRetrievalHit:
        return HybridRetrievalHit(
            document_id=self.document_id,
            chunk_id=self.chunk_id,
            chunk_index=self.chunk_index,
            content=self.content,
            char_start=self.char_start,
            char_end=self.char_end,
            vector_rank=self.vector_rank,
            keyword_rank=self.keyword_rank,
            rrf_score=self.rrf_score,
            similarity=self.similarity,
        )


def _dedupe(hits: list[object]) -> list[object]:
    seen: set[UUID] = set()
    unique: list[object] = []
    for hit in hits:
        chunk_id = _uuid_attr(hit, "chunk_id")
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        unique.append(hit)
    return unique


def _embedding_seconds(client: object | None) -> float | None:
    if client is None:
        return None
    value = getattr(client, "last_query_seconds", None)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(float(value), 0.0)


def _similarity(hit: object) -> float | None:
    value = getattr(hit, "similarity", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _uuid_attr(hit: object, name: str) -> UUID:
    value = getattr(hit, name, None)
    if not isinstance(value, UUID):
        raise HybridValidationError(f"检索结果缺少 {name}")
    return value


def _int_attr(hit: object, name: str) -> int:
    value = getattr(hit, name, None)
    if type(value) is not int:
        raise HybridValidationError(f"检索结果缺少 {name}")
    return value


def _optional_int_attr(hit: object, name: str) -> int | None:
    value = getattr(hit, name, None)
    if value is None:
        return None
    if type(value) is not int:
        raise HybridValidationError(f"检索结果缺少 {name}")
    return value


def _text_attr(hit: object, name: str) -> str:
    value = getattr(hit, name, None)
    if not isinstance(value, str):
        raise HybridValidationError(f"检索结果缺少 {name}")
    return value


def _require_rrf_k(rrf_k: object) -> int:
    if type(rrf_k) is not int or rrf_k < 1:
        raise HybridValidationError("rrf_k 必须是大于等于 1 的整数")
    return rrf_k


def _require_top_k(top_k: object) -> int:
    if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
        raise HybridValidationError(f"top_k 必须是 1 到 {MAX_TOP_K} 之间的整数")
    return top_k


def _require_candidate(value: int | None, top_k: int, name: str) -> int:
    if value is None:
        return max(top_k, DEFAULT_CANDIDATE_K) if top_k < DEFAULT_CANDIDATE_K else top_k
    if type(value) is not int or not 1 <= value <= MAX_TOP_K:
        raise HybridValidationError(f"{name} 必须是 1 到 {MAX_TOP_K} 之间的整数")
    if value < top_k:
        raise HybridValidationError(f"{name} 不能小于 top_k")
    return value
