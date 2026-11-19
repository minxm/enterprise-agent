"""Internal Knowledge QA stream for the Java gateway.

The caller must present the shared internal token. Tenant, actor, and
conversation come from that authenticated request body. thread_id is rejected
if a client tries to send it.
"""

from __future__ import annotations

import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, field_validator

from app.agents.knowledge_qa.service import HistoryMessage, HistoryResult, KnowledgeQAService
from app.agents.knowledge_qa.sse import open_knowledge_qa_sse
from app.core.config import RAG_RETRIEVAL_STRATEGIES, settings

router = APIRouter()
_TOKEN_HEADER = "X-Agent-Token"


class KnowledgeQaStreamBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    actor_id: UUID
    conversation_id: UUID
    question: str
    top_k: int | None = None
    retrieval_strategy: str | None = None

    @field_validator("question")
    @classmethod
    def question_text(cls, value: str) -> str:
        text = value.strip()
        if not text or len(text) > 2000:
            raise ValueError("问题长度不合法")
        return text

    @field_validator("top_k")
    @classmethod
    def top_k_range(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 20:
            raise ValueError("top_k 不合法")
        return value

    @field_validator("retrieval_strategy")
    @classmethod
    def strategy_name(cls, value: str | None) -> str | None:
        if value is not None and value not in RAG_RETRIEVAL_STRATEGIES:
            raise ValueError("检索策略不合法")
        return value


def require_gateway(x_agent_token: str | None = Header(default=None, alias=_TOKEN_HEADER)) -> None:
    expected = settings.agent_internal_token
    provided = x_agent_token or ""
    if not expected or len(expected) != len(provided) or not secrets.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="未认证")


def get_knowledge_qa_service(request: Request) -> KnowledgeQAService:
    service = getattr(request.app.state, "knowledge_qa", None)
    if not isinstance(service, KnowledgeQAService):
        raise HTTPException(status_code=503, detail="知识问答尚未就绪")
    return service


@router.post("/knowledge-qa/stream", dependencies=[Depends(require_gateway)])
async def stream_knowledge_qa(
    body: KnowledgeQaStreamBody,
    service: KnowledgeQAService = Depends(get_knowledge_qa_service),
):
    arguments: dict[str, object] = {
        "tenant_id": body.tenant_id,
        "question": body.question,
        "conversation_id": body.conversation_id,
        "actor_id": body.actor_id,
    }
    if body.top_k is not None:
        arguments["top_k"] = body.top_k
    if body.retrieval_strategy is not None:
        arguments["retrieval_strategy"] = body.retrieval_strategy
    return open_knowledge_qa_sse(service, **arguments)


class KnowledgeQaHistoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    actor_id: UUID


@router.get(
    "/knowledge-qa/conversations/{conversation_id}/messages",
    dependencies=[Depends(require_gateway)],
)
async def read_knowledge_qa_history(
    conversation_id: UUID,
    request: Request,
    tenant_id: UUID = Query(),
    actor_id: UUID = Query(),
    service: KnowledgeQAService = Depends(get_knowledge_qa_service),
):
    unknown = set(request.query_params) - {"tenant_id", "actor_id"}
    if unknown:
        raise HTTPException(status_code=422, detail="请求参数不合法")
    KnowledgeQaHistoryQuery(tenant_id=tenant_id, actor_id=actor_id)
    result = service.read_history(tenant_id, conversation_id, actor_id)
    return _history_response(result)


def _history_response(result: HistoryResult) -> dict[str, object]:
    if result.error_code == "thread_forbidden":
        raise HTTPException(status_code=404, detail="会话不存在")
    if result.error_code == "thread_busy":
        raise HTTPException(status_code=409, detail="同一个会话正在执行，请等待本次完成后再继续。")
    if result.error_code == "checkpoint_unavailable":
        raise HTTPException(status_code=503, detail="工作流状态暂时不可用")
    if result.error_code:
        raise HTTPException(status_code=422, detail="请求参数不合法")
    return {"messages": [_message_body(message) for message in result.messages]}


def _message_body(message: HistoryMessage) -> dict[str, object]:
    return {
        "role": message.role,
        "content": message.content,
        "status": message.status,
        "source_ids": list(message.source_ids),
        "sources": [{"chunk_id": source.chunk_id, "content": source.content} for source in message.sources],
    }
