"""Normalized error codes and exception type for Evidence-First."""
from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RBAC_FORBIDDEN = "RBAC_FORBIDDEN"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    RESOURCE_CONFLICT = "RESOURCE_CONFLICT"
    STORAGE_INLINE_TOO_LARGE = "STORAGE_INLINE_TOO_LARGE"
    APPEND_ONLY_VIOLATION = "APPEND_ONLY_VIOLATION"
    IDEMPOTENCY_REPLAY_MISMATCH = "IDEMPOTENCY_REPLAY_MISMATCH"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    DOCUMENT_STORAGE_NOT_READY = "DOCUMENT_STORAGE_NOT_READY"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# Default HTTP status code for each error.
HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.AUTH_REQUIRED: 401,
    ErrorCode.RBAC_FORBIDDEN: 403,
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.RESOURCE_CONFLICT: 409,
    ErrorCode.STORAGE_INLINE_TOO_LARGE: 413,
    ErrorCode.APPEND_ONLY_VIOLATION: 409,
    ErrorCode.IDEMPOTENCY_REPLAY_MISMATCH: 409,
    ErrorCode.RATE_LIMIT_EXCEEDED: 429,
    ErrorCode.DOCUMENT_STORAGE_NOT_READY: 501,
    ErrorCode.INTERNAL_ERROR: 500,
}


class NormalizedError(Exception):
    """Application exception that maps to the normalized error envelope.

    Envelope shape:
        {
          "error": {
            "code": "...",
            "message": "...",
            "details": {...},
            "request_id": "...",
            "policy_version": null,
            "remediation_hint": "..."
          }
        }
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        remediation_hint: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.remediation_hint = remediation_hint
        self.http_status = http_status or HTTP_STATUS.get(code, 500)

    def to_envelope(
        self,
        *,
        request_id: str | None = None,
        policy_version: str | None = None,
    ) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details,
                "request_id": request_id,
                "policy_version": policy_version,
                "remediation_hint": self.remediation_hint,
            }
        }


# ---------------------------------------------------------------------------
# FastAPI integration
# ---------------------------------------------------------------------------
def install_normalized_error_handler(app: Any) -> None:
    """Install FastAPI exception handlers for normalized errors.

    Response envelope:
        {"error": {"code": "...", "message": "...", "details": {...}, ...}}

    This handles:
      - application-raised NormalizedError;
      - FastAPI/Pydantic RequestValidationError, so validation failures do not
        leak FastAPI's default {"detail": [...]} shape.
    """
    from fastapi import Request
    from fastapi.encoders import jsonable_encoder
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    @app.exception_handler(NormalizedError)
    async def _normalized_error_handler(
        request: Request,
        exc: NormalizedError,
    ) -> JSONResponse:
        request_id = request.headers.get("x-request-id")
        return JSONResponse(
            status_code=exc.http_status,
            content=exc.to_envelope(request_id=request_id),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        request_id = request.headers.get("x-request-id")
        normalized = NormalizedError(
            ErrorCode.VALIDATION_ERROR,
            "Validation error",
            details={"errors": jsonable_encoder(exc.errors())},
            http_status=HTTP_STATUS[ErrorCode.VALIDATION_ERROR],
        )
        return JSONResponse(
            status_code=normalized.http_status,
            content=normalized.to_envelope(request_id=request_id),
        )
