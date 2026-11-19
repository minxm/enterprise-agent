"""Compare vector, hybrid, and hybrid-rerank answers on one dataset.

Retrieval metrics reuse score_retrieval. Answer checks reuse score_answer.
Fact overlap is not a faithfulness score; this module does not invent one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from uuid import UUID, uuid4

from app.evaluation.answer_quality import aggregate_quality, assess_answer
from app.evaluation.answers import KEYWORD_MATCH_NOTE, aggregate_answers, score_answer
from app.evaluation.dataset import EvaluationDataset
from app.evaluation.hybrid_eval import INVOICE_STANDARD_CHUNK_ID
from app.evaluation.metrics import aggregate_retrieval, score_retrieval
from app.evaluation.pgvector_eval import PGVECTOR_EVAL_TENANT_ID, PassageBinding
from app.knowledge.rag import RAGService

ANSWER_STRATEGIES = ("vector", "hybrid", "hybrid_rerank")
FAITHFULNESS_NOTE = "顶层 faithfulness 保持为空。字符串覆盖不是 Faithfulness。Judge 语义结果在 quality.judge，未启用时为 not_evaluated，不记 0 分。"
_SUPPORT_NOTE = "cited_fact_support 表示预期事实字符串同时出现在答案和被引用来源正文中。它不是 Faithfulness。"


def compare_rag_answers(
    dataset: EvaluationDataset,
    bindings: tuple[PassageBinding, ...] | list[PassageBinding],
    *,
    rag_service: RAGService,
    k: int,
    tenant_id: UUID = PGVECTOR_EVAL_TENANT_ID,
    strategies: tuple[str, ...] = ANSWER_STRATEGIES,
    chat_model: str | None = None,
    embedding_model: str | None = None,
    reranker_model: str | None = None,
    failure_policy: str | None = None,
    candidate_k: int | None = None,
    quality_judge: object | None = None,
) -> dict[str, object]:
    index = _binding_index(bindings)
    started = perf_counter()
    buckets = {name: [] for name in strategies}
    for case in dataset.cases:
        for name in strategies:
            buckets[name].append(_run_case(case, rag_service, index, k, tenant_id, name, quality_judge))
    strategies_payload = {name: _strategy_payload(buckets[name], k) for name in strategies}
    return {
        "evaluation_id": str(uuid4()),
        "evaluation_mode": "rag-answer",
        "mode": "rag-answer",
        "dataset_name": dataset.dataset_name,
        "tenant_id": str(tenant_id),
        "top_k": k,
        "k": k,
        "chat_model": chat_model,
        "embedding_model": embedding_model,
        "reranker_model": reranker_model,
        "failure_policy": failure_policy,
        "candidate_k": candidate_k,
        "execution": "serial",
        "strategies": strategies_payload,
        "comparison": _comparison(strategies_payload),
        "focus_case": _focus(dataset, strategies_payload),
        "faithfulness": None,
        "faithfulness_note": FAITHFULNESS_NOTE,
        "cited_fact_support_note": _SUPPORT_NOTE,
        "keyword_match_note": KEYWORD_MATCH_NOTE,
        "elapsed_seconds": perf_counter() - started,
        "evaluation_time": datetime.now(timezone.utc).isoformat(),
    }


def _run_case(case, rag_service: RAGService, index: dict[str, str], k: int, tenant_id: UUID, strategy: str, quality_judge: object | None = None) -> dict[str, object]:
    try:
        answer = rag_service.ask(tenant_id, case.question, top_k=k, strategy=strategy)
        timing = rag_service.last_timing
        retrieved = [] if timing is None else [index.get(chunk_id, chunk_id) for chunk_id in timing.retrieved_ids]
        sources = list(answer.sources)
        source_ids = [index.get(str(item.chunk_id), str(item.chunk_id)) for item in sources]
        graded = score_answer(
            answer=answer.answer,
            source_count=len(sources),
            should_refuse=case.should_refuse,
            expected_facts=case.expected_facts,
        )
        support = _cited_support(answer.answer, sources, graded.citations, case.expected_facts)
        quality = assess_answer(
            question=case.question,
            answer=answer.answer,
            sources=[{"source_id": source_id, "content": source.content} for source, source_id in zip(sources, source_ids)],
            expected_response_type=case.expected_response_type,
            expected_facts=case.expected_facts,
            expected_evidence=case.expected_evidence,
            judge=quality_judge,
        )
        scored = score_retrieval(retrieved, list(case.expected_chunk_ids), k)
        return {
            "case_id": case.case_id,
            "description": case.description,
            "question": case.question,
            "status": "success",
            "error": None,
            "requested_strategy": answer.retrieval_strategy,
            "effective_strategy": answer.effective_strategy,
            "degraded": answer.degraded,
            "degradation_reason": answer.degradation_reason,
            "expected_chunk_ids": list(case.expected_chunk_ids),
            "retrieved_chunk_ids": retrieved,
            "source_chunk_ids": source_ids,
            "candidate_count": answer.candidate_count,
            "context_count": answer.context_count,
            "answer_text": answer.answer,
            "hit_rate_at_k": scored.hit,
            "recall_at_k": scored.recall,
            "mrr_at_k": scored.mrr,
            "retrieval": {
                "applicable": scored.applicable,
                "hit": scored.hit,
                "recall": scored.recall,
                "mrr": scored.mrr,
                "first_relevant_rank": scored.first_relevant_rank,
                "duplicate_chunk_ids": list(scored.duplicate_chunk_ids),
            },
            "quality": quality,
            "answer": {
                "is_refusal": graded.is_refusal,
                "refusal_correct": graded.refusal_correct,
                "citation_valid": graded.citation_valid,
                "citations": list(graded.citations),
                "malformed_citations": list(graded.malformed_citations),
                "out_of_range_citations": list(graded.out_of_range_citations),
                "matched_facts": list(graded.matched_facts),
                "missing_facts": list(graded.missing_facts),
                "keyword_coverage": graded.keyword_coverage,
                "cited_fact_support": support,
            },
            "retrieval_latency_ms": None if timing is None else _ms(timing.retrieval_seconds),
            "rerank_latency_ms": None if timing is None or timing.rerank_seconds is None else _ms(timing.rerank_seconds),
            "chat_latency_ms": None if timing is None else _ms(timing.chat_seconds),
            "total_latency_ms": None if timing is None else _ms(timing.total_seconds),
        }
    except Exception as exc:
        return {
            "case_id": case.case_id,
            "description": case.description,
            "question": case.question,
            "status": "failed",
            "error": f"{exc.__class__.__name__}: {exc}",
            "expected_chunk_ids": list(case.expected_chunk_ids),
            "retrieved_chunk_ids": [],
            "source_chunk_ids": [],
            "answer_text": None,
            "hit_rate_at_k": None,
            "recall_at_k": None,
            "mrr_at_k": None,
            "retrieval": None,
            "answer": None,
            "retrieval_latency_ms": None,
            "rerank_latency_ms": None,
            "chat_latency_ms": None,
            "total_latency_ms": None,
        }


def _strategy_payload(cases: list[dict[str, object]], k: int) -> dict[str, object]:
    successes = [item for item in cases if item["status"] == "success"]
    failures = [
        {"case_id": str(item["case_id"]), "reason": str(item["error"] or "未知错误")}
        for item in cases
        if item["status"] == "failed"
    ]
    retrieval_scores = [_score_from_case(item["retrieval"]) for item in successes if isinstance(item["retrieval"], dict)]
    answer_scores = [_answer_from_case(item["answer"]) for item in successes if isinstance(item["answer"], dict)]
    metrics = aggregate_retrieval(retrieval_scores)
    metrics["k"] = k
    answers = aggregate_answers(answer_scores)
    answers["cited_fact_support"] = _mean_support(successes)
    answers["faithfulness"] = None
    return {
        "total_cases": len(cases),
        "successful_cases": len(successes),
        "failed_cases": len(failures),
        "hit_rate_at_k": metrics["hit_rate"],
        "recall_at_k": metrics["recall"],
        "mrr_at_k": metrics["mrr"],
        "retrieval_latency_ms": _sum(successes, "retrieval_latency_ms"),
        "rerank_latency_ms": _sum(successes, "rerank_latency_ms"),
        "chat_latency_ms": _sum(successes, "chat_latency_ms"),
        "total_latency_ms": _sum(successes, "total_latency_ms"),
        "candidate_count": _sum(successes, "candidate_count"),
        "context_count": _sum(successes, "context_count"),
        "retrieval_metrics": metrics,
        "answer_metrics": answers,
        "quality_metrics": aggregate_quality([item["quality"] for item in successes if isinstance(item.get("quality"), dict)]),
        "per_case_results": cases,
        "failures": failures,
    }


def _comparison(strategies: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    compared: dict[str, dict[str, object]] = {}
    for field in ("hit_rate_at_k", "recall_at_k", "mrr_at_k"):
        compared[field] = {name: payload[field] for name, payload in strategies.items()}
    compared["answer"] = {
        name: payload["answer_metrics"]
        for name, payload in strategies.items()
        if isinstance(payload.get("answer_metrics"), dict)
    }
    return compared


def _focus(dataset: EvaluationDataset, strategies: dict[str, dict[str, object]]) -> dict[str, object] | None:
    case = next((item for item in dataset.cases if item.case_id == "multi-condition-large-payment"), None)
    if case is None:
        return None
    payload: dict[str, object] = {
        "case_id": case.case_id,
        "question": case.question,
        "expected_chunk_ids": list(case.expected_chunk_ids),
        "invoice_standard_chunk_id": INVOICE_STANDARD_CHUNK_ID,
    }
    for name, strategy in strategies.items():
        found = next((item for item in strategy["per_case_results"] if item["case_id"] == case.case_id), None)
        retrieved = [] if not isinstance(found, dict) else list(found.get("retrieved_chunk_ids") or [])
        payload[name] = {
            "status": None if not isinstance(found, dict) else found.get("status"),
            "retrieved_chunk_ids": retrieved,
            "contains_invoice": INVOICE_STANDARD_CHUNK_ID in retrieved,
            "answer_text": None if not isinstance(found, dict) else found.get("answer_text"),
        }
    return payload


def _cited_support(answer: str, sources: list[object], citations: tuple[int, ...], expected_facts: tuple[str, ...]) -> float | None:
    facts = tuple(fact for fact in expected_facts if fact)
    if not facts:
        return None
    cited = []
    for number in citations:
        if 1 <= number <= len(sources):
            content = getattr(sources[number - 1], "content", "")
            if isinstance(content, str):
                cited.append(content)
    supported = 0
    for fact in facts:
        if fact in answer and any(fact in content for content in cited):
            supported += 1
    return supported / len(facts)


def _binding_index(bindings) -> dict[str, str]:
    index: dict[str, str] = {}
    for binding in bindings:
        index[str(binding.chunk_id)] = binding.standard_chunk_id
    return index


def _score_from_case(retrieval: dict[str, object]):
    from app.evaluation.metrics import RetrievalScore

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


def _answer_from_case(payload: dict[str, object]):
    from app.evaluation.answers import AnswerScore

    citations = payload.get("citations", [])
    malformed = payload.get("malformed_citations", [])
    out_of_range = payload.get("out_of_range_citations", [])
    matched = payload.get("matched_facts", [])
    missing = payload.get("missing_facts", [])
    return AnswerScore(
        is_refusal=bool(payload["is_refusal"]),
        refusal_correct=bool(payload["refusal_correct"]),
        citations=tuple(int(item) for item in citations) if isinstance(citations, list) else (),
        malformed_citations=tuple(str(item) for item in malformed) if isinstance(malformed, list) else (),
        out_of_range_citations=tuple(int(item) for item in out_of_range) if isinstance(out_of_range, list) else (),
        citation_valid=bool(payload["citation_valid"]),
        matched_facts=tuple(str(item) for item in matched) if isinstance(matched, list) else (),
        missing_facts=tuple(str(item) for item in missing) if isinstance(missing, list) else (),
        keyword_coverage=_optional_float(payload["keyword_coverage"]),
        keyword_match_note=KEYWORD_MATCH_NOTE,
    )


def _mean_support(cases: list[dict[str, object]]) -> float | None:
    values: list[float] = []
    for item in cases:
        answer = item.get("answer")
        if not isinstance(answer, dict):
            continue
        value = answer.get("cited_fact_support")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        values.append(float(value))
    if not values:
        return None
    return sum(values) / len(values)


def _sum(cases: list[dict[str, object]], key: str) -> float | None:
    values = [float(item[key]) for item in cases if isinstance(item.get(key), (int, float)) and not isinstance(item.get(key), bool)]
    if not values:
        return None
    return round(sum(values), 3)


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _optional_int(value: object) -> int | None:
    if type(value) is not int:
        return None
    return value


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 3)
