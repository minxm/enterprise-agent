import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.core.config import EMBEDDING_MODEL_NAME, Settings
from app.evaluation.answers import aggregate_answers, is_refusal, score_answer
from app.evaluation.dataset import EvaluationDatasetError, load_dataset, parse_dataset
from app.evaluation.evaluator import RAGEvaluator, report_to_dict
from app.evaluation.live import EmbeddingCorpusRetriever, EvaluationConfigError, build_live_rag
from app.evaluation.metrics import EvaluationInputError, aggregate_retrieval, score_retrieval
from app.knowledge.prompts import INSUFFICIENT_KNOWLEDGE
from scripts.evaluate_rag import main

DATASET = "evaluation_data/enterprise_contract_eval.json"
CHUNK_A = "00000000-0000-4000-8000-000000000001"
CHUNK_B = "00000000-0000-4000-8000-000000000002"
CHUNK_C = "00000000-0000-4000-8000-000000000003"
DOCUMENT = "00000000-0000-4000-8000-0000000000a1"


def test_dataset_file_loads_the_synthetic_cases():
    dataset = load_dataset(DATASET)

    assert dataset.dataset_name == "enterprise-contract-eval-v1"
    assert len(dataset.cases) == 12
    known = set(dataset.passage_map())
    descriptions = {case.description for case in dataset.cases}
    assert "单个知识片段即可回答" in descriptions
    assert "需要多个知识片段共同回答" in descriptions
    assert "知识库没有答案" in descriptions
    assert "容易与付款 50 万元阈值混淆" in descriptions
    assert "问法不同但语义相同" in descriptions
    assert "包含多个条件" in descriptions
    assert "资料存在冲突，应同时看到双方说法" in descriptions
    for case in dataset.cases:
        assert set(case.expected_chunk_ids) <= known
        if case.should_refuse:
            assert case.expected_chunk_ids == ()


def test_dataset_rejects_a_missing_field():
    payload = _payload()
    del payload["cases"][0]["question"]

    with pytest.raises(EvaluationDatasetError, match="question"):
        parse_dataset(payload)


def test_dataset_rejects_a_wrong_type():
    payload = _payload()
    payload["cases"][0]["should_refuse"] = 1

    with pytest.raises(EvaluationDatasetError, match="布尔值"):
        parse_dataset(payload)


def test_dataset_rejects_an_unknown_chunk_id():
    payload = _payload()
    payload["cases"][0]["expected_chunk_ids"] = ["00000000-0000-4000-8000-000000000099"]

    with pytest.raises(EvaluationDatasetError, match="不存在的片段"):
        parse_dataset(payload)


def test_hit_rate_recall_and_mrr():
    score = score_retrieval([CHUNK_C, CHUNK_A, CHUNK_B], [CHUNK_A, CHUNK_B], 3)

    assert score.hit == 1.0
    assert score.recall == 1.0
    assert score.mrr == 0.5
    assert score.first_relevant_rank == 2

    partial = score_retrieval([CHUNK_A, CHUNK_C], [CHUNK_A, CHUNK_B], 2)
    assert partial.hit == 1.0
    assert partial.recall == 0.5
    assert partial.mrr == 1.0


def test_empty_retrieval_scores_zero_when_gold_exists():
    score = score_retrieval([], [CHUNK_A], 5)

    assert score.applicable is True
    assert score.hit == 0.0
    assert score.recall == 0.0
    assert score.mrr == 0.0


def test_empty_gold_is_not_a_retrieval_miss():
    score = score_retrieval([CHUNK_A], [], 5)
    summary = aggregate_retrieval(
        [
            score,
            score_retrieval([CHUNK_A], [CHUNK_A], 5),
        ]
    )

    assert score.applicable is False
    assert score.hit is None
    assert summary["applicable_cases"] == 1
    assert summary["skipped_cases"] == 1
    assert summary["hit_rate"] == 1.0


def test_duplicate_hits_do_not_increase_recall():
    score = score_retrieval([CHUNK_A, CHUNK_A, CHUNK_B], [CHUNK_A, CHUNK_B], 2)

    assert score.duplicate_chunk_ids == (CHUNK_A,)
    assert score.hit == 1.0
    assert score.recall == 0.5
    assert score.mrr == 1.0

    later = score_retrieval([CHUNK_A, CHUNK_A, CHUNK_B], [CHUNK_B], 3)
    assert later.mrr == pytest.approx(1 / 3)
    assert later.recall == 1.0


def test_k_must_be_a_positive_integer():
    with pytest.raises(EvaluationInputError, match="K"):
        score_retrieval([CHUNK_A], [CHUNK_A], 0)
    with pytest.raises(EvaluationInputError, match="K"):
        score_retrieval([CHUNK_A], [CHUNK_A], True)  # type: ignore[arg-type]


def test_citation_numbers_must_point_at_returned_sources():
    valid = score_answer(answer="部门负责人审批。[1]", source_count=2, should_refuse=False, expected_facts=["部门负责人"])
    out_of_range = score_answer(answer="见[3]", source_count=2, should_refuse=False, expected_facts=["部门负责人"])
    malformed = score_answer(answer="见[01]和[1, 2]", source_count=2, should_refuse=False, expected_facts=[])

    assert valid.citation_valid is True
    assert valid.citations == (1,)
    assert out_of_range.citation_valid is False
    assert out_of_range.out_of_range_citations == (3,)
    assert malformed.malformed_citations == ("[01]", "[1, 2]")


def test_refusal_judgement_accepts_more_than_one_sentence():
    assert is_refusal(INSUFFICIENT_KNOWLEDGE) is True
    assert is_refusal("当前知识库中没有这项制度。") is True
    assert is_refusal("付款由部门负责人审批。[1]") is False

    answered = score_answer(
        answer="付款由部门负责人审批。[1]",
        source_count=1,
        should_refuse=True,
        expected_facts=[],
    )
    refused = score_answer(
        answer="知识库资料不足，无法回答班车时间。",
        source_count=0,
        should_refuse=False,
        expected_facts=["发车时刻表"],
    )

    assert answered.refusal_correct is False
    assert refused.refusal_correct is False
    assert refused.keyword_coverage == 0.0


def test_keyword_match_is_labeled_as_a_surface_check():
    score = score_answer(
        answer="须先由部门负责人审批。[1]",
        source_count=1,
        should_refuse=False,
        expected_facts=["部门负责人", "总经理"],
    )

    assert score.matched_facts == ("部门负责人",)
    assert score.missing_facts == ("总经理",)
    assert score.keyword_coverage == 0.5
    assert "不等于事实正确" in score.keyword_match_note
    assert "忠实" in score.keyword_match_note


def test_one_failed_case_is_recorded_and_excluded_from_the_average():
    dataset = parse_dataset(
        _payload(
            [
                _case("ok", "付款由谁审批？", [CHUNK_A]),
                _case("bad", "会失败的问题", [CHUNK_A]),
                _case("refuse", "食堂补贴多少？", [], should_refuse=True, facts=[]),
            ]
        )
    )
    report = RAGEvaluator().evaluate(
        dataset,
        retriever=_Retriever(
            {
                "付款由谁审批？": [CHUNK_A],
                "食堂补贴多少？": [],
            },
            error_on="会失败的问题",
        ),
        rag_service=_Answerer(
            {
                "付款由谁审批？": "部门负责人审批。[1]",
                "食堂补贴多少？": INSUFFICIENT_KNOWLEDGE,
            }
        ),
        k=5,
    )

    assert report.total_cases == 3
    assert report.successful_cases == 2
    assert report.failed_cases == 1
    assert report.failures == ({"case_id": "bad", "reason": "RuntimeError: 检索失败"},)
    assert report.retrieval_metrics["hit_rate"] == 1.0
    assert report.retrieval_metrics["skipped_cases"] == 1
    assert report.answer_metrics["refusal_accuracy"] == 1.0
    failed = [item for item in report.per_case_results if item.case_id == "bad"][0]
    assert failed.status == "failed"
    assert failed.retrieval is None


def test_json_report_contains_the_required_sections(tmp_path):
    dataset = parse_dataset(_payload([_case("ok", "付款由谁审批？", [CHUNK_A])]))
    report = RAGEvaluator().evaluate(
        dataset,
        retriever=_Retriever({"付款由谁审批？": [CHUNK_A]}),
        rag_service=_Answerer({"付款由谁审批？": "部门负责人审批。[1]"}),
        k=5,
    )
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report_to_dict(report), ensure_ascii=False), encoding="utf-8")
    loaded = json.loads(path.read_text(encoding="utf-8"))

    for key in (
        "evaluation_id",
        "dataset_name",
        "total_cases",
        "successful_cases",
        "failed_cases",
        "retrieval_metrics",
        "answer_metrics",
        "per_case_results",
        "evaluation_time",
    ):
        assert key in loaded
    assert loaded["per_case_results"][0]["case_id"] == "ok"
    assert loaded["answer_metrics"]["keyword_match_note"]
    summary = aggregate_answers([])
    assert summary["case_count"] == 0


def test_offline_cli_writes_a_report_without_calling_a_model(tmp_path, monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("不应创建模型客户端")

    monkeypatch.setattr("app.knowledge.embeddings.EmbeddingClient.__init__", explode)
    monkeypatch.setattr("app.models.chat.ChatClient.__init__", explode)
    output = tmp_path / "offline.json"

    code = main(["--dataset", DATASET, "--top-k", "5", "--output", str(output)])

    report = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert report["mode"] == "offline"
    assert report["total_cases"] == 12
    assert report["failed_cases"] == 0


def test_cli_rejects_invalid_arguments(tmp_path):
    assert main(["-h"]) == 0
    assert main(["--output", str(tmp_path / "missing.json")]) == 2
    assert main(["--dataset", DATASET, "--top-k", "0", "--output", str(tmp_path / "bad.json")]) == 2


def test_memory_retriever_ranks_with_a_fake_embedding_client():
    dataset = load_dataset(DATASET)
    client = _FakeEmbedder()
    retriever = EmbeddingCorpusRetriever(dataset.passages, client)

    hits = retriever.search(UUID(DOCUMENT), "付款申请由谁审批？", top_k=2)

    assert hits[0].content.startswith("付款申请须先由部门负责人")
    assert client.embed_query_calls == 1
    assert client.embed_calls == 1


def test_live_builder_stops_before_creating_clients_when_the_key_is_missing(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("不应创建模型客户端")

    monkeypatch.setattr("app.evaluation.live.EmbeddingClient.from_settings", explode)
    monkeypatch.setattr("app.evaluation.live.ChatClient.from_settings", explode)

    with pytest.raises(EvaluationConfigError, match="SILICONFLOW_API_KEY"):
        build_live_rag([], Settings(siliconflow_api_key=""))


class _Hit:
    def __init__(self, chunk_id: str) -> None:
        self.chunk_id = chunk_id


class _Retriever:
    def __init__(self, mapping: dict[str, list[str]], *, error_on: str | None = None) -> None:
        self.mapping = mapping
        self.error_on = error_on

    def search(self, _tenant_id: UUID, query: str, *, top_k: int = 5, **_kwargs: object) -> list[_Hit]:
        if query == self.error_on:
            raise RuntimeError("检索失败")
        return [_Hit(chunk_id) for chunk_id in self.mapping.get(query, [])][:top_k]


class _Answerer:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers

    def ask(self, _tenant_id: UUID, question: str, *, top_k: int = 5) -> SimpleNamespace:
        del top_k
        return SimpleNamespace(answer=self.answers[question], sources=(object(),))


class _FakeEmbedder:
    model_name = EMBEDDING_MODEL_NAME
    def __init__(self) -> None:
        self.embed_calls = 0
        self.embed_query_calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls += 1
        return [[1.0, 0.0] if "付款申请须先由部门负责人" in text else [0.0, 1.0] for text in texts]

    def embed_query(self, query: str) -> list[float]:
        del query
        self.embed_query_calls += 1
        return [1.0, 0.0]


def _payload(cases: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "dataset_name": "unit",
        "passages": [
            {
                "chunk_id": CHUNK_A,
                "document_id": DOCUMENT,
                "title": "部门审批",
                "content": "付款申请须先由部门负责人审批。",
            },
            {
                "chunk_id": CHUNK_B,
                "document_id": DOCUMENT,
                "title": "出纳",
                "content": "出纳在两个工作日内完成付款。",
            },
            {
                "chunk_id": CHUNK_C,
                "document_id": DOCUMENT,
                "title": "其他",
                "content": "另一条无关资料。",
            },
        ],
        "cases": cases or [_case("single", "付款由谁审批？", [CHUNK_A])],
    }


def _case(
    case_id: str,
    question: str,
    chunk_ids: list[str],
    *,
    should_refuse: bool = False,
    facts: list[str] | None = None,
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "question": question,
        "expected_chunk_ids": chunk_ids,
        "expected_facts": ["部门负责人"] if facts is None else facts,
        "expected_answer": None,
        "should_refuse": should_refuse,
        "description": "单元样本",
    }
