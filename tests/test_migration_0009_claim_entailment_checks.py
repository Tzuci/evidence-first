"""Constraint-level tests on Phase 8.8A-SCHEMA claim_entailment_checks schema
(root, DB-only).

Coverage (per Block 8.8A-SCHEMA requirements):
  A. Valid insert with verdict='entailed' is readable and payload default
     works.
  B. Verdict invalid is rejected by CHECK cec_verdict_chk.
  C. Confidence range:
       - confidence=-0.1   -> rejected
       - confidence= 1.1   -> rejected
       - confidence=NULL   -> accepted
       - confidence=0.0    -> accepted
       - confidence=1.0    -> accepted
  D. version_no:
       - version_no=0   -> rejected
       - version_no=1   -> accepted
  E. UNIQUE (claim_ledger_entry_id, evidence_span_id, version_no):
       duplicate of (entry, span, v1) -> rejected.
  F. UNIQUE (claim_ledger_entry_id, evidence_span_id, idempotency_key):
       same idempotency_key on the same (entry, span) -> rejected.
  G. FK composite cec_entry_logical_consistency:
       (claim_ledger_entry_id of claim A, claim_logical_id of claim B)
       -> rejected with ForeignKeyViolation.
  H. Append-only:
       - UPDATE on claim_entailment_checks -> rejected.
       - DELETE on claim_entailment_checks -> rejected.
  I. Index / trigger / constraint smoke via information_schema and
     pg_catalog:
       - table exists;
       - CHECK constraint cec_verdict_chk exists;
       - trigger claim_entailment_checks_append_only exists with the
         correct firing condition (BEFORE UPDATE OR DELETE);
       - the two UNIQUE indexes cec_entry_span_version_uq and
         cec_entry_span_idem_uq exist.

Rerun-safety:
  All identifiers and hashes are uuid.uuid4()-derived per invocation, so
  this test file is safe to rerun against a long-lived dev DB.

This file is self-contained: it does NOT import helpers from other test
files (per Phase 8.8A-SCHEMA prompt). Seed helpers are local. The
migration runner is invoked via importlib.util (same pattern used by
tests/test_migration_0007_source_quality.py and
tests/test_migration_0008_coverage_gap_source_quality.py).
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
    (tests/test_migration_0007_source_quality.py,
    tests/test_migration_0008_coverage_gap_source_quality.py, etc.):
    load scripts/migrate.py as a module and call cmd_apply().
    """
    spec = importlib.util.spec_from_file_location(
        "migrate_module_0009", REPO_ROOT / "scripts" / "migrate.py"
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


# Whitelist of tables we are allowed to introspect via _count_table. The
# helper interpolates the table name into the SQL text because psycopg's
# placeholder syntax cannot parameterize identifiers; the whitelist makes
# the interpolation typo-safe and injection-safe. Same pattern as
# tests/test_migration_0008_coverage_gap_source_quality.py.
_ALLOWED_TABLES_FOR_INTROSPECTION: frozenset[str] = frozenset({
    "claim_entailment_checks",
})


def _count_table(cur, *, table: str) -> int:
    """Return COUNT(*) of ``table``. Validated against an explicit whitelist."""
    if table not in _ALLOWED_TABLES_FOR_INTROSPECTION:
        raise ValueError(
            f"_count_table: table {table!r} is not in the allowed "
            f"whitelist {_ALLOWED_TABLES_FOR_INTROSPECTION!r}"
        )
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return int(cur.fetchone()[0])


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

    project_name = f"cec-test-{uuid.uuid4()}"
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

    Returns a dict with the resulting ids:
      - document_id, document_version_id, document_chunk_id,
        evidence_span_id

    Honors:
      - storage_blobs.sb_global_uq (content_hash salted with uuid)
      - dc_origin_xor on document_chunks (document_version_id NOT NULL,
        source_version_id NULL)
      - evidence_spans char_start/char_end validity
    """
    blob_text = f"cec-chunk-{uuid.uuid4()}"
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
        "document_id": document_id,
        "document_version_id": document_version_id,
        "document_chunk_id": document_chunk_id,
        "evidence_span_id": evidence_span_id,
    }


def _create_logical_claim_with_v1(
    cur,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    state: str = "verified_fact",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert one logical_claims row + one v1 claim_ledger_entries row.

    Returns (claim_logical_id, claim_ledger_entry_id_v1).
    """
    claim_logical_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO logical_claims
            (id, tenant_id, project_id, task_id,
             canonical_claim_text, canonical_claim_hash)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            claim_logical_id,
            tenant_id,
            project_id,
            task_id,
            f"canonical-{uuid.uuid4()}",
            _unique_hex(),
        ),
    )

    claim_ledger_entry_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO claim_ledger_entries
            (id, claim_logical_id, version_no, state,
             support_scope, user_provided_dependency,
             transition_reason)
        VALUES (%s, %s, 1, %s,
                'supported_by_user_corpus_only',
                'supported_by_user_corpus_only',
                %s)
        """,
        (
            claim_ledger_entry_id,
            claim_logical_id,
            state,
            f"reason-{uuid.uuid4()}",
        ),
    )
    return claim_logical_id, claim_ledger_entry_id


# ---------------------------------------------------------------------------
# valid base parameters for an INSERT
# ---------------------------------------------------------------------------
def _valid_kwargs(
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID,
    claim_logical_id: uuid.UUID,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
    version_no: int = 1,
    verdict: str = "entailed",
    confidence: float | None = 0.7,
    checker_name: str = "mvp0_mock_entailment_checker",
    checker_version: str = "0.1.0",
    policy_name: str = "mvp0_mock_entailment",
    policy_version: str = "0.1.0",
    idempotency_key: str | None = None,
    rationale: str | None = "test rationale",
    payload: dict | None = None,
    include_payload: bool = True,
) -> dict:
    """Return a dict of valid base kwargs for an INSERT.

    Caller overrides only the fields under test. ``include_payload``
    toggles whether ``payload`` is sent at all (so the DEFAULT '{}'::jsonb
    on the table can be exercised by setting include_payload=False).
    """
    kw: dict = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "task_id": task_id,
        "claim_logical_id": claim_logical_id,
        "claim_ledger_entry_id": claim_ledger_entry_id,
        "evidence_span_id": evidence_span_id,
        "version_no": version_no,
        "verdict": verdict,
        "confidence": confidence,
        "checker_name": checker_name,
        "checker_version": checker_version,
        "policy_name": policy_name,
        "policy_version": policy_version,
        "idempotency_key": idempotency_key or _unique_hex(),
        "rationale": rationale,
    }
    if include_payload:
        kw["payload"] = json.dumps(payload if payload is not None else {})
    return kw


def _insert(cur, kwargs: dict) -> uuid.UUID:
    """Execute the INSERT and return the new row id.

    The ``id`` column is intentionally omitted so PostgreSQL applies the
    table-level DEFAULT app_new_uuid() defined by the migration. The
    ``payload`` column is omitted from the INSERT statement when it is
    not present in kwargs, so the DEFAULT '{}'::jsonb is applied.
    """
    if "payload" in kwargs:
        cur.execute(
            """
            INSERT INTO claim_entailment_checks (
                tenant_id, project_id, task_id,
                claim_logical_id, claim_ledger_entry_id, evidence_span_id,
                version_no, verdict, confidence,
                checker_name, checker_version,
                policy_name, policy_version,
                idempotency_key, rationale, payload
            ) VALUES (
                %(tenant_id)s, %(project_id)s, %(task_id)s,
                %(claim_logical_id)s, %(claim_ledger_entry_id)s, %(evidence_span_id)s,
                %(version_no)s, %(verdict)s, %(confidence)s,
                %(checker_name)s, %(checker_version)s,
                %(policy_name)s, %(policy_version)s,
                %(idempotency_key)s, %(rationale)s, %(payload)s::jsonb
            )
            RETURNING id
            """,
            kwargs,
        )
    else:
        cur.execute(
            """
            INSERT INTO claim_entailment_checks (
                tenant_id, project_id, task_id,
                claim_logical_id, claim_ledger_entry_id, evidence_span_id,
                version_no, verdict, confidence,
                checker_name, checker_version,
                policy_name, policy_version,
                idempotency_key, rationale
            ) VALUES (
                %(tenant_id)s, %(project_id)s, %(task_id)s,
                %(claim_logical_id)s, %(claim_ledger_entry_id)s, %(evidence_span_id)s,
                %(version_no)s, %(verdict)s, %(confidence)s,
                %(checker_name)s, %(checker_version)s,
                %(policy_name)s, %(policy_version)s,
                %(idempotency_key)s, %(rationale)s
            )
            RETURNING id
            """,
            kwargs,
        )
    return uuid.UUID(str(cur.fetchone()[0]))


# ===========================================================================
# A) Valid INSERT with verdict='entailed' is readable; payload default works.
# ===========================================================================
def test_insert_valid_entailed_row_and_payload_default(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    lc_id, le_id = _create_logical_claim_with_v1(
        cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    db_conn.commit()

    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        claim_logical_id=lc_id,
        claim_ledger_entry_id=le_id,
        evidence_span_id=chain["evidence_span_id"],
        verdict="entailed",
        # Exercise the DEFAULT '{}'::jsonb on payload by NOT sending it.
        include_payload=False,
    )
    new_id = _insert(cur, kwargs)
    db_conn.commit()

    cur.execute(
        """
        SELECT verdict, version_no, confidence, payload, rationale,
               checker_name, checker_version, policy_name, policy_version,
               tenant_id, project_id, task_id,
               claim_logical_id, claim_ledger_entry_id, evidence_span_id
        FROM claim_entailment_checks
        WHERE id = %s
        """,
        (new_id,),
    )
    row = cur.fetchone()
    assert row is not None
    (
        verdict,
        version_no,
        confidence,
        payload,
        rationale,
        checker_name,
        checker_version,
        policy_name,
        policy_version,
        row_tenant_id,
        row_project_id,
        row_task_id,
        row_lc_id,
        row_le_id,
        row_es_id,
    ) = row
    assert str(verdict) == "entailed"
    assert int(version_no) == 1
    assert float(confidence) == pytest.approx(0.7)
    # payload DEFAULT '{}'::jsonb — psycopg may surface JSONB as native
    # dict or as a JSON string depending on driver config; accept both.
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload == {}
    assert str(rationale) == "test rationale"
    assert str(checker_name) == "mvp0_mock_entailment_checker"
    assert str(checker_version) == "0.1.0"
    assert str(policy_name) == "mvp0_mock_entailment"
    assert str(policy_version) == "0.1.0"
    assert uuid.UUID(str(row_tenant_id)) == tenant_id
    assert uuid.UUID(str(row_project_id)) == project_id
    assert uuid.UUID(str(row_task_id)) == task_id
    assert uuid.UUID(str(row_lc_id)) == lc_id
    assert uuid.UUID(str(row_le_id)) == le_id
    assert uuid.UUID(str(row_es_id)) == chain["evidence_span_id"]


# ===========================================================================
# B) Verdict invalid is rejected.
# ===========================================================================
def test_verdict_invalid_is_rejected(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    lc_id, le_id = _create_logical_claim_with_v1(
        cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    db_conn.commit()

    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        claim_logical_id=lc_id,
        claim_ledger_entry_id=le_id,
        evidence_span_id=chain["evidence_span_id"],
        verdict="bogus",
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(cur, kwargs)
        db_conn.commit()
    db_conn.rollback()


@pytest.mark.parametrize(
    "valid_verdict",
    ["entailed", "partially_supported", "not_supported", "contradicted", "uncertain"],
)
def test_each_valid_verdict_value_is_accepted(db_conn, valid_verdict):
    """Defensive coverage: every value listed in cec_verdict_chk must be
    accepted exactly as declared by 0009.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    lc_id, le_id = _create_logical_claim_with_v1(
        cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    db_conn.commit()

    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        claim_logical_id=lc_id,
        claim_ledger_entry_id=le_id,
        evidence_span_id=chain["evidence_span_id"],
        verdict=valid_verdict,
    )
    new_id = _insert(cur, kwargs)
    db_conn.commit()
    assert new_id is not None


# ===========================================================================
# C) Confidence range.
# ===========================================================================
def test_confidence_negative_is_rejected(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    lc_id, le_id = _create_logical_claim_with_v1(
        cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    db_conn.commit()

    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        claim_logical_id=lc_id,
        claim_ledger_entry_id=le_id,
        evidence_span_id=chain["evidence_span_id"],
        confidence=-0.1,
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(cur, kwargs)
        db_conn.commit()
    db_conn.rollback()


def test_confidence_above_one_is_rejected(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    lc_id, le_id = _create_logical_claim_with_v1(
        cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    db_conn.commit()

    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        claim_logical_id=lc_id,
        claim_ledger_entry_id=le_id,
        evidence_span_id=chain["evidence_span_id"],
        confidence=1.1,
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(cur, kwargs)
        db_conn.commit()
    db_conn.rollback()


@pytest.mark.parametrize("confidence_value", [None, 0.0, 1.0])
def test_confidence_boundary_values_are_accepted(db_conn, confidence_value):
    """NULL / 0.0 / 1.0 must all pass the CHECK cec_confidence_range."""
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    lc_id, le_id = _create_logical_claim_with_v1(
        cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    db_conn.commit()

    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        claim_logical_id=lc_id,
        claim_ledger_entry_id=le_id,
        evidence_span_id=chain["evidence_span_id"],
        confidence=confidence_value,
    )
    new_id = _insert(cur, kwargs)
    db_conn.commit()

    cur.execute(
        "SELECT confidence FROM claim_entailment_checks WHERE id = %s",
        (new_id,),
    )
    db_value = cur.fetchone()[0]
    if confidence_value is None:
        assert db_value is None
    else:
        assert float(db_value) == pytest.approx(confidence_value)


# ===========================================================================
# D) version_no.
# ===========================================================================
def test_version_no_zero_is_rejected(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    lc_id, le_id = _create_logical_claim_with_v1(
        cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    db_conn.commit()

    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        claim_logical_id=lc_id,
        claim_ledger_entry_id=le_id,
        evidence_span_id=chain["evidence_span_id"],
        version_no=0,
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(cur, kwargs)
        db_conn.commit()
    db_conn.rollback()


def test_version_no_one_is_accepted(db_conn):
    """Already exercised by happy-path tests, but kept here explicitly
    so the version_no = 1 boundary has its own assertion line in this
    file (per Block 8.8A-SCHEMA scenario D).
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    lc_id, le_id = _create_logical_claim_with_v1(
        cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    db_conn.commit()

    kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        claim_logical_id=lc_id,
        claim_ledger_entry_id=le_id,
        evidence_span_id=chain["evidence_span_id"],
        version_no=1,
    )
    new_id = _insert(cur, kwargs)
    db_conn.commit()
    assert new_id is not None


# ===========================================================================
# E) UNIQUE (claim_ledger_entry_id, evidence_span_id, version_no).
# ===========================================================================
def test_unique_entry_span_version_rejects_duplicate(db_conn):
    """A second row with the same (entry, span, version_no) is rejected.
    A v2 row on the same (entry, span) pair is accepted (version_no
    differs).
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    lc_id, le_id = _create_logical_claim_with_v1(
        cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    db_conn.commit()

    base = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        claim_logical_id=lc_id,
        claim_ledger_entry_id=le_id,
        evidence_span_id=chain["evidence_span_id"],
        version_no=1,
    )
    _insert(cur, base)
    db_conn.commit()

    # v2 same pair, different idempotency_key -> accepted.
    base_v2 = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        claim_logical_id=lc_id,
        claim_ledger_entry_id=le_id,
        evidence_span_id=chain["evidence_span_id"],
        version_no=2,
    )
    _insert(cur, base_v2)
    db_conn.commit()

    # Re-inserting v1 on the same pair with a fresh idempotency_key
    # must violate cec_entry_span_version_uq.
    base_dup = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        claim_logical_id=lc_id,
        claim_ledger_entry_id=le_id,
        evidence_span_id=chain["evidence_span_id"],
        version_no=1,
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert(cur, base_dup)
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# F) UNIQUE (claim_ledger_entry_id, evidence_span_id, idempotency_key).
# ===========================================================================
def test_unique_entry_span_idem_rejects_duplicate(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    lc_id, le_id = _create_logical_claim_with_v1(
        cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    db_conn.commit()

    shared_idem = f"shared-idem-{uuid.uuid4()}"

    # First insert with (entry, span, idem, v1) — fine.
    _insert(
        cur,
        _valid_kwargs(
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            claim_logical_id=lc_id,
            claim_ledger_entry_id=le_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=1,
            idempotency_key=shared_idem,
        ),
    )
    db_conn.commit()

    # Second insert with same (entry, span, idem) but different
    # version_no. version_no alone would not collide, but idempotency_key
    # must.
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert(
            cur,
            _valid_kwargs(
                tenant_id=tenant_id,
                project_id=project_id,
                task_id=task_id,
                claim_logical_id=lc_id,
                claim_ledger_entry_id=le_id,
                evidence_span_id=chain["evidence_span_id"],
                version_no=2,
                idempotency_key=shared_idem,
            ),
        )
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# G) FK composite cec_entry_logical_consistency.
# ===========================================================================
def test_fk_composite_rejects_inconsistent_entry_logical_pair(db_conn):
    """A row whose (claim_ledger_entry_id, claim_logical_id) is
    structurally inconsistent — claim_ledger_entry_id from claim A but
    claim_logical_id of claim B — must be rejected by the composite FK
    cec_entry_logical_consistency (target: cle_id_logical_uq in 0004).
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )

    # Two distinct logical claims with their own v1 ledger entries.
    lc_a, le_a = _create_logical_claim_with_v1(
        cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    lc_b, le_b = _create_logical_claim_with_v1(
        cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    db_conn.commit()
    assert lc_a != lc_b
    assert le_a != le_b

    # Use entry_id of claim A but claim_logical_id of claim B. The
    # composite FK must reject because the target tuple does not exist
    # in claim_ledger_entries(id, claim_logical_id).
    bad_kwargs = _valid_kwargs(
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        claim_logical_id=lc_b,        # WRONG (belongs to claim B)
        claim_ledger_entry_id=le_a,   # entry of claim A
        evidence_span_id=chain["evidence_span_id"],
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _insert(cur, bad_kwargs)
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# H) Append-only: UPDATE and DELETE both rejected.
# ===========================================================================
def test_append_only_rejects_update(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    lc_id, le_id = _create_logical_claim_with_v1(
        cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    db_conn.commit()

    new_id = _insert(
        cur,
        _valid_kwargs(
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            claim_logical_id=lc_id,
            claim_ledger_entry_id=le_id,
            evidence_span_id=chain["evidence_span_id"],
        ),
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "UPDATE claim_entailment_checks SET verdict = 'uncertain' WHERE id = %s",
            (new_id,),
        )
        db_conn.commit()
    db_conn.rollback()


def test_append_only_rejects_delete(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    chain = _create_evidence_span_chain(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    lc_id, le_id = _create_logical_claim_with_v1(
        cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    db_conn.commit()

    new_id = _insert(
        cur,
        _valid_kwargs(
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            claim_logical_id=lc_id,
            claim_ledger_entry_id=le_id,
            evidence_span_id=chain["evidence_span_id"],
        ),
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "DELETE FROM claim_entailment_checks WHERE id = %s", (new_id,)
        )
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# I) Index / trigger / constraint smoke via information_schema + pg_catalog.
# ===========================================================================
def test_table_exists(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = current_schema()
          AND table_name   = 'claim_entailment_checks'
        """
    )
    assert cur.fetchone() is not None
    # Also assert the table is empty-friendly: COUNT(*) works (rerun-safe
    # tests may have inserted rows, so we don't assert the actual value;
    # we only assert the helper works without raising).
    n = _count_table(cur, table="claim_entailment_checks")
    assert isinstance(n, int)
    assert n >= 0


def test_verdict_check_constraint_exists(db_conn):
    """The named CHECK cec_verdict_chk must exist on the table."""
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        """
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'claim_entailment_checks'::regclass
          AND conname  = 'cec_verdict_chk'
          AND contype  = 'c'
        """
    )
    assert cur.fetchone() is not None


def test_append_only_trigger_exists_with_correct_firing(db_conn):
    """The trigger ``claim_entailment_checks_append_only`` must exist,
    BEFORE-fire on both UPDATE and DELETE, and be bound to the shared
    reject_modify_append_only() function.

    We use information_schema.triggers (textual, version-stable) for
    action_timing / event_manipulation / action_orientation, and
    pg_trigger/pg_proc only to map the trigger to its function. This
    avoids depending on the internal pg_trigger.tgtype bitmask layout.

    information_schema.triggers emits one row per (trigger, event), so
    a trigger declared as BEFORE UPDATE OR DELETE surfaces as two rows
    with event_manipulation='UPDATE' and event_manipulation='DELETE'.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()

    # 1) function binding: the trigger must call reject_modify_append_only.
    cur.execute(
        """
        SELECT p.proname
        FROM pg_trigger t
        JOIN pg_proc p ON p.oid = t.tgfoid
        WHERE t.tgrelid = 'claim_entailment_checks'::regclass
          AND t.tgname  = 'claim_entailment_checks_append_only'
          AND NOT t.tgisinternal
        """
    )
    row = cur.fetchone()
    assert row is not None, "append-only trigger is missing"
    assert str(row[0]) == "reject_modify_append_only", (
        f"trigger is bound to the wrong function: {row[0]!r}"
    )

    # 2) firing condition: BEFORE, ROW-level, on UPDATE and on DELETE.
    cur.execute(
        """
        SELECT event_manipulation, action_timing, action_orientation
        FROM information_schema.triggers
        WHERE event_object_schema = current_schema()
          AND event_object_table  = 'claim_entailment_checks'
          AND trigger_name        = 'claim_entailment_checks_append_only'
        """
    )
    rows = cur.fetchall()
    assert len(rows) >= 2, (
        f"expected at least 2 information_schema.triggers rows "
        f"(one per event), got {rows!r}"
    )
    events_seen: set[str] = set()
    for ev, timing, orientation in rows:
        events_seen.add(str(ev).upper())
        assert str(timing).upper() == "BEFORE", (
            f"trigger firing is not BEFORE: timing={timing!r} for event={ev!r}"
        )
        assert str(orientation).upper() == "ROW", (
            f"trigger is not row-level: orientation={orientation!r} for event={ev!r}"
        )
    assert "UPDATE" in events_seen, f"trigger does not cover UPDATE: {events_seen}"
    assert "DELETE" in events_seen, f"trigger does not cover DELETE: {events_seen}"
    # Append-only must allow INSERT.
    assert "INSERT" not in events_seen, (
        f"trigger unexpectedly covers INSERT: {events_seen}; "
        "append-only must allow INSERT"
    )


def test_unique_indexes_exist(db_conn):
    """Both UNIQUE indexes declared by 0009 must exist and be UNIQUE.

    Inspected via pg_indexes (textual) AND pg_index.indisunique (boolean).
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()

    expected = {
        "cec_entry_span_version_uq",
        "cec_entry_span_idem_uq",
    }

    cur.execute(
        """
        SELECT i.relname, idx.indisunique
        FROM pg_index idx
        JOIN pg_class i ON i.oid = idx.indexrelid
        WHERE idx.indrelid = 'claim_entailment_checks'::regclass
          AND i.relname = ANY(%s)
        """,
        (list(expected),),
    )
    rows = cur.fetchall()
    found = {str(r[0]) for r in rows}
    missing = expected - found
    assert not missing, f"missing expected indexes on claim_entailment_checks: {missing}"
    for r in rows:
        name, is_unique = str(r[0]), bool(r[1])
        assert is_unique, f"index {name} is not UNIQUE"


def test_lookup_indexes_exist(db_conn):
    """Defensive coverage on the four lookup indexes declared by 0009
    (cec_task_idx, cec_claim_logical_idx, cec_evidence_span_idx,
    cec_verdict_idx). We don't assert any uniqueness on these; we only
    verify their presence so a future refactor that drops one of them
    fails loudly here.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()

    expected = {
        "cec_task_idx",
        "cec_claim_logical_idx",
        "cec_evidence_span_idx",
        "cec_verdict_idx",
    }

    cur.execute(
        """
        SELECT i.relname
        FROM pg_index idx
        JOIN pg_class i ON i.oid = idx.indexrelid
        WHERE idx.indrelid = 'claim_entailment_checks'::regclass
          AND i.relname = ANY(%s)
        """,
        (list(expected),),
    )
    found = {str(r[0]) for r in cur.fetchall()}
    missing = expected - found
    assert not missing, f"missing expected lookup indexes: {missing}"


def test_composite_fk_consistency_constraint_exists(db_conn):
    """The composite FK cec_entry_logical_consistency must exist as a
    FOREIGN KEY on claim_entailment_checks targeting two columns of
    claim_ledger_entries.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        """
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'claim_entailment_checks'::regclass
          AND conname  = 'cec_entry_logical_consistency'
          AND contype  = 'f'
        """
    )
    assert cur.fetchone() is not None
