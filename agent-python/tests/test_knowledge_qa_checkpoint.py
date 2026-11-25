from uuid import UUID, uuid4

from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.memory import MemorySaver
from psycopg import OperationalError

from app.agents.knowledge_qa.nodes import MAX_CONVERSATION_TURNS
from app.agents.knowledge_qa.service import HistoryMessage, KnowledgeQAService, _apply_citation_text, _load_citation_text
from lease_fakes import FakeThreadLock
from app.agents.knowledge_qa.threads import thread_id_for
from app.knowledge.rag import RAGAnswer, RAGSource

TENANT_A = UUID("00000000-0000-4000-8000-00000000e271")
TENANT_B = UUID("00000000-0000-4000-8000-00000000e272")
ACTOR_A = UUID("00000000-0000-4000-8000-00000000a271")
ACTOR_B = UUID("00000000-0000-4000-8000-00000000a272")
DOC = UUID("00000000-0000-4000-8000-0000000000d1")
CHUNK = UUID("00000000-0000-4000-8000-0000000000c1")


class ScriptedRAG:
    def __init__(self, results: list[RAGAnswer]) -> None:
        self.results = list(results)
        self.calls: list[tuple[object, object]] = []
        self.last_timing = {"answer": "shared-timing-must-not-be-the-result"}
        self._default_top_k = 5
        self._default_strategy = "vector"

    def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None) -> RAGAnswer:
        del top_k, strategy
        self.calls.append((tenant_id, question))
        self.last_timing = {"answer": "stale-shared-timing"}
        return self.results.pop(0)


def test_graph_compiles_with_a_checkpointer():
    rag = ScriptedRAG([_answer("第一轮")])
    saver = MemorySaver()
    service = KnowledgeQAService(rag, checkpointer=saver)

    assert service._graph.checkpointer is saver
    result = service.ask(TENANT_A, "第一题", conversation_id=uuid4())

    assert result.status == "answered"
    assert result.answer == "第一轮"
    assert result.answer != rag.last_timing["answer"]


def test_same_thread_restores_history_and_clears_previous_error_state():
    rag = ScriptedRAG(
        [
            _answer("第一轮", degraded=True, degradation_reason="rerank timeout"),
            _answer("第二轮"),
        ]
    )
    service = KnowledgeQAService(rag, checkpointer=MemorySaver())
    conversation = uuid4()

    first = service.ask(TENANT_A, "第一题", conversation_id=conversation)
    second = service.ask(TENANT_A, "第二题", conversation_id=conversation)

    assert first.degraded is True
    assert first.degradation_reason == "rerank timeout"
    assert second.answer == "第二轮"
    assert second.degraded is False
    assert second.degradation_reason is None
    assert second.error_code is None
    assert second.error_message is None
    assert [turn["question"] for turn in second.turns] == ["第一题", "第二题"]
    assert second.turns[1]["source_ids"] == [str(CHUNK)]
    assert "rag_result" not in second.turns[0]
    assert second.sources[0]["content"] == "第二轮证据"


def test_failed_turn_does_not_remain_on_the_next_answer():
    rag = ScriptedRAG([_answer("恢复后的回答")])
    service = KnowledgeQAService(rag, checkpointer=MemorySaver())
    conversation = uuid4()
    rag.ask = _raise_on_first(rag)  # type: ignore[method-assign]

    failed = service.ask(TENANT_A, "会失败的问题", conversation_id=conversation)
    recovered = service.ask(TENANT_A, "新的问题", conversation_id=conversation)

    assert failed.status == "failed"
    assert "sk-" not in str(failed.error_message)
    assert "password" not in str(failed.error_message).lower()
    assert recovered.status == "answered"
    assert recovered.answer == "恢复后的回答"
    assert recovered.error_message is None
    assert recovered.degraded is False
    assert recovered.turns[-1]["answer"] == "恢复后的回答"


def test_new_thread_and_other_tenant_do_not_see_history():
    rag = ScriptedRAG([_answer("租户甲"), _answer("新会话"), _answer("租户乙")])
    service = KnowledgeQAService(rag, checkpointer=MemorySaver())
    conversation = uuid4()

    first = service.ask(TENANT_A, "甲的问题", conversation_id=conversation)
    fresh = service.ask(TENANT_A, "另一会话", conversation_id=uuid4())
    other = service.ask(TENANT_B, "乙的问题", conversation_id=conversation)

    assert first.answer == "租户甲"
    assert [turn["question"] for turn in fresh.turns] == ["另一会话"]
    assert [turn["question"] for turn in other.turns] == ["乙的问题"]
    assert "甲的问题" not in [turn["question"] for turn in other.turns]


def test_other_actor_cannot_read_the_thread():
    rag = ScriptedRAG([_answer("仅甲可见")])
    service = KnowledgeQAService(rag, checkpointer=MemorySaver())
    conversation = uuid4()

    owned = service.ask(TENANT_A, "甲的问题", conversation_id=conversation, actor_id=ACTOR_A)
    denied = service.ask(TENANT_A, "乙来读取", conversation_id=conversation, actor_id=ACTOR_B)

    assert owned.answer == "仅甲可见"
    assert denied.status == "failed"
    assert denied.error_code == "thread_forbidden"
    assert denied.answer == ""
    assert denied.turns == ()
    assert rag.calls == [(TENANT_A, "甲的问题")]


def test_client_thread_metadata_is_not_accepted_as_access():
    rag = ScriptedRAG([_answer("不应执行")])
    service = KnowledgeQAService(rag, checkpointer=_ForeignCheckpoint(str(TENANT_B)))

    denied = service.ask(TENANT_A, "读取别人的线程", conversation_id=uuid4())

    assert denied.error_code == "thread_forbidden"
    assert denied.answer == ""
    assert rag.calls == []


def test_checkpoint_disconnect_does_not_expose_the_database_error():
    rag = ScriptedRAG([_answer("不应执行")])
    service = KnowledgeQAService(rag, checkpointer=_BrokenCheckpoint())

    result = service.ask(TENANT_A, "数据库断开", conversation_id=uuid4())

    assert result.status == "failed"
    assert result.error_code == "checkpoint_unavailable"
    assert result.error_message == "工作流状态暂时不可用"
    assert "sk-" not in result.error_message
    assert "super-secret-value" not in result.error_message
    assert rag.calls == []


def test_same_thread_rejects_an_overlapping_run():
    rag = ScriptedRAG([_answer("不会执行")])
    lock = FakeThreadLock()
    service = KnowledgeQAService(rag, checkpointer=MemorySaver(), thread_lock=lock)
    conversation = uuid4()
    thread_id = thread_id_for(TENANT_A, conversation)
    lock.hold(TENANT_A, thread_id)
    try:
        result = service.ask(TENANT_A, "并发问题", conversation_id=conversation)
    finally:
        lock.release_hold(TENANT_A, thread_id)
        service.close()

    assert result.error_code == "thread_busy"
    assert rag.calls == []
    assert lock.renews == 0


def test_conversation_history_drops_the_oldest_turn():
    rag = ScriptedRAG([_answer("一"), _answer("二"), _answer("三")])
    service = KnowledgeQAService(rag, checkpointer=MemorySaver())
    conversation = uuid4()
    import app.agents.knowledge_qa.nodes as nodes

    original = nodes.MAX_CONVERSATION_TURNS
    nodes.MAX_CONVERSATION_TURNS = 2
    try:
        service.ask(TENANT_A, "问题一", conversation_id=conversation)
        service.ask(TENANT_A, "问题二", conversation_id=conversation)
        third = service.ask(TENANT_A, "问题三", conversation_id=conversation)
    finally:
        nodes.MAX_CONVERSATION_TURNS = original

    assert [turn["question"] for turn in third.turns] == ["问题二", "问题三"]
    assert MAX_CONVERSATION_TURNS == 20


def test_read_history_returns_turns_in_order_without_internal_fields():
    rag = ScriptedRAG([_answer("第一轮"), _answer("第二轮")])
    service = KnowledgeQAService(rag, checkpointer=MemorySaver())
    conversation = uuid4()
    service.ask(TENANT_A, "第一题", conversation_id=conversation, actor_id=ACTOR_A)
    service.ask(TENANT_A, "第二题", conversation_id=conversation, actor_id=ACTOR_A)
    calls = len(rag.calls)

    history = service.read_history(TENANT_A, conversation, ACTOR_A)

    assert history.error_code is None
    assert [(item.role, item.content) for item in history.messages] == [
        ("user", "第一题"),
        ("assistant", "第一轮"),
        ("user", "第二题"),
        ("assistant", "第二轮"),
    ]
    assert history.messages[1].source_ids == (str(CHUNK),)
    assert history.messages[0].source_ids == ()
    rendered = repr(history)
    assert "rag_result" not in rendered
    assert "thread_id" not in rendered
    assert "blob" not in rendered
    assert [question for _tenant, question in rag.calls] == ["第一题", "第二题"]
    assert len(rag.calls) == calls


def test_history_citations_use_chunk_text_and_skip_unknown_ids():
    chunk_id = "7340955c-53a6-40df-8b89-e40fc6b4035c"
    message = HistoryMessage("assistant", "部门负责人审批。", "answered", (chunk_id, "missing-id"))

    enriched = _apply_citation_text([message], {chunk_id: "付款申请须先由部门负责人审批。"})

    assert [(item.chunk_id, item.content) for item in enriched[0].sources] == [
        (chunk_id, "付款申请须先由部门负责人审批。"),
    ]
    assert "missing-id" not in [item.chunk_id for item in enriched[0].sources]


def test_citation_lookup_is_limited_to_the_tenant(monkeypatch):
    seen: dict[str, object] = {}

    class Connection:
        def execute(self, statement, params):
            seen["statement"] = statement
            seen["params"] = params
            return self

        def fetchall(self):
            return [("7340955c-53a6-40df-8b89-e40fc6b4035c", "付款申请须先由部门负责人审批。")]

    from contextlib import contextmanager

    @contextmanager
    def fake_transaction(pool, tenant_id):
        seen["tenant"] = tenant_id
        seen["pool"] = pool
        yield Connection()

    monkeypatch.setattr("app.persistence.postgres.tenant_transaction", fake_transaction)
    rag = type("Rag", (), {"_retriever": type("Retriever", (), {"_pool": object()})()})()

    found = _load_citation_text(rag, TENANT_A, ["7340955c-53a6-40df-8b89-e40fc6b4035c"])

    assert found == {"7340955c-53a6-40df-8b89-e40fc6b4035c": "付款申请须先由部门负责人审批。"}
    assert seen["tenant"] == str(TENANT_A)
    assert "tenant_id = %s" in seen["statement"]
    assert seen["params"][0] == TENANT_A


def test_read_history_of_a_new_thread_is_empty_and_does_not_call_rag():
    rag = ScriptedRAG([_answer("不应执行")])
    service = KnowledgeQAService(rag, checkpointer=MemorySaver())

    history = service.read_history(TENANT_A, uuid4(), ACTOR_A)

    assert history.messages == ()
    assert history.error_code is None
    assert rag.calls == []


def test_other_actor_and_tenant_cannot_read_history_or_run_rag():
    rag = ScriptedRAG([_answer("仅甲可见"), _answer("不应执行"), _answer("不应执行")])
    service = KnowledgeQAService(rag, checkpointer=MemorySaver())
    conversation = uuid4()
    service.ask(TENANT_A, "甲的问题", conversation_id=conversation, actor_id=ACTOR_A)

    other_actor = service.read_history(TENANT_A, conversation, ACTOR_B)
    other_tenant = service.read_history(TENANT_B, conversation, ACTOR_A)

    assert other_actor.error_code == "thread_forbidden"
    assert other_actor.messages == ()
    assert other_tenant.messages == ()
    assert other_tenant.error_code is None
    assert rag.calls == [(TENANT_A, "甲的问题")]


def test_read_history_does_not_bypass_a_busy_thread():
    rag = ScriptedRAG([_answer("不应执行")])
    lock = FakeThreadLock()
    service = KnowledgeQAService(rag, checkpointer=MemorySaver(), thread_lock=lock)
    conversation = uuid4()
    thread_id = thread_id_for(TENANT_A, conversation)
    lock.hold(TENANT_A, thread_id)
    try:
        history = service.read_history(TENANT_A, conversation, ACTOR_A)
    finally:
        lock.release_hold(TENANT_A, thread_id)
        service.close()

    assert history.error_code == "thread_busy"
    assert history.messages == ()
    assert rag.calls == []


def test_malformed_checkpoint_history_is_a_safe_error():
    rag = ScriptedRAG([_answer("不应执行")])
    service = KnowledgeQAService(rag, checkpointer=_MalformedTurns())

    history = service.read_history(TENANT_A, uuid4(), ACTOR_A)

    assert history.error_code == "checkpoint_unavailable"
    assert history.error_message == "工作流状态暂时不可用"
    assert "rag_result" not in (history.error_message or "")
    assert "secret-blob" not in (history.error_message or "")
    assert rag.calls == []


def test_broken_checkpoint_history_does_not_expose_the_database_error():
    rag = ScriptedRAG([_answer("不应执行")])
    service = KnowledgeQAService(rag, checkpointer=_BrokenCheckpoint())

    history = service.read_history(TENANT_A, uuid4(), ACTOR_A)

    assert history.error_code == "checkpoint_unavailable"
    assert history.error_message == "工作流状态暂时不可用"
    assert "super-secret-value" not in history.error_message
    assert rag.calls == []


def test_missing_conversation_does_not_call_rag():
    rag = ScriptedRAG([_answer("不应执行")])
    service = KnowledgeQAService(rag, checkpointer=MemorySaver())

    missing = service.ask(TENANT_A, "付款由谁审批？")
    bad_actor = service.ask(TENANT_A, "付款由谁审批？", conversation_id=uuid4(), actor_id="user-1")

    assert missing.status == "invalid_input"
    assert missing.error_message == "conversation_id 必须是 UUID"
    assert bad_actor.error_message == "actor_id 必须是 UUID"
    assert rag.calls == []


class _ForeignCheckpoint(MemorySaver):
    def __init__(self, tenant_id: str) -> None:
        super().__init__()
        self.tenant_id = tenant_id

    def get_tuple(self, config):
        return CheckpointTuple(
            config=config,
            checkpoint={"channel_values": {"tenant_id": self.tenant_id, "answer": "别人的回答"}},
            metadata={"tenant_id": self.tenant_id, "actor_id": ""},
        )


class _MalformedTurns(MemorySaver):
    def get_tuple(self, config):
        return CheckpointTuple(
            config=config,
            checkpoint={
                "channel_values": {
                    "tenant_id": str(TENANT_A),
                    "turns": [{"question": "问题", "rag_result": {"blob": "secret-blob"}}],
                }
            },
            metadata={"tenant_id": str(TENANT_A), "actor_id": str(ACTOR_A)},
        )


class _BrokenCheckpoint(MemorySaver):
    def get_tuple(self, config):
        del config
        raise OperationalError("password=super-secret-value sk-live")


def _raise_on_first(rag: ScriptedRAG):
    original = ScriptedRAG.ask
    failed = False

    def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None) -> RAGAnswer:
        nonlocal failed
        if not failed:
            failed = True
            self.calls.append((tenant_id, question))
            raise RuntimeError("postgres://agent_app:password@127.0.0.1/agent sk-live-secret")
        return original(self, tenant_id, question, top_k=top_k, strategy=strategy)

    return ask.__get__(rag, ScriptedRAG)


def _answer(text: str, *, degraded: bool = False, degradation_reason: str | None = None) -> RAGAnswer:
    source = RAGSource(DOC, CHUNK, 0, f"{text}证据", 0.9, "Qwen/Qwen3-Embedding-0.6B", 0, 4)
    return RAGAnswer(
        answer=text,
        sources=(source,),
        retrieval_count=1,
        model="fake",
        degraded=degraded,
        degradation_reason=degradation_reason,
    )
