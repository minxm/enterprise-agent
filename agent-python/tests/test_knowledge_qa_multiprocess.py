"""Two operating-system processes sharing one Redis and one PostgreSQL.

These tests use spawn. A child receives configuration and synchronization
events only. It opens its own Redis client, fence pool, and checkpoint pool.
Nothing here mocks the lock, the fence, or the saver.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import OperationalError
from queue import Empty

from app.agents.knowledge_qa.threads import thread_id_for
from app.core.config import Settings
from app.persistence.checkpoint import CheckpointResources
from app.persistence.fence_context import tenant_scope
from app.persistence.migrate import apply_migrations
from app.persistence.postgres import build_conninfo
from app.persistence.redis_thread_lock import lock_key
from app.persistence.thread_fence import FenceNotFound
from mp_knowledge_qa import (
    worker_busy_then_run,
    worker_hold_graph,
    worker_jam_then_write,
    worker_read_checkpoint,
    worker_read_history,
    worker_redis_down,
    worker_reject_foreign_thread,
    worker_renew_loss,
    worker_run_graph,
    worker_sleep_with_lock,
    worker_stale_writer,
    worker_stranger_renew,
    worker_successor_writer,
    worker_write_checkpoint,
)
from qa_execution import ExecutionLease, ExecutionUnavailable


@pytest.fixture(scope="module")
def execution():
    _prepare_spawn()
    env = _root_env()
    if not env.get("AGENT_APP_PASSWORD") or not env.get("AGENT_MIGRATOR_PASSWORD") or not env.get("POSTGRES_DB"):
        pytest.skip("仓库根目录 .env 未提供数据库密码，跳过真实数据库测试")
    if not env.get("REDIS_PASSWORD"):
        pytest.skip("仓库根目录 .env 未提供 Redis 密码，跳过多进程测试")
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
        pytest.skip(f"Redis 不可用，跳过多进程测试: {exc.__class__.__name__}")
    except OperationalError as exc:
        if resources is not None:
            resources.close()
        pytest.skip(f"PostgreSQL 不可用，跳过多进程测试: {exc.__class__.__name__}")
    except RuntimeError as exc:
        if resources is not None:
            resources.close()
        if lease is not None:
            lease.close()
        if "连接" in str(exc) or "PostgreSQL" in str(exc) or "超级用户" in str(exc):
            pytest.skip(f"PostgreSQL 不可用，跳过多进程测试: {exc.__class__.__name__}")
        raise
    assert lease is not None and resources is not None and resources.saver is not None and resources.pool is not None
    try:
        yield lease, resources
    finally:
        lease.close(resources.pool)
        resources.close()


@pytest.fixture
def spawn_context():
    return mp.get_context("spawn")


@pytest.mark.postgres
def test_two_processes_compete_for_one_session(execution, spawn_context):
    lease, _resources = execution
    tenant, conversation = uuid4(), uuid4()
    lease.track(tenant)
    thread = thread_id_for(tenant, conversation)
    key = lock_key(tenant, thread)
    entered, release = spawn_context.Event(), spawn_context.Event()
    busy_done, go_again = spawn_context.Event(), spawn_context.Event()
    first, second = spawn_context.Queue(), spawn_context.Queue()
    procs: list = []
    try:
        holder = _spawn(spawn_context, procs, worker_hold_graph, (lease.env, str(tenant), str(conversation), entered, release, first))
        assert entered.wait(40), "持有进程没有进入测试节点"
        assert lease.lock_exists(key)
        assert lease.store is not None and lease.store.check_fence(tenant, thread, 1) == 1
        rival = _spawn(
            spawn_context,
            procs,
            worker_busy_then_run,
            (lease.env, str(tenant), str(conversation), busy_done, go_again, second),
        )
        assert busy_done.wait(40), "竞争进程没有返回第一次结果"
        assert holder.pid != rival.pid
        assert holder.pid != os.getpid() and rival.pid != os.getpid()
        print(f"MULTIPROCESS compete pid_a={holder.pid} pid_b={rival.pid} parent={os.getpid()}")
        assert lease.store.check_fence(tenant, thread, 1) == 1
        assert lease.lock_exists(key)
        release.set()
        held = _take(first, 40)
        _join(holder)
        assert held["error_code"] is None
        assert held["calls"] == 1
        assert held["created_redis"] is True
        assert held["created_checkpoint_pool"] is True
        assert held["created_fence_pool"] is True
        assert not lease.lock_exists(key)
        go_again.set()
        competed = _take(second, 40)
        _join(rival)
    finally:
        _stop(procs)
        _drop(first, second)
    assert competed["first_error"] == "thread_busy"
    assert competed["first_calls"] == 0
    assert competed["second_error"] is None
    assert competed["second_answer"] == "进程乙"
    assert competed["second_calls"] == 1
    assert competed["pid"] == rival.pid
    assert competed["created_redis"] is True and competed["created_checkpoint_pool"] is True
    assert lease.store.check_fence(tenant, thread, 2) == 2


@pytest.mark.postgres
def test_expired_owner_process_cannot_write_after_successor_fence(execution, spawn_context):
    lease, resources = execution
    tenant, conversation = uuid4(), uuid4()
    lease.track(tenant)
    thread = thread_id_for(tenant, conversation)
    key = lock_key(tenant, thread)
    ready, start = spawn_context.Event(), spawn_context.Event()
    allocated = spawn_context.Event()
    write_old, write_new = spawn_context.Event(), spawn_context.Event()
    old_queue, new_queue = spawn_context.Queue(), spawn_context.Queue()
    procs: list = []
    try:
        old = _spawn(spawn_context, procs, worker_stale_writer, (lease.env, str(tenant), str(conversation), ready, write_old, old_queue))
        new = _spawn(
            spawn_context,
            procs,
            worker_successor_writer,
            (lease.env, str(tenant), str(conversation), start, allocated, write_new, new_queue),
        )
        assert ready.wait(40), "旧执行者没有完成 fence=1"
        original_ttl = lease.lock_ttl(key)
        assert 30_000 < original_ttl <= 60_000
        assert lease.store is not None and lease.store.check_fence(tenant, thread, 1) == 1
        assert _counts(resources.pool, thread) == (0, 0, 0)
        lease.expire_lock(key, 400)
        _wait_until_absent(lease, key, 5)
        assert lease.store.check_fence(tenant, thread, 1) == 1
        start.set()
        assert allocated.wait(30), "新执行者没有分配 fence"
        assert lease.store.check_fence(tenant, thread, 2) == 2
        assert old.pid != new.pid and old.pid != os.getpid()
        print(f"MULTIPROCESS fence pid_a={old.pid} pid_b={new.pid} parent={os.getpid()} original_pttl={original_ttl}")
        write_old.set()
        rejected = _take(old_queue, 30)
        assert rejected["outcome"] == "FencingViolation"
        assert rejected["fence"] == 1
        assert rejected["pid"] == old.pid
        assert rejected["created_checkpoint_pool"] is True
        assert _counts(resources.pool, thread) == (0, 0, 0)
        write_new.set()
        accepted = _take(new_queue, 30)
        _join(old)
        _join(new)
    finally:
        _stop(procs)
        _drop(old_queue, new_queue)
    assert accepted["fence"] == 2
    assert accepted["pid"] == new.pid
    assert accepted["checkpoint_id"] == "cp-successor-process"
    assert accepted["created_redis"] is True and accepted["created_fence_pool"] is True
    with tenant_scope(tenant):
        loaded = resources.saver.get_tuple({"configurable": {"thread_id": thread, "checkpoint_ns": ""}})
    assert loaded is not None
    assert loaded.checkpoint["id"] == "cp-successor-process"
    assert loaded.checkpoint["channel_values"]["answer"] == "new"
    assert loaded.checkpoint["channel_values"]["payload"] == {"owner": "new"}
    assert _counts(resources.pool, thread) == (1, 1, 0)


@pytest.mark.postgres
def test_killed_process_lock_expires_and_successor_writes(execution, spawn_context):
    lease, resources = execution
    tenant, conversation = uuid4(), uuid4()
    lease.track(tenant)
    thread = thread_id_for(tenant, conversation)
    key = lock_key(tenant, thread)
    ready = spawn_context.Event()
    result = spawn_context.Queue()
    procs: list = []
    try:
        killed = _spawn(spawn_context, procs, worker_sleep_with_lock, (lease.env, str(tenant), str(conversation), ready))
        assert ready.wait(40), "被终止进程没有拿到锁"
        original_ttl = lease.lock_ttl(key)
        assert 30_000 < original_ttl <= 60_000
        assert lease.store is not None and lease.store.check_fence(tenant, thread, 1) == 1
        killed.kill()
        killed.join(10)
        assert not killed.is_alive()
        assert killed.exitcode not in (0, None)
        assert lease.lock_exists(key)
        remaining = lease.lock_ttl(key)
        assert 0 < remaining <= original_ttl
        print(
            f"MULTIPROCESS kill pid_a={killed.pid} parent={os.getpid()} "
            f"exit={killed.exitcode} original_pttl={original_ttl} remaining_pttl={remaining}"
        )
        _wait_until_absent(lease, key, 75)
        assert not lease.lock_exists(key)
        successor = _spawn(spawn_context, procs, worker_run_graph, (lease.env, str(tenant), str(conversation), result))
        restored = _take(result, 40)
        _join(successor)
    finally:
        _stop(procs)
        _drop(result)
    assert successor.pid != killed.pid
    print(f"MULTIPROCESS kill-recovery pid_b={successor.pid}")
    assert restored["pid"] == successor.pid
    assert restored["error_code"] is None
    assert restored["calls"] == 1
    assert restored["answer"] == "恢复后的回答"
    assert restored["created_redis"] is True and restored["created_checkpoint_pool"] is True
    assert lease.store.check_fence(tenant, thread, 2) == 2
    with tenant_scope(tenant):
        loaded = resources.saver.get_tuple({"configurable": {"thread_id": thread, "checkpoint_ns": ""}})
    assert loaded is not None
    assert _counts(resources.pool, thread)[0] >= 1


@pytest.mark.postgres
def test_processes_isolate_conversations_and_tenants(execution, spawn_context):
    lease, resources = execution
    tenant = uuid4()
    first_conversation, second_conversation = uuid4(), uuid4()
    lease.track(tenant)
    first_thread = thread_id_for(tenant, first_conversation)
    second_thread = thread_id_for(tenant, second_conversation)
    first_key = lock_key(tenant, first_thread)
    second_key = lock_key(tenant, second_thread)
    entered_a, release_a = spawn_context.Event(), spawn_context.Event()
    entered_b, release_b = spawn_context.Event(), spawn_context.Event()
    queue_a, queue_b = spawn_context.Queue(), spawn_context.Queue()
    queues = [queue_a, queue_b]
    procs: list = []
    try:
        left = _spawn(
            spawn_context,
            procs,
            worker_hold_graph,
            (lease.env, str(tenant), str(first_conversation), entered_a, release_a, queue_a),
        )
        right = _spawn(
            spawn_context,
            procs,
            worker_hold_graph,
            (lease.env, str(tenant), str(second_conversation), entered_b, release_b, queue_b),
        )
        assert entered_a.wait(40) and entered_b.wait(40)
        assert left.pid != right.pid
        assert lease.lock_exists(first_key) and lease.lock_exists(second_key)
        assert first_key != second_key
        assert first_key == f"agent-run:v1:{tenant}:{first_thread}"
        assert lease.store is not None
        assert lease.store.check_fence(tenant, first_thread, 1) == 1
        assert lease.store.check_fence(tenant, second_thread, 1) == 1
        release_a.set()
        release_b.set()
        _take(queue_a, 40)
        _take(queue_b, 40)
        _join(left)
        _join(right)

        other = uuid4()
        lease.track(other)
        shared = uuid4()
        own_thread = thread_id_for(tenant, shared)
        foreign_thread = thread_id_for(other, shared)
        own_key = lock_key(tenant, own_thread)
        foreign_key = lock_key(other, foreign_thread)
        entered_own, release_own = spawn_context.Event(), spawn_context.Event()
        entered_foreign, release_foreign = spawn_context.Event(), spawn_context.Event()
        own_queue, foreign_queue = spawn_context.Queue(), spawn_context.Queue()
        queues.extend((own_queue, foreign_queue))
        own = _spawn(spawn_context, procs, worker_hold_graph, (lease.env, str(tenant), str(shared), entered_own, release_own, own_queue))
        foreign = _spawn(
            spawn_context,
            procs,
            worker_hold_graph,
            (lease.env, str(other), str(shared), entered_foreign, release_foreign, foreign_queue),
        )
        assert entered_own.wait(40) and entered_foreign.wait(40)
        assert own.pid != foreign.pid
        assert lease.lock_exists(own_key) and lease.lock_exists(foreign_key)
        assert own_key != foreign_key
        assert lease.store.check_fence(tenant, own_thread, 1) == 1
        assert lease.store.check_fence(other, foreign_thread, 1) == 1
        print(f"MULTIPROCESS isolate pid_a={own.pid} pid_b={foreign.pid} parent={os.getpid()}")
        release_own.set()
        release_foreign.set()
        _take(own_queue, 40)
        _take(foreign_queue, 40)
        _join(own)
        _join(foreign)
        before = _counts(resources.pool, own_thread)
        assert before[0] >= 1
        verdict_queue = spawn_context.Queue()
        queues.append(verdict_queue)
        checker = _spawn(
            spawn_context,
            procs,
            worker_reject_foreign_thread,
            (lease.env, str(other), str(shared), own_thread, verdict_queue),
        )
        verdict = _take(verdict_queue, 30)
        _join(checker)
    finally:
        _stop(procs)
        _drop(*queues)
    assert verdict["visible_fences"] == 1
    assert verdict["foreign_fences"] == 0
    assert verdict["rejected"] == "FenceContextError"
    assert verdict["pid"] not in (os.getpid(),)
    assert _counts(resources.pool, own_thread) == before


@pytest.mark.postgres
def test_owner_renew_restores_ttl_and_stranger_cannot_renew(execution, spawn_context):
    lease, _resources = execution
    tenant, conversation = uuid4(), uuid4()
    lease.track(tenant)
    thread = thread_id_for(tenant, conversation)
    key = lock_key(tenant, thread)
    entered, release = spawn_context.Event(), spawn_context.Event()
    held_queue, stranger_queue = spawn_context.Queue(), spawn_context.Queue()
    procs: list = []
    try:
        holder = _spawn(
            spawn_context,
            procs,
            worker_hold_graph,
            (lease.env, str(tenant), str(conversation), entered, release, held_queue, 0.4),
        )
        assert entered.wait(40)
        initial = lease.lock_ttl(key)
        assert initial > 50_000
        time.sleep(1.5)
        restored = lease.lock_ttl(key)
        assert restored > 50_000
        assert restored + 400 > initial
        stranger = _spawn(spawn_context, procs, worker_stranger_renew, (lease.env, str(tenant), thread, stranger_queue))
        lost = _take(stranger_queue, 20)
        _join(stranger)
        assert lease.lock_exists(key)
        assert lease.lock_ttl(key) > 50_000
        print(f"MULTIPROCESS renew pid_a={holder.pid} pid_b={stranger.pid} initial_pttl={initial} restored_pttl={restored}")
        release.set()
        finished = _take(held_queue, 30)
        _join(holder)
    finally:
        _stop(procs)
        _drop(held_queue, stranger_queue)
    assert lost["lost"] is True
    assert lost["pid"] == stranger.pid
    assert finished["error_code"] is None
    assert not lease.lock_exists(key)


@pytest.mark.postgres
def test_renew_loss_does_not_start_the_next_superstep(execution, spawn_context):
    lease, _resources = execution
    tenant, conversation = uuid4(), uuid4()
    lease.track(tenant)
    thread = thread_id_for(tenant, conversation)
    key = lock_key(tenant, thread)
    entered, release = spawn_context.Event(), spawn_context.Event()
    result = spawn_context.Queue()
    procs: list = []
    try:
        holder = _spawn(spawn_context, procs, worker_renew_loss, (lease.env, str(tenant), str(conversation), entered, release, result))
        assert entered.wait(40)
        assert lease.lock_exists(key)
        lease.expire_lock(key, 300)
        _wait_until_absent(lease, key, 5)
        time.sleep(3.5)
        assert not lease.lock_exists(key)
        release.set()
        finished = _take(result, 30)
        _join(holder)
    finally:
        _stop(procs)
        _drop(result)
    print(f"MULTIPROCESS renew-loss pid_a={holder.pid} nodes={finished['nodes']} error={finished['error_code']}")
    assert finished["pid"] == holder.pid
    assert finished["calls"] == 1
    assert finished["error_code"] == "lease_lost"
    assert "format_response" not in finished["nodes"]
    assert set(finished["nodes"]) <= {"validate_input", "route_intent", "knowledge_qa"}
    assert lease.store is not None and lease.store.check_fence(tenant, thread, 1) == 1


@pytest.mark.postgres
def test_blocked_lease_loop_still_rejects_the_old_fence(execution, spawn_context):
    lease, resources = execution
    tenant, conversation = uuid4(), uuid4()
    lease.track(tenant)
    thread = thread_id_for(tenant, conversation)
    key = lock_key(tenant, thread)
    entered, release = spawn_context.Event(), spawn_context.Event()
    start, allocated, write_new = spawn_context.Event(), spawn_context.Event(), spawn_context.Event()
    old_queue, new_queue = spawn_context.Queue(), spawn_context.Queue()
    procs: list = []
    try:
        stalled = _spawn(spawn_context, procs, worker_jam_then_write, (lease.env, str(tenant), str(conversation), entered, release, old_queue))
        successor = _spawn(
            spawn_context,
            procs,
            worker_successor_writer,
            (lease.env, str(tenant), str(conversation), start, allocated, write_new, new_queue),
        )
        assert entered.wait(40)
        assert lease.lock_ttl(key) > 30_000
        lease.expire_lock(key, 300)
        _wait_until_absent(lease, key, 5)
        time.sleep(0.4)
        assert not lease.lock_exists(key)
        start.set()
        assert allocated.wait(30)
        assert lease.store is not None and lease.store.check_fence(tenant, thread, 2) == 2
        print(f"MULTIPROCESS loop-jam pid_a={stalled.pid} pid_b={successor.pid}")
        release.set()
        failed = _take(old_queue, 40)
        write_new.set()
        written = _take(new_queue, 30)
        _join(stalled)
        _join(successor)
    finally:
        _stop(procs)
        _drop(old_queue, new_queue)
    assert failed["error_code"] == "lease_lost"
    assert failed["calls"] == 1
    assert failed["pid"] == stalled.pid
    assert written["fence"] == 2
    assert written["pid"] == successor.pid
    assert _checkpoint_value(resources.pool, thread, "cp-successor-process", "answer") == "new"


@pytest.mark.postgres
def test_redis_outage_does_not_allocate_a_fence_or_enter_the_graph(execution, spawn_context):
    lease, _resources = execution
    tenant, conversation = uuid4(), uuid4()
    lease.track(tenant)
    thread = thread_id_for(tenant, conversation)
    result = spawn_context.Queue()
    procs: list = []
    try:
        failed = _spawn(spawn_context, procs, worker_redis_down, (lease.env, str(tenant), str(conversation), result))
        finished = _take(result, 90)
        _join(failed, 20)
    finally:
        _stop(procs)
        _drop(result)
    print(f"MULTIPROCESS redis-down pid_a={failed.pid} error={finished.get('error_code')}")
    secret = lease.env["REDIS_PASSWORD"]
    assert secret not in str(finished)
    assert finished["error_code"] == "lease_unavailable"
    assert finished["calls"] == 0
    assert finished["pid"] == failed.pid
    assert lease.store is not None
    with pytest.raises(FenceNotFound):
        lease.store.check_fence(tenant, thread, 1)


@pytest.mark.postgres
def test_read_history_uses_the_lock_without_advancing_the_fence(execution, spawn_context):
    lease, _resources = execution
    tenant, conversation = uuid4(), uuid4()
    lease.track(tenant)
    thread = thread_id_for(tenant, conversation)
    key = lock_key(tenant, thread)
    entered, release = spawn_context.Event(), spawn_context.Event()
    first_done, go_again = spawn_context.Event(), spawn_context.Event()
    writer_queue, reader_queue = spawn_context.Queue(), spawn_context.Queue()
    procs: list = []
    try:
        writer = _spawn(spawn_context, procs, worker_hold_graph, (lease.env, str(tenant), str(conversation), entered, release, writer_queue))
        assert entered.wait(40)
        assert lease.store is not None and lease.store.check_fence(tenant, thread, 1) == 1
        reader = _spawn(
            spawn_context,
            procs,
            worker_read_history,
            (lease.env, str(tenant), str(conversation), first_done, go_again, reader_queue),
        )
        assert first_done.wait(40)
        assert lease.lock_exists(key)
        assert lease.store.check_fence(tenant, thread, 1) == 1
        print(f"MULTIPROCESS history pid_a={writer.pid} pid_b={reader.pid}")
        release.set()
        written = _take(writer_queue, 40)
        _join(writer)
        assert not lease.lock_exists(key)
        go_again.set()
        history = _take(reader_queue, 30)
        _join(reader)
    finally:
        _stop(procs)
        _drop(writer_queue, reader_queue)
    assert written["error_code"] is None
    assert history["first_error"] == "thread_busy"
    assert history["second_error"] is None
    assert "进程甲的问题" in history["second_contents"]
    assert "进程甲" in history["second_contents"]
    assert history["pid"] == reader.pid
    assert lease.store.check_fence(tenant, thread, 1) == 1


@pytest.mark.postgres
def test_another_process_cannot_read_a_foreign_checkpoint(execution, spawn_context):
    lease, _resources = execution
    owner, reader_tenant, conversation = uuid4(), uuid4(), uuid4()
    lease.track(owner)
    lease.track(reader_tenant)
    thread = thread_id_for(owner, conversation)
    writer_queue, reader_queue = spawn_context.Queue(), spawn_context.Queue()
    procs: list = []
    try:
        writer = _spawn(
            spawn_context,
            procs,
            worker_write_checkpoint,
            (lease.env, str(owner), str(conversation), writer_queue),
        )
        written = _take(writer_queue, 40)
        _join(writer)
        reader = _spawn(
            spawn_context,
            procs,
            worker_read_checkpoint,
            (lease.env, str(reader_tenant), thread, reader_queue),
        )
        hidden = _take(reader_queue, 40)
        _join(reader)
    finally:
        _stop(procs)
        _drop(writer_queue, reader_queue)
    assert written["pid"] != hidden["pid"]
    assert written["pid"] != os.getpid() and hidden["pid"] != os.getpid()
    print(f"MULTIPROCESS rls pid_owner={written['pid']} pid_reader={hidden['pid']} parent={os.getpid()}")
    assert written["found"] is True
    assert written["answer"] == "tenant-a"
    assert written["created_checkpoint_pool"] is True
    assert hidden["found"] is False
    assert hidden["answer"] is None
    assert hidden["created_checkpoint_pool"] is True


def _spawn(ctx, procs: list, target, args):
    process = ctx.Process(target=target, args=args)
    process.start()
    procs.append(process)
    assert process.pid and process.pid != os.getpid()
    return process


def _take(queue, timeout: float) -> dict:
    try:
        item = queue.get(timeout=timeout)
    except Empty as exc:
        raise AssertionError("子进程没有返回结果") from exc
    if isinstance(item, dict) and "error" in item and "error_code" not in item and "outcome" not in item:
        raise AssertionError(f"子进程失败 pid={item.get('pid')} {item['error']}")
    return item


def _join(process, timeout: float = 15) -> None:
    process.join(timeout)
    if process.is_alive():
        process.kill()
        process.join(5)
        raise AssertionError(f"子进程没有退出 pid={process.pid}")


def _stop(procs: list) -> None:
    for process in procs:
        if process.is_alive():
            process.kill()
        process.join(5)


def _drop(*queues) -> None:
    for queue in queues:
        queue.close()
        queue.cancel_join_thread()


def _wait_until_absent(lease: ExecutionLease, key: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not lease.lock_exists(key):
            return
        time.sleep(0.25)
    raise AssertionError("Redis 锁没有在本次 Key 的 TTL 内消失")


def _counts(pool, thread_id: str) -> tuple[int, int, int]:
    _prefix, tenant, _conversation = thread_id.split(":")
    with pool.connection() as connection:
        with connection.transaction():
            connection.execute("select set_config('app.tenant_id', %s, true)", (tenant,))
            return tuple(
                _scalar(connection, f"select count(*) from agent.{table} where thread_id = %s", (thread_id,))
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
            )


def _checkpoint_value(pool, thread_id: str, checkpoint_id: str, channel: str):
    _prefix, tenant, _conversation = thread_id.split(":")
    with pool.connection() as connection:
        with connection.transaction():
            connection.execute("select set_config('app.tenant_id', %s, true)", (tenant,))
            row = connection.execute(
                "select checkpoint->'channel_values'->>%s as value from agent.checkpoints "
                "where thread_id = %s and checkpoint_ns = '' and checkpoint_id = %s",
                (channel, thread_id, checkpoint_id),
            ).fetchone()
    if row is None:
        return None
    return row["value"] if isinstance(row, dict) else row[0]


def _scalar(connection, sql: str, params: tuple) -> int:
    row = connection.execute(sql, params).fetchone()
    value = next(iter(row.values())) if isinstance(row, dict) else row[0]
    return int(value)


def _prepare_spawn() -> None:
    root = Path(__file__).resolve().parents[1]
    tests = Path(__file__).resolve().parent
    current = os.environ.get("PYTHONPATH", "")
    parts = [str(root), str(tests)]
    if current:
        parts.append(current)
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)


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
