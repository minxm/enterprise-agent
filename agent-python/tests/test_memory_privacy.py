"""Privacy of the Day41 memory sentinel across prompts, callbacks, logs, and SSE."""

from __future__ import annotations

import json
import logging
from uuid import UUID

import httpx
from langchain_core.callbacks import BaseCallbackHandler

from app.agents.knowledge_qa.builder import build_knowledge_qa_graph
from app.agents.knowledge_qa.run_context import RunContext
from app.agents.knowledge_qa.service import KnowledgeQAService
from app.knowledge.prompts import SYSTEM_PROMPT, build_user_prompt
from app.knowledge.rag import RAGService
from app.knowledge.retrieval import RetrievalHit
from app.memory.context import MemoryAccessContext
from app.memory.service import MemorySnippet
from app.models.chat import ChatAPIError, ChatClient

SENTINEL = "MEMORY_SENTINEL_DAY41_91ab"
TENANT = UUID("00000000-0000-4000-8000-00000000e431")
ACTOR = UUID("00000000-0000-4000-8000-00000000a431")
DOC = UUID("00000000-0000-4000-8000-0000000000d8")
CHUNK = UUID("00000000-0000-4000-8000-0000000000c8")
PASSAGE = "付款由部门负责人审批。"


class _Callbacks(BaseCallbackHandler):
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.chat_model_starts = 0
        self.chat_model_ends = 0
        self.llm_errors = 0

    def on_chain_start(self, serialized, inputs, **kwargs):  # type: ignore[no-untyped-def]
        del serialized
        self._keep(inputs, kwargs)

    def on_chain_end(self, outputs, **kwargs):  # type: ignore[no-untyped-def]
        self._keep(outputs, kwargs)

    def on_chat_model_start(self, serialized, messages, **kwargs):  # type: ignore[no-untyped-def]
        del serialized
        self.chat_model_starts += 1
        self._keep(messages, kwargs)

    def on_chat_model_end(self, response, **kwargs):  # type: ignore[no-untyped-def]
        self.chat_model_ends += 1
        self._keep(response, kwargs)

    def on_llm_error(self, error, **kwargs):  # type: ignore[no-untyped-def]
        self.llm_errors += 1
        self._keep(error, kwargs)

    def _keep(self, payload: object, kwargs: dict[str, object]) -> None:
        visible = {key: value for key, value in kwargs.items() if key != "run_id"}
        self.parts.append(repr(payload))
        self.parts.append(repr(visible))


def test_sentinel_reaches_the_model_prompt_and_not_the_callbacks_or_sse(caplog):
    prompt = build_user_prompt("付款由谁审批？", [_unused_source()], memories=(SENTINEL,))
    assert SENTINEL in prompt
    assert SENTINEL not in SYSTEM_PROMPT

    chat = _Chat()
    rag = RAGService(_Retriever(), chat)
    memory = _Memory()
    capture = _Callbacks()
    graph = build_knowledge_qa_graph(rag)
    with caplog.at_level(logging.DEBUG):
        state = graph.invoke(
            {"tenant_id": TENANT, "question": "付款由谁审批？", "requested_intent": "knowledge_qa"},
            {"callbacks": [capture]},
            context=RunContext(memories=(SENTINEL,)),
        )
        events = list(
            KnowledgeQAService(rag, memory=memory).stream_events(TENANT, "付款由谁审批？", actor_id=ACTOR)
        )
    assert chat.users and SENTINEL in chat.users[-1]
    assert all(SENTINEL not in system for system in chat.systems)
    assert SENTINEL not in "\n".join(capture.parts)
    assert capture.chat_model_starts == 0
    assert capture.chat_model_ends == 0
    assert capture.llm_errors == 0
    assert SENTINEL not in caplog.text
    assert SENTINEL not in repr(events)
    assert SENTINEL not in str(state.get("question"))
    assert "memory" not in state
    history_question = state["turns"][-1]["question"]
    assert SENTINEL not in history_question


def test_chat_client_error_does_not_echo_the_prompt_or_provider_body():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert SENTINEL in payload["messages"][1]["content"]
        assert SENTINEL not in payload["messages"][0]["content"]
        return httpx.Response(500, text=SENTINEL, headers={"x-siliconcloud-trace-id": "trace-day41"})

    client = ChatClient(
        api_key="sk-test-not-a-real-key",
        base_url="https://example.test/v1",
        model="fixture-chat",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _seconds: None,
    )
    try:
        client.complete(system=SYSTEM_PROMPT, user=build_user_prompt("付款由谁审批？", [], memories=(SENTINEL,)))
    except ChatAPIError as exc:
        message = str(exc)
    else:
        raise AssertionError("provider failure was not raised")
    assert SENTINEL not in message
    assert "sk-test" not in message


class _Chat:
    def __init__(self) -> None:
        self.systems: list[str] = []
        self.users: list[str] = []
        self.model_name = "fixture-chat"

    def complete(self, *, system: str, user: str) -> str:
        self.systems.append(system)
        self.users.append(user)
        return PASSAGE


class _Retriever:
    def search(self, tenant_id: object, question: object, *, top_k: object = 5):
        del tenant_id, question, top_k
        return [RetrievalHit(DOC, CHUNK, 0, PASSAGE, 0, len(PASSAGE), 0.9, "fake")]


class _Memory:
    def recall(self, ctx: MemoryAccessContext):
        assert ctx.allow_tenant_write is False
        return (MemorySnippet(UUID("00000000-0000-4000-8000-00000000d431"), "user", "preference", SENTINEL),)


def _unused_source():
    from app.knowledge.rag import RAGSource

    return RAGSource(DOC, CHUNK, 0, PASSAGE, 0.9, "fake", 0, len(PASSAGE))
