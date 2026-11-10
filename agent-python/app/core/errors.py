import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.request_context import REQUEST_ID_HEADER, current_request_id

logger = logging.getLogger("app.request")


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected_error)


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    logger.warning("Rejected invalid request requestId=%s", current_request_id())
    return _error(422, "请求参数不合法")


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    status_code = exc.status_code if isinstance(exc, HTTPException) else 500
    message = "服务内部错误" if status_code >= 500 else "请求无法完成"
    logger.warning("HTTP error status=%s requestId=%s", status_code, current_request_id())
    return _error(status_code, message)


async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled request failure requestId=%s", current_request_id())
    return _error(500, "服务内部错误")


def _error(status_code: int, message: str) -> JSONResponse:
    request_id = current_request_id()
    return JSONResponse(
        status_code=status_code,
        content={"code": status_code, "message": message, "requestId": request_id},
        headers={REQUEST_ID_HEADER: request_id or ""},
    )
