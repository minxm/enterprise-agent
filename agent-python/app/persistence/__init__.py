from app.core.config import Settings
from app.persistence.postgres import PostgresResources, build_agent_conninfo
from app.persistence.redis_client import RedisResources, build_redis


class InfraResources:
    def __init__(self) -> None:
        self.postgres: PostgresResources | None = None
        self.redis: RedisResources | None = None

    async def open(self, settings: Settings) -> None:
        postgres = PostgresResources(build_agent_conninfo(settings), settings.agent_app_password)
        redis = RedisResources(build_redis(settings), settings.redis_password)
        try:
            await postgres.open()
            await redis.open()
        except Exception:
            await postgres.close()
            await redis.close()
            raise
        self.postgres = postgres
        self.redis = redis

    async def close(self) -> None:
        if self.redis is not None:
            await self.redis.close()
            self.redis = None
        if self.postgres is not None:
            await self.postgres.close()
            self.postgres = None


infra = InfraResources()
