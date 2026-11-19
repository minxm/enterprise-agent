"""PostgreSQL persistence for Knowledge QA checkpoints.

Uses a dedicated tenant prefix and deletes only those threads. It does not read
or modify the evaluation corpus.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import OperationalError

from app.agents.knowledge_qa.service import KnowledgeQAService
from app.agents.knowledge_qa.threads import thread_id_for
from app.core.config import Settings
from app.knowledge.rag import RAGAnswer, RAGSource
from app.persistence.checkpoint import CheckpointResources
from app.persistence.migrate import apply_migrations
from app.persistence.postgres import build_conninfo

TENANT_A = UUID("00000000-0000-4000-8000-00000000e273")
TENANT_B = UUID("00000000-0000-4000-8000-00000000e274")
ACTOR_A = UUID("00000000-0000-4000-8000-00000000a273")
ACTOR_B = UUID("00000000-0000-4000-8000-00000000a274")
DOC = UUID("00000000-0000-4000-8000-0000000000d2")
CHUNK = UUID("00000000-0000-4000-8000-0000000000c2")


class ScriptedRAG:
    def __init__(self, results: list[RAGAnswer]) -> None:
        self.results = list(results)
        self.calls: list[str] = []
        self._default_top_k = 5
        self._default_strategy = "vector"

    def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None) -> RAGAnswer:
        del tenant_id, top_k, strategy
        self.calls.append(str(question))
        return self.results.pop(0)


@pytest.fixture(scope="module")
def checkpoint_db():
    env = _root_env()
    if not env.get("AGENT_APP_PASSWORD") or not env.get("AGENT_MIGRATOR_PASSWORD") or not env.get("POSTGRES_DB"):
        pytest.skip("仓库根目录 .env 未提供数据库密码，跳过真实数据库测试")
    settings = _settings(env)
    try:
        apply_migrations(settings)
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL 不可用，跳过真实数据库测试: {exc.__class__.__name__}")
    except RuntimeError as exc:
        if "连接" in str(exc) or "PostgreSQL" in str(exc):
            pytest.skip(f"PostgreSQL 不可用，跳过真实数据库测试: {exc.__class__.__name__}")
        raise
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
        pytest.skip(f"PostgreSQL 不可用，跳过真实数据库测试: {exc.__class__.__name__}")
    try:
        yield resources, env["AGENT_APP_PASSWORD"]
    finally:
        _delete_threads(resources, TENANT_A, TENANT_B)
        resources.close()


@pytest.mark.postgres
def test_checkpoint_tables_are_in_the_agent_schema_and_runtime_has_no_create(checkpoint_db):
    resources, _password = checkpoint_db
    with resources.pool.connection() as connection:
        rows = connection.execute(
            """
            select n.nspname, r.rolname
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            join pg_roles r on r.oid = c.relowner
            where c.relname = 'checkpoints'
            """
        ).fetchall()
        privilege = connection.execute(
            """
            select has_schema_privilege('agent_app', 'agent', 'CREATE') as schema_create,
                   has_table_privilege('agent_app', 'agent.checkpoints', 'INSERT') as checkpoint_insert,
                   has_table_privilege('agent_app', 'agent.checkpoint_migrations', 'INSERT') as migration_insert
            """
        ).fetchone()
    assert rows == [{"nspname": "agent", "rolname": "agent_migrator"}]
    assert privilege == {"schema_create": False, "checkpoint_insert": True, "migration_insert": False}


@pytest.mark.postgres
def test_postgres_thread_persists_across_a_new_graph(checkpoint_db):
    resources, password = checkpoint_db
    rag = ScriptedRAG([_answer("第一轮", degraded=True), _answer("第二轮"), _answer("重建后")])
    first_service = KnowledgeQAService(rag, checkpointer=resources.saver)
    conversation = uuid4()

    first = first_service.ask(TENANT_A, "第一题", conversation_id=conversation)
    second = first_service.ask(TENANT_A, "第二题", conversation_id=conversation)
    assert _checkpoint_count(resources, thread_id_for(TENANT_A, conversation)) >= 1

    rebuilt = CheckpointResources(resources.conninfo, resources.secret)
    rebuilt.open()
    try:
        assert rebuilt.saver is not None
        resumed = KnowledgeQAService(rag, checkpointer=rebuilt.saver)
        third = resumed.ask(TENANT_A, "第三题", conversation_id=conversation)
    finally:
        rebuilt.close()

    assert first.degraded is True
    assert second.answer == "第二轮"
    assert second.degraded is False
    assert second.error_message is None
    assert third.answer == "重建后"
    assert [turn["question"] for turn in third.turns] == ["第一题", "第二题", "第三题"]
    stored = _stored_text(resources, thread_id_for(TENANT_A, conversation))
    if "sk-" in stored:
        raise AssertionError("checkpoint contains an API key prefix")
    if password and password in stored:
        raise AssertionError("checkpoint contains the database password")


@pytest.mark.postgres
def test_postgres_threads_are_isolated_by_tenant_and_actor(checkpoint_db):
    resources, _password = checkpoint_db
    rag = ScriptedRAG([_answer("甲"), _answer("乙"), _answer("不应执行")])
    service = KnowledgeQAService(rag, checkpointer=resources.saver)
    conversation = uuid4()

    owned = service.ask(TENANT_A, "甲的问题", conversation_id=conversation, actor_id=ACTOR_A)
    other_tenant = service.ask(TENANT_B, "乙的问题", conversation_id=conversation, actor_id=ACTOR_A)
    denied = service.ask(TENANT_A, "乙来读取", conversation_id=conversation, actor_id=ACTOR_B)
    fresh = service.ask(TENANT_A, "另一会话", conversation_id=uuid4(), actor_id=ACTOR_A)

    assert owned.answer == "甲"
    assert [turn["question"] for turn in other_tenant.turns] == ["乙的问题"]
    assert denied.error_code == "thread_forbidden"
    assert denied.answer == ""
    assert [turn["question"] for turn in fresh.turns] == ["另一会话"]
    assert "乙来读取" not in rag.calls


@pytest.mark.postgres
def test_postgres_read_history_returns_turns_without_writing(checkpoint_db):
    resources, _password = checkpoint_db
    rag = ScriptedRAG([_answer("第一轮"), _answer("第二轮")])
    service = KnowledgeQAService(rag, checkpointer=resources.saver)
    conversation = uuid4()
    thread_id = thread_id_for(TENANT_A, conversation)
    service.ask(TENANT_A, "第一题", conversation_id=conversation, actor_id=ACTOR_A)
    service.ask(TENANT_A, "第二题", conversation_id=conversation, actor_id=ACTOR_A)
    before = _checkpoint_count(resources, thread_id)

    history = service.read_history(TENANT_A, conversation, ACTOR_A)
    denied = service.read_history(TENANT_A, conversation, ACTOR_B)
    empty = service.read_history(TENANT_A, uuid4(), ACTOR_A)

    assert [(item.role, item.content) for item in history.messages] == [
        ("user", "第一题"),
        ("assistant", "第一轮"),
        ("user", "第二题"),
        ("assistant", "第二轮"),
    ]
    assert history.messages[1].source_ids == (str(CHUNK),)
    assert "rag_result" not in repr(history)
    assert denied.error_code == "thread_forbidden"
    assert denied.messages == ()
    assert empty.messages == ()
    assert _checkpoint_count(resources, thread_id) == before
    assert rag.calls == ["第一题", "第二题"]


@pytest.mark.postgres
def test_closed_checkpoint_pool_returns_a_safe_error(checkpoint_db):
    resources, password = checkpoint_db
    standalone = CheckpointResources(resources.conninfo, resources.secret)
    standalone.open()
    rag = ScriptedRAG([_answer("第一轮"), _answer("断开后")])
    service = KnowledgeQAService(rag, checkpointer=standalone.saver)
    conversation = uuid4()
    service.ask(TENANT_A, "第一题", conversation_id=conversation)
    standalone.close()

    result = service.ask(TENANT_A, "第二题", conversation_id=conversation)

    assert result.status == "failed"
    assert result.error_code == "checkpoint_unavailable"
    leaked = bool(password) and password in (result.error_message or "")
    if leaked:
        raise AssertionError("checkpoint error exposed the database password")
    assert rag.calls == ["第一题"]


def _checkpoint_count(resources: CheckpointResources, thread_id: str) -> int:
    with resources.pool.connection() as connection:
        row = connection.execute(
            "select count(*) as count from agent.checkpoints where thread_id = %s",
            (thread_id,),
        ).fetchone()
    return int(row["count"])


def _stored_text(resources: CheckpointResources, thread_id: str) -> str:
    with resources.pool.connection() as connection:
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
    parts = [row["checkpoint"] + row["metadata"] for row in rows]
    for blob in blobs:
        payload = blob["blob"]
        if isinstance(payload, memoryview):
            payload = payload.tobytes()
        if isinstance(payload, bytes):
            parts.append(payload.decode("utf-8", errors="ignore"))
    return "\n".join(parts)


def _delete_threads(resources: CheckpointResources, *tenants: UUID) -> None:
    if resources.pool is None:
        return
    for tenant in tenants:
        prefix = f"kq:{tenant}:%"
        with resources.pool.connection() as connection:
            connection.execute("delete from agent.checkpoint_writes where thread_id like %s", (prefix,))
            connection.execute("delete from agent.checkpoint_blobs where thread_id like %s", (prefix,))
            connection.execute("delete from agent.checkpoints where thread_id like %s", (prefix,))


def _answer(text: str, *, degraded: bool = False) -> RAGAnswer:
    source = RAGSource(DOC, CHUNK, 0, f"{text}证据", 0.9, "fake-embedding", 0, 4)
    return RAGAnswer(
        answer=text,
        sources=(source,),
        retrieval_count=1,
        model="fake",
        degraded=degraded,
        degradation_reason="rerank timeout" if degraded else None,
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
