"""Pytest configuration for apps/api.

Tests require Postgres + Redis up and migrations + seeds applied.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import get_engine
from app.redis import get_redis


def _reachable() -> bool:
    try:
        with get_engine().connect() as conn:
            from sqlalchemy import text
            conn.execute(text("SELECT 1"))
        get_redis().ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def client() -> TestClient:
    if not os.environ.get("DATABASE_URL") or not os.environ.get("REDIS_URL"):
        pytest.skip("DATABASE_URL/REDIS_URL not set; bring up the stack first.")
    if not _reachable():
        pytest.skip("DB or Redis unreachable; run `make up` and `make migrate && make seed`.")
    return TestClient(app)