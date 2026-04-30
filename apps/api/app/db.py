"""Database connection helpers (SQLAlchemy 2.0 Core)."""
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
            pool_size=5,
            max_overflow=5,
        )
    return _engine


@contextmanager
def transaction() -> Iterator[Connection]:
    """Context manager that yields a Connection in an explicit transaction.

    The transaction is committed when the block exits normally, rolled back on
    exception. Used by routes that need to commit BEFORE performing side effects
    on external systems (e.g., emitting Redis events post-commit).
    """
    eng = get_engine()
    with eng.begin() as conn:
        yield conn


def get_conn() -> Iterator[Connection]:
    """FastAPI dependency that yields a transactional Connection.

    NOTE: routes that need post-commit side effects (Redis publish, etc.) must
    NOT rely on get_conn — they should use the transaction() context manager
    directly so they can run side effects after the explicit commit.
    """
    eng = get_engine()
    with eng.begin() as conn:
        yield conn