import threading
import time
from uuid import UUID, uuid4

import pytest
from langgraph.runtime import RunControl

from app.agents.knowledge_qa.service import KnowledgeQAService
from app.agents.knowledge_qa.sse import iter_sse_bytes
from app.agents.knowledge_qa.threads import thread_id_for
from app.knowledge.rag import RAGAnswer, RAGSource
from app.persistence.checkpoint import CheckpointResources
from app.persistence.fence_context import current_fence
from app.persistence.fenced_saver import FencedPostgresSaver
from app.persistence.redis_thread_lock import LockOwnershipLost, LockStoreError
from app.persistence.thread_fence import FenceStoreError
from lease_fakes import FakeFenceStore, FakeThreadLock

TENANT = UUID("00000000-0000-4000-8000-00000000e551")
OTHER = UUID("00000000-0000-4000-8000-00000000e552")
DOC = UUID("00000000-0000-4000-8000-0000000000d1")
CHUNK = UUID("00000000-0000-4000-8000-0000000000c1")


class QuietSaver(FencedPostgresSaver):
    """Fenced saver that records the fence and does not touch PostgreSQL."""

    def __init__(self) -> None:
        super().__init__(object())
        self.fences: list[object] = []
        self.configs: list[dict] = []

    def get_tuple(self, config):
        self.configs.append(config)
        return None

    def put(self, config, checkpoint, metadata, new_versions):
        del metadata, new_versions
        self.fences.append(current_fence())
        return {
            "configurable": {
                "thread_id": config["configurable"]["thread_id"],
                "checkpoint_ns": "",
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put_writes(self, config, writes, task_id, task_path=""):
        del config, writes, task_id, task_path
        self.fences.append(current_fence())

    def list(self, config, *, filter=None, before=None, limit=None):
        del config, filter, before, limit
        return iter(())


class RecordingRAG:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fences: list[object] = []
        self._default_top_k = 5
        self._default_strategy = "vector"
        self._wait = None

    def ask(self, tenant_id, question, *, top_k=5, strategy=None):
        del tenant_id, top_k, strategy
        self.calls.append(str(question))
        try:
            self.fences.append(current_fence())
        except Exception as exc:
            self.fences.append(exc)
        if self._wait is not None:
            self._wait()
        return _answer("部门负责人审批。")


def test_redis_failure_does_not_allocate_a_fence_or_run_the_graph():
    rag = RecordingRAG()
    lock = FakeThreadLock()
    fence = FakeFenceStore()
    lock.fail_acquire = LockStoreError("Redis 锁失败: ConnectionError")
    service = _service(rag, lock, fence)
    try:
        result = service.ask(TENANT, "付款由谁审批？", conversation_id=uuid4())
    finally:
        service.close()

    assert result.error_code == "lease_unavailable"
    assert result.answer == ""
    assert rag.calls == []
    assert fence.values == {}
    assert lock.releases == []


def test_busy_lock_does_not_allocate_a_fence():
    rag = RecordingRAG()
    lock = FakeThreadLock()
    fence = FakeFenceStore()
    service = _service(rag, lock, fence)
    conversation = uuid4()
    thread_id = thread_id_for(TENANT, conversation)
    lock.hold(TENANT, thread_id)
    try:
        result = service.ask(TENANT, "付款由谁审批？", conversation_id=conversation)
    finally:
        lock.release_hold(TENANT, thread_id)
        service.close()

    assert result.error_code == "thread_busy"
    assert rag.calls == []
    assert fence.values == {}


def test_fence_failure_releases_the_redis_lease_and_skips_the_graph():
    rag = RecordingRAG()
    lock = FakeThreadLock()
    fence = FakeFenceStore()
    fence.fail = FenceStoreError("数据库事务失败")
    service = _service(rag, lock, fence)
    try:
        result = service.ask(TENANT, "付款由谁审批？", conversation_id=uuid4())
    finally:
        service.close()

    assert result.error_code == "lease_unavailable"
    assert rag.calls == []
    assert lock.releases
    assert lock.held == {}


def test_ready_run_allocates_a_fence_and_the_graph_uses_the_fenced_saver():
    rag = RecordingRAG()
    lock = FakeThreadLock()
    fence = FakeFenceStore()
    saver = QuietSaver()
    service = _service(rag, lock, fence, saver)
    conversation = uuid4()
    thread_id = thread_id_for(TENANT, conversation)
    try:
        result = service.ask(TENANT, "付款由谁审批？", conversation_id=conversation)
    finally:
        service.close()

    assert result.answer == "部门负责人审批。"
    assert service._graph.checkpointer is saver
    assert isinstance(service._graph.checkpointer, FencedPostgresSaver)
    assert fence.values[(TENANT, thread_id)] == 1
    assert rag.fences[0].tenant_id == TENANT
    assert rag.fences[0].thread_id == thread_id
    assert rag.fences[0].fence == 1
    assert saver.fences
    assert saver.fences[0].fence == 1
    assert saver.configs
    configurable = saver.configs[0]["configurable"]
    assert configurable["thread_id"] == thread_id
    assert configurable["tenant_id"] == str(TENANT)
    assert "fence" not in configurable
    assert lock.held == {}
    assert lock.releases == [(TENANT, thread_id)]


def test_graph_exception_is_not_replaced_when_release_fails():
    rag = RecordingRAG()
    lock = FakeThreadLock()
    lock.fail_release = RuntimeError("release-failed")
    service = _service(rag, lock, FakeFenceStore())

    def explode(*_args, **_kwargs):
        raise RuntimeError("graph-failed")

    service._graph.invoke = explode
    try:
        with pytest.raises(RuntimeError, match="graph-failed"):
            service.ask(TENANT, "付款由谁审批？", conversation_id=uuid4())
    finally:
        service.close()

    assert lock.releases


def test_successful_renew_lets_the_graph_finish():
    rag = RecordingRAG()
    lock = FakeThreadLock()
    rag._wait = lambda: _wait_until(lambda: lock.renews >= 1)
    service = _service(rag, lock, FakeFenceStore(), interval=0.05)
    try:
        result = service.ask(TENANT, "付款由谁审批？", conversation_id=uuid4())
    finally:
        service.close()

    assert result.answer == "部门负责人审批。"
    assert lock.renews >= 1
    assert lock.held == {}


def test_lost_lease_stops_the_next_graph_step():
    _assert_renew_stops(LockOwnershipLost("锁已丢失"), "lease_lost")


def test_renew_store_error_stops_the_next_graph_step():
    _assert_renew_stops(LockStoreError("Redis 锁失败: TimeoutError"), "lease_unavailable")


def test_sse_disconnect_keeps_the_lease_until_the_worker_leaves_the_graph():
    import asyncio

    started = threading.Event()
    release = threading.Event()
    held_while_running: list[bool] = []
    rag = RecordingRAG()

    def wait() -> None:
        held_while_running.append(current_fence())
        started.set()
        assert release.wait(5)

    rag._wait = wait
    lock = FakeThreadLock()
    service = _service(rag, lock, FakeFenceStore())
    conversation = uuid4()

    async def consume() -> None:
        agen = iter_sse_bytes(
            service,
            tenant_id=TENANT,
            question="付款由谁审批？",
            conversation_id=conversation,
            heartbeat_seconds=0.05,
            max_seconds=5,
        )
        async for _chunk in agen:
            if started.is_set():
                assert lock.held
                await agen.aclose()
                return

    async def main() -> None:
        task = asyncio.create_task(consume())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            assert lock.held
        finally:
            release.set()
            await task

    try:
        asyncio.run(main())
    finally:
        service.close()

    assert len(held_while_running) == 1
    assert held_while_running[0].tenant_id == TENANT
    assert held_while_running[0].thread_id == thread_id_for(TENANT, conversation)
    assert held_while_running[0].fence == 1
    assert lock.held == {}
    assert lock.releases


def test_concurrent_runs_keep_separate_fence_contexts():
    first = _capture(TENANT, uuid4())
    second = _capture(OTHER, uuid4())
    assert first.exception is None and second.exception is None
    assert first.fence.tenant_id == TENANT
    assert second.fence.tenant_id == OTHER
    assert first.fence.thread_id != second.fence.thread_id
    assert first.fence.fence == 1
    assert second.fence.fence == 1


def test_checkpoint_resources_install_the_fenced_saver(monkeypatch):
    class Pool:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def open(self, **kwargs):
            del kwargs

        def connection(self):
            return self

        def close(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return self

        def fetchone(self):
            return {"rolsuper": False}

    monkeypatch.setattr("app.persistence.checkpoint.ConnectionPool", Pool)
    resources = CheckpointResources("host=127.0.0.1 dbname=enterprise user=agent_app password=secret", "secret")
    resources.open()
    try:
        assert isinstance(resources.saver, FencedPostgresSaver)
        assert resources.saver.conn is resources.pool
    finally:
        resources.close()


def _assert_renew_stops(error: BaseException, code: str) -> None:
    rag = RecordingRAG()
    lock = FakeThreadLock()
    control = RunControl()

    def wait() -> None:
        lock.fail_renew = error
        _wait_until(lambda: control.drain_requested)

    rag._wait = wait
    service = _service(rag, lock, FakeFenceStore(), interval=0.05)
    conversation = uuid4()
    try:
        events = list(service.stream_events(TENANT, "付款由谁审批？", conversation_id=conversation, control=control))
    finally:
        service.close()

    names = [name for name, _data in events]
    assert "run_completed" not in names
    assert names[-1] == "run_failed"
    assert events[-1][1]["error_code"] == code
    assert "format_response" not in [data.get("node") for name, data in events if name == "node_completed"]
    assert rag.calls == ["付款由谁审批？"]
    assert lock.held == {}


class _Capture:
    def __init__(self) -> None:
        self.fence = None
        self.exception: BaseException | None = None


def _capture(tenant: UUID, conversation: UUID) -> _Capture:
    found = _Capture()
    rag = RecordingRAG()

    def remember() -> None:
        found.fence = current_fence()

    rag._wait = remember
    lock = FakeThreadLock()
    service = _service(rag, lock, FakeFenceStore())

    def run() -> None:
        try:
            service.ask(tenant, "付款由谁审批？", conversation_id=conversation)
        except BaseException as exc:
            found.exception = exc
        finally:
            service.close()

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(5)
    return found


def _service(rag: RecordingRAG, lock: FakeThreadLock, fence: FakeFenceStore, saver: object | None = None, interval: float = 20):
    return KnowledgeQAService(
        rag,
        checkpointer=saver or QuietSaver(),
        thread_lock=lock,
        fence_store=fence,
        renew_interval_seconds=interval,
    )


def _wait_until(ready) -> None:
    deadline = time.monotonic() + 2
    while not ready():
        if time.monotonic() > deadline:
            raise AssertionError("续租没有在预期时间内完成")
        time.sleep(0.01)


def _answer(text: str) -> RAGAnswer:
    source = RAGSource(DOC, CHUNK, 0, "证据", 0.9, "fake", 0, 4)
    return RAGAnswer(text, (source,), 1, "fake", retrieval_strategy="vector", effective_strategy="vector")
