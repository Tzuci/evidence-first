"""Database engine for the worker."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine

from .config import get_settings


_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            get_settings().DATABASE_URL,
            future=True,
            pool_pre_ping=True,
            pool_size=3,
            max_overflow=3,
        )
    return _engine


@contextmanager
def transaction() -> Iterator[Connection]:
    eng = get_engine()
    with eng.begin() as conn:
        yield conn