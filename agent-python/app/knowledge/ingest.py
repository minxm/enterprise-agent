"""Persist parsed documents and their chunks.

The caller supplies trusted tenant and file metadata. This service does not
fetch bytes, open an HTTP route, or write embeddings.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from psycopg import Connection
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.documents.extract import extract_document
from app.knowledge.chunking import TextChunk, chunk_text
from app.persistence.postgres import tenant_transaction


class KnowledgeValidationError(ValueError):
    """The ingestion request itself is not acceptable."""


@dataclass(frozen=True)
class IngestDocument:
    tenant_id: UUID
    content: bytes
    file_type: str
    document_name: str
    file_id: UUID | None = None


@dataclass(frozen=True)
class IngestResult:
    document_id: UUID
    chunk_count: int
    status: str
    reused: bool


@dataclass(frozen=True)
class _PreparedDocument:
    tenant_id: UUID
    document_name: str
    file_id: UUID | None
    file_type: str
    checksum: str
    chunks: tuple[TextChunk, ...]


@dataclass(frozen=True)
class _ExistingDocument:
    document_id: UUID
    checksum: str
    total_chunks: int


class KnowledgeIngestionService:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def ingest(self, document: IngestDocument) -> IngestResult:
        prepared = _prepare(document)
        try:
            return self._write(prepared)
        except UniqueViolation:
            # The failed transaction is already closed. Retry once on a new one.
            return self._write(prepared)

    def _write(self, prepared: _PreparedDocument) -> IngestResult:
        with tenant_transaction(self._pool, str(prepared.tenant_id)) as connection:
            return _write_document(connection, prepared)


def _prepare(document: IngestDocument) -> _PreparedDocument:
    tenant_id = _require_tenant(document.tenant_id)
    document_name = _require_name(document.document_name)
    file_id = _require_file_id(document.file_id)
    extracted = extract_document(document.content, document.file_type)
    checksum = hashlib.sha256(document.content).hexdigest()
    chunks = tuple(chunk_text(extracted.text))
    return _PreparedDocument(
        tenant_id=tenant_id,
        document_name=document_name,
        file_id=file_id,
        file_type=extracted.file_type,
        checksum=checksum,
        chunks=chunks,
    )


def _write_document(connection: Connection, prepared: _PreparedDocument) -> IngestResult:
    with connection.cursor() as cursor:
        cursor.execute(
            "select pg_advisory_xact_lock(hashtext(%s)::bigint)",
            (_lock_identity(prepared),),
        )
        cursor.fetchone()
        existing = _lock_existing(cursor, prepared)
        if existing is not None and existing.checksum == prepared.checksum:
            return IngestResult(
                document_id=existing.document_id,
                chunk_count=existing.total_chunks,
                status="ready",
                reused=True,
            )
        if existing is None:
            document_id = _insert_document(cursor, prepared)
            if document_id is None:
                existing = _lock_existing(cursor, prepared)
                if existing is None:
                    raise RuntimeError("知识文档写入失败")
                if existing.checksum == prepared.checksum:
                    return IngestResult(
                        document_id=existing.document_id,
                        chunk_count=existing.total_chunks,
                        status="ready",
                        reused=True,
                    )
                document_id = existing.document_id
                _replace_document(cursor, prepared, document_id)
        else:
            document_id = existing.document_id
            _replace_document(cursor, prepared, document_id)
        _insert_chunks(cursor, prepared, document_id)
    return IngestResult(
        document_id=document_id,
        chunk_count=len(prepared.chunks),
        status="ready",
        reused=False,
    )


def _lock_identity(prepared: _PreparedDocument) -> str:
    if prepared.file_id is not None:
        return f"knowledge:{prepared.tenant_id}:{prepared.file_id}"
    return f"knowledge:{prepared.tenant_id}:checksum:{prepared.checksum}"


def _lock_existing(cursor: object, prepared: _PreparedDocument) -> _ExistingDocument | None:
    if prepared.file_id is None:
        cursor.execute(
            """
            select id, file_checksum, total_chunks
            from agent.knowledge_document
            where tenant_id = %s and file_id is null and file_checksum = %s
            for update
            """,
            (prepared.tenant_id, prepared.checksum),
        )
    else:
        cursor.execute(
            """
            select id, file_checksum, total_chunks
            from agent.knowledge_document
            where tenant_id = %s and file_id = %s
            for update
            """,
            (prepared.tenant_id, prepared.file_id),
        )
    row = cursor.fetchone()
    if row is None:
        return None
    return _ExistingDocument(document_id=row[0], checksum=row[1], total_chunks=row[2])


def _insert_document(cursor: object, prepared: _PreparedDocument) -> UUID | None:
    if prepared.file_id is None:
        statement = """
            insert into agent.knowledge_document (
                tenant_id, document_name, file_id, file_type,
                file_checksum, status, total_chunks
            ) values (%s, %s, null, %s, %s, 'ready', %s)
            on conflict (tenant_id, file_checksum) where file_id is null do nothing
            returning id
        """
        parameters = (
            prepared.tenant_id,
            prepared.document_name,
            prepared.file_type,
            prepared.checksum,
            len(prepared.chunks),
        )
    else:
        statement = """
            insert into agent.knowledge_document (
                tenant_id, document_name, file_id, file_type,
                file_checksum, status, total_chunks
            ) values (%s, %s, %s, %s, %s, 'ready', %s)
            on conflict (tenant_id, file_id) where file_id is not null do nothing
            returning id
        """
        parameters = (
            prepared.tenant_id,
            prepared.document_name,
            prepared.file_id,
            prepared.file_type,
            prepared.checksum,
            len(prepared.chunks),
        )
    cursor.execute(statement, parameters)
    row = cursor.fetchone()
    if row is None:
        return None
    return row[0]


def _replace_document(cursor: object, prepared: _PreparedDocument, document_id: UUID) -> None:
    cursor.execute(
        """
        update agent.knowledge_document
        set document_name = %s,
            file_type = %s,
            file_checksum = %s,
            total_chunks = %s,
            updated_at = now()
        where id = %s and tenant_id = %s
        """,
        (
            prepared.document_name,
            prepared.file_type,
            prepared.checksum,
            len(prepared.chunks),
            document_id,
            prepared.tenant_id,
        ),
    )
    cursor.execute(
        """
        delete from agent.knowledge_chunk
        where document_id = %s and tenant_id = %s
        """,
        (document_id, prepared.tenant_id),
    )


def _insert_chunks(cursor: object, prepared: _PreparedDocument, document_id: UUID) -> None:
    cursor.executemany(
        """
        insert into agent.knowledge_chunk (
            tenant_id, document_id, chunk_index, content, char_count, metadata
        ) values (%s, %s, %s, %s, %s, %s)
        """,
        [
            (
                prepared.tenant_id,
                document_id,
                chunk.chunk_index,
                chunk.content,
                len(chunk.content),
                Jsonb(
                    {
                        "char_start": chunk.char_start,
                        "char_end": chunk.char_end,
                        "file_type": prepared.file_type,
                    }
                ),
            )
            for chunk in prepared.chunks
        ],
    )


def _require_tenant(tenant_id: UUID) -> UUID:
    if not isinstance(tenant_id, UUID):
        raise KnowledgeValidationError("tenant_id 必须是 UUID")
    return tenant_id


def _require_name(document_name: str) -> str:
    if not isinstance(document_name, str):
        raise KnowledgeValidationError("document_name 必须是字符串")
    normalized = document_name.strip()
    if not 1 <= len(normalized) <= 200:
        raise KnowledgeValidationError("document_name 长度必须在 1 到 200 之间")
    return normalized


def _require_file_id(file_id: UUID | None) -> UUID | None:
    if file_id is None:
        return None
    if not isinstance(file_id, UUID):
        raise KnowledgeValidationError("file_id 必须是 UUID")
    return file_id
