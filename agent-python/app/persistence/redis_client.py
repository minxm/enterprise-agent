from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings


def build_redis_client(host: str, port: int, password: str) -> Redis:
    if not host or not password:
        raise RuntimeError("Redis 连接配置不完整：需要 REDIS_HOST 和 REDIS_PASSWORD")
    return Redis(
        host=host,
        port=port,
        password=password,
        socket_connect_timeout=2,
        socket_timeout=2,
        decode_responses=True,
    )


def build_redis(settings: Settings) -> Redis:
    return build_redis_client(settings.redis_host, settings.redis_port, settings.redis_password)


class RedisResources:
    def __init__(self, client: Redis, secret: str = "") -> None:
        self.client = client
        self.secret = secret
        self.opened = False

    async def open(self) -> None:
        try:
            await self.ping()
        except RuntimeError:
            await self.close()
            raise
        self.opened = True

    async def ping(self) -> str:
        try:
            if await self.client.ping() is not True:
                raise RuntimeError("Redis PING 未返回成功")
        except RedisError as exc:
            message = str(exc).replace(self.secret, "***") if self.secret else str(exc)
            raise RuntimeError(f"Redis 连接失败: {message}") from exc
        return "PONG"

    async def close(self) -> None:
        await self.client.aclose()
        self.opened = False
