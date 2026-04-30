"""Redis client (sync) with connection pool."""
from __future__ import annotations

from redis import Redis, ConnectionPool

from .config import get_settings


_pool: ConnectionPool | None = None


def get_redis() -> Redis:
    global _pool
    if _pool is None:
        _pool = ConnectionPool.from_url(get_settings().REDIS_URL, decode_responses=True)
    return Redis(connection_pool=_pool)