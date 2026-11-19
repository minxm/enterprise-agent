"""Request-scoped actor ticket. It is not part of graph state or checkpoints."""

from __future__ import annotations

from contextvars import ContextVar, Token

_ACTOR_TICKET: ContextVar[str | None] = ContextVar("actor_ticket", default=None)


def bind_actor_ticket(ticket: str | None) -> Token[str | None]:
    if ticket is not None and (not isinstance(ticket, str) or not ticket.strip()):
        ticket = None
    return _ACTOR_TICKET.set(ticket)


def reset_actor_ticket(token: Token[str | None]) -> None:
    _ACTOR_TICKET.reset(token)


def current_actor_ticket() -> str | None:
    return _ACTOR_TICKET.get()
