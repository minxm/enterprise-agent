"""Shared Redis lease and PostgreSQL fence for Knowledge QA execution tests.

Cleanup deletes only the tenants this object tracked. It does not flush Redis
or drop a database volume.
"""

from __future__ import annotations

from uuid import UUID

import redis
from psycopg import Connection
from psycopg_pool import ConnectionPool

from app.agents.knowledge_qa.lease import LeaseBridge, LeaseLoop
from app.agents.knowledge_qa.service import KnowledgeQAService
from app.persistence.postgres import build_conninfo
from app.persistence.redis_client import build_redis_client
from app.persistence.redis_thread_lock import LOCK_KEY_PREFIX, RedisThreadLock
from app.persistence.thread_fence import ThreadFenceStore


class ExecutionUnavailable(RuntimeError):
    """The local Redis or PostgreSQL credentials are not usable for this test."""


class ExecutionLease:
    def __init__(self, env: dict[str, str]) -> None:
        if not env.get("REDIS_PASSWORD") or not env.get("AGENT_APP_PASSWORD") or not env.get("POSTGRES_DB"):
            raise ExecutionUnavailable("仓库根目录 .env 未提供执行链路测试所需的连接配置")
        self.env = env
        self.tenants: list[UUID] = []
        self._closed = False
        self.loop = LeaseLoop()
        self.bridge = LeaseBridge(self.loop.loop)
        self.client = None
        self.observer = None
        self.store_pool = None
        self.store: ThreadFenceStore | None = None
        self.lock: RedisThreadLock | None = None
        try:
            self.client = build_redis_client(
                env.get("REDIS_HOST", "127.0.0.1"),
                int(env.get("REDIS_PORT", "6379")),
                env["REDIS_PASSWORD"],
            )
            self.observer = redis.Redis(
                host=env.get("REDIS_HOST", "127.0.0.1"),
                port=int(env.get("REDIS_PORT", "6379")),
                password=env["REDIS_PASSWORD"],
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
            self.store_pool = ConnectionPool(
                _conninfo(env),
                min_size=1,
                max_size=2,
                timeout=10,
                kwargs={"options": "-c search_path=agent"},
                open=True,
            )
            self.store = ThreadFenceStore(self.store_pool)
            self.lock = RedisThreadLock(self.client)
        except Exception:
            self.close()
            raise

    @classmethod
    def open(cls, env: dict[str, str]) -> "ExecutionLease":
        lease = cls(env)
        try:
            if lease.bridge.call(lease.client.ping()) is not True:
                raise ExecutionUnavailable("Redis PING 未返回成功")
            if lease.observer.ping() is not True:
                raise ExecutionUnavailable("Redis PING 未返回成功")
        except ExecutionUnavailable:
            lease.close()
            raise
        except Exception as exc:
            lease.close()
            raise ExecutionUnavailable(exc.__class__.__name__) from exc
        return lease

    def track(self, tenant_id: UUID) -> None:
        if tenant_id not in self.tenants:
            self.tenants.append(tenant_id)

    def service(self, rag: object, checkpointer: object, **kwargs: object) -> KnowledgeQAService:
        return KnowledgeQAService(
            rag,
            checkpointer=checkpointer,
            thread_lock=self.lock,
            fence_store=self.store,
            lease_loop=self.loop.loop,
            **kwargs,
        )

    def another_lock(self) -> RedisThreadLock:
        return RedisThreadLock(self.client)

    def lock_exists(self, key: str) -> bool:
        return bool(self.observer.exists(key))

    def lock_ttl(self, key: str) -> int:
        return int(self.observer.pttl(key))

    def expire_lock(self, key: str, ttl_ms: int) -> None:
        self.observer.pexpire(key, ttl_ms)

    def close(self, checkpoint_pool: object | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self._delete_redis_keys()
        observer = self.observer
        self.observer = None
        if observer is not None:
            observer.close()
        client = self.client
        self.client = None
        if client is not None and self.loop.loop.is_running():
            try:
                self.bridge.call(client.aclose())
            except Exception:
                pass
        self.loop.close()
        pool = self.store_pool
        self.store_pool = None
        if pool is not None:
            pool.close()
        self._delete_thread_run()
        self._delete_checkpoints(checkpoint_pool)

    def _delete_redis_keys(self) -> None:
        observer = self.observer
        if observer is None:
            return
        for tenant in self.tenants:
            prefix = f"{LOCK_KEY_PREFIX}{tenant}:"
            for key in observer.scan_iter(match=f"{prefix}*", count=50):
                if isinstance(key, str) and key.startswith(prefix):
                    observer.delete(key)

    def _delete_thread_run(self) -> None:
        if not self.tenants or not self.env.get("AGENT_MIGRATOR_PASSWORD"):
            return
        with Connection.connect(
            _conninfo(self.env, "agent_migrator", self.env["AGENT_MIGRATOR_PASSWORD"]),
            autocommit=True,
            options="-c search_path=agent",
        ) as connection:
            connection.execute(
                "delete from agent.thread_run where tenant_id = any(%s)",
                (self.tenants,),
            )

    def _delete_checkpoints(self, pool: object | None) -> None:
        if pool is None or not self.tenants:
            return
        for tenant in self.tenants:
            prefix = f"kq:{tenant}:%"
            with pool.connection() as connection:  # type: ignore[attr-defined]
                with connection.transaction():
                    connection.execute("select set_config('app.tenant_id', %s, true)", (str(tenant),))
                    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                        connection.execute(f"delete from agent.{table} where thread_id like %s", (prefix,))


def _conninfo(env: dict[str, str], user: str = "agent_app", password: str | None = None) -> str:
    return build_conninfo(
        env.get("POSTGRES_HOST", "127.0.0.1"),
        int(env.get("POSTGRES_PORT", "5432")),
        env["POSTGRES_DB"],
        user,
        password if password is not None else env["AGENT_APP_PASSWORD"],
    )
