import inspect
import json
from uuid import UUID

import pytest

from app.evaluation.dataset import EvalCase, EvalPassage, EvaluationDataset
from app.evaluation.hybrid_eval import INVOICE_STANDARD_CHUNK_ID, compare_strategies
from app.evaluation.pgvector_eval import PGVECTOR_EVAL_TENANT_ID, PassageBinding, TimedEmbeddingClient
from app.knowledge.hybrid import (
    DEFAULT_RRF_K,
    HybridRetriever,
    HybridValidationError,
    fuse_ranks,
)
from app.knowledge.keyword_retrieval import (
    KeywordRetriever,
    KeywordValidationError,
    _SEARCH_SQL,
    keyword_terms,
)
from app.knowledge.retrieval import RetrievalHit
from scripts.evaluate_rag import main

TENANT = PGVECTOR_EVAL_TENANT_ID
OTHER = UUID("00000000-0000-4000-8000-00000000e099")
DOC = UUID("00000000-0000-4000-8000-00000000d001")
DOC_B = UUID("00000000-0000-4000-8000-00000000d002")
CHUNK_A = UUID("00000000-0000-4000-8000-0000000000a1")
CHUNK_B = UUID("00000000-0000-4000-8000-0000000000b2")
CHUNK_C = UUID("00000000-0000-4000-8000-0000000000c3")
INVOICE = "付款前必须取得增值税专用发票，发票抬头为公司全称。"
LEAVE = "员工入职满一年后，每年可休年假 5 天。"
QUESTION = "超过 50 万元付给新供应商时，审批、发票和准入分别有什么要求？"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class InnerEmbed:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.model_name = "Qwen/Qwen3-Embedding-0.6B"

    def embed_query(self, _query: str) -> list[float]:
        self.clock.advance(0.012)
        return [0.1]


class Leg:
    def __init__(self, hits: list[object], clock: Clock | None = None, *, fail: bool = False) -> None:
        self.hits = hits
        self.clock = clock
        self.fail = fail
        self.calls = 0
        self.tenants: list[UUID] = []
        self.documents: list[UUID | None] = []
        self.limits: list[int] = []

    def search(self, tenant_id: UUID, _query: str, *, top_k: int = 5, document_id: UUID | None = None):
        self.calls += 1
        self.tenants.append(tenant_id)
        self.documents.append(document_id)
        self.limits.append(top_k)
        if self.fail:
            raise RuntimeError("检索失败")
        if self.clock is not None and not isinstance(self, VectorLeg):
            self.clock.advance(0.03)
        return list(self.hits)


class VectorLeg(Leg):
    def __init__(self, hits, client, clock: Clock, *, fail: bool = False) -> None:
        super().__init__(hits, clock, fail=fail)
        self.client = client

    def search(self, tenant_id: UUID, query: str, *, top_k: int = 5, document_id: UUID | None = None):
        self.calls += 1
        self.tenants.append(tenant_id)
        self.documents.append(document_id)
        self.limits.append(top_k)
        if self.fail:
            raise RuntimeError("向量检索失败")
        self.client.embed_query(query)
        self.clock.advance(0.02)
        return list(self.hits)


def test_keyword_validation_and_short_query_without_a_database():
    retriever = KeywordRetriever(_BoomPool())

    with pytest.raises(KeywordValidationError, match="不能为空"):
        retriever.search(TENANT, "  ")
    with pytest.raises(KeywordValidationError, match="top_k"):
        retriever.search(TENANT, "发票", top_k=0)
    with pytest.raises(KeywordValidationError, match="tenant_id"):
        retriever.search("tenant", "发票")  # type: ignore[arg-type]
    with pytest.raises(KeywordValidationError, match="document_id"):
        retriever.search(TENANT, "发票", document_id="doc")  # type: ignore[arg-type]
    assert retriever.search(TENANT, "发") == []
    assert keyword_terms("发票") == ("发票",)
    assert "HT2024" in keyword_terms("合同编号 HT2024")
    assert "50" in keyword_terms("超过 50 万元")


def test_keyword_terms_match_invoice_and_not_unrelated_text():
    terms = keyword_terms(QUESTION)
    invoice_matches = [term for term in terms if term in INVOICE]
    leave_matches = [term for term in terms if term in LEAVE]

    assert "发票" in invoice_matches
    assert leave_matches == []
    assert "business" not in _SEARCH_SQL
    assert "tenant_id = %s" in _SEARCH_SQL
    assert "order by hit.matched desc, c.id asc" in _SEARCH_SQL
    assert QUESTION not in _SEARCH_SQL


def test_rrf_single_path_double_path_and_duplicate_collapse():
    vector_only = fuse_ranks([_vector(CHUNK_A, 0.9), _vector(CHUNK_A, 0.1)], [], top_k=5)
    both = fuse_ranks([_vector(CHUNK_A, 0.8)], [_keyword(CHUNK_A)], top_k=5)
    custom = fuse_ranks([_vector(CHUNK_A, 0.8)], [], top_k=5, rrf_k=10)

    assert vector_only[0].vector_rank == 1
    assert vector_only[0].keyword_rank is None
    assert vector_only[0].rrf_score == pytest.approx(1 / (DEFAULT_RRF_K + 1))
    assert vector_only[0].similarity == 0.9
    assert both[0].vector_rank == 1
    assert both[0].keyword_rank == 1
    assert both[0].rrf_score == pytest.approx(2 / (DEFAULT_RRF_K + 1))
    assert custom[0].rrf_score == pytest.approx(1 / 11)


def test_rrf_tie_uses_chunk_id_and_top_k_limits_the_list():
    tied = fuse_ranks([_vector(CHUNK_B, 0.4)], [_keyword(CHUNK_A)], top_k=5)
    limited = fuse_ranks(
        [_vector(CHUNK_A, 0.9), _vector(CHUNK_B, 0.8), _vector(CHUNK_C, 0.7)],
        [],
        top_k=1,
    )

    assert [item.chunk_id for item in tied] == [CHUNK_A, CHUNK_B]
    assert tied[0].rrf_score == tied[1].rrf_score
    assert len(limited) == 1
    assert limited[0].chunk_id == CHUNK_A


def test_hybrid_empty_inputs_tenant_and_document_filter():
    clock = Clock()
    client = TimedEmbeddingClient(InnerEmbed(clock), clock=clock)
    vector = VectorLeg([], client, clock)
    keyword = Leg([], clock)
    hybrid = HybridRetriever(vector, keyword, embedding_client=client, clock=clock)

    assert hybrid.search(TENANT, "发票", top_k=5, document_id=DOC) == []
    assert vector.tenants == [TENANT]
    assert keyword.tenants == [TENANT]
    assert vector.documents == [DOC]
    assert keyword.documents == [DOC]
    assert vector.limits == [20]
    assert keyword.limits == [20]


def test_hybrid_stops_when_either_leg_fails():
    clock = Clock()
    client = TimedEmbeddingClient(InnerEmbed(clock), clock=clock)
    vector = VectorLeg([], client, clock, fail=True)
    keyword = Leg([_keyword(CHUNK_A)], clock)
    hybrid = HybridRetriever(vector, keyword, embedding_client=client, clock=clock)
    with pytest.raises(RuntimeError, match="向量检索失败"):
        hybrid.search(TENANT, "发票")
    assert keyword.calls == 0

    vector.fail = False
    keyword.fail = True
    with pytest.raises(RuntimeError, match="检索失败"):
        hybrid.search(TENANT, "发票")
    assert vector.calls == 2


def test_hybrid_rejects_invalid_rrf_and_candidate_width():
    hybrid = HybridRetriever(Leg([]), Leg([]), rrf_k=60)
    with pytest.raises(HybridValidationError, match="rrf_k"):
        HybridRetriever(Leg([]), Leg([]), rrf_k=0)
    with pytest.raises(HybridValidationError, match="vector_k"):
        hybrid.search(TENANT, "发票", top_k=5, vector_k=2)


def test_hybrid_does_not_mix_tenant_arguments():
    clock = Clock()
    client = TimedEmbeddingClient(InnerEmbed(clock), clock=clock)
    vector = VectorLeg([_vector(CHUNK_A, 0.5)], client, clock)
    keyword = Leg([_keyword(CHUNK_B)], clock)
    hybrid = HybridRetriever(vector, keyword, embedding_client=client, clock=clock)

    found = hybrid.search(TENANT, "发票", top_k=5)

    assert vector.tenants == [TENANT]
    assert keyword.tenants == [TENANT]
    assert OTHER not in vector.tenants
    assert {item.chunk_id for item in found} == {CHUNK_A, CHUNK_B}
    assert found[0].rrf_score == found[1].rrf_score


def test_strategy_metrics_skip_refusals_and_record_latency(tmp_path):
    clock = Clock()
    client = TimedEmbeddingClient(InnerEmbed(clock), clock=clock)
    invoice = UUID("00000000-0000-4000-8000-000000000008")
    vector = VectorLeg([_vector(CHUNK_A, 0.8)], client, clock)
    keyword = Leg([_keyword(invoice)], clock)
    hybrid = HybridRetriever(vector, keyword, embedding_client=client, clock=clock)
    dataset = _dataset()
    bindings = (
        PassageBinding(str(CHUNK_A), DOC, CHUNK_A, DOC, "Qwen/Qwen3-Embedding-0.6B", 1024, True, True),
        PassageBinding(INVOICE_STANDARD_CHUNK_ID, DOC, invoice, DOC_B, "Qwen/Qwen3-Embedding-0.6B", 1024, True, True),
    )

    report = compare_strategies(
        dataset,
        bindings,
        vector_retriever=vector,
        keyword_retriever=keyword,
        hybrid_retriever=hybrid,
        k=5,
        embedding_client=client,
        clock=clock,
    )

    assert report["strategies"]["vector"]["hit_rate_at_k"] == 0.5
    assert report["strategies"]["keyword"]["hit_rate_at_k"] == 0.5
    assert report["strategies"]["hybrid"]["hit_rate_at_k"] == 1.0
    assert report["strategies"]["vector"]["retrieval_metrics"]["skipped_cases"] == 1
    assert report["strategies"]["keyword"]["retrieval_metrics"]["skipped_cases"] == 1
    assert report["strategies"]["hybrid"]["retrieval_metrics"]["skipped_cases"] == 1
    assert report["comparison"]["hit_rate_at_k"]["vector"] == 0.5
    assert report["focus_case"]["hybrid"]["contains_invoice"] is True
    assert report["focus_case"]["vector"]["contains_invoice"] is False
    assert report["strategies"]["hybrid"]["embedding_latency_ms"] == 36.0
    assert report["strategies"]["hybrid"]["vector_latency_ms"] == 60.0
    assert report["strategies"]["hybrid"]["keyword_latency_ms"] == 90.0
    assert report["strategies"]["hybrid"]["fusion_latency_ms"] == 0.0
    assert report["answer_metrics"]["evaluated"] is False
    loaded = json.loads(json.dumps(report, ensure_ascii=False))
    assert loaded["evaluation_mode"] == "hybrid-compare"
    assert set(loaded["comparison"]["hit_rate_at_k"]) == {"vector", "keyword", "hybrid"}
    assert "ChatClient" not in inspect.getsource(HybridRetriever)
    import app.evaluation.hybrid_eval as hybrid_eval
    import app.knowledge.keyword_retrieval as keyword_retrieval

    assert "ChatClient" not in inspect.getsource(hybrid_eval)
    assert "ChatClient" not in inspect.getsource(keyword_retrieval)
    assert main(
        ["--dataset", str(tmp_path / "missing.json"), "--output", str(tmp_path / "out.json"), "--search-strategy", "all"]
    ) == 2


def _vector(chunk_id: UUID, similarity: float) -> RetrievalHit:
    return RetrievalHit(DOC, chunk_id, 0, "向量片段", 0, 1, similarity, "Qwen/Qwen3-Embedding-0.6B")


def _keyword(chunk_id: UUID):
    from app.knowledge.keyword_retrieval import KeywordHit

    return KeywordHit(DOC, chunk_id, 0, "关键词片段", 0, 1, 1, 1.0)


def _dataset() -> EvaluationDataset:
    return EvaluationDataset(
        dataset_name="unit-hybrid",
        passages=(
            EvalPassage(str(CHUNK_A), str(DOC), "付款申请须先由部门负责人审批。", "a"),
            EvalPassage(INVOICE_STANDARD_CHUNK_ID, str(DOC), INVOICE, "invoice"),
        ),
        cases=(
            EvalCase("hit", "谁先审批？", (str(CHUNK_A),), ("审批",), None, False, "命中"),
            EvalCase(
                "multi-condition-large-payment",
                QUESTION,
                (INVOICE_STANDARD_CHUNK_ID,),
                ("发票",),
                None,
                False,
                "发票",
            ),
            EvalCase("refuse", "食堂补贴多少？", (), (), None, True, "拒答"),
        ),
    )


class _BoomPool:
    def connection(self):
        raise AssertionError("不应连接数据库")
