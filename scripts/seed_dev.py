#!/usr/bin/env python3
"""
Seed di sviluppo per Evidence-First MVP-0.

Esegue lo script SQL `seeds/0001_dev_seed.sql` in modo idempotente
(usa ON CONFLICT DO NOTHING / UPDATE dove applicabile).

Uso:
  python scripts/seed_dev.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import psycopg
except ImportError:
    print(
        "ERRORE: psycopg non installato.\nInstalla con: pip install 'psycopg[binary]>=3.1'",
        file=sys.stderr,
    )
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_FILE = REPO_ROOT / "seeds" / "0001_dev_seed.sql"


def _psycopg_url(url: str) -> str:
    """Convert SQLAlchemy-style psycopg URL into psycopg/libpq URL."""
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url.removeprefix("postgresql+psycopg://")
    return url


def main() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERRORE: DATABASE_URL non impostata.", file=sys.stderr)
        return 2

    if not SEED_FILE.exists():
        print(f"ERRORE: file seed non trovato: {SEED_FILE}", file=sys.stderr)
        return 2

    sql = SEED_FILE.read_text(encoding="utf-8")
    try:
        with psycopg.connect(_psycopg_url(db_url), autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
    except psycopg.errors.UndefinedTable as exc:
        print(
            f"ERRORE: una tabella richiesta dal seed non esiste: {exc}\n"
            "Hai eseguito `make migrate`?",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"ERRORE eseguendo il seed: {exc}", file=sys.stderr)
        return 1

    print("Seed di sviluppo applicato (idempotente).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
