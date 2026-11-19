"""Compare vector, keyword, and hybrid retrieval on one evaluation dataset.

Metrics come from the existing score_retrieval helpers. The three strategies
are stored separately and are not averaged together. This module does not
call a chat model.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from time import perf_counter
from uuid import UUID, uuid4

from app.evaluation.answers import aggregate_answers
from app.evaluation.dataset import EvaluationDataset
from app.evaluation.metrics import RetrievalScore, aggregate_retrieval, score_retrieval
from app.evaluation.pgvector_eval import PGVECTOR_EVAL_TENANT_ID, PassageBinding
from app.knowledge.hybrid import DEFAULT_CANDIDATE_K, DEFAULT_RRF_K

FOCUS_CASE_ID = "multi-condition-large-payment"
INVOICE_STANDARD_CHUNK_ID = "00000000-0000-4000-8000-000000000008"
STRATEGIES = ("vector", "keyword", "hybrid")
RERANK_COMPARE = ("vector", "hybrid", "hybrid_rerank")
_KNOWN_STRATEGIES = STRATEGIES + ("hybrid_rerank",)
_LATENCY_NOTE = (
    "三种策略分开计时、分开平均。"
    "Hybrid 先做向量检索，再做关键词检索，最后做 RRF，三段时间相加就是总耗时。"
    "embedding_latency_ms 只计查询向量；vector_latency_ms 是向量 search 减去查询向量后的剩余时间。"
    "这些数字只描述当前机器和当前配置。"
)
_RERANK_LATENCY_NOTE = (
    _LATENCY_NOTE
    + " Hybrid + Reranker 在 RRF 之后串行调用 Reranker，rerank_latency_ms 只计这一段。"
    + " 不要把 Vector、Hybrid 和 Hybrid + Reranker 的耗时加在一起当成一次请求。"
)


class HybridEvaluationError(RuntimeError):
    """A comparison report could not be built."""


def compare_strategies(
    dataset: EvaluationDataset,
    bindings: Sequence[PassageBinding],
    *,
    vector_retriever: object,
    keyword_retriever: object,
    hybrid_retriever: object,
    k: int,
    embedding_client: object | None = None,
    clock: Callable[[], float] | None = None,
    tenant_id: UUID = PGVECTOR_EVAL_TENANT_ID,
    strategies: Sequence[str] = STRATEGIES,
    rerank_retriever: object | None = None,
    reranker_model: str | None = None,
) -> dict[str, object]:
    selected = _require_strategies(strategies)
    if "hybrid_rerank" in selected and rerank_retriever is None:
        raise HybridEvaluationError("未提供 Reranker")
    index = _binding_index(bindings)
    timer = clock or perf_counter
    started = timer()
    buckets = {name: [] for name in selected}
    for case in dataset.cases:
        if "vector" in selected:
            buckets["vector"].append(
                _run_vector(case, index, k, tenant_id, vector_retriever, embedding_client, timer)
            )
        if "keyword" in selected:
            buckets["keyword"].append(
                _run_keyword(case, index, k, tenant_id, keyword_retriever, timer)
            )
        if "hybrid" in selected:
            buckets["hybrid"].append(
                _run_hybrid(case, index, k, tenant_id, hybrid_retriever, timer)
            )
        if "hybrid_rerank" in selected:
            buckets["hybrid_rerank"].append(
                _run_rerank(case, index, k, tenant_id, rerank_retriever, timer)
            )
    strategies_payload = {name: _strategy_payload(buckets[name], k) for name in selected}
    answers = aggregate_answers([])
    answers["evaluated"] = False
    answers["reason"] = "本次只比较检索策略，未调用 Chat Model"
    return {
        "evaluation_id": str(uuid4()),
        "evaluation_mode": "rerank-compare" if "hybrid_rerank" in selected else "hybrid-compare",
        "mode": "rerank-compare" if "hybrid_rerank" in selected else "hybrid-compare",
        "dataset_name": dataset.dataset_name,
        "tenant_id": str(tenant_id),
        "top_k": k,
        "k": k,
        "rrf_k": DEFAULT_RRF_K,
        "vector_candidate_k": DEFAULT_CANDIDATE_K,
        "keyword_candidate_k": DEFAULT_CANDIDATE_K,
        "execution": "serial",
        "reranker_model": reranker_model if "hybrid_rerank" in selected else None,
        "strategies": strategies_payload,
        "comparison": _comparison(strategies_payload),
        "focus_case": _focus_case(dataset, strategies_payload),
        "rerank_changes": _rerank_changes(strategies_payload) if "hybrid_rerank" in selected else None,
        "answer_metrics": answers,
        "elapsed_seconds": timer() - started,
        "evaluation_time": datetime.now(timezone.utc).isoformat(),
        "latency_note": _RERANK_LATENCY_NOTE if "hybrid_rerank" in selected else _LATENCY_NOTE,
    }


def _run_vector(case, index, k, tenant_id, retriever, embedding_client, clock) -> dict[str, object]:
    started = clock()
    calls_before = _query_calls(embedding_client)
    if embedding_client is not None:
        embedding_client.last_query_seconds = None
    try:
        hits = retriever.search(tenant_id, case.question, top_k=k)
        ranked = [_vector_hit(hit) for hit in list(hits)]
        embed_seconds = _measured_embed(embedding_client, calls_before)
        total_seconds = clock() - started
        vector_seconds = total_seconds if embed_seconds is None else max(total_seconds - embed_seconds, 0.0)
        return _success_case(
            case,
            index,
            k,
            actual=[item[0] for item in ranked],
            details=[
                {"chunk_id": item[0], "rank": rank, "similarity": item[1]}
                for rank, item in enumerate(ranked, start=1)
            ],
            latencies={
                "embedding_latency_ms": None if embed_seconds is None else _ms(embed_seconds),
                "vector_latency_ms": _ms(vector_seconds),
                "keyword_latency_ms": None,
                "fusion_latency_ms": None,
                "total_latency_ms": _ms(total_seconds),
            },
        )
    except Exception as exc:
        return _failed_case(case, exc)


def _run_keyword(case, index, k, tenant_id, retriever, clock) -> dict[str, object]:
    started = clock()
    try:
        hits = retriever.search(tenant_id, case.question, top_k=k)
        actual = [_chunk_id(hit) for hit in list(hits)]
        total_seconds = clock() - started
        return _success_case(
            case,
            index,
            k,
            actual=actual,
            details=[{"chunk_id": chunk_id, "rank": rank} for rank, chunk_id in enumerate(actual, start=1)],
            latencies={
                "embedding_latency_ms": None,
                "vector_latency_ms": None,
                "keyword_latency_ms": _ms(total_seconds),
                "fusion_latency_ms": None,
                "total_latency_ms": _ms(total_seconds),
            },
        )
    except Exception as exc:
        return _failed_case(case, exc)


def _run_hybrid(case, index, k, tenant_id, retriever, clock) -> dict[str, object]:
    try:
        hits = retriever.search(tenant_id, case.question, top_k=k)
        timing = getattr(retriever, "last_timing", None)
        actual = [_chunk_id(hit) for hit in list(hits)]
        details = []
        for rank, hit in enumerate(list(hits), start=1):
            details.append(
                {
                    "chunk_id": _chunk_id(hit),
                    "rank": rank,
                    "vector_rank": getattr(hit, "vector_rank", None),
                    "keyword_rank": getattr(hit, "keyword_rank", None),
                    "rrf_score": getattr(hit, "rrf_score", None),
                    "similarity": getattr(hit, "similarity", None),
                }
            )
        latencies = _hybrid_latencies(timing, clock)
        return _success_case(case, index, k, actual=actual, details=details, latencies=latencies)
    except Exception as exc:
        return _failed_case(case, exc)


def _run_rerank(case, index, k, tenant_id, retriever, clock) -> dict[str, object]:
    del clock
    try:
        hits = list(retriever.search(tenant_id, case.question, top_k=k))
        timing = getattr(retriever, "last_timing", None)
        candidates = list(getattr(retriever, "last_candidates", ()) or ())
        actual = [_chunk_id(hit) for hit in hits]
        details = []
        for rank, hit in enumerate(hits, start=1):
            details.append(
                {
                    "chunk_id": _chunk_id(hit),
                    "rank": rank,
                    "vector_rank": getattr(hit, "vector_rank", None),
                    "keyword_rank": getattr(hit, "keyword_rank", None),
                    "rrf_score": getattr(hit, "rrf_score", None),
                    "similarity": getattr(hit, "similarity", None),
                    "rerank_score": getattr(hit, "rerank_score", None),
                }
            )
        payload = _success_case(
            case,
            index,
            k,
            actual=actual,
            details=details,
            latencies=_rerank_latencies(timing),
        )
        payload["rerank_change"] = _rank_change(candidates, hits, list(case.expected_chunk_ids), index, k)
        return payload
    except Exception as exc:
        return _failed_case(case, exc)


def _success_case(case, index, k, *, actual, details, latencies) -> dict[str, object]:
    mapped = [index.get(chunk_id, chunk_id) for chunk_id in actual]
    for item in details:
        item["standard_chunk_id"] = index.get(item["chunk_id"], item["chunk_id"])
    scored = score_retrieval(mapped, list(case.expected_chunk_ids), k)
    return {
        "case_id": case.case_id,
        "description": case.description,
        "question": case.question,
        "status": "success",
        "error": None,
        "expected_chunk_ids": list(case.expected_chunk_ids),
        "actual_chunk_ids": actual,
        "retrieved_chunk_ids": mapped,
        "ranks": details,
        "hit_rate_at_k": scored.hit,
        "recall_at_k": scored.recall,
        "mrr_at_k": scored.mrr,
        "retrieval": _score_dict(scored),
        **latencies,
    }


def _failed_case(case, exc: Exception) -> dict[str, object]:
    return {
        "case_id": case.case_id,
        "description": case.description,
        "question": case.question,
        "status": "failed",
        "error": f"{exc.__class__.__name__}: {exc}",
        "expected_chunk_ids": list(case.expected_chunk_ids),
        "actual_chunk_ids": [],
        "retrieved_chunk_ids": [],
        "ranks": [],
        "hit_rate_at_k": None,
        "recall_at_k": None,
        "mrr_at_k": None,
        "retrieval": None,
        "embedding_latency_ms": None,
        "vector_latency_ms": None,
        "keyword_latency_ms": None,
        "fusion_latency_ms": None,
        "total_latency_ms": None,
    }


def _strategy_payload(cases: list[dict[str, object]], k: int) -> dict[str, object]:
    successes = [item for item in cases if item["status"] == "success"]
    failures = [
        {"case_id": str(item["case_id"]), "reason": str(item["error"] or "未知错误")}
        for item in cases
        if item["status"] == "failed"
    ]
    scores = [
        _score_from_case(item["retrieval"])
        for item in successes
        if isinstance(item["retrieval"], dict)
    ]
    metrics = aggregate_retrieval(scores)
    metrics["k"] = k
    return {
        "total_cases": len(cases),
        "successful_cases": len(successes),
        "failed_cases": len(failures),
        "hit_rate_at_k": metrics["hit_rate"],
        "recall_at_k": metrics["recall"],
        "mrr_at_k": metrics["mrr"],
        "embedding_latency_ms": _sum_latency(successes, "embedding_latency_ms"),
        "vector_latency_ms": _sum_latency(successes, "vector_latency_ms"),
        "keyword_latency_ms": _sum_latency(successes, "keyword_latency_ms"),
        "fusion_latency_ms": _sum_latency(successes, "fusion_latency_ms"),
        "rerank_latency_ms": _sum_latency(successes, "rerank_latency_ms"),
        "total_latency_ms": _sum_latency(successes, "total_latency_ms"),
        "retrieval_metrics": metrics,
        "per_case_results": cases,
        "failures": failures,
    }


def _comparison(strategies: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    compared: dict[str, dict[str, object]] = {}
    for field in ("hit_rate_at_k", "recall_at_k", "mrr_at_k"):
        compared[field] = {name: payload[field] for name, payload in strategies.items()}
    return compared


def _focus_case(dataset: EvaluationDataset, strategies: dict[str, dict[str, object]]) -> dict[str, object] | None:
    case = next((item for item in dataset.cases if item.case_id == FOCUS_CASE_ID), None)
    if case is None:
        return None
    payload: dict[str, object] = {
        "case_id": case.case_id,
        "question": case.question,
        "expected_chunk_ids": list(case.expected_chunk_ids),
        "invoice_standard_chunk_id": INVOICE_STANDARD_CHUNK_ID,
    }
    for name, strategy in strategies.items():
        found = next(
            (item for item in strategy["per_case_results"] if item["case_id"] == FOCUS_CASE_ID),
            None,
        )
        retrieved = [] if found is None else list(found["retrieved_chunk_ids"])
        payload[name] = {
            "status": None if found is None else found["status"],
            "retrieved_chunk_ids": retrieved,
            "ranks": [] if found is None else found["ranks"],
            "contains_invoice": INVOICE_STANDARD_CHUNK_ID in retrieved,
            "hit_rate_at_k": None if found is None else found["hit_rate_at_k"],
            "recall_at_k": None if found is None else found["recall_at_k"],
            "mrr_at_k": None if found is None else found["mrr_at_k"],
        }
    return payload


def _rank_change(candidates, hits, expected: list[str], index: dict[str, str], k: int) -> dict[str, object]:
    before = [_mapped_chunk(item, index) for item in candidates]
    after = [_mapped_chunk(item, index) for item in hits]
    before_rank = {chunk_id: rank for rank, chunk_id in enumerate(before, start=1)}
    after_rank = {chunk_id: rank for rank, chunk_id in enumerate(after, start=1)}
    promoted: list[str] = []
    demoted: list[str] = []
    dropped: list[str] = []
    entered: list[str] = []
    expected_ranks = []
    for chunk_id in expected:
        previous = before_rank.get(chunk_id)
        current = after_rank.get(chunk_id)
        expected_ranks.append({"chunk_id": chunk_id, "before_rank": previous, "after_rank": current})
        was_in = previous is not None and previous <= k
        now_in = current is not None and current <= k
        if was_in and not now_in:
            dropped.append(chunk_id)
        if not was_in and now_in:
            entered.append(chunk_id)
        if previous is not None and current is not None and current < previous:
            promoted.append(chunk_id)
        if previous is not None and current is not None and current > previous:
            demoted.append(chunk_id)
    return {
        "before_chunk_ids": before,
        "after_chunk_ids": after,
        "expected_ranks": expected_ranks,
        "promoted": promoted,
        "demoted": demoted,
        "dropped_from_top_k": dropped,
        "entered_top_k": entered,
    }


def _rerank_changes(strategies: dict[str, dict[str, object]]) -> dict[str, int]:
    payload = strategies.get("hybrid_rerank", {})
    cases = payload.get("per_case_results", []) if isinstance(payload, dict) else []
    counts = {
        "questions_promoted": 0,
        "questions_demoted": 0,
        "questions_dropped_from_top_k": 0,
        "questions_entered_top_k": 0,
    }
    if not isinstance(cases, list):
        return counts
    for item in cases:
        if not isinstance(item, dict):
            continue
        change = item.get("rerank_change")
        if not isinstance(change, dict):
            continue
        if change.get("promoted"):
            counts["questions_promoted"] += 1
        if change.get("demoted"):
            counts["questions_demoted"] += 1
        if change.get("dropped_from_top_k"):
            counts["questions_dropped_from_top_k"] += 1
        if change.get("entered_top_k"):
            counts["questions_entered_top_k"] += 1
    return counts


def _mapped_chunk(hit: object, index: dict[str, str]) -> str:
    chunk_id = _chunk_id(hit)
    return index.get(chunk_id, chunk_id)


def _rerank_latencies(timing: object) -> dict[str, float | None]:
    if timing is None:
        return {
            "embedding_latency_ms": None,
            "vector_latency_ms": None,
            "keyword_latency_ms": None,
            "fusion_latency_ms": None,
            "rerank_latency_ms": None,
            "total_latency_ms": None,
        }
    embedding = getattr(timing, "embedding_seconds", None)
    return {
        "embedding_latency_ms": None if embedding is None else _ms(float(embedding)),
        "vector_latency_ms": _ms(float(getattr(timing, "vector_seconds", 0.0))),
        "keyword_latency_ms": _ms(float(getattr(timing, "keyword_seconds", 0.0))),
        "fusion_latency_ms": _ms(float(getattr(timing, "fusion_seconds", 0.0))),
        "rerank_latency_ms": _ms(float(getattr(timing, "rerank_seconds", 0.0))),
        "total_latency_ms": _ms(float(getattr(timing, "total_seconds", 0.0))),
    }


def _hybrid_latencies(timing: object, clock: Callable[[], float]) -> dict[str, float | None]:
    del clock
    if timing is None:
        return {
            "embedding_latency_ms": None,
            "vector_latency_ms": None,
            "keyword_latency_ms": None,
            "fusion_latency_ms": None,
            "total_latency_ms": None,
        }
    embedding = getattr(timing, "embedding_seconds", None)
    return {
        "embedding_latency_ms": None if embedding is None else _ms(float(embedding)),
        "vector_latency_ms": _ms(float(getattr(timing, "vector_seconds", 0.0))),
        "keyword_latency_ms": _ms(float(getattr(timing, "keyword_seconds", 0.0))),
        "fusion_latency_ms": _ms(float(getattr(timing, "fusion_seconds", 0.0))),
        "total_latency_ms": _ms(float(getattr(timing, "total_seconds", 0.0))),
    }


def _binding_index(bindings: Sequence[PassageBinding]) -> dict[str, str]:
    index: dict[str, str] = {}
    for binding in bindings:
        key = str(binding.chunk_id)
        if key in index:
            raise HybridEvaluationError("实际 Chunk ID 重复，无法映射")
        index[key] = binding.standard_chunk_id
    return index


def _vector_hit(hit: object) -> tuple[str, float | None]:
    similarity = getattr(hit, "similarity", None)
    if isinstance(similarity, bool) or not isinstance(similarity, (int, float)):
        similarity = None
    else:
        similarity = float(similarity)
    return _chunk_id(hit), similarity


def _chunk_id(hit: object) -> str:
    value = getattr(hit, "chunk_id", None)
    if isinstance(value, UUID):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise HybridEvaluationError("检索结果缺少 chunk_id")
    return value


def _query_calls(client: object | None) -> int:
    if client is None:
        return 0
    value = getattr(client, "query_calls", 0)
    return value if type(value) is int else 0


def _measured_embed(client: object | None, calls_before: int) -> float | None:
    if client is None:
        return None
    if getattr(client, "query_calls", 0) != calls_before + 1:
        raise HybridEvaluationError("未能单独测量查询 Embedding 耗时")
    value = getattr(client, "last_query_seconds", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HybridEvaluationError("未能单独测量查询 Embedding 耗时")
    return float(value)


def _score_dict(scored: RetrievalScore) -> dict[str, object]:
    return {
        "applicable": scored.applicable,
        "hit": scored.hit,
        "recall": scored.recall,
        "mrr": scored.mrr,
        "first_relevant_rank": scored.first_relevant_rank,
        "duplicate_chunk_ids": list(scored.duplicate_chunk_ids),
    }


def _score_from_case(retrieval: object) -> RetrievalScore:
    if not isinstance(retrieval, dict):
        raise HybridEvaluationError("检索指标类型不正确")
    duplicates = retrieval.get("duplicate_chunk_ids", [])
    if not isinstance(duplicates, list):
        duplicates = []
    return RetrievalScore(
        applicable=bool(retrieval["applicable"]),
        hit=_optional_float(retrieval["hit"]),
        recall=_optional_float(retrieval["recall"]),
        mrr=_optional_float(retrieval["mrr"]),
        first_relevant_rank=_optional_int(retrieval["first_relevant_rank"]),
        duplicate_chunk_ids=tuple(str(item) for item in duplicates),
    )


def _require_strategies(strategies: Sequence[str]) -> tuple[str, ...]:
    if not strategies:
        raise HybridEvaluationError("至少选择一种检索策略")
    unknown = [item for item in strategies if item not in _KNOWN_STRATEGIES]
    if unknown:
        raise HybridEvaluationError("未知检索策略：" + ",".join(unknown))
    return tuple(dict.fromkeys(strategies))


def _sum_latency(cases: Sequence[dict[str, object]], key: str) -> float | None:
    values: list[float] = []
    for item in cases:
        value = item.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        values.append(float(value))
    if not values:
        return None
    return round(sum(values), 3)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HybridEvaluationError("检索指标类型不正确")
    return float(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise HybridEvaluationError("检索排名类型不正确")
    return value


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 3)
