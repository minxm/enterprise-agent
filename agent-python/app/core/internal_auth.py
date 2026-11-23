"""Shared internal token check for every /internal/v1 route.

Java and Python compare the same header value. An empty server token rejects
every caller. Length is compared before the constant-time compare.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from app.core.config import settings

TOKEN_HEADER = "X-Agent-Token"


def require_gateway(x_agent_token: str | None = Header(default=None, alias=TOKEN_HEADER)) -> None:
    expected = settings.agent_internal_token
    provided = x_agent_token or ""
    if not expected or len(expected) != len(provided) or not secrets.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="未认证")
