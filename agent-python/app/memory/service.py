"""Create and read explicit long-term memories.

Every method takes a MemoryAccessContext. The context is applied with
set_config for the transaction so RLS can see it. The service does not
treat those settings as proof of identity, does not embed text, and does
not write a checkpoint.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from psycopg import Connection
from psycopg.errors import CheckViolation
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.memory.context import MemoryAccessContext
from app.memory.errors import (
    MemoryForbidden,
    MemoryNotFound,
    MemoryValidationError,
    MemoryVersionConflict,
)
from app.memory.sensitive import looks_sensitive
from app.persistence.postgres import memory_transaction

_SOURCE = "user_explicit"
_CONFIDENCE = Decimal("1.00")
_SCOPES = frozenset({"user", "tenant"})
_TYPES = frozenset({"preference", "format", "work_scope", "organization_practice"})
_BUSINESS_FACT_TYPES = frozenset(
    {
        "contract",
        "contract_no",
        "contract_number",
        "contract_amount",
        "contract_fact",
        "approval",
        "approval_status",
        "credential",
        "password",
        "secret",
        "api_key",
    }
)
_STATUSES = frozenset({"active", "archived", "expired", "deleted"})
_MUTABLE = frozenset({"active", "archived"})
_LIST_LIMIT = 100
_RECALL_USER_LIMIT = 5
_RECALL_TENANT_LIMIT = 3
_RECALL_TOTAL_LIMIT = 8
_RECALL_CHAR_LIMIT = 2000
_CONTENT_LIMIT = 1000

_COLUMNS = """
    id, tenant_id, user_id, scope, memory_type, content, source,
    source_conversation_id, confidence, status, created_at, updated_at,
    expires_at, deleted_at, created_by, updated_by, version, content_sha256
"""


@dataclass(frozen=True)
class MemoryRecord:
    id: UUID
    tenant_id: UUID
    user_id: UUID | None
    scope: str
    memory_type: str
    content: str
    source: str
    source_conversation_id: UUID | None
    confidence: Decimal
    status: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    deleted_at: datetime | None
    created_by: UUID
    updated_by: UUID | None
    version: int
    content_sha256: str


@dataclass(frozen=True)
class MemorySnippet:
    id: UUID
    scope: str
    memory_type: str
    content: str


class MemoryService:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def create(
        self,
        ctx: MemoryAccessContext,
        *,
        scope: str,
        memory_type: str,
        content: str,
        expires_at: datetime | None = None,
        source_conversation_id: UUID | None = None,
    ) -> MemoryRecord:
        _require_context(ctx)
        scope = _require_scope(scope)
        memory_type = _require_type(memory_type)
        text = _require_content(content)
        expires_at = _require_expiry(expires_at)
        conversation_id = _require_conversation(source_conversation_id)
        if scope == "tenant" and not ctx.allow_tenant_write:
            raise MemoryForbidden("无权写入租户记忆")
        user_id = ctx.actor_id if scope == "user" else None
        digest = _digest(text)
        try:
            with _transaction(self._pool, ctx) as connection:
                row = connection.execute(
                    f"""
                    insert into agent.memory_item (
                        tenant_id, user_id, scope, memory_type, content, source,
                        source_conversation_id, confidence, status, created_by,
                        version, metadata, content_sha256, expires_at
                    ) values (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, 'active', %s,
                        1, %s, %s, %s
                    )
                    returning {_COLUMNS}
                    """,
                    (
                        ctx.tenant_id,
                        user_id,
                        scope,
                        memory_type,
                        text,
                        _SOURCE,
                        conversation_id,
                        _CONFIDENCE,
                        ctx.actor_id,
                        Jsonb({}),
                        digest,
                        expires_at,
                    ),
                ).fetchone()
                record = _record(row)
                _audit(connection, ctx, record.id, "create", record.content_sha256)
                return record
        except CheckViolation:
            raise MemoryValidationError("记忆内容不符合保存要求") from None

    def list(
        self,
        ctx: MemoryAccessContext,
        *,
        scope: str | None = None,
        memory_type: str | None = None,
        status: str | None = None,
        include_expired: bool = False,
    ) -> tuple[MemoryRecord, ...]:
        _require_context(ctx)
        if type(include_expired) is not bool:
            raise MemoryValidationError("include_expired 必须是布尔值")
        if scope is not None:
            scope = _require_scope(scope)
        if scope == "tenant" and not (ctx.allow_tenant_read or ctx.allow_tenant_write):
            raise MemoryForbidden("无权读取租户记忆")
        if memory_type is not None:
            memory_type = _require_type(memory_type)
        if status is not None and status not in _STATUSES:
            raise MemoryValidationError("记忆状态不合法")
        clauses = ["true"]
        parameters: list[object] = []
        if scope is None:
            if not (ctx.allow_tenant_read or ctx.allow_tenant_write):
                clauses.append("scope = 'user'")
        else:
            clauses.append("scope = %s")
            parameters.append(scope)
        if memory_type is not None:
            clauses.append("memory_type = %s")
            parameters.append(memory_type)
        if status is None:
            clauses.append("status <> 'deleted'")
            if not include_expired:
                clauses.append("status <> 'expired'")
                clauses.append(
                    "not (status = 'active' and expires_at is not null and expires_at <= now())"
                )
        else:
            clauses.append("status = %s")
            parameters.append(status)
            if status == "active" and not include_expired:
                clauses.append("not (expires_at is not null and expires_at <= now())")
        parameters.append(_LIST_LIMIT)
        with _transaction(self._pool, ctx) as connection:
            rows = connection.execute(
                f"""
                select {_COLUMNS}
                from agent.memory_item
                where {" and ".join(clauses)}
                order by updated_at desc, id
                limit %s
                """,
                parameters,
            ).fetchall()
        return tuple(_record(row) for row in rows)

    def get(self, ctx: MemoryAccessContext, memory_id: UUID) -> MemoryRecord:
        _require_context(ctx)
        _require_id(memory_id)
        with _transaction(self._pool, ctx) as connection:
            return _visible(connection, memory_id)

    def update(
        self,
        ctx: MemoryAccessContext,
        *,
        memory_id: UUID,
        expected_version: int,
        content: str,
        expires_at: datetime | None,
    ) -> MemoryRecord:
        _require_context(ctx)
        _require_id(memory_id)
        version = _require_version(expected_version)
        text = _require_content(content)
        expires_at = _require_expiry(expires_at)
        digest = _digest(text)
        try:
            with _transaction(self._pool, ctx) as connection:
                current = _visible(connection, memory_id)
                _require_tenant_write(ctx, current.scope, "无权修改租户记忆")
                if current.status not in _MUTABLE:
                    raise MemoryValidationError("当前状态不能修改")
                if current.version != version:
                    raise MemoryVersionConflict("记忆版本冲突")
                row = connection.execute(
                    f"""
                    update agent.memory_item
                    set content = %s,
                        content_sha256 = %s,
                        expires_at = %s,
                        version = version + 1,
                        updated_at = now(),
                        updated_by = %s
                    where id = %s
                      and version = %s
                      and status in ('active', 'archived')
                    returning {_COLUMNS}
                    """,
                    (text, digest, expires_at, ctx.actor_id, memory_id, version),
                ).fetchone()
                if row is None:
                    raise MemoryVersionConflict("记忆版本冲突")
                record = _record(row)
                _audit(connection, ctx, record.id, "update", record.content_sha256)
                return record
        except CheckViolation:
            raise MemoryValidationError("记忆内容不符合保存要求") from None

    def delete(
        self,
        ctx: MemoryAccessContext,
        *,
        memory_id: UUID,
        expected_version: int,
    ) -> MemoryRecord:
        _require_context(ctx)
        _require_id(memory_id)
        version = _require_version(expected_version)
        with _transaction(self._pool, ctx) as connection:
            current = _visible(connection, memory_id)
            _require_tenant_write(ctx, current.scope, "无权删除租户记忆")
            if current.status == "deleted":
                raise MemoryValidationError("记忆已经删除")
            if current.version != version:
                raise MemoryVersionConflict("记忆版本冲突")
            retained = current.content_sha256
            # Future embedding, embedding_model, and embedded_at columns must be
            # cleared in this same update. There is no vector column in Day 38.
            row = connection.execute(
                f"""
                update agent.memory_item
                set status = 'deleted',
                    deleted_at = now(),
                    content = '',
                    metadata = '{{}}'::jsonb,
                    version = version + 1,
                    updated_at = now(),
                    updated_by = %s
                where id = %s
                  and version = %s
                  and status <> 'deleted'
                returning {_COLUMNS}
                """,
                (ctx.actor_id, memory_id, version),
            ).fetchone()
            if row is None:
                raise MemoryVersionConflict("记忆版本冲突")
            record = _record(row)
            if record.content_sha256 != retained or record.content != "":
                raise MemoryValidationError("记忆内容不符合保存要求")
            _audit(connection, ctx, record.id, "delete", retained)
            return record

    def archive(
        self,
        ctx: MemoryAccessContext,
        *,
        memory_id: UUID,
        expected_version: int,
    ) -> MemoryRecord:
        _require_context(ctx)
        _require_id(memory_id)
        version = _require_version(expected_version)
        with _transaction(self._pool, ctx) as connection:
            current = _visible(connection, memory_id)
            _require_tenant_write(ctx, current.scope, "无权归档租户记忆")
            if current.status != "active":
                raise MemoryValidationError("只能归档处于有效状态的记忆")
            if current.version != version:
                raise MemoryVersionConflict("记忆版本冲突")
            row = connection.execute(
                f"""
                update agent.memory_item
                set status = 'archived',
                    version = version + 1,
                    updated_at = now(),
                    updated_by = %s
                where id = %s
                  and version = %s
                  and status = 'active'
                returning {_COLUMNS}
                """,
                (ctx.actor_id, memory_id, version),
            ).fetchone()
            if row is None:
                raise MemoryVersionConflict("记忆版本冲突")
            record = _record(row)
            _audit(connection, ctx, record.id, "archive", record.content_sha256)
            return record

    def expire_due(self, ctx: MemoryAccessContext) -> int:
        _require_context(ctx)
        with _transaction(self._pool, ctx) as connection:
            rows = connection.execute(
                """
                update agent.memory_item
                set status = 'expired',
                    version = version + 1,
                    updated_at = now(),
                    updated_by = %s
                where status = 'active'
                  and expires_at is not null
                  and expires_at <= now()
                returning id, content_sha256
                """,
                (ctx.actor_id,),
            ).fetchall()
            for memory_id, digest in rows:
                _audit(connection, ctx, memory_id, "expire", digest)
        return len(rows)

    def recall(self, ctx: MemoryAccessContext) -> tuple[MemorySnippet, ...]:
        _require_context(ctx)
        with _transaction(self._pool, ctx) as connection:
            users = _recall_scope(connection, "user", _RECALL_USER_LIMIT)
            tenants: list[tuple[object, ...]] = []
            if ctx.allow_tenant_read:
                tenants = _recall_scope(connection, "tenant", _RECALL_TENANT_LIMIT)
        chosen: list[MemorySnippet] = []
        used = 0
        for row in users + tenants:
            if len(chosen) >= _RECALL_TOTAL_LIMIT:
                break
            content = str(row[3])
            if used + len(content) > _RECALL_CHAR_LIMIT:
                continue
            chosen.append(
                MemorySnippet(
                    id=row[0],
                    scope=str(row[1]),
                    memory_type=str(row[2]),
                    content=content,
                )
            )
            used += len(content)
        return tuple(chosen)


def _transaction(pool: ConnectionPool, ctx: MemoryAccessContext):
    return memory_transaction(
        pool,
        tenant_id=str(ctx.tenant_id),
        actor_id=str(ctx.actor_id),
        allow_tenant_read=ctx.allow_tenant_read,
        allow_tenant_write=ctx.allow_tenant_write,
    )


def _audit(
    connection: Connection,
    ctx: MemoryAccessContext,
    memory_id: UUID,
    action: str,
    digest: str,
) -> None:
    connection.execute(
        """
        insert into agent.memory_audit (
            memory_id, tenant_id, actor_id, action, request_id, content_sha256
        ) values (%s, %s, %s, %s, %s, %s)
        """,
        (memory_id, ctx.tenant_id, ctx.actor_id, action, ctx.request_id, digest),
    )


def _visible(connection: Connection, memory_id: UUID) -> MemoryRecord:
    row = connection.execute(
        f"select {_COLUMNS} from agent.memory_item where id = %s",
        (memory_id,),
    ).fetchone()
    if row is None:
        raise MemoryNotFound("记忆不存在")
    return _record(row)


def _recall_scope(connection: Connection, scope: str, limit: int) -> list[tuple[object, ...]]:
    return connection.execute(
        """
        select id, scope, memory_type, content
        from agent.memory_item
        where scope = %s
          and status = 'active'
          and (expires_at is null or expires_at > now())
        order by updated_at desc, id
        limit %s
        """,
        (scope, limit),
    ).fetchall()


def _record(row: tuple[object, ...]) -> MemoryRecord:
    confidence = row[8]
    if not isinstance(confidence, Decimal):
        confidence = Decimal(str(confidence))
    return MemoryRecord(
        id=row[0],  # type: ignore[arg-type]
        tenant_id=row[1],  # type: ignore[arg-type]
        user_id=row[2],  # type: ignore[arg-type]
        scope=str(row[3]),
        memory_type=str(row[4]),
        content=str(row[5]),
        source=str(row[6]),
        source_conversation_id=row[7],  # type: ignore[arg-type]
        confidence=confidence,
        status=str(row[9]),
        created_at=row[10],  # type: ignore[arg-type]
        updated_at=row[11],  # type: ignore[arg-type]
        expires_at=row[12],  # type: ignore[arg-type]
        deleted_at=row[13],  # type: ignore[arg-type]
        created_by=row[14],  # type: ignore[arg-type]
        updated_by=row[15],  # type: ignore[arg-type]
        version=int(row[16]),
        content_sha256=str(row[17]),
    )


def _require_context(ctx: object) -> MemoryAccessContext:
    if type(ctx) is not MemoryAccessContext:
        raise MemoryValidationError("访问上下文不合法")
    return ctx


def _require_scope(scope: object) -> str:
    if scope not in _SCOPES:
        raise MemoryValidationError("记忆范围不合法")
    return str(scope)


def _require_type(memory_type: object) -> str:
    if not isinstance(memory_type, str):
        raise MemoryValidationError("记忆类型不合法")
    if memory_type in _BUSINESS_FACT_TYPES:
        raise MemoryValidationError("业务事实不能写入长期记忆")
    if memory_type not in _TYPES:
        raise MemoryValidationError("记忆类型不合法")
    return memory_type


def _require_content(content: object) -> str:
    if not isinstance(content, str):
        raise MemoryValidationError("记忆内容长度不合法")
    text = content.strip()
    if not text or len(text) > _CONTENT_LIMIT:
        raise MemoryValidationError("记忆内容长度不合法")
    if looks_sensitive(text):
        raise MemoryValidationError("记忆内容不符合保存要求")
    return text


def _require_expiry(value: object) -> datetime | None:
    if value is None:
        return None
    if type(value) is not datetime or value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise MemoryValidationError("到期时间不合法")
    return value.astimezone(timezone.utc)


def _require_conversation(value: object) -> UUID | None:
    if value is None:
        return None
    if type(value) is not UUID:
        raise MemoryValidationError("来源会话不合法")
    return value


def _require_id(value: object) -> UUID:
    if type(value) is not UUID:
        raise MemoryValidationError("记忆标识不合法")
    return value


def _require_version(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 1:
        raise MemoryValidationError("记忆版本不合法")
    return value


def _require_tenant_write(ctx: MemoryAccessContext, scope: str, message: str) -> None:
    if scope == "tenant" and not ctx.allow_tenant_write:
        raise MemoryForbidden(message)


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
