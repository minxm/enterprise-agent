"""Internal memory API for the Java gateway.

The browser never calls this router. Java sends the shared internal token
and a caller block built from the authenticated user. Identity is accepted
only inside that block and is passed to MemoryAccessContext.for_trusted_caller.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.internal_auth import require_gateway
from app.core.request_context import current_request_id
from app.memory.context import MemoryAccessContext
from app.memory.errors import (
    MemoryAccessError,
    MemoryForbidden,
    MemoryNotFound,
    MemoryValidationError,
    MemoryVersionConflict,
)
from app.memory.service import MemoryRecord, MemoryService

router = APIRouter()
logger = logging.getLogger(__name__)
_SCOPES = {"user", "tenant"}
_TYPES = {"preference", "format", "work_scope", "organization_practice"}
_STATUSES = {"active", "archived", "expired", "deleted"}


class TrustedCaller(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    actor_id: UUID
    allow_tenant_read: bool
    allow_tenant_write: bool

    @field_validator("allow_tenant_read", "allow_tenant_write", mode="before")
    @classmethod
    def only_bool(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("授权必须是布尔值")
        return value


class CreateMemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    caller: TrustedCaller
    scope: Literal["user", "tenant"]
    memory_type: Literal["preference", "format", "work_scope", "organization_practice"]
    content: str = Field(min_length=1, max_length=1000)
    expires_at: datetime | None = None
    source_conversation_id: UUID | None = None


class UpdateMemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    caller: TrustedCaller
    expected_version: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=1000)
    expires_at: datetime | None = None

    @field_validator("expected_version", mode="before")
    @classmethod
    def only_int(cls, value: object) -> int:
        if type(value) is not int:
            raise ValueError("记忆版本不合法")
        return value


class VersionMemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    caller: TrustedCaller
    expected_version: int = Field(ge=1)

    @field_validator("expected_version", mode="before")
    @classmethod
    def only_int(cls, value: object) -> int:
        if type(value) is not int:
            raise ValueError("记忆版本不合法")
        return value


def get_memory_service(request: Request) -> MemoryService:
    service = getattr(request.app.state, "memory", None)
    if not isinstance(service, MemoryService):
        raise HTTPException(status_code=503, detail="记忆服务尚未就绪")
    return service


def _context(caller: TrustedCaller) -> MemoryAccessContext:
    return MemoryAccessContext.for_trusted_caller(
        tenant_id=caller.tenant_id,
        actor_id=caller.actor_id,
        allow_tenant_read=caller.allow_tenant_read,
        allow_tenant_write=caller.allow_tenant_write,
        request_id=current_request_id(),
    )


def _query_context(
    tenant_id: UUID = Query(),
    actor_id: UUID = Query(),
    allow_tenant_read: str = Query(),
    allow_tenant_write: str = Query(),
) -> MemoryAccessContext:
    return MemoryAccessContext.for_trusted_caller(
        tenant_id=tenant_id,
        actor_id=actor_id,
        allow_tenant_read=_strict_bool(allow_tenant_read),
        allow_tenant_write=_strict_bool(allow_tenant_write),
        request_id=current_request_id(),
    )


def _strict_bool(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise HTTPException(status_code=422, detail="请求参数不合法")


def _public(record: MemoryRecord) -> dict[str, object]:
    return {
        "id": str(record.id),
        "scope": record.scope,
        "memory_type": record.memory_type,
        "content": record.content,
        "source": record.source,
        "source_conversation_id": None
        if record.source_conversation_id is None
        else str(record.source_conversation_id),
        "status": record.status,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "expires_at": None if record.expires_at is None else record.expires_at.isoformat(),
        "deleted_at": None if record.deleted_at is None else record.deleted_at.isoformat(),
        "version": record.version,
    }


def _call(action):
    try:
        return action()
    except MemoryNotFound:
        raise HTTPException(status_code=404, detail="记忆不存在") from None
    except MemoryForbidden:
        raise HTTPException(status_code=403, detail="无权访问租户记忆") from None
    except MemoryVersionConflict:
        raise HTTPException(status_code=409, detail="记忆版本冲突") from None
    except (MemoryValidationError, MemoryAccessError):
        raise HTTPException(status_code=422, detail="请求参数不合法") from None
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("memory request failed error=%s", exc.__class__.__name__)
        raise HTTPException(status_code=500, detail="服务内部错误") from None


def _reject_unknown(request: Request, allowed: set[str]) -> None:
    unknown = set(request.query_params) - allowed
    if unknown:
        raise HTTPException(status_code=422, detail="请求参数不合法")


@router.post("/memories", dependencies=[Depends(require_gateway)])
def create_memory(body: CreateMemoryBody, service: MemoryService = Depends(get_memory_service)):
    if body.scope == "tenant" and not body.caller.allow_tenant_write:
        raise HTTPException(status_code=403, detail="无权访问租户记忆")
    context = _context(body.caller)
    record = _call(lambda: service.create(
        context,
        scope=body.scope,
        memory_type=body.memory_type,
        content=body.content,
        expires_at=body.expires_at,
        source_conversation_id=body.source_conversation_id,
    ))
    return _public(record)


@router.get("/memories", dependencies=[Depends(require_gateway)])
def list_memories(
    request: Request,
    context: MemoryAccessContext = Depends(_query_context),
    scope: str | None = Query(default=None),
    memory_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    include_expired: str = Query(default="false"),
    service: MemoryService = Depends(get_memory_service),
):
    _reject_unknown(
        request,
        {
            "tenant_id",
            "actor_id",
            "allow_tenant_read",
            "allow_tenant_write",
            "scope",
            "memory_type",
            "status",
            "include_expired",
        },
    )
    if scope is not None and scope not in _SCOPES:
        raise HTTPException(status_code=422, detail="请求参数不合法")
    if memory_type is not None and memory_type not in _TYPES:
        raise HTTPException(status_code=422, detail="请求参数不合法")
    if status is not None and status not in _STATUSES:
        raise HTTPException(status_code=422, detail="请求参数不合法")
    include = _strict_bool(include_expired)
    effective_scope = scope
    if effective_scope == "tenant" and not context.allow_tenant_read:
        raise HTTPException(status_code=403, detail="无权访问租户记忆")
    if effective_scope is None and not context.allow_tenant_read:
        effective_scope = "user"
    records = _call(lambda: service.list(
        context,
        scope=effective_scope,
        memory_type=memory_type,
        status=status,
        include_expired=include,
    ))
    return {"items": [_public(record) for record in records]}


@router.get("/memories/{memory_id}", dependencies=[Depends(require_gateway)])
def get_memory(
    memory_id: UUID,
    request: Request,
    context: MemoryAccessContext = Depends(_query_context),
    service: MemoryService = Depends(get_memory_service),
):
    _reject_unknown(
        request,
        {"tenant_id", "actor_id", "allow_tenant_read", "allow_tenant_write"},
    )
    record = _call(lambda: service.get(context, memory_id))
    if record.scope == "tenant" and not context.allow_tenant_read:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return _public(record)


@router.put("/memories/{memory_id}", dependencies=[Depends(require_gateway)])
def update_memory(
    memory_id: UUID,
    body: UpdateMemoryBody,
    service: MemoryService = Depends(get_memory_service),
):
    context = _context(body.caller)
    record = _call(lambda: service.update(
        context,
        memory_id=memory_id,
        expected_version=body.expected_version,
        content=body.content,
        expires_at=body.expires_at,
    ))
    return _public(record)


@router.post("/memories/{memory_id}/archive", dependencies=[Depends(require_gateway)])
def archive_memory(
    memory_id: UUID,
    body: VersionMemoryBody,
    service: MemoryService = Depends(get_memory_service),
):
    context = _context(body.caller)
    record = _call(lambda: service.archive(
        context,
        memory_id=memory_id,
        expected_version=body.expected_version,
    ))
    return _public(record)


@router.delete("/memories/{memory_id}", dependencies=[Depends(require_gateway)])
def delete_memory(
    memory_id: UUID,
    body: VersionMemoryBody,
    service: MemoryService = Depends(get_memory_service),
):
    context = _context(body.caller)
    record = _call(lambda: service.delete(
        context,
        memory_id=memory_id,
        expected_version=body.expected_version,
    ))
    return _public(record)
