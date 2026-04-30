"""
Configurazione pytest condivisa per i test DB root.

I test richiedono un Postgres locale raggiungibile via DATABASE_URL.
Supporta sia URL SQLAlchemy:
  postgresql+psycopg://...
sia URL libpq/psycopg:
  postgresql://...
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import pytest
import psycopg


def _psycopg_url(url: str) -> str:
    """Convert SQLAlchemy-style psycopg URL into psycopg/libpq URL."""
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url.removeprefix("postgresql+psycopg://")
    return url


def _db_reachable(url: str) -> bool:
    try:
        with psycopg.connect(_psycopg_url(url), connect_timeout=2):
            return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL non impostata; esegui `cp .env.example .env` e poi `make up`.")
    if not _db_reachable(url):
        pytest.skip(
            "Postgres non raggiungibile su DATABASE_URL. "
            "Avvia con `make up` e attendi che il servizio sia healthy."
        )
    return _psycopg_url(url)


@pytest.fixture()
def db_conn(database_url: str):
    conn = psycopg.connect(database_url, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
