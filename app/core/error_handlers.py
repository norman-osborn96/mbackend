"""Global HTTP exception mapping for MailPulse."""

from __future__ import annotations

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from slowapi.errors import RateLimitExceeded

from app.core.exceptions import MailPulseBaseException
from app.core.logging import request_id_var


def _error_body(code: str, message: str) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id_var.get("-"),
        }
    }


async def mailpulse_exception_handler(request: Request, exc: MailPulseBaseException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(exc.code, str(exc)),
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, list):
        message = jsonable_encoder(detail)
    else:
        message = str(detail)
    code = "HTTP_ERROR"
    if exc.status_code == 401:
        code = "NOT_AUTHENTICATED"
    elif exc.status_code == 404:
        code = "NOT_FOUND"
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(code, message if isinstance(message, str) else str(message)),
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_error_body("VALIDATION_ERROR", str(exc.errors())),
    )


async def rate_limit_exception_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    retry = getattr(exc, "retry_after", None)
    if retry is None:
        hdr = getattr(exc, "headers", None) or {}
        retry = hdr.get("Retry-After", 60)
    try:
        retry_int = max(1, int(retry))
    except (TypeError, ValueError):
        retry_int = 60
    return JSONResponse(
        status_code=429,
        content=_error_body("RATE_LIMITED", str(exc.detail)),
        headers={"Retry-After": str(retry_int)},
    )
