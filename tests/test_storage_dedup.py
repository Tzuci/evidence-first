"""Storage layer tests (root, DB-only): dedup, refcount, blob delete protection,
evidence_spans append-only.

Rerun-safety (8.2a-patch):
  Tests no longer rely on hardcoded fake hashes like 'a'*64. Each test invocation
  uses unique content_hash values derived from uuid.uuid4() so the suite is
  idempotent on a long-lived dev database. The dedup test verifies dedup by
  inserting a SECOND row with the SAME unique hash and asserting UniqueViolation,
  not by relying on hashes from previous runs.
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import psycopg
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parent.parent


def _ensure_migrations(db_conn):
    spec = importlib.util.spec_from_file_location(
        "migrate_module", REPO_ROOT / "scripts" / "migrate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rc = module.cmd_apply(db_conn, target=None, dry_run=False)
    assert rc == 0


def _seed_dev(cur) -> tuple[uuid.UUID, uuid.UUID]:
    cur.execute(
        "INSERT INTO tenants (name, slug, status) VALUES ('Dev','dev','active') "
        "ON CONFLICT (slug) DO NOTHING RETURNING id"
    )
    row = cur.fetchone()
    if row is None:
        cur.execute("SELECT id FROM tenants WHERE slug = 'dev'")
        row = cur.fetchone()
    tenant_id = uuid.UUID(str(row[0]))
    cur.execute(
        "INSERT INTO users (tenant_id, email, display_name, status) "
        "VALUES (%s,'dev@local','Dev','active') ON CONFLICT (tenant_id, email) DO NOTHING RETURNING id",
        (tenant_id,),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "SELECT id FROM users WHERE tenant_id = %s AND email = 'dev@local'",
            (tenant_id,),
        )
        row = cur.fetchone()

    # Use a unique project name per test session to avoid collisions on reruns.
    project_name = f"storage-test-project-{uuid.uuid4()}"
    cur.execute(
        "INSERT INTO projects (tenant_id, name, mode_default) "
        "VALUES (%s, %s, 'closed_corpus') "
        "ON CONFLICT (tenant_id, name) DO NOTHING RETURNING id",
        (tenant_id, project_name),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "SELECT id FROM projects WHERE tenant_id = %s AND name = %s",
            (tenant_id, project_name),
        )
        row = cur.fetchone()
    project_id = uuid.UUID(str(row[0]))
    return tenant_id, project_id


def _unique_hash() -> str:
    """Return a 64-hex content_hash unique to this invocation."""
    return uuid.uuid4().hex + uuid.uuid4().hex  # 64 hex chars


def _insert_blob(cur, *, content_hash: str, size: int, local_path: str) -> uuid.UUID:
    bid = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO storage_blobs (id, tenant_namespace_id, content_hash, hash_algorithm,
                                   size_bytes, mime_type, storage_backend, local_path, refcount)
        VALUES (%s, NULL, %s, 'sha256', %s, 'text/plain', 'local_fs', %s, 0)
        """,
        (bid, content_hash, size, local_path),
    )
    return bid


def _insert_object(cur, *, tenant_id, project_id, blob_id, owner_id) -> uuid.UUID:
    oid = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO storage_objects (id, tenant_id, project_id, blob_id,
                                     object_type, logical_owner_kind, logical_owner_id)
        VALUES (%s, %s, %s, %s, 'upload', 'uploaded_document', %s)
        """,
        (oid, tenant_id, project_id, blob_id, owner_id),
    )
    return oid


def _refcount(cur, blob_id: uuid.UUID) -> int:
    cur.execute("SELECT refcount FROM storage_blobs WHERE id = %s", (blob_id,))
    return int(cur.fetchone()[0])


def test_storage_blobs_dedup_global(db_conn):
    """Inserting two rows with the same (content_hash, sha256, NULL namespace) must
    fail on the partial UNIQUE index sb_global_uq.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id = _seed_dev(cur)
    db_conn.commit()

    h = _unique_hash()
    bid1 = _insert_blob(cur, content_hash=h, size=10, local_path=f"/tmp/{h}")
    db_conn.commit()
    assert bid1 is not None

    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_blob(cur, content_hash=h, size=10, local_path=f"/tmp/{h}")
        db_conn.commit()
    db_conn.rollback()


def test_storage_objects_refcount(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id = _seed_dev(cur)
    db_conn.commit()

    h = _unique_hash()
    bid = _insert_blob(cur, content_hash=h, size=10, local_path=f"/tmp/{h}")
    db_conn.commit()
    assert _refcount(cur, bid) == 0

    oid1 = _insert_object(
        cur, tenant_id=tenant_id, project_id=project_id, blob_id=bid, owner_id=uuid.uuid4()
    )
    db_conn.commit()
    assert _refcount(cur, bid) == 1

    oid2 = _insert_object(
        cur, tenant_id=tenant_id, project_id=project_id, blob_id=bid, owner_id=uuid.uuid4()
    )
    db_conn.commit()
    assert _refcount(cur, bid) == 2

    cur.execute("DELETE FROM storage_objects WHERE id = %s", (oid1,))
    db_conn.commit()
    assert _refcount(cur, bid) == 1
    assert oid2 is not None


def test_delete_blob_with_refs_rejected(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id = _seed_dev(cur)
    db_conn.commit()

    h = _unique_hash()
    bid = _insert_blob(cur, content_hash=h, size=10, local_path=f"/tmp/{h}")
    _insert_object(
        cur, tenant_id=tenant_id, project_id=project_id, blob_id=bid, owner_id=uuid.uuid4()
    )
    db_conn.commit()
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute("DELETE FROM storage_blobs WHERE id = %s", (bid,))
        db_conn.commit()
    db_conn.rollback()


def test_evidence_spans_append_only(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id = _seed_dev(cur)
    db_conn.commit()

    h = _unique_hash()
    bid = _insert_blob(cur, content_hash=h, size=10, local_path=f"/tmp/{h}")
    oid = _insert_object(
        cur, tenant_id=tenant_id, project_id=project_id, blob_id=bid, owner_id=uuid.uuid4()
    )
    db_conn.commit()

    cur.execute(
        """
        INSERT INTO uploaded_documents (id, tenant_id, project_id, storage_object_id,
                                        filename, content_hash, mime_type, size_bytes, tier)
        VALUES (gen_random_uuid(), %s, %s, %s, 'x.txt', %s, 'text/plain', 10, 'user_provided')
        RETURNING id
        """,
        (tenant_id, project_id, oid, h),
    )
    doc_id = uuid.UUID(str(cur.fetchone()[0]))

    text_hash = _unique_hash()
    cur.execute(
        """
        INSERT INTO document_versions (id, document_id, version_no, version_kind,
                                       storage_object_id, inline_text, text_hash)
        VALUES (gen_random_uuid(), %s, 1, 'parsed', %s, 'hello', %s) RETURNING id
        """,
        (doc_id, oid, text_hash),
    )
    dv_id = uuid.UUID(str(cur.fetchone()[0]))

    chunk_hash = _unique_hash()
    cur.execute(
        """
        INSERT INTO document_chunks (id, document_version_id, chunk_index,
                                     char_start, char_end, inline_text, text_hash)
        VALUES (gen_random_uuid(), %s, 0, 0, 5, 'hello', %s) RETURNING id
        """,
        (dv_id, chunk_hash),
    )
    chunk_id = uuid.UUID(str(cur.fetchone()[0]))

    quote_hash = _unique_hash()
    cur.execute(
        """
        INSERT INTO evidence_spans (id, document_chunk_id, char_start, char_end, quote, quote_hash)
        VALUES (gen_random_uuid(), %s, 0, 5, 'hello', %s) RETURNING id
        """,
        (chunk_id, quote_hash),
    )
    span_id = uuid.UUID(str(cur.fetchone()[0]))
    db_conn.commit()

    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute("UPDATE evidence_spans SET quote = 'x' WHERE id = %s", (span_id,))
        db_conn.commit()
    db_conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute("DELETE FROM evidence_spans WHERE id = %s", (span_id,))
        db_conn.commit()
    db_conn.rollback()