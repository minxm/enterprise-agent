"""Contract lookup node. Facts in the answer come from the tool result."""

from __future__ import annotations

import logging
import re
from uuid import UUID

from app.agents.knowledge_qa.contract_intent import AMBIGUOUS_NUMBER, MISSING_NUMBER, extract_contract_numbers
from app.agents.knowledge_qa.nodes import _failure, _source_payload
from app.core.request_context import current_request_id
from app.tools.contract_query import ContractToolResult

logger = logging.getLogger(__name__)
_POLICY = re.compile(r"制度|规定|条款")
_SAFE_FAILURE = {
    "contract_unauthorized": "合同查询未授权",
    "contract_unavailable": "合同服务暂时不可用",
}


def contract_query(state: dict[str, object], *, contract_tool: object | None, rag_service: object) -> dict[str, object]:
    numbers = extract_contract_numbers(state.get("question"))
    if not numbers:
        return _prompt(MISSING_NUMBER, "missing_contract_no")
    if len(numbers) > 1:
        return _prompt(AMBIGUOUS_NUMBER, "ambiguous_contract_no")
    if contract_tool is None:
        return _failure("failed", "contract_unavailable", _SAFE_FAILURE["contract_unavailable"])
    try:
        result = contract_tool.query(numbers[0])  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning(
            "合同查询节点失败 requestId=%s error=%s",
            current_request_id() or "-",
            exc.__class__.__name__,
        )
        return _failure("failed", "contract_unavailable", _SAFE_FAILURE["contract_unavailable"])
    if not isinstance(result, ContractToolResult):
        return _failure("failed", "contract_unavailable", _SAFE_FAILURE["contract_unavailable"])
    if result.status != "answered":
        code = result.error_code or "contract_unavailable"
        return _failure("failed", code, _SAFE_FAILURE.get(code, _SAFE_FAILURE["contract_unavailable"]))
    appendix, sources, count = _policy_passages(state, rag_service)
    return {
        "answer": result.answer + appendix,
        "sources": sources,
        "retrieval_count": count,
        "rag_result": None,
        "effective_strategy": None,
        "degraded": False,
        "degradation_reason": None,
        "status": "answered",
        "error_code": result.error_code,
        "error_message": None,
    }


def _prompt(answer: str, code: str) -> dict[str, object]:
    return {
        "answer": answer,
        "sources": [],
        "retrieval_count": 0,
        "rag_result": None,
        "effective_strategy": None,
        "degraded": False,
        "degradation_reason": None,
        "status": "answered",
        "error_code": code,
        "error_message": None,
    }


def _policy_passages(state: dict[str, object], rag_service: object) -> tuple[str, list[dict[str, object]], int]:
    question = state.get("question")
    if not isinstance(question, str) or _POLICY.search(question) is None:
        return "", [], 0
    try:
        result = rag_service.ask(  # type: ignore[attr-defined]
            UUID(str(state["tenant_id"])),
            question,
            top_k=state.get("top_k"),
            strategy=state.get("retrieval_strategy"),
        )
    except Exception as exc:
        logger.warning(
            "合同制度检索失败 requestId=%s error=%s",
            current_request_id() or "-",
            exc.__class__.__name__,
        )
        return "\n\n相关制度暂时无法检索。", [], 0
    sources = []
    lines: list[str] = []
    for source in getattr(result, "sources", ()) or ():
        content = getattr(source, "content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        text = content.strip()
        lines.append(f"- {text}")
        sources.append(_source_payload(source))
        if len(lines) == 5:
            break
    if not lines:
        return "\n\n未检索到相关合同制度。", [], 0
    return "\n\n相关制度：\n" + "\n".join(lines), sources, len(sources)
