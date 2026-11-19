"""Live contract evaluation through the Java knowledge-QA entry.

Each case creates a new conversation. Java issues a fresh actor ticket for
that stream and revokes it when the stream ends. This module does not send a
ticket, a tenant id, or a user id, and it does not open PostgreSQL.

CT-001 and other fixture-only contracts are not sent to Java. Ticket replay
and mixed questions that would call a chat model are left unexecuted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from app.evaluation.contract_dataset import ContractEvalCase, ContractEvalDataset
from app.evaluation.contract_evaluator import _lines

_LIVE_NUMBER = "HT-2026-001"
_NOT_FOUND = "未找到该合同。"
_FORBIDDEN = "无权查询该合同。"


@dataclass(frozen=True)
class LiveConfig:
    base_url: str
    bearer: str
    timeout: float = 90.0
    no_permission_bearer: str = ""
    other_tenant_bearer: str = ""
    probe_unavailable: bool = False


def not_executed(reason: str) -> dict[str, object]:
    return _envelope(
        status="not_executed",
        reason=reason,
        ground_truth="not_executed",
        java_service="not_contacted",
        postgres_participated=False,
        total_cases=0,
        results=[],
    )


def run_live(dataset: ContractEvalDataset, config: LiveConfig, *, transport: httpx.BaseTransport | None = None) -> dict[str, object]:
    if not config.base_url.strip() or not config.bearer.strip():
        return not_executed("缺少 Java 地址或读取权限用户的登录凭据，未连接服务。")
    client = httpx.Client(base_url=config.base_url.rstrip("/"), timeout=config.timeout, transport=transport)
    results: list[dict[str, object]] = []
    try:
        java_service = _java_status(client)
        for case in dataset.cases:
            disposition, reason = _disposition(case, config.probe_unavailable)
            if disposition != "execute":
                results.append(_idle(case, disposition, reason))
                continue
            token = _token_for(case, config)
            if not token:
                results.append(_idle(case, "not_executed", "缺少该身份的登录凭据，未发起问答。"))
                continue
            results.append(_run_case(client, token, case))
    finally:
        client.close()
    return _envelope(
        status="executed",
        reason="通过 Java 问答入口执行。Java 再调用 Python Agent，Agent 经内部合同接口读取 PostgreSQL。评估进程不连接 business schema，也不调用聊天模型。",
        ground_truth="java_agent_sse",
        java_service=java_service,
        postgres_participated=True,
        total_cases=len(dataset.cases),
        results=results,
    )


def replace_result(report: dict[str, object], result: dict[str, object]) -> dict[str, object]:
    """Replace one live case and recompute totals. Scores stay inside this report."""
    results = []
    replaced = False
    current = report.get("results")
    if not isinstance(current, list):
        current = []
    for item in current:
        if isinstance(item, dict) and item.get("case_id") == result.get("case_id"):
            results.append(result)
            replaced = True
        else:
            results.append(item)
    if not replaced:
        results.append(result)
    rebuilt = _envelope(
        status="executed",
        reason=str(report.get("reason") or ""),
        ground_truth=str(report.get("ground_truth") or "java_agent_sse"),
        java_service=str(report.get("java_service") or "unknown"),
        postgres_participated=bool(report.get("postgres_participated")),
        total_cases=int(report.get("total_cases") or len(results)),
        results=results,
    )
    rebuilt["actor_ticket"] = report.get("actor_ticket")
    return rebuilt


def parse_sse(body: str) -> dict[str, str | None]:
    answer = None
    status = None
    error_code = None
    safe_error_message = None
    for block in body.split("\n\n"):
        event = None
        data = None
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        if not event or not data:
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if event == "message" and isinstance(payload.get("answer"), str):
            answer = payload["answer"]
            if isinstance(payload.get("status"), str):
                status = payload["status"]
        elif event == "run_completed" and isinstance(payload.get("status"), str):
            status = payload["status"]
        elif event == "run_failed":
            status = "failed"
            if isinstance(payload.get("error_code"), str):
                error_code = payload["error_code"]
            if isinstance(payload.get("safe_error_message"), str):
                safe_error_message = payload["safe_error_message"]
    return {
        "answer": answer,
        "status": status,
        "error_code": error_code,
        "safe_error_message": safe_error_message,
    }


def _disposition(case: ContractEvalCase, probe_unavailable: bool = False) -> tuple[str, str]:
    if case.expect_policy or case.answer_kind == "knowledge":
        return "not_applicable", "该样本会走知识检索。本次真实评估不调用聊天模型。"
    if case.answer_kind in {"unsupported", "missing_number", "ambiguous"}:
        return "not_applicable", "该样本只验证路由或提示，不读取真实合同。"
    if case.answer_kind == "unauthorized":
        return "not_executed", "每次问答由 Java 签发新票据并在结束时撤销。评估器不能提交或复用票据，因此缺票据和已消费票据不在这里重放。"
    if case.answer_kind == "unavailable":
        if probe_unavailable:
            return "execute", ""
        return "not_executed", "合同问答入口和合同接口属于同一个 Java 进程。主流程不停止 Java，也不把离线 Fixture 算进真实结果。"
    if case.answer_kind == "record" and case.expected_numbers != (_LIVE_NUMBER,):
        return "not_applicable", f"{case.expected_numbers[0] if case.expected_numbers else '该编号'} 不是当前测试库合同，不能用 Fixture 冒充。"
    if case.actor == "reader" and case.answer_kind in {"record", "not_found"}:
        return "execute", ""
    if case.actor == "no_permission" and case.answer_kind == "forbidden":
        return "execute", ""
    if case.actor == "other_tenant" and case.answer_kind == "not_found":
        return "execute", ""
    return "not_applicable", "没有对应的真实身份或合同。"


def _token_for(case: ContractEvalCase, config: LiveConfig) -> str:
    if case.actor == "no_permission":
        return config.no_permission_bearer.strip()
    if case.actor == "other_tenant":
        return config.other_tenant_bearer.strip()
    return config.bearer.strip()


def _run_case(client: httpx.Client, bearer: str, case: ContractEvalCase) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
    try:
        created = client.post("/api/v1/agent/conversations", headers=headers)
        conversation_id = _conversation_id(created)
        if conversation_id is None:
            return _finished(case, "failed", None, None, "未能创建会话")
        response = client.post(
            "/api/v1/agent/knowledge-qa/stream",
            headers={**headers, "Accept": "text/event-stream", "Content-Type": "application/json"},
            json={"conversationId": conversation_id, "question": case.question, "topK": 5, "retrievalStrategy": "vector"},
        )
    except httpx.HTTPError:
        return _finished(case, "failed", None, None, "Java 服务不可达")
    if response.status_code != 200:
        return _finished(case, "failed", response.status_code, parse_sse(response.text), "Java 返回了非成功状态")
    parsed = parse_sse(response.text)
    passed, reason = _judge(case, parsed)
    return _finished(case, "passed" if passed else "failed", response.status_code, parsed, None if passed else reason)


def _judge(case: ContractEvalCase, parsed: dict[str, str | None]) -> tuple[bool, str]:
    answer = parsed.get("answer") or ""
    if any(snippet and snippet in answer for snippet in case.forbidden_snippets):
        return False, "回答包含了不应出现的合同字段"
    if case.answer_kind == "not_found":
        return (answer == _NOT_FOUND, "未返回明确的未找到提示" if answer != _NOT_FOUND else "")
    if case.answer_kind == "forbidden":
        ok = answer == _FORBIDDEN
        return (ok, "无权限时未拒绝或泄露了字段" if not ok else "")
    if case.answer_kind == "unavailable":
        ok = parsed.get("error_code") == "contract_unavailable" and parsed.get("safe_error_message") == "合同服务暂时不可用"
        return (ok, "服务不可用时未返回安全失败" if not ok else "")
    _hits, ok = _field_hits(case.field_map(), answer)
    return (ok, "回答与真实合同字段不一致" if not ok else "")


def _field_hits(fields: dict[str, str], answer: str) -> tuple[dict[str, bool], bool]:
    lines = _lines(answer)
    amount_line = lines.get("金额")
    status_line = lines.get("审批状态")
    hits = {
        "contract_no": lines.get("合同编号") == fields.get("contract_no"),
        "title": lines.get("标题") == fields.get("title"),
        "contract_type": lines.get("类型") == fields.get("contract_type"),
        "counterparty_name": lines.get("相对方") == fields.get("counterparty_name"),
        "amount": amount_line == f"{fields.get('amount')} {fields.get('currency')}",
        "currency": amount_line == f"{fields.get('amount')} {fields.get('currency')}",
        "status": status_line == f"{fields.get('status_label')}（{fields.get('status')}）",
        "version_no": lines.get("版本序号") == fields.get("version_no") and lines.get("当前版本") == fields.get("version_label"),
    }
    return hits, all(hits.values())


def _idle(case: ContractEvalCase, status: str, reason: str) -> dict[str, object]:
    return {
        "case_id": case.case_id,
        "status": status,
        "reason": reason,
        "predicted": None,
        "expected": _expected(case),
    }


def _finished(
    case: ContractEvalCase,
    status: str,
    http_status: int | None,
    parsed: dict[str, str | None] | None,
    reason: str | None,
) -> dict[str, object]:
    predicted = None
    if parsed is not None or http_status is not None:
        predicted = {
            "http_status": http_status,
            "run_status": None if parsed is None else parsed.get("status"),
            "error_code": None if parsed is None else parsed.get("error_code"),
            "safe_error_message": None if parsed is None else parsed.get("safe_error_message"),
            "answer": None if parsed is None else parsed.get("answer"),
        }
    return {
        "case_id": case.case_id,
        "status": status,
        "reason": reason,
        "predicted": predicted,
        "expected": _expected(case),
    }


def _expected(case: ContractEvalCase) -> dict[str, object]:
    return {
        "answer_kind": case.answer_kind,
        "actor": case.actor,
        "numbers": list(case.expected_numbers),
        "fields": case.field_map(),
    }


def _envelope(
    *,
    status: str,
    reason: str,
    ground_truth: str,
    java_service: str,
    postgres_participated: bool,
    total_cases: int,
    results: list[dict[str, object]],
) -> dict[str, object]:
    executed = [item for item in results if item["status"] in {"passed", "failed"}]
    passed = [item for item in executed if item["status"] == "passed"]
    not_applicable = [item for item in results if item["status"] == "not_applicable"]
    not_run = [item for item in results if item["status"] == "not_executed"]
    return {
        "evaluation_mode": "live-java-agent",
        "status": status,
        "reason": reason,
        "ground_truth": ground_truth,
        "java_service": java_service,
        "postgres_participated": postgres_participated,
        "model_participated": False,
        "actor_ticket": "Java 为每次问答流单独签发，评估请求不携带、不记录、不复用票据。",
        "total_cases": total_cases,
        "executed_cases": len(executed),
        "passed_cases": len(passed),
        "failed_cases": len(executed) - len(passed),
        "skipped_cases": len(not_applicable) + len(not_run),
        "not_applicable_cases": len(not_applicable),
        "not_executed_cases": len(not_run),
        "metrics": _metrics(results),
        "results": results,
    }


def _metrics(results: list[dict[str, object]]) -> dict[str, object]:
    executed = [item for item in results if item["status"] in {"passed", "failed"}]

    def tally(kind: str) -> dict[str, object]:
        selected = [item for item in executed if _kind(item) == kind]
        skipped = len(results) - len(selected)
        numerator = sum(1 for item in selected if item["status"] == "passed")
        return {
            "numerator": numerator,
            "denominator": len(selected),
            "skipped": skipped,
            "accuracy": None if not selected else numerator / len(selected),
        }

    return {
        "field_accuracy": {
            **tally("record"),
            "definition": "分母是实际执行且期望返回合同记录的样本。分子是回答中的编号、标题、类型、相对方、金额、币种、状态和版本号都与该真实合同一致的样本。未执行和 Fixture 专有样本计入 skipped。",
        },
        "not_found_accuracy": {
            **tally("not_found"),
            "definition": "分母是实际执行且期望未找到的样本，包含另一租户。分子是回答恰好为“未找到该合同。”且不含被禁止字段的样本。",
        },
        "authorization_rejection_accuracy": {
            **tally("forbidden"),
            "definition": "分母是实际执行的无 contract:read 样本。分子是回答恰好为“无权查询该合同。”且不含合同字段的样本。",
        },
        "service_unavailable_accuracy": {
            **tally("unavailable"),
            "definition": "分母是实际执行的合同后端不可达样本。分子是 SSE 返回 contract_unavailable，且安全文案为“合同服务暂时不可用”。未做该探测的样本计入 skipped，不用 Fixture 填充分母。",
        },
        "cross_tenant_accuracy": {
            **_tally_actor(executed, results, "other_tenant"),
            "definition": "分母是实际执行的另一租户样本。分子是回答恰好为“未找到该合同。”且不含合同字段的样本。",
        },
        "tool_argument_accuracy": {
            "numerator": 0,
            "denominator": 0,
            "skipped": len(results),
            "accuracy": None,
            "definition": "问答 SSE 不返回工具参数。真实评估不从答案反推参数准确率，因此全部 skipped。参数准确率只在离线执行结果中计算。",
        },
    }


def _tally_actor(executed: list[dict[str, object]], results: list[dict[str, object]], actor: str) -> dict[str, object]:
    selected = [item for item in executed if _actor(item) == actor]
    numerator = sum(1 for item in selected if item["status"] == "passed")
    return {
        "numerator": numerator,
        "denominator": len(selected),
        "skipped": len(results) - len(selected),
        "accuracy": None if not selected else numerator / len(selected),
    }


def _actor(item: dict[str, object]) -> str:
    expected = item.get("expected")
    if not isinstance(expected, dict):
        return ""
    value = expected.get("actor")
    return value if isinstance(value, str) else ""


def _kind(item: dict[str, object]) -> str:
    expected = item.get("expected")
    if not isinstance(expected, dict):
        return ""
    value = expected.get("answer_kind")
    return value if isinstance(value, str) else ""


def _java_status(client: httpx.Client) -> str:
    try:
        response = client.get("/api/v1/integration/agent/health")
    except httpx.HTTPError:
        return "unreachable"
    if response.status_code == 200:
        return "up"
    return f"http_{response.status_code}"


def _conversation_id(response: httpx.Response) -> str | None:
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    value = data.get("conversationId") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return value
