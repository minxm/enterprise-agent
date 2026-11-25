"""Knowledge QA execution against the local Redis and PostgreSQL.

Each test tracks its own tenant. Cleanup deletes that tenant's lock keys,
agent.thread_run rows, and checkpoint rows. It does not flush Redis or touch
the Docker volume.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import OperationalError

from app.agents.knowledge_qa.sse import iter_sse_bytes
from app.agents.knowledge_qa.threads import thread_id_for
from app.core.config import Settings
from app.knowledge.rag import RAGAnswer, RAGSource
from app.persistence.checkpoint import CheckpointResources
from app.persistence.fence_context import current_fence, fence_scope, tenant_scope
from app.persistence.fenced_saver import FencingViolation
from app.persistence.migrate import apply_migrations
from app.persistence.postgres import build_conninfo
from app.persistence.redis_client import build_redis_client
from app.persistence.redis_thread_lock import RedisThreadLock, lock_key
from app.persistence.thread_fence import FenceNotFound
from app.agents.knowledge_qa.lease import LeaseBridge, LeaseLoop
from app.agents.knowledge_qa.service import KnowledgeQAService
from qa_execution import ExecutionLease, ExecutionUnavailable

DOC = UUID("00000000-0000-4000-8000-0000000000d6")
CHUNK = UUID("00000000-0000-4000-8000-0000000000c6")


class ScriptedRAG:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []
        self._default_top_k = 5
        self._default_strategy = "vector"

    def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None) -> RAGAnswer:
        del tenant_id, top_k, strategy
        self.calls.append(str(question))
        return _answer(self.answers.pop(0))


class HoldingRAG:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[str] = []
        self.fences: list[object] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self._default_top_k = 5
        self._default_strategy = "vector"

    def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None) -> RAGAnswer:
        del tenant_id, top_k, strategy
        self.calls.append(str(question))
        self.fences.append(current_fence())
        self.entered.set()
        if not self.release.wait(20):
            raise TimeoutError("等待放行超时")
        return _answer(self.answer)


@pytest.fixture(scope="module")
def execution():
    env = _root_env()
    if not env.get("AGENT_APP_PASSWORD") or not env.get("AGENT_MIGRATOR_PASSWORD") or not env.get("POSTGRES_DB"):
        pytest.skip("仓库根目录 .env 未提供数据库密码，跳过真实数据库测试")
    settings = _settings(env)
    resources = None
    lease = None
    try:
        apply_migrations(settings)
        resources = CheckpointResources(
            build_conninfo(
                settings.postgres_host,
                settings.postgres_port,
                settings.postgres_db,
                settings.agent_app_user,
                settings.agent_app_password,
            ),
            settings.agent_app_password,
        )
        resources.open()
        lease = ExecutionLease.open(env)
    except ExecutionUnavailable as exc:
        if resources is not None:
            resources.close()
        pytest.skip(f"Redis 不可用，跳过执行链路测试: {exc.__class__.__name__}")
    except OperationalError as exc:
        if resources is not None:
            resources.close()
        pytest.skip(f"PostgreSQL 不可用，跳过真实数据库测试: {exc.__class__.__name__}")
    except RuntimeError as exc:
        if resources is not None:
            resources.close()
        if lease is not None:
            lease.close()
        if "连接" in str(exc) or "PostgreSQL" in str(exc) or "超级用户" in str(exc):
            pytest.skip(f"PostgreSQL 不可用，跳过真实数据库测试: {exc.__class__.__name__}")
        raise
    assert lease is not None and resources is not None and resources.saver is not None
    try:
        yield lease, resources
    finally:
        lease.close(resources.pool)
        resources.close()


@pytest.mark.postgres
def test_held_lock_allocates_fence_one_and_a_second_caller_is_busy(execution):
    lease, resources = execution
    tenant = uuid4()
    lease.track(tenant)
    conversation = uuid4()
    thread_id = thread_id_for(tenant, conversation)
    key = lock_key(tenant, thread_id)
    holder = HoldingRAG("第一轮")
    other = ScriptedRAG(["不应执行"])
    service = lease.service(holder, resources.saver)
    found: list[object] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            found.append(service.ask(tenant, "第一题", conversation_id=conversation))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert holder.entered.wait(15)
        assert lease.lock_exists(key)
        ttl = lease.lock_ttl(key)
        assert 30_000 < ttl <= 60_000
        assert holder.fences[0].tenant_id == tenant
        assert holder.fences[0].thread_id == thread_id
        assert holder.fences[0].fence == 1
        busy = lease.service(other, resources.saver).ask(tenant, "第二题", conversation_id=conversation)
    finally:
        holder.release.set()
        worker.join(20)

    assert not worker.is_alive()
    assert errors == []
    assert found[0].answer == "第一轮"
    assert busy.error_code == "thread_busy"
    assert other.calls == []
    assert lease.store.check_fence(tenant, thread_id, 1) == 1
    assert not lease.lock_exists(key)


@pytest.mark.postgres
def test_reacquire_increments_the_fence_and_only_the_current_fence_can_write(execution):
    lease, resources = execution
    tenant = uuid4()
    lease.track(tenant)
    conversation = uuid4()
    thread_id = thread_id_for(tenant, conversation)
    rag = ScriptedRAG(["第一轮", "第二轮"])
    service = lease.service(rag, resources.saver)

    first = service.ask(tenant, "第一题", conversation_id=conversation)
    assert lease.store.check_fence(tenant, thread_id, 1) == 1
    second = service.ask(tenant, "第二题", conversation_id=conversation)

    assert first.answer == "第一轮"
    assert second.answer == "第二轮"
    assert [turn["question"] for turn in second.turns] == ["第一题", "第二题"]
    assert lease.store.check_fence(tenant, thread_id, 2) == 2
    stale, stale_versions = _checkpoint("cp-stale-fence", {"answer": "expired"})
    with fence_scope(tenant, thread_id, 1):
        with pytest.raises(FencingViolation):
            resources.saver.put(_config(thread_id), stale, {"source": "loop", "step": 1}, stale_versions)
    with tenant_scope(tenant):
        assert resources.saver.get_tuple(_config(thread_id, "cp-stale-fence")) is None
    current, current_versions = _checkpoint("cp-current-fence", {"answer": "current"})
    with fence_scope(tenant, thread_id, 2):
        saved = resources.saver.put(_config(thread_id), current, {"source": "loop", "step": 2}, current_versions)
    with tenant_scope(tenant):
        loaded = resources.saver.get_tuple(saved)
    assert loaded is not None
    assert loaded.checkpoint["id"] == "cp-current-fence"
    assert loaded.checkpoint["channel_values"]["answer"] == "current"
    with resources.pool.connection() as connection:
        with connection.transaction():
            connection.execute("select set_config('app.tenant_id', %s, true)", (str(tenant),))
            stored = connection.execute(
                "select metadata::text as metadata from agent.checkpoints where thread_id = %s",
                (thread_id,),
            ).fetchall()
    assert stored
    assert all("__agent_execution_token" not in row["metadata"] for row in stored)


@pytest.mark.postgres
def test_two_redis_lock_instances_compete_for_one_session(execution):
    lease, resources = execution
    tenant = uuid4()
    lease.track(tenant)
    conversation = uuid4()
    thread_id = thread_id_for(tenant, conversation)
    holder = HoldingRAG("持有者")
    other = ScriptedRAG(["不应执行"])
    first = KnowledgeQAService(
        holder,
        checkpointer=resources.saver,
        thread_lock=lease.another_lock(),
        fence_store=lease.store,
        lease_loop=lease.loop.loop,
    )
    second = KnowledgeQAService(
        other,
        checkpointer=resources.saver,
        thread_lock=lease.another_lock(),
        fence_store=lease.store,
        lease_loop=lease.loop.loop,
    )
    assert first._thread_lock is not second._thread_lock
    errors: list[BaseException] = []

    def run() -> None:
        try:
            first.ask(tenant, "第一题", conversation_id=conversation)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert holder.entered.wait(15)
        busy = second.ask(tenant, "第二题", conversation_id=conversation)
    finally:
        holder.release.set()
        worker.join(20)

    assert not worker.is_alive()
    assert errors == []
    assert busy.error_code == "thread_busy"
    assert other.calls == []
    assert lease.store.check_fence(tenant, thread_id, 1) == 1


@pytest.mark.postgres
def test_expired_owner_cannot_write_after_a_new_fence(execution):
    lease, resources = execution
    tenant = uuid4()
    lease.track(tenant)
    conversation = uuid4()
    thread_id = thread_id_for(tenant, conversation)
    key = lock_key(tenant, thread_id)
    holder = HoldingRAG("过期执行者")
    successor_rag = ScriptedRAG(["新执行者"])
    previous = lease.service(holder, resources.saver)
    successor = lease.service(successor_rag, resources.saver)
    found: list[object] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            found.append(previous.ask(tenant, "旧问题", conversation_id=conversation))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert holder.entered.wait(15)
        assert holder.fences[0].fence == 1
        lease.expire_lock(key, 300)
        time.sleep(0.5)
        assert not lease.lock_exists(key)
        fresh = successor.ask(tenant, "新问题", conversation_id=conversation)
        assert fresh.answer == "新执行者"
        assert lease.store.check_fence(tenant, thread_id, 2) == 2
        stale, versions = _checkpoint("cp-expired-owner", {"answer": "old"})
        with fence_scope(tenant, thread_id, 1):
            with pytest.raises(FencingViolation):
                resources.saver.put(_config(thread_id), stale, {"source": "loop", "step": 1}, versions)
        current, current_versions = _checkpoint("cp-new-owner", {"answer": "new"})
        with fence_scope(tenant, thread_id, 2):
            saved = resources.saver.put(_config(thread_id), current, {"source": "loop", "step": 2}, current_versions)
        with tenant_scope(tenant):
            loaded = resources.saver.get_tuple(saved)
        assert loaded is not None
        assert loaded.checkpoint["channel_values"]["answer"] == "new"
    finally:
        holder.release.set()
        worker.join(20)

    assert not worker.is_alive()
    assert errors == []
    assert found[0].error_code == "lease_lost"
    assert found[0].answer == ""
    assert not lease.lock_exists(key)


@pytest.mark.postgres
def test_redis_outage_does_not_start_the_graph(execution):
    lease, resources = execution
    tenant = uuid4()
    lease.track(tenant)
    conversation = uuid4()
    thread_id = thread_id_for(tenant, conversation)
    loop = LeaseLoop()
    client = build_redis_client("127.0.0.1", 1, "redis-secret-value")
    rag = ScriptedRAG(["不应执行"])
    service = KnowledgeQAService(
        rag,
        checkpointer=resources.saver,
        thread_lock=RedisThreadLock(client),
        fence_store=lease.store,
        lease_loop=loop.loop,
    )
    try:
        result = service.ask(tenant, "付款由谁审批？", conversation_id=conversation)
    finally:
        try:
            LeaseBridge(loop.loop).call(client.aclose())
        finally:
            loop.close()

    assert result.error_code == "lease_unavailable"
    assert result.answer == ""
    assert "redis-secret-value" not in (result.error_message or "")
    assert rag.calls == []
    with pytest.raises(FenceNotFound):
        lease.store.check_fence(tenant, thread_id, 1)


@pytest.mark.postgres
def test_sse_worker_keeps_the_redis_lock_until_it_finishes(execution):
    lease, resources = execution
    tenant = uuid4()
    lease.track(tenant)
    conversation = uuid4()
    thread_id = thread_id_for(tenant, conversation)
    key = lock_key(tenant, thread_id)
    holder = HoldingRAG("流式回答")
    service = lease.service(holder, resources.saver)

    async def consume() -> bytes:
        chunks: list[bytes] = []
        agen = iter_sse_bytes(
            service,
            tenant_id=tenant,
            question="流式问题",
            conversation_id=conversation,
            heartbeat_seconds=0.05,
            max_seconds=20,
        )
        async for chunk in agen:
            chunks.append(chunk)
            if b"event: run_completed" in chunk or b"event: run_failed" in chunk:
                break
        return b"".join(chunks)

    async def main() -> bytes:
        task = asyncio.create_task(consume())
        try:
            assert await asyncio.to_thread(holder.entered.wait, 15)
            assert lease.lock_exists(key)
        finally:
            holder.release.set()
        return await task

    raw = asyncio.run(main())
    assert b"event: run_completed" in raw
    assert holder.fences[0].tenant_id == tenant
    assert holder.fences[0].thread_id == thread_id
    assert holder.fences[0].fence == 1
    assert not lease.lock_exists(key)


def _answer(text: str) -> RAGAnswer:
    source = RAGSource(DOC, CHUNK, 0, f"{text}证据", 0.9, "fake", 0, 4)
    return RAGAnswer(text, (source,), 1, "fake", retrieval_strategy="vector", effective_strategy="vector")


def _config(thread_id: str, checkpoint_id: str | None = None) -> dict:
    configurable = {"thread_id": thread_id, "checkpoint_ns": ""}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def _checkpoint(checkpoint_id: str, values: dict) -> tuple[dict, dict]:
    versions = {
        key: f"00000000000000000000000000000001.{index:016d}"
        for index, key in enumerate(values, start=1)
    }
    checkpoint = {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-11-23T08:00:00.000000+00:00",
        "channel_values": values,
        "channel_versions": versions,
        "versions_seen": {},
        "updated_channels": list(values),
    }
    return checkpoint, versions


def _settings(env: dict[str, str]) -> Settings:
    return Settings(
        postgres_host=env.get("POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(env.get("POSTGRES_PORT", "5432")),
        postgres_db=env["POSTGRES_DB"],
        agent_app_user="agent_app",
        agent_app_password=env["AGENT_APP_PASSWORD"],
        agent_migrator_user="agent_migrator",
        agent_migrator_password=env["AGENT_MIGRATOR_PASSWORD"],
    )


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
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values
