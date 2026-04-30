#!/usr/bin/env python3
"""
Migration runner per Evidence-First MVP-0.

Comportamento:
- Crea la tabella schema_migrations se non esiste.
- Legge i file migrations/*.sql in ordine numerico (lessicografico stabile).
- Per ogni file:
  - Calcola sha-256 del contenuto.
  - Se non è in schema_migrations, lo applica in transazione.
  - Se è già applicato:
    - Se il checksum corrisponde, salta.
    - Se il checksum è cambiato, esce con errore.
- Supporta --status, --dry-run, --target.

Uso:
  python scripts/migrate.py
  python scripts/migrate.py --status
  python scripts/migrate.py --dry-run
  python scripts/migrate.py --target=0001

Variabili d'ambiente:
  DATABASE_URL  (es. postgresql://user:pass@host:5432/db)

Dipendenze:
  - psycopg (v3)

Note:
  - Le migration applicate sono immutabili: modificare un file 0001 già applicato
    comporta un errore di checksum. Per cambi schema, creare una nuova migration.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

try:
    import psycopg
except ImportError:
    print(
        "ERRORE: psycopg non installato.\n"
        "Installa con: pip install 'psycopg[binary]>=3.1'",
        file=sys.stderr,
    )
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"
MIGRATION_FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def get_db_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print(
            "ERRORE: DATABASE_URL non impostata. Esegui `cp .env.example .env` "
            "e adatta i valori, oppure esporta DATABASE_URL nell'ambiente.",
            file=sys.stderr,
        )
        sys.exit(2)
    return url


def _psycopg_url(url: str) -> str:
    """Accept both SQLAlchemy-style and psycopg/libpq-style PostgreSQL URLs.

    SQLAlchemy uses:
      postgresql+psycopg://user:pass@host:port/db

    psycopg.connect() expects:
      postgresql://user:pass@host:port/db
    """
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url.removeprefix("postgresql+psycopg://")
    return url


def discover_migrations() -> list[Path]:
    if not MIGRATIONS_DIR.exists():
        print(f"ERRORE: cartella migrations non trovata: {MIGRATIONS_DIR}", file=sys.stderr)
        sys.exit(2)
    files: list[Path] = []
    for p in sorted(MIGRATIONS_DIR.iterdir()):
        if not p.is_file():
            continue
        if p.name.startswith("."):
            continue
        if not MIGRATION_FILENAME_RE.match(p.name):
            print(
                f"ATTENZIONE: file in migrations/ con nome non conforme ignorato: {p.name}",
                file=sys.stderr,
            )
            continue
        files.append(p)
    return files


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_schema_migrations(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   TEXT PRIMARY KEY,
                checksum   TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    conn.commit()


def fetch_applied(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename, checksum FROM schema_migrations")
        rows = cur.fetchall()
    return {filename: checksum for filename, checksum in rows}


def file_prefix(path: Path) -> str:
    m = MIGRATION_FILENAME_RE.match(path.name)
    assert m is not None
    return m.group(1)


def cmd_status(conn: psycopg.Connection) -> int:
    ensure_schema_migrations(conn)
    applied = fetch_applied(conn)
    files = discover_migrations()
    print("File                                   Stato        Checksum")
    print("-" * 80)
    rc = 0
    for f in files:
        on_disk = sha256_of_file(f)
        if f.name in applied:
            status = "applied" if applied[f.name] == on_disk else "CHECKSUM-MISMATCH"
            if status != "applied":
                rc = 1
        else:
            status = "pending"
        print(f"{f.name:<40} {status:<12} {on_disk[:12]}")
    only_in_db = [name for name in applied.keys() if not any(f.name == name for f in files)]
    for name in only_in_db:
        print(f"{name:<40} {'orphan-in-db':<12} {applied[name][:12]}")
        rc = 1
    return rc


def apply_migration(conn: psycopg.Connection, path: Path, checksum: str, dry_run: bool) -> None:
    sql = path.read_text(encoding="utf-8")
    print(f"Applico {path.name} (checksum {checksum[:12]})...", end=" ", flush=True)
    if dry_run:
        print("DRY-RUN, niente eseguito.")
        return
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(
            "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
            (path.name, checksum),
        )
    conn.commit()
    print("OK")


def cmd_apply(conn: psycopg.Connection, target: str | None, dry_run: bool) -> int:
    ensure_schema_migrations(conn)
    applied = fetch_applied(conn)
    files = discover_migrations()
    if not files:
        print("Nessuna migration trovata in migrations/.")
        return 0

    pending: list[tuple[Path, str]] = []
    for f in files:
        on_disk = sha256_of_file(f)
        if f.name in applied:
            if applied[f.name] != on_disk:
                print(
                    f"ERRORE: checksum mismatch per migration già applicata: {f.name}\n"
                    f"  applicato:  {applied[f.name]}\n"
                    f"  on-disk:    {on_disk}\n"
                    "Le migration sono immutabili una volta applicate.",
                    file=sys.stderr,
                )
                return 1
            continue
        pending.append((f, on_disk))
        if target is not None and file_prefix(f) == target:
            break
    else:
        if target is not None and not any(file_prefix(f) == target for f, _ in pending):
            applied_prefixes = {file_prefix(Path(name)) for name in applied.keys()}
            if target not in applied_prefixes:
                print(
                    f"ERRORE: target {target} non trovato tra le migration disponibili.",
                    file=sys.stderr,
                )
                return 2

    if not pending:
        print("Nessuna migration pendente.")
        return 0

    for path, checksum in pending:
        try:
            apply_migration(conn, path, checksum, dry_run=dry_run)
        except Exception as exc:
            conn.rollback()
            print(f"ERRORE applicando {path.name}: {exc}", file=sys.stderr)
            return 1

    print(f"Completato. {len(pending)} migration {'simulata' if dry_run else 'applicata'}{'e' if len(pending) != 1 else ''}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration runner Evidence-First MVP-0")
    parser.add_argument("--status", action="store_true", help="Mostra lo stato delle migration")
    parser.add_argument("--dry-run", action="store_true", help="Simula senza applicare")
    parser.add_argument("--target", default=None, help="Applica fino al prefisso indicato (es. 0001)")
    args = parser.parse_args()

    db_url = get_db_url()
    try:
        with psycopg.connect(_psycopg_url(db_url), autocommit=False) as conn:
            if args.status:
                return cmd_status(conn)
            return cmd_apply(conn, target=args.target, dry_run=args.dry_run)
    except psycopg.OperationalError as exc:
        print(f"ERRORE di connessione al DB: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
 
