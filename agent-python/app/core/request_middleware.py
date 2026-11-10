import logging

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.request_context import REQUEST_ID_HEADER, reset_request_id, resolve_request_id, set_request_id

logger = logging.getLogger("app.request")


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = resolve_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        token = set_request_id(request_id)
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = list(message.get("headers", []))
                if not any(name.lower() == b"x-request-id" for name, _value in headers):
                    headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            try:
                await self.app(scope, receive, send_with_request_id)
                logger.info(
                    "request completed %s %s requestId=%s",
                    scope.get("method"),
                    scope.get("path"),
                    request_id,
                )
            except Exception:
                if response_started:
                    raise
                logger.exception("Unhandled request failure requestId=%s", request_id)
                response = JSONResponse(
                    status_code=500,
                    content={"code": 500, "message": "服务内部错误", "requestId": request_id},
                    headers={REQUEST_ID_HEADER: request_id},
                )
                await response(scope, receive, send)
        finally:
            reset_request_id(token)
