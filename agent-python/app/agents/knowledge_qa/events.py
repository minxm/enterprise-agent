"""SSE event names and encoding for Knowledge QA.

Events are workflow progress, not token streams. Payloads stay on one JSON line
so the frame remains valid SSE.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

_EVENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SENSITIVE = ("sk-", "api_key", "api-key", "bearer ", "password", "postgres://", "postgresql://", "secret")


def encode_sse(event: str, data: Mapping[str, object]) -> bytes:
    if _EVENT_NAME.fullmatch(event) is None:
        raise ValueError("SSE 事件名不合法")
    body = json.dumps(dict(data), ensure_ascii=False, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {body}\n\n".encode("utf-8")


def public_text(value: object, fallback: str) -> str:
    """Return a short error string that does not carry secrets."""
    if not isinstance(value, str):
        return fallback
    text = value.replace("\n", " ").replace("\r", " ").strip()
    if not text or any(token in text.lower() for token in _SENSITIVE):
        return fallback
    return text[:200]


def degradation_category(reason: object) -> str:
    """Keep the exception class from a rerank fallback and drop the detail."""
    if not isinstance(reason, str) or not reason.strip():
        return "RerankDegraded"
    head = reason.split(":", 1)[0].strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", head) is None:
        return "RerankDegraded"
    if any(token in head.lower() for token in _SENSITIVE):
        return "RerankDegraded"
    return head
