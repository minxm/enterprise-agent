"""Bridge a synchronous Knowledge QA run to an SSE response.

The graph runs on a worker thread. The async generator only forwards encoded
events and writes a comment when the worker has been quiet. This module does
not register an HTTP route. The internal gateway calls open_knowledge_qa_sse
after the shared service token is accepted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Iterator
from contextvars import copy_context

from langgraph.runtime import RunControl
from starlette.responses import StreamingResponse

from app.agents.knowledge_qa.events import encode_sse
from app.core.request_context import current_request_id

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
HEARTBEAT_SECONDS = 15.0
MAX_STREAM_SECONDS = 180.0
_HEARTBEAT = b": heartbeat\n\n"
logger = logging.getLogger(__name__)


def build_sse_response(chunks: AsyncIterator[bytes] | Iterator[bytes]) -> StreamingResponse:
    return StreamingResponse(chunks, media_type="text/event-stream", headers=dict(SSE_HEADERS))


def open_knowledge_qa_sse(service: object, **kwargs: object) -> StreamingResponse:
    """Return an SSE response for a trusted in-process caller.

    kwargs are the same keyword arguments accepted by KnowledgeQAService.stream_events,
    except the service itself. Do not mount this on a public route.
    """
    return build_sse_response(iter_sse_bytes(service, **kwargs))


async def iter_sse_bytes(
    service: object,
    *,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    max_seconds: float = MAX_STREAM_SECONDS,
    **kwargs: object,
) -> AsyncIterator[bytes]:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
    control = RunControl()
    started = time.monotonic()
    request_id = current_request_id() or ""
    context = copy_context()

    def produce() -> None:
        def run() -> None:
            try:
                events = service.stream_events(control=control, **kwargs)  # type: ignore[attr-defined]
                for name, data in events:
                    loop.call_soon_threadsafe(queue.put_nowait, ("event", (name, data)))
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc.__class__.__name__))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        context.run(run)

    worker = asyncio.create_task(asyncio.to_thread(produce))
    cancelled = False
    try:
        while True:
            remaining = max_seconds - (time.monotonic() - started)
            if remaining <= 0:
                control.request_drain("timeout")
                timeout = heartbeat_seconds
            else:
                timeout = min(heartbeat_seconds, remaining)
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                yield _HEARTBEAT
                continue
            if kind == "done":
                break
            if kind == "error":
                name = payload if isinstance(payload, str) and payload.isidentifier() else "WorkflowError"
                yield encode_sse(
                    "run_failed",
                    {
                        "error_code": "run_failed",
                        "safe_error_message": name,
                        "request_id": request_id,
                    },
                )
                continue
            name, data = payload  # type: ignore[misc]
            yield encode_sse(str(name), data)  # type: ignore[arg-type]
    except (asyncio.CancelledError, GeneratorExit):
        cancelled = True
        raise
    finally:
        control.request_drain("client_disconnect")
        if cancelled:
            logger.info("knowledge qa client disconnected requestId=%s", request_id)
        await worker
