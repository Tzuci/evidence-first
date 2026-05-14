"""Worker-level tests for apps/worker/app/services/source_quality_evaluator.py
(Phase 8.7 — Block D).

Coverage map (13 scenarios required by the block prompt + 1 safety regression):

  1.  test_assess_evidence_span_inserts_valid_row
  2.  test_assess_document_chunk_inserts_valid_row
  3.  test_assess_document_inserts_valid_row
  4.  test_idempotency_replay_same_target_same_key
  5.  test_new_idempotency_key_creates_version_no_2
  6.  test_same_idempotency_key_across_different_targets_is_allowed
  7.  test_target_not_found_returns_not_found
  8.  test_invalid_target_zero_targets_no_insert
  9.  test_invalid_target_multiple_targets_no_insert
  10. test_does_not_mutate_claim_ledger_gate_or_source_loss_tables
  11. test_jsonb_payload_preserves_input_payload_and_semantic_warning
  12. test_confidence_in_expected_range
  13. test_service_imports_codomain_constants_from_shared
  14. test_assessment_uses_canonical_target_scope_not_caller_scope

Design notes:

  - This file lives under apps/worker/tests/. The Python package
    ``app`` resolves to apps/worker/app, so we can import the
    service entry point and the worker DB helper directly without
    any sys.path tweaking.

  - We DO NOT spin up Redis, a worker loop, an API, or a
    dispatcher. The service runs in isolation against the real DB.

  - All helpers are LOCAL to this file (no imports from other test
    files, per Phase 8.7D prompt). Seed helpers are duplicated from
    the patterns used in
    apps/worker/tests/test_source_loss_propagator_service.py and
    are NOT re-exported.

  - All identifiers, hashes, and idempotency keys are
    uuid.uuid4()-derived per invocation, so this file is safe to
    rerun against a long-lived dev DB.

  - The service requires an active SQLAlchemy Connection inside an
    explicit transaction. We always wrap setup writes in
    ``with engine.begin() as conn:``. Test assertions use
    ``with engine.connect() as conn:`` for read-only inspection.

  - We never touch claim_ledger_entries, final_gate_reports,
    published_answers, source_loss_events, etc. Scenario 10
    asserts that explicitly.

  - Per the block prompt, tests must be DB-real and must skip
    cleanly when DATABASE_URL is missing or the DB is unreachable.
    We expose ``_skip_if_db_unreachable()`` and call it at the
    start of every test.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db import get_engine
from app.services.source_quality_evaluator import (
    DEFAULT_EVALUATOR_NAME,
    DEFAULT_EVALUATOR_VERSION,
    DEFAULT_POLICY_NAME,
    DEFAULT_POLICY_VERSION,
    STATUS_ALREADY_ASSESSED,
    STATUS_ASSESSED,
    STATUS_INVALID_TARGET,
    STATUS_NOT_FOUND,
    assess_source_quality,
)


# ---------------------------------------------------------------------------
# environment guard
# ---------------------------------------------------------------------------
def _skip_if_db_unreachable() -> None:
    """Skip the test if Postgres is not reachable.

    The worker test suite normally assumes ``make up`` has been run; we
    add this guard so a stray invocation in a stripped environment
    skips cleanly rather than crashing on connection errors.
    """
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set; bring up the stack first.")
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("DB unreachable; run `make up` and `make migrate`.")


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------
def _unique_hex() -> str:
    """Return a rerun-safe sha256 hex string unique per call."""
    return hashlib.sha256(uuid.uuid4().bytes + uuid.uuid4().bytes).hexdigest()


# ---------------------------------------------------------------------------
# seed: tenant / user / project / task
# ---------------------------------------------------------------------------
def _seeded_dev(
    conn: Connection,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ensure tenant + user; create a FRESH project and task per invocation.

    Returns (tenant_id, project_id, user_id, task_id).
    """
    row = conn.execute(
        text(
            """
            INSERT INTO tenants (name, slug, status)
            VALUES ('Dev','dev','active')
            ON CONFLICT (slug) DO NOTHING
            RETURNING id
            """
        )
    ).first()
    if row is None:
        row = conn.execute(text("SELECT id FROM tenants WHERE slug = 'dev'")).one()
    tenant_id = uuid.UUID(str(row[0]))

    row = conn.execute(
        text(
            """
            INSERT INTO users (tenant_id, email, display_name, status)
            VALUES (:t, 'dev@local', 'Dev', 'active')
            ON CONFLICT (tenant_id, email) DO NOTHING
            RETURNING id
            """
        ),
        {"t": tenant_id},
    ).first()
    if row is None:
        row = conn.execute(
            text(
                "SELECT id FROM users WHERE tenant_id = :t AND email = 'dev@local'"
            ),
            {"t": tenant_id},
        ).one()
    user_id = uuid.UUID(str(row[0]))

    project_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO projects (tenant_id, name, mode_default)
                    VALUES (:t, :n, 'closed_corpus')
                    RETURNING id
                    """
                ),
                {"t": tenant_id, "n": f"sqe-svc-test-{uuid.uuid4()}"},
            ).first()[0]
        )
    )

    task_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO task_masters
                        (tenant_id, project_id, created_by, mode, objective, status)
                    VALUES (:t, :p, :u, 'closed_corpus', :o, 'created')
                    RETURNING id
                    """
                ),
                {
                    "t": tenant_id,
                    "p": project_id,
                    "u": user_id,
                    "o": f"obj-{uuid.uuid4()}",
                },
            ).first()[0]
        )
    )
    return tenant_id, project_id, user_id, task_id


# ---------------------------------------------------------------------------
# seed: storage chain ending in evidence_span
# ---------------------------------------------------------------------------
def _create_evidence_span_chain(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    created_by: uuid.UUID,
) -> dict[str, uuid.UUID]:
    """Create the full storage chain ending in an evidence_spans row.

    Order of inserts (to honor every FK and the storage_blobs unique
    partial index sb_global_uq):
      storage_blobs -> storage_objects -> uploaded_documents
        -> document_versions (kind='parsed') -> document_chunks
        -> evidence_spans

    Returns a dict with all the resulting ids:
      - document_id, document_version_id, document_chunk_id,
        evidence_span_id
    """
    marker = uuid.uuid4().hex[:12]
    quote = f"quotable span {marker}"
    chunk_text = (
        f"Source quality evaluator test marker {marker}. "
        f"This sentence contains the digit 7 and a {quote}."
    )
    content_hash_payload = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
    size_bytes = len(chunk_text.encode("utf-8"))

    blob_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO storage_blobs
                        (id, tenant_namespace_id, content_hash, hash_algorithm,
                         size_bytes, mime_type, storage_backend, local_path, refcount)
                    VALUES
                        (:id, NULL, :h, 'sha256',
                         :sz, 'text/plain', 'local_fs', :lp, 0)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    # Salt the content_hash so the global UNIQUE
                    # (content_hash, hash_algorithm) WHERE
                    # tenant_namespace_id IS NULL never collides on a
                    # long-running dev DB.
                    "h": content_hash_payload + "-" + uuid.uuid4().hex,
                    "sz": size_bytes,
                    "lp": f"/dev/null/{uuid.uuid4()}",
                },
            ).first()[0]
        )
    )

    storage_object_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO storage_objects
                        (id, tenant_id, project_id, blob_id,
                         object_type, logical_owner_kind, logical_owner_id)
                    VALUES
                        (:id, :t, :p, :b,
                         'upload', 'uploaded_document', :oid)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": tenant_id,
                    "p": project_id,
                    "b": blob_id,
                    "oid": uuid.uuid4(),
                },
            ).first()[0]
        )
    )

    document_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO uploaded_documents
                        (id, tenant_id, project_id, storage_object_id,
                         filename, content_hash, mime_type, size_bytes,
                         tier, language, created_by)
                    VALUES
                        (:id, :t, :p, :so,
                         :fn, :h, 'text/plain', :sz,
                         'user_provided', 'und', :u)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": tenant_id,
                    "p": project_id,
                    "so": storage_object_id,
                    "fn": f"doc-{marker}.txt",
                    "h": content_hash_payload,
                    "sz": size_bytes,
                    "u": created_by,
                },
            ).first()[0]
        )
    )

    document_version_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO document_versions
                        (id, document_id, version_no, version_kind,
                         storage_object_id, inline_text, text_hash)
                    VALUES
                        (:id, :did, 1, 'parsed',
                         :so, :it, :th)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "did": document_id,
                    "so": storage_object_id,
                    "it": chunk_text,
                    "th": hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                },
            ).first()[0]
        )
    )

    document_chunk_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO document_chunks
                        (id, document_version_id, chunk_index,
                         char_start, char_end, inline_text, text_hash)
                    VALUES
                        (:id, :dv, 0,
                         0, :ce, :it, :th)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "dv": document_version_id,
                    "ce": len(chunk_text),
                    "it": chunk_text,
                    "th": hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                },
            ).first()[0]
        )
    )

    char_start = chunk_text.index(quote)
    char_end = char_start + len(quote)
    evidence_span_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO evidence_spans
                        (id, document_chunk_id, char_start, char_end,
                         quote, quote_hash)
                    VALUES
                        (:id, :cid, :cs, :ce, :q, :qh)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "cid": document_chunk_id,
                    "cs": char_start,
                    "ce": char_end,
                    "q": quote,
                    "qh": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                },
            ).first()[0]
        )
    )

    return {
        "document_id": document_id,
        "document_version_id": document_version_id,
        "document_chunk_id": document_chunk_id,
        "evidence_span_id": evidence_span_id,
    }


# ---------------------------------------------------------------------------
# DB inspection helpers
# ---------------------------------------------------------------------------
def _fetch_assessment(
    conn: Connection, *, assessment_id: uuid.UUID
) -> dict[str, Any]:
    """Return the full source_quality_assessments row as a dict.

    Caller MUST have ensured the row exists; otherwise this raises
    NoResultFound. The payload column is normalized so callers always
    receive a Python dict (psycopg may surface JSONB either as a
    native object or as a JSON string depending on driver config).
    """
    row = conn.execute(
        text(
            """
            SELECT id, tenant_id, project_id,
                   evidence_span_id, document_chunk_id, document_id,
                   version_no,
                   source_type, source_role, authority_level, independence_level,
                   freshness, relevance, extract_quality, contradiction_status,
                   overall_quality, confidence,
                   evaluator_name, evaluator_version,
                   policy_name, policy_version,
                   idempotency_key, payload, created_at
            FROM source_quality_assessments
            WHERE id = :id
            """
        ),
        {"id": assessment_id},
    ).one()
    m = dict(row._mapping)
    payload = m["payload"]
    if isinstance(payload, str):
        import json as _json
        m["payload"] = _json.loads(payload)
    return m


def _count_assessments_for_target(
    conn: Connection,
    *,
    evidence_span_id: uuid.UUID | None = None,
    document_chunk_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
) -> int:
    if evidence_span_id is not None:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM source_quality_assessments "
                    "WHERE evidence_span_id = :tid"
                ),
                {"tid": evidence_span_id},
            ).scalar_one()
        )
    if document_chunk_id is not None:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM source_quality_assessments "
                    "WHERE document_chunk_id = :tid"
                ),
                {"tid": document_chunk_id},
            ).scalar_one()
        )
    if document_id is not None:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM source_quality_assessments "
                    "WHERE document_id = :tid"
                ),
                {"tid": document_id},
            ).scalar_one()
        )
    raise ValueError("_count_assessments_for_target: provide one target")


def _table_count(conn: Connection, *, table: str) -> int:
    return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


# ===========================================================================
# 1) assess evidence_span — inserts a valid row
# ===========================================================================
def test_assess_evidence_span_inserts_valid_row():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, _task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )

    idem = _unique_hex()
    with engine.begin() as conn:
        result = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            idempotency_key=idem,
        )

    assert result["status"] == STATUS_ASSESSED
    assert result["target_type"] == "evidence_span"
    assert result["version_no"] == 1
    assessment_id = uuid.UUID(result["assessment_id"])

    with engine.connect() as conn:
        row = _fetch_assessment(conn, assessment_id=assessment_id)

    # Target XOR: evidence_span_id is set, the other two are NULL.
    assert uuid.UUID(str(row["evidence_span_id"])) == chain["evidence_span_id"]
    assert row["document_chunk_id"] is None
    assert row["document_id"] is None

    # version_no.
    assert int(row["version_no"]) == 1

    # Mock dimensions — see service module docstring.
    assert str(row["source_type"]) == "user_document"
    assert str(row["source_role"]) == "unclear"
    assert str(row["authority_level"]) == "unknown"
    assert str(row["independence_level"]) == "unknown"
    assert str(row["freshness"]) == "undated"
    assert str(row["relevance"]) == "direct_support"
    assert str(row["extract_quality"]) == "exact_quote_match"
    assert str(row["contradiction_status"]) == "unchecked"
    assert str(row["overall_quality"]) == "unknown"
    assert float(row["confidence"]) == pytest.approx(0.5)

    # Provenance and idempotency.
    assert str(row["evaluator_name"]) == DEFAULT_EVALUATOR_NAME
    assert str(row["evaluator_version"]) == DEFAULT_EVALUATOR_VERSION
    assert str(row["policy_name"]) == DEFAULT_POLICY_NAME
    assert str(row["policy_version"]) == DEFAULT_POLICY_VERSION
    assert str(row["idempotency_key"]) == idem

    # Scope written to DB is the CANONICAL scope of the target row. In
    # this happy path the caller passed the same tenant/project that
    # _create_evidence_span_chain stamped on uploaded_documents, so the
    # caller scope and the canonical scope happen to coincide; we lock
    # the contract down explicitly so a future refactor cannot quietly
    # diverge from this. The dedicated test
    # test_assessment_uses_canonical_target_scope_not_caller_scope
    # exercises the case where caller scope differs from canonical
    # scope and asserts that canonical wins.
    assert uuid.UUID(str(row["tenant_id"])) == tenant_id
    assert uuid.UUID(str(row["project_id"])) == project_id
    assert result["tenant_id"] == str(tenant_id)
    assert result["project_id"] == str(project_id)

    # Payload markers.
    payload = row["payload"]
    assert isinstance(payload, dict)
    assert payload.get("mock") is True
    assert payload.get("target_type") == "evidence_span"
    assert payload.get("semantic_warning") == (
        "source_quality_does_not_mean_claim_truth"
    )


# ===========================================================================
# 2) assess document_chunk — inserts a valid row
# ===========================================================================
def test_assess_document_chunk_inserts_valid_row():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, _task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )

    idem = _unique_hex()
    with engine.begin() as conn:
        result = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            document_chunk_id=chain["document_chunk_id"],
            idempotency_key=idem,
        )

    assert result["status"] == STATUS_ASSESSED
    assert result["target_type"] == "document_chunk"
    assert result["version_no"] == 1
    assessment_id = uuid.UUID(result["assessment_id"])

    with engine.connect() as conn:
        row = _fetch_assessment(conn, assessment_id=assessment_id)

    assert row["evidence_span_id"] is None
    assert uuid.UUID(str(row["document_chunk_id"])) == chain["document_chunk_id"]
    assert row["document_id"] is None
    assert int(row["version_no"]) == 1
    assert str(row["relevance"]) == "contextual_support"
    assert str(row["extract_quality"]) == "partial_match"
    assert str(row["overall_quality"]) == "unknown"


# ===========================================================================
# 3) assess document — inserts a valid row
# ===========================================================================
def test_assess_document_inserts_valid_row():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, _task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )

    idem = _unique_hex()
    with engine.begin() as conn:
        result = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            document_id=chain["document_id"],
            idempotency_key=idem,
        )

    assert result["status"] == STATUS_ASSESSED
    assert result["target_type"] == "document"
    assert result["version_no"] == 1
    assessment_id = uuid.UUID(result["assessment_id"])

    with engine.connect() as conn:
        row = _fetch_assessment(conn, assessment_id=assessment_id)

    assert row["evidence_span_id"] is None
    assert row["document_chunk_id"] is None
    assert uuid.UUID(str(row["document_id"])) == chain["document_id"]
    assert int(row["version_no"]) == 1


# ===========================================================================
# 4) idempotency replay — same target + same idempotency_key
# ===========================================================================
def test_idempotency_replay_same_target_same_key():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, _task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )

    idem = _unique_hex()

    with engine.begin() as conn:
        result_1 = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            idempotency_key=idem,
        )
    assert result_1["status"] == STATUS_ASSESSED

    with engine.begin() as conn:
        result_2 = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            idempotency_key=idem,
        )
    assert result_2["status"] == STATUS_ALREADY_ASSESSED
    assert result_2["assessment_id"] == result_1["assessment_id"]
    assert result_2["version_no"] == result_1["version_no"] == 1

    with engine.connect() as conn:
        assert (
            _count_assessments_for_target(
                conn, evidence_span_id=chain["evidence_span_id"]
            )
            == 1
        )


# ===========================================================================
# 5) new idempotency_key on same target -> version_no 2
# ===========================================================================
def test_new_idempotency_key_creates_version_no_2():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, _task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )

    idem_1 = _unique_hex()
    idem_2 = _unique_hex()

    with engine.begin() as conn:
        result_1 = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            idempotency_key=idem_1,
        )
    assert result_1["status"] == STATUS_ASSESSED
    assert result_1["version_no"] == 1

    with engine.begin() as conn:
        result_2 = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            idempotency_key=idem_2,
        )
    assert result_2["status"] == STATUS_ASSESSED
    assert result_2["version_no"] == 2
    assert result_2["assessment_id"] != result_1["assessment_id"]

    with engine.connect() as conn:
        assert (
            _count_assessments_for_target(
                conn, evidence_span_id=chain["evidence_span_id"]
            )
            == 2
        )


# ===========================================================================
# 6) same idempotency_key across different targets is allowed
# ===========================================================================
def test_same_idempotency_key_across_different_targets_is_allowed():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, _task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )

    shared_idem = f"cross-target-{_unique_hex()}"

    with engine.begin() as conn:
        result_span = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            idempotency_key=shared_idem,
        )
    assert result_span["status"] == STATUS_ASSESSED

    with engine.begin() as conn:
        result_chunk = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            document_chunk_id=chain["document_chunk_id"],
            idempotency_key=shared_idem,
        )
    assert result_chunk["status"] == STATUS_ASSESSED

    assert result_span["assessment_id"] != result_chunk["assessment_id"]

    with engine.connect() as conn:
        # Each target has exactly one assessment with the shared key.
        assert (
            _count_assessments_for_target(
                conn, evidence_span_id=chain["evidence_span_id"]
            )
            == 1
        )
        assert (
            _count_assessments_for_target(
                conn, document_chunk_id=chain["document_chunk_id"]
            )
            == 1
        )


# ===========================================================================
# 7) target not found -> status='not_found', no insert
# ===========================================================================
def test_target_not_found_returns_not_found():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, _user_id, _task_id = _seeded_dev(conn)
        before = _table_count(conn, table="source_quality_assessments")

    bogus_span = uuid.uuid4()
    with engine.begin() as conn:
        result = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=bogus_span,
            idempotency_key=_unique_hex(),
        )

    assert result["status"] == STATUS_NOT_FOUND
    assert result["assessment_id"] is None
    assert result["version_no"] is None

    with engine.connect() as conn:
        after = _table_count(conn, table="source_quality_assessments")
    assert after == before  # no insert


# ===========================================================================
# 8) invalid target — zero targets -> no insert
# ===========================================================================
def test_invalid_target_zero_targets_no_insert():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, _user_id, _task_id = _seeded_dev(conn)
        before = _table_count(conn, table="source_quality_assessments")

    with engine.begin() as conn:
        result = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            # zero targets
            idempotency_key=_unique_hex(),
        )

    assert result["status"] == STATUS_INVALID_TARGET
    assert result["assessment_id"] is None
    assert result["version_no"] is None
    assert result["target_type"] is None

    with engine.connect() as conn:
        after = _table_count(conn, table="source_quality_assessments")
    assert after == before  # no insert


# ===========================================================================
# 9) invalid target — multiple targets -> no insert
# ===========================================================================
def test_invalid_target_multiple_targets_no_insert():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, _task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        before = _table_count(conn, table="source_quality_assessments")

    # Two targets at the same time.
    with engine.begin() as conn:
        result_two = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=chain["document_chunk_id"],
            idempotency_key=_unique_hex(),
        )
    assert result_two["status"] == STATUS_INVALID_TARGET

    # Three targets at the same time.
    with engine.begin() as conn:
        result_three = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=chain["document_chunk_id"],
            document_id=chain["document_id"],
            idempotency_key=_unique_hex(),
        )
    assert result_three["status"] == STATUS_INVALID_TARGET

    with engine.connect() as conn:
        after = _table_count(conn, table="source_quality_assessments")
    assert after == before  # no insert


# ===========================================================================
# 10) does NOT mutate claim ledger / gate / source_loss tables
# ===========================================================================
def test_does_not_mutate_claim_ledger_gate_or_source_loss_tables():
    """Snapshot count pre/post on every table the service must NEVER touch.

    The seven tables enumerated in the prompt are the only ones whose
    counts must stay invariant across the call. If a future refactor
    accidentally chains a write into any of them, this test fails loudly.

    audit_records is also snapshotted because the prompt explicitly says
    "non emette audit_records" — though it is not in the enumerated list
    of seven, the audit invariant is part of the same scope. We assert
    on it separately and clearly so the reader can see it covered.
    """
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, _task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )

    snapshot_tables = (
        "claim_ledger_entries",
        "logical_claims",
        "verification_records",
        "final_gate_reports",
        "published_answers",
        "source_loss_events",
        "source_loss_propagation_records",
    )

    with engine.connect() as conn:
        before = {t: _table_count(conn, table=t) for t in snapshot_tables}
        audit_before = _table_count(conn, table="audit_records")

    with engine.begin() as conn:
        result = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            idempotency_key=_unique_hex(),
        )
    assert result["status"] == STATUS_ASSESSED

    with engine.connect() as conn:
        after = {t: _table_count(conn, table=t) for t in snapshot_tables}
        audit_after = _table_count(conn, table="audit_records")

    for t in snapshot_tables:
        assert before[t] == after[t], (
            f"service must NOT mutate {t}: before={before[t]} after={after[t]}"
        )
    assert audit_before == audit_after, (
        f"service must NOT emit audit_records: "
        f"before={audit_before} after={audit_after}"
    )


# ===========================================================================
# 11) JSONB payload preserves input_payload and semantic_warning
# ===========================================================================
def test_jsonb_payload_preserves_input_payload_and_semantic_warning():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, _task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )

    input_payload = {
        "scenario": "phase_8_7_d_payload_preservation",
        "scores": {"a": 1, "b": 0.5},
        "tags": ["primary", "stale"],
        "nested": {"deep": {"value": True}},
    }

    with engine.begin() as conn:
        result = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            idempotency_key=_unique_hex(),
            payload=input_payload,
        )
    assert result["status"] == STATUS_ASSESSED
    assessment_id = uuid.UUID(result["assessment_id"])

    with engine.connect() as conn:
        row = _fetch_assessment(conn, assessment_id=assessment_id)

    payload = row["payload"]
    assert isinstance(payload, dict)
    # Structural keys stamped by the service.
    assert payload.get("mock") is True
    assert payload.get("target_type") == "evidence_span"
    assert payload.get("semantic_warning") == (
        "source_quality_does_not_mean_claim_truth"
    )
    # Caller payload preserved verbatim under input_payload.
    assert payload.get("input_payload") == input_payload


# ===========================================================================
# 12) confidence is in [0, 1]
# ===========================================================================
def test_confidence_in_expected_range():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, _task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )

    with engine.begin() as conn:
        result = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            idempotency_key=_unique_hex(),
        )
    assert result["status"] == STATUS_ASSESSED
    assessment_id = uuid.UUID(result["assessment_id"])

    with engine.connect() as conn:
        row = _fetch_assessment(conn, assessment_id=assessment_id)

    conf = row["confidence"]
    assert conf is not None
    conf_f = float(conf)
    assert 0.0 <= conf_f <= 1.0
    # The mock policy fixes it at 0.5 — assert that too so future
    # refactors don't silently shift the value.
    assert conf_f == pytest.approx(0.5)


# ===========================================================================
# 13) service imports codomain constants from shared (no blind duplication)
# ===========================================================================
def test_service_imports_codomain_constants_from_shared():
    """Verify the service module imports SOURCE_QUALITY_*_VALUES from the
    shared package rather than re-declaring its own codomain constants.

    This is the explicit contract from the block prompt: 'Il service
    deve usare SOURCE_QUALITY_*_VALUES o almeno importare i codomini
    dal package shared per evitare duplicazione cieca.'

    We check it by importing the service module and asserting that
    the relevant names resolve to the SAME object identities as in
    evidencefirst_shared.schemas. Identity comparison (``is``) is
    deliberate: equality on tuples would also pass for an accidentally
    duplicated tuple of the same strings, defeating the contract.
    """
    from app.services import source_quality_evaluator as svc
    from evidencefirst_shared import schemas as shared

    paired = (
        "SOURCE_QUALITY_SOURCE_TYPE_VALUES",
        "SOURCE_QUALITY_SOURCE_ROLE_VALUES",
        "SOURCE_QUALITY_AUTHORITY_LEVEL_VALUES",
        "SOURCE_QUALITY_INDEPENDENCE_LEVEL_VALUES",
        "SOURCE_QUALITY_FRESHNESS_VALUES",
        "SOURCE_QUALITY_RELEVANCE_VALUES",
        "SOURCE_QUALITY_EXTRACT_QUALITY_VALUES",
        "SOURCE_QUALITY_CONTRADICTION_STATUS_VALUES",
        "SOURCE_QUALITY_OVERALL_QUALITY_VALUES",
    )
    for name in paired:
        svc_value = getattr(svc, name, None)
        shared_value = getattr(shared, name)
        assert svc_value is shared_value, (
            f"{name} on the service module must BE the same tuple as on "
            f"evidencefirst_shared.schemas; got "
            f"svc={svc_value!r}, shared={shared_value!r}"
        )


# ===========================================================================
# 14) assessment uses CANONICAL target scope, NOT caller-supplied scope
# ===========================================================================
def _create_extra_project(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    name_suffix: str,
) -> uuid.UUID:
    """Create a second project on the same tenant.

    Local helper used only by the canonical-scope test below. We
    deliberately do NOT touch the seed helpers further up because the
    rest of the suite depends on their exact behavior.
    """
    return uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO projects (tenant_id, name, mode_default)
                    VALUES (:t, :n, 'closed_corpus')
                    RETURNING id
                    """
                ),
                {"t": tenant_id, "n": f"sqe-svc-extra-{name_suffix}-{uuid.uuid4()}"},
            ).first()[0]
        )
    )


def test_assessment_uses_canonical_target_scope_not_caller_scope():
    """The INSERT must use the target row's CANONICAL tenant/project,
    not what the caller passed in.

    Setup:
      - one tenant, two projects (A and B);
      - storage/document/evidence_span chain rooted in project A
        (uploaded_documents.project_id = project_A).

    Action:
      - call assess_source_quality with project_id=project_B but
        evidence_span_id pointing into project_A's chain.

    Expected:
      - the row in source_quality_assessments has
        project_id == project_A (CANONICAL scope of the target);
      - result['project_id'] == project_A (same value also returned);
      - result['tenant_id'] == tenant (canonical, unambiguous);
      - the call succeeds with status='assessed' (no error: caller
        supplying a different project_id is not a target-not-found
        condition — the service silently overrides it with the
        canonical scope).

    This is a regression test for the micro-fix that switched the
    INSERT call site from
        tenant_id=tenant_id, project_id=project_id   (caller-supplied)
    to
        tenant_id=canonical_tenant_id,
        project_id=canonical_project_id              (read from target).
    """
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_a_id, user_id, _task_id = _seeded_dev(conn)
        project_b_id = _create_extra_project(
            conn, tenant_id=tenant_id, name_suffix="b"
        )
        # The chain is rooted in project_A.
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_a_id,
            created_by=user_id,
        )

    assert project_a_id != project_b_id  # sanity

    with engine.begin() as conn:
        result = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            # Caller deliberately passes the WRONG project.
            project_id=project_b_id,
            evidence_span_id=chain["evidence_span_id"],
            idempotency_key=_unique_hex(),
        )

    assert result["status"] == STATUS_ASSESSED
    assessment_id = uuid.UUID(result["assessment_id"])

    # Response carries the canonical scope.
    assert result["tenant_id"] == str(tenant_id)
    assert result["project_id"] == str(project_a_id)
    assert result["project_id"] != str(project_b_id)

    # The row actually persisted to source_quality_assessments carries
    # the canonical scope as well. This is the critical invariant: a
    # mistaken caller cannot pollute the table with FK-valid but
    # semantically wrong project assignments.
    with engine.connect() as conn:
        row = _fetch_assessment(conn, assessment_id=assessment_id)

    assert uuid.UUID(str(row["tenant_id"])) == tenant_id
    assert uuid.UUID(str(row["project_id"])) == project_a_id
    assert uuid.UUID(str(row["project_id"])) != project_b_id
