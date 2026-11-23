"""Reject obvious credentials before they are written.

This is a denylist of common secret shapes. It is not a complete detector
and it does not try to classify every contract fact in free text. Contract
amounts and approval state are rejected by the memory type allow-list.
"""

from __future__ import annotations

import re

_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}", re.IGNORECASE),
    re.compile(r"bearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(
        r"(?:api[_-]?key|secret|access[_-]?key|token|password|passwd|pwd)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://\S+", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
)


def looks_sensitive(content: str) -> bool:
    if any(pattern.search(content) for pattern in _PATTERNS):
        return True
    lowered = content.lower()
    return "user=" in lowered and "password=" in lowered
