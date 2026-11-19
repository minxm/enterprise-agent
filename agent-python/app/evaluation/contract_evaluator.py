"""Score contract routing and fixture-backed tool results.

The default path calls the existing intent functions and the contract node.
It does not open a database, call a chat model, or read the business schema.
Fixture records are the ground truth for this mode. They are not PostgreSQL
rows. One case exception is recorded and the remaining cases still run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from uuid import UUID, uuid4

from app.agents.knowledge_qa.contract_intent import (
    AMBIGUOUS_NUMBER,
    MISSING_NUMBER,
    extract_contract_numbers,
    looks_like_contract_query,
)
from app.agents.knowledge_qa.contract_node import contract_query
from app.agents.knowledge_qa.nodes import UNSUPPORTED_ANSWER, route_intent, unsupported
from app.clients.java_backend.client import ContractRecord, ContractVersionRecord, JavaBackendError
from app.evaluation.contract_dataset import ContractEvalCase, ContractEvalDataset, ContractFixture
from app.evaluation.contract_metrics import (
    field_metrics,
    metric,
    routing_breakdown,
    score_tool_argument,
    tool_outcome_metrics,
)
from app.knowledge.rag import RAGAnswer, RAGSource
from app.tools.contract_context import bind_actor_ticket, reset_actor_ticket
from app.tools.contract_query import ContractQueryTool

EVAL_TENANT_ID = UUID("00000000-0000-4000-8000-00000000e035")
_TICKET = "eval-ticket-placeholder-0123456789"
_DOC = UUID("00000000-0000-4000-8000-0000000000d5")
_CHUNK = UUID("00000000-0000-4000-8000-0000000000c5")
_BUSINESS_REJECTION = frozenset({"contract_not_found", "contract_forbidden", "invalid_contract_no"})
_TOOL_FAILURE = frozenset({"contract_unauthorized", "contract_unavailable"})


@dataclass
class CaseObservation:
    case_id: str
    scenario: str
    description: str
    question: str
    answer_kind: str
    expected_route: str
    expected_numbers: tuple[str, ...]
    status: str
    error: str | None
    predicted_intent: str | None
    predicted_numbers: tuple[str, ...]
    tool_invoked: bool
    tool_arguments: tuple[str, ...]
    backend_calls: int
    tool_outcome: str | None
    answer: str | None
    error_code: str | None
    intent_pass: bool
    numbers_pass: bool
    tool_call_pass: bool
    argument_pass: bool | None
    route_pass: bool
    authorization_pass: bool | None
    not_found_pass: bool | None
    fields_pass: bool | None
    field_hits: dict[str, bool] = field(default_factory=dict)
    policy_pass: bool | None = None
    leak_pass: bool | None = None
    outcome_pass: bool = True
    failure_reasons: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        if self.status != "success":
            return False
        flags = (
            self.intent_pass,
            self.numbers_pass,
            self.tool_call_pass,
            self.argument_pass,
            self.route_pass,
            self.authorization_pass,
            self.not_found_pass,
            self.fields_pass,
            self.policy_pass,
            self.leak_pass,
            self.outcome_pass,
        )
        return all(flag is not False for flag in flags)


class ContractEvaluator:
    def evaluate(self, dataset: ContractEvalDataset) -> "ContractEvaluationReport":
        started = perf_counter()
        results = tuple(self._evaluate_case(case, dataset) for case in dataset.cases)
        failures = tuple(
            {"case_id": item.case_id, "reason": item.error or "；".join(item.failure_reasons) or "未通过"}
            for item in results
            if not item.passed
        )
        return ContractEvaluationReport(
            evaluation_id=str(uuid4()),
            dataset_name=dataset.dataset_name,
            mode="offline-fixture",
            total_cases=len(results),
            passed_cases=sum(1 for item in results if item.passed),
            execution_failures=sum(1 for item in results if item.status == "failed"),
            check_failures=sum(1 for item in results if item.status == "success" and not item.passed),
            metrics=_metrics(results),
            per_case_results=results,
            failures=failures,
            live_database={
                "status": "not_executed",
                "reason": "离线评估不连接 Java，也不读取 business schema。字段对照的是数据集 Fixture。",
            },
            evaluation_time=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=perf_counter() - started,
        )

    def _evaluate_case(self, case: ContractEvalCase, dataset: ContractEvalDataset) -> CaseObservation:
        try:
            return _score_case(case, dataset)
        except Exception as exc:
            return CaseObservation(
                case_id=case.case_id,
                scenario=case.scenario,
                description=case.description,
                question=case.question,
                answer_kind=case.answer_kind,
                expected_route=case.expected_route,
                expected_numbers=case.expected_numbers,
                status="failed",
                error=exc.__class__.__name__,
                predicted_intent=None,
                predicted_numbers=(),
                tool_invoked=False,
                tool_arguments=(),
                backend_calls=0,
                tool_outcome=None,
                answer=None,
                error_code=None,
                intent_pass=False,
                numbers_pass=False,
                tool_call_pass=False,
                argument_pass=None,
                route_pass=False,
                authorization_pass=False if case.answer_kind == "forbidden" else None,
                not_found_pass=False if case.answer_kind == "not_found" else None,
                fields_pass=False if case.answer_kind == "record" else None,
                field_hits={},
                policy_pass=False if case.expect_policy else None,
                leak_pass=False if case.forbidden_snippets else None,
                outcome_pass=False,
                failure_reasons=(exc.__class__.__name__,),
            )


@dataclass(frozen=True)
class ContractEvaluationReport:
    evaluation_id: str
    dataset_name: str
    mode: str
    total_cases: int
    passed_cases: int
    execution_failures: int
    check_failures: int
    metrics: dict[str, object]
    per_case_results: tuple[CaseObservation, ...]
    failures: tuple[dict[str, str], ...]
    live_database: dict[str, str]
    evaluation_time: str
    elapsed_seconds: float


def report_to_dict(report: ContractEvaluationReport) -> dict[str, object]:
    payload = {
        "evaluation_id": report.evaluation_id,
        "dataset_name": report.dataset_name,
        "mode": report.mode,
        "total_cases": report.total_cases,
        "passed_cases": report.passed_cases,
        "execution_failures": report.execution_failures,
        "check_failures": report.check_failures,
        "metrics": report.metrics,
        "per_case_results": [_case_dict(item) for item in report.per_case_results],
        "failures": list(report.failures),
        "live_database": report.live_database,
        "evaluation_time": report.evaluation_time,
        "elapsed_seconds": report.elapsed_seconds,
        "ground_truth": "fixture",
        "java_service": "not_used",
        "postgres_participated": False,
        "model_participated": False,
    }
    rendered = str(payload)
    if _TICKET in rendered or "X-Actor-Ticket" in rendered:
        raise RuntimeError("评估报告包含票据")
    return payload


def _score_case(case: ContractEvalCase, dataset: ContractEvalDataset) -> CaseObservation:
    state = {
        "question": case.question,
        "requested_intent": case.requested_intent,
        "tenant_id": str(EVAL_TENANT_ID),
        "top_k": 5,
        "retrieval_strategy": "vector",
    }
    predicted_intent = str(route_intent(state)["intent"])
    numbers = tuple(extract_contract_numbers(case.question))
    calls: list[str] = []
    client = _FixtureClient(dataset, case.actor)
    answer = None
    error_code = None
    if predicted_intent == "contract_query":
        token = bind_actor_ticket(None if case.actor == "no_ticket" else _TICKET)
        try:
            result = contract_query(
                state,
                contract_tool=_RecordingTool(ContractQueryTool(client), calls),
                rag_service=_PassageRAG(dataset.policy_passage),
            )
        finally:
            reset_actor_ticket(token)
        answer = str(result.get("answer") or "")
        error_code = result.get("error_code") if isinstance(result.get("error_code"), str) else None
    elif predicted_intent == "unsupported":
        result = unsupported(state)
        answer = str(result.get("answer") or "")
        error_code = result.get("error_code") if isinstance(result.get("error_code"), str) else None
    tool_invoked = len(calls) > 0
    field_hits, fields_pass = _field_hits(case, answer)
    leak_pass = _leak_pass(case, answer)
    observation = CaseObservation(
        case_id=case.case_id,
        scenario=case.scenario,
        description=case.description,
        question=case.question,
        answer_kind=case.answer_kind,
        expected_route=case.expected_route,
        expected_numbers=case.expected_numbers,
        status="success",
        error=None,
        predicted_intent=predicted_intent,
        predicted_numbers=numbers,
        tool_invoked=tool_invoked,
        tool_arguments=tuple(calls),
        backend_calls=client.calls,
        tool_outcome=_tool_outcome(tool_invoked, error_code),
        answer=answer,
        error_code=error_code,
        intent_pass=predicted_intent == case.expected_intent,
        numbers_pass=numbers == case.expected_numbers,
        tool_call_pass=tool_invoked == case.expected_tool_call,
        argument_pass=score_tool_argument(tool_invoked, calls, numbers, case.expected_numbers),
        route_pass=predicted_intent == case.expected_route,
        authorization_pass=_authorization_pass(case, answer, error_code, leak_pass),
        not_found_pass=_not_found_pass(case, answer, error_code, leak_pass),
        fields_pass=fields_pass,
        field_hits=field_hits,
        policy_pass=_policy_pass(case, answer, dataset.policy_passage),
        leak_pass=leak_pass,
        outcome_pass=_outcome_pass(case, answer, error_code),
    )
    observation.failure_reasons = tuple(_reasons(case, observation))
    return observation


def _metrics(results: tuple[CaseObservation, ...]) -> dict[str, object]:
    return {
        "intent_accuracy": metric(
            "intent_accuracy",
            "分子是 route_intent 与 expected_intent 一致的样本数。分母是全部样本。执行异常计入分母且不算命中。没有跳过。",
            results,
            lambda _item: True,
            lambda item: item.intent_pass,
        ),
        "contract_number_extraction_accuracy": metric(
            "contract_number_extraction_accuracy",
            "分子是预测编号与期望编号完全一致的样本数。空列表、单个编号和多个编号都参加，多个编号还要求数量和顺序一致。分母是全部样本。没有不适用样本，skipped 为 0。执行异常计入分母且不算命中。",
            results,
            lambda _item: True,
            lambda item: item.numbers_pass,
        ),
        "contract_number_extraction_with_number": metric(
            "contract_number_extraction_with_number",
            "分子是期望里至少有一个编号、且预测列表与期望完全一致的样本数。分母是有编号样本。无编号样本不适用，计入 skipped。",
            results,
            lambda item: len(item.expected_numbers) > 0,
            lambda item: item.numbers_pass,
        ),
        "contract_number_extraction_without_number": metric(
            "contract_number_extraction_without_number",
            "分子是期望编号为空、且预测结果也是空列表的样本数。分母是无编号样本。有编号样本不适用，计入 skipped。",
            results,
            lambda item: len(item.expected_numbers) == 0,
            lambda item: item.numbers_pass and len(item.predicted_numbers) == 0,
        ),
        "contract_number_extraction_multi": metric(
            "contract_number_extraction_multi",
            "分子是期望编号多于一个、且预测的数量、顺序和内容都一致的样本数。分母只含多编号样本。单编号和空编号不适用，计入 skipped；它们已计入总体编号提取，以及有编号或无编号子集。",
            results,
            lambda item: len(item.expected_numbers) > 1,
            lambda item: item.numbers_pass,
        ),
        "tool_call_accuracy": metric(
            "tool_call_accuracy",
            "分子是实际是否调用 Contract Tool 与期望一致的样本数。只看 Agent 是否执行了调用，不看参数。分母是全部样本。skipped 为 0。",
            results,
            lambda _item: True,
            lambda item: item.tool_call_pass,
        ),
        "tool_argument_accuracy": metric(
            "tool_argument_accuracy",
            "分子是实际调用参数等于 Agent 提取出的编号，且该编号等于期望编号的样本数。提取错误时，即使工具返回了结果，也不算参数准确。评估器不会把期望编号填进调用。分母是实际发生了工具调用的样本。没有调用的样本不适用，计入 skipped。",
            results,
            lambda item: item.tool_invoked,
            lambda item: item.argument_pass is True,
        ),
        "tool_success": tool_outcome_metrics(results),
        "authorization_rejection_accuracy": metric(
            "authorization_rejection_accuracy",
            "分子是无 contract:read 时返回无权查询、且答案不含合同字段的样本数。分母是 answer_kind 为 forbidden 的样本。其他样本计入 skipped。另一租户不在此项。",
            results,
            lambda item: item.answer_kind == "forbidden",
            lambda item: item.authorization_pass is True,
        ),
        "cross_tenant_accuracy": metric(
            "cross_tenant_accuracy",
            "分子是另一租户查询返回未找到、且不泄露合同字段的样本数。分母是场景为 authorization 且答案类型为 not_found 的样本。",
            results,
            lambda item: item.scenario == "authorization" and item.answer_kind == "not_found",
            lambda item: item.not_found_pass is True,
        ),
        "field_accuracy": field_metrics(results),
        "not_found_accuracy": metric(
            "not_found_accuracy",
            "分子是答案恰好为“未找到该合同。”且不包含 Fixture 合同字段的样本数。分母是 answer_kind 为 not_found 的样本。其余计入 skipped。",
            results,
            lambda item: item.answer_kind == "not_found",
            lambda item: item.not_found_pass is True,
        ),
        "routing_accuracy": metric(
            "routing_accuracy",
            "分子是预测路由与 expected_route 一致的样本数。分母是全部样本。路由值只有 contract_query、knowledge_qa、unsupported。混合问题的细分见 routing_breakdown.mixed。",
            results,
            lambda _item: True,
            lambda item: item.route_pass,
        ),
        "routing_breakdown": routing_breakdown(results),
        "looks_like_contract_query_note": (
            "意图由现有 route_intent 决定。未指定 requested_intent 时，是否像合同查询由 looks_like_contract_query 判断。"
            f" 该函数在本数据集上的正例计数不单独作为分数。当前样本中判定为合同查询的问题数见 per_case_results。"
        ),
        "contract_query_signal_count": sum(
            1 for item in results if item.status == "success" and looks_like_contract_query(item.question)
        ),
        "fixture_limitations": [
            "字段准确率对照数据集 Fixture，不是这次运行读到的 PostgreSQL 行。",
            "CT-001 只存在于 Fixture，不能当作真实库里的合同。",
            "已消费票据在离线模式里模拟为授权失败，不验证票据存储的原子消费。",
        ],
    }


def _field_hits(case: ContractEvalCase, answer: str | None) -> tuple[dict[str, bool], bool | None]:
    if case.answer_kind != "record":
        return {}, None
    if answer is None:
        return {}, False
    lines = _lines(answer)
    fields = case.field_map()
    amount_line = lines.get("金额")
    status_line = lines.get("审批状态")
    hits = {
        "contract_no": lines.get("合同编号") == fields["contract_no"],
        "title": lines.get("标题") == fields["title"],
        "contract_type": lines.get("类型") == fields["contract_type"],
        "counterparty_name": lines.get("相对方") == fields["counterparty_name"],
        "amount": amount_line == f"{fields['amount']} {fields['currency']}",
        "currency": amount_line == f"{fields['amount']} {fields['currency']}",
        "status": status_line == f"{fields['status_label']}（{fields['status']}）",
        "version_no": lines.get("版本序号") == fields["version_no"] and lines.get("当前版本") == fields["version_label"],
    }
    leaked = any(snippet in answer for snippet in case.forbidden_snippets)
    return hits, all(hits.values()) and not leaked


def _authorization_pass(case: ContractEvalCase, answer: str | None, error_code: str | None, leak_pass: bool | None) -> bool | None:
    if case.answer_kind != "forbidden":
        return None
    return answer == "无权查询该合同。" and error_code == "contract_forbidden" and leak_pass is True


def _not_found_pass(case: ContractEvalCase, answer: str | None, error_code: str | None, leak_pass: bool | None) -> bool | None:
    if case.answer_kind != "not_found":
        return None
    return answer == "未找到该合同。" and error_code == "contract_not_found" and leak_pass is True


def _policy_pass(case: ContractEvalCase, answer: str | None, passage: str) -> bool | None:
    if case.answer_kind == "knowledge":
        return None
    if answer is None:
        return False
    present = passage in answer
    if case.expect_policy:
        return present
    return not present


def _outcome_pass(case: ContractEvalCase, answer: str | None, error_code: str | None) -> bool:
    if case.answer_kind == "missing_number":
        return answer == MISSING_NUMBER
    if case.answer_kind == "ambiguous":
        return answer == AMBIGUOUS_NUMBER
    if case.answer_kind == "unauthorized":
        return error_code == "contract_unauthorized" and answer == ""
    if case.answer_kind == "unavailable":
        return error_code == "contract_unavailable" and answer == ""
    if case.answer_kind == "unsupported":
        return answer == UNSUPPORTED_ANSWER
    if case.answer_kind == "knowledge":
        return answer is None
    return True


def _leak_pass(case: ContractEvalCase, answer: str | None) -> bool | None:
    if case.answer_kind == "knowledge":
        return None
    if answer is None:
        return False if case.forbidden_snippets else None
    if _TICKET in answer:
        return False
    if not case.forbidden_snippets:
        return True
    return all(snippet not in answer for snippet in case.forbidden_snippets)


def _tool_outcome(invoked: bool, error_code: str | None) -> str | None:
    if not invoked:
        return None
    if error_code is None:
        return "success"
    if error_code in _BUSINESS_REJECTION:
        return "business_rejection"
    if error_code in _TOOL_FAILURE:
        return "failure"
    return "failure"


def _reasons(case: ContractEvalCase, item: CaseObservation) -> list[str]:
    reasons: list[str] = []
    if not item.intent_pass:
        reasons.append(f"意图期望 {case.expected_intent}，实际 {item.predicted_intent}")
    if not item.numbers_pass:
        reasons.append(f"编号期望 {list(case.expected_numbers)}，实际 {list(item.predicted_numbers)}")
    if not item.tool_call_pass:
        reasons.append(f"工具调用期望 {case.expected_tool_call}，实际 {item.tool_invoked}")
    if item.argument_pass is False:
        reasons.append(
            f"工具参数期望 {list(case.expected_numbers)}，提取 {list(item.predicted_numbers)}，实际调用 {list(item.tool_arguments)}"
        )
    if item.authorization_pass is False:
        reasons.append("无权限拒绝不正确或泄露了合同字段")
    if item.not_found_pass is False:
        reasons.append("未找到结果不正确或编造了合同字段")
    if item.fields_pass is False:
        missed = [name for name, hit in item.field_hits.items() if not hit]
        reasons.append("字段不准确：" + "、".join(missed or ["泄漏"]))
    if item.policy_pass is False:
        reasons.append("制度原文缺失" if case.expect_policy else "制度原文不应出现")
    if item.leak_pass is False and item.answer_kind not in {"record", "not_found", "forbidden"}:
        reasons.append("答案包含了不应出现的合同字段")
    if not item.outcome_pass:
        reasons.append("工具结果与期望的答案类型不一致")
    return reasons


def _lines(answer: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in answer.splitlines():
        if "：" not in line:
            continue
        key, value = line.split("：", 1)
        found.setdefault(key, value)
    return found


def _case_dict(item: CaseObservation) -> dict[str, object]:
    return {
        "case_id": item.case_id,
        "scenario": item.scenario,
        "description": item.description,
        "question": item.question,
        "status": item.status,
        "passed": item.passed,
        "error": item.error,
        "failure_reasons": list(item.failure_reasons),
        "predicted": {
            "intent": item.predicted_intent,
            "numbers": list(item.predicted_numbers),
            "tool_call": item.tool_invoked,
            "tool_arguments": list(item.tool_arguments),
            "backend_calls": item.backend_calls,
            "tool_outcome": item.tool_outcome,
            "error_code": item.error_code,
            "answer": item.answer,
        },
        "expected": {
            "intent": item.expected_route,
            "route": item.expected_route,
            "numbers": list(item.expected_numbers),
            "answer_kind": item.answer_kind,
        },
        "checks": {
            "intent": item.intent_pass,
            "numbers": item.numbers_pass,
            "tool_call": item.tool_call_pass,
            "tool_argument": item.argument_pass,
            "route": item.route_pass,
            "authorization": item.authorization_pass,
            "not_found": item.not_found_pass,
            "fields": item.fields_pass,
            "field_hits": item.field_hits,
            "policy": item.policy_pass,
            "leak": item.leak_pass,
            "outcome": item.outcome_pass,
        },
    }


class _RecordingTool:
    def __init__(self, inner: ContractQueryTool, calls: list[str]) -> None:
        self._inner = inner
        self._calls = calls

    def query(self, contract_no: object) -> object:
        self._calls.append(contract_no if isinstance(contract_no, str) else "")
        return self._inner.query(contract_no)


class _FixtureClient:
    """In-memory stand-in for the Java client. It has no database connection."""

    def __init__(self, dataset: ContractEvalDataset, actor: str | None) -> None:
        self._records = {item.contract_no: _record(item) for item in dataset.fixtures}
        self._actor = actor
        self.calls = 0

    def get_contract_by_number(self, contract_no: str, ticket: str) -> ContractRecord:
        self.calls += 1
        if ticket == "" or _TICKET not in ticket:
            raise JavaBackendError("unauthorized", "合同查询未授权")
        if self._actor == "java_down":
            raise JavaBackendError("unavailable", "合同服务暂时不可用")
        if self._actor in {"invalid_ticket", "consumed_ticket"}:
            raise JavaBackendError("unauthorized", "合同查询未授权")
        if self._actor == "no_permission":
            raise JavaBackendError("forbidden", "无权查询合同")
        if self._actor == "other_tenant":
            raise JavaBackendError("not_found", "合同不存在")
        record = self._records.get(contract_no)
        if record is None:
            raise JavaBackendError("not_found", "合同不存在")
        return record


class _PassageRAG:
    """Returns one fixed passage. This is not a retrieval evaluation."""

    def __init__(self, passage: str) -> None:
        self._passage = passage

    def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None) -> RAGAnswer:
        del tenant_id, question, top_k, strategy
        source = RAGSource(_DOC, _CHUNK, 0, self._passage, None, None, 0, len(self._passage))
        return RAGAnswer(answer="模型回答不应进入合同事实", sources=(source,), retrieval_count=1, model="offline-fixture")


def _record(fixture: ContractFixture) -> ContractRecord:
    return ContractRecord(
        contract_no=fixture.contract_no,
        title=fixture.title,
        contract_type=fixture.contract_type,
        counterparty_name=fixture.counterparty_name,
        amount=fixture.amount,
        currency=fixture.currency,
        sign_date=fixture.sign_date,
        effective_date=fixture.effective_date,
        expiry_date=fixture.expiry_date,
        status=fixture.status,
        status_label=fixture.status_label,
        description=fixture.description,
        current_version=ContractVersionRecord(
            version_no=fixture.version_no,
            version_label=fixture.version_label,
            change_summary=fixture.change_summary,
            filename=fixture.filename,
            created_at=fixture.created_at,
        ),
    )
