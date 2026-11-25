"""Run Redis lease commands on the loop that owns the client.

Graph execution is synchronous and usually runs on a worker thread. The Redis
client is async and stays on one event loop. This module schedules acquire,
renew, and release onto that loop and waits for them from the worker. It does
not open a second Redis connection.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from uuid import UUID

from langgraph.runtime import RunControl

from app.persistence.redis_thread_lock import LockOwnershipLost, LockStoreError

logger = logging.getLogger(__name__)


class LeaseLoop:
    """Private loop for synchronous callers that were not given one."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="knowledge-qa-lease", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("租约事件循环未能启动")

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()
        self.loop.close()

    def close(self) -> None:
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)


class LeaseBridge:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def call(self, coro: object) -> object:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            coro.close()  # type: ignore[attr-defined]
            raise LockStoreError("Redis 锁失败: CalledOnRedisLoop")
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()  # type: ignore[arg-type]


class ActiveLease:
    """One owner token, renewed until the graph worker releases it."""

    def __init__(
        self,
        lock: object,
        bridge: LeaseBridge,
        tenant_id: UUID,
        thread_id: str,
        token: str,
        interval: float,
        control: RunControl | None,
    ) -> None:
        self._lock = lock
        self._bridge = bridge
        self.tenant_id = tenant_id
        self.thread_id = thread_id
        self._token = token
        self._interval = interval
        self._control = control
        self.lost = threading.Event()
        self.reason: str | None = None
        self._stop: asyncio.Event | None = None
        self._task: object | None = None

    def __repr__(self) -> str:
        return "ActiveLease(token=***)"

    def start(self) -> None:
        self._stop = self._bridge.call(_event())  # type: ignore[assignment]
        self._task = asyncio.run_coroutine_threadsafe(self._renew(), self._bridge.loop)

    def close(self) -> None:
        if self._stop is not None:
            try:
                self._bridge.call(_signal(self._stop))
            except Exception as exc:
                logger.warning("knowledge qa renew stop failed error=%s", type(exc).__name__)
        task = self._task
        if task is not None:
            try:
                task.result()  # type: ignore[attr-defined]
            except Exception as exc:
                logger.warning("knowledge qa renew stopped error=%s", type(exc).__name__)
        try:
            self._bridge.call(self._lock.release(self.tenant_id, self.thread_id, self._token))  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("knowledge qa lease release failed error=%s", type(exc).__name__)

    async def _renew(self) -> None:
        stop = self._stop
        if stop is None:
            return
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), self._interval)
                return
            except TimeoutError:
                pass
            try:
                await self._lock.renew(self.tenant_id, self.thread_id, self._token)  # type: ignore[attr-defined]
            except LockOwnershipLost:
                self._lose("lease_lost")
                return
            except Exception:
                self._lose("lease_unavailable")
                return

    def _lose(self, reason: str) -> None:
        self.reason = reason
        self.lost.set()
        control = self._control
        if control is not None and not control.drain_requested:
            control.request_drain(reason)


async def _event() -> asyncio.Event:
    return asyncio.Event()


async def _signal(event: asyncio.Event) -> None:
    event.set()
