"""Internal gateway route for Knowledge QA SSE.

The model is scripted. Authentication uses a test token, not a production secret.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.knowledge_qa.service import KnowledgeQAService
from app.agents.knowledge_qa.threads import thread_id_for
from app.api.routes.internal_knowledge_qa import router
from app.core.config import settings
from app.core.errors import install_exception_handlers
from app.core.request_middleware import RequestIdMiddleware
from app.knowledge.prompts import INSUFFICIENT_KNOWLEDGE
from app.knowledge.rag import RAGAnswer, RAGSource
from langgraph.checkpoint.memory import MemorySaver

TOKEN = "test-internal-token"
TENANT = UUID("00000000-0000-4000-8000-00000000e291")
OTHER_TENANT = UUID("00000000-0000-4000-8000-00000000e292")
ACTOR = UUID("00000000-0000-4000-8000-00000000a291")
OTHER_ACTOR = UUID("00000000-0000-4000-8000-00000000a292")
DOC = UUID("00000000-0000-4000-8000-0000000000d9")
CHUNK = UUID("00000000-0000-4000-8000-0000000000c9")


class ScriptedRAG:
    def __init__(self, results: list[RAGAnswer | Exception]) -> None:
        self.results = list(results)
        self.calls: list[tuple[object, object, object]] = []
        self._default_top_k = 5
        self._default_strategy = "vector"

    def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None) -> RAGAnswer:
        self.calls.append((tenant_id, question, strategy))
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_missing_or_wrong_internal_token_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", TOKEN)
    rag = ScriptedRAG([_answer("不应执行")])
    with TestClient(_app(KnowledgeQAService(rag))) as client:
        missing = client.post("/internal/v1/knowledge-qa/stream", json=_body())
        wrong = client.post(
            "/internal/v1/knowledge-qa/stream",
            json=_body(),
            headers={"X-Agent-Token": "other-token"},
        )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert rag.calls == []
    assert TOKEN not in missing.text
    assert "traceback" not in missing.text.lower()
    assert "traceback" not in wrong.text.lower()


def test_blank_server_token_and_unready_service_are_rejected(monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", "")
    with TestClient(_app(KnowledgeQAService(ScriptedRAG([_answer("不应执行")])))) as client:
        rejected = client.post(
            "/internal/v1/knowledge-qa/stream",
            headers={"X-Agent-Token": TOKEN},
            json=_body(),
        )
    assert rejected.status_code == 401
    assert "traceback" not in rejected.text.lower()

    monkeypatch.setattr(settings, "agent_internal_token", TOKEN)
    app = _app(KnowledgeQAService(ScriptedRAG([_answer("不应执行")])))
    app.state.knowledge_qa = None
    with TestClient(app) as client:
        unready = client.post(
            "/internal/v1/knowledge-qa/stream",
            headers=_headers("req-unready"),
            json=_body(),
        )
    assert unready.status_code == 503
    assert "Traceback" not in unready.text
    assert TOKEN not in unready.text


def test_authenticated_stream_uses_gateway_identity_and_rejects_thread_id(monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", TOKEN)
    rag = ScriptedRAG([_answer("部门负责人审批。[1]")])
    service = KnowledgeQAService(rag, checkpointer=MemorySaver())
    conversation = uuid4()
    with TestClient(_app(service)) as client:
        rejected = client.post(
            "/internal/v1/knowledge-qa/stream",
            headers=_headers("req-internal-1"),
            json={**_body(conversation), "thread_id": "client-picked"},
        )
        with client.stream(
            "POST",
            "/internal/v1/knowledge-qa/stream",
            headers=_headers("req-internal-1"),
            json={**_body(conversation), "top_k": 5, "retrieval_strategy": "vector"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            raw = b"".join(response.iter_bytes()).decode("utf-8")

    assert rejected.status_code == 422
    assert "client-picked" not in rejected.text
    events = _parse_sse(raw)
    assert [name for name, _data in events].count("run_completed") == 1
    assert events[0][0] == "run_started"
    assert all(data.get("request_id") == "req-internal-1" for _name, data in events)
    message = next(data for name, data in events if name == "message")
    assert message["answer"] == "部门负责人审批。[1]"
    assert message["sources"][0]["chunk_id"] == str(CHUNK)
    assert rag.calls == [(TENANT, "付款由谁审批？", "vector")]


def test_other_actor_is_forbidden_and_a_busy_thread_does_not_run_again(monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", TOKEN)
    rag = ScriptedRAG([_answer("仅本人可见"), _answer("不应执行")])
    service = KnowledgeQAService(rag, checkpointer=MemorySaver())
    conversation = uuid4()
    with TestClient(_app(service)) as client:
        with client.stream(
            "POST",
            "/internal/v1/knowledge-qa/stream",
            headers=_headers("req-owner"),
            json=_body(conversation),
        ) as first:
            assert first.status_code == 200
            first_raw = b"".join(first.iter_bytes()).decode("utf-8")
        with client.stream(
            "POST",
            "/internal/v1/knowledge-qa/stream",
            headers=_headers("req-other"),
            json=_body(conversation, actor=OTHER_ACTOR),
        ) as second:
            second_raw = b"".join(second.iter_bytes()).decode("utf-8")
        lock = service._locks.lock_for(thread_id_for(TENANT, conversation))
        assert lock.acquire(blocking=False)
        try:
            with client.stream(
                "POST",
                "/internal/v1/knowledge-qa/stream",
                headers=_headers("req-busy"),
                json=_body(conversation),
            ) as busy:
                busy_raw = b"".join(busy.iter_bytes()).decode("utf-8")
        finally:
            lock.release()

    assert _parse_sse(first_raw)[-1][0] == "run_completed"
    denied = _parse_sse(second_raw)
    assert denied[-1][0] == "run_failed"
    assert denied[-1][1]["error_code"] == "thread_forbidden"
    assert "仅本人可见" not in second_raw
    busy_events = _parse_sse(busy_raw)
    assert busy_events[-1][1]["error_code"] == "thread_busy"
    assert rag.calls == [(TENANT, "付款由谁审批？", "vector")]
    assert lock.acquire(blocking=False)
    lock.release()


def test_failure_event_hides_secrets_and_other_tenant_is_a_different_thread(monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", TOKEN)
    rag = ScriptedRAG([
        RuntimeError("postgres://agent_app:password@127.0.0.1/db sk-live-secret"),
        _answer("其他租户"),
    ])
    service = KnowledgeQAService(rag, checkpointer=MemorySaver())
    conversation = uuid4()
    with TestClient(_app(service)) as client:
        with client.stream(
            "POST",
            "/internal/v1/knowledge-qa/stream",
            headers=_headers("req-fail"),
            json=_body(conversation),
        ) as failed:
            failed_raw = b"".join(failed.iter_bytes())
        with client.stream(
            "POST",
            "/internal/v1/knowledge-qa/stream",
            headers=_headers("req-other-tenant"),
            json=_body(conversation, tenant=OTHER_TENANT),
        ) as other:
            other_raw = b"".join(other.iter_bytes()).decode("utf-8")

    assert b"sk-live-secret" not in failed_raw
    assert b"password" not in failed_raw
    assert b"127.0.0.1" not in failed_raw
    failed_events = _parse_sse(failed_raw.decode("utf-8"))
    assert failed_events[-1][0] == "run_failed"
    assert failed_events[-1][1]["safe_error_message"] == "RuntimeError"
    other_events = _parse_sse(other_raw)
    assert other_events[-2][1]["answer"] == "其他租户"
    assert other_events[-2][1]["request_id"] == "req-other-tenant"


def test_checkpoint_history_survives_a_second_internal_call(monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", TOKEN)

    class MappedRAG:
        def __init__(self) -> None:
            self._default_top_k = 5
            self._default_strategy = "vector"

        def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None) -> RAGAnswer:
            del tenant_id, top_k, strategy
            return _answer(str(question))

    service = KnowledgeQAService(MappedRAG(), checkpointer=MemorySaver())
    conversation = uuid4()
    with TestClient(_app(service)) as client:
        with client.stream(
            "POST",
            "/internal/v1/knowledge-qa/stream",
            headers=_headers("req-1"),
            json=_body(conversation, question="第一题"),
        ) as first:
            assert b"event: run_completed" in b"".join(first.iter_bytes())
        with client.stream(
            "POST",
            "/internal/v1/knowledge-qa/stream",
            headers=_headers("req-2"),
            json=_body(conversation, question="第二题"),
        ) as second:
            second_raw = b"".join(second.iter_bytes()).decode("utf-8")
    message = next(data for name, data in _parse_sse(second_raw) if name == "message")
    assert message["answer"] == "第二题"
    restored = service.ask(TENANT, "第三题", conversation_id=conversation, actor_id=ACTOR)
    assert [turn["question"] for turn in restored.turns] == ["第一题", "第二题", "第三题"]


def test_production_app_exposes_only_the_internal_stream():
    from app.main import app

    paths: list[str] = []
    for route in app.routes:
        original = getattr(route, "original_router", None)
        context = getattr(route, "include_context", None)
        prefix = getattr(context, "prefix", "") if context is not None else ""
        if original is None:
            continue
        for nested in original.routes:
            nested_path = getattr(nested, "path", "")
            if isinstance(nested_path, str):
                paths.append(f"{prefix}{nested_path}")
    assert "/internal/v1/knowledge-qa/stream" in paths
    assert "/internal/v1/knowledge-qa/conversations/{conversation_id}/messages" in paths
    assert "/api/v1/agent/knowledge-qa/stream" not in paths
    assert sorted(path for path in paths if "knowledge-qa" in path) == [
        "/internal/v1/knowledge-qa/conversations/{conversation_id}/messages",
        "/internal/v1/knowledge-qa/stream",
    ]


def test_history_route_projects_messages_and_rejects_thread_id(monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", TOKEN)
    rag = ScriptedRAG([_answer("第一轮"), _answer("第二轮")])
    service = KnowledgeQAService(rag, checkpointer=MemorySaver())
    conversation = uuid4()
    with TestClient(_app(service)) as client:
        for question in ("第一题", "第二题"):
            with client.stream(
                "POST",
                "/internal/v1/knowledge-qa/stream",
                headers=_headers("req-history"),
                json=_body(conversation, question=question),
            ) as response:
                assert response.status_code == 200
                b"".join(response.iter_bytes())
        rejected = client.get(
            f"/internal/v1/knowledge-qa/conversations/{conversation}/messages",
            headers=_headers("req-history"),
            params={"tenant_id": str(TENANT), "actor_id": str(ACTOR), "thread_id": thread_id_for(TENANT, conversation)},
        )
        denied = client.get(
            f"/internal/v1/knowledge-qa/conversations/{conversation}/messages",
            headers=_headers("req-history"),
            params={"tenant_id": str(TENANT), "actor_id": str(OTHER_ACTOR)},
        )
        allowed = client.get(
            f"/internal/v1/knowledge-qa/conversations/{conversation}/messages",
            headers=_headers("req-history"),
            params={"tenant_id": str(TENANT), "actor_id": str(ACTOR)},
        )
    assert rejected.status_code == 422
    assert "thread_id" not in rejected.text
    assert denied.status_code == 404
    assert denied.json()["message"] == "请求无法完成"
    assert "thread_forbidden" not in denied.text
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["messages"][0] == {
        "role": "user",
        "content": "第一题",
        "status": "answered",
        "source_ids": [],
        "sources": [],
    }
    assert body["messages"][1]["role"] == "assistant"
    assert body["messages"][1]["content"] == "第一轮"
    assert body["messages"][3]["content"] == "第二轮"
    assert "rag_result" not in allowed.text
    assert "blob" not in allowed.text
    assert "metadata" not in allowed.text
    assert thread_id_for(TENANT, conversation) not in allowed.text


def _app(service: KnowledgeQAService) -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(router, prefix="/internal/v1")
    app.state.knowledge_qa = service
    return app


def _headers(request_id: str) -> dict[str, str]:
    return {"X-Agent-Token": TOKEN, "X-Request-ID": request_id}


def _body(
    conversation: UUID | None = None,
    *,
    tenant: UUID = TENANT,
    actor: UUID = ACTOR,
    question: str = "付款由谁审批？",
) -> dict[str, str]:
    return {
        "tenant_id": str(tenant),
        "actor_id": str(actor),
        "conversation_id": str(conversation or uuid4()),
        "question": question,
    }


def _parse_sse(raw: str) -> list[tuple[str, dict[str, object]]]:
    parsed: list[tuple[str, dict[str, object]]] = []
    for block in raw.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        event_name = ""
        data_line = ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_line = line.split(":", 1)[1].strip()
        parsed.append((event_name, json.loads(data_line)))
    return parsed


def _answer(text: str) -> RAGAnswer:
    if text == INSUFFICIENT_KNOWLEDGE:
        sources: tuple[RAGSource, ...] = ()
    else:
        sources = (RAGSource(DOC, CHUNK, 0, "证据", 0.9, "fake", 0, 2),)
    return RAGAnswer(text, sources, len(sources), "fake", retrieval_strategy="vector", effective_strategy="vector")
