from uuid import UUID

from langgraph.checkpoint.memory import MemorySaver

from app.agents.knowledge_qa.contract_intent import extract_contract_numbers, looks_like_contract_query
from app.agents.knowledge_qa.service import KnowledgeQAService
from app.knowledge.prompts import INSUFFICIENT_KNOWLEDGE
from app.knowledge.rag import RAGAnswer, RAGSource
from app.tools.contract_query import ContractToolResult

TENANT = UUID("00000000-0000-4000-8000-00000000e033")
ACTOR = UUID("00000000-0000-4000-8000-00000000a033")
CONVERSATION = UUID("00000000-0000-4000-8000-00000000c033")
DOC = UUID("00000000-0000-4000-8000-0000000000d1")
CHUNK = UUID("00000000-0000-4000-8000-0000000000a1")
TICKET = "actor-ticket-value-0123456789abcdef"
QUESTION = "查询合同编号 HT-2026-001 的基本信息、当前版本和审批状态。"
ANSWER = "\n".join([
    "合同编号：HT-2026-001",
    "标题：年度采购合同",
    "类型：采购",
    "相对方：南方公司",
    "金额：1.00 CNY",
    "签订日期：无",
    "生效日期：无",
    "到期日期：无",
    "审批状态：待审核（pending_review）",
    "说明：无",
    "当前版本：V1",
    "版本序号：1",
    "版本说明：首版",
    "版本文件：采购合同.pdf",
])


class FakeRAG:
    def __init__(self, result: RAGAnswer | None = None, error: Exception | None = None) -> None:
        self.result = result or RAGAnswer(
            answer="模型不该被拿来编造合同状态",
            sources=(),
            retrieval_count=0,
            model="Qwen/Qwen2.5-7B-Instruct",
            retrieval_strategy="vector",
            effective_strategy="vector",
            degraded=False,
            degradation_reason=None,
        )
        self.error = error
        self.calls: list[tuple[object, object]] = []
        self._default_top_k = 5
        self._default_strategy = "vector"

    def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None) -> RAGAnswer:
        self.calls.append((tenant_id, question))
        if self.error is not None:
            raise self.error
        return self.result


class FakeTool:
    def __init__(self, result: ContractToolResult | Exception) -> None:
        self.result = result
        self.calls: list[str] = []

    def query(self, contract_no: str) -> ContractToolResult:
        self.calls.append(contract_no)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_contract_number_extraction_and_intent_boundaries():
    assert extract_contract_numbers(QUESTION) == ["HT-2026-001"]
    assert extract_contract_numbers("查询合同编号 HT-2026-001 和 CT-001 的状态") == ["HT-2026-001", "CT-001"]
    assert extract_contract_numbers("付款由谁审批？") == []
    assert looks_like_contract_query(QUESTION) is True
    assert looks_like_contract_query("查询合同的基本信息和审批状态") is True
    assert looks_like_contract_query("请审批这笔合同并安排付款") is False
    assert looks_like_contract_query("付款由谁审批？") is False
    assert looks_like_contract_query("查看合同制度里的付款规则") is False


def test_contract_query_routes_and_keeps_only_tool_facts():
    rag = FakeRAG()
    tool = FakeTool(ContractToolResult("answered", ANSWER))
    result = KnowledgeQAService(rag, contract_tool=tool).ask(TENANT, QUESTION, actor_ticket=TICKET)

    assert tool.calls == ["HT-2026-001"]
    assert rag.calls == []
    assert result.status == "answered"
    assert result.answer == ANSWER
    assert "已审核" not in result.answer
    assert "已通过" not in result.answer
    assert "模型不该被拿来编造" not in result.answer
    assert TICKET not in result.answer


def test_missing_or_multiple_numbers_do_not_call_the_tool():
    rag = FakeRAG()
    tool = FakeTool(ContractToolResult("answered", ANSWER))
    service = KnowledgeQAService(rag, contract_tool=tool)

    missing = service.ask(TENANT, "查询合同的基本信息和当前版本")
    many = service.ask(TENANT, "查询合同编号 HT-2026-001 和 CT-001 的审批状态")

    assert tool.calls == []
    assert rag.calls == []
    assert missing.status == "answered"
    assert "请提供要查询的合同编号" in missing.answer
    assert "HT-2026-001" in missing.answer
    assert many.answer.startswith("问题里有多个合同编号")


def test_tool_failure_exits_safely_and_does_not_invent_a_contract():
    rag = FakeRAG()
    tool = FakeTool(ContractToolResult("failed", "", "contract_unavailable"))
    result = KnowledgeQAService(rag, contract_tool=tool).ask(TENANT, QUESTION)

    assert result.status == "failed"
    assert result.error_code == "contract_unavailable"
    assert result.error_message == "合同服务暂时不可用"
    assert result.answer == ""
    assert "年度采购合同" not in result.error_message
    assert "pending_review" not in result.error_message
    assert rag.calls == []


def test_policy_text_is_appended_from_retrieved_chunks_only():
    source = RAGSource(DOC, CHUNK, 0, "付款超过十万元需要总经理审批。", 0.8, "Qwen/Qwen3-Embedding-0.6B", 0, 16)
    rag = FakeRAG(RAGAnswer(
        answer="模型声称合同已经通过，这段不能出现。",
        sources=(source,),
        retrieval_count=1,
        model="Qwen/Qwen2.5-7B-Instruct",
        retrieval_strategy="vector",
        effective_strategy="vector",
        degraded=False,
        degradation_reason=None,
    ))
    tool = FakeTool(ContractToolResult("answered", ANSWER))
    question = QUESTION + "同时给出相关制度。"
    result = KnowledgeQAService(rag, contract_tool=tool).ask(TENANT, question)

    assert tool.calls == ["HT-2026-001"]
    assert rag.calls == [(TENANT, question)]
    assert result.answer.startswith(ANSWER)
    assert "付款超过十万元需要总经理审批。" in result.answer
    assert "模型声称合同已经通过" not in result.answer
    assert result.sources[0]["content"] == "付款超过十万元需要总经理审批。"
    assert result.retrieval_count == 1


def test_knowledge_qa_path_is_unchanged_when_a_contract_tool_is_installed():
    rag = FakeRAG(RAGAnswer(
        answer="部门负责人审批。",
        sources=(RAGSource(DOC, CHUNK, 0, "部门负责人审批。", 0.9, "Qwen/Qwen3-Embedding-0.6B", 0, 8),),
        retrieval_count=1,
        model="Qwen/Qwen2.5-7B-Instruct",
        retrieval_strategy="vector",
        effective_strategy="vector",
        degraded=False,
        degradation_reason=None,
    ))
    tool = FakeTool(ContractToolResult("answered", ANSWER))
    service = KnowledgeQAService(rag, contract_tool=tool)

    knowledge = service.ask(TENANT, "付款由谁审批？")
    business = service.ask(TENANT, "请审批这笔合同并安排付款")
    forced = service.ask(TENANT, QUESTION, requested_intent="knowledge_qa")
    unsupported = service.ask(TENANT, "审批这笔合同", requested_intent="contract_approval")

    assert tool.calls == []
    assert [call[1] for call in rag.calls] == ["付款由谁审批？", "请审批这笔合同并安排付款", QUESTION]
    assert knowledge.status == "answered"
    assert knowledge.answer == "部门负责人审批。"
    assert business.status == "answered"
    assert forced.status == "answered"
    assert unsupported.status == "unsupported"
    assert INSUFFICIENT_KNOWLEDGE not in unsupported.answer


def test_history_restore_does_not_include_the_actor_ticket():
    rag = FakeRAG()
    tool = FakeTool(ContractToolResult("answered", ANSWER))
    service = KnowledgeQAService(rag, checkpointer=MemorySaver(), contract_tool=tool)
    service.ask(
        TENANT,
        QUESTION,
        conversation_id=CONVERSATION,
        actor_id=ACTOR,
        actor_ticket=TICKET,
    )

    history = service.read_history(TENANT, CONVERSATION, actor_id=ACTOR)

    assert history.error_code is None
    assert history.messages[-1].content == ANSWER
    assert TICKET not in history.messages[-1].content
    assert all(TICKET not in message.content for message in history.messages)


def test_actor_ticket_is_not_written_into_the_checkpoint():
    rag = FakeRAG()
    tool = FakeTool(ContractToolResult("answered", ANSWER))
    saver = MemorySaver()
    service = KnowledgeQAService(rag, checkpointer=saver, contract_tool=tool)
    result = service.ask(
        TENANT,
        QUESTION,
        conversation_id=CONVERSATION,
        actor_id=ACTOR,
        actor_ticket=TICKET,
    )

    stored = repr(saver.storage)
    assert result.status == "answered"
    assert TICKET not in stored
    assert TICKET not in result.answer
