"""Trusted identity for memory reads and writes.

Day39's Java internal call is the production constructor. It must pass the
tenant and actor taken from the authenticated server context, and the two
tenant-memory grants computed from permissions. This module does not read a
browser body, a GUC, or a model argument.

Tests may call for_trusted_caller directly. Nothing in MemoryService turns a
false grant into a true one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from app.memory.errors import MemoryAccessError

_REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


@dataclass(frozen=True)
class MemoryAccessContext:
    tenant_id: UUID
    actor_id: UUID
    allow_tenant_read: bool = False
    allow_tenant_write: bool = False
    request_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.tenant_id) is not UUID or type(self.actor_id) is not UUID:
            raise MemoryAccessError("身份必须是 UUID")
        if type(self.allow_tenant_read) is not bool or type(self.allow_tenant_write) is not bool:
            raise MemoryAccessError("租户记忆授权必须是布尔值")
        if self.request_id is None:
            return
        if type(self.request_id) is not str or _REQUEST_ID.fullmatch(self.request_id) is None:
            raise MemoryAccessError("request_id 不合法")

    @classmethod
    def for_trusted_caller(
        cls,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        allow_tenant_read: bool = False,
        allow_tenant_write: bool = False,
        request_id: str | None = None,
    ) -> MemoryAccessContext:
        """Build a context after the caller has already authenticated the actor."""
        return cls(
            tenant_id=tenant_id,
            actor_id=actor_id,
            allow_tenant_read=allow_tenant_read,
            allow_tenant_write=allow_tenant_write,
            request_id=request_id,
        )
