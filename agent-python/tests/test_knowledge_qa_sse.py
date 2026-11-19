"""SSE workflow events for Knowledge QA.

Model calls are scripted. The production FastAPI app is not given an SSE route.
"""

from __future__ import annotations

import asyncio
import json
import threading
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from app.agents.knowledge_qa.events import encode_sse
from app.agents.knowledge_qa.nodes import UNSUPPORTED_ANSWER
from app.agents.knowledge_qa.service import KnowledgeQAService
from app.agents.knowledge_qa.sse import iter_sse_bytes, open_knowledge_qa_sse
from app.agents.knowledge_qa.threads import thread_id_for
from app.core.request_context import reset_request_id, set_request_id
from app.knowledge.prompts import INSUFFICIENT_KNOWLEDGE
from app.knowledge.rag import RAGAnswer, RAGSource
from app.main import app as production_app

TENANT = UUID("00000000-0000-4000-8000-00000000e281")
OTHER = UUID("00000000-0000-4000-8000-00000000e282")
DOC = UUID("00000000-0000-4000-8000-0000000000d8")
CHUNK_A = UUID("00000000-0000-4000-8000-0000000000a8")
CHUNK_B = UUID("00000000-0000-4000-8000-0000000000b8")


class ScriptedRAG:
    def __init__(self, results: list[RAGAnswer | Exception]) -> None:
        self.results = list(results)
        self.calls: list[str] = []
        self._default_top_k = 5
        self._default_strategy = "vector"

    def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None) -> RAGAnswer:
        del tenant_id, top_k, strategy
        self.calls.append(str(question))
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_production_app_has_no_sse_route():
    """Public routes stay closed. Knowledge QA stays on the internal gateway."""
    paths = _production_paths()
    assert "/internal/v1/knowledge-qa/stream" in paths
    assert "/internal/v1/knowledge-qa/conversations/{conversation_id}/messages" in paths
    assert "/api/v1/agent/knowledge-qa/stream" not in paths
    knowledge = sorted(path for path in paths if "knowledge-qa" in path or path.endswith("/sse"))
    assert knowledge == [
        "/internal/v1/knowledge-qa/conversations/{conversation_id}/messages",
        "/internal/v1/knowledge-qa/stream",
    ]


def test_sse_success_events_use_one_request_id_and_standard_frames():
    rag = ScriptedRAG([_answer("部门负责人审批。[1][2]", sources=(_source(CHUNK_A, "部门负责人审批。"), _source(CHUNK_B, "总经理审批。")))])
    service = KnowledgeQAService(rag)
    token = set_request_id("req-sse-1")
    try:
        events = list(service.stream_events(TENANT, "付款由谁审批？", retrieval_strategy="vector"))
    finally:
        reset_request_id(token)

    names = [name for name, _data in events]
    assert names[0] == "run_started"
    assert "node_started" in names
    assert "node_completed" in names
    assert names.count("retrieval_completed") == 1
    assert names.count("message") == 1
    assert names.count("run_completed") == 1
    assert "run_failed" not in names
    assert "rerank_completed" not in names
    assert "retrieval_started" not in names
    assert "generation_started" not in names
    assert rag.calls == ["付款由谁审批？"]

    started = events[0][1]
    assert started["request_id"] == "req-sse-1"
    assert started["status"] == "started"
    assert "timestamp" in started
    assert all(data.get("request_id") == "req-sse-1" for _name, data in events)

    nodes = [data["node"] for name, data in events if name == "node_started"]
    assert nodes == ["validate_input", "route_intent", "knowledge_qa", "format_response"]
    completed = [(data["node"], data["status"]) for name, data in events if name == "node_completed"]
    assert ("knowledge_qa", "answered") in completed
    knowledge_index = names.index("retrieval_completed")
    qa_start = next(index for index, (name, data) in enumerate(events) if name == "node_started" and data["node"] == "knowledge_qa")
    qa_done = next(index for index, (name, data) in enumerate(events) if name == "node_completed" and data["node"] == "knowledge_qa")
    assert qa_start < knowledge_index < qa_done
    assert names[knowledge_index - 1] == "node_started"
    retrieval = events[knowledge_index][1]
    assert retrieval["retrieval_count"] == 2
    assert retrieval["requested_strategy"] == "vector"
    assert retrieval["effective_strategy"] == "vector"

    message = dict(events[-2][1])
    assert events[-2][0] == "message"
    assert message["answer"] == "部门负责人审批。[1][2]"
    assert message["status"] == "answered"
    assert [item["chunk_id"] for item in message["sources"]] == [str(CHUNK_A), str(CHUNK_B)]
    assert message["sources"][1]["rerank_score"] == 3.5
    assert "turns" not in message
    assert events[-1] == ("run_completed", {"status": "answered", "request_id": "req-sse-1", "conversation_id": None})

    raw = b"".join(encode_sse(name, data) for name, data in events).decode("utf-8")
    assert raw.endswith("\n\n")
    parsed = _parse_sse(raw)
    assert [name for name, _data in parsed] == names
    assert parsed[-2][1]["answer"] == "部门负责人审批。[1][2]"


def test_rerank_completed_and_degraded_events_do_not_repeat_retrieval():
    done = ScriptedRAG([
        _answer(
            "已重排。[1]",
            strategy="hybrid_rerank",
            candidate_count=4,
            context_count=1,
        )
    ])
    done_service = KnowledgeQAService(done)
    done_events = list(done_service.stream_events(TENANT, "付款由谁审批？", retrieval_strategy="hybrid_rerank"))
    done_names = [name for name, _data in done_events]
    rerank = next(data for name, data in done_events if name == "rerank_completed")
    assert rerank == {"candidate_count": 4, "final_count": 1, "request_id": done_events[0][1]["request_id"]}
    assert done_names.count("rerank_degraded") == 0
    assert done.calls == ["付款由谁审批？"]

    degraded = ScriptedRAG([
        _answer(
            "降级后的回答",
            strategy="hybrid_rerank",
            effective="hybrid",
            degraded=True,
            degradation_reason="RerankAPIError: password=secret sk-live trace_id=trace-28",
            candidate_count=4,
            context_count=1,
        )
    ])
    degraded_events = list(KnowledgeQAService(degraded).stream_events(TENANT, "付款由谁审批？", retrieval_strategy="hybrid_rerank"))
    degraded_payload = next(data for name, data in degraded_events if name == "rerank_degraded")
    assert degraded_payload["degradation_reason"] == "RerankAPIError"
    assert degraded_payload["requested_strategy"] == "hybrid_rerank"
    assert degraded_payload["effective_strategy"] == "hybrid"
    assert "rerank_completed" not in [name for name, _data in degraded_events]
    encoded = b"".join(encode_sse(name, data) for name, data in degraded_events)
    assert b"sk-live" not in encoded
    assert b"password" not in encoded
    assert degraded.calls == ["付款由谁审批？"]


def test_refusal_unsupported_and_invalid_input_complete_once():
    refusal = list(
        KnowledgeQAService(ScriptedRAG([_answer(INSUFFICIENT_KNOWLEDGE, sources=(), retrieval_count=0)])).stream_events(
            TENANT,
            "班车几点发？",
        )
    )
    assert refusal[-2][1]["status"] == "refused"
    assert refusal[-2][1]["answer"] == INSUFFICIENT_KNOWLEDGE
    assert refusal[-2][1]["sources"] == []
    assert refusal[-1][0] == "run_completed"
    assert [name for name, _data in refusal].count("run_completed") == 1

    unsupported_rag = ScriptedRAG([_answer("不应执行")])
    unsupported = list(
        KnowledgeQAService(unsupported_rag).stream_events(TENANT, "请审批合同", requested_intent="contract_approval")
    )
    assert unsupported_rag.calls == []
    assert unsupported[-2][1]["status"] == "unsupported"
    assert unsupported[-2][1]["answer"] == UNSUPPORTED_ANSWER
    assert unsupported[-1] == (
        "run_completed",
        {"status": "unsupported", "request_id": unsupported[0][1]["request_id"], "conversation_id": None},
    )

    invalid_rag = ScriptedRAG([_answer("不应执行")])
    invalid = list(KnowledgeQAService(invalid_rag).stream_events(TENANT, "  "))
    assert invalid_rag.calls == []
    assert invalid[-2][1]["status"] == "invalid_input"
    assert invalid[-2][1]["answer"] == ""
    assert invalid[-1][1]["status"] == "invalid_input"
    assert "knowledge_qa" not in [data["node"] for name, data in invalid if name == "node_started"]
    assert [name for name, _data in invalid].count("run_completed") == 1
    assert "run_failed" not in [name for name, _data in invalid]


def test_rag_exception_sends_one_safe_failure_event():
    rag = ScriptedRAG([RuntimeError("postgres://agent_app:password@127.0.0.1/agent sk-live-secret")])
    events = list(KnowledgeQAService(rag).stream_events(TENANT, "付款由谁审批？"))
    names = [name for name, _data in events]
    assert names.count("run_failed") == 1
    assert "run_completed" not in names
    assert "message" not in names
    failed = events[-1][1]
    assert failed["error_code"] == "rag_failed"
    assert failed["safe_error_message"] == "RuntimeError"
    encoded = b"".join(encode_sse(name, data) for name, data in events)
    assert b"sk-live-secret" not in encoded
    assert b"password" not in encoded
    assert b"127.0.0.1" not in encoded


def test_sse_response_headers_disable_buffering():
    rag = ScriptedRAG([_answer("部门负责人审批。")])
    service = KnowledgeQAService(rag)
    probe = FastAPI()

    @probe.get("/internal-sse-probe")
    async def probe_route():
        return open_knowledge_qa_sse(service, tenant_id=TENANT, question="付款由谁审批？")

    with TestClient(probe) as client:
        with client.stream("GET", "/internal-sse-probe") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert "no-cache" in response.headers["cache-control"]
            assert "no-transform" in response.headers["cache-control"]
            assert response.headers["x-accel-buffering"] == "no"
            body = b"".join(response.iter_bytes())
    parsed = _parse_sse(body.decode("utf-8"))
    assert parsed[0][0] == "run_started"
    assert parsed[-1][0] == "run_completed"
    assert b"sk-" not in body


def test_parallel_threads_keep_their_own_request_ids():
    rag = _QuestionRAG({"问题甲": "回答甲", "问题乙": "回答乙"})
    service = KnowledgeQAService(rag)
    found: list[tuple[str, str, str]] = []
    errors: list[BaseException] = []

    def run(request_id: str, question: str) -> None:
        token = set_request_id(request_id)
        try:
            for name, data in service.stream_events(TENANT, question):
                if name == "message":
                    found.append((request_id, str(data["request_id"]), str(data["answer"])))
        except BaseException as exc:
            errors.append(exc)
        finally:
            reset_request_id(token)

    first = threading.Thread(target=run, args=("req-a", "问题甲"))
    second = threading.Thread(target=run, args=("req-b", "问题乙"))
    first.start()
    second.start()
    first.join(5)
    second.join(5)

    assert errors == []
    assert sorted(found) == [("req-a", "req-a", "回答甲"), ("req-b", "req-b", "回答乙")]


def test_same_thread_stays_busy_and_the_lock_is_released():
    started = threading.Event()
    release = threading.Event()
    rag = _BlockingRAG(started, release, [_answer("第一轮")])
    service = KnowledgeQAService(rag, checkpointer=MemorySaver())
    conversation = uuid4()

    def run_first() -> None:
        list(service.stream_events(TENANT, "第一题", conversation_id=conversation))

    worker = threading.Thread(target=run_first)
    worker.start()
    assert started.wait(5)
    try:
        busy = list(service.stream_events(TENANT, "第二题", conversation_id=conversation))
    finally:
        release.set()
    worker.join(5)

    assert [name for name, _data in busy] == ["run_failed"]
    assert busy[0][1]["error_code"] == "thread_busy"
    assert rag.calls == ["第一题"]
    lock = service._locks.lock_for(thread_id_for(TENANT, conversation))
    assert lock.acquire(blocking=False)
    lock.release()


def test_disconnect_and_failure_release_the_thread_lock():
    started = threading.Event()
    release = threading.Event()
    rag = _BlockingRAG(started, release, [_answer("中断后的回答")])
    service = KnowledgeQAService(rag, checkpointer=MemorySaver())
    conversation = uuid4()

    async def consume() -> None:
        agen = iter_sse_bytes(
            service,
            tenant_id=TENANT,
            question="第一题",
            conversation_id=conversation,
            heartbeat_seconds=0.05,
            max_seconds=5,
        )
        async for chunk in agen:
            if b"event: node_started" in chunk and b"knowledge_qa" in chunk:
                await asyncio.sleep(0.05)
                await agen.aclose()
                return

    async def main() -> None:
        task = asyncio.create_task(consume())
        try:
            assert await asyncio.to_thread(started.wait, 5)
        finally:
            release.set()
            await task

    asyncio.run(main())
    lock = service._locks.lock_for(thread_id_for(TENANT, conversation))
    assert lock.acquire(blocking=False)
    lock.release()

    failed = ScriptedRAG([RuntimeError("password=secret")])
    failed_service = KnowledgeQAService(failed, checkpointer=MemorySaver())
    failed_conversation = uuid4()
    list(failed_service.stream_events(TENANT, "会失败", conversation_id=failed_conversation))
    failed_lock = failed_service._locks.lock_for(thread_id_for(TENANT, failed_conversation))
    assert failed_lock.acquire(blocking=False)
    failed_lock.release()


def test_heartbeat_is_sent_while_the_sync_graph_is_blocked():
    started = threading.Event()
    release = threading.Event()
    rag = _BlockingRAG(started, release, [_answer("心跳之后")])
    service = KnowledgeQAService(rag)

    async def main() -> list[bytes]:
        chunks: list[bytes] = []
        agen = iter_sse_bytes(
            service,
            tenant_id=TENANT,
            question="付款由谁审批？",
            heartbeat_seconds=0.05,
            max_seconds=5,
        )
        async for chunk in agen:
            chunks.append(chunk)
            if b": heartbeat" in chunk:
                release.set()
            if b"event: run_completed" in chunk:
                break
        return chunks

    chunks = asyncio.run(main())
    assert any(chunk.startswith(b": heartbeat\n\n") for chunk in chunks)
    assert any(b"event: run_completed" in chunk for chunk in chunks)


def test_stream_persists_checkpoint_state_for_a_later_ask():
    rag = _QuestionRAG({"第一题": "第一轮", "第二题": "第二轮", "别人的问题": "其他租户"})
    service = KnowledgeQAService(rag, checkpointer=MemorySaver())
    conversation = uuid4()
    token = set_request_id("req-checkpoint")
    try:
        first = list(service.stream_events(TENANT, "第一题", conversation_id=conversation))
        restored = service.ask(TENANT, "第二题", conversation_id=conversation)
        other = service.ask(OTHER, "别人的问题", conversation_id=conversation)
    finally:
        reset_request_id(token)

    assert first[-1][0] == "run_completed"
    assert restored.answer == "第二轮"
    assert [turn["question"] for turn in restored.turns] == ["第一题", "第二题"]
    assert restored.request_id == "req-checkpoint"
    assert other.answer == "其他租户"
    assert [turn["question"] for turn in other.turns] == ["别人的问题"]


class _BlockingRAG:
    def __init__(self, started: threading.Event, release: threading.Event, results: list[RAGAnswer]) -> None:
        self.started = started
        self.release = release
        self.results = list(results)
        self.calls: list[str] = []
        self._default_top_k = 5
        self._default_strategy = "vector"

    def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None) -> RAGAnswer:
        del tenant_id, top_k, strategy
        self.calls.append(str(question))
        self.started.set()
        assert self.release.wait(5)
        return self.results.pop(0)


class _QuestionRAG:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[str] = []
        self._default_top_k = 5
        self._default_strategy = "vector"

    def ask(self, tenant_id: object, question: object, *, top_k: object = 5, strategy: object = None) -> RAGAnswer:
        del tenant_id, top_k, strategy
        text = str(question)
        self.calls.append(text)
        return _answer(self.answers[text])


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
        assert event_name
        assert data_line
        parsed.append((event_name, json.loads(data_line)))
    return parsed


def _answer(
    text: str,
    *,
    sources: tuple[RAGSource, ...] | None = None,
    retrieval_count: int | None = None,
    strategy: str = "vector",
    effective: str | None = None,
    degraded: bool = False,
    degradation_reason: str | None = None,
    candidate_count: int = 0,
    context_count: int | None = None,
) -> RAGAnswer:
    selected = (_source(CHUNK_A, f"{text}证据"),) if sources is None else sources
    return RAGAnswer(
        answer=text,
        sources=selected,
        retrieval_count=len(selected) if retrieval_count is None else retrieval_count,
        model="fake",
        retrieval_strategy=strategy,
        effective_strategy=effective or strategy,
        degraded=degraded,
        degradation_reason=degradation_reason,
        candidate_count=candidate_count,
        context_count=len(selected) if context_count is None else context_count,
    )


def _production_paths() -> list[str]:
    paths: list[str] = []
    for route in production_app.routes:
        path = getattr(route, "path", None)
        if isinstance(path, str) and path:
            paths.append(path)
        original = getattr(route, "original_router", None)
        context = getattr(route, "include_context", None)
        prefix = getattr(context, "prefix", "") if context is not None else ""
        if original is None:
            continue
        for nested in original.routes:
            nested_path = getattr(nested, "path", "")
            if isinstance(nested_path, str):
                paths.append(f"{prefix}{nested_path}")
    return paths


def _source(chunk_id: UUID, content: str) -> RAGSource:
    return RAGSource(
        DOC,
        chunk_id,
        0,
        content,
        0.9,
        "fake-embedding",
        0,
        len(content),
        rerank_score=3.5 if chunk_id == CHUNK_B else None,
    )
