"""One long-lived fenced checkpoint saver for Knowledge QA threads.

The pool is opened with the process and closed with it. Requests borrow a
connection from that pool. This module does not call PostgresSaver.setup();
migration 004 owns the tables.
"""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.persistence.fenced_saver import FencedPostgresSaver
from app.persistence.postgres import _public_error, reject_superuser


class CheckpointResources:
    def __init__(self, conninfo: str, secret: str = "") -> None:
        self.conninfo = conninfo
        self.secret = secret
        self.pool: ConnectionPool | None = None
        self.saver: FencedPostgresSaver | None = None

    def open(self) -> None:
        pool = ConnectionPool(
            self.conninfo,
            min_size=1,
            max_size=4,
            timeout=10,
            open=False,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
                "options": "-c search_path=agent",
            },
        )
        try:
            pool.open(wait=True, timeout=5)
            with pool.connection() as connection:
                reject_superuser(connection)
        except Exception as exc:
            pool.close()
            raise _public_error("工作流状态存储连接失败", exc, self.secret) from exc
        self.pool = pool
        self.saver = FencedPostgresSaver(pool)

    def close(self) -> None:
        pool = self.pool
        self.pool = None
        self.saver = None
        if pool is not None:
            pool.close()
