"""Checkpoint writes that refuse a stale fence.

Reviewed against langgraph-checkpoint-postgres 3.2.0
(langgraph 1.2.20, langgraph-checkpoint 4.3.1):

- PostgresSaver.put: langgraph/checkpoint/postgres/__init__.py lines 268-350
- PostgresSaver.put_writes: same file, lines 352-384
- PostgresSaver._cursor: same file, lines 476-514
- AsyncPostgresSaver.aput / aput_writes: aio.py, both use _cursor(pipeline=True)

The installed saver opens a pooled connection with autocommit=True and, when
psycopg supports pipelines, wraps put and put_writes in pipeline() without
BEGIN. Calling PostgresSaver.put after a separate fence check would commit
the check on one connection and write the checkpoint on another.

This class therefore does not call put or put_writes on the parent. It keeps
the parent's SQL constants, blob splitting, serializer, and write-index
rules, and runs them on the same cursor as the fence UPDATE inside
Connection.transaction(). psycopg issues BEGIN for that block and COMMIT
only when the block exits without an error. The pool then sees an idle
connection and returns it.

Upgrade review: compare those four methods, UPSERT_CHECKPOINT_BLOBS_SQL,
UPSERT_CHECKPOINTS_SQL, UPSERT_CHECKPOINT_WRITES_SQL,
INSERT_CHECKPOINT_WRITES_SQL, _dump_blobs, and _dump_writes. The application
does not use AsyncPostgresSaver or ShallowPostgresSaver. There is no public
hook that runs a statement inside put's transaction.

The process-wide CheckpointResources still constructs PostgresSaver. Graph
entry is unchanged until a later change enters fence_scope before the run.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib.metadata import version
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    get_serializable_checkpoint_metadata,
)
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres._internal import get_connection
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from psycopg import Cursor
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from app.persistence.fence_context import FenceContext, current_fence
from app.persistence.thread_fence import FenceContextError, FenceStoreError

SUPPORTED_CHECKPOINT_POSTGRES = "3.2.0"

_FENCE_TOUCH = """
update agent.thread_run
set fence = fence
where tenant_id = %s
  and thread_id = %s
  and fence = %s
"""


class FencingViolation(Exception):
    """The stored fence is not the fence held by this run."""


class FenceIntegrityError(Exception):
    """The fence update affected an unexpected number of rows."""


class FencedPostgresSaver(PostgresSaver):
    """PostgresSaver writes guarded by agent.thread_run."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        installed = version("langgraph-checkpoint-postgres")
        if installed != SUPPORTED_CHECKPOINT_POSTGRES:
            raise RuntimeError(
                "FencedPostgresSaver 只核对过 "
                f"langgraph-checkpoint-postgres {SUPPORTED_CHECKPOINT_POSTGRES}，"
                f"当前是 {installed}"
            )
        super().__init__(*args, **kwargs)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        context = current_fence()
        configurable = config["configurable"].copy()
        thread_id = configurable.pop("thread_id")
        checkpoint_ns = configurable.pop("checkpoint_ns")
        checkpoint_id = configurable.pop("checkpoint_id", None)
        _require_same_thread(context, thread_id)
        copy = checkpoint.copy()
        copy["channel_values"] = copy["channel_values"].copy()
        next_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }
        blob_values: dict[str, Any] = {}
        for key, value in checkpoint["channel_values"].items():
            if isinstance(value, _DeltaSnapshot):
                blob_values[key] = copy["channel_values"].pop(key)
                copy["channel_values"][key] = True
            elif value is None or isinstance(value, (str, int, float, bool)):
                pass
            else:
                blob_values[key] = copy["channel_values"].pop(key)
        with self._fenced_transaction(context) as cursor:
            self._write_checkpoint(
                cursor,
                thread_id,
                checkpoint_ns,
                checkpoint,
                checkpoint_id,
                copy,
                config,
                metadata,
                new_versions,
                blob_values,
            )
        return next_config

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        context = current_fence()
        thread_id = config["configurable"]["thread_id"]
        _require_same_thread(context, thread_id)
        query = (
            self.UPSERT_CHECKPOINT_WRITES_SQL
            if all(write[0] in WRITES_IDX_MAP for write in writes)
            else self.INSERT_CHECKPOINT_WRITES_SQL
        )
        with self._fenced_transaction(context) as cursor:
            self._write_pending(cursor, query, config, writes, task_id, task_path)

    def _write_checkpoint(
        self,
        cursor: Cursor[DictRow],
        thread_id: str,
        checkpoint_ns: str,
        checkpoint: Checkpoint,
        parent_checkpoint_id: str | None,
        copy: Checkpoint,
        config: RunnableConfig,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
        blob_values: dict[str, Any],
    ) -> None:
        blob_versions = {key: value for key, value in new_versions.items() if key in blob_values}
        if blob_versions:
            cursor.executemany(
                self.UPSERT_CHECKPOINT_BLOBS_SQL,
                self._dump_blobs(thread_id, checkpoint_ns, blob_values, blob_versions),
            )
        cursor.execute(
            self.UPSERT_CHECKPOINTS_SQL,
            (
                thread_id,
                checkpoint_ns,
                checkpoint["id"],
                parent_checkpoint_id,
                Jsonb(copy),
                Jsonb(get_serializable_checkpoint_metadata(config, metadata)),
            ),
        )

    def _write_pending(
        self,
        cursor: Cursor[DictRow],
        query: str,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str,
    ) -> None:
        cursor.executemany(
            query,
            self._dump_writes(
                config["configurable"]["thread_id"],
                config["configurable"]["checkpoint_ns"],
                config["configurable"]["checkpoint_id"],
                task_id,
                task_path,
                writes,
            ),
        )

    @contextmanager
    def _fenced_transaction(self, context: FenceContext) -> Iterator[Cursor[DictRow]]:
        with self.lock, get_connection(self.conn) as connection:
            with connection.transaction():
                with connection.cursor(binary=True, row_factory=dict_row) as cursor:
                    _require_current_fence(cursor, context)
                    yield cursor


def _require_same_thread(context: FenceContext, thread_id: object) -> None:
    if thread_id != context.thread_id:
        raise FenceContextError("写入目标与执行上下文不一致")


def _require_current_fence(cursor: Cursor[DictRow], context: FenceContext) -> None:
    cursor.execute("select set_config('app.tenant_id', %s, true)", (str(context.tenant_id),))
    written = _scalar(cursor.fetchone())
    cursor.execute("select current_setting('app.tenant_id', true)")
    observed = _scalar(cursor.fetchone())
    if written != str(context.tenant_id) or observed != str(context.tenant_id):
        raise FenceStoreError("租户上下文未能写入事务")
    cursor.execute(
        _FENCE_TOUCH,
        (context.tenant_id, context.thread_id, context.fence),
    )
    if cursor.rowcount == 0:
        raise FencingViolation("fence 已失效")
    if cursor.rowcount != 1:
        raise FenceIntegrityError("fence 命中行数异常")


def _scalar(row: DictRow | tuple[object, ...] | None) -> str:
    if row is None:
        raise FenceStoreError("租户上下文未能写入事务")
    value = next(iter(row.values())) if isinstance(row, dict) else row[0]
    if not isinstance(value, str) or not value:
        raise FenceStoreError("租户上下文未能写入事务")
    return value
