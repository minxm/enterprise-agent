import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import Connection, OperationalError
from psycopg.errors import CheckViolation, InsufficientPrivilege
from psycopg_pool import ConnectionPool

from app.api.routes.internal_memory import router as internal_memory_router
from app.core.config import Settings, settings
from app.core.errors import install_exception_handlers
from app.memory.context import MemoryAccessContext
from app.memory.errors import MemoryForbidden, MemoryNotFound, MemoryValidationError, MemoryVersionConflict
from app.memory.service import MemoryRecord, MemoryService
from app.persistence.migrate import apply_migrations
from app.persistence.postgres import build_conninfo, memory_transaction


def test_user_memory_can_be_created_listed_updated_and_read(memory_db):
    tenant_id, actor_id = uuid4(), uuid4()
    service = MemoryService(memory_db.pool)
    context = _context(tenant_id, actor_id, request_id="day38-user")
    conversation_id = uuid4()
    try:
        created = service.create(
            context,
            scope="user",
            memory_type="preference",
            content="以后优先使用表格",
            source_conversation_id=conversation_id,
        )
        listed = service.list(context, scope="user", memory_type="preference", status="active")
        loaded = service.get(context, created.id)
        updated = service.update(
            context,
            memory_id=created.id,
            expected_version=created.version,
            content="以后优先使用表格，并附上来源",
            expires_at=None,
        )

        assert created.user_id == actor_id
        assert created.created_by == actor_id
        assert created.source == "user_explicit"
        assert created.confidence == updated.confidence
        assert created.status == "active"
        assert created.version == 1
        assert created.source_conversation_id == conversation_id
        assert [item.id for item in listed] == [created.id]
        assert loaded.content == "以后优先使用表格"
        assert updated.version == 2
        assert updated.updated_by == actor_id
        assert updated.content_sha256 != created.content_sha256
        assert _audit_actions(memory_db, context, created.id) == ["create", "update"]
        assert _metadata(memory_db, context, created.id) == {}
    finally:
        _purge(memory_db, tenant_id)


def test_archive_hides_recall_and_delete_clears_the_body(memory_db):
    tenant_id, actor_id = uuid4(), uuid4()
    service = MemoryService(memory_db.pool)
    context = _context(tenant_id, actor_id)
    try:
        kept = service.create(context, scope="user", memory_type="format", content="保留这条格式")
        archived = service.archive(context, memory_id=kept.id, expected_version=1)
        removed = service.create(context, scope="user", memory_type="work_scope", content="负责采购范围说明")
        original_hash = removed.content_sha256
        deleted = service.delete(context, memory_id=removed.id, expected_version=1)
        recalled = service.recall(context)

        assert archived.status == "archived"
        assert archived.content == "保留这条格式"
        assert archived.version == 2
        assert deleted.status == "deleted"
        assert deleted.content == ""
        assert deleted.deleted_at is not None
        assert deleted.version == 2
        assert deleted.content_sha256 == original_hash
        assert _metadata(memory_db, context, removed.id) == {}
        assert recalled == ()
        assert [item.id for item in service.list(context)] == [kept.id]
        assert service.list(context, status="deleted")[0].content == ""
        assert service.get(context, removed.id).content == ""
        assert _audit_actions(memory_db, context, removed.id) == ["create", "delete"]
        with pytest.raises(InsufficientPrivilege):
            with memory_transaction(
                memory_db.pool,
                tenant_id=str(tenant_id),
                actor_id=str(actor_id),
                allow_tenant_read=False,
                allow_tenant_write=False,
            ) as connection:
                connection.execute("delete from agent.memory_item where id = %s", (removed.id,))
    finally:
        _purge(memory_db, tenant_id)


def test_expiry_is_limited_to_the_visible_tenant(memory_db):
    first_tenant, second_tenant = uuid4(), uuid4()
    actor_id = uuid4()
    service = MemoryService(memory_db.pool)
    first = _context(first_tenant, actor_id)
    second = _context(second_tenant, actor_id)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    try:
        own = service.create(
            first,
            scope="user",
            memory_type="preference",
            content="这条已经到期",
            expires_at=past,
        )
        other = service.create(
            second,
            scope="user",
            memory_type="preference",
            content="另一租户的到期记忆",
            expires_at=past,
        )
        fresh = service.create(first, scope="user", memory_type="format", content="这条仍然有效")

        assert [item.id for item in service.recall(first)] == [fresh.id]
        assert [item.id for item in service.list(first)] == [fresh.id]
        assert service.expire_due(first) == 1
        assert service.get(first, own.id).status == "expired"
        assert service.get(second, other.id).status == "active"
        assert _audit_actions(memory_db, first, own.id)[-1] == "expire"
        assert service.list(first, include_expired=True, status="expired")[0].id == own.id
    finally:
        _purge(memory_db, first_tenant)
        _purge(memory_db, second_tenant)


def test_tenant_and_user_isolation_cannot_be_bypassed_by_id(memory_db):
    tenant_id, other_tenant = uuid4(), uuid4()
    owner, peer, admin = uuid4(), uuid4(), uuid4()
    service = MemoryService(memory_db.pool)
    owner_ctx = _context(tenant_id, owner)
    peer_ctx = _context(tenant_id, peer)
    outsider = _context(other_tenant, owner)
    reader = _context(tenant_id, admin, allow_tenant_read=True)
    writer = _context(tenant_id, admin, allow_tenant_write=True)
    try:
        personal = service.create(owner_ctx, scope="user", memory_type="preference", content="只属于本人的偏好")
        with pytest.raises(MemoryForbidden):
            service.create(reader, scope="tenant", memory_type="organization_practice", content="组织审核口径")
        shared = service.create(
            writer,
            scope="tenant",
            memory_type="organization_practice",
            content="组织审核口径",
        )

        with pytest.raises(MemoryNotFound):
            service.get(peer_ctx, personal.id)
        with pytest.raises(MemoryNotFound):
            service.get(outsider, personal.id)
        with pytest.raises(MemoryNotFound):
            service.update(
                peer_ctx,
                memory_id=personal.id,
                expected_version=1,
                content="试图覆盖",
                expires_at=None,
            )
        with pytest.raises(MemoryNotFound):
            service.get(peer_ctx, shared.id)
        assert service.get(reader, shared.id).content == "组织审核口径"
        with pytest.raises(MemoryForbidden):
            service.update(
                reader,
                memory_id=shared.id,
                expected_version=1,
                content="读者不能改",
                expires_at=None,
            )
        assert service.list(peer_ctx, scope="user") == ()
        with pytest.raises(MemoryForbidden):
            service.list(peer_ctx, scope="tenant")
        assert service.recall(reader)[0].id == shared.id
        assert service.recall(peer_ctx) == ()
    finally:
        _purge(memory_db, tenant_id)
        _purge(memory_db, other_tenant)


def test_missing_guc_fails_closed_and_direct_source_is_rejected(memory_db):
    tenant_id, actor_id = uuid4(), uuid4()
    service = MemoryService(memory_db.pool)
    context = _context(tenant_id, actor_id, allow_tenant_write=True)
    try:
        created = service.create(context, scope="user", memory_type="preference", content="失败关闭探针")
        service.create(
            context,
            scope="tenant",
            memory_type="organization_practice",
            content="租户探针",
        )
        with memory_db.pool.connection() as connection:
            hidden = connection.execute("select count(*) from agent.memory_item").fetchone()
        with memory_db.pool.connection() as connection:
            connection.execute("select set_config('app.tenant_id', %s, true)", (str(tenant_id),))
            tenant_only = connection.execute("select count(*) from agent.memory_item").fetchone()
        with pytest.raises(CheckViolation):
            with memory_transaction(
                memory_db.pool,
                tenant_id=str(tenant_id),
                actor_id=str(actor_id),
                allow_tenant_read=False,
                allow_tenant_write=False,
            ) as connection:
                connection.execute(
                    """
                    insert into agent.memory_item (
                        tenant_id, user_id, scope, memory_type, content, source,
                        confidence, status, created_by, content_sha256
                    ) values (
                        %s, %s, 'user', 'preference', '非法来源', 'authorized_workflow',
                        1.00, 'active', %s, repeat('ab', 32)
                    )
                    """,
                    (tenant_id, actor_id, actor_id),
                )
        assert hidden[0] == 0
        assert tenant_only[0] == 0
        assert service.get(context, created.id).content == "失败关闭探针"
    finally:
        _purge(memory_db, tenant_id)


def test_same_version_updates_conflict(memory_db):
    tenant_id, actor_id = uuid4(), uuid4()
    service = MemoryService(memory_db.pool)
    context = _context(tenant_id, actor_id)
    try:
        created = service.create(context, scope="user", memory_type="preference", content="原始偏好")
        barrier = threading.Barrier(2)
        outcomes: list[object] = []

        def worker(text: str) -> None:
            try:
                barrier.wait(5)
                outcomes.append(
                    service.update(
                        context,
                        memory_id=created.id,
                        expected_version=1,
                        content=text,
                        expires_at=None,
                    )
                )
            except Exception as exc:
                outcomes.append(exc)

        threads = [threading.Thread(target=worker, args=(text,)) for text in ("左侧偏好", "右侧偏好")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        saved = [item for item in outcomes if isinstance(item, MemoryRecord)]
        conflicts = [item for item in outcomes if isinstance(item, MemoryVersionConflict)]
        current = service.get(context, created.id)
        assert len(saved) == 1 and len(conflicts) == 1
        assert saved[0].version == 2
        assert current.version == 2
        assert current.content == saved[0].content
        assert str(conflicts[0]) == "记忆版本冲突"
    finally:
        _purge(memory_db, tenant_id)


def test_recall_orders_user_memory_and_caps_size_without_touching_rows(memory_db):
    tenant_id, actor_id = uuid4(), uuid4()
    service = MemoryService(memory_db.pool)
    context = _context(tenant_id, actor_id, allow_tenant_read=True, allow_tenant_write=True)
    try:
        for index in range(6):
            service.create(
                context,
                scope="user",
                memory_type="preference",
                content=f"用户记忆{index}-" + ("甲" * 600),
            )
        for index in range(4):
            service.create(
                context,
                scope="tenant",
                memory_type="organization_practice",
                content=f"租户记忆{index}",
            )
        before = service.get(context, service.list(context, scope="user")[0].id)
        audits_before = len(_audit_actions(memory_db, context, before.id))
        recalled = service.recall(context)
        after = service.get(context, before.id)

        assert [item.scope for item in recalled].count("user") <= 5
        assert [item.scope for item in recalled].count("tenant") <= 3
        assert len(recalled) <= 8
        assert sum(len(item.content) for item in recalled) <= 2000
        assert recalled[0].scope == "user"
        assert after.updated_at == before.updated_at
        assert after.version == before.version
        assert len(_audit_actions(memory_db, context, before.id)) == audits_before
    finally:
        _purge(memory_db, tenant_id)


def test_guc_does_not_survive_commit_or_rollback(memory_db):
    pool = _pool(memory_db.settings, min_size=1, max_size=1)
    context_args = {
        "tenant_id": str(uuid4()),
        "actor_id": str(uuid4()),
        "allow_tenant_read": True,
        "allow_tenant_write": False,
    }
    try:
        with memory_transaction(pool, **context_args) as connection:
            first_pid = connection.execute("select pg_backend_pid()").fetchone()[0]
        with pool.connection() as connection:
            committed = _guc(connection)
            committed_pid = connection.execute("select pg_backend_pid()").fetchone()[0]
        try:
            with memory_transaction(pool, **context_args) as connection:
                raise RuntimeError("rollback-marker")
        except RuntimeError as exc:
            assert str(exc) == "rollback-marker"
        with pool.connection() as connection:
            rolled_back = _guc(connection)
            rolled_pid = connection.execute("select pg_backend_pid()").fetchone()[0]
        assert first_pid == committed_pid == rolled_pid
        assert committed == ("", "", "", "")
        assert rolled_back == ("", "", "", "")
    finally:
        pool.close()


def test_roles_rls_and_audit_grants_match_the_runtime_boundary(memory_db):
    tenant_id, actor_id = uuid4(), uuid4()
    service = MemoryService(memory_db.pool)
    context = _context(tenant_id, actor_id)
    try:
        created = service.create(context, scope="user", memory_type="format", content="权限探针")
        with _migrator(memory_db.env) as connection:
            roles = connection.execute(
                """
                select rolname, rolsuper, rolbypassrls, rolcreaterole
                from pg_roles
                where rolname in ('agent_app', 'agent_migrator')
                order by rolname
                """
            ).fetchall()
            membership = connection.execute(
                """
                select count(*)
                from pg_auth_members member
                join pg_roles member_role on member_role.oid = member.member
                join pg_roles granted_role on granted_role.oid = member.roleid
                where member_role.rolname = 'agent_app'
                  and granted_role.rolname = 'agent_migrator'
                """
            ).fetchone()
            tables = connection.execute(
                """
                select relation.relname, pg_get_userbyid(relation.relowner),
                       relation.relrowsecurity, relation.relforcerowsecurity
                from pg_class relation
                join pg_namespace namespace on namespace.oid = relation.relnamespace
                where namespace.nspname = 'agent'
                  and relation.relname in ('memory_item', 'memory_audit')
                order by relation.relname
                """
            ).fetchall()
            privileges = connection.execute(
                """
                select
                    has_table_privilege('agent_app', 'agent.memory_item', 'DELETE'),
                    has_table_privilege('agent_app', 'agent.memory_item', 'TRUNCATE'),
                    has_table_privilege('agent_app', 'agent.memory_audit', 'UPDATE'),
                    has_table_privilege('agent_app', 'agent.memory_audit', 'DELETE'),
                    has_table_privilege('agent_app', 'agent.memory_audit', 'TRUNCATE'),
                    has_table_privilege('agent_app', 'agent.memory_item', 'SELECT'),
                    has_table_privilege('agent_app', 'agent.memory_audit', 'INSERT')
                """
            ).fetchone()
            policies = connection.execute(
                """
                select policy.polname,
                       coalesce(pg_get_expr(policy.polqual, policy.polrelid), '')
                           || ' ' ||
                       coalesce(pg_get_expr(policy.polwithcheck, policy.polrelid), '')
                from pg_policy policy
                join pg_class relation on relation.oid = policy.polrelid
                join pg_namespace namespace on namespace.oid = relation.relnamespace
                where namespace.nspname = 'agent'
                  and relation.relname = 'memory_item'
                """
            ).fetchall()
            visible = connection.execute(
                "select count(*) from agent.memory_item where id = %s",
                (created.id,),
            ).fetchone()
            embedding = connection.execute(
                """
                select count(*)
                from pg_attribute attribute
                join pg_class relation on relation.oid = attribute.attrelid
                join pg_namespace namespace on namespace.oid = relation.relnamespace
                where namespace.nspname = 'agent'
                  and relation.relname = 'memory_item'
                  and attribute.attname = 'embedding'
                  and not attribute.attisdropped
                """
            ).fetchone()
        expressions = " ".join(expression or "" for _, expression in policies)
        assert roles == [
            ("agent_app", False, False, False),
            ("agent_migrator", False, False, False),
        ]
        assert membership[0] == 0
        assert tables == [
            ("memory_audit", "agent_migrator", True, True),
            ("memory_item", "agent_migrator", True, True),
        ]
        assert privileges == (False, False, False, False, False, True, True)
        assert "app.actor_id" in expressions
        assert "app.tenant_memory_read" in expressions
        assert "app.tenant_memory_write" in expressions
        assert visible[0] == 1
        assert embedding[0] == 0
        _assert_set_role_denied(memory_db)
        _assert_row_security_off_still_hides_rows(memory_db)
        _assert_business_schema_denied(memory_db)
        _assert_audit_trigger_blocks_app(memory_db, context, created.id)
    finally:
        _purge(memory_db, tenant_id)


def test_migration_is_recorded_once_and_leaves_existing_tables(memory_db):
    apply_migrations(memory_db.settings)
    with _migrator(memory_db.env) as connection:
        version = connection.execute(
            "select count(*) from agent.schema_migrations where version = '005'"
        ).fetchone()
        checkpoint = connection.execute("select to_regclass('agent.checkpoints')").fetchone()
        knowledge = connection.execute("select to_regclass('agent.knowledge_document')").fetchone()
        business = connection.execute(
            "select has_schema_privilege('agent_app', 'business', 'USAGE')"
        ).fetchone()
    assert version[0] == 1
    assert checkpoint[0] is not None
    assert knowledge[0] is not None
    assert business[0] is False


def test_internal_http_keeps_user_memories_private(memory_db, monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", "day39-route-token")
    tenant_id, owner_id, admin_id = uuid4(), uuid4(), uuid4()
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(internal_memory_router, prefix="/internal/v1")
    app.state.memory = MemoryService(memory_db.pool)
    headers = {"X-Agent-Token": "day39-route-token"}
    try:
        with TestClient(app) as client:
            created = client.post(
                "/internal/v1/memories",
                headers=headers,
                json=_http_body(tenant_id, owner_id, scope="user", content="只属于本人"),
            )
            assert created.status_code == 200, created.text
            memory_id = created.json()["id"]
            assert "day39-route-token" not in created.text
            assert "tenant_id" not in created.json()

            hidden = client.get(
                f"/internal/v1/memories/{memory_id}",
                headers=headers,
                params=_http_params(tenant_id, admin_id, read=True, write=True),
            )
            assert hidden.status_code == 404
            assert "只属于本人" not in hidden.text

            denied = client.post(
                "/internal/v1/memories",
                headers=headers,
                json=_http_body(tenant_id, admin_id, scope="tenant", content="不能写"),
            )
            assert denied.status_code == 403
            assert "不能写" not in denied.text

            shared = client.post(
                "/internal/v1/memories",
                headers=headers,
                json=_http_body(
                    tenant_id, admin_id, scope="tenant", content="租户口径", write=True
                ),
            )
            assert shared.status_code == 200, shared.text
            shared_id = shared.json()["id"]

            admin_list = client.get(
                "/internal/v1/memories",
                headers=headers,
                params=_http_params(tenant_id, admin_id, read=True, write=True),
            )
            assert admin_list.status_code == 200
            admin_ids = [item["id"] for item in admin_list.json()["items"]]
            assert shared_id in admin_ids
            assert memory_id not in admin_ids

            owner_list = client.get(
                "/internal/v1/memories",
                headers=headers,
                params=_http_params(tenant_id, owner_id, write=True),
            )
            assert owner_list.status_code == 200
            assert shared_id not in [item["id"] for item in owner_list.json()["items"]]
            assert memory_id in [item["id"] for item in owner_list.json()["items"]]

            owner_get = client.get(
                f"/internal/v1/memories/{shared_id}",
                headers=headers,
                params=_http_params(tenant_id, owner_id, write=True),
            )
            assert owner_get.status_code == 404
            assert "租户口径" not in owner_get.text

            blocked = client.put(
                f"/internal/v1/memories/{shared_id}",
                headers=headers,
                json=_http_change(tenant_id, admin_id, content="未授权修改", read=True),
            )
            assert blocked.status_code == 403
            assert "未授权修改" not in blocked.text
            assert "租户口径" not in blocked.text

            changed = client.put(
                f"/internal/v1/memories/{shared_id}",
                headers=headers,
                json=_http_change(tenant_id, admin_id, content="已修改", write=True),
            )
            assert changed.status_code == 200
            assert changed.json()["version"] == 2
            conflict = client.put(
                f"/internal/v1/memories/{shared_id}",
                headers=headers,
                json=_http_change(tenant_id, admin_id, content="再次修改", write=True),
            )
            assert conflict.status_code == 409
            assert "已修改" not in conflict.text

            archived = client.post(
                f"/internal/v1/memories/{shared_id}/archive",
                headers=headers,
                json=_http_version(tenant_id, admin_id, version=2, write=True),
            )
            assert archived.status_code == 200
            assert archived.json()["status"] == "archived"
            assert archived.json()["content"] == "已修改"

            removed = client.request(
                "DELETE",
                f"/internal/v1/memories/{memory_id}",
                headers=headers,
                json=_http_version(tenant_id, owner_id, version=1),
            )
            assert removed.status_code == 200
            assert removed.json()["content"] == ""
            assert removed.json()["status"] == "deleted"
            assert "day39-route-token" not in removed.text

        service = MemoryService(memory_db.pool)
        deleted = service.get(_context(tenant_id, owner_id), UUID(memory_id))
        assert deleted.content == ""
        kept = service.get(
            _context(tenant_id, admin_id, allow_tenant_read=True, allow_tenant_write=True),
            UUID(shared_id),
        )
        assert kept.content == "已修改"
        assert kept.status == "archived"
        assert _audit_actions(memory_db, _context(tenant_id, owner_id), UUID(memory_id)) == [
            "create",
            "delete",
        ]
        assert _audit_actions(
            memory_db,
            _context(tenant_id, admin_id, allow_tenant_write=True),
            UUID(shared_id),
        ) == ["create", "update", "archive"]
    finally:
        _purge(memory_db, tenant_id)


def _http_body(tenant_id, actor_id, *, scope, content, read=False, write=False):
    return {
        "caller": _http_caller(tenant_id, actor_id, read=read, write=write),
        "scope": scope,
        "memory_type": "preference",
        "content": content,
    }


def _http_change(tenant_id, actor_id, *, content, read=False, write=False):
    return {
        "caller": _http_caller(tenant_id, actor_id, read=read, write=write),
        "expected_version": 1,
        "content": content,
    }


def _http_version(tenant_id, actor_id, *, version, write=False):
    return {
        "caller": _http_caller(tenant_id, actor_id, write=write),
        "expected_version": version,
    }


def _http_caller(tenant_id, actor_id, *, read=False, write=False):
    return {
        "tenant_id": str(tenant_id),
        "actor_id": str(actor_id),
        "allow_tenant_read": read,
        "allow_tenant_write": write,
    }


def _http_params(tenant_id, actor_id, *, read=False, write=False):
    return {
        "tenant_id": str(tenant_id),
        "actor_id": str(actor_id),
        "allow_tenant_read": "true" if read else "false",
        "allow_tenant_write": "true" if write else "false",
    }


@pytest.fixture(scope="module")
def memory_db():
    env = _root_env()
    if (
        not env.get("AGENT_APP_PASSWORD")
        or not env.get("AGENT_MIGRATOR_PASSWORD")
        or not env.get("POSTGRES_DB")
    ):
        pytest.skip("仓库根目录 .env 未提供 AGENT_APP_PASSWORD 或 AGENT_MIGRATOR_PASSWORD，跳过真实数据库测试")
    settings = _settings(env)
    try:
        apply_migrations(settings)
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL 不可用，跳过真实数据库测试: {exc.__class__.__name__}")
    pool = _pool(settings, min_size=2, max_size=4)
    try:
        yield type("MemoryDatabase", (), {"pool": pool, "env": env, "settings": settings})()
    finally:
        pool.close()


def _context(
    tenant_id: UUID,
    actor_id: UUID,
    *,
    allow_tenant_read: bool = False,
    allow_tenant_write: bool = False,
    request_id: str | None = None,
) -> MemoryAccessContext:
    return MemoryAccessContext.for_trusted_caller(
        tenant_id=tenant_id,
        actor_id=actor_id,
        allow_tenant_read=allow_tenant_read,
        allow_tenant_write=allow_tenant_write,
        request_id=request_id,
    )


def _audit_actions(memory_db, context: MemoryAccessContext, memory_id: UUID) -> list[str]:
    with memory_transaction(
        memory_db.pool,
        tenant_id=str(context.tenant_id),
        actor_id=str(context.actor_id),
        allow_tenant_read=context.allow_tenant_read,
        allow_tenant_write=context.allow_tenant_write,
    ) as connection:
        rows = connection.execute(
            """
            select action
            from agent.memory_audit
            where memory_id = %s
            order by created_at, action
            """,
            (memory_id,),
        ).fetchall()
    return [row[0] for row in rows]


def _metadata(memory_db, context: MemoryAccessContext, memory_id: UUID):
    with memory_transaction(
        memory_db.pool,
        tenant_id=str(context.tenant_id),
        actor_id=str(context.actor_id),
        allow_tenant_read=context.allow_tenant_read,
        allow_tenant_write=context.allow_tenant_write,
    ) as connection:
        row = connection.execute(
            "select metadata from agent.memory_item where id = %s",
            (memory_id,),
        ).fetchone()
    return row[0]


def _guc(connection: Connection) -> tuple[object, ...]:
    return connection.execute(
        """
        select
            current_setting('app.tenant_id', true),
            current_setting('app.actor_id', true),
            current_setting('app.tenant_memory_read', true),
            current_setting('app.tenant_memory_write', true)
        """
    ).fetchone()


def _assert_set_role_denied(memory_db) -> None:
    connection = _app_connection(memory_db)
    try:
        with pytest.raises(InsufficientPrivilege):
            connection.execute("set role agent_migrator")
    finally:
        connection.close()


def _assert_row_security_off_still_hides_rows(memory_db) -> None:
    connection = _app_connection(memory_db)
    try:
        connection.execute("set row_security = off")
        with pytest.raises(InsufficientPrivilege):
            connection.execute("select count(*) from agent.memory_item")
    finally:
        connection.close()


def _assert_business_schema_denied(memory_db) -> None:
    connection = _app_connection(memory_db)
    try:
        with pytest.raises(InsufficientPrivilege):
            connection.execute("select count(*) from business.contract")
    finally:
        connection.close()


def _assert_audit_trigger_blocks_app(memory_db, context: MemoryAccessContext, memory_id: UUID) -> None:
    with pytest.raises(InsufficientPrivilege):
        with memory_transaction(
            memory_db.pool,
            tenant_id=str(context.tenant_id),
            actor_id=str(context.actor_id),
            allow_tenant_read=False,
            allow_tenant_write=False,
        ) as connection:
            connection.execute(
                "update agent.memory_audit set action = 'create' where memory_id = %s",
                (memory_id,),
            )
    _set_audit_mutation(memory_db.env, granted=True)
    try:
        with pytest.raises(InsufficientPrivilege, match="append-only"):
            with memory_transaction(
                memory_db.pool,
                tenant_id=str(context.tenant_id),
                actor_id=str(context.actor_id),
                allow_tenant_read=False,
                allow_tenant_write=False,
            ) as connection:
                connection.execute(
                    "update agent.memory_audit set action = 'create' where memory_id = %s",
                    (memory_id,),
                )
        with pytest.raises(InsufficientPrivilege, match="append-only"):
            with memory_transaction(
                memory_db.pool,
                tenant_id=str(context.tenant_id),
                actor_id=str(context.actor_id),
                allow_tenant_read=False,
                allow_tenant_write=False,
            ) as connection:
                connection.execute(
                    "delete from agent.memory_audit where memory_id = %s",
                    (memory_id,),
                )
    finally:
        _set_audit_mutation(memory_db.env, granted=False)


def _set_audit_mutation(env: dict[str, str], *, granted: bool) -> None:
    verb = "grant" if granted else "revoke"
    direction = "to" if granted else "from"
    with _migrator(env) as connection:
        connection.execute(
            f"{verb} update, delete on table agent.memory_audit {direction} agent_app"
        )
        connection.execute(
            f"{verb} execute on function agent.memory_audit_immutable() {direction} agent_app"
        )


def _purge(memory_db, tenant_id: UUID) -> None:
    with _migrator(memory_db.env) as connection:
        connection.execute("delete from agent.memory_audit where tenant_id = %s", (tenant_id,))
        connection.execute("delete from agent.memory_item where tenant_id = %s", (tenant_id,))


def _pool(settings: Settings, *, min_size: int, max_size: int) -> ConnectionPool:
    return ConnectionPool(
        build_conninfo(
            settings.postgres_host,
            settings.postgres_port,
            settings.postgres_db,
            settings.agent_app_user,
            settings.agent_app_password,
        ),
        min_size=min_size,
        max_size=max_size,
        timeout=10,
        kwargs={"options": "-c search_path=agent"},
        open=True,
    )


def _settings(env: dict[str, str]) -> Settings:
    return Settings(
        postgres_host=env.get("POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(env.get("POSTGRES_PORT", "5432")),
        postgres_db=env["POSTGRES_DB"],
        agent_app_user="agent_app",
        agent_app_password=env["AGENT_APP_PASSWORD"],
        agent_migrator_user="agent_migrator",
        agent_migrator_password=env["AGENT_MIGRATOR_PASSWORD"],
    )


def _root_env() -> dict[str, str]:
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _migrator(env: dict[str, str]) -> Connection:
    settings = _settings(env)
    return Connection.connect(
        build_conninfo(
            settings.postgres_host,
            settings.postgres_port,
            settings.postgres_db,
            settings.agent_migrator_user,
            settings.agent_migrator_password,
        ),
        options="-c search_path=agent",
    )


def _app_connection(memory_db) -> Connection:
    return Connection.connect(
        build_conninfo(
            memory_db.settings.postgres_host,
            memory_db.settings.postgres_port,
            memory_db.settings.postgres_db,
            memory_db.settings.agent_app_user,
            memory_db.settings.agent_app_password,
        ),
        options="-c search_path=agent",
    )
