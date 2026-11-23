"""Short diagnostics for logs and client-visible errors.

The result keeps the exception type and a short safe message. Connection
passwords, API keys, and long third-party bodies are not included.
"""

from __future__ import annotations

import re

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}", re.IGNORECASE),
    re.compile(r"bearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(
        r"(?:api[_-]?key|secret|access[_-]?key|token|password|passwd|pwd)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://\S+", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_SENSITIVE_WORDS = ("password", "api_key", "api-key", "bearer ", "postgres://", "postgresql://", "sk-", "secret")


def diagnostic(exc: BaseException, limit: int = 200) -> str:
    name = exc.__class__.__name__
    detail = " ".join(str(exc).split())
    lowered = detail.lower()
    if not detail or any(word in lowered for word in _SENSITIVE_WORDS):
        return name
    redacted = detail
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("***", redacted)
    if redacted != detail or "***" in redacted:
        return name
    text = f"{name}: {redacted}"
    if len(text) > limit:
        return name
    return text
