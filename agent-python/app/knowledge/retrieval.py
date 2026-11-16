"""Search knowledge chunks by cosine similarity.

The caller supplies a trusted tenant id. Query text is embedded with a
retrieval instruction, then read inside tenant_transaction so RLS stays in
force. Document chunk vectors are not rewritten here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from uuid import UUID

from psycopg_pool import ConnectionPool

from app.core.config import EMBEDDING_MODEL_NAME, EMBEDDING_WIDTH
from app.knowledge.embeddings import (
    EmbeddingConfigurationError,
    EmbeddingResponseError,
    format_vector,
)
from app.persistence.postgres import tenant_transaction

MAX_TOP_K = 20


class RetrievalValidationError(ValueError):
    """The retrieval request itself is not acceptable."""


@dataclass(frozen=True)
class RetrievalHit:
    document_id: UUID
    chunk_id: UUID
    chunk_index: int
    content: str
    char_start: int | None
    char_end: int | None
    similarity: float
    embedding_model: str


class VectorRetriever:
    def __init__(self, pool: ConnectionPool, embedding_client: object) -> None:
        self._pool = pool
        self._embedding_client = embedding_client

    def search(
        self,
        tenant_id: UUID,
        query: str,
        *,
        top_k: int = 5,
        similarity_threshold: float | None = None,
        document_id: UUID | None = None,
    ) -> list[RetrievalHit]:
        tenant = _require_tenant(tenant_id)
        question = _require_query(query)
        limit = _require_top_k(top_k)
        threshold = _require_threshold(similarity_threshold)
        document = _require_document_id(document_id)
        raw_vector = self._embedding_client.embed_query(question)  # type: ignore[attr-defined]
        vector = _validate_query_vector(raw_vector)
        model_name = getattr(self._embedding_client, "model_name", "")
        if model_name != EMBEDDING_MODEL_NAME:
            raise EmbeddingConfigurationError("Embedding 模型不正确")
        statement, parameters = _search_statement(
            vector=vector,
            tenant_id=tenant,
            document_id=document,
            similarity_threshold=threshold,
            top_k=limit,
        )
        with tenant_transaction(self._pool, str(tenant)) as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [_hit_from_row(row) for row in rows]


# The pool sets search_path to agent, so the pgvector operator must be qualified.
_SEARCH_SQL = """
select
    scored.chunk_id,
    scored.document_id,
    scored.chunk_index,
    scored.content,
    scored.char_start,
    scored.char_end,
    scored.embedding_model,
    1 - scored.cosine_distance as similarity
from (
    select
        c.id as chunk_id,
        c.document_id,
        c.chunk_index,
        c.content,
        c.metadata ->> 'char_start' as char_start,
        c.metadata ->> 'char_end' as char_end,
        c.embedding_model,
        c.embedding OPERATOR(public.<=>) query_vector.embedding as cosine_distance
    from agent.knowledge_chunk c
    cross join (select %s::public.vector as embedding) as query_vector
    where c.tenant_id = %s
      and c.embedding is not null
      and c.embedding_model = %s{document_filter}
) as scored
{threshold_filter}
order by scored.cosine_distance asc
limit %s
"""


def _search_statement(
    *,
    vector: list[float],
    tenant_id: UUID,
    document_id: UUID | None,
    similarity_threshold: float | None,
    top_k: int,
) -> tuple[str, tuple[object, ...]]:
    document_filter = ""
    threshold_filter = ""
    parameters: list[object] = [format_vector(vector), tenant_id, EMBEDDING_MODEL_NAME]
    if document_id is not None:
        document_filter = "\n      and c.document_id = %s"
        parameters.append(document_id)
    if similarity_threshold is not None:
        # Compare against cosine similarity, not the <=> distance.
        threshold_filter = "where 1 - scored.cosine_distance >= %s"
        parameters.append(similarity_threshold)
    parameters.append(top_k)
    statement = _SEARCH_SQL.format(
        document_filter=document_filter,
        threshold_filter=threshold_filter,
    )
    return statement, tuple(parameters)


def _hit_from_row(row: tuple[object, ...]) -> RetrievalHit:
    chunk_id, document_id, chunk_index, content, char_start, char_end, embedding_model, similarity = row
    score = float(similarity)  # type: ignore[arg-type]
    if not math.isfinite(score):
        raise EmbeddingResponseError("向量包含非法数值")
    if not isinstance(chunk_id, UUID) or not isinstance(document_id, UUID):
        raise EmbeddingResponseError("检索结果标识不正确")
    if type(chunk_index) is not int or not isinstance(content, str) or not isinstance(embedding_model, str):
        raise EmbeddingResponseError("检索结果格式不正确")
    return RetrievalHit(
        document_id=document_id,
        chunk_id=chunk_id,
        chunk_index=chunk_index,
        content=content,
        char_start=_offset(char_start),
        char_end=_offset(char_end),
        similarity=score,
        embedding_model=embedding_model,
    )


def _offset(value: object) -> int | None:
    if not isinstance(value, str) or not value.isdecimal():
        return None
    number = int(value)
    if number > 2_147_483_647:
        return None
    return number


def _require_tenant(tenant_id: object) -> UUID:
    if not isinstance(tenant_id, UUID):
        raise RetrievalValidationError("tenant_id 必须是 UUID")
    return tenant_id


def _require_query(query: object) -> str:
    if not isinstance(query, str) or not query.strip():
        raise RetrievalValidationError("查询内容不能为空")
    return query.strip()


def _require_top_k(top_k: object) -> int:
    if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
        raise RetrievalValidationError(f"top_k 必须是 1 到 {MAX_TOP_K} 之间的整数")
    return top_k


def _require_threshold(similarity_threshold: object) -> float | None:
    if similarity_threshold is None:
        return None
    if isinstance(similarity_threshold, bool) or not isinstance(similarity_threshold, (int, float)):
        raise RetrievalValidationError("similarity_threshold 必须在 0 到 1 之间")
    number = float(similarity_threshold)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise RetrievalValidationError("similarity_threshold 必须在 0 到 1 之间")
    return number


def _require_document_id(document_id: object) -> UUID | None:
    if document_id is None:
        return None
    if not isinstance(document_id, UUID):
        raise RetrievalValidationError("document_id 必须是 UUID")
    return document_id


def _validate_query_vector(vector: object) -> list[float]:
    if not isinstance(vector, list) or len(vector) != EMBEDDING_WIDTH:
        raise EmbeddingResponseError("向量维度不正确")
    checked: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EmbeddingResponseError("向量包含非法数值")
        number = float(value)
        if not math.isfinite(number):
            raise EmbeddingResponseError("向量包含非法数值")
        checked.append(number)
    return checked
