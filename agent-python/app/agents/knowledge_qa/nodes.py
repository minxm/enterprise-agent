"""Nodes for the Knowledge QA graph.

Each node returns only the fields it owns. Routing does not call a model and
does not infer a business intent from question keywords.
"""

from __future__ import annotations

import logging
import re
from uuid import UUID

from langgraph.config import get_stream_writer

from app.agents.knowledge_qa.events import degradation_category
from app.agents.knowledge_qa.state import MAX_CONVERSATION_TURNS
from app.core.request_context import current_request_id
from app.knowledge.prompts import INSUFFICIENT_KNOWLEDGE
from app.knowledge.rag import RAGAnswer, RAGValidationError, _OMITTED, normalize_rag_request

logger = logging.getLogger(__name__)

_INTENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SENSITIVE = ("sk-", "api_key", "api-key", "bearer ", "password", "postgres://", "postgresql://", "secret")
UNSUPPORTED_ANSWER = "当前版本只回答企业知识库中的资料问题，不能执行合同审批、付款审核或其他业务操作。"


def validate_input(state: dict[str, object], *, default_top_k: int, default_strategy: str) -> dict[str, object]:
    try:
        tenant, question, top_k, strategy = normalize_rag_request(
            state.get("tenant_id"),
            state.get("question"),
            state["top_k"] if "top_k" in state else _OMITTED,
            state.get("retrieval_strategy"),
            default_top_k=default_top_k,
            default_strategy=default_strategy,
        )
        requested_intent = _require_requested_intent(state.get("requested_intent"))
    except RAGValidationError as exc:
        return _failure("invalid_input", "invalid_input", str(exc))
    return {
        "tenant_id": str(tenant),
        "question": question,
        "top_k": top_k,
        "retrieval_strategy": strategy,
        "requested_intent": requested_intent,
        "status": "validated",
        "answer": "",
        "sources": [],
        "retrieval_count": 0,
        "rag_result": None,
        "effective_strategy": None,
        "degraded": False,
        "degradation_reason": None,
        "error_code": None,
        "error_message": None,
    }


def route_intent(state: dict[str, object]) -> dict[str, object]:
    """Keep unspecified requests on knowledge QA. Any other explicit intent is unsupported."""
    requested = state.get("requested_intent")
    intent = "knowledge_qa" if requested in {None, "knowledge_qa"} else "unsupported"
    return {"intent": intent}


def knowledge_qa(state: dict[str, object], *, rag_service: object) -> dict[str, object]:
    try:
        result = rag_service.ask(  # type: ignore[attr-defined]
            UUID(str(state["tenant_id"])),
            str(state["question"]),
            top_k=state["top_k"],
            strategy=str(state["retrieval_strategy"]),
        )
    except Exception as exc:
        logger.warning(
            "知识问答节点失败 requestId=%s error=%s",
            current_request_id() or "-",
            exc.__class__.__name__,
        )
        return _failure("failed", "rag_failed", _safe_error(exc))
    if not isinstance(result, RAGAnswer):
        return _failure("failed", "rag_failed", "RAGService 返回了无法识别的结果")
    refused = result.retrieval_count == 0 and result.answer == INSUFFICIENT_KNOWLEDGE
    sources = [_source_payload(source) for source in result.sources]
    _observe_rag(result)
    return {
        "rag_result": {
            "answer": result.answer,
            "model": result.model,
            "retrieval_count": result.retrieval_count,
            "retrieval_strategy": result.retrieval_strategy,
            "effective_strategy": result.effective_strategy,
            "degraded": result.degraded,
            "degradation_reason": result.degradation_reason,
            "sources": sources,
        },
        "answer": result.answer,
        "sources": sources,
        "retrieval_count": result.retrieval_count,
        "effective_strategy": result.effective_strategy,
        "degraded": result.degraded,
        "degradation_reason": result.degradation_reason,
        "status": "refused" if refused else "answered",
        "error_code": None,
        "error_message": None,
    }


def unsupported(_state: dict[str, object]) -> dict[str, object]:
    return {
        "answer": UNSUPPORTED_ANSWER,
        "sources": [],
        "retrieval_count": 0,
        "rag_result": None,
        "effective_strategy": None,
        "degraded": False,
        "degradation_reason": None,
        "status": "unsupported",
        "error_code": "unsupported_intent",
        "error_message": "当前版本不支持该请求。",
    }


def format_response(state: dict[str, object]) -> dict[str, object]:
    status = state.get("status")
    if status not in {"answered", "refused", "unsupported", "invalid_input", "failed"}:
        status = "failed"
    retrieval_count = state.get("retrieval_count")
    return {
        "status": status,
        "answer": state.get("answer") or "",
        "sources": list(state.get("sources") or []),
        "retrieval_count": retrieval_count if isinstance(retrieval_count, int) else 0,
        "requested_strategy": None if status == "invalid_input" else _text(state.get("retrieval_strategy")),
        "effective_strategy": state.get("effective_strategy"),
        "degraded": bool(state.get("degraded")),
        "degradation_reason": state.get("degradation_reason"),
        "error_code": state.get("error_code"),
        "error_message": state.get("error_message"),
        "request_id": current_request_id(),
        "turns": _append_turn(state),
    }


def _append_turn(state: dict[str, object]) -> list[dict[str, object]]:
    prior = state.get("turns")
    turns = [item for item in prior if isinstance(item, dict)] if isinstance(prior, list) else []
    source_ids: list[str] = []
    for source in state.get("sources") or []:
        if isinstance(source, dict) and source.get("chunk_id"):
            source_ids.append(str(source["chunk_id"]))
            if len(source_ids) == 8:
                break
    turns.append(
        {
            "question": _bounded_text(state.get("question")),
            "answer": _bounded_text(state.get("answer")),
            "status": str(state.get("status") or ""),
            "source_ids": source_ids,
        }
    )
    overflow = len(turns) - MAX_CONVERSATION_TURNS
    if overflow > 0:
        del turns[:overflow]
    return turns


def _bounded_text(value: object) -> str:
    text = value if isinstance(value, str) else ""
    if len(text) <= 500:
        return text
    return text[:500]


def _observe_rag(result: RAGAnswer) -> None:
    """Report phases that RAGService.ask has already finished.

    Retrieval, rerank, and generation run inside that one synchronous call.
    Started and generation boundaries are not visible here, so they are not emitted.
    """
    _write_phase(
        {
            "event": "retrieval_completed",
            "retrieval_count": result.retrieval_count,
            "requested_strategy": result.retrieval_strategy,
            "effective_strategy": result.effective_strategy,
        }
    )
    if result.retrieval_strategy != "hybrid_rerank":
        return
    if result.degraded:
        _write_phase(
            {
                "event": "rerank_degraded",
                "requested_strategy": result.retrieval_strategy,
                "effective_strategy": result.effective_strategy,
                "degradation_reason": degradation_category(result.degradation_reason),
            }
        )
        return
    final_count = result.context_count if result.context_count else len(result.sources)
    _write_phase(
        {
            "event": "rerank_completed",
            "candidate_count": result.candidate_count,
            "final_count": final_count,
        }
    )


def _write_phase(payload: dict[str, object]) -> None:
    try:
        get_stream_writer()(payload)
    except Exception:
        return


def _source_payload(source: object) -> dict[str, object]:
    return {
        "document_id": str(getattr(source, "document_id")),
        "chunk_id": str(getattr(source, "chunk_id")),
        "chunk_index": getattr(source, "chunk_index"),
        "content": getattr(source, "content"),
        "similarity": getattr(source, "similarity", None),
        "embedding_model": getattr(source, "embedding_model", None),
        "char_start": getattr(source, "char_start", None),
        "char_end": getattr(source, "char_end", None),
        "vector_rank": getattr(source, "vector_rank", None),
        "keyword_rank": getattr(source, "keyword_rank", None),
        "rrf_score": getattr(source, "rrf_score", None),
        "rerank_score": getattr(source, "rerank_score", None),
    }


def _failure(status: str, code: str, message: str) -> dict[str, object]:
    return {
        "answer": "",
        "sources": [],
        "retrieval_count": 0,
        "rag_result": None,
        "effective_strategy": None,
        "degraded": False,
        "degradation_reason": None,
        "status": status,
        "error_code": code,
        "error_message": message,
    }


def _require_requested_intent(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _INTENT_NAME.fullmatch(value) is None:
        raise RAGValidationError("requested_intent 必须是小写意图名称或留空")
    return value


def _safe_error(exc: Exception) -> str:
    detail = str(exc).replace("\n", " ").strip()
    lowered = detail.lower()
    if any(token in lowered for token in _SENSITIVE):
        return exc.__class__.__name__
    if not detail:
        return exc.__class__.__name__
    return f"{exc.__class__.__name__}: {detail[:200]}"


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value
