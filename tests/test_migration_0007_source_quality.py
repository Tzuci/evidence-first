"""Constraint-level tests on Phase 8.7B source_quality schema (root, DB-only).

Coverage (20 cases as required by the prompt):
  1.  source_quality_assessments table exists.
  2.  Valid INSERT targeting evidence_span_id.
  3.  Valid INSERT targeting document_chunk_id.
  4.  Valid INSERT targeting document_id.
  5.  CHECK XOR rejects an INSERT with no target set.
  6.  CHECK XOR rejects an INSERT with more than one target set.
  7.  CHECK enum rejects an invalid source_type.
  8.  CHECK enum rejects an invalid overall_quality.
  9.  CHECK confidence range rejects values < 0 and > 1.
  10. version_no is unique per evidence_span_id (partial unique index).
  11. The same version_no is allowed across different targets.
  12. idempotency_key is unique per target (e.g. per evidence_span_id).
  13. The same idempotency_key is allowed across different targets.
  14. UPDATE on source_quality_assessments is rejected (append-only).
  15. DELETE on source_quality_assessments is rejected (append-only).
  16. JSONB payload roundtrips correctly.
  17. created_at is populated automatically.
  18. FK on evidence_span_id rejects references to non-existent rows.
  19. FK on document_chunk_id rejects references to non-existent rows.
  20. FK on document_id rejects references to non-existent rows.

Rerun-safety:
  All identifiers and hashes are uuid.uuid4()-derived per invocation, so this
  test file is safe to rerun against a long-lived dev DB.

This file is self-contained: it does NOT import helpers from other test files
(per Phase 8.7B prompt). Seed helpers are local.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import uuid
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# migration bootstrap
# ---------------------------------------------------------------------------
def _ensure_migrations(db_conn) -> None:
    """Apply all migrations idempotently before the test runs.

    Same pattern used by the other root tests
    (tests/test_lifecycle_constraints.py, tests/test_claim_ledger_constraints.py,
    etc.): load scripts/migrate.py as a module and call cmd_apply().
    """
    spec = importlib.util.spec_from_file_location(
        "migrate_module", REPO_ROOT / "scripts" / "migrate.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    rc = module.cmd_apply(db_conn, target=None, dry_run=False)
    assert rc == 0


# ---------------------------------------------------------------------------
# rerun-safe helpers
# ---------------------------------------------------------------------------
def _unique_hex() -> str:
    """Return a 64-hex string unique to this invocation."""
    return hashlib.sha256(uuid.uuid4().bytes + uuid.uuid4().bytes).hexdigest()


# ---------------------------------------------------------------------------
# seed helpers — fully local, no cross-test imports
# ---------------------------------------------------------------------------
def _seed_dev(cur) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ensure tenant + user; create a FRESH project and task per invocation.

    Returns (tenant_id, project_id, user_id, task_id).
    """
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
        "VALUES (%s,'dev@local','Dev','active') "
        "ON CONFLICT (tenant_id, email) DO NOTHING RETURNING id",
        (tenant_id,),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "SELECT id FROM users WHERE tenant_id = %s AND email = 'dev@local'",
            (tenant_id,),
        )
        row = cur.fetchone()
    user_id = uuid.UUID(str(row[0]))

    project_name = f"sqa-test-{uuid.uuid4()}"
    cur.execute(
        "INSERT INTO projects (tenant_id, name, mode_default) "
        "VALUES (%s, %s, 'closed_corpus') RETURNING id",
        (tenant_id, project_name),
    )
    project_id = uuid.UUID(str(cur.fetchone()[0]))

    cur.execute(
        """
        INSERT INTO task_masters
            (tenant_id, project_id, created_by, mode, objective, status)
        VALUES (%s, %s, %s, 'closed_corpus', %s, 'created')
        RETURNING id
        """,
        (tenant_id, project_id, user_id, f"obj-{uuid.uuid4()}"),
    )
    task_id = uuid.UUID(str(cur.fetchone()[0]))
    return tenant_id, project_id, user_id, task_id


def _create_evidence_span_chain(
    cur,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    created_by: uuid.UUID,
) -> dict:
    """Create storage_blobs -> storage_objects -> uploaded_documents
    -> document_versions -> document_chunks -> evidence_spans.

    Returns a dict with all the resulting ids:
      - blob_id, storage_object_id, document_id, document_version_id,
        document_chunk_id, evidence_span_id

    Honors:
      - storage_blobs.sb_global_uq (content_hash salted with uuid)
      - dc_origin_xor on document_chunks (document_version_id NOT NULL,
        source_version_id NULL)
      - evidence_spans char_start/char_end validity
    """
    blob_text = f"sqa-chunk-{uuid.uuid4()}"
    blob_size = len(blob_text.encode("utf-8"))
    content_hash = _unique_hex()

    blob_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO storage_blobs (
            id, tenant_namespace_id, content_hash, hash_algorithm,
            size_bytes, mime_type, storage_backend, local_path, refcount
        ) VALUES (
            %s, NULL, %s, 'sha256',
            %s, 'text/plain', 'local_fs', %s, 0
        )
        """,
        (blob_id, content_hash, blob_size, f"/tmp/{content_hash}"),
    )

    storage_object_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO storage_objects (
            id, tenant_id, project_id, blob_id,
            object_type, logical_owner_kind, logical_owner_id
        ) VALUES (
            %s, %s, %s, %s,
            'upload', 'uploaded_document', %s
        )
        """,
        (storage_object_id, tenant_id, project_id, blob_id, uuid.uuid4()),
    )

    document_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO uploaded_documents (
            id, tenant_id, project_id, storage_object_id,
            filename, content_hash, mime_type, size_bytes,
            tier, language, created_by
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, 'text/plain', %s,
            'user_provided', 'und', %s
        )
        """,
        (
            document_id,
            tenant_id,
            project_id,
            storage_object_id,
            f"doc-{uuid.uuid4()}.txt",
            content_hash,
            blob_size,
            created_by,
        ),
    )

    document_version_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO document_versions (
            id, document_id, version_no, version_kind,
            storage_object_id, inline_text, text_hash
        ) VALUES (
            %s, %s, 1, 'parsed',
            %s, %s, %s
        )
        """,
        (document_version_id, document_id, storage_object_id, blob_text, _unique_hex()),
    )

    document_chunk_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO document_chunks (
            id, document_version_id, chunk_index,
            char_start, char_end, inline_text, text_hash
        ) VALUES (
            %s, %s, 0,
            0, %s, %s, %s
        )
        """,
        (document_chunk_id, document_version_id, len(blob_text), blob_text, _unique_hex()),
    )

    evidence_span_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO evidence_spans (
            id, document_chunk_id, char_start, char_end, quote, quote_hash
        ) VALUES (
            %s, %s, 0, %s, %s, %s
        )
        """,
        (
            evidence_span_id,
            document_chunk_id,
            len(blob_text),
            blob_text,
            _unique_hex(),
        ),
    )

    return {
        "blob_id": blob_id,
        "storage_object_id": storage_object_id,
        "document_id": document_id,
        "document_version_id": document_version_id,
        "document_chunk_id": document_chunk_id,
        "evidence_span_id": evidence_span_id,
    }


# ---------------------------------------------------------------------------
# valid base parameters for an INSERT
# ---------------------------------------------------------------------------
def _valid_kwargs(
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    evidence_span_id: uuid.UUID | None = None,
    document_chunk_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    version_no: int = 1,
    source_type: str = "user_document",
    source_role: str = "secondary",
    authority_level: str = "unknown",
    independence_level: str = "unknown",
    freshness: str = "undated",
    relevance: str = "direct_support",
    extract_quality: str = "exact_quote_match",
    contradiction_status: str = "unchecked",
    overall_quality: str = "adequate",
    confidence: float | None = 0.7,
    evaluator_name: str = "mvp0_source_quality_v1",
    evaluator_version: str = "0.1.0",
    policy_name: str = "mvp0_default_policy",
    policy_version: str = "0.1.0",
    idempotency_key: str | None = None,
    payload: dict | None = None,
) -> dict:
    """Return a dict of valid base kwargs for an INSERT.

    Caller overrides only the fields that are under test. By default the
    target is left unset (None for all three target columns) — caller
    MUST set exactly one.
    """
    return {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "evidence_span_id": evidence_span_id,
        "document_chunk_id": document_chunk_id,
        "document_id": document_id,
        "version_no": version_no,
        "source_type": source_type,
        "source_role": source_role,
        "authority_level": authority_level,
        "independence_level": independence_level,
        "freshness": freshness,
        "relevance": relevance,
        "extract_quality": extract_quality,
        "contradiction_status": contradiction_status,
        "overall_quality": overall_quality,
        "confidence": confidence,
        "evaluator_name": evaluator_name,
        "evaluator_version": evaluator_version,
        "policy_name": policy_name,
        "policy_version": policy_version,
        "idempotency_key": idempotency_key or _unique_hex(),
        "payload": json.dumps(payload if payload is not None else {}),
    }


def _insert(cur, kwargs: dict) -> uuid.UUID:
    """Execute the INSERT and return the new row id.

    The ``id`` column is intentionally omitted so PostgreSQL applies the
    table-level DEFAULT app_new_uuid() defined by the migration. This
    way the test exercises the actual DEFAULT, not a test-side
    gen_random_uuid() override.
    """
    cur.execute(
        """
        INSERT INTO source_quality_assessments (
            tenant_id, project_id,
            evidence_span_id, document_chunk_id, document_id,
            version_no,
            source_type, source_role, authority_level, independence_level,
            freshness, relevance, extract_quality, contradiction_status,
            overall_quality, confidence,
            evaluator_name, evaluator_version,
            policy_name, policy_version,
            idempotency_key, payload
        ) VALUES (
            %(tenant_id)s, %(project_id)s,
            %(evidence_span_id)s, %(document_chunk_id)s, %(document_id)s,
            %(version_no)s,
            %(source_type)s, %(source_role)s, %(authority_level)s, %(independence_level)s,
            %(freshness)s, %(relevance)s, %(extract_quality)s, %(contradiction_status)s,
            %(overall_quality)s, %(confidence)s,
            %(evaluator_name)s, %(evaluator_version)s,
            %(policy_name)s, %(policy_version)s,
            %(idempotency_key)s, %(payload)s::jsonb
        )
        RETURNING id
        """,
        kwargs,
    )
    return uuid.UUID(str(cur.fetchone()[0]))


# ===========================================================================
# 1) Table exists
# ===========================================================================
def test_source_quality_assessments_table_exists(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = current_schema()
          AND table_name = 'source_quality_assessments'
        """
    )
    assert cur.fetchone() is not None


# ===========================================================================
# 2) Valid INSERT — target evidence_span_id
# ===========================================================================
def test_insert_valid_target_evidence_span(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        evidence_span_id=chain["evidence_span_id"],
    )
    new_id = _insert(cur, kwargs)
    db_conn.commit()
    assert new_id is not None


# ===========================================================================
# 3) Valid INSERT — target document_chunk_id
# ===========================================================================
def test_insert_valid_target_document_chunk(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        document_chunk_id=chain["document_chunk_id"],
    )
    new_id = _insert(cur, kwargs)
    db_conn.commit()
    assert new_id is not None


# ===========================================================================
# 4) Valid INSERT — target document_id
# ===========================================================================
def test_insert_valid_target_document(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        document_id=chain["document_id"],
    )
    new_id = _insert(cur, kwargs)
    db_conn.commit()
    assert new_id is not None


# ===========================================================================
# 5) CHECK XOR — zero targets rejected
# ===========================================================================
def test_xor_rejects_zero_targets(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, _user_id, _task_id = _seed_dev(cur)
    db_conn.commit()

    kwargs = _valid_kwargs(tenant_id=tenant_id, project_id=project_id)
    # all three target columns are NULL by default
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(cur, kwargs)
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# 6) CHECK XOR — more than one target rejected
# ===========================================================================
def test_xor_rejects_multiple_targets(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    # two targets set at the same time
    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        evidence_span_id=chain["evidence_span_id"],
        document_chunk_id=chain["document_chunk_id"],
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(cur, kwargs)
        db_conn.commit()
    db_conn.rollback()

    # three targets set at the same time
    kwargs3 = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        evidence_span_id=chain["evidence_span_id"],
        document_chunk_id=chain["document_chunk_id"],
        document_id=chain["document_id"],
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(cur, kwargs3)
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# 7) CHECK enum — invalid source_type rejected
# ===========================================================================
def test_check_source_type_rejects_invalid(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        evidence_span_id=chain["evidence_span_id"],
        source_type="made_up_type",
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(cur, kwargs)
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# 8) CHECK enum — invalid overall_quality rejected
# ===========================================================================
def test_check_overall_quality_rejects_invalid(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        evidence_span_id=chain["evidence_span_id"],
        overall_quality="excellent",  # not in codomain
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(cur, kwargs)
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# 9) CHECK confidence range — outside [0,1] rejected
# ===========================================================================
def test_check_confidence_out_of_range(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    # confidence < 0
    kwargs_neg = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        evidence_span_id=chain["evidence_span_id"],
        confidence=-0.1,
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(cur, kwargs_neg)
        db_conn.commit()
    db_conn.rollback()

    # confidence > 1
    kwargs_hi = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        evidence_span_id=chain["evidence_span_id"],
        confidence=1.5,
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(cur, kwargs_hi)
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# 10) version_no unique per evidence_span_id
# ===========================================================================
def test_version_no_unique_per_evidence_span(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    base = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        evidence_span_id=chain["evidence_span_id"],
        version_no=1,
    )
    _insert(cur, base)
    db_conn.commit()

    # Insert v2 (different version_no) is fine.
    base2 = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        evidence_span_id=chain["evidence_span_id"],
        version_no=2,
    )
    _insert(cur, base2)
    db_conn.commit()

    # Re-inserting v1 with a fresh idempotency_key must violate the
    # partial unique index sqa_evidence_version_uq.
    base_dup = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        evidence_span_id=chain["evidence_span_id"],
        version_no=1,
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert(cur, base_dup)
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# 11) version_no can repeat across different targets
# ===========================================================================
def test_version_no_can_repeat_across_targets(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    # v1 on evidence_span_id
    _insert(
        cur,
        _valid_kwargs(
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=1,
        ),
    )
    # v1 on document_chunk_id — different target, partial unique does not block
    _insert(
        cur,
        _valid_kwargs(
            tenant_id=tenant_id,
            project_id=project_id,
            document_chunk_id=chain["document_chunk_id"],
            version_no=1,
        ),
    )
    # v1 on document_id — same reasoning
    _insert(
        cur,
        _valid_kwargs(
            tenant_id=tenant_id,
            project_id=project_id,
            document_id=chain["document_id"],
            version_no=1,
        ),
    )
    db_conn.commit()

    cur.execute(
        """
        SELECT COUNT(*) FROM source_quality_assessments
        WHERE version_no = 1
          AND (
            evidence_span_id  = %s
         OR document_chunk_id = %s
         OR document_id       = %s
          )
        """,
        (chain["evidence_span_id"], chain["document_chunk_id"], chain["document_id"]),
    )
    assert int(cur.fetchone()[0]) == 3


# ===========================================================================
# 12) idempotency_key unique per same target
# ===========================================================================
def test_idempotency_key_unique_per_same_target(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    idem = f"shared-idem-{uuid.uuid4()}"

    # First INSERT with (span, idem, v1) — fine.
    _insert(
        cur,
        _valid_kwargs(
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=1,
            idempotency_key=idem,
        ),
    )
    db_conn.commit()

    # Second INSERT with same (span, idem) but different version_no.
    # version_no alone would not collide, but idempotency_key must.
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert(
            cur,
            _valid_kwargs(
                tenant_id=tenant_id,
                project_id=project_id,
                evidence_span_id=chain["evidence_span_id"],
                version_no=2,
                idempotency_key=idem,
            ),
        )
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# 13) idempotency_key can repeat across different targets
# ===========================================================================
def test_idempotency_key_can_repeat_across_targets(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    idem = f"cross-target-idem-{uuid.uuid4()}"

    # Same idempotency_key on three different targets — fine.
    _insert(
        cur,
        _valid_kwargs(
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=1,
            idempotency_key=idem,
        ),
    )
    _insert(
        cur,
        _valid_kwargs(
            tenant_id=tenant_id,
            project_id=project_id,
            document_chunk_id=chain["document_chunk_id"],
            version_no=1,
            idempotency_key=idem,
        ),
    )
    _insert(
        cur,
        _valid_kwargs(
            tenant_id=tenant_id,
            project_id=project_id,
            document_id=chain["document_id"],
            version_no=1,
            idempotency_key=idem,
        ),
    )
    db_conn.commit()

    cur.execute(
        "SELECT COUNT(*) FROM source_quality_assessments WHERE idempotency_key = %s",
        (idem,),
    )
    assert int(cur.fetchone()[0]) == 3


# ===========================================================================
# 14) UPDATE rejected (append-only)
# ===========================================================================
def test_append_only_rejects_update(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    new_id = _insert(
        cur,
        _valid_kwargs(
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
        ),
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "UPDATE source_quality_assessments SET overall_quality = 'weak' WHERE id = %s",
            (new_id,),
        )
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# 15) DELETE rejected (append-only)
# ===========================================================================
def test_append_only_rejects_delete(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    new_id = _insert(
        cur,
        _valid_kwargs(
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
        ),
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "DELETE FROM source_quality_assessments WHERE id = %s", (new_id,)
        )
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# 16) JSONB payload roundtrips correctly
# ===========================================================================
def test_jsonb_payload_roundtrip(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    expected_payload = {
        "scenario": "phase_8_7_b_payload_roundtrip",
        "scores": {"a": 1, "b": 0.5},
        "tags": ["primary", "stale"],
        "nested": {"deep": {"value": True}},
    }

    new_id = _insert(
        cur,
        _valid_kwargs(
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            payload=expected_payload,
        ),
    )
    db_conn.commit()

    cur.execute(
        "SELECT payload FROM source_quality_assessments WHERE id = %s", (new_id,)
    )
    raw = cur.fetchone()[0]
    # psycopg3 typically returns JSONB as native Python objects, but some
    # configurations surface a JSON string. Accept both.
    actual = json.loads(raw) if isinstance(raw, str) else raw
    assert actual == expected_payload


# ===========================================================================
# 17) created_at populated automatically
# ===========================================================================
def test_created_at_populated_automatically(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    new_id = _insert(
        cur,
        _valid_kwargs(
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
        ),
    )
    db_conn.commit()

    cur.execute(
        "SELECT created_at FROM source_quality_assessments WHERE id = %s", (new_id,)
    )
    created_at = cur.fetchone()[0]
    assert created_at is not None


# ===========================================================================
# 18) FK evidence_span_id rejects bogus references
# ===========================================================================
def test_fk_evidence_span_id_rejects_bogus(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, _user_id, _task_id = _seed_dev(cur)
    db_conn.commit()

    bogus_span = uuid.uuid4()
    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        evidence_span_id=bogus_span,
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _insert(cur, kwargs)
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# 19) FK document_chunk_id rejects bogus references
# ===========================================================================
def test_fk_document_chunk_id_rejects_bogus(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, _user_id, _task_id = _seed_dev(cur)
    db_conn.commit()

    bogus_chunk = uuid.uuid4()
    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        document_chunk_id=bogus_chunk,
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _insert(cur, kwargs)
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# 20) FK document_id rejects bogus references
# ===========================================================================
def test_fk_document_id_rejects_bogus(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, _user_id, _task_id = _seed_dev(cur)
    db_conn.commit()

    bogus_doc = uuid.uuid4()
    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        document_id=bogus_doc,
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _insert(cur, kwargs)
        db_conn.commit()
    db_conn.rollback()
