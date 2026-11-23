import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

from psycopg import Connection, OperationalError
from psycopg_pool import ConnectionPool

from app.core.config import Settings


def build_conninfo(host: str, port: int, database: str, user: str, password: str) -> str:
    if not host or not database or not password:
        raise RuntimeError("PostgreSQL 连接配置不完整：需要 POSTGRES_HOST、POSTGRES_DB 和 AGENT_APP_PASSWORD")
    return (
        f"host={host} port={port} dbname={database} user={user} "
        f"password={password} connect_timeout=3"
    )


def reject_superuser(connection: Connection) -> None:
    row = connection.execute("select rolsuper from pg_roles where rolname = current_user").fetchone()
    if row is None:
        raise RuntimeError("数据库连接不能使用超级用户")
    value = row["rolsuper"] if isinstance(row, dict) else row[0]
    if value:
        raise RuntimeError("数据库连接不能使用超级用户")


def build_agent_conninfo(settings: Settings) -> str:
    return build_conninfo(
        settings.postgres_host,
        settings.postgres_port,
        settings.postgres_db,
        settings.agent_app_user,
        settings.agent_app_password,
    )


def _public_error(prefix: str, exc: Exception, secret: str) -> RuntimeError:
    message = str(exc)
    if secret:
        message = message.replace(secret, "***")
    return RuntimeError(f"{prefix}: {message}")


class PostgresResources:
    """Sync psycopg pool used from async handlers.

    psycopg's async connections reject the Windows Proactor event loop, which
    uvicorn uses by default. Blocking calls run in a worker thread.
    """

    def __init__(self, conninfo: str, secret: str = "") -> None:
        self.conninfo = conninfo
        self.secret = secret
        self.pool: ConnectionPool | None = None

    async def open(self) -> None:
        def _open() -> ConnectionPool:
            pool = ConnectionPool(
                self.conninfo,
                min_size=1,
                max_size=4,
                timeout=3,
                open=False,
                kwargs={"options": "-c search_path=agent"},
            )
            try:
                pool.open(wait=True, timeout=5)
                with pool.connection() as connection:
                    reject_superuser(connection)
            except Exception:
                pool.close()
                raise
            return pool

        try:
            self.pool = await asyncio.to_thread(_open)
        except Exception as exc:
            self.pool = None
            raise _public_error("PostgreSQL 连接失败", exc, self.secret) from exc

    async def close(self) -> None:
        pool = self.pool
        self.pool = None
        if pool is not None:
            await asyncio.to_thread(pool.close)

    async def probe(self) -> dict[str, object]:
        if self.pool is None:
            raise RuntimeError("PostgreSQL 连接池尚未打开")

        def _probe() -> dict[str, object]:
            assert self.pool is not None
            with self.pool.connection() as connection:
                return read_probe(connection)

        try:
            return await asyncio.to_thread(_probe)
        except Exception as exc:
            raise _public_error("PostgreSQL 查询失败", exc, self.secret) from exc


async def probe_postgres(conninfo: str, secret: str = "") -> dict[str, object]:
    def _connect() -> dict[str, object]:
        try:
            with Connection.connect(conninfo, options="-c search_path=agent") as connection:
                return read_probe(connection)
        except OperationalError as exc:
            raise _public_error("PostgreSQL 连接失败", exc, secret) from exc

    return await asyncio.to_thread(_connect)


def read_probe(connection: Connection) -> dict[str, object]:
    with connection.cursor() as cursor:
        cursor.execute("select current_database(), current_user, current_schema()")
        database, user, schema = cursor.fetchone()
        cursor.execute("select extversion from pg_extension where extname = 'vector'")
        vector = cursor.fetchone()
        cursor.execute("select has_schema_privilege(current_user, 'agent', 'USAGE')")
        agent_usage = cursor.fetchone()[0]
        cursor.execute("select has_schema_privilege(current_user, 'business', 'USAGE')")
        business_usage = cursor.fetchone()[0]
        cursor.execute("select to_regclass('agent.agent_run') is null")
        agent_run_absent = cursor.fetchone()[0]
    if vector is None:
        raise RuntimeError("PostgreSQL 未启用 vector 扩展")
    return {
        "database": database,
        "user": user,
        "schema": schema,
        "vector_version": vector[0],
        "agent_usage": bool(agent_usage),
        "business_usage": bool(business_usage),
        "agent_run_absent": bool(agent_run_absent),
    }


@contextmanager
def memory_transaction(
    pool: ConnectionPool,
    *,
    tenant_id: str,
    actor_id: str,
    allow_tenant_read: bool,
    allow_tenant_write: bool,
) -> Iterator[Connection]:
    """Bind an already validated memory context for one transaction.

    The four settings are transaction-local. They tell RLS which rows to
    show. They do not authenticate the caller. The pool commits on success
    and rolls back when the block raises, which clears the settings.
    """
    if not isinstance(tenant_id, str) or not tenant_id:
        raise RuntimeError("tenant_id 不能为空")
    if not isinstance(actor_id, str) or not actor_id:
        raise RuntimeError("actor_id 不能为空")
    if type(allow_tenant_read) is not bool or type(allow_tenant_write) is not bool:
        raise RuntimeError("租户记忆授权必须是布尔值")
    read_flag = "on" if allow_tenant_read else "off"
    write_flag = "on" if allow_tenant_write else "off"
    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select
                    set_config('app.tenant_id', %s, true),
                    set_config('app.actor_id', %s, true),
                    set_config('app.tenant_memory_read', %s, true),
                    set_config('app.tenant_memory_write', %s, true)
                """,
                (tenant_id, actor_id, read_flag, write_flag),
            )
            cursor.fetchone()
            cursor.execute(
                """
                select
                    current_setting('app.tenant_id', true),
                    current_setting('app.actor_id', true),
                    current_setting('app.tenant_memory_read', true),
                    current_setting('app.tenant_memory_write', true)
                """
            )
            observed = cursor.fetchone()
        if observed != (tenant_id, actor_id, read_flag, write_flag):
            raise RuntimeError("访问上下文未能写入事务")
        yield connection


@contextmanager
def tenant_transaction(pool: ConnectionPool, tenant_id: str) -> Iterator[Connection]:
    """Run one transaction as agent_app for a single tenant.

    The pool context commits when this block exits normally and rolls the
    transaction back when it raises. set_config is local to that transaction,
    so the tenant does not stay on the pooled connection.
    """
    if not isinstance(tenant_id, str) or not tenant_id:
        raise RuntimeError("tenant_id 不能为空")
    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("select set_config('app.tenant_id', %s, true)", (tenant_id,))
            cursor.fetchone()
        yield connection
