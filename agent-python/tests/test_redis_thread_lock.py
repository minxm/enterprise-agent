import asyncio
import inspect
import logging
from uuid import UUID, uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.persistence.redis_thread_lock import (
    LOCK_RENEW_INTERVAL_SECONDS,
    LOCK_TTL_MS,
    RELEASE_SCRIPT,
    RENEW_SCRIPT,
    LockAcquired,
    LockBusy,
    LockOwnershipLost,
    LockStoreError,
    LockValidationError,
    RedisThreadLock,
    lock_key,
)


class FakeRedis:
    """Records SET NX PX and EVAL. It has no GET, PEXPIRE, or DEL methods."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.deadlines: dict[str, int] = {}
        self.now = 0
        self.calls: list[tuple[object, ...]] = []
        self.fail: RedisError | None = None

    def advance(self, milliseconds: int) -> None:
        self.now += milliseconds

    def ttl(self, key: str) -> int | None:
        if self._live(key) is None:
            return None
        return self.deadlines[key] - self.now

    async def set(self, name: str, value: str, nx: bool = False, px: int | None = None):
        self.calls.append(("set", name, nx, px))
        if self.fail is not None:
            raise self.fail
        if nx and self._live(name) is not None:
            return None
        if type(px) is not int or px <= 0:
            raise RuntimeError("px required")
        self.values[name] = value
        self.deadlines[name] = self.now + px
        return True

    async def eval(self, script: str, numkeys: int, *keys_and_args: object):
        self.calls.append(("eval", script, numkeys))
        if self.fail is not None:
            raise self.fail
        if numkeys != 1 or not keys_and_args:
            raise RuntimeError("eval shape")
        key = str(keys_and_args[0])
        token = str(keys_and_args[1])
        current = self._live(key)
        if script == RENEW_SCRIPT:
            if current is not None and current == token:
                self.deadlines[key] = self.now + int(keys_and_args[2])
                return 1
            return 0
        if script == RELEASE_SCRIPT:
            if current is None:
                return 0
            if current != token:
                return -1
            self.values.pop(key, None)
            self.deadlines.pop(key, None)
            return 1
        raise RuntimeError("unexpected script")

    def _live(self, key: str) -> str | None:
        deadline = self.deadlines.get(key)
        if deadline is None or deadline <= self.now:
            self.values.pop(key, None)
            self.deadlines.pop(key, None)
            return None
        return self.values.get(key)


def test_acquire_succeeds():
    asyncio.run(_acquire_succeeds())


async def _acquire_succeeds():
    client = FakeRedis()
    tenant_id = uuid4()
    thread_id = _thread(tenant_id)

    acquired = await RedisThreadLock(client).acquire(tenant_id, thread_id)

    assert isinstance(acquired, LockAcquired)
    assert client.values[lock_key(tenant_id, thread_id)] == acquired.token
    assert client.calls == [("set", lock_key(tenant_id, thread_id), True, LOCK_TTL_MS)]
    assert client.ttl(lock_key(tenant_id, thread_id)) == LOCK_TTL_MS


def test_second_acquire_is_busy():
    asyncio.run(_second_acquire_is_busy())


async def _second_acquire_is_busy():
    client = FakeRedis()
    tenant_id = uuid4()
    thread_id = _thread(tenant_id)
    first = RedisThreadLock(client)
    second = RedisThreadLock(client)

    acquired = await first.acquire(tenant_id, thread_id)
    busy = await second.acquire(tenant_id, thread_id)

    assert isinstance(acquired, LockAcquired)
    assert isinstance(busy, LockBusy)
    assert client.values[lock_key(tenant_id, thread_id)] == acquired.token


def test_same_conversation_is_isolated_by_tenant():
    asyncio.run(_same_conversation_is_isolated_by_tenant())


async def _same_conversation_is_isolated_by_tenant():
    client = FakeRedis()
    conversation_id = uuid4()
    tenant_a, tenant_b = uuid4(), uuid4()
    thread_a = _thread(tenant_a, conversation_id)
    thread_b = _thread(tenant_b, conversation_id)

    first = await RedisThreadLock(client).acquire(tenant_a, thread_a)
    second = await RedisThreadLock(client).acquire(tenant_b, thread_b)

    assert isinstance(first, LockAcquired)
    assert isinstance(second, LockAcquired)
    assert lock_key(tenant_a, thread_a) != lock_key(tenant_b, thread_b)
    assert set(client.values) == {lock_key(tenant_a, thread_a), lock_key(tenant_b, thread_b)}


def test_different_threads_acquire_independently():
    asyncio.run(_different_threads_acquire_independently())


async def _different_threads_acquire_independently():
    client = FakeRedis()
    tenant_id = uuid4()
    first_thread = _thread(tenant_id)
    second_thread = _thread(tenant_id)

    first = await RedisThreadLock(client).acquire(tenant_id, first_thread)
    second = await RedisThreadLock(client).acquire(tenant_id, second_thread)

    assert isinstance(first, LockAcquired)
    assert isinstance(second, LockAcquired)
    assert lock_key(tenant_id, first_thread) != lock_key(tenant_id, second_thread)


def test_owner_can_renew_and_a_stranger_cannot():
    asyncio.run(_owner_can_renew_and_a_stranger_cannot())


async def _owner_can_renew_and_a_stranger_cannot():
    client = FakeRedis()
    lock = RedisThreadLock(client)
    tenant_id = uuid4()
    thread_id = _thread(tenant_id)
    key = lock_key(tenant_id, thread_id)
    acquired = await lock.acquire(tenant_id, thread_id)
    assert isinstance(acquired, LockAcquired)
    client.advance(20_000)
    stranger = "b" * 43

    await lock.renew(tenant_id, thread_id, acquired.token)

    assert client.ttl(key) == LOCK_TTL_MS
    assert [call[0] for call in client.calls].count("eval") == 1
    assert client.calls[-1] == ("eval", RENEW_SCRIPT, 1)
    deadline = client.deadlines[key]
    with pytest.raises(LockOwnershipLost):
        await lock.renew(tenant_id, thread_id, stranger)
    assert client.deadlines[key] == deadline
    assert client.values[key] == acquired.token


def test_owner_can_release_and_a_stranger_cannot():
    asyncio.run(_owner_can_release_and_a_stranger_cannot())


async def _owner_can_release_and_a_stranger_cannot():
    client = FakeRedis()
    lock = RedisThreadLock(client)
    tenant_id = uuid4()
    thread_id = _thread(tenant_id)
    key = lock_key(tenant_id, thread_id)
    acquired = await lock.acquire(tenant_id, thread_id)
    assert isinstance(acquired, LockAcquired)
    stranger = "c" * 43

    with pytest.raises(LockOwnershipLost):
        await lock.release(tenant_id, thread_id, stranger)
    assert client.values[key] == acquired.token
    assert client.calls[-1] == ("eval", RELEASE_SCRIPT, 1)

    await lock.release(tenant_id, thread_id, acquired.token)
    await lock.release(tenant_id, thread_id, acquired.token)

    assert key not in client.values
    assert client.ttl(key) is None


def test_expired_owner_cannot_touch_the_next_lease():
    asyncio.run(_expired_owner_cannot_touch_the_next_lease())


async def _expired_owner_cannot_touch_the_next_lease():
    client = FakeRedis()
    tenant_id = uuid4()
    thread_id = _thread(tenant_id)
    key = lock_key(tenant_id, thread_id)
    previous = RedisThreadLock(client)
    successor = RedisThreadLock(client)
    first = await previous.acquire(tenant_id, thread_id)
    assert isinstance(first, LockAcquired)
    client.advance(LOCK_TTL_MS)

    second = await successor.acquire(tenant_id, thread_id)

    assert isinstance(second, LockAcquired)
    assert second.token != first.token
    with pytest.raises(LockOwnershipLost):
        await previous.release(tenant_id, thread_id, first.token)
    assert client.values[key] == second.token
    with pytest.raises(LockOwnershipLost):
        await previous.renew(tenant_id, thread_id, first.token)
    assert client.values[key] == second.token
    assert client.ttl(key) == LOCK_TTL_MS


def test_redis_failure_is_a_store_error_and_not_a_local_lock():
    asyncio.run(_redis_failure_is_a_store_error_and_not_a_local_lock())


async def _redis_failure_is_a_store_error_and_not_a_local_lock():
    client = FakeRedis()
    lock = RedisThreadLock(client)
    tenant_id = uuid4()
    thread_id = _thread(tenant_id)
    client.fail = RedisConnectionError("password=redis-secret")

    with pytest.raises(LockStoreError, match="ConnectionError") as caught:
        await lock.acquire(tenant_id, thread_id)

    assert "password" not in str(caught.value)
    assert "redis-secret" not in str(caught.value)
    assert client.values == {}
    assert set(vars(lock)) == {"_client"}
    with pytest.raises(LockStoreError):
        await RedisThreadLock(client).acquire(tenant_id, thread_id)
    client.fail = None
    acquired = await lock.acquire(tenant_id, thread_id)
    assert isinstance(acquired, LockAcquired)

    client.fail = RedisTimeoutError("token=should-not-surface")
    with pytest.raises(LockStoreError, match="TimeoutError") as timed_out:
        await lock.renew(tenant_id, thread_id, acquired.token)
    assert "should-not-surface" not in str(timed_out.value)
    assert client.values[lock_key(tenant_id, thread_id)] == acquired.token

    client.fail = RedisError("password=hunter2")
    with pytest.raises(LockStoreError, match="RedisError") as failed:
        await lock.release(tenant_id, thread_id, acquired.token)
    assert "hunter2" not in str(failed.value)
    assert client.values[lock_key(tenant_id, thread_id)] == acquired.token


def test_invalid_identity_and_token_do_not_touch_redis():
    asyncio.run(_invalid_identity_and_token_do_not_touch_redis())


async def _invalid_identity_and_token_do_not_touch_redis():
    client = FakeRedis()
    lock = RedisThreadLock(client)
    tenant_id = uuid4()
    thread_id = _thread(tenant_id)
    foreign = _thread(uuid4())

    with pytest.raises(LockValidationError, match="租户上下文缺失"):
        await lock.acquire(None, thread_id)
    with pytest.raises(LockValidationError, match="tenant_id 不合法"):
        await lock.acquire("tenant", thread_id)  # type: ignore[arg-type]
    with pytest.raises(LockValidationError, match="thread_id 不合法"):
        await lock.acquire(tenant_id, None)
    with pytest.raises(LockValidationError, match="thread_id 不合法"):
        await lock.acquire(tenant_id, foreign)
    with pytest.raises(LockValidationError, match="thread_id 不合法"):
        await lock.acquire(tenant_id, f"agent-run:v1:{tenant_id}:{thread_id}")
    assert client.calls == []

    acquired = await lock.acquire(tenant_id, thread_id)
    assert isinstance(acquired, LockAcquired)
    client.calls.clear()
    with pytest.raises(LockValidationError, match="owner token 不合法"):
        await lock.renew(tenant_id, thread_id, "short")
    with pytest.raises(LockValidationError, match="owner token 不合法"):
        await lock.release(tenant_id, thread_id, "")
    assert client.calls == []
    assert client.values[lock_key(tenant_id, thread_id)] == acquired.token


def test_owner_token_is_random_and_not_visible_in_logs(caplog, monkeypatch):
    def token_urlsafe(nbytes: int) -> str:
        if nbytes != 32:
            raise AssertionError(nbytes)
        return "a" * 43

    monkeypatch.setattr("app.persistence.redis_thread_lock.secrets.token_urlsafe", token_urlsafe)
    caplog.set_level(logging.DEBUG)
    asyncio.run(_owner_token_is_random_and_not_visible_in_logs(caplog))


async def _owner_token_is_random_and_not_visible_in_logs(caplog):
    client = FakeRedis()
    lock = RedisThreadLock(client)
    tenant_id = uuid4()
    thread_id = _thread(tenant_id)
    before = len(asyncio.all_tasks())

    acquired = await lock.acquire(tenant_id, thread_id)

    assert isinstance(acquired, LockAcquired)
    assert acquired.token == "a" * 43
    assert acquired.token not in repr(acquired)
    assert acquired.token not in caplog.text
    assert acquired.token not in str(vars(lock))
    assert len(asyncio.all_tasks()) == before
    assert LOCK_RENEW_INTERVAL_SECONDS == 20
    assert "pexpire" in RENEW_SCRIPT and "del" not in RENEW_SCRIPT
    assert "del" in RELEASE_SCRIPT and "pexpire" not in RELEASE_SCRIPT


def test_commands_do_not_accept_a_raw_redis_key():
    assert "key" not in inspect.signature(RedisThreadLock.acquire).parameters
    assert "key" not in inspect.signature(RedisThreadLock.renew).parameters
    assert "key" not in inspect.signature(RedisThreadLock.release).parameters
    tenant_id = uuid4()
    assert lock_key(tenant_id, _thread(tenant_id)).startswith(f"agent-run:v1:{tenant_id}:kq:{tenant_id}:")


def _thread(tenant_id: UUID, conversation_id: UUID | None = None) -> str:
    return f"kq:{tenant_id}:{conversation_id or uuid4()}"
