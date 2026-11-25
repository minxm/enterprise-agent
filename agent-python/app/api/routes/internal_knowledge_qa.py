"""Internal Knowledge QA stream for the Java gateway.

The caller must present the shared internal token and a one-time ticket.
Tenant, actor, and allow_tenant_read come only from the redeemed ticket.
thread_id is rejected if a client tries to send it.
"""

from __future__ import annotations

import asyncio
import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, field_validator

from app.agents.knowledge_qa.service import HistoryMessage, HistoryResult, KnowledgeQAService
from app.agents.knowledge_qa.sse import open_knowledge_qa_sse
from app.clients.java_backend.client import JavaBackendError, KnowledgeQaGrant
from app.core.config import RAG_RETRIEVAL_STRATEGIES
from app.core.internal_auth import require_gateway

router = APIRouter()

ACTION_STREAM = "knowledge_qa_stream"
ACTION_HISTORY = "knowledge_qa_history"
_TICKET = re.compile(r"[A-Za-z0-9_-]{20,128}")


class KnowledgeQaStreamBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    question: str
    knowledge_qa_ticket: str
    top_k: int | None = None
    retrieval_strategy: str | None = None
    actor_ticket: str | None = None

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

    @field_validator("knowledge_qa_ticket", "actor_ticket")
    @classmethod
    def ticket_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _TICKET.fullmatch(value) is None:
            raise ValueError("票据不合法")
        return value


def get_knowledge_qa_service(request: Request) -> KnowledgeQAService:
    service = getattr(request.app.state, "knowledge_qa", None)
    if not isinstance(service, KnowledgeQAService):
        raise HTTPException(status_code=503, detail="知识问答尚未就绪")
    return service


def get_knowledge_qa_grants(request: Request) -> object:
    grants = getattr(request.app.state, "knowledge_qa_grants", None)
    if grants is None or not hasattr(grants, "redeem_knowledge_qa_ticket"):
        raise HTTPException(status_code=503, detail="身份授权尚未就绪")
    return grants


@router.post("/knowledge-qa/stream", dependencies=[Depends(require_gateway)])
async def stream_knowledge_qa(
    body: KnowledgeQaStreamBody,
    service: KnowledgeQAService = Depends(get_knowledge_qa_service),
    grants: object = Depends(get_knowledge_qa_grants),
):
    grant = _redeem(grants, body.knowledge_qa_ticket, action=ACTION_STREAM, conversation_id=body.conversation_id)
    arguments: dict[str, object] = {
        "tenant_id": grant.tenant_id,
        "question": body.question,
        "conversation_id": body.conversation_id,
        "actor_id": grant.actor_id,
        "allow_tenant_read": grant.allow_tenant_read,
    }
    if body.top_k is not None:
        arguments["top_k"] = body.top_k
    if body.retrieval_strategy is not None:
        arguments["retrieval_strategy"] = body.retrieval_strategy
    if body.actor_ticket is not None:
        arguments["actor_ticket"] = body.actor_ticket
    return open_knowledge_qa_sse(service, **arguments)


@router.get(
    "/knowledge-qa/conversations/{conversation_id}/messages",
    dependencies=[Depends(require_gateway)],
)
async def read_knowledge_qa_history(
    conversation_id: UUID,
    request: Request,
    service: KnowledgeQAService = Depends(get_knowledge_qa_service),
    grants: object = Depends(get_knowledge_qa_grants),
):
    if request.query_params:
        raise HTTPException(status_code=422, detail="请求参数不合法")
    ticket = request.headers.get("x-knowledge-qa-ticket", "")
    if not ticket:
        raise HTTPException(status_code=401, detail="未认证")
    grant = _redeem(grants, ticket, action=ACTION_HISTORY, conversation_id=conversation_id)
    result = await asyncio.to_thread(service.read_history, grant.tenant_id, conversation_id, grant.actor_id)
    return _history_response(result)


def _redeem(grants: object, ticket: str, *, action: str, conversation_id: UUID) -> KnowledgeQaGrant:
    try:
        grant = grants.redeem_knowledge_qa_ticket(ticket)
    except JavaBackendError as exc:
        if exc.code == "unauthorized":
            raise HTTPException(status_code=401, detail="未认证") from exc
        raise HTTPException(status_code=503, detail="身份授权暂时不可用") from exc
    if not isinstance(grant, KnowledgeQaGrant):
        raise HTTPException(status_code=401, detail="未认证")
    if grant.action != action or grant.conversation_id != conversation_id:
        raise HTTPException(status_code=401, detail="未认证")
    return grant


def _history_response(result: HistoryResult) -> dict[str, object]:
    if result.error_code == "thread_forbidden":
        raise HTTPException(status_code=404, detail="会话不存在")
    if result.error_code == "thread_busy":
        raise HTTPException(status_code=409, detail="同一个会话正在执行，请等待本次完成后再继续。")
    if result.error_code == "checkpoint_unavailable":
        raise HTTPException(status_code=503, detail="工作流状态暂时不可用")
    if result.error_code == "lease_unavailable":
        raise HTTPException(status_code=503, detail="会话锁暂时不可用")
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
