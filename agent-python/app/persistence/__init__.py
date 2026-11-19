import asyncio

from app.core.config import Settings
from app.persistence.checkpoint import CheckpointResources
from app.persistence.postgres import PostgresResources, build_agent_conninfo
from app.persistence.redis_client import RedisResources, build_redis


class InfraResources:
    def __init__(self) -> None:
        self.postgres: PostgresResources | None = None
        self.redis: RedisResources | None = None
        self.checkpoints: CheckpointResources | None = None

    async def open(self, settings: Settings) -> None:
        postgres = PostgresResources(build_agent_conninfo(settings), settings.agent_app_password)
        redis = RedisResources(build_redis(settings), settings.redis_password)
        checkpoints = CheckpointResources(build_agent_conninfo(settings), settings.agent_app_password)
        try:
            await postgres.open()
            await redis.open()
            await asyncio.to_thread(checkpoints.open)
        except Exception:
            await postgres.close()
            await redis.close()
            await asyncio.to_thread(checkpoints.close)
            raise
        self.postgres = postgres
        self.redis = redis
        self.checkpoints = checkpoints

    async def close(self) -> None:
        if self.redis is not None:
            await self.redis.close()
            self.redis = None
        if self.checkpoints is not None:
            await asyncio.to_thread(self.checkpoints.close)
            self.checkpoints = None
        if self.postgres is not None:
            await self.postgres.close()
            self.postgres = None


infra = InfraResources()
