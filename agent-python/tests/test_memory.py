from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.memory.context import MemoryAccessContext
from app.memory.errors import MemoryAccessError, MemoryForbidden, MemoryValidationError
from app.memory.sensitive import looks_sensitive
from app.memory.service import MemoryService
from app.persistence.migrate import _split_sql


def test_context_defaults_to_no_tenant_grants():
    context = MemoryAccessContext.for_trusted_caller(tenant_id=uuid4(), actor_id=uuid4())

    assert context.allow_tenant_read is False
    assert context.allow_tenant_write is False
    assert context.request_id is None


def test_context_rejects_untyped_identity_and_grants():
    with pytest.raises(MemoryAccessError):
        MemoryAccessContext.for_trusted_caller(tenant_id="not-a-uuid", actor_id=uuid4())  # type: ignore[arg-type]
    with pytest.raises(MemoryAccessError):
        MemoryAccessContext.for_trusted_caller(
            tenant_id=uuid4(),
            actor_id=uuid4(),
            allow_tenant_write="true",  # type: ignore[arg-type]
        )
    with pytest.raises(MemoryAccessError):
        MemoryAccessContext.for_trusted_caller(
            tenant_id=uuid4(),
            actor_id=uuid4(),
            request_id="bad id",
        )


def test_sensitive_denylist_matches_common_secret_shapes():
    samples = [
        "sk-live-secret-value",
        "Authorization Bearer abcdefghijklmnop",
        "password=hunter2",
        "api_key: abcdefghij",
        "postgresql://app:secret@db.internal/enterprise",
        "-----BEGIN PRIVATE KEY-----\nabc",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature",
        "host=db user=app password=secret",
    ]

    for sample in samples:
        assert looks_sensitive(sample) is True

    assert looks_sensitive("以后请用表格回答") is False


def test_service_rejects_secrets_and_business_facts_without_a_database():
    service = MemoryService(pool=None)  # type: ignore[arg-type]
    context = MemoryAccessContext.for_trusted_caller(tenant_id=uuid4(), actor_id=uuid4())
    secret = "password=hunter2"

    with pytest.raises(MemoryValidationError, match="业务事实不能写入长期记忆") as business:
        service.create(
            context,
            scope="user",
            memory_type="contract_amount",
            content="不要把金额写进来",
        )
    with pytest.raises(MemoryValidationError, match="记忆内容不符合保存要求") as rejected:
        service.create(context, scope="user", memory_type="preference", content=secret)
    with pytest.raises(MemoryForbidden, match="无权写入租户记忆"):
        service.create(
            context,
            scope="tenant",
            memory_type="organization_practice",
            content="合同审核先看相对方",
        )

    assert "hunter2" not in str(rejected.value)
    assert "12800" not in str(business.value)


def test_service_rejects_a_naive_expiry_before_opening_a_transaction():
    service = MemoryService(pool=None)  # type: ignore[arg-type]
    context = MemoryAccessContext.for_trusted_caller(tenant_id=uuid4(), actor_id=uuid4())

    with pytest.raises(MemoryValidationError, match="到期时间不合法"):
        service.create(
            context,
            scope="user",
            memory_type="format",
            content="用表格",
            expires_at=datetime(2026, 1, 1),
        )


def test_memory_migration_keeps_the_trigger_body_together_and_avoids_vectors():
    script = Path(__file__).resolve().parents[1].joinpath("migrations", "005_memory.sql")
    sql = script.read_text(encoding="utf-8")
    statements = _split_sql(sql)
    trigger = next(statement for statement in statements if "memory_audit_immutable" in statement and "BEGIN" in statement)

    assert "business." not in sql.lower()
    assert "vector(" not in sql.lower()
    assert "checkpoint" not in sql.lower()
    assert "RAISE EXCEPTION 'memory_audit is append-only'" in trigger
    assert trigger.count(";") >= 2
    assert all(not statement.startswith("IF current_user") for statement in statements)


def test_expiry_helper_accepts_an_aware_clock():
    service = MemoryService(pool=None)  # type: ignore[arg-type]
    context = MemoryAccessContext.for_trusted_caller(tenant_id=uuid4(), actor_id=uuid4())

    with pytest.raises(MemoryValidationError, match="记忆内容长度不合法"):
        service.create(
            context,
            scope="user",
            memory_type="preference",
            content="  ",
            expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
