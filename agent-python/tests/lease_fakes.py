import threading

from app.persistence.redis_thread_lock import LockAcquired, LockBusy, LockOwnershipLost
from app.persistence.thread_fence import FenceStoreError


class FakeThreadLock:
    """Async lock double whose state lives in a dict, not on an event loop."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self.held: dict[tuple[object, str], str] = {}
        self.acquire_calls = 0
        self.renews = 0
        self.releases: list[tuple[object, str]] = []
        self.fail_acquire: BaseException | None = None
        self.fail_renew: BaseException | None = None
        self.fail_release: BaseException | None = None
        self.renewed = threading.Event()

    def hold(self, tenant_id: object, thread_id: str) -> None:
        with self._guard:
            self.held[(tenant_id, thread_id)] = "h" * 43

    def release_hold(self, tenant_id: object, thread_id: str) -> None:
        with self._guard:
            self.held.pop((tenant_id, thread_id), None)

    async def acquire(self, tenant_id: object, thread_id: str):
        if self.fail_acquire is not None:
            raise self.fail_acquire
        with self._guard:
            self.acquire_calls += 1
            key = (tenant_id, thread_id)
            if key in self.held:
                return LockBusy()
            token = f"{self.acquire_calls:043d}"
            self.held[key] = token
            return LockAcquired(token)

    async def renew(self, tenant_id: object, thread_id: str, token: str) -> None:
        with self._guard:
            self.renews += 1
            current = self.held.get((tenant_id, thread_id))
            fail = self.fail_renew
        self.renewed.set()
        if fail is not None:
            raise fail
        if current != token:
            raise LockOwnershipLost("锁已丢失")

    async def release(self, tenant_id: object, thread_id: str, token: str) -> None:
        with self._guard:
            self.releases.append((tenant_id, thread_id))
            fail = self.fail_release
            current = self.held.get((tenant_id, thread_id))
            if fail is None and current == token:
                self.held.pop((tenant_id, thread_id), None)
        if fail is not None:
            raise fail


class FakeFenceStore:
    def __init__(self) -> None:
        self.values: dict[tuple[object, str], int] = {}
        self.fail: BaseException | None = None

    def next_fence(self, tenant_id: object, thread_id: str) -> int:
        if self.fail is not None:
            raise self.fail
        key = (tenant_id, thread_id)
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]


def fence_failure() -> FenceStoreError:
    return FenceStoreError("数据库事务失败")
