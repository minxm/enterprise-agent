import json

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import EMBEDDING_MODEL_NAME, Settings
from app.evaluation.answer_quality import assess_answer, load_calibration, score_calibration
from app.evaluation.answers import is_refusal
from app.evaluation.claim_extractor import ClaimExtractionError, extract_claims
from app.evaluation.dataset import EvaluationDatasetError, load_dataset, parse_dataset
from app.evaluation.faithfulness import validate_judgement
from app.evaluation.judge_client import JudgeAPIError, JudgeClient, JudgeConfigurationError, JudgeResponseError
from scripts.evaluate_answer_quality import main

DATASET = "evaluation_data/enterprise_contract_eval.json"
CALIBRATION = "evaluation_data/answer_quality_calibration.json"
INVOICE = "00000000-0000-4000-8000-000000000008"
GENERAL = "00000000-0000-4000-8000-000000000003"
ADMIN = "00000000-0000-4000-8000-000000000009"
BUSINESS = "00000000-0000-4000-8000-00000000000a"
PAY = "00000000-0000-4000-8000-000000000001"
SUPPLIER = "00000000-0000-4000-8000-00000000000b"
LEAVE = "00000000-0000-4000-8000-000000000007"
GOLD_CHUNKS = {
    "single-pay-first-step": [PAY],
    "multi-pay-flow": [PAY, "00000000-0000-4000-8000-000000000002"],
    "refuse-cafeteria": [],
    "confuse-contract-threshold": ["00000000-0000-4000-8000-000000000006"],
    "paraphrase-pay-first": [PAY],
    "multi-condition-large-payment": [GENERAL, INVOICE, SUPPLIER],
    "conflict-seal": [ADMIN, BUSINESS],
    "single-legal-duty": ["00000000-0000-4000-8000-000000000004"],
    "multi-contract-sign": ["00000000-0000-4000-8000-000000000004", "00000000-0000-4000-8000-000000000005"],
    "refuse-shuttle": [],
    "paraphrase-legal": ["00000000-0000-4000-8000-000000000004"],
    "single-leave-days": [LEAVE],
}


class Judge:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls = 0

    def complete_json(self, *, system: str, user: str) -> dict[str, object]:
        del system, user
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        if not isinstance(self.payload, dict):
            raise JudgeResponseError("Judge 输出不是合法 JSON")
        return self.payload


def test_dataset_annotations_keep_the_original_gold():
    dataset = load_dataset(DATASET)

    assert len(dataset.cases) == 12
    for case in dataset.cases:
        assert list(case.expected_chunk_ids) == GOLD_CHUNKS[case.case_id]
        assert case.expected_response_type in {"answer", "refusal", "conflict"}
        if case.case_id == "conflict-seal":
            assert case.expected_conflict is True
            assert case.should_refuse is False
        if case.should_refuse:
            assert case.expected_response_type == "refusal"
            assert case.expected_evidence == ()
            assert case.expected_refusal_reason
    payload = {"dataset_name": "unit", "passages": [], "cases": []}
    del payload
    broken = _minimal_payload()
    broken["cases"][0]["unexpected"] = True
    with pytest.raises(EvaluationDatasetError, match="未知字段"):
        parse_dataset(broken)


def test_response_classes_do_not_treat_a_phrase_as_the_decision():
    refusal = assess_answer(
        question="公司班车早上几点发车？",
        answer="当前提供的知识片段中没有关于公司班车发车时间的信息，因此无法回答。",
        sources=[{"source_id": LEAVE, "content": "员工入职满一年后，每年可休年假 5 天。"}],
        expected_response_type="refusal",
    )
    assert refusal["deterministic"]["response_outcome"] == "correct_refusal"
    assert refusal["deterministic"]["phrase_refusal_hint"] is False
    assert is_refusal("当前提供的知识片段中没有关于公司班车发车时间的信息，因此无法回答。") is False

    wrong_refusal = assess_answer(
        question="入职满一年可以休几天年假？",
        answer="当前知识库资料不足，无法回答。",
        sources=[{"source_id": LEAVE, "content": "员工入职满一年后，每年可休年假 5 天。"}],
        expected_response_type="answer",
    )
    assert wrong_refusal["deterministic"]["response_outcome"] == "incorrect_refusal"

    answered = assess_answer(
        question="员工食堂每月补贴多少钱？",
        answer="员工食堂每月补贴 300 元。[1]",
        sources=[{"source_id": LEAVE, "content": "员工入职满一年后，每年可休年假 5 天。"}],
        expected_response_type="refusal",
    )
    assert answered["deterministic"]["response_outcome"] == "answered_when_should_refuse"

    hedged = assess_answer(
        question="付款发票什么时候提供？",
        answer="当前知识库资料不足。发票可以在付款后五个工作日内补交。[1]",
        sources=[{"source_id": INVOICE, "content": "付款前必须取得增值税专用发票，发票抬头为公司全称。"}],
        expected_response_type="answer",
    )
    assert hedged["deterministic"]["response_outcome"] == "unsupported_conclusion_with_insufficiency"
    assert hedged["deterministic"]["response_class"] != "refusal"

    partial = assess_answer(
        question="超过 50 万元付给新供应商时，审批和食堂补贴分别是什么？",
        answer="单笔付款金额超过 50 万元时，必须再由总经理审批。[1]。食堂补贴没有具体说明。",
        sources=[{"source_id": GENERAL, "content": "单笔付款金额超过 50 万元时，必须再由总经理审批。"}],
        expected_response_type="partial_answer",
        expected_evidence=[GENERAL],
    )
    assert partial["deterministic"]["response_class"] == "partial_answer"
    assert partial["deterministic"]["expected_evidence_coverage"] == 1.0

    normal = assess_answer(
        question="付款申请的第一步由谁审批？",
        answer="付款申请须先由部门负责人审批，审批通过后才能提交财务。[1]",
        sources=[{"source_id": PAY, "content": "付款申请须先由部门负责人审批，审批通过后才能提交财务。"}],
        expected_response_type="answer",
        expected_facts=["部门负责人"],
    )
    assert normal["deterministic"]["response_outcome"] == "answered"
    assert normal["deterministic"]["fact_keyword_coverage"] == 1.0
    assert normal["judge"]["status"] == "not_evaluated"
    assert normal["judge"]["overall_faithfulness"] is None


def test_claims_keep_conditions_and_fail_closed():
    claims = extract_claims("金额超过 50 万元的付款需要财务负责人审批，并且必须在付款前取得增值税专用发票。")

    assert len(claims) == 1
    assert "财务负责人" in claims[0].text
    assert "增值税专用发票" in claims[0].text
    assert claims[0].sentence_index == 0
    assert claims[0].kind == "fact"
    with pytest.raises(ClaimExtractionError):
        extract_claims(None)  # type: ignore[arg-type]
    failed = assess_answer(question="问题", answer=None, sources=[], judge=Judge({}))
    assert failed["claim_extraction"]["status"] == "failed"
    assert failed["judge"]["overall_faithfulness"] is None
    assert failed["judge"]["status"] == "not_evaluated"


def test_citation_support_is_separate_from_validity_and_conflict_is_not_chosen():
    answer = "付款后可以补交发票。[1]"
    sources = [{"source_id": INVOICE, "content": "付款前必须取得增值税专用发票，发票抬头为公司全称。"}]
    claims = extract_claims(answer)
    contradicted = assess_answer(
        question="付款发票什么时候提供？",
        answer=answer,
        sources=sources,
        expected_response_type="answer",
        judge=Judge(_judgement(claims, "contradicted", [], [INVOICE], "来源要求付款前取得发票。")),
    )
    assert contradicted["deterministic"]["citation_validity"] == 1.0
    assert contradicted["judge"]["citation_support_rate"] == 0.0
    assert contradicted["judge"]["contradicted_claim_rate"] == 1.0
    assert contradicted["judge"]["overall_faithfulness"] == 0.0

    missing = assess_answer(
        question="付款发票什么时候提供？",
        answer="见[9]。",
        sources=sources,
        expected_response_type="answer",
    )
    assert missing["deterministic"]["citation_validity"] == 0.0

    joint = extract_claims("付款申请须先由部门负责人审批，并且向新供应商付款前必须先完成供应商准入登记。[1][2]")
    supported = assess_answer(
        question="向新供应商付款前要做什么？",
        answer="付款申请须先由部门负责人审批，并且向新供应商付款前必须先完成供应商准入登记。[1][2]",
        sources=[
            {"source_id": PAY, "content": "付款申请须先由部门负责人审批，审批通过后才能提交财务。"},
            {"source_id": SUPPLIER, "content": "向新供应商付款前，必须先完成供应商准入登记。"},
        ],
        judge=Judge(_judgement(joint, "supported", [PAY, SUPPLIER], [], "两份片段合在一起写出了两个条件。")),
    )
    assert supported["judge"]["citation_support_rate"] == 1.0
    assert supported["judge"]["citation_completeness"] == 1.0
    assert len(joint) == 1

    conflict_answer = "合同盖章由行政部统一办理。[1]。合同盖章由业务部门负责人直接办理。[2]。两份资料存在冲突。"
    conflict_claims = extract_claims(conflict_answer)
    conflict = assess_answer(
        question="合同最终由谁盖章？",
        answer=conflict_answer,
        sources=[
            {"source_id": ADMIN, "content": "合同盖章由行政部统一办理，业务部门不得自行盖章。"},
            {"source_id": BUSINESS, "content": "合同盖章由业务部门负责人在审批完成后直接办理。"},
        ],
        expected_response_type="conflict",
        judge=Judge(_per_claim(conflict_claims, [
            ("insufficient", [ADMIN], [BUSINESS], "两份片段指定的办理部门不同。"),
            ("insufficient", [ADMIN], [BUSINESS], "不能选择其中一份作为唯一事实。"),
            ("irrelevant", [], [], "这是冲突说明。"),
        ], identifies_conflict=True, response_class="conflict")),
    )
    assert conflict["deterministic"]["response_class"] == "conflict"
    assert conflict["judge"]["supported_claim_rate"] == 0.0
    assert conflict["judge"]["insufficient_claim_rate"] == 1.0
    assert conflict["judge"]["identifies_conflict"] is True

    invalid = assess_answer(
        question="合同最终由谁盖章？",
        answer="合同盖章由行政部统一办理。[1]",
        sources=[{"source_id": ADMIN, "content": "合同盖章由行政部统一办理，业务部门不得自行盖章。"}],
        judge=Judge(_judgement(extract_claims("合同盖章由行政部统一办理。[1]"), "supported", [ADMIN, BUSINESS], [BUSINESS], "选了一边。")),
    )
    assert invalid["judge"]["status"] == "failed"
    assert invalid["judge"]["overall_faithfulness"] is None


def test_judge_failures_stay_null_and_do_not_invent_scores():
    answer = "付款申请须先由部门负责人审批。[1]"
    sources = [{"source_id": PAY, "content": "付款申请须先由部门负责人审批，审批通过后才能提交财务。"}]
    for error in (JudgeResponseError("Judge 输出不是合法 JSON"), JudgeAPIError("Judge 请求超时"), JudgeAPIError("Judge 请求失败，HTTP 503")):
        judge = Judge(error)
        result = assess_answer(question="谁审批？", answer=answer, sources=sources, judge=judge)
        assert judge.calls == 1
        assert result["judge"]["status"] == "failed"
        assert result["judge"]["overall_faithfulness"] is None
        assert result["judge"]["supported_claim_rate"] is None
        assert "sk-" not in str(result["judge"]["error"])

    bad_source = assess_answer(
        question="谁审批？",
        answer=answer,
        sources=sources,
        judge=Judge(_judgement(extract_claims(answer), "supported", ["missing-source"], [], "编号不存在。")),
    )
    assert bad_source["judge"]["status"] == "failed"
    assert bad_source["judge"]["overall_faithfulness"] is None

    suggestion = "建议向行政部门咨询。"
    no_facts = assess_answer(
        question="谁盖章？",
        answer=suggestion,
        sources=sources,
        judge=Judge(_judgement(extract_claims(suggestion), "irrelevant", [], [], "这是建议。")),
    )
    assert no_facts["judge"]["status"] == "success"
    assert no_facts["judge"]["overall_faithfulness"] is None
    assert no_facts["judge"]["citation_support_rate"] is None


def test_calibration_is_preliminary_and_judge_can_disagree(tmp_path):
    payload = load_calibration(CALIBRATION)
    baseline = score_calibration(payload)
    assert baseline["preliminary"] is True
    assert baseline["sample_size"] == 10
    assert baseline["deterministic_agreement"] == 1.0
    assert baseline["judge_status"] == "not_evaluated"
    assert baseline["judge_agreement"] is None

    echoed = score_calibration(payload, judge=_EchoJudge(payload))
    assert echoed["judge_agreement"] == 1.0
    assert echoed["judge_claim_agreement"] == 1.0
    disagree = score_calibration(payload, judge=_EchoJudge(payload, force_refusal=True))
    assert disagree["judge_agreement"] is not None
    assert disagree["judge_agreement"] < 1.0
    assert disagree["disagreements"]
    assert all(item.get("human_reason") for item in disagree["disagreements"])

    code = main(["--output", str(tmp_path / "out.json"), "--answers", str(tmp_path / "missing.json"), "--dataset", DATASET, "--calibration", CALIBRATION])
    assert code == 0
    report = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert report["judge_enabled"] is False
    assert report["calibration"]["judge_agreement"] is None
    assert main(["--dataset", str(tmp_path / "missing.json"), "--output", str(tmp_path / "bad.json")]) == 2
    with pytest.raises(ValidationError, match="RAG_EVAL_JUDGE_MODEL"):
        Settings(rag_eval_judge_model=EMBEDDING_MODEL_NAME)
    with pytest.raises(JudgeConfigurationError, match="未开启"):
        JudgeClient.from_settings(Settings(rag_eval_judge_enabled=False))


def test_judge_client_rejects_bad_json_and_timeout_without_the_key():
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    client = JudgeClient(api_key="sk-test-secret", model="Qwen/Qwen2.5-7B-Instruct", client=httpx.Client(transport=httpx.MockTransport(timeout)), sleeper=lambda _seconds: None)
    with pytest.raises(JudgeAPIError, match="超时") as caught:
        client.complete_json(system="只输出 JSON", user="判断")
    assert "sk-test-secret" not in str(caught.value)

    def bad_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "Qwen/Qwen2.5-7B-Instruct", "choices": [{"message": {"content": "不是 JSON"}}]})

    client = JudgeClient(api_key="sk-test-secret", model="Qwen/Qwen2.5-7B-Instruct", client=httpx.Client(transport=httpx.MockTransport(bad_json)))
    with pytest.raises(JudgeResponseError, match="JSON"):
        client.complete_json(system="只输出 JSON", user="判断")
    with pytest.raises(JudgeResponseError, match="source_id"):
        validate_judgement(
            _judgement(extract_claims("付款申请须先由部门负责人审批。"), "supported", ["missing"], [], "不存在。"),
            extract_claims("付款申请须先由部门负责人审批。"),
            (),
        )


class _EchoJudge:
    def __init__(self, payload: dict[str, object], *, force_refusal: bool = False) -> None:
        self.payload = payload
        self.force_refusal = force_refusal

    def complete_json(self, *, system: str, user: str) -> dict[str, object]:
        del system
        item = next(row for row in self.payload["items"] if isinstance(row, dict) and str(row["answer"]) in user)
        claims = extract_claims(str(item["answer"]))
        human = item["human_claims"] if isinstance(item.get("human_claims"), list) else []
        judged = []
        for claim in claims:
            match = next((row for row in human if isinstance(row, dict) and _same(str(row.get("text")), claim.text)), None)
            if isinstance(match, dict):
                judged.append(
                    {
                        "claim_id": claim.claim_id,
                        "verdict": match["verdict"],
                        "supporting_source_ids": list(match["supporting_source_ids"]),
                        "contradicting_source_ids": list(match["contradicting_source_ids"]),
                        "explanation": "与人工标注一致。",
                    }
                )
            else:
                judged.append(
                    {
                        "claim_id": claim.claim_id,
                        "verdict": "irrelevant",
                        "supporting_source_ids": [],
                        "contradicting_source_ids": [],
                        "explanation": "不是待核对的事实。",
                    }
                )
        response_class = "refusal" if self.force_refusal else item["human_response_class"]
        unsupported = response_class == "answered_despite_insufficiency"
        return {
            "expresses_insufficient_information": response_class in {"refusal", "partial_answer", "answered_despite_insufficiency"},
            "provides_unsupported_conclusion": unsupported,
            "identifies_conflict": response_class == "conflict",
            "answers_supported_part": response_class in {"answer", "partial_answer", "conflict"},
            "response_class": response_class,
            "claims": judged,
        }


def _judgement(claims, verdict: str, supporting: list[str], contradicting: list[str], explanation: str, *, response_class: str = "answer") -> dict[str, object]:
    return _per_claim(claims, [(verdict, supporting, contradicting, explanation)], response_class=response_class)


def _per_claim(claims, rows: list[tuple[str, list[str], list[str], str]], *, identifies_conflict: bool = False, response_class: str = "answer") -> dict[str, object]:
    return {
        "expresses_insufficient_information": False,
        "provides_unsupported_conclusion": False,
        "identifies_conflict": identifies_conflict,
        "answers_supported_part": True,
        "response_class": response_class,
        "claims": [
            {
                "claim_id": claim.claim_id,
                "verdict": verdict,
                "supporting_source_ids": supporting,
                "contradicting_source_ids": contradicting,
                "explanation": explanation,
            }
            for claim, (verdict, supporting, contradicting, explanation) in zip(claims, rows, strict=True)
        ],
    }


def _same(left: str, right: str) -> bool:
    normalized_left = "".join(left.split())
    normalized_right = "".join(right.split())
    return normalized_left in normalized_right or normalized_right in normalized_left


def _minimal_payload() -> dict[str, object]:
    chunk = "00000000-0000-4000-8000-000000000001"
    document = "00000000-0000-4000-8000-0000000000a1"
    return {
        "dataset_name": "unit",
        "passages": [{"chunk_id": chunk, "document_id": document, "title": "审批", "content": "付款申请须先由部门负责人审批。"}],
        "cases": [
            {
                "case_id": "one",
                "question": "谁审批？",
                "expected_chunk_ids": [chunk],
                "expected_facts": ["部门负责人"],
                "expected_answer": None,
                "should_refuse": False,
                "description": "单元样本",
            }
        ],
    }

