"""Redis lease for one tenant and one Knowledge QA thread.

The caller supplies the process Redis client, so this module does not open
another connection pool. The lease lasts 60 seconds. Renew and release compare
the owner token inside one Lua script. A Redis failure stays a store error and
is never replaced with an in-process lock. This module does not start the
renew loop.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

LOCK_KEY_PREFIX = "agent-run:v1:"
LOCK_TTL_MS = 60_000
LOCK_RENEW_INTERVAL_SECONDS = 20

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

# Redis runs each script to completion. No other command can land between the
# token comparison and PEXPIRE or DEL.
RENEW_SCRIPT = (
    "local current = redis.call('get', KEYS[1])\n"
    "if current == ARGV[1] then\n"
    "  return redis.call('pexpire', KEYS[1], tonumber(ARGV[2]))\n"
    "end\n"
    "return 0\n"
)
RELEASE_SCRIPT = (
    "local current = redis.call('get', KEYS[1])\n"
    "if current == ARGV[1] then\n"
    "  return redis.call('del', KEYS[1])\n"
    "end\n"
    "if not current then\n"
    "  return 0\n"
    "end\n"
    "return -1\n"
)


class LockError(Exception):
    """Base error for the distributed thread lock."""


class LockValidationError(LockError):
    """The tenant, thread, or owner token is not usable."""


class LockOwnershipLost(LockError):
    """This token no longer owns the lock."""


class LockStoreError(LockError):
    """Redis could not finish the command."""


@dataclass(frozen=True)
class LockAcquired:
    token: str

    def __repr__(self) -> str:
        return "LockAcquired(token=***)"


@dataclass(frozen=True)
class LockBusy:
    """Another execution owns this tenant thread."""


class RedisThreadLock:
    def __init__(self, client: Redis) -> None:
        if client is None:
            raise LockValidationError("Redis 客户端缺失")
        self._client = client

    async def acquire(self, tenant_id: UUID | None, thread_id: str | None) -> LockAcquired | LockBusy:
        """Take the lease once. A held lease is busy; a Redis failure is not."""
        key = lock_key(tenant_id, thread_id)
        token = secrets.token_urlsafe(32)
        try:
            stored = await self._client.set(key, token, nx=True, px=LOCK_TTL_MS)
        except RedisError as exc:
            raise _store_error(exc) from exc
        if stored is True:
            return LockAcquired(token)
        if stored is None:
            return LockBusy()
        raise LockStoreError("Redis 锁失败: UnexpectedSetReply")

    async def renew(self, tenant_id: UUID | None, thread_id: str | None, token: object) -> None:
        """Extend this owner's lease by another 60 seconds."""
        key = lock_key(tenant_id, thread_id)
        owner = _owner_token(token)
        try:
            result = await self._client.eval(RENEW_SCRIPT, 1, key, owner, LOCK_TTL_MS)
        except RedisError as exc:
            raise _store_error(exc) from exc
        if _script_result(result) == 1:
            return
        raise LockOwnershipLost("锁已丢失")

    async def release(self, tenant_id: UUID | None, thread_id: str | None, token: object) -> None:
        """Delete this owner's lease. A missing lease is already released."""
        key = lock_key(tenant_id, thread_id)
        owner = _owner_token(token)
        try:
            result = await self._client.eval(RELEASE_SCRIPT, 1, key, owner)
        except RedisError as exc:
            raise _store_error(exc) from exc
        reply = _script_result(result)
        if reply in (0, 1):
            return
        if reply == -1:
            raise LockOwnershipLost("锁已丢失")
        raise LockStoreError("Redis 锁失败: UnexpectedReleaseReply")


def lock_key(tenant_id: UUID | None, thread_id: str | None) -> str:
    """Build the only key shape this component writes."""
    tenant, thread = _identity(tenant_id, thread_id)
    return f"{LOCK_KEY_PREFIX}{tenant}:{thread}"


def _identity(tenant_id: UUID | None, thread_id: str | None) -> tuple[UUID, str]:
    if tenant_id is None:
        raise LockValidationError("租户上下文缺失")
    if type(tenant_id) is not UUID:
        raise LockValidationError("tenant_id 不合法")
    if type(thread_id) is not str:
        raise LockValidationError("thread_id 不合法")
    prefix = f"kq:{tenant_id}:"
    if not thread_id.startswith(prefix):
        raise LockValidationError("thread_id 不合法")
    try:
        conversation_id = UUID(thread_id[len(prefix):])
    except ValueError:
        raise LockValidationError("thread_id 不合法") from None
    canonical = f"kq:{tenant_id}:{conversation_id}"
    if thread_id != canonical:
        raise LockValidationError("thread_id 不合法")
    return tenant_id, canonical


def _owner_token(token: object) -> str:
    if type(token) is not str or _TOKEN_RE.fullmatch(token) is None:
        raise LockValidationError("owner token 不合法")
    return token


def _script_result(result: object) -> int:
    if type(result) is not int:
        raise LockStoreError("Redis 锁失败: UnexpectedReply")
    return result


def _store_error(exc: RedisError) -> LockStoreError:
    return LockStoreError(f"Redis 锁失败: {type(exc).__name__}")
