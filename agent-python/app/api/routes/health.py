from fastapi import APIRouter

from app.core.request_context import current_request_id

router = APIRouter()


@router.get("/health", tags=["health"])
def health_check():
    return {
        "status": "ok",
        "service": "agent-runtime",
        "requestId": current_request_id(),
    }
