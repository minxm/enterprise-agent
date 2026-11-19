import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agents.knowledge_qa.runtime import build_knowledge_qa_runtime
from app.api.routes.health import router as health_router
from app.api.routes.internal_knowledge_qa import router as internal_knowledge_qa_router
from app.core.config import settings
from app.core.errors import install_exception_handlers
from app.core.logging_config import configure_logging
from app.core.request_middleware import RequestIdMiddleware
from app.persistence import infra
from app.persistence.migrate import apply_migrations

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.enterprise_infra_enabled:
        await asyncio.to_thread(apply_migrations, settings)
        await infra.open(settings)
        pool = None if infra.postgres is None else infra.postgres.pool
        saver = None if infra.checkpoints is None else infra.checkpoints.saver
        if pool is not None and saver is not None:
            try:
                runtime = build_knowledge_qa_runtime(settings, pool, saver)
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
        await infra.close()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(RequestIdMiddleware)
install_exception_handlers(app)

app.include_router(
    health_router,
    prefix=settings.api_prefix,
)
app.include_router(internal_knowledge_qa_router, prefix="/internal/v1")


@app.get("/", include_in_schema=False)
def root():
    return {
        "service": "agent-runtime",
        "docs": "/docs",
    }
