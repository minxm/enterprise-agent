import re
from contextvars import ContextVar, Token
from uuid import uuid4

REQUEST_ID_HEADER = "X-Request-ID"
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def resolve_request_id(incoming: str | None) -> str:
    if incoming and _SAFE_REQUEST_ID.fullmatch(incoming):
        return incoming
    return str(uuid4())


def current_request_id() -> str | None:
    return _request_id.get()


def set_request_id(value: str) -> Token[str | None]:
    return _request_id.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)
