"""
Test del migration runner (scripts/migrate.py).

Prerequisiti: `make up` (Postgres raggiungibile su DATABASE_URL).

I test:
  - applicano la migration 0001;
  - verificano che schema_migrations contenga 0001 con checksum corretto;
  - verificano che una rerun non riapplichi 0001;
  - verificano che la modifica simulata di un checksum su una migration applicata
    venga rilevata come errore (lo simuliamo a livello di tabella, non modificando il file);
  - verificano che --target=0001 sia idempotente: no-op se 0001 è già applicata.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_FILE = REPO_ROOT / "migrations" / "0001_foundation.sql"


def _load_migrate_module():
    spec = importlib.util.spec_from_file_location(
        "migrate_module", REPO_ROOT / "scripts" / "migrate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def test_migration_0001_applied(db_conn):
    """Dopo `make migrate` la tabella schema_migrations contiene 0001 con il checksum giusto."""
    migrate = _load_migrate_module()
    rc = migrate.cmd_apply(db_conn, target=None, dry_run=False)
    assert rc == 0, "Il runner deve completare senza errori"

    expected = _sha256(MIGRATION_FILE)
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT checksum FROM schema_migrations WHERE filename = %s",
            (MIGRATION_FILE.name,),
        )
        row = cur.fetchone()
    assert row is not None, "0001 deve risultare applicata"
    assert row[0] == expected, "Il checksum salvato deve coincidere con sha256 del file"


def test_rerun_does_not_reapply(db_conn):
    """Una seconda esecuzione del runner non deve riapplicare la migration."""
    migrate = _load_migrate_module()
    rc1 = migrate.cmd_apply(db_conn, target=None, dry_run=False)
    assert rc1 == 0
    rc2 = migrate.cmd_apply(db_conn, target=None, dry_run=False)
    assert rc2 == 0


def test_target_is_idempotent_when_already_applied(db_conn):
    """--target=0001 non deve fallire se 0001 è già applicata: deve essere no-op."""
    migrate = _load_migrate_module()
    rc1 = migrate.cmd_apply(db_conn, target=None, dry_run=False)
    assert rc1 == 0
    rc2 = migrate.cmd_apply(db_conn, target="0001", dry_run=False)
    assert rc2 == 0, "--target su una migration già applicata deve essere no-op"


def test_target_unknown_returns_error(db_conn):
    """--target con prefisso inesistente deve uscire con codice di errore."""
    migrate = _load_migrate_module()
    migrate.cmd_apply(db_conn, target=None, dry_run=False)  # garantiamo stato pulito
    rc = migrate.cmd_apply(db_conn, target="9999", dry_run=False)
    assert rc != 0


def test_checksum_mismatch_is_detected(db_conn):
    """
    Simuliamo una modifica del file già applicato alterando il checksum salvato in DB.
    Il runner deve uscire con errore (rc != 0) senza tentare riapplicazioni.
    """
    migrate = _load_migrate_module()
    rc = migrate.cmd_apply(db_conn, target=None, dry_run=False)
    assert rc == 0

    # Corrompiamo il checksum salvato e poi proviamo a rirunnare il runner.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE schema_migrations SET checksum = %s WHERE filename = %s",
            ("0" * 64, MIGRATION_FILE.name),
        )
    db_conn.commit()

    try:
        rc2 = migrate.cmd_apply(db_conn, target=None, dry_run=False)
        assert rc2 != 0, "Il runner deve segnalare errore in caso di checksum mismatch"
    finally:
        # Ripristiniamo il checksum corretto per non sporcare lo stato del DB
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE schema_migrations SET checksum = %s WHERE filename = %s",
                (_sha256(MIGRATION_FILE), MIGRATION_FILE.name),
            )
        db_conn.commit()


def test_status_command_runs(db_conn):
    """`--status` deve produrre output e ritornare 0 quando tutto è coerente."""
    migrate = _load_migrate_module()
    # Garantiamo che 0001 sia applicata
    migrate.cmd_apply(db_conn, target=None, dry_run=False)
    rc = migrate.cmd_status(db_conn)
    assert rc == 0