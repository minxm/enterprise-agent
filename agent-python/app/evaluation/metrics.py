"""Retrieval metrics for one ranked list and for a set of cases.

Refuse cases with an empty gold set are not scored. Their absence is not a miss.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_EVAL_K = 20


class EvaluationInputError(ValueError):
    """An evaluation parameter or result cannot be scored."""


@dataclass(frozen=True)
class RetrievalScore:
    applicable: bool
    hit: float | None
    recall: float | None
    mrr: float | None
    first_relevant_rank: int | None
    duplicate_chunk_ids: tuple[str, ...]


def require_k(k: object) -> int:
    if type(k) is not int or not 1 <= k <= MAX_EVAL_K:
        raise EvaluationInputError(f"K 必须是 1 到 {MAX_EVAL_K} 之间的整数")
    return k


def score_retrieval(retrieved_ids: list[str], expected_ids: list[str] | tuple[str, ...], k: int) -> RetrievalScore:
    limit = require_k(k)
    ranked = [_require_id(item) for item in retrieved_ids]
    expected = [_require_id(item) for item in expected_ids]
    window = ranked[:limit]
    duplicates = _duplicates(window)
    if not expected:
        return RetrievalScore(
            applicable=False,
            hit=None,
            recall=None,
            mrr=None,
            first_relevant_rank=None,
            duplicate_chunk_ids=duplicates,
        )
    expected_set = set(expected)
    rank = _first_rank(window, expected_set)
    matched = len(set(window) & expected_set)
    return RetrievalScore(
        applicable=True,
        hit=1.0 if rank is not None else 0.0,
        recall=matched / len(expected_set),
        mrr=0.0 if rank is None else 1.0 / rank,
        first_relevant_rank=rank,
        duplicate_chunk_ids=duplicates,
    )


def aggregate_retrieval(scores: list[RetrievalScore]) -> dict[str, float | int | None]:
    applicable = [score for score in scores if score.applicable and score.hit is not None]
    skipped = len(scores) - len(applicable)
    if not applicable:
        return {
            "applicable_cases": 0,
            "skipped_cases": skipped,
            "hit_rate": None,
            "recall": None,
            "mrr": None,
        }
    return {
        "applicable_cases": len(applicable),
        "skipped_cases": skipped,
        "hit_rate": _mean([score.hit for score in applicable if score.hit is not None]),
        "recall": _mean([score.recall for score in applicable if score.recall is not None]),
        "mrr": _mean([score.mrr for score in applicable if score.mrr is not None]),
    }


def _first_rank(ranked: list[str], expected: set[str]) -> int | None:
    for index, chunk_id in enumerate(ranked, start=1):
        if chunk_id in expected:
            return index
    return None


def _duplicates(ranked: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for chunk_id in ranked:
        if chunk_id in seen and chunk_id not in duplicates:
            duplicates.append(chunk_id)
        seen.add(chunk_id)
    return tuple(duplicates)


def _require_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationInputError("chunk_id 必须是非空字符串")
    return value.strip()


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)
