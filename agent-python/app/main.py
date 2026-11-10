from fastapi import FastAPI

from app.api.routes.health import router as health_router
from app.core.config import settings

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
)

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
