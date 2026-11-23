"""Internal memory route. The service is scripted; the token is a test value."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.internal_memory import router
from app.core.config import settings
from app.core.errors import install_exception_handlers
from app.main import app as runtime
from app.memory.context import MemoryAccessContext
from app.memory.errors import MemoryForbidden, MemoryNotFound, MemoryVersionConflict
from app.memory.service import MemoryRecord, MemoryService

TOKEN = "test-internal-token"
TENANT = UUID("00000000-0000-4000-8000-00000000e391")
ACTOR = UUID("00000000-0000-4000-8000-00000000a391")
OTHER = UUID("00000000-0000-4000-8000-00000000a392")
MEMORY = UUID("00000000-0000-4000-8000-00000000d391")
NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
BODY = "以后优先使用表格"


class RecordingMemory(MemoryService):
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.records: dict[UUID, MemoryRecord] = {}
        self.boom: Exception | None = None
        self.expired = False

    def create(self, ctx, *, scope, memory_type, content, expires_at=None, source_conversation_id=None):
        self.calls.append(("create", ctx, scope, content))
        if self.boom is not None:
            raise self.boom
        if scope == "tenant" and not ctx.allow_tenant_write:
            raise MemoryForbidden("无权写入租户记忆")
        record = _record(
            scope=scope,
            content=content,
            user_id=ctx.actor_id if scope == "user" else None,
            memory_type=memory_type,
            source_conversation_id=source_conversation_id,
        )
        self.records[record.id] = record
        return record

    def list(self, ctx, *, scope=None, memory_type=None, status=None, include_expired=False):
        self.calls.append(("list", ctx, scope, include_expired))
        if scope == "tenant" and not (ctx.allow_tenant_read or ctx.allow_tenant_write):
            raise MemoryForbidden("无权读取租户记忆")
        items = self.records.values()
        if scope is not None:
            items = [item for item in items if item.scope == scope]
        return tuple(items)

    def get(self, ctx, memory_id):
        self.calls.append(("get", ctx, memory_id))
        record = self.records.get(memory_id)
        if record is None or (record.scope == "user" and record.user_id != ctx.actor_id):
            raise MemoryNotFound("记忆不存在")
        return record

    def update(self, ctx, *, memory_id, expected_version, content, expires_at):
        self.calls.append(("update", ctx, memory_id, expected_version, content))
        if self.boom is not None:
            raise self.boom
        current = self.get(ctx, memory_id)
        if current.scope == "tenant" and not ctx.allow_tenant_write:
            raise MemoryForbidden("无权修改租户记忆")
        if current.version != expected_version:
            raise MemoryVersionConflict("记忆版本冲突")
        updated = _replace(current, content=content, version=current.version + 1)
        self.records[memory_id] = updated
        return updated

    def archive(self, ctx, *, memory_id, expected_version):
        self.calls.append(("archive", ctx, memory_id))
        current = self.get(ctx, memory_id)
        if current.scope == "tenant" and not ctx.allow_tenant_write:
            raise MemoryForbidden("无权归档租户记忆")
        updated = _replace(current, status="archived", version=current.version + 1)
        self.records[memory_id] = updated
        return updated

    def delete(self, ctx, *, memory_id, expected_version):
        self.calls.append(("delete", ctx, memory_id))
        current = self.get(ctx, memory_id)
        if current.scope == "tenant" and not ctx.allow_tenant_write:
            raise MemoryForbidden("无权删除租户记忆")
        updated = _replace(current, content="", status="deleted", version=current.version + 1)
        self.records[memory_id] = updated
        return updated

    def expire_due(self, ctx):
        self.expired = True
        return 0


def test_missing_or_wrong_internal_token_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", TOKEN)
    service = RecordingMemory()
    with TestClient(_app(service)) as client:
        missing = client.post("/internal/v1/memories", json=_create())
        wrong = client.post("/internal/v1/memories", json=_create(), headers={"X-Agent-Token": "other-token"})
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert service.calls == []
    assert TOKEN not in missing.text
    assert TOKEN not in wrong.text
    assert "traceback" not in missing.text.lower()


def test_valid_token_creates_a_user_memory(monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", TOKEN)
    service = RecordingMemory()
    with TestClient(_app(service)) as client:
        response = client.post("/internal/v1/memories", headers=_headers(), json=_create())
    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "user"
    assert body["content"] == BODY
    assert body["source"] == "user_explicit"
    assert "tenant_id" not in body
    assert "confidence" not in body
    assert "content_sha256" not in body
    assert TOKEN not in response.text
    context = service.calls[0][1]
    assert isinstance(context, MemoryAccessContext)
    assert context.tenant_id == TENANT
    assert context.actor_id == ACTOR
    assert context.allow_tenant_read is False
    assert context.allow_tenant_write is False


def test_missing_caller_illegal_uuid_and_non_boolean_grants_are_rejected(monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", TOKEN)
    service = RecordingMemory()
    with TestClient(_app(service)) as client:
        missing_caller = client.post(
            "/internal/v1/memories",
            headers=_headers(),
            json={"scope": "user", "memory_type": "preference", "content": BODY},
        )
        illegal_uuid = client.get(
            "/internal/v1/memories/not-a-uuid",
            headers=_headers(),
            params=_params(False, False),
        )
        text_bool = client.post(
            "/internal/v1/memories",
            headers=_headers(),
            json=_create(allow_tenant_read="true"),
        )
        query_bool = client.get(
            "/internal/v1/memories",
            headers=_headers(),
            params={**_params(False, False), "allow_tenant_read": "yes"},
        )
    assert missing_caller.status_code == 422
    assert illegal_uuid.status_code == 422
    assert text_bool.status_code == 422
    assert query_bool.status_code == 422
    assert service.calls == []
    for response in (missing_caller, illegal_uuid, text_bool):
        assert response.json()["message"] == "请求参数不合法"
    assert query_bool.json()["message"] == "请求无法完成"
    for response in (missing_caller, illegal_uuid, text_bool, query_bool):
        assert TOKEN not in response.text
        assert BODY not in response.text


def test_tenant_create_requires_write_and_user_create_does_not(monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", TOKEN)
    service = RecordingMemory()
    with TestClient(_app(service)) as client:
        denied = client.post(
            "/internal/v1/memories",
            headers=_headers(),
            json=_create(scope="tenant"),
        )
        allowed = client.post(
            "/internal/v1/memories",
            headers=_headers(),
            json=_create(scope="tenant", allow_tenant_write=True),
        )
    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["scope"] == "tenant"
    assert [call[0] for call in service.calls] == ["create"]
    assert service.calls[0][1].allow_tenant_write is True
    assert service.calls[0][2] == "tenant"


def test_user_memory_is_hidden_from_another_actor(monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", TOKEN)
    service = RecordingMemory()
    with TestClient(_app(service)) as client:
        created = client.post("/internal/v1/memories", headers=_headers(), json=_create())
        memory_id = created.json()["id"]
        hidden = client.get(
            f"/internal/v1/memories/{memory_id}",
            headers=_headers(),
            params=_params(False, False, actor=OTHER),
        )
    assert created.status_code == 200
    assert hidden.status_code == 404
    assert BODY not in hidden.text
    assert hidden.json()["message"] == "请求无法完成"


def test_tenant_read_and_write_are_independent(monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", TOKEN)
    service = RecordingMemory()
    with TestClient(_app(service)) as client:
        created = client.post(
            "/internal/v1/memories",
            headers=_headers(),
            json=_create(scope="tenant", allow_tenant_write=True, content="租户共享口径"),
        )
        memory_id = created.json()["id"]
        listed = client.get("/internal/v1/memories", headers=_headers(), params=_params(False, True))
        hidden = client.get(
            f"/internal/v1/memories/{memory_id}",
            headers=_headers(),
            params=_params(False, True),
        )
        denied_update = client.put(
            f"/internal/v1/memories/{memory_id}",
            headers=_headers(),
            json=_change(allow_tenant_write=False),
        )
        denied_delete = client.request(
            "DELETE",
            f"/internal/v1/memories/{memory_id}",
            headers=_headers(),
            json=_version(allow_tenant_write=False),
        )
    assert created.status_code == 200
    assert listed.status_code == 200
    assert listed.json()["items"] == []
    assert service.calls[1][0] == "list"
    assert service.calls[1][2] == "user"
    assert hidden.status_code == 404
    assert "租户共享口径" not in hidden.text
    assert denied_update.status_code == 403
    assert denied_delete.status_code == 403


def test_version_conflict_archive_and_delete_shapes(monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", TOKEN)
    service = RecordingMemory()
    with TestClient(_app(service)) as client:
        created = client.post("/internal/v1/memories", headers=_headers(), json=_create())
        memory_id = created.json()["id"]
        conflict = client.put(
            f"/internal/v1/memories/{memory_id}",
            headers=_headers(),
            json=_change(expected_version=9),
        )
        archived = client.post(
            f"/internal/v1/memories/{memory_id}/archive",
            headers=_headers(),
            json=_version(),
        )
        removed = client.request(
            "DELETE",
            f"/internal/v1/memories/{memory_id}",
            headers=_headers(),
            json=_version(expected_version=2),
        )
    assert conflict.status_code == 409
    assert conflict.json()["message"] == "请求无法完成"
    assert BODY not in conflict.text
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert archived.json()["content"] == BODY
    assert removed.status_code == 200
    assert removed.json()["content"] == ""
    assert removed.json()["status"] == "deleted"


def test_request_cannot_override_source_confidence_or_identity(monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", TOKEN)
    service = RecordingMemory()
    forged = _create()
    forged["source"] = "authorized_workflow"
    forged["confidence"] = "0.10"
    forged["user_id"] = str(OTHER)
    forged["tenant_id"] = str(uuid4())
    with TestClient(_app(service)) as client:
        response = client.post("/internal/v1/memories", headers=_headers(), json=forged)
        query = client.get(
            "/internal/v1/memories",
            headers=_headers(),
            params={**_params(False, False), "source": "user_explicit", "confidence": "1"},
        )
    assert response.status_code == 422
    assert query.status_code == 422
    assert service.calls == []


def test_internal_failure_does_not_leak_and_route_does_not_expire(monkeypatch, caplog):
    monkeypatch.setattr(settings, "agent_internal_token", TOKEN)
    service = RecordingMemory()
    service.boom = RuntimeError("postgres://agent_app:secret@db/agent sk-live " + TOKEN + " " + BODY)
    with TestClient(_app(service)) as client:
        response = client.post("/internal/v1/memories", headers=_headers(), json=_create())
    assert response.status_code == 500
    assert response.json()["message"] == "服务内部错误"
    assert "secret" not in response.text
    assert "sk-live" not in response.text
    assert TOKEN not in response.text
    assert BODY not in response.text
    assert "traceback" not in response.text.lower()
    assert "postgres://" not in response.text
    assert service.expired is False
    assert TOKEN not in caplog.text
    assert BODY not in caplog.text
    assert "sk-live" not in caplog.text

    source = (Path(__file__).resolve().parents[1] / "app" / "api" / "routes" / "internal_memory.py").read_text(
        encoding="utf-8"
    )
    assert "expire_due" not in source
    assert "MemoryAccessContext(" not in source
    assert "for_trusted_caller" in source
    assert "execute(" not in source
    assert "checkpoint" not in source
    assert "agent_conversation" not in source
    mounted = []
    for route in runtime.routes:
        context = getattr(route, "include_context", None)
        if context is None:
            continue
        for child in context.included_router.routes:
            mounted.append(context.prefix + getattr(child, "path", ""))
    assert "/internal/v1/memories" in mounted
    assert "/api/v1/memories" not in mounted
    assert not any(path.startswith("/api/") and "memor" in path for path in mounted)


def _app(service: RecordingMemory) -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(router, prefix="/internal/v1")
    app.state.memory = service
    return app


def _headers() -> dict[str, str]:
    return {"X-Agent-Token": TOKEN}


def _caller(read: bool, write: bool, actor: UUID = ACTOR) -> dict[str, object]:
    return {
        "tenant_id": str(TENANT),
        "actor_id": str(actor),
        "allow_tenant_read": read,
        "allow_tenant_write": write,
    }


def _create(**overrides: object) -> dict[str, object]:
    read = overrides.pop("allow_tenant_read", False)
    write = overrides.pop("allow_tenant_write", False)
    actor = overrides.pop("actor", ACTOR)
    body: dict[str, object] = {
        "caller": _caller(bool(read) if isinstance(read, bool) else read, bool(write) if isinstance(write, bool) else write, actor),
        "scope": "user",
        "memory_type": "preference",
        "content": BODY,
    }
    if not isinstance(read, bool) or not isinstance(write, bool):
        body["caller"] = {
            "tenant_id": str(TENANT),
            "actor_id": str(actor),
            "allow_tenant_read": read,
            "allow_tenant_write": write,
        }
    body.update(overrides)
    return body


def _change(**overrides: object) -> dict[str, object]:
    write = overrides.pop("allow_tenant_write", False)
    version = overrides.pop("expected_version", 1)
    return {
        "caller": _caller(False, bool(write)),
        "expected_version": version,
        "content": overrides.get("content", "改成列表"),
    }


def _version(**overrides: object) -> dict[str, object]:
    write = overrides.pop("allow_tenant_write", False)
    version = overrides.pop("expected_version", 1)
    return {"caller": _caller(False, bool(write)), "expected_version": version}


def _params(read: bool, write: bool, actor: UUID = ACTOR) -> dict[str, str]:
    return {
        "tenant_id": str(TENANT),
        "actor_id": str(actor),
        "allow_tenant_read": "true" if read else "false",
        "allow_tenant_write": "true" if write else "false",
    }


def _record(**overrides: object) -> MemoryRecord:
    values: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": TENANT,
        "user_id": ACTOR,
        "scope": "user",
        "memory_type": "preference",
        "content": BODY,
        "source": "user_explicit",
        "source_conversation_id": None,
        "confidence": Decimal("1.00"),
        "status": "active",
        "created_at": NOW,
        "updated_at": NOW,
        "expires_at": None,
        "deleted_at": None,
        "created_by": ACTOR,
        "updated_by": None,
        "version": 1,
        "content_sha256": "ab" * 32,
    }
    values.update(overrides)
    return MemoryRecord(**values)


def _replace(record: MemoryRecord, **overrides: object) -> MemoryRecord:
    data = {field: getattr(record, field) for field in record.__dataclass_fields__}
    data.update(overrides)
    return MemoryRecord(**data)
