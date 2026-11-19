"""Judge whether each claim is supported by the supplied sources.

overall_faithfulness is the supported-claim rate. Contradicted and insufficient
claims stay visible as their own rates. No valid factual claim yields null,
never 0 or 1. A failed judge call also yields null.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.evaluation.claim_extractor import Claim
from app.evaluation.judge_client import JudgeError, JudgeResponseError

JUDGE_SYSTEM_PROMPT = """你是企业知识库答案评估器。

只依据用户提供的知识片段判断。不要使用常识、行业惯例或外部知识补全证据。
不要改写答案，不要改写来源，不要补充来源里没有写出的规则。
来源之间冲突时，不要选择其中一份作为唯一事实。
只输出一个 JSON 对象，不要输出解释性前后文。"""

_VERDICTS = frozenset({"supported", "contradicted", "insufficient", "irrelevant"})
_RESPONSE_CLASSES = frozenset({"answer", "refusal", "partial_answer", "conflict", "answered_despite_insufficiency"})
_NOT_EVALUATED = "not_evaluated"


class _ClaimVerdictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    claim_id: str
    verdict: str
    supporting_source_ids: list[str]
    contradicting_source_ids: list[str]
    explanation: str

    @field_validator("claim_id", "verdict", "explanation")
    @classmethod
    def _text(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("字段不能为空")
        return value.strip()


class _JudgeModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    expresses_insufficient_information: bool
    provides_unsupported_conclusion: bool
    identifies_conflict: bool
    answers_supported_part: bool
    response_class: str
    claims: list[_ClaimVerdictModel]

    @field_validator("response_class")
    @classmethod
    def _class(cls, value: str) -> str:
        if value not in _RESPONSE_CLASSES:
            raise ValueError("response_class 不受支持")
        return value


@dataclass(frozen=True)
class SourcePassage:
    source_id: str
    content: str
    citation: int


@dataclass(frozen=True)
class ClaimJudgement:
    claim_id: str
    verdict: str
    supporting_source_ids: tuple[str, ...]
    contradicting_source_ids: tuple[str, ...]
    explanation: str


@dataclass(frozen=True)
class FaithfulnessJudgement:
    status: str
    error: str | None
    claims: tuple[ClaimJudgement, ...]
    expresses_insufficient_information: bool | None
    provides_unsupported_conclusion: bool | None
    identifies_conflict: bool | None
    answers_supported_part: bool | None
    response_class: str | None
    supported_claim_rate: float | None
    contradicted_claim_rate: float | None
    insufficient_claim_rate: float | None
    overall_faithfulness: float | None
    citation_support_rate: float | None
    citation_completeness: float | None


def not_evaluated() -> FaithfulnessJudgement:
    return _empty(_NOT_EVALUATED, None)


def judge_claims(
    *,
    question: str,
    answer: str,
    sources: tuple[SourcePassage, ...] | list[SourcePassage],
    claims: tuple[Claim, ...] | list[Claim],
    client: object,
) -> FaithfulnessJudgement:
    claim_list = tuple(claims)
    source_list = tuple(sources)
    if not claim_list:
        return _empty("no_claims", None)
    try:
        raw = client.complete_json(  # type: ignore[attr-defined]
            system=JUDGE_SYSTEM_PROMPT,
            user=_user_prompt(question, answer, source_list, claim_list),
        )
        parsed = validate_judgement(raw, claim_list, source_list)
    except JudgeError as exc:
        return _empty("failed", _safe_error(exc))
    except ValidationError:
        return _empty("failed", "Judge 输出缺少字段或类型不正确")
    return _score(parsed, claim_list, source_list)


def validate_judgement(
    payload: object,
    claims: tuple[Claim, ...],
    sources: tuple[SourcePassage, ...],
) -> _JudgeModel:
    if not isinstance(payload, dict):
        raise JudgeResponseError("Judge 输出不是 JSON 对象")
    required = (
        "expresses_insufficient_information",
        "provides_unsupported_conclusion",
        "identifies_conflict",
        "answers_supported_part",
        "response_class",
        "claims",
    )
    missing = [key for key in required if key not in payload]
    if missing:
        raise JudgeResponseError(f"Judge 输出缺少字段：{missing[0]}")
    try:
        parsed = _JudgeModel.model_validate(payload)
    except ValidationError as exc:
        location = exc.errors()[0].get("loc", ())
        field = ".".join(str(part) for part in location) or "unknown"
        raise JudgeResponseError(f"Judge 输出字段类型不正确：{field}") from None
    allowed = {source.source_id for source in sources}
    expected_ids = [claim.claim_id for claim in claims]
    seen = [item.claim_id for item in parsed.claims]
    if seen != expected_ids:
        raise JudgeResponseError("Judge 输出的 claim_id 与提取结果不一致")
    for item in parsed.claims:
        if item.verdict not in _VERDICTS:
            raise JudgeResponseError("Judge verdict 不受支持")
        if len(item.explanation) > 500:
            raise JudgeResponseError("Judge explanation 过长")
        _check_ids(item.supporting_source_ids, allowed)
        _check_ids(item.contradicting_source_ids, allowed)
        _check_verdict_shape(item)
    if parsed.response_class == "refusal" and parsed.provides_unsupported_conclusion:
        raise JudgeResponseError("Judge 把仍给出具体结论的答案标成了拒答")
    return parsed


def _check_ids(values: list[str], allowed: set[str]) -> None:
    for value in values:
        if not isinstance(value, str) or value not in allowed:
            raise JudgeResponseError("Judge 返回了未提供的 source_id")


def _check_verdict_shape(item: _ClaimVerdictModel) -> None:
    supporting = item.supporting_source_ids
    contradicting = item.contradicting_source_ids
    if item.verdict == "supported" and (not supporting or contradicting):
        raise JudgeResponseError("supported 必须只列出来源明确支持的 source_id")
    if item.verdict == "contradicted" and (not contradicting or supporting):
        raise JudgeResponseError("contradicted 必须只列出来源明确反对的 source_id")
    if item.verdict == "irrelevant" and (supporting or contradicting):
        raise JudgeResponseError("irrelevant 不能附带来源")
    if item.verdict == "insufficient" and ((supporting and not contradicting) or (contradicting and not supporting)):
        return
    if item.verdict == "insufficient" and supporting and contradicting:
        return


def _score(parsed: _JudgeModel, claims: tuple[Claim, ...], sources: tuple[SourcePassage, ...]) -> FaithfulnessJudgement:
    judgements = tuple(
        ClaimJudgement(
            item.claim_id,
            item.verdict,
            tuple(item.supporting_source_ids),
            tuple(item.contradicting_source_ids),
            item.explanation,
        )
        for item in parsed.claims
    )
    by_id = {item.claim_id: item for item in judgements}
    factual = [claim for claim in claims if claim.kind == "fact"]
    relevant = [by_id[claim.claim_id] for claim in factual if by_id[claim.claim_id].verdict != "irrelevant"]
    supported_rate, contradicted_rate, insufficient_rate = _three_rates(relevant)
    return FaithfulnessJudgement(
        status="success",
        error=None,
        claims=judgements,
        expresses_insufficient_information=parsed.expresses_insufficient_information,
        provides_unsupported_conclusion=parsed.provides_unsupported_conclusion,
        identifies_conflict=parsed.identifies_conflict,
        answers_supported_part=parsed.answers_supported_part,
        response_class=parsed.response_class,
        supported_claim_rate=supported_rate,
        contradicted_claim_rate=contradicted_rate,
        insufficient_claim_rate=insufficient_rate,
        overall_faithfulness=supported_rate,
        citation_support_rate=_citation_support(factual, by_id, sources),
        citation_completeness=_citation_completeness(relevant),
    )


def _three_rates(relevant: list[ClaimJudgement]) -> tuple[float | None, float | None, float | None]:
    if not relevant:
        return None, None, None
    total = len(relevant)
    return (
        _ratio(sum(item.verdict == "supported" for item in relevant), total),
        _ratio(sum(item.verdict == "contradicted" for item in relevant), total),
        _ratio(sum(item.verdict == "insufficient" for item in relevant), total),
    )


def _citation_support(
    factual: list[Claim],
    by_id: dict[str, ClaimJudgement],
    sources: tuple[SourcePassage, ...],
) -> float | None:
    pairs = 0
    supported = 0
    for claim in factual:
        judgement = by_id[claim.claim_id]
        for number in claim.citations:
            source = next((item for item in sources if item.citation == number), None)
            if source is None:
                continue
            pairs += 1
            if judgement.verdict == "supported" and source.source_id in judgement.supporting_source_ids:
                supported += 1
    if pairs == 0:
        return None
    return _ratio(supported, pairs)


def _citation_completeness(relevant: list[ClaimJudgement]) -> float | None:
    if not relevant:
        return None
    covered = sum(item.verdict == "supported" and bool(item.supporting_source_ids) for item in relevant)
    return _ratio(covered, len(relevant))


def _ratio(count: int, total: int) -> float:
    return count / total


def _empty(status: str, error: str | None) -> FaithfulnessJudgement:
    return FaithfulnessJudgement(status, error, (), None, None, None, None, None, None, None, None, None, None, None)


def _user_prompt(question: str, answer: str, sources: tuple[SourcePassage, ...], claims: tuple[Claim, ...]) -> str:
    source_lines = [
        json.dumps({"source_id": source.source_id, "citation": source.citation, "content": source.content}, ensure_ascii=False)
        for source in sources
    ]
    claim_lines = [
        json.dumps({"claim_id": claim.claim_id, "text": claim.text, "kind": claim.kind, "citations": list(claim.citations)}, ensure_ascii=False)
        for claim in claims
    ]
    return (
        "根对象必须包含 expresses_insufficient_information、provides_unsupported_conclusion、"
        "identifies_conflict、answers_supported_part、response_class、claims。"
        "前四个值必须是 JSON 布尔值 true 或 false，不能省略，也不能照抄示例。\n"
        "示例只说明键名："
        '{"expresses_insufficient_information":false,"provides_unsupported_conclusion":false,'
        '"identifies_conflict":false,"answers_supported_part":true,"response_class":"answer","claims":[]}\n'
        f"问题：{question}\n"
        f"答案：{answer}\n"
        "知识片段：\n"
        + "\n".join(source_lines)
        + "\n待判断陈述：\n"
        + "\n".join(claim_lines)
        + "\n\n对每条 claim_id 单独给出 verdict：supported、contradicted、insufficient、irrelevant 之一。\n"
        "只有来源原文明确写出该事实，才能是 supported，并且 supporting_source_ids 列出这些 source_id。\n"
        "来源原文明确相反时才是 contradicted，并且只填写 contradicting_source_ids。\n"
        "来源没有写明，或来源互相冲突而不能选边时，是 insufficient。冲突时两边 source_id 可以同时列出。\n"
        "与问题无关的陈述是 irrelevant，来源列表必须为空。\n"
        "引用编号合法本身不能作为 supported 的理由。\n"
        "一条事实可以由多个来源共同支持；缺少其中的必要条件时不要判 supported。\n"
        "response_class 只能是 answer、refusal、partial_answer、conflict、answered_despite_insufficiency。\n"
        "如果答案表示信息不足，同时又给出来源没有支持的具体结论，response_class 必须是 answered_despite_insufficiency，"
        "provides_unsupported_conclusion 为 true，不能标成 refusal。\n"
        "JSON 键为 expresses_insufficient_information、provides_unsupported_conclusion、identifies_conflict、"
        "answers_supported_part、response_class、claims。"
        "claims 中每项含 claim_id、verdict、supporting_source_ids、contradicting_source_ids、explanation。"
        "claims 的顺序和 claim_id 必须与输入一致。"
    )


def _safe_error(exc: Exception) -> str:
    text = f"{exc.__class__.__name__}: {exc}".replace("\n", " ").strip()
    lowered = text.lower()
    if "sk-" in lowered or "api_key" in lowered or "bearer " in lowered:
        return exc.__class__.__name__
    return text[:240]
