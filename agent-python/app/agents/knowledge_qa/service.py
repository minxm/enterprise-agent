"""Run the compiled Knowledge QA graph.

Without a checkpointer, each ask() starts from a new state. With a checkpointer,
the service derives a thread id from the trusted tenant and conversation, checks
the stored tenant and actor, and passes that thread id in RunnableConfig. A
client-supplied thread id is not accepted.

stream_events() uses the same thread rules. It yields workflow events and does
not replace ask().
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

from langgraph.errors import GraphDrained
from langgraph.runtime import RunControl
from psycopg import Error as PsycopgError
from psycopg_pool import PoolClosed, PoolTimeout

from app.agents.knowledge_qa.builder import build_knowledge_qa_graph
from app.agents.knowledge_qa.events import public_text
from app.agents.knowledge_qa.intent import RECALL_INTENTS, classify_intent
from app.agents.knowledge_qa.lease import ActiveLease, LeaseBridge, LeaseLoop
from app.agents.knowledge_qa.nodes import _failure, format_response, validate_input
from app.agents.knowledge_qa.run_context import RunContext
from app.memory.context import MemoryAccessContext
from app.agents.knowledge_qa.threads import ThreadAccessError, assert_thread_access, parse_uuid, thread_id_for
from app.persistence.fence_context import (
    EXECUTION_TOKEN_KEY,
    fence_scope,
    register_execution,
    tenant_scope,
    unregister_execution,
)
from app.persistence.fenced_saver import FencedPostgresSaver, FencingViolation
from app.persistence.redis_thread_lock import LOCK_RENEW_INTERVAL_SECONDS, LockAcquired, LockBusy
from app.tools.contract_context import bind_actor_ticket, reset_actor_ticket
from app.core.request_context import current_request_id, reset_request_id, set_request_id

logger = logging.getLogger(__name__)
_UNSET = object()
_CHECKPOINT_ERRORS = (PsycopgError, PoolTimeout, PoolClosed)
_TERMINAL_OK = {"answered", "refused", "unsupported", "invalid_input"}
Event = tuple[str, dict[str, object]]


@dataclass(frozen=True)
class HistorySource:
    chunk_id: str
    content: str


@dataclass(frozen=True)
class HistoryMessage:
    role: str
    content: str
    status: str
    source_ids: tuple[str, ...] = ()
    sources: tuple[HistorySource, ...] = ()


@dataclass(frozen=True)
class HistoryResult:
    messages: tuple[HistoryMessage, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class KnowledgeQAResult:
    status: str
    answer: str
    sources: tuple[dict[str, object], ...]
    retrieval_count: int
    requested_strategy: str | None
    effective_strategy: str | None
    degraded: bool
    degradation_reason: str | None
    error_code: str | None
    error_message: str | None
    request_id: str | None
    turns: tuple[dict[str, object], ...] = ()


class KnowledgeQAService:
    def __init__(
        self,
        rag_service: object,
        *,
        checkpointer: object | None = None,
        contract_tool: object | None = None,
        memory: object | None = None,
        thread_lock: object | None = None,
        fence_store: object | None = None,
        lease_loop: object | None = None,
        renew_interval_seconds: float = LOCK_RENEW_INTERVAL_SECONDS,
    ) -> None:
        default_top_k = getattr(rag_service, "_default_top_k", 5)
        default_strategy = getattr(rag_service, "_default_strategy", "vector")
        self._default_top_k = default_top_k if isinstance(default_top_k, int) else 5
        self._default_strategy = default_strategy if isinstance(default_strategy, str) else "vector"
        self._rag = rag_service
        self._memory = memory
        self._checkpointer = checkpointer
        self._thread_lock = thread_lock
        self._fence_store = fence_store
        if isinstance(renew_interval_seconds, bool) or not isinstance(renew_interval_seconds, (int, float)):
            raise ValueError("续租间隔不合法")
        if not 0 < float(renew_interval_seconds) <= LOCK_RENEW_INTERVAL_SECONDS:
            raise ValueError("续租间隔不合法")
        self._renew_interval = float(renew_interval_seconds)
        self._owned_loop: LeaseLoop | None = None
        if thread_lock is not None and lease_loop is None:
            self._owned_loop = LeaseLoop()
            lease_loop = self._owned_loop.loop
        self._bridge = LeaseBridge(lease_loop) if lease_loop is not None else None
        self._graph = build_knowledge_qa_graph(
            rag_service,
            contract_tool=contract_tool,
            default_top_k=self._default_top_k,
            default_strategy=self._default_strategy,
            checkpointer=checkpointer,
        )

    def close(self) -> None:
        owned = self._owned_loop
        self._owned_loop = None
        if owned is not None:
            owned.close()

    def ask(
        self,
        tenant_id: object,
        question: object,
        *,
        top_k: object = _UNSET,
        retrieval_strategy: str | None = None,
        requested_intent: str | None = None,
        conversation_id: object = None,
        actor_id: object = None,
        actor_ticket: str | None = None,
        allow_tenant_read: bool = False,
    ) -> KnowledgeQAResult:
        ticket = bind_actor_ticket(actor_ticket)
        try:
            return self._ask(
                tenant_id,
                question,
                top_k,
                retrieval_strategy,
                requested_intent,
                conversation_id,
                actor_id,
                allow_tenant_read,
            )
        finally:
            reset_actor_ticket(ticket)

    def _ask(
        self,
        tenant_id: object,
        question: object,
        top_k: object,
        retrieval_strategy: str | None,
        requested_intent: str | None,
        conversation_id: object,
        actor_id: object,
        allow_tenant_read: bool,
    ) -> KnowledgeQAResult:
        payload = _payload(tenant_id, question, top_k, retrieval_strategy, requested_intent)
        tenant = parse_uuid(tenant_id)
        actor = parse_uuid(actor_id) if actor_id is not None else None
        if self._checkpointer is None:
            return self._invoke(payload, None, self._recall_context(payload, tenant, actor, allow_tenant_read), None, None)
        conversation = parse_uuid(conversation_id)
        if tenant is None or conversation is None or (actor_id is not None and actor is None):
            return self._reject_scope(payload, tenant, conversation, actor_id, actor)
        control = RunControl()
        with self._open_thread(tenant, conversation, actor, execute=True, control=control) as (kind, config, lease):
            if kind == "busy":
                return _result(_failure("failed", "thread_busy", "同一个会话正在执行，请等待本次完成后再继续。"))
            if kind == "unavailable":
                return _checkpoint_unavailable()
            if kind == "lease_unavailable":
                return _lease_failure("lease_unavailable")
            if kind == "forbidden":
                return _result(_failure("failed", "thread_forbidden", "无权访问该会话。"))
            return self._invoke(payload, config, self._recall_context(payload, tenant, actor, allow_tenant_read), control, lease)

    def stream_events(
        self,
        tenant_id: object,
        question: object,
        *,
        top_k: object = _UNSET,
        retrieval_strategy: str | None = None,
        requested_intent: str | None = None,
        conversation_id: object = None,
        actor_id: object = None,
        actor_ticket: str | None = None,
        allow_tenant_read: bool = False,
        control: RunControl | None = None,
    ) -> Iterator[Event]:
        """Yield workflow events for one trusted call.

        The caller owns tenant_id, actor_id, and conversation_id. thread_id is
        never taken from the caller. The thread lock is held until this
        generator closes, including when the consumer stops early.
        """
        request_id, token = _bind_request_id()
        ticket = bind_actor_ticket(actor_ticket)
        try:
            payload = _payload(tenant_id, question, top_k, retrieval_strategy, requested_intent)
            tenant = parse_uuid(tenant_id)
            actor = parse_uuid(actor_id) if actor_id is not None else None
            if self._checkpointer is None:
                context = self._recall_context(payload, tenant, actor, allow_tenant_read)
                yield from self._play(payload, None, request_id, _conversation_text(conversation_id), control, context, None)
                return
            conversation = parse_uuid(conversation_id)
            if tenant is None or conversation is None or (actor_id is not None and actor is None):
                yield from self._play_rejection(payload, tenant, conversation, actor_id, actor, request_id)
                return
            if control is None:
                control = RunControl()
            with self._open_thread(tenant, conversation, actor, execute=True, control=control) as (kind, config, lease):
                if kind == "busy":
                    yield _failed(request_id, "thread_busy", "同一个会话正在执行，请等待本次完成后再继续。")
                    return
                if kind == "unavailable":
                    yield _failed(request_id, "checkpoint_unavailable", "工作流状态暂时不可用")
                    return
                if kind == "lease_unavailable":
                    yield _failed(request_id, "lease_unavailable", "会话锁暂时不可用")
                    return
                if kind == "forbidden":
                    yield _failed(request_id, "thread_forbidden", "无权访问该会话。")
                    return
                context = self._recall_context(payload, tenant, actor, allow_tenant_read)
                yield from self._play(payload, config, request_id, str(conversation), control, context, lease)
        finally:
            reset_actor_ticket(ticket)
            if token is not None:
                reset_request_id(token)

    def read_history(
        self,
        tenant_id: object,
        conversation_id: object,
        actor_id: object = None,
    ) -> HistoryResult:
        """Read completed turns for a trusted tenant, actor, and conversation.

        thread_id is derived here. The caller cannot supply one. This does not
        invoke the graph or RAG.
        """
        tenant = parse_uuid(tenant_id)
        conversation = parse_uuid(conversation_id)
        actor = parse_uuid(actor_id) if actor_id is not None else None
        if tenant is None or conversation is None or (actor_id is not None and actor is None):
            return HistoryResult(error_code="invalid_input", error_message="请求参数不合法")
        if self._checkpointer is None:
            return HistoryResult(error_code="checkpoint_unavailable", error_message="工作流状态暂时不可用")
        with self._open_thread(tenant, conversation, actor, execute=False, control=None) as (kind, config, _lease):
            if kind == "busy":
                return HistoryResult(error_code="thread_busy", error_message="同一个会话正在执行，请等待本次完成后再继续。")
            if kind == "unavailable":
                return HistoryResult(error_code="checkpoint_unavailable", error_message="工作流状态暂时不可用")
            if kind == "lease_unavailable":
                return HistoryResult(error_code="lease_unavailable", error_message="会话锁暂时不可用")
            if kind == "forbidden":
                return HistoryResult(error_code="thread_forbidden", error_message="会话不存在")
            try:
                saved = self._checkpointer.get_tuple(config)  # type: ignore[attr-defined]
            except _CHECKPOINT_ERRORS:
                return HistoryResult(error_code="checkpoint_unavailable", error_message="工作流状态暂时不可用")
            except Exception as exc:
                logger.warning("knowledge qa history unreadable error=%s", exc.__class__.__name__)
                return HistoryResult(error_code="checkpoint_unavailable", error_message="工作流状态暂时不可用")
            if saved is None:
                return HistoryResult()
            try:
                messages = _project_turns(saved)
            except _HistoryShapeError:
                return HistoryResult(error_code="checkpoint_unavailable", error_message="工作流状态暂时不可用")
            contents = _load_citation_text(self._rag, tenant, [item for message in messages for item in message.source_ids])
            return HistoryResult(messages=tuple(_apply_citation_text(messages, contents)))

    def _play_rejection(
        self,
        payload: dict[str, object],
        tenant: UUID | None,
        conversation: UUID | None,
        actor_id: object,
        actor: UUID | None,
        request_id: str,
    ) -> Iterator[Event]:
        state = _rejection_state(
            payload,
            tenant,
            conversation,
            actor_id,
            actor,
            self._default_top_k,
            self._default_strategy,
        )
        conversation_text = str(conversation) if conversation is not None else None
        if state.get("status") == "invalid_input":
            yield _started(request_id, conversation_text)
            yield _message(state, request_id)
            yield _completed(str(state.get("status")), request_id, conversation_text)
            return
        yield _failed(
            request_id,
            str(state.get("error_code") or "invalid_input"),
            public_text(state.get("error_message"), "请求无法执行"),
        )

    def _play(
        self,
        payload: dict[str, object],
        config: dict[str, object] | None,
        request_id: str,
        conversation_id: str | None,
        control: RunControl | None,
        run_context: RunContext,
        lease: ActiveLease | None,
    ) -> Iterator[Event]:
        finished = False
        try:
            yield _started(request_id, conversation_id)
            final: dict[str, object] | None = None
            drained = False
            try:
                stream_kwargs: dict[str, object] = {"stream_mode": ["tasks", "custom"]}
                if config is not None:
                    stream_kwargs["durability"] = "sync"
                if control is not None:
                    stream_kwargs["control"] = control
                stream_kwargs["context"] = run_context
                for item in self._graph.stream(payload, config, **stream_kwargs):
                    if not isinstance(item, tuple) or len(item) != 2:
                        continue
                    mode, data = item
                    if mode == "tasks" and isinstance(data, dict):
                        event = _node_event(data, request_id)
                        if event is not None:
                            yield event
                        if "result" in data and data.get("name") == "format_response" and isinstance(data.get("result"), dict):
                            final = data["result"]
                    elif mode == "custom" and isinstance(data, dict):
                        phase = _phase_event(data, request_id)
                        if phase is not None:
                            yield phase
                    if control is not None and control.drain_requested:
                        drained = True
                        break
            except GraphDrained:
                drained = True
            except FencingViolation:
                yield _failed(request_id, "lease_lost", "会话执行权已丢失")
                finished = True
                return
            except _CHECKPOINT_ERRORS:
                yield _failed(request_id, "checkpoint_unavailable", "工作流状态暂时不可用")
                finished = True
                return
            lost = _lease_event(lease, request_id)
            if lost is not None:
                yield lost
                finished = True
                return
            if drained:
                logger.info("knowledge qa drain observed requestId=%s", request_id)
                yield _failed(request_id, "run_cancelled", "工作流已取消")
                finished = True
                return
            if final is None:
                yield _failed(request_id, "run_failed", "工作流执行失败")
                finished = True
                return
            status = str(final.get("status") or "failed")
            if status not in _TERMINAL_OK:
                yield _failed(
                    request_id,
                    str(final.get("error_code") or "run_failed"),
                    public_text(final.get("error_message"), "工作流执行失败"),
                )
                finished = True
                return
            yield _message(final, request_id)
            yield _completed(status, request_id, conversation_id)
            finished = True
        except Exception as exc:
            if not finished:
                logger.warning("knowledge qa play failed error=%s", exc.__class__.__name__)
                yield _failed(request_id, "run_failed", exc.__class__.__name__)

    def _reject_scope(
        self,
        payload: dict[str, object],
        tenant: UUID | None,
        conversation: UUID | None,
        actor_id: object,
        actor: UUID | None,
    ) -> KnowledgeQAResult:
        state = _rejection_state(
            payload,
            tenant,
            conversation,
            actor_id,
            actor,
            self._default_top_k,
            self._default_strategy,
        )
        return _result(state)

    def _writes_require_fence(self) -> bool:
        return isinstance(self._checkpointer, FencedPostgresSaver)

    @contextmanager
    def _open_thread(
        self,
        tenant: UUID,
        conversation: UUID,
        actor: UUID | None,
        *,
        execute: bool,
        control: RunControl | None,
    ) -> Iterator[tuple[str, dict[str, object] | None, ActiveLease | None]]:
        if self._thread_lock is None or self._bridge is None:
            if self._writes_require_fence():
                yield "lease_unavailable", None, None
                return
            with self._open_without_lease(tenant, conversation, actor) as opened:
                yield opened
            return
        thread_id = thread_id_for(tenant, conversation)
        lease: ActiveLease | None = None
        try:
            try:
                acquired = self._bridge.call(self._thread_lock.acquire(tenant, thread_id))  # type: ignore[attr-defined]
            except Exception as exc:
                logger.warning("knowledge qa lease acquire failed error=%s", type(exc).__name__)
                yield "lease_unavailable", None, None
                return
            if isinstance(acquired, LockBusy):
                yield "busy", None, None
                return
            if not isinstance(acquired, LockAcquired):
                yield "lease_unavailable", None, None
                return
            lease = ActiveLease(
                self._thread_lock,
                self._bridge,
                tenant,
                thread_id,
                acquired.token,
                self._renew_interval,
                control if execute else None,
            )
            if execute:
                try:
                    lease.start()
                except Exception as exc:
                    logger.warning("knowledge qa renew start failed error=%s", type(exc).__name__)
                    lease.reason = "lease_unavailable"
                    lease.lost.set()
                    yield "lease_unavailable", None, None
                    return
            with tenant_scope(tenant):
                config = _thread_config(tenant, conversation, actor, thread_id)
                try:
                    saved = self._checkpointer.get_tuple(config)  # type: ignore[attr-defined]
                except _CHECKPOINT_ERRORS:
                    yield "unavailable", None, None
                    return
                try:
                    assert_thread_access(saved, tenant_id=tenant, actor_id=actor)
                except ThreadAccessError:
                    yield "forbidden", None, None
                    return
                if not execute or not self._writes_require_fence():
                    yield "ready", config, lease
                    return
                if self._fence_store is None:
                    yield "lease_unavailable", None, None
                    return
                try:
                    fence = self._fence_store.next_fence(tenant, thread_id)  # type: ignore[attr-defined]
                except Exception as exc:
                    logger.warning("knowledge qa fence allocation failed error=%s", type(exc).__name__)
                    yield "lease_unavailable", None, None
                    return
                execution_token = secrets.token_urlsafe(32)
                register_execution(execution_token, tenant, thread_id, fence)
                config = _thread_config(tenant, conversation, actor, thread_id, execution_token)
                try:
                    with fence_scope(tenant, thread_id, fence):
                        yield "ready", config, lease
                finally:
                    unregister_execution(execution_token)
        finally:
            if lease is not None:
                lease.close()
                logger.info("knowledge qa thread lease released threadId=%s", thread_id)

    @contextmanager
    def _open_without_lease(
        self,
        tenant: UUID,
        conversation: UUID,
        actor: UUID | None,
    ) -> Iterator[tuple[str, dict[str, object] | None, ActiveLease | None]]:
        """In-memory graphs have no distributed lease and do not allocate a fence."""
        thread_id = thread_id_for(tenant, conversation)
        with tenant_scope(tenant):
            config = _thread_config(tenant, conversation, actor, thread_id)
            try:
                saved = self._checkpointer.get_tuple(config)  # type: ignore[attr-defined]
            except _CHECKPOINT_ERRORS:
                yield "unavailable", None, None
                return
            try:
                assert_thread_access(saved, tenant_id=tenant, actor_id=actor)
            except ThreadAccessError:
                yield "forbidden", None, None
                return
            yield "ready", config, None

    def _invoke(
        self,
        payload: dict[str, object],
        config: dict[str, object] | None,
        run_context: RunContext,
        control: RunControl | None,
        lease: ActiveLease | None,
    ) -> KnowledgeQAResult:
        try:
            state = self._graph.invoke(payload, config, context=run_context, control=control)
        except GraphDrained:
            return _lease_failure(lease.reason if lease is not None else None)
        except FencingViolation:
            return _lease_failure("lease_lost")
        except _CHECKPOINT_ERRORS:
            return _checkpoint_unavailable()
        lost = _lease_event(lease, "")
        if lost is not None:
            return _lease_failure(lease.reason if lease is not None else None)
        return _result(state)

    def _recall_context(
        self,
        payload: dict[str, object],
        tenant: UUID | None,
        actor: UUID | None,
        allow_tenant_read: object,
    ) -> RunContext:
        """Recall before the graph starts. Failures continue with an empty context.

        allow_tenant_write is always false. Recall is not a management operation.
        The snapshot is not refreshed if a memory changes while this run is in flight.
        """
        preview = validate_input(
            payload,
            default_top_k=self._default_top_k,
            default_strategy=self._default_strategy,
        )
        if preview.get("status") == "invalid_input":
            return RunContext()
        intent = classify_intent(preview.get("requested_intent"), preview.get("question"))
        if intent not in RECALL_INTENTS or self._memory is None or tenant is None or actor is None:
            return RunContext()
        granted = allow_tenant_read if type(allow_tenant_read) is bool else False
        try:
            access = MemoryAccessContext.for_trusted_caller(
                tenant_id=tenant,
                actor_id=actor,
                allow_tenant_read=granted,
                allow_tenant_write=False,
                request_id=current_request_id(),
            )
            snippets = self._memory.recall(access)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("memory recall skipped error=%s", exc.__class__.__name__)
            return RunContext()
        lines: list[str] = []
        for item in snippets or ():
            content = getattr(item, "content", None)
            if type(content) is str and content.strip():
                lines.append(content)
        logger.info("memory recall ready intent=%s count=%s", intent, len(lines))
        return RunContext(memories=tuple(lines))


def _payload(
    tenant_id: object,
    question: object,
    top_k: object,
    retrieval_strategy: str | None,
    requested_intent: str | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "tenant_id": tenant_id,
        "question": question,
        "requested_intent": requested_intent,
    }
    if top_k is not _UNSET:
        payload["top_k"] = top_k
    if retrieval_strategy is not None:
        payload["retrieval_strategy"] = retrieval_strategy
    return payload


def _rejection_state(
    payload: dict[str, object],
    tenant: UUID | None,
    conversation: UUID | None,
    actor_id: object,
    actor: UUID | None,
    default_top_k: int,
    default_strategy: str,
) -> dict[str, object]:
    state = validate_input(payload, default_top_k=default_top_k, default_strategy=default_strategy)
    if state.get("status") != "invalid_input":
        if tenant is None:
            state = _failure("invalid_input", "invalid_input", "tenant_id 必须是 UUID")
        elif conversation is None:
            state = _failure("invalid_input", "invalid_input", "conversation_id 必须是 UUID")
        elif actor_id is not None and actor is None:
            state = _failure("invalid_input", "invalid_input", "actor_id 必须是 UUID")
    return format_response({**payload, **state})


def _bind_request_id() -> tuple[str, object]:
    current = current_request_id()
    if current:
        return current, None
    request_id = str(uuid4())
    return request_id, set_request_id(request_id)


def _conversation_text(value: object) -> str | None:
    parsed = parse_uuid(value)
    if parsed is None:
        return None
    return str(parsed)


class _HistoryShapeError(Exception):
    """The stored turns cannot be projected without exposing checkpoint internals."""


def _project_turns(saved: object) -> list[HistoryMessage]:
    checkpoint = getattr(saved, "checkpoint", None)
    if not isinstance(checkpoint, dict):
        raise _HistoryShapeError
    values = checkpoint.get("channel_values")
    if not isinstance(values, dict):
        raise _HistoryShapeError
    raw_turns = values.get("turns", [])
    if raw_turns is None:
        raw_turns = []
    if not isinstance(raw_turns, list):
        raise _HistoryShapeError
    messages: list[HistoryMessage] = []
    for turn in raw_turns:
        if not isinstance(turn, dict):
            raise _HistoryShapeError
        question = turn.get("question")
        answer = turn.get("answer")
        status = turn.get("status")
        source_ids = turn.get("source_ids", [])
        if not isinstance(question, str) or not isinstance(answer, str) or not isinstance(status, str):
            raise _HistoryShapeError
        if not isinstance(source_ids, list) or any(not isinstance(item, str) for item in source_ids):
            raise _HistoryShapeError
        messages.append(HistoryMessage("user", question, status))
        messages.append(HistoryMessage("assistant", answer, status, tuple(source_ids)))
    return messages


def _apply_citation_text(messages: list[HistoryMessage], contents: dict[str, str]) -> list[HistoryMessage]:
    enriched: list[HistoryMessage] = []
    for message in messages:
        sources = tuple(
            HistorySource(chunk_id, contents[chunk_id])
            for chunk_id in message.source_ids
            if contents.get(chunk_id)
        )
        enriched.append(replace(message, sources=sources))
    return enriched


def _load_citation_text(rag: object, tenant: UUID, chunk_ids: list[str]) -> dict[str, str]:
    pool = _knowledge_pool(rag)
    parsed = [item for item in (parse_uuid(chunk_id) for chunk_id in chunk_ids) if item is not None]
    if pool is None or not parsed:
        return {}
    try:
        from app.persistence.postgres import tenant_transaction

        with tenant_transaction(pool, str(tenant)) as connection:
            rows = connection.execute(
                """
                select id::text, content
                from agent.knowledge_chunk
                where tenant_id = %s and id = any(%s::uuid[])
                """,
                (tenant, parsed),
            ).fetchall()
    except Exception as exc:
        logger.warning("knowledge qa citation unreadable error=%s", exc.__class__.__name__)
        return {}
    found: dict[str, str] = {}
    for row in rows:
        chunk_id = row[0] if not isinstance(row, dict) else row.get("id")
        content = row[1] if not isinstance(row, dict) else row.get("content")
        if isinstance(chunk_id, str) and isinstance(content, str) and content.strip():
            found[chunk_id] = content
    return found


def _knowledge_pool(rag: object) -> object | None:
    retriever = getattr(rag, "_retriever", None)
    pool = getattr(retriever, "_pool", None)
    if pool is not None:
        return pool
    strategies = getattr(rag, "_strategies", None)
    vector = getattr(strategies, "_vector", None) if strategies is not None else None
    return getattr(vector, "_pool", None)


def _checkpoint_unavailable() -> KnowledgeQAResult:
    return _result(_failure("failed", "checkpoint_unavailable", "工作流状态暂时不可用"))


def _lease_failure(reason: str | None) -> KnowledgeQAResult:
    if reason == "lease_unavailable":
        return _result(_failure("failed", "lease_unavailable", "会话锁暂时不可用"))
    return _result(_failure("failed", "lease_lost", "会话执行权已丢失"))


def _lease_event(lease: ActiveLease | None, request_id: str) -> Event | None:
    if lease is None or not lease.lost.is_set():
        return None
    if lease.reason == "lease_unavailable":
        return _failed(request_id, "lease_unavailable", "会话锁暂时不可用")
    return _failed(request_id, "lease_lost", "会话执行权已丢失")


def _thread_config(
    tenant: UUID,
    conversation: UUID,
    actor: UUID | None,
    thread_id: str,
    execution_token: str | None = None,
) -> dict[str, object]:
    configurable: dict[str, object] = {
        "thread_id": thread_id,
        "tenant_id": str(tenant),
        "conversation_id": str(conversation),
        "actor_id": "" if actor is None else str(actor),
    }
    if execution_token is not None:
        configurable[EXECUTION_TOKEN_KEY] = execution_token
    return {"configurable": configurable}


def _result(state: dict[str, object]) -> KnowledgeQAResult:
    sources = state.get("sources") or []
    retrieval_count = state.get("retrieval_count")
    raw_turns = state.get("turns") or []
    turns = tuple(item for item in raw_turns if isinstance(item, dict)) if isinstance(raw_turns, list) else ()
    return KnowledgeQAResult(
        status=str(state.get("status") or "failed"),
        answer=str(state.get("answer") or ""),
        sources=tuple(item for item in sources if isinstance(item, dict)),
        retrieval_count=retrieval_count if isinstance(retrieval_count, int) else 0,
        requested_strategy=state.get("requested_strategy") if isinstance(state.get("requested_strategy"), str) else None,
        effective_strategy=state.get("effective_strategy") if isinstance(state.get("effective_strategy"), str) else None,
        degraded=bool(state.get("degraded")),
        degradation_reason=state.get("degradation_reason") if isinstance(state.get("degradation_reason"), str) else None,
        error_code=state.get("error_code") if isinstance(state.get("error_code"), str) else None,
        error_message=state.get("error_message") if isinstance(state.get("error_message"), str) else None,
        request_id=current_request_id() or (state.get("request_id") if isinstance(state.get("request_id"), str) else None),
        turns=turns,
    )


def _stamp(data: dict[str, object]) -> dict[str, object]:
    data["timestamp"] = datetime.now(timezone.utc).isoformat()
    return data


def _started(request_id: str, conversation_id: str | None) -> Event:
    return (
        "run_started",
        _stamp({"request_id": request_id, "conversation_id": conversation_id, "status": "started"}),
    )


def _completed(status: str, request_id: str, conversation_id: str | None) -> Event:
    return ("run_completed", {"status": status, "request_id": request_id, "conversation_id": conversation_id})


def _failed(request_id: str, error_code: str, message: str) -> Event:
    return (
        "run_failed",
        {
            "error_code": error_code,
            "safe_error_message": public_text(message, "工作流执行失败"),
            "request_id": request_id,
        },
    )


def _message(state: dict[str, object], request_id: str) -> Event:
    sources = state.get("sources") or []
    retrieval_count = state.get("retrieval_count")
    return (
        "message",
        {
            "answer": str(state.get("answer") or ""),
            "sources": [item for item in sources if isinstance(item, dict)],
            "status": str(state.get("status") or "failed"),
            "retrieval_count": retrieval_count if isinstance(retrieval_count, int) else 0,
            "effective_strategy": state.get("effective_strategy") if isinstance(state.get("effective_strategy"), str) else None,
            "degraded": bool(state.get("degraded")),
            "request_id": request_id,
        },
    )


def _node_event(data: dict[str, object], request_id: str) -> Event | None:
    name = data.get("name")
    if not isinstance(name, str) or not name:
        return None
    if "result" not in data:
        return ("node_started", _stamp({"node": name, "request_id": request_id}))
    result = data.get("result")
    status = "completed"
    if isinstance(result, dict) and isinstance(result.get("status"), str):
        status = str(result["status"])
    if data.get("error") is not None:
        status = "failed"
    return ("node_completed", _stamp({"node": name, "status": status, "request_id": request_id}))


def _phase_event(data: dict[str, object], request_id: str) -> Event | None:
    event = data.get("event")
    if event == "retrieval_completed":
        count = data.get("retrieval_count")
        return (
            "retrieval_completed",
            {
                "retrieval_count": count if isinstance(count, int) else 0,
                "requested_strategy": data.get("requested_strategy") if isinstance(data.get("requested_strategy"), str) else None,
                "effective_strategy": data.get("effective_strategy") if isinstance(data.get("effective_strategy"), str) else None,
                "request_id": request_id,
            },
        )
    if event == "rerank_completed":
        candidate_count = data.get("candidate_count")
        final_count = data.get("final_count")
        return (
            "rerank_completed",
            {
                "candidate_count": candidate_count if isinstance(candidate_count, int) else 0,
                "final_count": final_count if isinstance(final_count, int) else 0,
                "request_id": request_id,
            },
        )
    if event == "rerank_degraded":
        return (
            "rerank_degraded",
            {
                "requested_strategy": data.get("requested_strategy") if isinstance(data.get("requested_strategy"), str) else None,
                "effective_strategy": data.get("effective_strategy") if isinstance(data.get("effective_strategy"), str) else None,
                "degradation_reason": public_text(data.get("degradation_reason"), "RerankDegraded"),
                "request_id": request_id,
            },
        )
    return None
