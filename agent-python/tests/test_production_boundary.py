import logging
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from app.agents.knowledge_qa.service import KnowledgeQAService
from app.core.config import Settings, settings
from app.core.internal_auth import require_gateway
from app.core.log_safety import diagnostic
from app.main import app, create_app
from app.persistence import infra


def _production(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "prod",
        "enterprise_infra_enabled": True,
        "postgres_host": "127.0.0.1",
        "postgres_db": "enterprise",
        "agent_app_user": "agent_app",
        "agent_app_password": "app-secret",
        "agent_migrator_user": "agent_migrator",
        "agent_migrator_password": "migrator-secret",
        "redis_host": "127.0.0.1",
        "redis_password": "redis-secret",
        "agent_internal_token": "internal-token-value",
        "java_base_url": "http://127.0.0.1:8080",
    }
    values.update(overrides)
    return Settings(**values)


def test_dev_settings_keep_the_optional_defaults():
    current = Settings(app_env="dev", enterprise_infra_enabled=False, agent_internal_token="")
    assert current.app_env == "dev"
    assert current.enterprise_infra_enabled is False
    assert current.agent_internal_token == ""


def test_prod_does_not_require_the_migrator_password():
    current = _production(agent_migrator_password="")
    assert current.app_env == "prod"
    assert current.agent_migrator_password == ""
    assert current.agent_app_user == "agent_app"


def test_prod_lifespan_checks_schema_without_migrating(monkeypatch):
    calls: list[str] = []

    def prepare(current):
        calls.append(current.app_env)
        if current.agent_migrator_password:
            raise AssertionError("prod used the migrator password")

    async def open_infra(_current):
        calls.append("open")

    monkeypatch.setattr("app.main.prepare_database", prepare)
    monkeypatch.setattr(infra, "open", open_infra)
    with TestClient(create_app(_production(agent_migrator_password=""))):
        pass
    assert calls == ["prod", "open"]


def test_prod_requires_infrastructure_database_and_token():
    with pytest.raises(ValueError, match="ENTERPRISE_INFRA_ENABLED"):
        _production(enterprise_infra_enabled=False)
    with pytest.raises(ValueError, match="AGENT_APP_PASSWORD") as missing_database:
        _production(agent_app_password="")
    assert "migrator-secret" not in str(missing_database.value)
    with pytest.raises(ValueError, match="AGENT_INTERNAL_TOKEN") as missing_token:
        _production(agent_internal_token="")
    message = str(missing_token.value)
    assert "app-secret" not in message
    assert "migrator-secret" not in message
    assert "redis-secret" not in message
    assert "input_value" not in message


def test_prod_rejects_shared_or_superuser_database_accounts():
    with pytest.raises(ValueError, match="迁移账号不能与业务账号相同"):
        _production(agent_migrator_user="agent_app")
    with pytest.raises(ValueError, match="超级用户"):
        _production(agent_app_user="postgres")
    with pytest.raises(ValueError, match="业务连接必须使用 agent_app"):
        _production(agent_app_user="business_app")


def test_diagnostic_hides_secrets_and_keeps_safe_provider_detail():
    secret = RuntimeError("connection postgres://agent:password@db/app sk-live-secret")
    assert diagnostic(secret) == "RuntimeError"
    visible = diagnostic(RuntimeError("Rerank 请求失败，HTTP 503，trace_id=trace-24"))
    assert "trace-24" in visible
    assert "sk-" not in visible


def test_unexpected_error_log_does_not_include_the_secret(caplog):
    from app.core.errors import _unexpected_error

    caplog.set_level(logging.ERROR)

    class Request:
        pass

    import asyncio

    asyncio.run(_unexpected_error(Request(), RuntimeError("password=hunter2 sk-abcdef12345678")))
    assert "hunter2" not in caplog.text
    assert "sk-" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_every_internal_route_checks_the_gateway_token(monkeypatch):
    monkeypatch.setattr(settings, "agent_internal_token", "expected-token")
    paths = []
    for route in app.routes:
        original = getattr(route, "original_router", None)
        context = getattr(route, "include_context", None)
        if original is None or not str(getattr(context, "prefix", "")).startswith("/internal/"):
            continue
        prefix = context.prefix
        for nested in original.routes:
            path = prefix + nested.path
            paths.append(path)
            calls = [dependency.call for dependency in nested.dependant.dependencies]
            assert require_gateway in calls
    assert "/internal/v1/knowledge-qa/stream" in paths
    assert "/internal/v1/memories" in paths

    client = TestClient(app)
    missing = client.post("/internal/v1/memories", json={})
    wrong = client.post("/internal/v1/memories", json={}, headers={"X-Agent-Token": "short"})
    accepted = client.post(
        "/internal/v1/memories",
        json={"thread_id": "client-picked", "tenant_id": "forged"},
        headers={"X-Agent-Token": "expected-token"},
    )
    stream = client.post(
        "/internal/v1/knowledge-qa/stream",
        json={"thread_id": "client-picked"},
        headers={"X-Agent-Token": "expected-token"},
    )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert accepted.status_code in {422, 503}
    assert stream.status_code in {422, 503}
    assert "forged" not in accepted.text


def test_prod_closes_api_docs():
    application = create_app(_production())

    @asynccontextmanager
    async def quiet(_app):
        yield

    application.router.lifespan_context = quiet
    client = TestClient(application)
    assert application.docs_url is None
    assert application.redoc_url is None
    assert application.openapi_url is None
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert "docs" not in client.get("/").json()


def test_dev_docs_remain_available():
    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
    assert app.openapi_url == "/openapi.json"


def test_health_does_not_call_a_model_or_hide_a_missing_key(monkeypatch):
    monkeypatch.setattr(settings, "siliconflow_api_key", "")
    monkeypatch.setattr(settings, "reranker_api_key", "")
    response = TestClient(app).get("/api/v1/health")
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["service"] == "agent-runtime"
    assert body["models"]["status"] == "unavailable"
    assert "sk-" not in response.text


def test_readiness_reports_database_failure_without_secrets(monkeypatch):
    class BrokenPostgres:
        pool = object()

        async def probe(self):
            raise RuntimeError("password=hunter2 host=db")

    monkeypatch.setattr(settings, "enterprise_infra_enabled", True)
    monkeypatch.setattr(infra, "postgres", BrokenPostgres())
    monkeypatch.setattr(infra, "redis", None)
    monkeypatch.setattr(infra, "checkpoints", None)
    response = TestClient(app).get("/api/v1/ready")
    body = response.json()
    assert response.status_code == 503
    assert body["status"] == "not_ready"
    assert body["postgres"] == "unavailable"
    assert "hunter2" not in response.text


def test_knowledge_qa_does_not_keep_a_process_local_execution_lock():
    service = KnowledgeQAService(object())
    assert not hasattr(service, "_locks")


def test_dev_prepare_still_migrates(monkeypatch):
    from app.persistence import migrate as migration

    calls: list[str] = []
    monkeypatch.setattr(migration, "apply_migrations", lambda settings: calls.append("migrate"))
    monkeypatch.setattr(migration, "assert_schema_ready", lambda settings: calls.append("check"))
    current = Settings(
        app_env="dev",
        enterprise_infra_enabled=True,
        postgres_host="127.0.0.1",
        postgres_db="enterprise",
        agent_migrator_password="unused",
    )
    migration.prepare_database(current)
    assert calls == ["migrate"]
