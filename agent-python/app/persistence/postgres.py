import asyncio

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
