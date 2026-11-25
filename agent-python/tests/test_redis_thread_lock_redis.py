import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from redis.exceptions import RedisError

from app.persistence.redis_client import build_redis_client
from app.persistence.redis_thread_lock import (
    LOCK_TTL_MS,
    LockAcquired,
    LockBusy,
    LockOwnershipLost,
    LockStoreError,
    RedisThreadLock,
    lock_key,
)


def test_two_locks_compete_and_only_one_acquires():
    env = _require_env()
    asyncio.run(_two_locks_compete_and_only_one_acquires(env))


async def _two_locks_compete_and_only_one_acquires(env: dict[str, str]):
    tenant_id = uuid4()
    thread_id = _thread(tenant_id)
    async with _redis(env, tenant_id, thread_id) as (client, key):
        first = RedisThreadLock(client)
        second = RedisThreadLock(client)

        results = await asyncio.gather(first.acquire(tenant_id, thread_id), second.acquire(tenant_id, thread_id))

        acquired = [item for item in results if isinstance(item, LockAcquired)]
        busy = [item for item in results if isinstance(item, LockBusy)]
        assert len(acquired) == 1
        assert len(busy) == 1
        assert await client.get(key) == acquired[0].token
        again = await RedisThreadLock(client).acquire(tenant_id, thread_id)
        assert isinstance(again, LockBusy)
        assert await client.get(key) == acquired[0].token


def test_release_lets_the_next_lock_acquire():
    env = _require_env()
    asyncio.run(_release_lets_the_next_lock_acquire(env))


async def _release_lets_the_next_lock_acquire(env: dict[str, str]):
    tenant_id = uuid4()
    thread_id = _thread(tenant_id)
    async with _redis(env, tenant_id, thread_id) as (client, key):
        owner = RedisThreadLock(client)
        waiter = RedisThreadLock(client)
        acquired = await owner.acquire(tenant_id, thread_id)
        assert isinstance(acquired, LockAcquired)
        assert isinstance(await waiter.acquire(tenant_id, thread_id), LockBusy)

        await owner.release(tenant_id, thread_id, acquired.token)
        await owner.release(tenant_id, thread_id, acquired.token)
        assert await client.get(key) is None

        nxt = await waiter.acquire(tenant_id, thread_id)
        assert isinstance(nxt, LockAcquired)
        assert nxt.token != acquired.token
        assert await client.get(key) == nxt.token


def test_expired_owner_cannot_release_or_renew_the_new_lock():
    env = _require_env()
    asyncio.run(_expired_owner_cannot_release_or_renew_the_new_lock(env))


async def _expired_owner_cannot_release_or_renew_the_new_lock(env: dict[str, str]):
    tenant_id = uuid4()
    thread_id = _thread(tenant_id)
    async with _redis(env, tenant_id, thread_id) as (client, key):
        previous = RedisThreadLock(client)
        successor = RedisThreadLock(client)
        first = await previous.acquire(tenant_id, thread_id)
        assert isinstance(first, LockAcquired)
        initial = await client.pttl(key)
        assert LOCK_TTL_MS - 2_000 <= initial <= LOCK_TTL_MS

        # The component always leases for 60 seconds. Shorten that remaining
        # TTL in Redis so this test can observe a real expiry without sleeping.
        await client.pexpire(key, 300)
        await asyncio.sleep(0.5)
        assert await client.get(key) is None

        second = await successor.acquire(tenant_id, thread_id)
        assert isinstance(second, LockAcquired)
        assert second.token != first.token
        with pytest.raises(LockOwnershipLost):
            await previous.release(tenant_id, thread_id, first.token)
        assert await client.get(key) == second.token
        with pytest.raises(LockOwnershipLost):
            await previous.renew(tenant_id, thread_id, first.token)
        assert await client.get(key) == second.token
        assert await client.pttl(key) > 10_000


def test_owner_renew_extends_the_redis_ttl():
    env = _require_env()
    asyncio.run(_owner_renew_extends_the_redis_ttl(env))


async def _owner_renew_extends_the_redis_ttl(env: dict[str, str]):
    tenant_id = uuid4()
    thread_id = _thread(tenant_id)
    async with _redis(env, tenant_id, thread_id) as (client, key):
        lock = RedisThreadLock(client)
        stranger = RedisThreadLock(client)
        acquired = await lock.acquire(tenant_id, thread_id)
        assert isinstance(acquired, LockAcquired)
        await asyncio.sleep(1.2)
        before = await client.pttl(key)
        assert 0 < before < LOCK_TTL_MS - 1_000

        await lock.renew(tenant_id, thread_id, acquired.token)

        after = await client.pttl(key)
        assert after > before
        assert after >= LOCK_TTL_MS - 2_000
        other = "d" * 43
        with pytest.raises(LockOwnershipLost):
            await stranger.renew(tenant_id, thread_id, other)
        assert await client.get(key) == acquired.token
        assert await client.pttl(key) > 10_000


def test_unavailable_redis_does_not_grant_a_local_lock():
    asyncio.run(_unavailable_redis_does_not_grant_a_local_lock())


async def _unavailable_redis_does_not_grant_a_local_lock():
    client = build_redis_client("127.0.0.1", 1, "redis-secret-value")
    lock = RedisThreadLock(client)
    tenant_id = uuid4()
    thread_id = _thread(tenant_id)
    try:
        with pytest.raises(LockStoreError, match="Redis 锁失败") as caught:
            await lock.acquire(tenant_id, thread_id)
        assert "redis-secret-value" not in str(caught.value)
        assert not isinstance(caught.value, LockBusy)
        with pytest.raises(LockStoreError):
            await lock.acquire(tenant_id, thread_id)
        assert set(vars(lock)) == {"_client"}
        assert isinstance(caught.value.__cause__, RedisError)
    finally:
        await client.aclose()


class _RedisLease:
    def __init__(self, env: dict[str, str], tenant_id: UUID, thread_id: str) -> None:
        self._env = env
        self.key = lock_key(tenant_id, thread_id)
        self.client = None

    async def __aenter__(self):
        self.client = build_redis_client(
            self._env.get("REDIS_HOST", "127.0.0.1"),
            int(self._env.get("REDIS_PORT", "6379")),
            self._env["REDIS_PASSWORD"],
        )
        await self.client.ping()
        return self.client, self.key

    async def __aexit__(self, *_args):
        if self.client is not None:
            await self.client.delete(self.key)
            await self.client.aclose()
        return False


def _redis(env: dict[str, str], tenant_id: UUID, thread_id: str) -> _RedisLease:
    return _RedisLease(env, tenant_id, thread_id)


def _require_env() -> dict[str, str]:
    env = _root_env()
    if not env.get("REDIS_PASSWORD"):
        pytest.skip("仓库根目录 .env 未提供 REDIS_PASSWORD，跳过真实 Redis 测试")
    return env


def _root_env() -> dict[str, str]:
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _thread(tenant_id: UUID, conversation_id: UUID | None = None) -> str:
    return f"kq:{tenant_id}:{conversation_id or uuid4()}"
