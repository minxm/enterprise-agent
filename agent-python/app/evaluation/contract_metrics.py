"""Aggregate contract-query checks.

Each metric keeps its numerator, denominator, and skipped count. A skipped
case is outside that metric. An executed case that does not match counts as a
miss, not as a skip. Execution exceptions are misses where the metric applies,
and they are also listed separately so one failure does not hide the rest.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

FIELD_NAMES = (
    "contract_no",
    "title",
    "contract_type",
    "counterparty_name",
    "amount",
    "currency",
    "status",
    "version_no",
)


def score_tool_argument(
    invoked: bool,
    arguments: Sequence[str],
    extracted: Sequence[str],
    expected: Sequence[str],
) -> bool | None:
    """Score the argument the agent actually passed to the tool.

    A case that did not call the tool has no argument and is not applicable.
    The argument must be the number the agent extracted. A gold number is not
    substituted for a wrong extraction. A wrong extraction therefore fails even
    when the tool call itself returns.
    """
    if not invoked:
        return None
    if len(arguments) != 1 or tuple(extracted) != (arguments[0],):
        return False
    return tuple(expected) == (arguments[0],)


def ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def metric(
    name: str,
    definition: str,
    results: Sequence[object],
    applicable: Callable[[object], bool],
    passed: Callable[[object], bool],
) -> dict[str, object]:
    selected = [item for item in results if applicable(item)]
    skipped = len(results) - len(selected)
    numerator = sum(1 for item in selected if item.status == "success" and passed(item))
    return {
        "name": name,
        "definition": definition,
        "numerator": numerator,
        "denominator": len(selected),
        "skipped": skipped,
        "accuracy": ratio(numerator, len(selected)),
    }


def field_metrics(results: Sequence[object]) -> dict[str, object]:
    """Field accuracy uses only cases that expect a contract record.

    The denominator of each field is the number of record cases. A record case
    whose run failed counts in the denominator and not the numerator. Other
    cases are skipped.
    """
    record_cases = [item for item in results if item.answer_kind == "record"]
    skipped = len(results) - len(record_cases)
    per_field: dict[str, object] = {}
    for name in FIELD_NAMES:
        numerator = sum(
            1
            for item in record_cases
            if item.status == "success" and item.field_hits.get(name) is True
        )
        per_field[name] = {
            "numerator": numerator,
            "denominator": len(record_cases),
            "skipped": skipped,
            "accuracy": ratio(numerator, len(record_cases)),
        }
    all_numerator = sum(
        1 for item in record_cases if item.status == "success" and item.fields_pass
    )
    return {
        "name": "field_accuracy",
        "definition": (
            "分子是全部约定字段都与 Fixture 一致、且没有出现另一份合同特征的样本数。"
            "分母是 answer_kind 为 record 的样本数。不期待合同字段的样本计入 skipped。"
            "单个字段的分母与此相同。金额和币种必须同时出现在金额行。状态必须同时匹配状态码和中文标签。"
            "版本号同时核对版本序号和版本标签。此指标对照数据集 Fixture，不代表已经查询 PostgreSQL。"
        ),
        "numerator": all_numerator,
        "denominator": len(record_cases),
        "skipped": skipped,
        "accuracy": ratio(all_numerator, len(record_cases)),
        "per_field": per_field,
    }


def tool_outcome_metrics(results: Sequence[object]) -> dict[str, object]:
    """Split successful lookups, business rejections, and transport failures.

    Permission denial and a missing contract are business rejections. They are
    not counted as tool failures. Cases that should not call the tool are skipped.
    """
    attempted = [item for item in results if item.tool_invoked]
    skipped = len(results) - len(attempted)
    success = sum(1 for item in attempted if item.tool_outcome == "success")
    rejected = sum(1 for item in attempted if item.tool_outcome == "business_rejection")
    failed = sum(1 for item in attempted if item.tool_outcome == "failure")
    denominator = len(attempted)
    return {
        "name": "tool_success",
        "definition": (
            "分母是合同节点实际调用了 Contract Tool 的样本数。"
            "success 是工具返回合同记录。business_rejection 是未找到、无权限或编号不合法。"
            "failure 是未授权或服务不可用。无权限只计入 business_rejection。"
            "不应调用工具的样本计入 skipped，不进入分母。"
        ),
        "attempted": denominator,
        "success": success,
        "business_rejection": rejected,
        "failure": failed,
        "skipped": skipped,
        "success_rate": ratio(success, denominator),
        "business_rejection_rate": ratio(rejected, denominator),
        "failure_rate": ratio(failed, denominator),
    }


def routing_breakdown(results: Sequence[object]) -> dict[str, object]:
    groups: dict[str, object] = {}
    for route in ("contract_query", "knowledge_qa", "unsupported"):
        groups[route] = metric(
            f"routing_{route}",
            f"分母是期望路由为 {route} 的样本数。分子是 route_intent 结果一致的样本数。其余样本计入 skipped。",
            results,
            lambda item, expected=route: item.expected_route == expected,
            lambda item: item.route_pass,
        )
    groups["mixed"] = metric(
        "routing_mixed",
        "分母是场景为 mixed 的样本数。分子是进入合同查询、调用工具，并且答案包含检索到的制度原文的样本数。知识库检索质量不在此项计算。",
        results,
        lambda item: item.scenario == "mixed",
        lambda item: item.route_pass and item.tool_call_pass and item.policy_pass,
    )
    return groups
