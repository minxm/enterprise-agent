"""Child-process entry points for Knowledge QA lease tests.

Each function creates its own Redis client and PostgreSQL pools. Callers must
pass configuration values, not live clients, pools, or event loops. The module
is imported again in a spawned interpreter.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agents.knowledge_qa.lease import LeaseBridge, LeaseLoop
from app.agents.knowledge_qa.service import KnowledgeQAService
from app.agents.knowledge_qa.threads import thread_id_for
from app.knowledge.rag import RAGAnswer, RAGSource
from app.persistence.checkpoint import CheckpointResources
from app.persistence.fence_context import fence_scope, tenant_scope
from app.persistence.fenced_saver import FencingViolation
from app.persistence.postgres import build_conninfo, tenant_transaction
from app.persistence.redis_client import build_redis_client
from app.persistence.redis_thread_lock import LockAcquired, LockOwnershipLost, RedisThreadLock
from app.persistence.thread_fence import FenceContextError, ThreadFenceStore
from psycopg_pool import ConnectionPool

DOC = UUID("00000000-0000-4000-8000-0000000000d7")
CHUNK = UUID("00000000-0000-4000-8000-0000000000c7")


class HoldingRAG:
    def __init__(self, entered, release, answer: str) -> None:
        self.entered = entered
        self.release = release
        self.answer = answer
        self.calls: list[str] = []
        self._default_top_k = 5
        self._default_strategy = "vector"

    def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None) -> RAGAnswer:
        del tenant_id, top_k, strategy
        self.calls.append(str(question))
        self.entered.set()
        if not self.release.wait(25):
            raise TimeoutError("测试节点没有等到放行")
        return _answer(self.answer)


class CountingRAG:
    def __init__(self, answer: str = "回答") -> None:
        self.answer = answer
        self.calls: list[str] = []
        self._default_top_k = 5
        self._default_strategy = "vector"

    def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None) -> RAGAnswer:
        del tenant_id, top_k, strategy
        self.calls.append(str(question))
        return _answer(self.answer)


class Bundle:
    """Redis, fence store, and checkpoint pool created inside one process."""

    def __init__(self, env: dict[str, str], *, redis_port: int | None = None, ping: bool = True) -> None:
        self.pid = os.getpid()
        self.env = env
        self.loop = LeaseLoop()
        self.bridge = LeaseBridge(self.loop.loop)
        self.redis = None
        self.checkpoints = None
        self.store_pool = None
        self.store = None
        self.lock = None
        port = int(env.get("REDIS_PORT", "6379")) if redis_port is None else redis_port
        self.redis = build_redis_client(env.get("REDIS_HOST", "127.0.0.1"), port, env["REDIS_PASSWORD"])
        if ping:
            self.bridge.call(self.redis.ping())
        conninfo = _conninfo(env)
        self.checkpoints = CheckpointResources(conninfo, env["AGENT_APP_PASSWORD"])
        self.checkpoints.open()
        self.store_pool = ConnectionPool(
            conninfo,
            min_size=1,
            max_size=2,
            timeout=10,
            kwargs={"options": "-c search_path=agent"},
            open=True,
        )
        self.store = ThreadFenceStore(self.store_pool)
        self.lock = RedisThreadLock(self.redis)

    def service(self, rag: object, renew_interval_seconds: float = 20) -> KnowledgeQAService:
        return KnowledgeQAService(
            rag,
            checkpointer=self.checkpoints.saver,
            thread_lock=self.lock,
            fence_store=self.store,
            lease_loop=self.loop.loop,
            renew_interval_seconds=renew_interval_seconds,
        )

    def close(self) -> None:
        redis = self.redis
        self.redis = None
        if redis is not None and self.loop.loop.is_running():
            try:
                self.bridge.call(redis.aclose())
            except Exception:
                pass
        try:
            self.loop.close()
        except Exception:
            pass
        pool = self.store_pool
        self.store_pool = None
        if pool is not None:
            pool.close()
        checkpoints = self.checkpoints
        self.checkpoints = None
        if checkpoints is not None:
            checkpoints.close()


def worker_hold_graph(env, tenant: str, conversation: str, entered, release, result, renew_interval: float = 20) -> None:
    _guard(result, env, lambda: _hold_graph(env, tenant, conversation, entered, release, result, renew_interval))


def worker_busy_then_run(env, tenant: str, conversation: str, busy_done, go_again, result) -> None:
    _guard(result, env, lambda: _busy_then_run(env, tenant, conversation, busy_done, go_again, result))


def worker_stale_writer(env, tenant: str, conversation: str, ready, go_write, result) -> None:
    _guard(result, env, lambda: _stale_writer(env, tenant, conversation, ready, go_write, result))


def worker_successor_writer(env, tenant: str, conversation: str, start, allocated, go_write, result) -> None:
    _guard(result, env, lambda: _successor_writer(env, tenant, conversation, start, allocated, go_write, result))


def worker_sleep_with_lock(env, tenant: str, conversation: str, ready) -> None:
    """Hold the lock until the process is killed. There is no release on this path."""
    bundle = Bundle(env)
    tenant_id = UUID(tenant)
    conversation_id = UUID(conversation)
    thread_id = thread_id_for(tenant_id, conversation_id)
    acquired = bundle.bridge.call(bundle.lock.acquire(tenant_id, thread_id))
    if not isinstance(acquired, LockAcquired):
        ready.set()
        return
    bundle.store.next_fence(tenant_id, thread_id)
    ready.set()
    threading.Event().wait(120)


def worker_run_graph(env, tenant: str, conversation: str, result) -> None:
    _guard(result, env, lambda: _run_graph(env, tenant, conversation, result))


def worker_reject_foreign_thread(env, tenant: str, own_conversation: str, foreign_thread: str, result) -> None:
    _guard(result, env, lambda: _reject_foreign_thread(env, tenant, own_conversation, foreign_thread, result))


def worker_stranger_renew(env, tenant: str, thread_id: str, result) -> None:
    _guard(result, env, lambda: _stranger_renew(env, tenant, thread_id, result))


def worker_renew_loss(env, tenant: str, conversation: str, entered, release, result) -> None:
    _guard(result, env, lambda: _renew_loss(env, tenant, conversation, entered, release, result))


def worker_jam_then_write(env, tenant: str, conversation: str, entered, release, result) -> None:
    _guard(result, env, lambda: _jam_then_write(env, tenant, conversation, entered, release, result))


def worker_redis_down(env, tenant: str, conversation: str, result) -> None:
    _guard(result, env, lambda: _redis_down(env, tenant, conversation, result))


def worker_read_history(env, tenant: str, conversation: str, first_done, go_again, result) -> None:
    _guard(result, env, lambda: _read_history(env, tenant, conversation, first_done, go_again, result))


def worker_write_checkpoint(env, tenant: str, conversation: str, result) -> None:
    _guard(result, env, lambda: _write_checkpoint(env, tenant, conversation, result))


def worker_read_checkpoint(env, tenant: str, thread_id: str, result) -> None:
    _guard(result, env, lambda: _read_checkpoint(env, tenant, thread_id, result))


def _hold_graph(env, tenant: str, conversation: str, entered, release, result, renew_interval: float) -> None:
    bundle = Bundle(env)
    try:
        rag = HoldingRAG(entered, release, "进程甲")
        service = bundle.service(rag, renew_interval)
        outcome = service.ask(UUID(tenant), "进程甲的问题", conversation_id=UUID(conversation))
        result.put(_outcome(bundle, outcome, calls=len(rag.calls)))
    finally:
        bundle.close()


def _busy_then_run(env, tenant: str, conversation: str, busy_done, go_again, result) -> None:
    bundle = Bundle(env)
    try:
        rag = CountingRAG("进程乙")
        service = bundle.service(rag)
        tenant_id = UUID(tenant)
        conversation_id = UUID(conversation)
        first = service.ask(tenant_id, "进程乙的第一次", conversation_id=conversation_id)
        first_calls = len(rag.calls)
        busy_done.set()
        if not go_again.wait(25):
            raise TimeoutError("第二轮执行没有等到放行")
        second = service.ask(tenant_id, "进程乙的第二次", conversation_id=conversation_id)
        result.put(
            {
                "pid": bundle.pid,
                "first_error": first.error_code,
                "first_calls": first_calls,
                "second_error": second.error_code,
                "second_answer": second.answer,
                "second_calls": len(rag.calls),
                "created_redis": bundle.redis is not None,
                "created_checkpoint_pool": bundle.checkpoints is not None and bundle.checkpoints.pool is not None,
                "created_fence_pool": bundle.store_pool is not None,
            }
        )
    finally:
        bundle.close()


def _stale_writer(env, tenant: str, conversation: str, ready, go_write, result) -> None:
    bundle = Bundle(env)
    tenant_id = UUID(tenant)
    conversation_id = UUID(conversation)
    thread_id = thread_id_for(tenant_id, conversation_id)
    acquired = None
    try:
        acquired = bundle.bridge.call(bundle.lock.acquire(tenant_id, thread_id))
        if not isinstance(acquired, LockAcquired):
            result.put({"pid": bundle.pid, "error": type(acquired).__name__})
            ready.set()
            return
        fence = bundle.store.next_fence(tenant_id, thread_id)
        ready.set()
        if not go_write.wait(30):
            raise TimeoutError("旧执行者没有等到写入通知")
        outcome = "wrote"
        try:
            _put(bundle, tenant_id, thread_id, fence, "cp-stale-process", "old")
        except FencingViolation:
            outcome = "FencingViolation"
        except Exception as exc:
            outcome = type(exc).__name__
        result.put({"pid": bundle.pid, "fence": fence, "outcome": outcome, "created_checkpoint_pool": True})
    finally:
        if isinstance(acquired, LockAcquired):
            try:
                bundle.bridge.call(bundle.lock.release(tenant_id, thread_id, acquired.token))
            except LockOwnershipLost:
                pass
        bundle.close()


def _successor_writer(env, tenant: str, conversation: str, start, allocated, go_write, result) -> None:
    bundle = Bundle(env)
    tenant_id = UUID(tenant)
    conversation_id = UUID(conversation)
    thread_id = thread_id_for(tenant_id, conversation_id)
    acquired = None
    try:
        if not start.wait(30):
            raise TimeoutError("新执行者没有等到开始通知")
        acquired = bundle.bridge.call(bundle.lock.acquire(tenant_id, thread_id))
        if not isinstance(acquired, LockAcquired):
            result.put({"pid": bundle.pid, "error": type(acquired).__name__})
            allocated.set()
            return
        fence = bundle.store.next_fence(tenant_id, thread_id)
        allocated.set()
        if not go_write.wait(30):
            raise TimeoutError("新执行者没有等到写入通知")
        saved = _put(bundle, tenant_id, thread_id, fence, "cp-successor-process", "new")
        result.put(
            {
                "pid": bundle.pid,
                "fence": fence,
                "checkpoint_id": saved["configurable"]["checkpoint_id"],
                "created_redis": True,
                "created_checkpoint_pool": True,
                "created_fence_pool": True,
            }
        )
    finally:
        if isinstance(acquired, LockAcquired):
            try:
                bundle.bridge.call(bundle.lock.release(tenant_id, thread_id, acquired.token))
            except LockOwnershipLost:
                pass
        bundle.close()


def _run_graph(env, tenant: str, conversation: str, result) -> None:
    bundle = Bundle(env)
    try:
        rag = CountingRAG("恢复后的回答")
        service = bundle.service(rag)
        outcome = service.ask(UUID(tenant), "恢复后的问题", conversation_id=UUID(conversation))
        result.put(_outcome(bundle, outcome, calls=len(rag.calls)))
    finally:
        bundle.close()


def _reject_foreign_thread(env, tenant: str, own_conversation: str, foreign_thread: str, result) -> None:
    bundle = Bundle(env)
    tenant_id = UUID(tenant)
    own_thread = thread_id_for(tenant_id, UUID(own_conversation))
    try:
        with tenant_transaction(bundle.store_pool, str(tenant_id)) as connection:
            visible = connection.execute("select count(*) from agent.thread_run").fetchone()[0]
            foreign = connection.execute(
                "select count(*) from agent.thread_run where thread_id = %s",
                (foreign_thread,),
            ).fetchone()[0]
        rejected = "wrote"
        checkpoint, versions = _checkpoint("cp-foreign-process", {"answer": "cross"})
        try:
            with fence_scope(tenant_id, own_thread, 1):
                bundle.checkpoints.saver.put(
                    {"configurable": {"thread_id": foreign_thread, "checkpoint_ns": ""}},
                    checkpoint,
                    {"source": "loop", "step": 1},
                    versions,
                )
        except FenceContextError:
            rejected = "FenceContextError"
        except Exception as exc:
            rejected = type(exc).__name__
        result.put({"pid": bundle.pid, "visible_fences": int(visible), "foreign_fences": int(foreign), "rejected": rejected})
    finally:
        bundle.close()


def _stranger_renew(env, tenant: str, thread_id: str, result) -> None:
    bundle = Bundle(env)
    try:
        try:
            bundle.bridge.call(bundle.lock.renew(UUID(tenant), thread_id, "b" * 43))
            lost = False
        except LockOwnershipLost:
            lost = True
        result.put({"pid": bundle.pid, "lost": lost})
    finally:
        bundle.close()


def _renew_loss(env, tenant: str, conversation: str, entered, release, result) -> None:
    bundle = Bundle(env)
    try:
        rag = HoldingRAG(entered, release, "不应完成")
        service = bundle.service(rag, renew_interval_seconds=3)
        events = list(service.stream_events(UUID(tenant), "续租失败的问题", conversation_id=UUID(conversation)))
        result.put(
            {
                "pid": bundle.pid,
                "calls": len(rag.calls),
                "nodes": [data.get("node") for name, data in events if name == "node_completed"],
                "error_code": events[-1][1].get("error_code") if events else None,
                "last_event": events[-1][0] if events else None,
            }
        )
    finally:
        bundle.close()


def _jam_then_write(env, tenant: str, conversation: str, entered, release, result) -> None:
    bundle = Bundle(env)
    try:
        rag = HoldingRAG(entered, release, "循环停顿后的回答")
        service = bundle.service(rag, renew_interval_seconds=5)

        async def block_loop() -> None:
            import time

            time.sleep(12)

        def jam() -> None:
            if entered.wait(20):
                bundle.bridge.call(block_loop())

        threading.Thread(target=jam, name="lease-loop-jam", daemon=True).start()
        outcome = service.ask(UUID(tenant), "循环停顿中的问题", conversation_id=UUID(conversation))
        result.put(_outcome(bundle, outcome, calls=len(rag.calls)))
    finally:
        bundle.close()


def _redis_down(env, tenant: str, conversation: str, result) -> None:
    bundle = Bundle(env, redis_port=1, ping=False)
    try:
        rag = CountingRAG("不应执行")
        service = bundle.service(rag)
        outcome = service.ask(UUID(tenant), "Redis 不可用", conversation_id=UUID(conversation))
        result.put(_outcome(bundle, outcome, calls=len(rag.calls)))
    finally:
        bundle.close()


def _read_history(env, tenant: str, conversation: str, first_done, go_again, result) -> None:
    bundle = Bundle(env)
    try:
        service = bundle.service(CountingRAG())
        tenant_id = UUID(tenant)
        conversation_id = UUID(conversation)
        first = service.read_history(tenant_id, conversation_id)
        first_done.set()
        if not go_again.wait(25):
            raise TimeoutError("历史读取没有等到第二轮")
        second = service.read_history(tenant_id, conversation_id)
        result.put(
            {
                "pid": bundle.pid,
                "first_error": first.error_code,
                "second_error": second.error_code,
                "second_contents": [message.content for message in second.messages],
            }
        )
    finally:
        bundle.close()


def _write_checkpoint(env, tenant: str, conversation: str, result) -> None:
    bundle = Bundle(env)
    try:
        tenant_id = UUID(tenant)
        thread_id = thread_id_for(tenant_id, UUID(conversation))
        fence = bundle.store.next_fence(tenant_id, thread_id)
        _put(bundle, tenant_id, thread_id, fence, "cp-rls-process", "tenant-a")
        with tenant_scope(tenant_id):
            loaded = bundle.checkpoints.saver.get_tuple(
                {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
            )
        answer = None if loaded is None else loaded.checkpoint["channel_values"].get("answer")
        result.put(
            {
                "pid": bundle.pid,
                "found": loaded is not None,
                "answer": answer,
                "created_checkpoint_pool": bundle.checkpoints is not None and bundle.checkpoints.pool is not None,
            }
        )
    finally:
        bundle.close()


def _read_checkpoint(env, tenant: str, thread_id: str, result) -> None:
    bundle = Bundle(env)
    try:
        with tenant_scope(UUID(tenant)):
            loaded = bundle.checkpoints.saver.get_tuple(
                {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
            )
        answer = None if loaded is None else loaded.checkpoint["channel_values"].get("answer")
        result.put(
            {
                "pid": bundle.pid,
                "found": loaded is not None,
                "answer": answer,
                "created_checkpoint_pool": bundle.checkpoints is not None and bundle.checkpoints.pool is not None,
            }
        )
    finally:
        bundle.close()


def _put(bundle: Bundle, tenant: UUID, thread_id: str, fence: int, checkpoint_id: str, answer: str):
    checkpoint, versions = _checkpoint(checkpoint_id, {"answer": answer, "payload": {"owner": answer}})
    with fence_scope(tenant, thread_id, fence):
        return bundle.checkpoints.saver.put(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
            checkpoint,
            {"source": "loop", "step": fence},
            versions,
        )


def _outcome(bundle: Bundle, outcome, *, calls: int) -> dict:
    return {
        "pid": bundle.pid,
        "error_code": outcome.error_code,
        "answer": outcome.answer,
        "calls": calls,
        "created_redis": bundle.redis is not None,
        "created_checkpoint_pool": bundle.checkpoints is not None and bundle.checkpoints.pool is not None,
        "created_fence_pool": bundle.store_pool is not None,
    }


def _guard(result, env: dict[str, str], body) -> None:
    try:
        body()
    except BaseException as exc:
        message = str(exc)
        for secret in (env.get("AGENT_APP_PASSWORD", ""), env.get("REDIS_PASSWORD", "")):
            if secret:
                message = message.replace(secret, "***")
        try:
            result.put({"pid": os.getpid(), "error": f"{type(exc).__name__}: {message[:400]}"})
        except Exception:
            pass


def _checkpoint(checkpoint_id: str, values: dict) -> tuple[dict, dict]:
    versions = {
        key: f"00000000000000000000000000000001.{index:016d}"
        for index, key in enumerate(values, start=1)
    }
    checkpoint = {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-11-25T08:00:00.000000+00:00",
        "channel_values": values,
        "channel_versions": versions,
        "versions_seen": {},
        "updated_channels": list(values),
    }
    return checkpoint, versions


def _answer(text: str) -> RAGAnswer:
    source = RAGSource(DOC, CHUNK, 0, f"{text}证据", 0.9, "fake", 0, 4)
    return RAGAnswer(text, (source,), 1, "fake", retrieval_strategy="vector", effective_strategy="vector")


def _conninfo(env: dict[str, str]) -> str:
    return build_conninfo(
        env.get("POSTGRES_HOST", "127.0.0.1"),
        int(env.get("POSTGRES_PORT", "5432")),
        env["POSTGRES_DB"],
        "agent_app",
        env["AGENT_APP_PASSWORD"],
    )
