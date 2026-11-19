import logging
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.core.config import CHAT_MODEL_NAME, Settings
from app.evaluation.answer_eval import FAITHFULNESS_NOTE, compare_rag_answers
from app.evaluation.dataset import EvalCase, EvalPassage, EvaluationDataset
from app.evaluation.pgvector_eval import PassageBinding
from app.knowledge.hybrid import HybridRetrievalHit
from app.knowledge.prompts import MAX_CONTEXT_CHARS, SYSTEM_PROMPT
from app.knowledge.rag import RAGService, RAGValidationError
from app.knowledge.reranker import RerankAPIError
from app.knowledge.reranking import RerankValidationError, RerankedHit
from app.knowledge.retrieval import RetrievalHit
from app.knowledge.strategies import RetrievalStrategies
from scripts.evaluate_rag import main

TENANT = UUID("00000000-0000-4000-8000-00000000e024")
OTHER = UUID("00000000-0000-4000-8000-00000000e099")
DOC = UUID("00000000-0000-4000-8000-00000000d024")
FIRST = UUID("00000000-0000-4000-8000-0000000000a1")
SECOND = UUID("00000000-0000-4000-8000-0000000000b2")
SECRET = "SECRET-PASSAGE-SHOULD-NOT-BE-LOGGED"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 0.01
        return self.now


class Chat:
    def __init__(self, answer: str = "部门负责人审核。[1][2]") -> None:
        self.model_name = CHAT_MODEL_NAME
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        if "食堂" in user:
            return "当前知识库中没有检索到与该问题相关的资料。"
        return self.answer


class VectorStub:
    def __init__(self, hits: list[RetrievalHit]) -> None:
        self.hits = hits
        self.tenants: list[UUID] = []
        self.calls = 0

    def search(self, tenant_id: UUID, _query: str, *, top_k: int = 5, document_id: UUID | None = None):
        del document_id
        self.calls += 1
        self.tenants.append(tenant_id)
        return list(self.hits[:top_k])


class HybridStub:
    def __init__(self, hits: list[HybridRetrievalHit]) -> None:
        self.hits = hits
        self.tenants: list[UUID] = []
        self.limits: list[int] = []
        self.last_timing = SimpleNamespace(
            embedding_seconds=0.01,
            vector_seconds=0.02,
            keyword_seconds=0.03,
            fusion_seconds=0.04,
        )

    def search(self, tenant_id: UUID, _query: str, *, top_k: int = 5, document_id: UUID | None = None):
        del document_id
        self.tenants.append(tenant_id)
        self.limits.append(top_k)
        return list(self.hits[:top_k])


class RerankStub:
    def __init__(self, hits: list[RerankedHit], candidates: list[HybridRetrievalHit], error: Exception | None = None) -> None:
        self.hits = hits
        self.last_candidates = tuple(candidates)
        self.error = error
        self.tenants: list[UUID] = []
        self.calls = 0
        self.last_timing = SimpleNamespace(
            embedding_seconds=0.01,
            vector_seconds=0.02,
            keyword_seconds=0.03,
            fusion_seconds=0.04,
            rerank_seconds=0.05,
        )

    def search(self, tenant_id: UUID, _query: str, *, top_k: int = 5, document_id: UUID | None = None):
        del document_id
        self.calls += 1
        self.tenants.append(tenant_id)
        if self.error is not None:
            raise self.error
        return list(self.hits[:top_k])


def test_three_strategies_keep_scores_order_and_tenant():
    vector_hit = _vector(FIRST, "向量片段", 0.8)
    hybrid_hits = [_hybrid(SECOND, "乙", 0.2, 0.4), _hybrid(FIRST, "甲", 0.9, 0.3)]
    reranked = [_reranked(hybrid_hits[0], 4.5), _reranked(hybrid_hits[1], 1.5)]
    vector = VectorStub([vector_hit])
    hybrid = HybridStub(hybrid_hits)
    rerank = RerankStub(reranked, hybrid_hits)
    chat = Chat()
    service = _service(vector, hybrid, rerank, chat)

    vector_answer = service.ask(TENANT, "问题", strategy="vector")
    hybrid_answer = service.ask(TENANT, "问题", strategy="hybrid")
    rerank_answer = service.ask(TENANT, "问题", strategy="hybrid_rerank")

    assert vector.tenants == [TENANT]
    assert hybrid.tenants == [TENANT]
    assert rerank.tenants == [TENANT]
    assert OTHER not in vector.tenants
    assert vector_answer.retrieval_strategy == "vector"
    assert vector_answer.sources[0].similarity == 0.8
    assert vector_answer.sources[0].rrf_score is None
    assert vector_answer.sources[0].rerank_score is None
    assert [item.chunk_id for item in hybrid_answer.sources] == [SECOND, FIRST]
    assert hybrid_answer.sources[0].rrf_score == 0.4
    assert hybrid_answer.sources[0].similarity == 0.2
    assert [item.chunk_id for item in rerank_answer.sources] == [SECOND, FIRST]
    assert rerank_answer.sources[0].rerank_score == 4.5
    assert rerank_answer.sources[0].rrf_score == 0.4
    assert rerank_answer.sources[0].similarity == 0.2
    assert chat.calls[-1][0] == SYSTEM_PROMPT
    user = chat.calls[-1][1]
    assert user.index("[1]") < user.index("乙") < user.index("[2]") < user.index("甲")
    assert rerank.calls == 1


def test_reranker_strict_fallback_and_disabled_paths(caplog):
    hybrid_hits = [_hybrid(FIRST, SECRET, 0.7, 0.5)]
    hybrid = HybridStub(hybrid_hits)
    error = RerankAPIError("Rerank 请求失败，HTTP 503，trace_id=trace-24")
    strict = RerankStub([], hybrid_hits, error=error)
    chat = Chat()
    strict_service = _service(VectorStub([]), hybrid, strict, chat, policy="strict")
    with pytest.raises(RerankAPIError, match="trace-24"):
        strict_service.ask(TENANT, "问题", strategy="hybrid_rerank")
    assert chat.calls == []

    fallback_rerank = RerankStub([], hybrid_hits, error=error)
    fallback_chat = Chat("依据资料回答。[1]")
    fallback = _service(VectorStub([]), hybrid, fallback_rerank, fallback_chat, policy="fallback")
    with caplog.at_level(logging.WARNING):
        answer = fallback.ask(TENANT, "问题", strategy="hybrid_rerank")
    assert answer.degraded is True
    assert answer.effective_strategy == "hybrid"
    assert answer.retrieval_strategy == "hybrid_rerank"
    assert answer.degradation_reason is not None
    assert "trace-24" in answer.degradation_reason
    assert SECRET not in answer.degradation_reason
    assert SECRET not in caplog.text
    assert answer.sources[0].chunk_id == FIRST
    assert answer.sources[0].rerank_score is None
    assert fallback_chat.calls

    disabled = RetrievalStrategies(vector=VectorStub([_vector(FIRST, "甲", 0.5)]), hybrid=hybrid, hybrid_rerank=None)
    disabled_chat = Chat()
    disabled_rerank = RerankStub([_reranked(hybrid_hits[0], 1.0)], hybrid_hits)
    service = RAGService(disabled._vector, disabled_chat, strategies=disabled)
    assert service.ask(TENANT, "问题", strategy="vector").sources[0].content == "甲"
    with pytest.raises(RAGValidationError, match="Reranker"):
        service.ask(TENANT, "问题", strategy="hybrid_rerank")
    assert disabled_rerank.calls == 0

    invalid = RerankStub([], hybrid_hits, error=RerankValidationError("候选内容不能为空"))
    for policy in ("strict", "fallback"):
        guarded = _service(VectorStub([]), hybrid, invalid, Chat(), policy=policy)
        with pytest.raises(RerankValidationError):
            guarded.ask(TENANT, "问题", strategy="hybrid_rerank")


def test_empty_retrieval_refuses_without_chat_and_citations_stay_aligned():
    chat = Chat("不应该被调用")
    service = _service(VectorStub([]), HybridStub([]), RerankStub([], []), chat)
    answer = service.ask(TENANT, "没有资料的问题", strategy="vector")
    assert chat.calls == []
    assert answer.sources == ()
    assert answer.context_count == 0
    assert "没有检索到" in answer.answer

    long_first = _reranked(_hybrid(SECOND, "乙" * 5000, 0.2, 0.2), 9.0)
    long_second = _reranked(_hybrid(FIRST, "甲" * 5000, 0.2, 0.2), 8.0)
    long_third = _reranked(_hybrid(uuid4(), "丙" * 100, 0.2, 0.2), 7.0)
    ordered = RerankStub([long_first, long_second, long_third], [])
    cited = Chat("第一段。[1] 不存在的来源。[9]")
    service = _service(VectorStub([]), HybridStub([]), ordered, cited)
    result = service.ask(TENANT, "截断", strategy="hybrid_rerank", top_k=3)
    assert result.context_count == 2
    assert result.retrieval_count == 3
    assert sum(len(item.content) for item in result.sources) == MAX_CONTEXT_CHARS
    assert result.sources[0].chunk_id == SECOND
    assert result.sources[0].content == "乙" * 5000
    assert result.sources[1].content.startswith("甲")
    user = cited.calls[0][1]
    assert user.index("[1]") < user.index("乙") < user.index("[2]") < user.index("甲")
    assert "丙" not in user
    assert "[9]" in result.answer
    assert len(result.sources) == 2


def test_answer_comparison_reuses_metrics_and_does_not_claim_faithfulness(tmp_path):
    passage = EvalPassage(str(FIRST), str(DOC), "部门负责人审核。", "审批")
    dataset = EvaluationDataset(
        dataset_name="unit-rag-answer",
        passages=(passage,),
        cases=(
            EvalCase("pay", "谁先审批？", (str(FIRST),), ("部门负责人",), None, False, "命中"),
            EvalCase("refuse", "食堂补贴多少？", (), (), None, True, "拒答"),
        ),
    )
    binding = PassageBinding(str(FIRST), DOC, FIRST, DOC, "Qwen/Qwen3-Embedding-0.6B", 1024, True, True)
    hit = _vector(FIRST, "部门负责人审核。", 0.9)
    chat = Chat("部门负责人审核。[1]")
    service = _service(VectorStub([hit]), HybridStub([_hybrid(FIRST, hit.content, 0.9, 0.3)]), RerankStub([_reranked(_hybrid(FIRST, hit.content, 0.9, 0.3), 2.0)], []), chat)
    report = compare_rag_answers(dataset, [binding], rag_service=service, k=5, tenant_id=TENANT, chat_model=CHAT_MODEL_NAME)

    assert report["faithfulness"] is None
    assert report["faithfulness_note"] == FAITHFULNESS_NOTE
    assert report["strategies"]["vector"]["retrieval_metrics"]["skipped_cases"] == 1
    assert report["strategies"]["hybrid"]["hit_rate_at_k"] == 1.0
    assert report["strategies"]["hybrid_rerank"]["answer_metrics"]["faithfulness"] is None
    assert report["strategies"]["vector"]["answer_metrics"]["refusal_accuracy"] == 1.0
    assert report["strategies"]["vector"]["answer_metrics"]["cited_fact_support"] == 1.0
    assert main(["--dataset", str(tmp_path / "missing.json"), "--output", str(tmp_path / "out.json"), "--search-strategy", "answer"]) == 2
    with pytest.raises(ValidationError, match="RAG_RETRIEVAL_STRATEGY"):
        Settings(rag_retrieval_strategy="keyword")
    with pytest.raises(ValidationError, match="RAG_RERANKER_FAILURE_POLICY"):
        Settings(rag_reranker_failure_policy="ignore")


def _service(vector, hybrid, rerank, chat, *, policy: str = "strict") -> RAGService:
    clock = Clock()
    strategies = RetrievalStrategies(
        vector=vector,
        hybrid=hybrid,
        hybrid_rerank=rerank,
        failure_policy=policy,
        candidate_k=20,
        clock=clock,
    )
    return RAGService(vector, chat, strategies=strategies, default_strategy="vector", clock=clock)


def _vector(chunk_id: UUID, content: str, similarity: float) -> RetrievalHit:
    return RetrievalHit(DOC, chunk_id, 0, content, 0, len(content), similarity, "Qwen/Qwen3-Embedding-0.6B")


def _hybrid(chunk_id: UUID, content: str, similarity: float, rrf_score: float) -> HybridRetrievalHit:
    return HybridRetrievalHit(DOC, chunk_id, 0, content, 0, len(content), 1, 2, rrf_score, similarity)


def _reranked(hit: HybridRetrievalHit, score: float) -> RerankedHit:
    return RerankedHit(
        hit.document_id,
        hit.chunk_id,
        hit.chunk_index,
        hit.content,
        hit.char_start,
        hit.char_end,
        hit.vector_rank,
        hit.keyword_rank,
        hit.rrf_score,
        hit.similarity,
        score,
    )
