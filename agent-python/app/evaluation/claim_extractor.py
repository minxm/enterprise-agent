"""Split an answer into verifiable statements without calling a model.

A sentence joined by words such as “并且” stays one claim, so a condition is
not dropped. Extraction failure raises; it never invents a claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BOUNDARY = re.compile(r"[。！？]+|\n+")
_ENUM = re.compile(r"^\s*(?:\d+[\.、．]|（\d+）)\s*")
_CITATION = re.compile(r"\[(\d+)\]")
_LEADING_CONNECTIVE = re.compile(r"^(?:因此|所以|综上所述)[，,]?\s*")
_SUGGESTION = re.compile(r"建议|请咨询|请提供")
_UNCERTAINTY = re.compile(r"没有检索到|资料不足|没有关于|无法回答|没有相关|不足以|知识片段中没有|没有具体|无法确定|无法从提供的")
_CONFLICT = re.compile(r"存在冲突|资料存在冲突|两份资料|不一致|互相矛盾")
_CONCRETE = re.compile(r"必须|需要|应当|不得|万元|工作日|\d+\s*天|增值税|准入|补交|补贴\s*\d+|由\S{0,12}(?:审批|审核|签署|盖章|办理)")


class ClaimExtractionError(ValueError):
    """The answer could not be split into claims."""


@dataclass(frozen=True)
class Claim:
    claim_id: str
    text: str
    sentence_index: int
    char_start: int
    char_end: int
    kind: str
    citations: tuple[int, ...]


def extract_claims(answer: str) -> tuple[Claim, ...]:
    if not isinstance(answer, str):
        raise ClaimExtractionError("答案必须是字符串")
    claims: list[Claim] = []
    sentence_index = 0
    for start, end, raw in _attach_trailing_citations(_sentences(answer)):
        citations = tuple(int(item) for item in _CITATION.findall(raw))
        text = _CITATION.sub("", raw)
        text = _ENUM.sub("", text).strip(" \t;；，,")
        if not text:
            continue
        claims.append(
            Claim(
                claim_id=f"c{len(claims) + 1}",
                text=text,
                sentence_index=sentence_index,
                char_start=start,
                char_end=end,
                kind=_kind(text),
                citations=citations,
            )
        )
        sentence_index += 1
    return tuple(claims)


def _attach_trailing_citations(spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    merged: list[tuple[int, int, str]] = []
    for start, end, raw in spans:
        if merged and raw.strip() and _CITATION.sub("", raw).strip() == "":
            previous_start, _previous_end, previous = merged[-1]
            merged[-1] = (previous_start, end, previous + raw)
            continue
        merged.append((start, end, raw))
    return merged


def _sentences(answer: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    for match in _BOUNDARY.finditer(answer):
        spans.append((cursor, match.start(), answer[cursor : match.start()]))
        cursor = match.end()
    if cursor < len(answer):
        spans.append((cursor, len(answer), answer[cursor:]))
    return [(start, end, text) for start, end, text in spans if text.strip()]


def _kind(text: str) -> str:
    concrete = _CONCRETE.search(text) is not None
    uncertainty = _UNCERTAINTY.search(text) is not None
    if _SUGGESTION.search(text) is not None and not concrete:
        return "suggestion"
    if _LEADING_CONNECTIVE.match(text) is not None and uncertainty and not concrete:
        return "uncertainty"
    if _CONFLICT.search(text) is not None and not concrete:
        return "conflict"
    if uncertainty and not concrete:
        return "uncertainty"
    if _LEADING_CONNECTIVE.match(text) is not None and not concrete:
        return "explanation"
    return "fact"
