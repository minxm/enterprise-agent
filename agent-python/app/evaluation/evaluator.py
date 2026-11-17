"""Run retrieval and answer checks against an injected retriever and RAG service.

The evaluator records a failed case and continues. It does not drop the error,
and it does not open a database connection of its own.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from time import perf_counter
from uuid import UUID, uuid4

from app.evaluation.answers import AnswerScore, aggregate_answers, score_answer
from app.evaluation.dataset import EvalCase, EvaluationDataset
from app.evaluation.metrics import (
    EvaluationInputError,
    RetrievalScore,
    aggregate_retrieval,
    require_k,
    score_retrieval,
)

EVAL_TENANT_ID = UUID("00000000-0000-4000-8000-00000000e001")


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    description: str
    question: str
    status: str
    error: str | None
    retrieval: RetrievalScore | None
    retrieved_chunk_ids: tuple[str, ...]
    answer_text: str | None
    answer: AnswerScore | None


@dataclass(frozen=True)
class EvaluationReport:
    evaluation_id: str
    dataset_name: str
    mode: str
    k: int
    total_cases: int
    successful_cases: int
    failed_cases: int
    retrieval_metrics: dict[str, float | int | None]
    answer_metrics: dict[str, float | int | str | None]
    per_case_results: tuple[CaseResult, ...]
    failures: tuple[dict[str, str], ...]
    evaluation_time: str
    elapsed_seconds: float


class RAGEvaluator:
    def evaluate(
        self,
        dataset: EvaluationDataset,
        *,
        retriever: object,
        rag_service: object,
        k: int,
        mode: str = "offline",
    ) -> EvaluationReport:
        limit = require_k(k)
        started = perf_counter()
        results: list[CaseResult] = []
        for case in dataset.cases:
            results.append(self._evaluate_case(case, retriever, rag_service, limit))
        successes = [item for item in results if item.status == "success"]
        failures = tuple(
            {"case_id": item.case_id, "reason": item.error or "未知错误"}
            for item in results
            if item.status == "failed"
        )
        retrieval_metrics = aggregate_retrieval(
            [item.retrieval for item in successes if item.retrieval is not None]
        )
        retrieval_metrics["k"] = limit
        answer_metrics = aggregate_answers([item.answer for item in successes if item.answer is not None])
        return EvaluationReport(
            evaluation_id=str(uuid4()),
            dataset_name=dataset.dataset_name,
            mode=mode,
            k=limit,
            total_cases=len(results),
            successful_cases=len(successes),
            failed_cases=len(failures),
            retrieval_metrics=retrieval_metrics,
            answer_metrics=answer_metrics,
            per_case_results=tuple(results),
            failures=failures,
            evaluation_time=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=perf_counter() - started,
        )

    def _evaluate_case(
        self,
        case: EvalCase,
        retriever: object,
        rag_service: object,
        k: int,
    ) -> CaseResult:
        try:
            hits = retriever.search(EVAL_TENANT_ID, case.question, top_k=k)  # type: ignore[attr-defined]
            retrieved_ids = tuple(_chunk_id(hit) for hit in list(hits))
            answer = rag_service.ask(EVAL_TENANT_ID, case.question, top_k=k)  # type: ignore[attr-defined]
            answer_text = _answer_text(answer)
            source_count = len(getattr(answer, "sources", ()))
            retrieval = score_retrieval(list(retrieved_ids), case.expected_chunk_ids, k)
            graded = score_answer(
                answer=answer_text,
                source_count=source_count,
                should_refuse=case.should_refuse,
                expected_facts=case.expected_facts,
            )
        except Exception as exc:
            return CaseResult(
                case_id=case.case_id,
                description=case.description,
                question=case.question,
                status="failed",
                error=f"{exc.__class__.__name__}: {exc}",
                retrieval=None,
                retrieved_chunk_ids=(),
                answer_text=None,
                answer=None,
            )
        return CaseResult(
            case_id=case.case_id,
            description=case.description,
            question=case.question,
            status="success",
            error=None,
            retrieval=retrieval,
            retrieved_chunk_ids=retrieved_ids,
            answer_text=answer_text,
            answer=graded,
        )


def report_to_dict(report: EvaluationReport) -> dict[str, object]:
    return asdict(report)


def _chunk_id(hit: object) -> str:
    if isinstance(hit, str):
        return hit.strip()
    value = getattr(hit, "chunk_id", None)
    if not isinstance(value, str) and value is not None:
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise EvaluationInputError("检索结果缺少 chunk_id")
    return value.strip()


def _answer_text(answer: object) -> str:
    text = getattr(answer, "answer", None)
    if not isinstance(text, str):
        raise EvaluationInputError("RAG 结果缺少 answer")
    return text
