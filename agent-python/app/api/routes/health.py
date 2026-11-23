import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import Settings, settings
from app.core.request_context import current_request_id
from app.memory.service import MemoryService
from app.persistence import infra

router = APIRouter()


def model_status(current: Settings) -> dict[str, str]:
    configured = bool(current.siliconflow_api_key)
    return {"status": "configured" if configured else "unavailable"}


def _runtime_settings(app: object) -> Settings:
    current = getattr(getattr(app, "state", None), "runtime_settings", None)
    return current if isinstance(current, Settings) else settings


async def inspect_readiness(app: object) -> dict[str, object]:
    current = _runtime_settings(app)
    models = model_status(current)
    if not current.enterprise_infra_enabled:
        return {
            "status": "ready",
            "postgres": "disabled",
            "redis": "disabled",
            "checkpointer": "disabled",
            "memory": "disabled",
            "models": models,
        }
    postgres = await _postgres_state()
    redis = await _redis_state()
    checkpointer = await _checkpointer_state()
    memory_ready = isinstance(getattr(getattr(app, "state", None), "memory", None), MemoryService)
    memory = "ok" if memory_ready and postgres == "ok" else "unavailable"
    checks = (postgres, redis, checkpointer, memory)
    return {
        "status": "ready" if all(item == "ok" for item in checks) else "not_ready",
        "postgres": postgres,
        "redis": redis,
        "checkpointer": checkpointer,
        "memory": memory,
        "models": models,
    }


async def _postgres_state() -> str:
    postgres = infra.postgres
    if postgres is None or postgres.pool is None:
        return "unavailable"
    try:
        await postgres.probe()
    except Exception:
        return "unavailable"
    return "ok"


async def _redis_state() -> str:
    redis = infra.redis
    if redis is None or not redis.opened:
        return "unavailable"
    try:
        if await redis.ping() != "PONG":
            return "unavailable"
    except Exception:
        return "unavailable"
    return "ok"


async def _checkpointer_state() -> str:
    checkpoints = infra.checkpoints
    if checkpoints is None or checkpoints.saver is None or checkpoints.pool is None:
        return "unavailable"

    def _probe() -> None:
        assert checkpoints.pool is not None
        with checkpoints.pool.connection() as connection:
            connection.execute("select 1").fetchone()

    try:
        await asyncio.to_thread(_probe)
    except Exception:
        return "unavailable"
    return "ok"


@router.get("/health", tags=["health"])
async def health_check(request: Request):
    ready = await inspect_readiness(request.app)
    return {
        "status": "ok",
        "service": "agent-runtime",
        "requestId": current_request_id(),
        "models": ready["models"],
        "readiness": ready["status"],
    }


@router.get("/ready", tags=["health"])
async def readiness(request: Request):
    report = await inspect_readiness(request.app)
    report["requestId"] = current_request_id()
    status_code = 200 if report["status"] == "ready" else 503
    return JSONResponse(status_code=status_code, content=report)
