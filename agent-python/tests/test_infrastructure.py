import asyncio
from pathlib import Path

import pytest

from app.core.config import Settings
from app.persistence.postgres import build_conninfo, probe_postgres
from app.persistence.redis_client import RedisResources, build_redis_client
from app.persistence import InfraResources


def test_missing_postgres_config_is_explicit():
    with pytest.raises(RuntimeError, match="PostgreSQL 连接配置不完整"):
        build_conninfo("", 5432, "", "agent_app", "")


def test_missing_redis_config_is_explicit():
    with pytest.raises(RuntimeError, match="Redis 连接配置不完整"):
        build_redis_client("", 6379, "")


def test_unreachable_postgres_reports_an_error():
    with pytest.raises(RuntimeError, match="PostgreSQL 连接失败"):
        asyncio.run(probe_postgres(
            "host=127.0.0.1 port=1 dbname=enterprise user=agent_app password=invalid connect_timeout=2",
            "invalid",
        ))


def test_unreachable_redis_reports_an_error():
    resources = RedisResources(build_redis_client("127.0.0.1", 1, "invalid"), "invalid")

    async def attempt() -> None:
        try:
            await resources.ping()
        finally:
            await resources.close()

    with pytest.raises(RuntimeError, match="Redis 连接失败"):
        asyncio.run(attempt())


def test_agent_app_can_use_postgres_and_redis():
    env = _root_env()
    if not env.get("AGENT_APP_PASSWORD") or not env.get("REDIS_PASSWORD"):
        pytest.skip("仓库根目录 .env 未提供数据库密码，跳过真实连接测试")
    settings = Settings(
        postgres_host=env.get("POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(env.get("POSTGRES_PORT", "5432")),
        postgres_db=env["POSTGRES_DB"],
        agent_app_user="agent_app",
        agent_app_password=env["AGENT_APP_PASSWORD"],
        redis_host=env.get("REDIS_HOST", "127.0.0.1"),
        redis_port=int(env.get("REDIS_PORT", "6379")),
        redis_password=env["REDIS_PASSWORD"],
    )
    asyncio.run(_verify(settings))


async def _verify(settings: Settings) -> None:
    resources = InfraResources()
    await resources.open(settings)
    try:
        assert resources.postgres is not None
        probe = await resources.postgres.probe()
        assert probe["user"] == "agent_app"
        assert probe["schema"] == "agent"
        assert probe["vector_version"]
        assert probe["agent_usage"] is True
        assert probe["business_usage"] is False
        assert probe["agent_run_absent"] is True
        assert resources.redis is not None
        assert await resources.redis.ping() == "PONG"
    finally:
        await resources.close()
    assert resources.postgres is None
    assert resources.redis is None


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
