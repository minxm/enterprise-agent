import json
from pathlib import Path

import httpx
import pytest

from app.agents.knowledge_qa.contract_node import _POLICY
from app.evaluation.contract_dataset import (
    POLICY_PASSAGE,
    ContractDatasetError,
    load_contract_dataset,
    parse_contract_dataset,
)
from app.evaluation.contract_evaluator import ContractEvaluator, report_to_dict
from app.evaluation.contract_metrics import score_tool_argument
from app.evaluation.contract_live import LiveConfig, parse_sse, run_live
from app.evaluation.dataset import load_dataset
from scripts.evaluate_contract import main

DATASET = "evaluation_data/contract_query_eval.json"
RAG_DATASET = "evaluation_data/enterprise_contract_eval.json"


def test_contract_dataset_covers_the_required_scenarios():
    dataset = load_contract_dataset(DATASET)

    assert dataset.dataset_name == "contract-query-agent-v1"
    assert len(dataset.cases) >= 30
    counts = {}
    for case in dataset.cases:
        counts[case.scenario] = counts.get(case.scenario, 0) + 1
    assert counts["intent"] >= 6
    assert counts["extraction"] >= 7
    assert counts["not_found"] >= 3
    assert counts["knowledge"] >= 4
    assert counts["mixed"] >= 3
    assert counts["authorization"] >= 3
    assert counts["exception"] >= 4
    assert counts["routing"] >= 2
    assert {item.contract_no for item in dataset.fixtures} == {"HT-2026-001", "CT-001"}
    for case in dataset.cases:
        if case.expect_policy:
            assert _POLICY.search(case.question)


def test_dataset_rejects_a_missing_field_wrong_type_and_fixture_mismatch():
    payload = json.loads(Path(DATASET).read_text(encoding="utf-8"))
    broken = json.loads(json.dumps(payload))
    del broken["cases"][0]["question"]
    with pytest.raises(ContractDatasetError, match="question"):
        parse_contract_dataset(broken)

    typed = json.loads(json.dumps(payload))
    typed["cases"][0]["expected_tool_call"] = "yes"
    with pytest.raises(ContractDatasetError, match="布尔值"):
        parse_contract_dataset(typed)

    actor = json.loads(json.dumps(payload))
    actor["cases"][0]["actor"] = "anonymous"
    with pytest.raises(ContractDatasetError, match="actor"):
        parse_contract_dataset(actor)

    mismatch = json.loads(json.dumps(payload))
    mismatch["cases"][0]["expected_fields"]["title"] = "另一份合同"
    with pytest.raises(ContractDatasetError, match="fixture"):
        parse_contract_dataset(mismatch)

    duplicate = json.loads(json.dumps(payload))
    duplicate["cases"][1]["case_id"] = duplicate["cases"][0]["case_id"]
    with pytest.raises(ContractDatasetError, match="case_id"):
        parse_contract_dataset(duplicate)


def test_offline_metrics_cover_routing_fields_permission_and_missing_contracts():
    report = ContractEvaluator().evaluate(load_contract_dataset(DATASET))
    metrics = report.metrics

    assert report.execution_failures == 0
    assert report.check_failures == 0
    assert report.passed_cases == report.total_cases
    assert report.mode == "offline-fixture"
    assert metrics["intent_accuracy"]["accuracy"] == 1
    assert metrics["intent_accuracy"]["denominator"] == report.total_cases
    assert metrics["intent_accuracy"]["skipped"] == 0
    extraction = metrics["contract_number_extraction_accuracy"]
    with_number = metrics["contract_number_extraction_with_number"]
    without_number = metrics["contract_number_extraction_without_number"]
    multi = metrics["contract_number_extraction_multi"]
    assert extraction["numerator"] == extraction["denominator"] == report.total_cases
    assert extraction["skipped"] == 0
    assert with_number["denominator"] + without_number["denominator"] == report.total_cases
    assert with_number["skipped"] == without_number["denominator"]
    assert without_number["skipped"] == with_number["denominator"]
    assert with_number["numerator"] == with_number["denominator"]
    assert without_number["numerator"] == without_number["denominator"]
    assert multi["denominator"] == 2
    assert multi["numerator"] == 2
    assert multi["skipped"] == report.total_cases - 2
    assert metrics["tool_call_accuracy"]["accuracy"] == 1
    assert metrics["tool_call_accuracy"]["skipped"] == 0
    tool = metrics["tool_success"]
    argument = metrics["tool_argument_accuracy"]
    assert argument["denominator"] == tool["attempted"]
    assert argument["numerator"] == tool["attempted"]
    assert argument["skipped"] == report.total_cases - tool["attempted"]
    assert metrics["authorization_rejection_accuracy"]["denominator"] == 1
    assert metrics["authorization_rejection_accuracy"]["numerator"] == 1
    assert metrics["cross_tenant_accuracy"]["numerator"] == 1
    assert metrics["not_found_accuracy"]["denominator"] == 7
    assert metrics["not_found_accuracy"]["numerator"] == 7
    assert metrics["field_accuracy"]["accuracy"] == 1
    assert metrics["field_accuracy"]["per_field"]["amount"]["accuracy"] == 1
    assert metrics["field_accuracy"]["per_field"]["status"]["accuracy"] == 1
    assert metrics["field_accuracy"]["per_field"]["version_no"]["accuracy"] == 1
    assert metrics["routing_accuracy"]["accuracy"] == 1
    assert metrics["routing_breakdown"]["knowledge_qa"]["denominator"] == 5
    assert metrics["routing_breakdown"]["mixed"]["numerator"] == 3
    assert metrics["routing_breakdown"]["unsupported"]["denominator"] == 1
    assert tool["success"] + tool["business_rejection"] + tool["failure"] == tool["attempted"]
    assert tool["business_rejection"] > tool["failure"]
    forbidden = next(item for item in report.per_case_results if item.case_id == "auth-forbidden")
    assert forbidden.answer == "无权查询该合同。"
    assert "年度采购合同" not in forbidden.answer
    missing = next(item for item in report.per_case_results if item.case_id == "missing-no-invention")
    assert missing.answer == "未找到该合同。"
    assert "南方公司" not in missing.answer
    no_ticket = next(item for item in report.per_case_results if item.case_id == "err-no-ticket")
    assert no_ticket.backend_calls == 0
    assert no_ticket.tool_outcome == "failure"
    assert no_ticket.tool_invoked is True
    consumed = next(item for item in report.per_case_results if item.case_id == "err-consumed-ticket")
    assert consumed.tool_outcome == "failure"
    assert consumed.error_code == "contract_unauthorized"
    explicit = next(item for item in report.per_case_results if item.case_id == "knowledge-explicit")
    assert explicit.predicted_intent == "knowledge_qa"
    assert explicit.tool_invoked is False
    assert explicit.predicted_numbers == ("HT-2026-001",)
    mixed = next(item for item in report.per_case_results if item.case_id == "mixed-policy")
    assert POLICY_PASSAGE in mixed.answer
    assert "模型回答不应进入合同事实" not in mixed.answer


def test_tool_argument_requires_the_extracted_number_and_ignores_a_substituted_gold_value():
    assert score_tool_argument(False, [], [], ["HT-2026-001"]) is None
    assert score_tool_argument(True, ["HT-2026-001"], ["HT-2026-001"], ["HT-2026-001"]) is True
    assert score_tool_argument(True, ["HT-2026-404"], ["HT-2026-404"], ["HT-2026-001"]) is False
    assert score_tool_argument(True, ["HT-2026-001"], ["HT-2026-404"], ["HT-2026-001"]) is False


def test_one_case_exception_does_not_stop_the_remaining_cases(monkeypatch):
    def explode(state):
        if state.get("question") == "报销制度有哪些要求？":
            raise RuntimeError("isolated")
        return original(state)

    original = __import__("app.evaluation.contract_evaluator", fromlist=["route_intent"]).route_intent
    monkeypatch.setattr("app.evaluation.contract_evaluator.route_intent", explode)
    report = ContractEvaluator().evaluate(load_contract_dataset(DATASET))

    failed = [item for item in report.per_case_results if item.status == "failed"]
    assert [item.case_id for item in failed] == ["knowledge-plain"]
    assert failed[0].error == "RuntimeError"
    assert report.execution_failures == 1
    assert report.passed_cases == report.total_cases - 1
    assert any(item.case_id == "intent-basic" and item.passed for item in report.per_case_results)


def test_cli_writes_a_json_report_without_the_ticket_or_a_live_claim(tmp_path):
    output = tmp_path / "contract-query.json"
    code = main(["--dataset", DATASET, "--output", str(output)])
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert code == 0
    assert payload["mode"] == "offline-fixture"
    assert payload["ground_truth"] == "fixture"
    assert payload["live_database"]["status"] == "not_executed"
    assert payload["passed_cases"] == payload["total_cases"]
    rendered = output.read_text(encoding="utf-8")
    assert "eval-ticket-placeholder" not in rendered
    assert "X-Actor-Ticket" not in rendered
    assert "psycopg" not in Path("app/evaluation/contract_evaluator.py").read_text(encoding="utf-8")
    assert "business." not in Path("app/evaluation/contract_live.py").read_text(encoding="utf-8")


def test_live_flag_without_credentials_stays_unexecuted(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTRACT_EVAL_JAVA_URL", raising=False)
    monkeypatch.delenv("CONTRACT_EVAL_BEARER", raising=False)
    output = tmp_path / "contract-live.json"
    code = main(["--dataset", DATASET, "--output", str(output), "--live"])
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert code == 0
    assert payload["live_database"]["status"] == "not_executed"
    assert payload["mode"] == "offline-fixture"


def test_mocked_java_stream_scores_eligible_cases_and_keeps_going_after_one_failure():
    dataset = load_contract_dataset(DATASET)
    selected = tuple(
        case for case in dataset.cases if case.case_id in {"intent-basic", "missing-ht", "intent-counterparty"}
    )
    scoped = type(dataset)(dataset.dataset_name, dataset.policy_passage, dataset.fixtures, selected)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "UP"})
        if request.url.path.endswith("/conversations"):
            return httpx.Response(200, json={"code": 200, "message": "ok", "data": {"conversationId": "c-1"}, "requestId": "r"})
        if "HT-2026-404" in request.content.decode("utf-8"):
            return httpx.Response(503, json={"code": 503, "message": "Agent 服务不可用"})
        answer = "\n".join([
            "合同编号：HT-2026-001",
            "标题：年度采购合同",
            "类型：采购",
            "相对方：南方公司",
            "金额：12800.50 CNY",
            "审批状态：待审核（pending_review）",
            "当前版本：V1",
            "版本序号：1",
        ])
        body = f"event: message\ndata: {json.dumps({'answer': answer, 'status': 'answered'}, ensure_ascii=False)}\n\n"
        return httpx.Response(200, text=body)

    report = run_live(scoped, LiveConfig("http://java.test", "token-not-printed"), transport=httpx.MockTransport(handler))

    assert report["ground_truth"] == "java_agent_sse"
    assert report["model_participated"] is False
    assert report["skipped_cases"] == 1
    assert report["passed_cases"] == 1
    assert report["failed_cases"] == 1
    assert "token-not-printed" not in json.dumps(report)
    by_id = {item["case_id"]: item for item in report["results"]}
    assert by_id["intent-counterparty"]["status"] == "not_applicable"
    assert by_id["intent-basic"]["status"] == "passed"
    assert by_id["intent-basic"]["predicted"]["answer"].startswith("合同编号：HT-2026-001")
    assert by_id["missing-ht"]["status"] == "failed"
    assert report["metrics"]["tool_argument_accuracy"]["denominator"] == 0


def test_sse_parser_reads_the_message_answer():
    body = "event: run_started\ndata: {\"status\": \"started\"}\n\nevent: message\ndata: {\"answer\": \"未找到该合同。\", \"status\": \"answered\"}\n\n"

    assert parse_sse(body)["answer"] == "未找到该合同。"


def test_existing_rag_dataset_still_loads():
    dataset = load_dataset(RAG_DATASET)

    assert dataset.dataset_name == "enterprise-contract-eval-v1"
    assert len(dataset.cases) == 12


def test_report_records_each_prediction(tmp_path):
    report = report_to_dict(ContractEvaluator().evaluate(load_contract_dataset(DATASET)))
    first = report["per_case_results"][0]

    assert first["predicted"]["intent"]
    assert "passed" in first
    assert report["metrics"]["field_accuracy"]["definition"]
    assert report["live_database"]["status"] == "not_executed"
