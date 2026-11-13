"""Write embedding vectors back onto knowledge chunks.

Model calls happen outside the database transaction. A failed call or a failed
write leaves any previously stored vectors unchanged.
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


class DocumentNotFoundError(LookupError):
    """The document is not visible to this tenant."""


class VectorizationConflictError(RuntimeError):
    """The document changed while vectors were being generated."""


@dataclass(frozen=True)
class VectorizationResult:
    document_id: UUID
    chunk_count: int
    embedding_model: str
    status: str


@dataclass(frozen=True)
class _ChunkSnapshot:
    chunk_id: UUID
    chunk_index: int
    content: str


class VectorizationService:
    def __init__(self, pool: ConnectionPool, embedding_client: object) -> None:
        self._pool = pool
        self._embedding_client = embedding_client

    def vectorize(self, tenant_id: UUID, document_id: UUID) -> VectorizationResult:
        if not isinstance(tenant_id, UUID) or not isinstance(document_id, UUID):
            raise VectorizationConflictError("tenant_id 和 document_id 必须是 UUID")
        total_chunks, chunks = _read_chunks(self._pool, tenant_id, document_id)
        vectors = self._embedding_client.embed([chunk.content for chunk in chunks])  # type: ignore[attr-defined]
        _validate_vectors(vectors, expected_count=len(chunks))
        model_name = getattr(self._embedding_client, "model_name", "")
        if model_name != EMBEDDING_MODEL_NAME:
            raise EmbeddingConfigurationError("Embedding 模型不正确")
        _write_vectors(self._pool, tenant_id, document_id, total_chunks, chunks, vectors, model_name)
        return VectorizationResult(
            document_id=document_id,
            chunk_count=len(chunks),
            embedding_model=model_name,
            status="ready",
        )


def _read_chunks(
    pool: ConnectionPool,
    tenant_id: UUID,
    document_id: UUID,
) -> tuple[int, tuple[_ChunkSnapshot, ...]]:
    with tenant_transaction(pool, str(tenant_id)) as connection:
        document = connection.execute(
            """
            select total_chunks
            from agent.knowledge_document
            where id = %s and tenant_id = %s
            """,
            (document_id, tenant_id),
        ).fetchone()
        if document is None:
            raise DocumentNotFoundError("知识文档不存在")
        total_chunks = int(document[0])
        rows = connection.execute(
            """
            select id, chunk_index, content
            from agent.knowledge_chunk
            where tenant_id = %s and document_id = %s
            order by chunk_index
            """,
            (tenant_id, document_id),
        ).fetchall()
    chunks = tuple(
        _ChunkSnapshot(chunk_id=row[0], chunk_index=row[1], content=row[2]) for row in rows
    )
    if not chunks or len(chunks) != total_chunks:
        raise VectorizationConflictError("Chunk 数量与文档不一致")
    expected_indexes = list(range(len(chunks)))
    if [chunk.chunk_index for chunk in chunks] != expected_indexes:
        raise VectorizationConflictError("Chunk 顺序不连续")
    return total_chunks, chunks


def _validate_vectors(vectors: object, *, expected_count: int) -> None:
    if not isinstance(vectors, list) or len(vectors) != expected_count:
        raise EmbeddingResponseError("向量数量与 Chunk 不一致")
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != EMBEDDING_WIDTH:
            raise EmbeddingResponseError("向量维度不正确")
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise EmbeddingResponseError("向量包含非法数值")


def _write_vectors(
    pool: ConnectionPool,
    tenant_id: UUID,
    document_id: UUID,
    total_chunks: int,
    chunks: tuple[_ChunkSnapshot, ...],
    vectors: list[list[float]],
    model_name: str,
) -> None:
    with tenant_transaction(pool, str(tenant_id)) as connection:
        locked = connection.execute(
            """
            select total_chunks
            from agent.knowledge_document
            where id = %s and tenant_id = %s
            for update
            """,
            (document_id, tenant_id),
        ).fetchone()
        if locked is None:
            raise VectorizationConflictError("知识文档已不存在，请重新向量化")
        if int(locked[0]) != total_chunks:
            raise VectorizationConflictError("知识文档已变化，请重新向量化")
        current_rows = connection.execute(
            """
            select id, chunk_index, content
            from agent.knowledge_chunk
            where tenant_id = %s and document_id = %s
            order by chunk_index
            for update
            """,
            (tenant_id, document_id),
        ).fetchall()
        current = tuple(
            _ChunkSnapshot(chunk_id=row[0], chunk_index=row[1], content=row[2]) for row in current_rows
        )
        if current != chunks:
            raise VectorizationConflictError("知识文档已变化，请重新向量化")
        for chunk, vector in zip(current, vectors, strict=True):
            updated = connection.execute(
                """
                update agent.knowledge_chunk
                set embedding = %s::public.vector,
                    embedding_model = %s,
                    embedded_at = now()
                where id = %s
                  and tenant_id = %s
                  and document_id = %s
                  and chunk_index = %s
                """,
                (
                    format_vector([float(value) for value in vector]),
                    model_name,
                    chunk.chunk_id,
                    tenant_id,
                    document_id,
                    chunk.chunk_index,
                ),
            )
            if updated.rowcount != 1:
                raise VectorizationConflictError("向量写入未命中目标 Chunk")
