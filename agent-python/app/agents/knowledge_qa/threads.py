"""Thread identity for the Knowledge QA graph.

thread_id is derived inside the service from a trusted tenant and a
conversation id. Callers cannot supply a thread_id. There is no user directory
in this service, so actor_id is an optional principal supplied by a trusted
caller. An empty actor means the trusted service itself. A different actor
cannot resume the thread. This lock is local to one service instance; it is
not a distributed lock.
"""

from __future__ import annotations

import threading
from uuid import UUID


class ThreadAccessError(Exception):
    """The caller is not allowed to read or update this thread."""


def thread_id_for(tenant_id: UUID, conversation_id: UUID) -> str:
    return f"kq:{tenant_id}:{conversation_id}"


def parse_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def assert_thread_access(saved: object, *, tenant_id: UUID, actor_id: UUID | None) -> None:
    if saved is None:
        return
    metadata = getattr(saved, "metadata", None) or {}
    expected_actor = "" if actor_id is None else str(actor_id)
    if metadata.get("tenant_id") != str(tenant_id) or metadata.get("actor_id") != expected_actor:
        raise ThreadAccessError
    checkpoint = getattr(saved, "checkpoint", None) or {}
    values = checkpoint.get("channel_values") or {}
    recorded_tenant = values.get("tenant_id")
    if isinstance(recorded_tenant, str) and recorded_tenant != str(tenant_id):
        raise ThreadAccessError


class ThreadLocks:
    """Reject a second overlapping run of the same thread in this process."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def lock_for(self, thread_id: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[thread_id] = lock
            return lock
