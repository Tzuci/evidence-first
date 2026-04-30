"""Health endpoints.

/health/live always returns 200.
/health/ready returns 200 only when db, redis, and storage are all 'ok',
otherwise returns 503 with the same JSON body shape.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..db import get_engine
from ..redis import get_redis
from ..storage import storage_health


router = APIRouter(tags=["health"])


def _db_status() -> str:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        return "fail"


def _redis_status() -> str:
    try:
        get_redis().ping()
        return "ok"
    except Exception:
        return "fail"


@router.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/db")
def db_health() -> dict[str, str]:
    return {"db": _db_status()}


@router.get("/health/queue")
def queue_health() -> dict[str, str]:
    return {"redis": _redis_status()}


@router.get("/health/storage")
def storage_health_endpoint() -> dict[str, str]:
    return {"storage": storage_health()}


@router.get("/health/ready")
def ready() -> JSONResponse:
    body = {
        "db": _db_status(),
        "redis": _redis_status(),
        "storage": storage_health(),
    }
    all_ok = body["db"] == "ok" and body["redis"] == "ok" and body["storage"] == "ok"
    status = 200 if all_ok else 503
    return JSONResponse(status_code=status, content=body)