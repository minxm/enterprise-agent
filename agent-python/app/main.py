from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes.health import router as health_router
from app.core.config import settings
from app.core.errors import install_exception_handlers
from app.core.logging_config import configure_logging
from app.core.request_middleware import RequestIdMiddleware
from app.persistence import infra

configure_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.enterprise_infra_enabled:
        await infra.open(settings)
    try:
        yield
    finally:
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


@app.get("/", include_in_schema=False)
def root():
    return {
        "service": "agent-runtime",
        "docs": "/docs",
    }
