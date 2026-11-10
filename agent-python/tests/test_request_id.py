import asyncio
import logging

import httpx
from fastapi.testclient import TestClient

from app.core.request_context import current_request_id
from app.main import app

client = TestClient(app)


def test_missing_id_uses_a_safe_fallback():
    response = client.get("/api/v1/health")
    request_id = response.headers["x-request-id"]
    assert response.status_code == 200
    assert request_id
    assert response.json()["requestId"] == request_id
    assert " " not in request_id
    assert current_request_id() is None


def test_valid_id_is_preserved(caplog):
    caplog.set_level("INFO")
    response = client.get("/api/v1/health", headers={"X-Request-ID": "trace_shared-1"})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "trace_shared-1"
    assert response.json()["requestId"] == "trace_shared-1"
    assert response.json()["status"] == "ok"
    assert any("requestId=trace_shared-1" in record.message for record in caplog.records)
    formatter = logging.Formatter("[requestId=%(requestId)s] %(message)s")
    rendered = formatter.format(next(record for record in caplog.records if "request completed" in record.message))
    assert "[requestId=trace_shared-1]" in rendered
    assert current_request_id() is None


def test_unsafe_id_is_replaced():
    response = client.get("/api/v1/health", headers={"X-Request-ID": "bad id\n<script>"})
    request_id = response.headers["x-request-id"]
    assert request_id != "bad id\n<script>"
    assert " " not in request_id
    assert "<" not in request_id
    assert response.json()["requestId"] == request_id


def test_unexpected_error_hides_the_stack():
    @app.get("/api/v1/test/boom")
    def boom():
        raise RuntimeError("secret database password=hidden")

    response = client.get("/api/v1/test/boom", headers={"X-Request-ID": "trace-error"})
    body = response.json()
    assert response.status_code == 500
    assert response.headers["x-request-id"] == "trace-error"
    assert body["requestId"] == "trace-error"
    assert body["message"] == "服务内部错误"
    assert "secret" not in response.text
    assert "Traceback" not in response.text
    assert current_request_id() is None


def test_concurrent_requests_do_not_mix_ids():
    asyncio.run(_concurrent_requests())


async def _concurrent_requests():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        async def one(index: int) -> None:
            request_id = f"trace-{index}"
            response = await async_client.get("/api/v1/health", headers={"X-Request-ID": request_id})
            assert response.status_code == 200
            assert response.headers["x-request-id"] == request_id
            assert response.json()["requestId"] == request_id

        await asyncio.gather(*(one(index) for index in range(20)))
    assert current_request_id() is None
