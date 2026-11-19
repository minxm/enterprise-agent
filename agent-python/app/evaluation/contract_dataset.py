"""Load the contract-query evaluation dataset.

The file is synthetic. Loading it does not open PostgreSQL, call Java, or
read the business schema. It is separate from the RAG passage dataset because
those cases require chunk ids and expected facts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.evaluation.dataset import EvaluationDatasetError

POLICY_PASSAGE = "合成制度：付款超过十万元需要总经理审批。"
INTENTS = frozenset({"contract_query", "knowledge_qa", "unsupported"})
SCENARIOS = frozenset(
    {"intent", "extraction", "not_found", "knowledge", "mixed", "authorization", "exception", "routing"}
)
ANSWER_KINDS = frozenset(
    {
        "record",
        "not_found",
        "forbidden",
        "unavailable",
        "unauthorized",
        "missing_number",
        "ambiguous",
        "unsupported",
        "knowledge",
    }
)
ACTORS = frozenset(
    {
        "reader",
        "no_permission",
        "other_tenant",
        "java_down",
        "no_ticket",
        "invalid_ticket",
        "consumed_ticket",
    }
)
FIELD_KEYS = (
    "contract_no",
    "title",
    "contract_type",
    "counterparty_name",
    "amount",
    "currency",
    "status",
    "status_label",
    "version_no",
    "version_label",
)
_ROOT_FIELDS = frozenset({"dataset_name", "policy_passage", "fixtures", "cases"})
_FIXTURE_FIELDS = frozenset(
    {
        "contract_no",
        "title",
        "contract_type",
        "counterparty_name",
        "amount",
        "currency",
        "sign_date",
        "effective_date",
        "expiry_date",
        "status",
        "status_label",
        "description",
        "current_version",
    }
)
_VERSION_FIELDS = frozenset({"version_no", "version_label", "change_summary", "filename", "created_at"})
_CASE_FIELDS = frozenset(
    {
        "case_id",
        "scenario",
        "description",
        "question",
        "requested_intent",
        "expected_intent",
        "expected_route",
        "expected_numbers",
        "expected_tool_call",
        "answer_kind",
        "actor",
        "expected_fields",
        "forbidden_snippets",
        "expect_policy",
    }
)


class ContractDatasetError(EvaluationDatasetError):
    """The contract evaluation dataset is missing a field or contradicts itself."""


@dataclass(frozen=True)
class ContractFixture:
    contract_no: str
    title: str
    contract_type: str
    counterparty_name: str
    amount: str
    currency: str
    sign_date: str | None
    effective_date: str | None
    expiry_date: str | None
    status: str
    status_label: str
    description: str | None
    version_no: int
    version_label: str
    change_summary: str
    filename: str
    created_at: str | None


@dataclass(frozen=True)
class ContractEvalCase:
    case_id: str
    scenario: str
    description: str
    question: str
    requested_intent: str | None
    expected_intent: str
    expected_route: str
    expected_numbers: tuple[str, ...]
    expected_tool_call: bool
    answer_kind: str
    actor: str | None
    expected_fields: tuple[tuple[str, str], ...]
    forbidden_snippets: tuple[str, ...]
    expect_policy: bool

    def field_map(self) -> dict[str, str]:
        return dict(self.expected_fields)


@dataclass(frozen=True)
class ContractEvalDataset:
    dataset_name: str
    policy_passage: str
    fixtures: tuple[ContractFixture, ...]
    cases: tuple[ContractEvalCase, ...]

    def fixture_map(self) -> dict[str, ContractFixture]:
        return {item.contract_no: item for item in self.fixtures}


def load_contract_dataset(path: Path | str) -> ContractEvalDataset:
    source = Path(path)
    if not source.is_file():
        raise ContractDatasetError(f"评估数据集不存在：{source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractDatasetError("评估数据集不是合法 JSON") from exc
    return parse_contract_dataset(payload)


def parse_contract_dataset(payload: object) -> ContractEvalDataset:
    if not isinstance(payload, dict):
        raise ContractDatasetError("评估数据集必须是 JSON 对象")
    _exact(payload, _ROOT_FIELDS, "数据集")
    name = _text(payload.get("dataset_name"), "dataset_name")
    passage = _text(payload.get("policy_passage"), "policy_passage")
    if passage != POLICY_PASSAGE:
        raise ContractDatasetError("policy_passage 必须使用评估器约定的合成制度原文")
    fixtures_raw = payload.get("fixtures")
    cases_raw = payload.get("cases")
    if not isinstance(fixtures_raw, list) or not fixtures_raw:
        raise ContractDatasetError("fixtures 必须是非空数组")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise ContractDatasetError("cases 必须是非空数组")
    fixtures = tuple(_fixture(item, index) for index, item in enumerate(fixtures_raw))
    numbers = [item.contract_no for item in fixtures]
    if len(numbers) != len(set(numbers)):
        raise ContractDatasetError("fixtures 中的 contract_no 不能重复")
    known = {item.contract_no: item for item in fixtures}
    cases = tuple(_case(item, index, known) for index, item in enumerate(cases_raw))
    case_ids = [item.case_id for item in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ContractDatasetError("cases 中的 case_id 不能重复")
    return ContractEvalDataset(dataset_name=name, policy_passage=passage, fixtures=fixtures, cases=cases)


def _fixture(item: object, index: int) -> ContractFixture:
    label = f"fixtures[{index}]"
    if not isinstance(item, dict):
        raise ContractDatasetError(f"{label} 必须是对象")
    _exact(item, _FIXTURE_FIELDS, label)
    version = item.get("current_version")
    if not isinstance(version, dict):
        raise ContractDatasetError(f"{label}.current_version 必须是对象")
    _exact(version, _VERSION_FIELDS, f"{label}.current_version")
    version_no = version.get("version_no")
    if type(version_no) is not int or isinstance(version_no, bool) or version_no < 1:
        raise ContractDatasetError(f"{label}.current_version.version_no 必须是正整数")
    created = version.get("created_at")
    if created is not None and not isinstance(created, str):
        raise ContractDatasetError(f"{label}.current_version.created_at 必须是字符串或 null")
    return ContractFixture(
        contract_no=_text(item.get("contract_no"), f"{label}.contract_no"),
        title=_text(item.get("title"), f"{label}.title"),
        contract_type=_text(item.get("contract_type"), f"{label}.contract_type"),
        counterparty_name=_text(item.get("counterparty_name"), f"{label}.counterparty_name"),
        amount=_text(item.get("amount"), f"{label}.amount"),
        currency=_text(item.get("currency"), f"{label}.currency"),
        sign_date=_optional_text(item.get("sign_date"), f"{label}.sign_date"),
        effective_date=_optional_text(item.get("effective_date"), f"{label}.effective_date"),
        expiry_date=_optional_text(item.get("expiry_date"), f"{label}.expiry_date"),
        status=_text(item.get("status"), f"{label}.status"),
        status_label=_text(item.get("status_label"), f"{label}.status_label"),
        description=_optional_text(item.get("description"), f"{label}.description"),
        version_no=version_no,
        version_label=_text(version.get("version_label"), f"{label}.current_version.version_label"),
        change_summary=_text(version.get("change_summary"), f"{label}.current_version.change_summary"),
        filename=_text(version.get("filename"), f"{label}.current_version.filename"),
        created_at=None if created is None else created,
    )


def _case(item: object, index: int, fixtures: dict[str, ContractFixture]) -> ContractEvalCase:
    label = f"cases[{index}]"
    if not isinstance(item, dict):
        raise ContractDatasetError(f"{label} 必须是对象")
    _exact(item, _CASE_FIELDS, label)
    case_id = _text(item.get("case_id"), f"{label}.case_id")
    label = case_id
    scenario = _choice(item.get("scenario"), SCENARIOS, f"{label}.scenario")
    description = _text(item.get("description"), f"{label}.description")
    question = _text(item.get("question"), f"{label}.question")
    if "eval-ticket" in question.lower() or "sk-" in question.lower():
        raise ContractDatasetError(f"{label}.question 不能包含票据或密钥片段")
    requested = item.get("requested_intent")
    if requested is not None:
        requested = _text(requested, f"{label}.requested_intent")
    intent = _choice(item.get("expected_intent"), INTENTS, f"{label}.expected_intent")
    route = _choice(item.get("expected_route"), INTENTS, f"{label}.expected_route")
    if intent != route:
        raise ContractDatasetError(f"{label} 的 expected_intent 与 expected_route 不一致")
    numbers = _text_list(item.get("expected_numbers"), f"{label}.expected_numbers")
    if len(numbers) != len(set(numbers)):
        raise ContractDatasetError(f"{label}.expected_numbers 不能重复")
    tool_call = _bool(item.get("expected_tool_call"), f"{label}.expected_tool_call")
    kind = _choice(item.get("answer_kind"), ANSWER_KINDS, f"{label}.answer_kind")
    actor = item.get("actor")
    if actor is not None:
        actor = _choice(actor, ACTORS, f"{label}.actor")
    fields = _fields(item.get("expected_fields"), f"{label}.expected_fields")
    snippets = _text_list(item.get("forbidden_snippets"), f"{label}.forbidden_snippets")
    expect_policy = _bool(item.get("expect_policy"), f"{label}.expect_policy")
    case = ContractEvalCase(
        case_id=case_id,
        scenario=scenario,
        description=description,
        question=question,
        requested_intent=requested,
        expected_intent=intent,
        expected_route=route,
        expected_numbers=tuple(numbers),
        expected_tool_call=tool_call,
        answer_kind=kind,
        actor=actor,
        expected_fields=tuple(fields.items()),
        forbidden_snippets=tuple(snippets),
        expect_policy=expect_policy,
    )
    _check_consistency(case, fixtures)
    return case


def _check_consistency(case: ContractEvalCase, fixtures: dict[str, ContractFixture]) -> None:
    kind = case.answer_kind
    if case.scenario == "mixed" and (not case.expect_policy or kind != "record"):
        raise ContractDatasetError(f"{case.case_id} 的混合问题必须期望制度原文并且返回合同记录")
    if case.scenario == "knowledge" and (kind != "knowledge" or case.expected_tool_call or case.expect_policy):
        raise ContractDatasetError(f"{case.case_id} 的知识问答样本不能调用合同工具")
    if kind == "record":
        if case.actor != "reader" or not case.expected_tool_call or len(case.expected_numbers) != 1:
            raise ContractDatasetError(f"{case.case_id} 的成功查询必须绑定 reader、调用一次工具并只有一个编号")
        if set(case.field_map()) != set(FIELD_KEYS):
            raise ContractDatasetError(f"{case.case_id} 的成功查询必须给出全部合同字段")
        _fields_match_fixture(case, fixtures)
    elif kind in {"not_found", "forbidden", "unavailable", "unauthorized"}:
        if not case.expected_tool_call or len(case.expected_numbers) != 1 or case.expected_fields:
            raise ContractDatasetError(f"{case.case_id} 的工具结果样本必须调用一次工具且不能期待合同字段")
        if not case.forbidden_snippets:
            raise ContractDatasetError(f"{case.case_id} 必须声明不能出现的合同字段")
    elif kind in {"missing_number", "ambiguous", "unsupported", "knowledge"}:
        if case.expected_tool_call or case.actor is not None or case.expected_fields:
            raise ContractDatasetError(f"{case.case_id} 不应调用工具")
    if kind == "ambiguous" and len(case.expected_numbers) < 2:
        raise ContractDatasetError(f"{case.case_id} 的多编号样本至少要有两个编号")
    if kind == "missing_number" and case.expected_numbers:
        raise ContractDatasetError(f"{case.case_id} 的缺编号样本不能期待编号")
    if kind == "knowledge" and case.expected_route != "knowledge_qa":
        raise ContractDatasetError(f"{case.case_id} 的知识问答路由不正确")
    if kind == "unsupported" and case.expected_route != "unsupported":
        raise ContractDatasetError(f"{case.case_id} 的不支持路由不正确")
    expected_actor = {
        "record": "reader",
        "forbidden": "no_permission",
        "unavailable": "java_down",
    }
    if kind in expected_actor and case.actor != expected_actor[kind]:
        raise ContractDatasetError(f"{case.case_id} 的 actor 与结果类型不一致")
    if kind == "not_found" and case.actor not in {"reader", "other_tenant"}:
        raise ContractDatasetError(f"{case.case_id} 的未找到样本 actor 不正确")
    if kind == "unauthorized" and case.actor not in {"no_ticket", "invalid_ticket", "consumed_ticket"}:
        raise ContractDatasetError(f"{case.case_id} 的票据样本 actor 不正确")


def _fields_match_fixture(case: ContractEvalCase, fixtures: dict[str, ContractFixture]) -> None:
    number = case.expected_numbers[0]
    fixture = fixtures.get(number)
    fields = case.field_map()
    if fixture is None:
        raise ContractDatasetError(f"{case.case_id} 的成功查询编号不在 fixtures 中")
    expected = {
        "contract_no": fixture.contract_no,
        "title": fixture.title,
        "contract_type": fixture.contract_type,
        "counterparty_name": fixture.counterparty_name,
        "amount": fixture.amount,
        "currency": fixture.currency,
        "status": fixture.status,
        "status_label": fixture.status_label,
        "version_no": str(fixture.version_no),
        "version_label": fixture.version_label,
    }
    if fields != expected:
        raise ContractDatasetError(f"{case.case_id} 的 expected_fields 与 fixture 不一致")


def _fields(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ContractDatasetError(f"{label} 必须是对象")
    unknown = sorted(set(value) - set(FIELD_KEYS))
    if unknown:
        raise ContractDatasetError(f"{label} 包含未知字段：{unknown[0]}")
    return {key: _text(item, f"{label}.{key}") for key, item in value.items()}


def _exact(payload: dict[str, object], expected: frozenset[str], label: str) -> None:
    missing = sorted(expected - payload.keys())
    extra = sorted(payload.keys() - expected)
    if missing:
        raise ContractDatasetError(f"{label} 缺少字段：{missing[0]}")
    if extra:
        raise ContractDatasetError(f"{label} 包含未知字段：{extra[0]}")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractDatasetError(f"{label} 必须是非空字符串")
    return value.strip()


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ContractDatasetError(f"{label} 必须是布尔值")
    return value


def _choice(value: object, allowed: frozenset[str], label: str) -> str:
    text = _text(value, label)
    if text not in allowed:
        raise ContractDatasetError(f"{label} 不受支持")
    return text


def _text_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ContractDatasetError(f"{label} 必须是数组")
    return [_text(item, f"{label}[{index}]") for index, item in enumerate(value)]
