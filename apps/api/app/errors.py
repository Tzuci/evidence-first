"""Global error handlers and request id middleware."""
from __future__ import annotations

import uuid
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from evidencefirst_shared.errors import ErrorCode, NormalizedError


REQUEST_ID_HEADER = "X-Request-ID"


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable],
    ):
        rid = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = rid
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = rid
        return response


def _envelope(err: NormalizedError, request: Request) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    return JSONResponse(status_code=err.http_status, content=err.to_envelope(request_id=rid))


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NormalizedError)
    async def _on_normalized_error(request: Request, exc: NormalizedError):
        return _envelope(exc, request)

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError):
        err = NormalizedError(
            ErrorCode.VALIDATION_ERROR,
            "Request validation failed",
            details={"errors": exc.errors()},
        )
        return _envelope(err, request)

    @app.exception_handler(Exception)
    async def _on_unexpected(request: Request, exc: Exception):
        err = NormalizedError(
            ErrorCode.INTERNAL_ERROR,
            "Unexpected internal error",
            details={"exception_type": type(exc).__name__},
        )
        return _envelope(err, request)