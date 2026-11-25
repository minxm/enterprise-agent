"""Real PostgreSQL proof for recall isolation and checkpoint exclusion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import Connection, OperationalError
from psycopg_pool import ConnectionPool

from app.agents.knowledge_qa.service import KnowledgeQAService
from app.agents.knowledge_qa.threads import thread_id_for
from app.core.config import Settings
from app.knowledge.rag import RAGAnswer, RAGSource
from app.memory.context import MemoryAccessContext
from app.memory.service import MemoryService
from app.persistence.checkpoint import CheckpointResources
from app.persistence.migrate import apply_migrations
from app.persistence.postgres import build_conninfo, memory_transaction
from qa_execution import ExecutionLease, ExecutionUnavailable

SENTINEL = "MEMORY_SENTINEL_DAY40_7f29"
SENTINEL_DAY41 = "MEMORY_SENTINEL_DAY41_91ab"
DOC = UUID("00000000-0000-4000-8000-0000000000d5")
CHUNK = UUID("00000000-0000-4000-8000-0000000000c5")


class _CountingMemory:
    def __init__(self, inner: MemoryService) -> None:
        self.inner = inner
        self.calls = 0

    def recall(self, ctx: MemoryAccessContext):
        self.calls += 1
        return self.inner.recall(ctx)


class RecordingRAG:
    def __init__(self) -> None:
        self.memories: list[tuple[str, ...]] = []
        self._default_top_k = 5
        self._default_strategy = "vector"

    def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None, memories: tuple[str, ...] = ()):
        del tenant_id, question, top_k, strategy
        self.memories.append(tuple(memories))
        source = RAGSource(DOC, CHUNK, 0, "部门负责人审批。", 0.9, "fake", 0, 4)
        return RAGAnswer("部门负责人审批。", (source,), 1, "fake", retrieval_strategy="vector", effective_strategy="vector")


@pytest.fixture(scope="module")
def recall_db():
    env = _root_env()
    if not env.get("AGENT_APP_PASSWORD") or not env.get("AGENT_MIGRATOR_PASSWORD") or not env.get("POSTGRES_DB"):
        pytest.skip("仓库根目录 .env 未提供数据库密码，跳过真实数据库测试")
    settings = _settings(env)
    try:
        apply_migrations(settings)
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL 不可用，跳过真实数据库测试: {exc.__class__.__name__}")
    pool = _pool(settings)
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
    try:
        resources.open()
    except RuntimeError as exc:
        pool.close()
        pytest.skip(f"PostgreSQL 不可用，跳过真实数据库测试: {exc.__class__.__name__}")
    try:
        yield type("RecallDatabase", (), {"pool": pool, "env": env, "settings": settings, "resources": resources})()
    finally:
        resources.close()
        pool.close()


@pytest.fixture(scope="module")
def execution(recall_db):
    try:
        lease = ExecutionLease.open(recall_db.env)
    except ExecutionUnavailable as exc:
        pytest.skip(f"Redis 不可用，跳过执行链路测试: {exc.__class__.__name__}")
    try:
        yield lease
    finally:
        lease.close(recall_db.resources.pool)


def test_recall_respects_scope_lifecycle_and_does_not_write(recall_db):
    service = MemoryService(recall_db.pool)
    tenant_a, tenant_b = uuid4(), uuid4()
    owner, other = uuid4(), uuid4()
    owner_ctx = _context(tenant_a, owner)
    try:
        active = service.create(owner_ctx, scope="user", memory_type="preference", content="以后优先使用表格")
        archived = service.create(owner_ctx, scope="user", memory_type="format", content="归档后不要召回")
        service.archive(owner_ctx, memory_id=archived.id, expected_version=1)
        expired = service.create(
            owner_ctx,
            scope="user",
            memory_type="work_scope",
            content="过期后不要召回",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        removed = service.create(owner_ctx, scope="user", memory_type="preference", content="删除后不要召回")
        service.delete(owner_ctx, memory_id=removed.id, expected_version=1)
        shared = service.create(
            _context(tenant_a, owner, allow_tenant_write=True),
            scope="tenant",
            memory_type="organization_practice",
            content="组织先给结论",
        )
        before = _row(recall_db, owner_ctx, active.id)
        audit_before = _audit_count(recall_db, owner_ctx, active.id)

        visible = service.recall(owner_ctx)
        hidden_user = service.recall(_context(tenant_a, other))
        other_tenant = service.recall(_context(tenant_b, owner, allow_tenant_read=True))
        write_only = service.recall(_context(tenant_a, owner, allow_tenant_read=False, allow_tenant_write=True))
        with_read = service.recall(_context(tenant_a, owner, allow_tenant_read=True))
        after = _row(recall_db, owner_ctx, active.id)

        assert [item.content for item in visible] == ["以后优先使用表格"]
        assert hidden_user == ()
        assert other_tenant == ()
        assert [item.content for item in write_only] == ["以后优先使用表格"]
        assert [item.content for item in with_read] == ["以后优先使用表格", "组织先给结论"]
        assert after == before
        assert _audit_count(recall_db, owner_ctx, active.id) == audit_before
        assert expired.id not in [item.id for item in visible]
        assert shared.id in [item.id for item in with_read]
        _assert_pool_guc_cleared(recall_db.pool)
    finally:
        _purge(recall_db, tenant_a)
        _purge(recall_db, tenant_b)


def test_checkpoint_does_not_store_recalled_memory_and_the_next_run_recalls_again(recall_db, execution):
    tenant_id, actor_id = uuid4(), uuid4()
    execution.track(tenant_id)
    memory = _CountingMemory(MemoryService(recall_db.pool))
    context = _context(tenant_id, actor_id)
    rag = RecordingRAG()
    service = execution.service(rag, recall_db.resources.saver, memory=memory)
    conversation = uuid4()
    thread_id = thread_id_for(tenant_id, conversation)
    try:
        created = memory.inner.create(context, scope="user", memory_type="preference", content=SENTINEL)
        first = service.ask(tenant_id, "付款由谁审批？", conversation_id=conversation, actor_id=actor_id)
        stored = _checkpoint_text(recall_db.resources, thread_id)
        history = service.read_history(tenant_id, conversation, actor_id)
        assert memory.calls == 1
        assert rag.memories == [(SENTINEL,)]
        assert "付款由谁审批" in stored
        assert SENTINEL not in stored
        assert SENTINEL not in first.answer
        assert all(SENTINEL not in str(turn) for turn in first.turns)
        assert history.error_code is None
        assert history.messages
        assert all(SENTINEL not in message.content for message in history.messages)

        memory.inner.delete(context, memory_id=created.id, expected_version=created.version)
        second = service.ask(tenant_id, "下一题", conversation_id=conversation, actor_id=actor_id)
        assert memory.calls == 2
        assert rag.memories[-1] == ()
        assert SENTINEL not in second.answer
        resumed = _checkpoint_text(recall_db.resources, thread_id)
        assert "下一题" in resumed
        assert SENTINEL not in resumed
        assert _audit_actions(recall_db, context, created.id) == ["create", "delete"]
    finally:
        _purge(recall_db, tenant_id)
        _delete_thread(recall_db.resources, thread_id)


def test_day41_sentinel_stays_out_of_checkpoint_history_question_and_logs(recall_db, caplog, execution):
    tenant_id, actor_id = uuid4(), uuid4()
    execution.track(tenant_id)
    memory = MemoryService(recall_db.pool)
    context = _context(tenant_id, actor_id)
    rag = RecordingRAG()
    service = execution.service(rag, recall_db.resources.saver, memory=memory)
    conversation = uuid4()
    thread_id = thread_id_for(tenant_id, conversation)
    try:
        created = memory.create(context, scope="user", memory_type="preference", content=SENTINEL_DAY41)
        with caplog.at_level("DEBUG"):
            first = service.ask(tenant_id, "付款由谁审批？", conversation_id=conversation, actor_id=actor_id)
        stored = _checkpoint_text(recall_db.resources, thread_id, needle=SENTINEL_DAY41)
        history = service.read_history(tenant_id, conversation, actor_id)
        assert SENTINEL_DAY41 not in first.answer
        assert rag.memories == [(SENTINEL_DAY41,)]
        assert SENTINEL_DAY41 not in stored
        assert "付款由谁审批" in stored
        assert history.messages[0].role == "user"
        assert SENTINEL_DAY41 not in history.messages[0].content
        assert SENTINEL_DAY41 not in caplog.text
        assert "memory recall ready" in caplog.text
        memory.delete(context, memory_id=created.id, expected_version=created.version)
        second = service.ask(tenant_id, "下一题", conversation_id=conversation, actor_id=actor_id)
        assert rag.memories[-1] == ()
        assert SENTINEL_DAY41 not in second.answer
        assert SENTINEL_DAY41 not in _checkpoint_text(recall_db.resources, thread_id, needle=SENTINEL_DAY41)
    finally:
        _purge(recall_db, tenant_id)
        _delete_thread(recall_db.resources, thread_id)


def _checkpoint_text(resources: CheckpointResources, thread_id: str, needle: str = SENTINEL) -> str:
    needle_bytes = needle.encode()
    with resources.pool.connection() as connection:
        with connection.transaction():
            connection.execute("select set_config('app.tenant_id', %s, true)", (_tenant_of(thread_id),))
            rows = connection.execute(
                """
                select checkpoint::text as checkpoint, metadata::text as metadata
                from agent.checkpoints
                where thread_id = %s
                """,
                (thread_id,),
            ).fetchall()
            blobs = connection.execute(
                "select blob from agent.checkpoint_blobs where thread_id = %s",
                (thread_id,),
            ).fetchall()
            writes = connection.execute(
                "select blob from agent.checkpoint_writes where thread_id = %s",
                (thread_id,),
            ).fetchall()
    parts = [str(row["checkpoint"]) + str(row["metadata"]) for row in rows]
    for row in list(blobs) + list(writes):
        payload = row["blob"]
        if isinstance(payload, memoryview):
            payload = payload.tobytes()
        if isinstance(payload, bytes) and needle_bytes in payload:
            parts.append(needle)
        elif isinstance(payload, bytes):
            parts.append(payload.decode("utf-8", errors="ignore"))
    return "\n".join(parts)


def _delete_thread(resources: CheckpointResources, thread_id: str) -> None:
    with resources.pool.connection() as connection:
        with connection.transaction():
            connection.execute("select set_config('app.tenant_id', %s, true)", (_tenant_of(thread_id),))
            connection.execute("delete from agent.checkpoint_writes where thread_id = %s", (thread_id,))
            connection.execute("delete from agent.checkpoint_blobs where thread_id = %s", (thread_id,))
            connection.execute("delete from agent.checkpoints where thread_id = %s", (thread_id,))


def _tenant_of(thread_id: str) -> str:
    """Test observation only: the thread was created by this test."""
    _prefix, tenant, _conversation = thread_id.split(":")
    return tenant


def _row(recall_db, context: MemoryAccessContext, memory_id: UUID):
    with memory_transaction(
        recall_db.pool,
        tenant_id=str(context.tenant_id),
        actor_id=str(context.actor_id),
        allow_tenant_read=False,
        allow_tenant_write=False,
    ) as connection:
        return connection.execute(
            """
            select content, status, version, updated_at, content_sha256
            from agent.memory_item
            where id = %s
            """,
            (memory_id,),
        ).fetchone()


def _audit_count(recall_db, context: MemoryAccessContext, memory_id: UUID) -> int:
    return len(_audit_actions(recall_db, context, memory_id))


def _audit_actions(recall_db, context: MemoryAccessContext, memory_id: UUID) -> list[str]:
    with memory_transaction(
        recall_db.pool,
        tenant_id=str(context.tenant_id),
        actor_id=str(context.actor_id),
        allow_tenant_read=context.allow_tenant_read,
        allow_tenant_write=context.allow_tenant_write,
    ) as connection:
        rows = connection.execute(
            "select action from agent.memory_audit where memory_id = %s order by created_at, action",
            (memory_id,),
        ).fetchall()
    return [row[0] for row in rows]


def _assert_pool_guc_cleared(pool: ConnectionPool) -> None:
    with pool.connection() as connection:
        row = connection.execute(
            """
            select
                current_setting('app.tenant_id', true),
                current_setting('app.actor_id', true),
                current_setting('app.tenant_memory_read', true),
                current_setting('app.tenant_memory_write', true)
            """
        ).fetchone()
    assert row == ("", "", "", "")


def _context(tenant_id: UUID, actor_id: UUID, *, allow_tenant_read: bool = False, allow_tenant_write: bool = False):
    return MemoryAccessContext.for_trusted_caller(
        tenant_id=tenant_id,
        actor_id=actor_id,
        allow_tenant_read=allow_tenant_read,
        allow_tenant_write=allow_tenant_write,
        request_id="day40-recall",
    )


def _purge(recall_db, tenant_id: UUID) -> None:
    with _migrator(recall_db.env) as connection:
        connection.execute("delete from agent.memory_audit where tenant_id = %s", (tenant_id,))
        connection.execute("delete from agent.memory_item where tenant_id = %s", (tenant_id,))


def _pool(settings: Settings) -> ConnectionPool:
    return ConnectionPool(
        build_conninfo(
            settings.postgres_host,
            settings.postgres_port,
            settings.postgres_db,
            settings.agent_app_user,
            settings.agent_app_password,
        ),
        min_size=1,
        max_size=1,
        timeout=10,
        kwargs={"options": "-c search_path=agent"},
        open=True,
    )


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
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _migrator(env: dict[str, str]) -> Connection:
    settings = _settings(env)
    return Connection.connect(
        build_conninfo(
            settings.postgres_host,
            settings.postgres_port,
            settings.postgres_db,
            settings.agent_migrator_user,
            settings.agent_migrator_password,
        ),
        options="-c search_path=agent",
        autocommit=True,
    )
