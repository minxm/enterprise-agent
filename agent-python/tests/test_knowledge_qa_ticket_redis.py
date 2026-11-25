"""Real Redis consumption of the Knowledge QA ticket layout.

The redeem handler performs the same GETDEL and value parse as Java. It does
not flush Redis and only deletes keys under this run's prefix.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis import Redis

from app.agents.knowledge_qa.service import KnowledgeQAService
from app.api.routes.internal_knowledge_qa import router
from app.clients.java_backend.client import JavaBackendClient
from app.core.errors import install_exception_handlers
from app.core.request_middleware import RequestIdMiddleware
from app.knowledge.rag import RAGAnswer
from langgraph.checkpoint.memory import MemorySaver

TOKEN = "test-internal-token"
TENANT = UUID("00000000-0000-4000-8000-00000000e360")
OTHER = UUID("00000000-0000-4000-8000-00000000e361")


class ScriptedRAG:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self._default_top_k = 5
        self._default_strategy = "vector"

    def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None) -> RAGAnswer:
        del top_k, strategy
        self.calls.append(tenant_id)
        return RAGAnswer(str(question), (), 0, "fake", retrieval_strategy="vector", effective_strategy="vector")


def test_real_redis_ticket_is_consumed_once_before_tenant_scope(monkeypatch):
    env = _root_env()
    password = env.get("REDIS_PASSWORD", "")
    if not password:
        pytest.skip("仓库根目录 .env 未提供 REDIS_PASSWORD，跳过真实 Redis 票据测试")
    host = env.get("REDIS_HOST") or "127.0.0.1"
    port = int(env.get("REDIS_PORT") or "6379")
    redis = Redis(host=host, port=port, password=password, socket_connect_timeout=2, socket_timeout=2, decode_responses=True)
    prefix = f"knowledge-qa-ticket:pyit:{uuid4()}:"
    try:
        redis.ping()
    except Exception as exc:
        pytest.skip(f"Redis 不可用，跳过真实 Redis 票据测试: {exc.__class__.__name__}")
    monkeypatch.setattr("app.core.config.settings.agent_internal_token", TOKEN)
    entered: list[object] = []

    def spy(tenant):
        entered.append(tenant)
        from app.persistence.fence_context import tenant_scope

        return tenant_scope(tenant)

    monkeypatch.setattr("app.agents.knowledge_qa.service.tenant_scope", spy)
    knowledge_before = set(redis.scan_iter(match="knowledge-qa-ticket:v1:*", count=200))
    contract_before = set(redis.scan_iter(match="actor-ticket:v1:*", count=200))
    conversation = uuid4()
    actor = uuid4()
    ticket = _ticket()
    key = prefix + hashlib.sha256(ticket.encode("utf-8")).hexdigest()
    assert ticket not in key
    redis.set(
        key,
        f"v1|{TENANT}|{actor}|{conversation}|knowledge_qa_stream|false|9999999999999",
        ex=180,
    )
    contract_ticket = _ticket()
    contract_key = "actor-ticket:v1:" + hashlib.sha256(contract_ticket.encode("utf-8")).hexdigest()
    contract_existed = redis.exists(contract_key)

    def handler(request: httpx.Request) -> httpx.Response:
        presented = request.headers.get("x-knowledge-qa-ticket", "")
        digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
        stored = redis.getdel(prefix + digest)
        if not isinstance(stored, str) or stored.count("|") != 6:
            return httpx.Response(401, json={"message": "未认证"})
        version, tenant_id, actor_id, conversation_id, action, allow, expires = stored.split("|")
        if version != "v1" or allow not in {"true", "false"}:
            return httpx.Response(401, json={"message": "未认证"})
        return httpx.Response(
            200,
            json={
                "data": {
                    "tenantId": tenant_id,
                    "actorId": actor_id,
                    "conversationId": conversation_id,
                    "action": action,
                    "allowTenantRead": allow == "true",
                    "expiresAt": expires,
                }
            },
        )

    rag = ScriptedRAG()
    app = FastAPI()
    install_exception_handlers(app)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(router, prefix="/internal/v1")
    app.state.knowledge_qa = KnowledgeQAService(rag, checkpointer=MemorySaver())
    app.state.knowledge_qa_grants = JavaBackendClient("http://java.internal", TOKEN, transport=httpx.MockTransport(handler))
    body = {
        "conversation_id": str(conversation),
        "question": "付款由谁审批？",
        "knowledge_qa_ticket": ticket,
        "actor_ticket": contract_ticket,
    }
    try:
        with TestClient(app) as client:
            forged = client.post(
                "/internal/v1/knowledge-qa/stream",
                headers={"X-Agent-Token": TOKEN},
                json={**body, "tenant_id": str(OTHER), "allow_tenant_read": True},
            )
            with client.stream(
                "POST",
                "/internal/v1/knowledge-qa/stream",
                headers={"X-Agent-Token": TOKEN, "X-Request-ID": "req-redis-ticket"},
                json=body,
            ) as response:
                assert response.status_code == 200
                raw = b"".join(response.iter_bytes())
            replay = client.post(
                "/internal/v1/knowledge-qa/stream",
                headers={"X-Agent-Token": TOKEN},
                json=body,
            )
        assert forged.status_code == 422
        assert replay.status_code == 401
        assert rag.calls == [TENANT]
        assert entered == [TENANT]
        assert ticket.encode("utf-8") not in raw
        assert redis.get(key) is None
        assert redis.exists(contract_key) == contract_existed
        assert set(redis.scan_iter(match="knowledge-qa-ticket:v1:*", count=200)) == knowledge_before
        assert set(redis.scan_iter(match="actor-ticket:v1:*", count=200)) == contract_before
    finally:
        keys = list(redis.scan_iter(match=prefix + "*", count=200))
        if keys:
            redis.delete(*keys)
        redis.close()


def _ticket() -> str:
    import secrets

    return secrets.token_urlsafe(32)


def _root_env() -> dict[str, str]:
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values
