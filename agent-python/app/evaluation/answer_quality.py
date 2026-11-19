"""Answer quality checks that keep rules and the judge apart.

Phrase matching remains an auxiliary hint. It does not decide whether an
answer refused. Judge metrics stay null until a judge actually returns a
valid judgement.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from app.evaluation.answers import KEYWORD_MATCH_NOTE, is_refusal, score_answer
from app.evaluation.claim_extractor import Claim, ClaimExtractionError, extract_claims
from app.evaluation.dataset import EvaluationDataset
from app.evaluation.faithfulness import (
    FaithfulnessJudgement,
    SourcePassage,
    judge_claims,
    not_evaluated,
)

_CONFLICT = re.compile(r"冲突|互相矛盾|两种说法|另一种说法")
_PARTIAL = re.compile(r"只能确定|目前只能|仅能确定|没有具体说明|无法进一步|其余没有|其余无法|无法确定")
_INSUFFICIENT = re.compile(r"没有检索到|资料不足|没有关于|无法回答|没有相关|不足以|知识片段中没有|没有具体|无法确定")
_PRELIMINARY_NOTE = "样本只有十余条，这是初步校准，不能说明 Judge 已达到生产级可靠性。"


def assess_answer(
    *,
    question: str,
    answer: object,
    sources: list[object],
    expected_response_type: str | None = None,
    expected_facts: tuple[str, ...] | list[str] = (),
    expected_evidence: tuple[str, ...] | list[str] | None = None,
    judge: object | None = None,
) -> dict[str, object]:
    try:
        passages = _sources(sources)
        claims = extract_claims(answer) if isinstance(answer, str) else _reject_answer(answer)
    except ClaimExtractionError as exc:
        del expected_facts, expected_evidence
        return _failed_extraction(str(exc), expected_response_type, judge)
    text = answer if isinstance(answer, str) else ""
    response_class = classify_response(text, claims)
    graded = score_answer(
        answer=text,
        source_count=len(passages),
        should_refuse=expected_response_type == "refusal",
        expected_facts=expected_facts,
    )
    judgement = _judge(question, text, passages, claims, judge)
    semantic_match = None
    if judgement.status == "success" and expected_response_type is not None and judgement.response_class is not None:
        semantic_match = judgement.response_class == expected_response_type
    return {
        "claim_extraction": {"status": "success", "error": None, "claims": [_claim_payload(claim) for claim in claims]},
        "deterministic": _deterministic(
            text,
            response_class,
            graded,
            passages,
            expected_response_type,
            expected_evidence,
        ),
        "judge": _judge_payload(judgement, semantic_match),
    }


def classify_response(answer: str, claims: tuple[Claim, ...]) -> str:
    facts = [claim for claim in claims if claim.kind == "fact"]
    insufficient = any(claim.kind == "uncertainty" for claim in claims) or _INSUFFICIENT.search(answer) is not None
    conflict = any(claim.kind == "conflict" for claim in claims) or _CONFLICT.search(answer) is not None
    partial = _PARTIAL.search(answer) is not None
    if conflict and (facts or any(claim.kind == "conflict" for claim in claims)):
        return "conflict"
    if insufficient and facts and partial:
        return "partial_answer"
    if insufficient and facts:
        return "answered_despite_insufficiency"
    if partial and facts:
        return "partial_answer"
    if insufficient and not facts:
        return "refusal"
    return "answer"


def aggregate_quality(rows: list[dict[str, object]]) -> dict[str, object]:
    deterministic = [row.get("deterministic") for row in rows if isinstance(row.get("deterministic"), dict)]
    judges = [row.get("judge") for row in rows if isinstance(row.get("judge"), dict)]
    attempted = [item for item in judges if item.get("status") in {"success", "failed", "no_claims"}]
    successes = [item for item in judges if item.get("status") == "success"]
    judge_status = "not_evaluated"
    if attempted:
        judge_status = "success" if len(successes) == len(attempted) else "partial"
    if attempted and not successes:
        judge_status = "failed"
    return {
        "deterministic": {
            "citation_validity": _mean(deterministic, "citation_validity"),
            "fact_keyword_coverage": _mean(deterministic, "fact_keyword_coverage"),
            "answerability_accuracy": _mean_bool(deterministic, "answerability_match"),
            "expected_evidence_coverage": _mean(deterministic, "expected_evidence_coverage"),
            "response_outcome_counts": dict(Counter(
                str(item.get("response_outcome"))
                for item in deterministic
                if isinstance(item.get("response_outcome"), str)
            )),
            "fact_coverage_note": KEYWORD_MATCH_NOTE,
        },
        "judge": {
            "status": judge_status,
            "evaluation_success_rate": None if not attempted else len(successes) / len(attempted),
            "supported_claim_rate": _mean(successes, "supported_claim_rate"),
            "contradicted_claim_rate": _mean(successes, "contradicted_claim_rate"),
            "insufficient_claim_rate": _mean(successes, "insufficient_claim_rate"),
            "overall_faithfulness": _mean(successes, "overall_faithfulness"),
            "citation_support_rate": _mean(successes, "citation_support_rate"),
            "citation_completeness": _mean(successes, "citation_completeness"),
            "refusal_semantic_accuracy": _mean_bool(successes, "refusal_semantic_match"),
            "note": "Judge 未启用或调用失败时，语义指标为 null，不记 0 分。overall_faithfulness 等于 Supported Claim Rate，不能代替矛盾和证据不足比例。",
        },
    }


def load_calibration(path: Path | str) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list) or not payload["items"]:
        raise ValueError("人工校准集格式不正确")
    return payload


def score_calibration(payload: dict[str, object], *, judge: object | None = None) -> dict[str, object]:
    items = payload["items"]
    if not isinstance(items, list):
        raise ValueError("人工校准集格式不正确")
    rows = []
    disagreements = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("人工校准项格式不正确")
        assessed = assess_answer(
            question=str(item["question"]),
            answer=str(item["answer"]),
            sources=list(item["sources"]) if isinstance(item.get("sources"), list) else [],
            expected_response_type=str(item["expected_response_type"]),
            judge=judge,
        )
        deterministic = assessed["deterministic"]
        judged = assessed["judge"]
        human_class = str(item["human_response_class"])
        class_match = isinstance(deterministic, dict) and deterministic.get("response_class") == human_class
        judge_class_match = None
        if isinstance(judged, dict) and judged.get("status") == "success":
            judge_class_match = judged.get("response_class") == human_class
        claim_match = _claim_agreement(assessed, item.get("human_claims"))
        rows.append(
            {
                "item_id": item.get("item_id"),
                "deterministic_match": class_match,
                "judge_status": None if not isinstance(judged, dict) else judged.get("status"),
                "judge_error": None if not isinstance(judged, dict) else judged.get("error"),
                "judge_response_class": None if not isinstance(judged, dict) else judged.get("response_class"),
                "judge_claim_verdicts": _verdict_summary(judged),
                "judge_response_match": judge_class_match,
                "judge_claim_match": claim_match,
                "human_reason": item.get("human_reason"),
            }
        )
        if not class_match:
            disagreements.append(_disagreement(item, "response_class", human_class, deterministic))
        if judge_class_match is False:
            disagreements.append(_disagreement(item, "judge_response_class", human_class, judged))
        if claim_match is False:
            disagreements.append(_disagreement(item, "claim_verdict", item.get("human_claims"), judged))
    judge_rows = [row["judge_response_match"] for row in rows if isinstance(row["judge_response_match"], bool)]
    claim_rows = [row["judge_claim_match"] for row in rows if isinstance(row["judge_claim_match"], bool)]
    attempted = [row for row in rows if row.get("judge_status") in {"success", "failed", "no_claims"}]
    succeeded = [row for row in attempted if row.get("judge_status") == "success"]
    return {
        "dataset_name": payload.get("dataset_name"),
        "sample_size": len(rows),
        "preliminary": True,
        "note": _PRELIMINARY_NOTE,
        "deterministic_agreement": _bool_rate(row["deterministic_match"] for row in rows),
        "judge_agreement": None if judge is None or not judge_rows else sum(1 for item in judge_rows if item) / len(judge_rows),
        "judge_agreement_scope": None if judge is None else "successful_cases_only",
        "judge_claim_agreement": None if judge is None or not claim_rows else sum(1 for item in claim_rows if item) / len(claim_rows),
        "judge_evaluation_success_rate": None if judge is None or not attempted else len(succeeded) / len(attempted),
        "judge_status": "not_evaluated" if judge is None else "evaluated",
        "disagreements": disagreements,
        "items": rows,
    }


def rescore_answers(dataset: EvaluationDataset, cases: list[dict[str, object]]) -> dict[str, object]:
    by_id = {case.case_id: case for case in dataset.cases}
    passages = dataset.passage_map()
    scored = []
    for item in cases:
        case = by_id.get(str(item.get("case_id")))
        if case is None or not isinstance(item.get("answer_text"), str):
            continue
        source_ids = item.get("source_chunk_ids")
        sources = []
        if isinstance(source_ids, list):
            for source_id in source_ids:
                passage = passages.get(str(source_id))
                if passage is not None:
                    sources.append({"source_id": passage.chunk_id, "content": passage.content})
        quality = assess_answer(
            question=case.question,
            answer=item["answer_text"],
            sources=sources,
            expected_response_type=case.expected_response_type,
            expected_facts=case.expected_facts,
            expected_evidence=case.expected_evidence,
            judge=None,
        )
        scored.append({"case_id": case.case_id, "quality": quality})
    return {"cases": scored, "metrics": aggregate_quality([item["quality"] for item in scored])}


def _judge(question: str, answer: str, sources: tuple[SourcePassage, ...], claims: tuple[Claim, ...], judge: object | None) -> FaithfulnessJudgement:
    if judge is None:
        return not_evaluated()
    return judge_claims(question=question, answer=answer, sources=sources, claims=claims, client=judge)


def _deterministic(answer, response_class, graded, sources, expected_response_type, expected_evidence) -> dict[str, object]:
    tokens = len(graded.citations) + len(graded.malformed_citations) + len(graded.out_of_range_citations)
    evidence = None if expected_evidence is None else tuple(expected_evidence)
    present = {source.source_id for source in sources}
    coverage = None
    if evidence:
        coverage = sum(1 for item in evidence if item in present) / len(evidence)
    return {
        "citation_validity": None if tokens == 0 else len(graded.citations) / tokens,
        "fact_keyword_coverage": graded.keyword_coverage,
        "fact_coverage_note": KEYWORD_MATCH_NOTE,
        "response_class": response_class,
        "response_outcome": _outcome(response_class, expected_response_type),
        "phrase_refusal_hint": is_refusal(answer),
        "answerability_match": None if expected_response_type is None else response_class == expected_response_type,
        "expected_evidence_coverage": coverage,
        "expected_response_type": expected_response_type,
    }


def _outcome(response_class: str, expected: str | None) -> str | None:
    if expected is None:
        return None
    if response_class == "answered_despite_insufficiency":
        if expected == "refusal":
            return "answered_when_should_refuse"
        return "unsupported_conclusion_with_insufficiency"
    if response_class == "refusal" and expected == "refusal":
        return "correct_refusal"
    if response_class == "refusal":
        return "incorrect_refusal"
    if expected == "refusal":
        return "answered_when_should_refuse"
    if response_class == "partial_answer":
        return "partial_answer"
    if response_class == "conflict":
        return "conflict_explained"
    if response_class == "answer" and expected == "answer":
        return "answered"
    return response_class


def _sources(sources: list[object]) -> tuple[SourcePassage, ...]:
    passages: list[SourcePassage] = []
    for index, source in enumerate(sources, start=1):
        if isinstance(source, dict):
            source_id = source.get("source_id", source.get("chunk_id"))
            content = source.get("content")
        else:
            source_id = getattr(source, "source_id", None) or getattr(source, "chunk_id", None)
            content = getattr(source, "content", None)
        if not isinstance(source_id, str) or not isinstance(content, str):
            raise ClaimExtractionError("来源格式不正确")
        passages.append(SourcePassage(source_id=source_id, content=content, citation=index))
    return tuple(passages)


def _failed_extraction(error: str, expected_response_type: str | None, judge: object | None) -> dict[str, object]:
    del judge
    return {
        "claim_extraction": {"status": "failed", "error": error, "claims": []},
        "deterministic": {
            "citation_validity": None,
            "fact_keyword_coverage": None,
            "fact_coverage_note": KEYWORD_MATCH_NOTE,
            "response_class": None,
            "response_outcome": None,
            "phrase_refusal_hint": False,
            "answerability_match": None,
            "expected_evidence_coverage": None,
            "expected_response_type": expected_response_type,
        },
        "judge": _judge_payload(not_evaluated(), None),
    }


def _judge_payload(judgement: FaithfulnessJudgement, semantic_match: bool | None) -> dict[str, object]:
    return {
        "status": judgement.status,
        "error": judgement.error,
        "response_class": judgement.response_class,
        "expresses_insufficient_information": judgement.expresses_insufficient_information,
        "provides_unsupported_conclusion": judgement.provides_unsupported_conclusion,
        "identifies_conflict": judgement.identifies_conflict,
        "answers_supported_part": judgement.answers_supported_part,
        "supported_claim_rate": judgement.supported_claim_rate,
        "contradicted_claim_rate": judgement.contradicted_claim_rate,
        "insufficient_claim_rate": judgement.insufficient_claim_rate,
        "overall_faithfulness": judgement.overall_faithfulness,
        "citation_support_rate": judgement.citation_support_rate,
        "citation_completeness": judgement.citation_completeness,
        "refusal_semantic_match": semantic_match,
        "claim_judgements": [
            {
                "claim_id": item.claim_id,
                "verdict": item.verdict,
                "supporting_source_ids": list(item.supporting_source_ids),
                "contradicting_source_ids": list(item.contradicting_source_ids),
                "explanation": item.explanation,
            }
            for item in judgement.claims
        ],
    }


def _claim_payload(claim: Claim) -> dict[str, object]:
    return {
        "claim_id": claim.claim_id,
        "text": claim.text,
        "sentence_index": claim.sentence_index,
        "char_start": claim.char_start,
        "char_end": claim.char_end,
        "kind": claim.kind,
        "citations": list(claim.citations),
    }


def _claim_agreement(assessed: dict[str, object], human_claims: object) -> bool | None:
    judged = assessed.get("judge")
    if not isinstance(judged, dict) or judged.get("status") != "success":
        return None
    if not isinstance(human_claims, list) or not human_claims:
        return None
    extraction = assessed.get("claim_extraction")
    extracted = extraction.get("claims") if isinstance(extraction, dict) else None
    verdicts = judged.get("claim_judgements")
    if not isinstance(extracted, list) or not isinstance(verdicts, list):
        return None
    matched = 0
    for human in human_claims:
        if not isinstance(human, dict):
            return False
        found = _find_claim(str(human.get("text") or ""), extracted)
        verdict = next((item for item in verdicts if isinstance(item, dict) and item.get("claim_id") == found), None)
        if not isinstance(verdict, dict) or verdict.get("verdict") != human.get("verdict"):
            return False
        if list(verdict.get("supporting_source_ids") or []) != list(human.get("supporting_source_ids") or []):
            return False
        if list(verdict.get("contradicting_source_ids") or []) != list(human.get("contradicting_source_ids") or []):
            return False
        matched += 1
    return matched == len(human_claims)


def _find_claim(text: str, extracted: list[object]) -> str | None:
    normalized = _normalize(text)
    for item in extracted:
        if not isinstance(item, dict):
            continue
        other = _normalize(str(item.get("text") or ""))
        if normalized and (normalized in other or other in normalized):
            return str(item.get("claim_id"))
    return None


def _normalize(text: str) -> str:
    return "".join(text.split())


def _disagreement(item: dict[str, object], field: str, human: object, observed: object) -> dict[str, object]:
    if field == "claim_verdict" and isinstance(observed, dict):
        shown: object = _verdict_summary(observed)
    elif isinstance(observed, dict):
        shown = observed.get("response_class")
    else:
        shown = observed
    return {
        "item_id": item.get("item_id"),
        "field": field,
        "human": human if field != "claim_verdict" else _human_verdicts(human),
        "observed": shown,
        "human_reason": item.get("human_reason"),
    }


def _verdict_summary(judged: object) -> list[dict[str, object]] | None:
    if not isinstance(judged, dict):
        return None
    verdicts = judged.get("claim_judgements")
    if not isinstance(verdicts, list):
        return None
    return [
        {
            "claim_id": item.get("claim_id"),
            "verdict": item.get("verdict"),
            "supporting_source_ids": item.get("supporting_source_ids"),
            "contradicting_source_ids": item.get("contradicting_source_ids"),
        }
        for item in verdicts
        if isinstance(item, dict)
    ]


def _human_verdicts(human: object) -> object:
    if not isinstance(human, list):
        return human
    return [
        {
            "text": item.get("text"),
            "verdict": item.get("verdict"),
            "supporting_source_ids": item.get("supporting_source_ids"),
            "contradicting_source_ids": item.get("contradicting_source_ids"),
        }
        for item in human
        if isinstance(item, dict)
    ]


def _mean(rows: list[dict[str, object]], key: str) -> float | None:
    values = [float(item[key]) for item in rows if isinstance(item.get(key), (int, float)) and not isinstance(item.get(key), bool)]
    if not values:
        return None
    return sum(values) / len(values)


def _mean_bool(rows: list[dict[str, object]], key: str) -> float | None:
    values = [item[key] for item in rows if isinstance(item.get(key), bool)]
    if not values:
        return None
    return sum(1.0 if item else 0.0 for item in values) / len(values)


def _bool_rate(values) -> float | None:
    items = [item for item in values if isinstance(item, bool)]
    if not items:
        return None
    return sum(1.0 if item else 0.0 for item in items) / len(items)


def _reject_answer(answer: object) -> tuple[Claim, ...]:
    del answer
    raise ClaimExtractionError("答案必须是字符串")
