import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agents.knowledge_qa.runtime import build_knowledge_qa_runtime
from app.api.routes.health import router as health_router
from app.api.routes.internal_knowledge_qa import router as internal_knowledge_qa_router
from app.api.routes.internal_memory import router as internal_memory_router
from app.memory.service import MemoryService
from app.core.config import settings
from app.core.errors import install_exception_handlers
from app.core.logging_config import configure_logging
from app.core.request_middleware import RequestIdMiddleware
from app.persistence import infra
from app.persistence.migrate import prepare_database

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    current = getattr(_app.state, "runtime_settings", settings)
    if current.enterprise_infra_enabled:
        await asyncio.to_thread(prepare_database, current)
        await infra.open(current)
        pool = None if infra.postgres is None else infra.postgres.pool
        saver = None if infra.checkpoints is None else infra.checkpoints.saver
        if pool is not None:
            _app.state.memory = MemoryService(pool)
        if pool is not None and saver is not None:
            try:
                runtime = build_knowledge_qa_runtime(current, pool, saver)
            except Exception as exc:
                logger.warning("知识问答运行时未装配 error=%s", exc.__class__.__name__)
            else:
                _app.state.knowledge_qa = runtime.service
                _app.state.knowledge_qa_runtime = runtime
    try:
        yield
    finally:
        runtime = getattr(_app.state, "knowledge_qa_runtime", None)
        if runtime is not None:
            await asyncio.to_thread(runtime.close)
            _app.state.knowledge_qa = None
            _app.state.knowledge_qa_runtime = None
        _app.state.memory = None
        await infra.close()


def create_app(runtime_settings=None) -> FastAPI:
    current = runtime_settings or settings
    production = current.app_env == "prod"
    application = FastAPI(
        title=current.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    application.state.runtime_settings = current
    application.add_middleware(RequestIdMiddleware)
    install_exception_handlers(application)
    application.include_router(health_router, prefix=current.api_prefix)
    application.include_router(internal_knowledge_qa_router, prefix="/internal/v1")
    application.include_router(internal_memory_router, prefix="/internal/v1")

    @application.get("/", include_in_schema=False)
    def root():
        body = {"service": "agent-runtime"}
        if current.app_env != "prod":
            body["docs"] = "/docs"
        return body

    return application


app = create_app()
