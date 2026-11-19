import inspect
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import EMBEDDING_MODEL_NAME, RERANKER_MODEL_NAME, Settings
from app.evaluation.dataset import EvalCase, EvalPassage, EvaluationDataset
from app.evaluation.hybrid_eval import compare_strategies
from app.evaluation.pgvector_eval import PassageBinding, TimedEmbeddingClient
from app.knowledge.hybrid import DEFAULT_RRF_K, HybridRetrievalHit, HybridRetriever, fuse_ranks
from app.knowledge.reranker import (
    RerankAPIError,
    RerankConfigurationError,
    RerankResponseError,
    RerankScore,
    SiliconFlowReranker,
)
from app.knowledge.reranking import HybridRerankRetriever, RerankService, RerankValidationError
from app.knowledge.retrieval import RetrievalHit
from scripts.evaluate_rag import main

API_KEY = "sk-test-not-a-real-key"
SECRET_TEXT = "UNIQUE-CHUNK-TEXT-SHOULD-NOT-LEAK"
TENANT = UUID("00000000-0000-4000-8000-00000000e023")
OTHER = UUID("00000000-0000-4000-8000-00000000e099")
DOC = UUID("00000000-0000-4000-8000-00000000d023")
LOW = UUID("00000000-0000-4000-8000-00000000aa01")
HIGH = UUID("00000000-0000-4000-8000-00000000aa02")
GOLD = UUID("00000000-0000-4000-8000-0000000000f6")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedReranker:
    def __init__(self, scores: list[float]) -> None:
        self.scores = scores
        self.calls = 0
        self.documents: list[list[str]] = []

    def score(self, _query: str, documents: list[str]) -> list[RerankScore]:
        self.calls += 1
        self.documents.append(list(documents))
        return [RerankScore(index, self.scores[index]) for index in range(len(documents))]


def test_empty_single_and_sorted_candidates_keep_original_fields():
    low = _hit(LOW, "甲", 0.2, 0.11)
    high = _hit(HIGH, "乙", 0.9, 0.22)
    third = _hit(GOLD, "丙", 0.4, 0.33)
    scorer = ScriptedReranker([1.0, 3.5, -1.0])
    service = RerankService(scorer, candidate_limit=20)

    assert service.rerank("问题", [], top_k=5) == []
    assert scorer.calls == 0

    single = service.rerank("问题", [high], top_k=5)
    ranked = service.rerank("问题", [low, high, third], top_k=2)

    assert single[0].chunk_id == HIGH
    assert single[0].rerank_score == 1.0
    assert [item.chunk_id for item in ranked] == [HIGH, LOW]
    assert [item.rerank_score for item in ranked] == [3.5, 1.0]
    assert ranked[0].similarity == 0.9
    assert ranked[0].rrf_score == 0.22
    assert ranked[0].vector_rank == 2
    assert ranked[0].keyword_rank == 4
    assert ranked[0].content == "乙"
    assert high.similarity == 0.9
    assert high.rrf_score == 0.22
    assert not hasattr(high, "rerank_score")


def test_ties_use_chunk_id_and_top_k_truncates():
    scorer = ScriptedReranker([1.0, 1.0])
    ranked = RerankService(scorer).rerank("问题", [_hit(HIGH, "乙", 0.2, 0.2), _hit(LOW, "甲", 0.2, 0.2)], top_k=5)
    limited = RerankService(ScriptedReranker([5.0, 4.0, 3.0])).rerank(
        "问题",
        [_hit(LOW, "甲", 0.1, 0.1), _hit(HIGH, "乙", 0.1, 0.1), _hit(GOLD, "丙", 0.1, 0.1)],
        top_k=1,
    )

    assert [item.chunk_id for item in ranked] == [LOW, HIGH]
    assert len(limited) == 1
    assert limited[0].chunk_id == LOW


def test_validation_errors_are_not_api_errors():
    service = RerankService(ScriptedReranker([]))
    with pytest.raises(RerankValidationError, match="top_k"):
        service.rerank("问题", [_hit(LOW, "甲", 0.1, 0.1)], top_k=0)
    with pytest.raises(RerankValidationError, match="不能为空"):
        service.rerank("  ", [_hit(LOW, "甲", 0.1, 0.1)])
    with pytest.raises(RerankValidationError, match="候选内容"):
        service.rerank("问题", [_hit(LOW, "  ", 0.1, 0.1)])


def test_reranker_only_scores_the_tenant_candidates_it_receives():
    mine = _hit(LOW, "本租户片段", 0.4, 0.4)
    other = _hit(HIGH, "其他租户片段", 0.9, 0.9)
    scorer = ScriptedReranker([2.0])
    hybrid = _HybridStub([mine])
    retriever = HybridRerankRetriever(hybrid, RerankService(scorer), candidate_k=20)

    found = retriever.search(TENANT, "问题", top_k=5)

    assert hybrid.tenants == [TENANT]
    assert OTHER not in hybrid.tenants
    assert hybrid.limits == [20]
    assert scorer.documents == [["本租户片段"]]
    assert [item.chunk_id for item in found] == [LOW]
    assert other.chunk_id not in {item.chunk_id for item in found}
    assert found[0].similarity == mine.similarity
    assert found[0].rrf_score == mine.rrf_score


def test_provider_rejects_bad_indexes_scores_and_errors():
    client = _client(lambda _request: _body([{"index": 0, "relevance_score": 0.2}, {"index": 2, "relevance_score": 0.1}]))
    with pytest.raises(RerankResponseError, match="index 不合法"):
        client.score("问题", ["甲", "乙"])

    client = _client(lambda _request: _body([{"index": 0, "relevance_score": 0.2}, {"index": 0, "relevance_score": 0.1}]))
    with pytest.raises(RerankResponseError, match="重复"):
        client.score("问题", ["甲", "乙"])

    client = _client(lambda _request: httpx.Response(200, content=b'{"results":[{"index":0,"relevance_score":NaN}]}'))
    with pytest.raises(RerankResponseError, match="非法分数"):
        client.score("问题", ["甲"])

    called = {"count": 0}

    def timeout(_request: httpx.Request) -> httpx.Response:
        called["count"] += 1
        raise httpx.ReadTimeout("timed out")

    client = _client(timeout, sleeper=lambda _seconds: None)
    with pytest.raises(RerankAPIError, match="超时") as caught:
        client.score(SECRET_TEXT, [SECRET_TEXT])
    assert called["count"] == 3
    assert API_KEY not in str(caught.value)
    assert SECRET_TEXT not in str(caught.value)

    calls = {"count": 0}

    def rejected(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(400, json={"message": f"{API_KEY} {SECRET_TEXT}"}, headers={"x-siliconcloud-trace-id": "trace-9"})

    client = _client(rejected, sleeper=lambda _seconds: None)
    with pytest.raises(RerankAPIError, match="HTTP 400") as caught:
        client.score(SECRET_TEXT, [SECRET_TEXT])
    assert calls["count"] == 1
    assert "trace_id=trace-9" in str(caught.value)
    assert API_KEY not in str(caught.value)
    assert SECRET_TEXT not in str(caught.value)
    assert str(request_url(client)).endswith("/rerank")


def test_request_uses_rerank_endpoint_and_settings_reject_the_embedding_model():
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url), "body": request.read()})
        return _body([{"index": 0, "relevance_score": 12.5}])

    client = SiliconFlowReranker.from_settings(
        Settings(siliconflow_api_key=API_KEY, siliconflow_base_url="https://api.siliconflow.cn/v1/"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _seconds: None,
    )
    scores = client.score("问题", ["甲"])

    assert seen[0]["url"] == "https://api.siliconflow.cn/v1/rerank"
    assert b"/embeddings" not in seen[0]["body"]
    assert b"Qwen/Qwen3-Reranker-0.6B" in seen[0]["body"]
    assert b'"return_documents":false' in seen[0]["body"] or b'"return_documents": false' in seen[0]["body"]
    assert scores == [RerankScore(0, 12.5)]
    with pytest.raises(ValidationError, match="RERANKER_MODEL"):
        Settings(reranker_model=EMBEDDING_MODEL_NAME)
    with pytest.raises(RerankConfigurationError, match="API Key"):
        SiliconFlowReranker(api_key="", base_url="https://api.siliconflow.cn/v1").score("问题", ["甲"])
    assert "ChatClient" not in inspect.getsource(SiliconFlowReranker)
    assert "rerank" not in inspect.getsource(HybridRetriever)
    assert RERANKER_MODEL_NAME == "Qwen/Qwen3-Reranker-0.6B"


def test_hybrid_rerank_evaluation_records_movement_without_replacing_vector_or_hybrid(tmp_path):
    clock = Clock()
    texts = ["甲", "乙", "丙", "丁", "戊", "正确片段"]
    ids = [UUID(f"00000000-0000-4000-8000-00000000000{c}") for c in "abcde"] + [GOLD]
    hits = [_vector(ids[index], texts[index], 0.9 - index / 100) for index in range(6)]
    client = TimedEmbeddingClient(_Embed(clock), clock=clock)
    vector = _Vector(hits, client, clock)
    keyword = _Keyword(clock)
    hybrid = HybridRetriever(vector, keyword, embedding_client=client, clock=clock)
    reranker = _QueryReranker(clock)
    combo = HybridRerankRetriever(hybrid, RerankService(reranker), candidate_k=20, clock=clock)
    dataset = _dataset(str(ids[0]))
    bindings = tuple(
        PassageBinding(str(chunk_id), DOC, chunk_id, DOC, "Qwen/Qwen3-Embedding-0.6B", 1024, True, True)
        for chunk_id in ids
    )

    report = compare_strategies(
        dataset,
        bindings,
        vector_retriever=vector,
        keyword_retriever=keyword,
        hybrid_retriever=hybrid,
        rerank_retriever=combo,
        k=5,
        embedding_client=client,
        clock=clock,
        tenant_id=TENANT,
        strategies=("vector", "hybrid", "hybrid_rerank"),
        reranker_model=RERANKER_MODEL_NAME,
    )
    baseline = compare_strategies(
        dataset,
        bindings,
        vector_retriever=vector,
        keyword_retriever=keyword,
        hybrid_retriever=hybrid,
        k=5,
        embedding_client=client,
        clock=clock,
        tenant_id=TENANT,
        strategies=("vector", "hybrid"),
    )

    assert report["evaluation_mode"] == "rerank-compare"
    assert report["strategies"]["vector"]["hit_rate_at_k"] == 0.5
    assert report["strategies"]["hybrid"]["hit_rate_at_k"] == 0.5
    assert report["strategies"]["hybrid_rerank"]["hit_rate_at_k"] == 0.5
    assert report["strategies"]["vector"]["retrieval_metrics"]["skipped_cases"] == 1
    assert report["rerank_changes"]["questions_entered_top_k"] == 1
    assert report["rerank_changes"]["questions_dropped_from_top_k"] == 1
    assert report["rerank_changes"]["questions_promoted"] == 1
    promoted = report["strategies"]["hybrid_rerank"]["per_case_results"][0]["rerank_change"]
    dropped = report["strategies"]["hybrid_rerank"]["per_case_results"][1]["rerank_change"]
    assert promoted["entered_top_k"] == [str(GOLD)]
    assert dropped["dropped_from_top_k"] == [str(ids[0])]
    assert report["strategies"]["hybrid_rerank"]["rerank_latency_ms"] == 120.0
    assert baseline["evaluation_mode"] == "hybrid-compare"
    assert baseline["rerank_changes"] is None
    assert "hybrid_rerank" not in baseline["strategies"]
    assert fuse_ranks([_vector(LOW, "甲", 0.2)], [], top_k=1, rrf_k=DEFAULT_RRF_K)[0].similarity == 0.2
    assert main(
        ["--dataset", str(tmp_path / "missing.json"), "--output", str(tmp_path / "out.json"), "--search-strategy", "rerank"]
    ) == 2


class _Embed:
    model_name = "Qwen/Qwen3-Embedding-0.6B"

    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def embed_query(self, _query: str) -> list[float]:
        self.clock.advance(0.01)
        return [0.1]


class _Vector:
    def __init__(self, hits: list[RetrievalHit], client: TimedEmbeddingClient, clock: Clock) -> None:
        self.hits = hits
        self.client = client
        self.clock = clock

    def search(self, _tenant_id: UUID, query: str, *, top_k: int = 5, document_id: UUID | None = None):
        del document_id
        self.client.embed_query(query)
        self.clock.advance(0.02)
        return list(self.hits[:top_k])


class _Keyword:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def search(self, _tenant_id: UUID, _query: str, *, top_k: int = 5, document_id: UUID | None = None):
        del top_k, document_id
        self.clock.advance(0.03)
        return []


class _QueryReranker:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def score(self, query: str, documents: list[str]) -> list[RerankScore]:
        self.clock.advance(0.04)
        if "提前" in query:
            return [RerankScore(index, 10.0 if text == "正确片段" else 1.0) for index, text in enumerate(documents)]
        return [RerankScore(index, 0.0 if index == 0 else 2.0) for index in range(len(documents))]


class _HybridStub:
    def __init__(self, hits: list[HybridRetrievalHit]) -> None:
        self.hits = hits
        self.tenants: list[UUID] = []
        self.limits: list[int] = []
        self.last_timing = type(
            "Timing",
            (),
            {
                "embedding_seconds": 0.01,
                "vector_seconds": 0.02,
                "keyword_seconds": 0.03,
                "fusion_seconds": 0.04,
            },
        )()

    def search(self, tenant_id: UUID, _query: str, *, top_k: int = 5, document_id: UUID | None = None):
        del document_id
        self.tenants.append(tenant_id)
        self.limits.append(top_k)
        return list(self.hits[:top_k])


def _hit(chunk_id: UUID, content: str, similarity: float, rrf_score: float) -> HybridRetrievalHit:
    return HybridRetrievalHit(DOC, chunk_id, 0, content, 0, 1, 2, 4, rrf_score, similarity)


def _vector(chunk_id: UUID, content: str, similarity: float) -> RetrievalHit:
    return RetrievalHit(DOC, chunk_id, 0, content, 0, 1, similarity, EMBEDDING_MODEL_NAME)


def _dataset(first_id: str) -> EvaluationDataset:
    passages = (
        EvalPassage(first_id, str(DOC), "甲", "first"),
        EvalPassage(str(GOLD), str(DOC), "正确片段", "gold"),
    )
    return EvaluationDataset(
        dataset_name="unit-rerank",
        passages=passages,
        cases=(
            EvalCase("promote", "提前正确片段", (str(GOLD),), ("正确",), None, False, "提前"),
            EvalCase("drop", "挤出原来的第一名", (first_id,), ("甲",), None, False, "挤出"),
            EvalCase("refuse", "食堂补贴多少？", (), (), None, True, "拒答"),
        ),
    )


def _client(handler, *, sleeper=None) -> SiliconFlowReranker:
    if sleeper is None:
        def sleeper(_seconds: float) -> None:
            return None
    return SiliconFlowReranker(
        api_key=API_KEY,
        base_url="https://api.siliconflow.cn/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=sleeper,
    )


def _body(results: list[dict[str, object]]) -> httpx.Response:
    return httpx.Response(200, json={"id": "rerank-test", "results": results})


def request_url(client: SiliconFlowReranker) -> str:
    return client._url
