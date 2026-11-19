from uuid import UUID

from app.agents.knowledge_qa.nodes import UNSUPPORTED_ANSWER
from app.agents.knowledge_qa.service import KnowledgeQAService
from app.knowledge.prompts import INSUFFICIENT_KNOWLEDGE
from app.knowledge.rag import RAGAnswer, RAGSource, RAGValidationError

TENANT_A = UUID("00000000-0000-4000-8000-00000000e026")
TENANT_B = UUID("00000000-0000-4000-8000-00000000e027")
DOC = UUID("00000000-0000-4000-8000-0000000000d1")
CHUNK_A = UUID("00000000-0000-4000-8000-0000000000a1")
CHUNK_B = UUID("00000000-0000-4000-8000-0000000000b2")


class FakeRAG:
    def __init__(self, result: RAGAnswer | None = None, error: Exception | None = None) -> None:
        self.result = result or _answer("部门负责人审批。[1]", (_source(CHUNK_A, "部门负责人审批。", 0.9),))
        self.error = error
        self.calls: list[tuple[object, object, object, object]] = []
        self._default_top_k = 5
        self._default_strategy = "vector"

    def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None) -> RAGAnswer:
        self.calls.append((tenant_id, question, top_k, strategy))
        if self.error is not None:
            raise self.error
        return self.result


def test_knowledge_question_runs_the_full_graph_and_keeps_sources():
    rag = FakeRAG(_answer("部门负责人审批。[1][2]", (_source(CHUNK_A, "部门负责人审批。", 0.8), _source(CHUNK_B, "总经理审批。", 0.7, rerank_score=3.5))))
    result = KnowledgeQAService(rag).ask(TENANT_A, "付款由谁审批？", top_k=5, retrieval_strategy="vector")

    assert rag.calls == [(TENANT_A, "付款由谁审批？", 5, "vector")]
    assert result.status == "answered"
    assert result.answer == "部门负责人审批。[1][2]"
    assert result.retrieval_count == 2
    assert [item["chunk_id"] for item in result.sources] == [str(CHUNK_A), str(CHUNK_B)]
    assert result.sources[0]["content"] == "部门负责人审批。"
    assert result.sources[1]["rerank_score"] == 3.5
    assert result.requested_strategy == "vector"
    assert result.effective_strategy == "vector"
    assert result.degraded is False
    assert result.error_code is None


def test_invalid_input_stops_before_rag():
    rag = FakeRAG()
    service = KnowledgeQAService(rag)

    blank = service.ask(TENANT_A, "  ")
    bad_tenant = service.ask("tenant-from-client", "付款由谁审批？")
    bad_top_k = service.ask(TENANT_A, "付款由谁审批？", top_k=0)
    none_top_k = service.ask(TENANT_A, "付款由谁审批？", top_k=None)
    bad_strategy = service.ask(TENANT_A, "付款由谁审批？", retrieval_strategy="keyword")

    assert rag.calls == []
    assert blank.status == "invalid_input"
    assert blank.error_message == "问题不能为空"
    assert bad_tenant.error_message == "tenant_id 必须是 UUID"
    assert "top_k" in str(bad_top_k.error_message)
    assert none_top_k.status == "invalid_input"
    assert bad_strategy.status == "invalid_input"
    assert INSUFFICIENT_KNOWLEDGE not in blank.answer


def test_strategies_are_forwarded_and_business_words_do_not_change_intent():
    rag = FakeRAG()
    service = KnowledgeQAService(rag)

    service.ask(TENANT_A, "请审批这笔合同并安排付款", retrieval_strategy="hybrid")
    service.ask(TENANT_A, "付款由谁审批？", retrieval_strategy="hybrid_rerank")
    service.ask(TENANT_A, "付款由谁审批？")

    assert [call[3] for call in rag.calls] == ["hybrid", "hybrid_rerank", "vector"]
    assert all(call[2] == 5 for call in rag.calls)


def test_unsupported_intent_does_not_call_rag_or_pretend_knowledge_is_missing():
    rag = FakeRAG()
    result = KnowledgeQAService(rag).ask(TENANT_A, "审批这笔合同", requested_intent="contract_approval")

    assert rag.calls == []
    assert result.status == "unsupported"
    assert result.error_code == "unsupported_intent"
    assert result.answer == UNSUPPORTED_ANSWER
    assert INSUFFICIENT_KNOWLEDGE not in result.answer
    assert result.sources == ()
    assert result.retrieval_count == 0


def test_refusal_degradation_and_errors_keep_safe_output():
    refused = FakeRAG(_answer(INSUFFICIENT_KNOWLEDGE, (), retrieval_count=0))
    refusal = KnowledgeQAService(refused).ask(TENANT_A, "食堂补贴多少？")
    assert refusal.status == "refused"
    assert refusal.answer == INSUFFICIENT_KNOWLEDGE
    assert refusal.sources == ()
    assert refusal.retrieval_count == 0

    degraded = FakeRAG(
        _answer(
            "部门负责人审批。[1]",
            (_source(CHUNK_A, "部门负责人审批。", 0.4),),
            degraded=True,
            degradation_reason="RerankAPIError: trace_id=trace-26",
            effective_strategy="hybrid",
        )
    )
    fallback = KnowledgeQAService(degraded).ask(TENANT_A, "付款由谁审批？", retrieval_strategy="hybrid_rerank")
    assert fallback.status == "answered"
    assert fallback.degraded is True
    assert fallback.effective_strategy == "hybrid"
    assert fallback.degradation_reason == "RerankAPIError: trace_id=trace-26"
    assert fallback.requested_strategy == "hybrid_rerank"

    secret = FakeRAG(error=RuntimeError("connection postgres://agent:password@db/app sk-live-secret"))
    failed = KnowledgeQAService(secret).ask(TENANT_A, "付款由谁审批？")
    assert failed.status == "failed"
    assert failed.error_code == "rag_failed"
    assert failed.error_message == "RuntimeError"
    assert "sk-live-secret" not in failed.error_message
    assert "password" not in failed.error_message
    assert failed.sources == ()

    visible = KnowledgeQAService(FakeRAG(error=RAGValidationError("未配置该检索策略"))).ask(TENANT_A, "付款由谁审批？", retrieval_strategy="hybrid")
    assert visible.status == "failed"
    assert visible.error_message == "RAGValidationError: 未配置该检索策略"


def test_requests_do_not_reuse_each_others_state():
    rag = FakeRAG()
    service = KnowledgeQAService(rag)

    def answer_for(tenant_id, question, *, top_k, strategy):
        del top_k, strategy
        chunk = CHUNK_A if tenant_id == TENANT_A else CHUNK_B
        return _answer(f"{tenant_id}:{question}", (_source(chunk, question, 0.5),))

    rag.ask = answer_for  # type: ignore[method-assign]
    rag.calls = []
    first = service.ask(TENANT_A, "第一题")
    second = service.ask(TENANT_B, "第二题", retrieval_strategy="hybrid", top_k=2)

    assert first.status == "answered"
    assert second.status == "answered"
    assert first.answer == f"{TENANT_A}:第一题"
    assert second.answer == f"{TENANT_B}:第二题"
    assert first.sources[0]["chunk_id"] == str(CHUNK_A)
    assert second.sources[0]["chunk_id"] == str(CHUNK_B)
    assert "第一题" not in second.answer
    assert second.requested_strategy == "hybrid"
    assert second.retrieval_count == 1
    assert first.sources != second.sources


def _answer(
    text: str,
    sources: tuple[RAGSource, ...],
    *,
    retrieval_count: int | None = None,
    degraded: bool = False,
    degradation_reason: str | None = None,
    effective_strategy: str = "vector",
) -> RAGAnswer:
    return RAGAnswer(
        answer=text,
        sources=sources,
        retrieval_count=len(sources) if retrieval_count is None else retrieval_count,
        model="Qwen/Qwen2.5-7B-Instruct",
        retrieval_strategy="hybrid_rerank" if effective_strategy == "hybrid" else effective_strategy,
        effective_strategy=effective_strategy,
        degraded=degraded,
        degradation_reason=degradation_reason,
    )


def _source(chunk_id: UUID, content: str, similarity: float, *, rerank_score: float | None = None) -> RAGSource:
    return RAGSource(DOC, chunk_id, 0, content, similarity, "Qwen/Qwen3-Embedding-0.6B", 0, len(content), rerank_score=rerank_score)

