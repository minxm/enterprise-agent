"""Answer checks that do not call a model.

Keyword overlap is recorded as a surface check. It is not a faithfulness score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.knowledge.prompts import INSUFFICIENT_KNOWLEDGE

KEYWORD_MATCH_NOTE = "关键词匹配只表示预期事实字符串是否出现在答案中，不等于事实正确，也不能证明答案忠实于检索上下文。"

_REFUSAL_MARKERS = (
    INSUFFICIENT_KNOWLEDGE,
    "没有检索到与该问题相关",
    "知识库资料不足",
    "无法根据企业知识库",
    "当前知识库中没有",
    "没有相关的企业资料",
    "不足以支持回答",
    "无法从提供的知识片段中得到答案",
)
_BRACKET = re.compile(r"\[[^\]]*\]")
_CANONICAL = re.compile(r"\[(\d+)\]")


@dataclass(frozen=True)
class AnswerScore:
    is_refusal: bool
    refusal_correct: bool
    citations: tuple[int, ...]
    malformed_citations: tuple[str, ...]
    out_of_range_citations: tuple[int, ...]
    citation_valid: bool
    matched_facts: tuple[str, ...]
    missing_facts: tuple[str, ...]
    keyword_coverage: float | None
    keyword_match_note: str


def score_answer(
    *,
    answer: str,
    source_count: int,
    should_refuse: bool,
    expected_facts: tuple[str, ...] | list[str],
) -> AnswerScore:
    if not isinstance(answer, str):
        raise ValueError("answer 必须是字符串")
    if type(source_count) is not int or source_count < 0:
        raise ValueError("source_count 必须是非负整数")
    citations, malformed, out_of_range = _citations(answer, source_count)
    refused = is_refusal(answer)
    matched = tuple(fact for fact in expected_facts if fact and fact in answer)
    missing = tuple(fact for fact in expected_facts if fact and fact not in answer)
    coverage = None if not expected_facts else len(matched) / len(tuple(expected_facts))
    return AnswerScore(
        is_refusal=refused,
        refusal_correct=refused if should_refuse else not refused and bool(answer.strip()),
        citations=citations,
        malformed_citations=malformed,
        out_of_range_citations=out_of_range,
        citation_valid=not malformed and not out_of_range,
        matched_facts=matched,
        missing_facts=missing,
        keyword_coverage=coverage,
        keyword_match_note=KEYWORD_MATCH_NOTE,
    )


def is_refusal(answer: str) -> bool:
    text = answer.strip()
    if not text:
        return False
    return any(marker in text for marker in _REFUSAL_MARKERS)


def aggregate_answers(scores: list[AnswerScore]) -> dict[str, float | int | str | None]:
    if not scores:
        return {
            "case_count": 0,
            "citation_valid_rate": None,
            "refusal_accuracy": None,
            "fact_keyword_coverage": None,
            "keyword_match_note": KEYWORD_MATCH_NOTE,
        }
    fact_scores = [score.keyword_coverage for score in scores if score.keyword_coverage is not None]
    coverage = None if not fact_scores else sum(fact_scores) / len(fact_scores)
    return {
        "case_count": len(scores),
        "citation_valid_rate": sum(1.0 if score.citation_valid else 0.0 for score in scores) / len(scores),
        "refusal_accuracy": sum(1.0 if score.refusal_correct else 0.0 for score in scores) / len(scores),
        "fact_keyword_coverage": coverage,
        "keyword_match_note": KEYWORD_MATCH_NOTE,
    }


def _citations(answer: str, source_count: int) -> tuple[tuple[int, ...], tuple[str, ...], tuple[int, ...]]:
    citations: list[int] = []
    malformed: list[str] = []
    out_of_range: list[int] = []
    for match in _BRACKET.finditer(answer):
        token = match.group(0)
        parsed = _CANONICAL.fullmatch(token)
        if parsed is None or parsed.group(1) != str(int(parsed.group(1))):
            malformed.append(token)
            continue
        number = int(parsed.group(1))
        if number < 1 or number > source_count:
            out_of_range.append(number)
            continue
        citations.append(number)
    return tuple(citations), tuple(malformed), tuple(out_of_range)
