"""Worker-level tests for apps/worker/app/services/source_quality_orchestrator.py
(Phase 8.7 — Block E).

Coverage map (7 scenarios required by the block prompt):

  1. test_orchestrator_assesses_each_evidence_span_linked_to_task
  2. test_orchestrator_returns_zero_for_task_without_claim_links
  3. test_orchestrator_is_idempotent_on_redelivery
  4. test_orchestrator_uses_deterministic_idempotency_key
  5. test_orchestrator_does_not_assess_spans_orphan_from_task
  6. test_orchestrator_does_not_mutate_other_domain_tables
  7. test_orchestrator_unknown_task_returns_not_found

Design notes:

  - This file lives under apps/worker/tests/. The Python package
    ``app`` resolves to apps/worker/app, so we can import the
    orchestrator entry point and the worker DB helper directly
    without any sys.path tweaking.

  - We DO NOT spin up Redis, a worker loop, an API, or a dispatcher.
    The orchestrator runs in isolation against the real DB.

  - All helpers are LOCAL to this file (no imports from other test
    files, per the Phase 8.7E prompt). Seed helpers mirror the
    patterns used in
    apps/worker/tests/test_source_quality_evaluator_service.py
    and are NOT re-exported.

  - All identifiers, hashes, and span texts are uuid.uuid4()-derived
    per invocation, so this file is safe to rerun against a
    long-lived dev DB.

  - The orchestrator MUST NOT touch any table other than
    source_quality_assessments (via assess_source_quality) and MUST
    NOT emit audit. Test 6 asserts that explicitly via a pre/post
    snapshot of every relevant table.

  - The ``_table_count`` helper interpolates the table name into the
    SQL string (no bind params allowed for identifiers in
    SQLAlchemy text()). To stay safe against accidental injection
    via test typos, ``_table_count`` validates ``table`` against a
    whitelist of allowed table names (Correction 5 of the 8.7E
    micro-fix).

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
from app.services.source_quality_orchestrator import (
    run_source_quality_assessment,
)


# ---------------------------------------------------------------------------
# environment guard
# ---------------------------------------------------------------------------
def _skip_if_db_unreachable() -> None:
    """Skip the test if Postgres is not reachable.

    The worker test suite normally assumes ``make up`` has been run;
    we add this guard so a stray invocation in a stripped environment
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


# Correction 5 (8.7E micro-fix): _table_count interpolates ``table`` into
# the SQL text (SQLAlchemy ``text()`` cannot parameterize identifiers).
# To prevent accidental injection via a test typo, we validate against
# this explicit whitelist. Add a name here only when a new test really
# needs to snapshot that table.
_ALLOWED_COUNT_TABLES: frozenset[str] = frozenset(
    {
        "source_quality_assessments",
        "claim_ledger_entries",
        "logical_claims",
        "verification_records",
        "final_gate_reports",
        "published_answers",
        "source_loss_events",
        "source_loss_propagation_records",
        "audit_records",
    }
)


# ---------------------------------------------------------------------------
# seed: tenant / user / project / task
# ---------------------------------------------------------------------------
def _ensure_tenant_and_user(
    conn: Connection,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Idempotently ensure the dev tenant + user; return (tenant_id, user_id)."""
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
    return tenant_id, user_id


def _create_project(
    conn: Connection, *, tenant_id: uuid.UUID, name_suffix: str
) -> uuid.UUID:
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
                {"t": tenant_id, "n": f"sqo-orch-test-{name_suffix}-{uuid.uuid4()}"},
            ).first()[0]
        )
    )


def _create_task(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
) -> uuid.UUID:
    return uuid.UUID(
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


def _seeded_dev(
    conn: Connection,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ensure tenant + user; create a FRESH project and task per
    invocation.

    Returns (tenant_id, project_id, user_id, task_id).
    """
    tenant_id, user_id = _ensure_tenant_and_user(conn)
    project_id = _create_project(conn, tenant_id=tenant_id, name_suffix="single")
    task_id = _create_task(
        conn, tenant_id=tenant_id, project_id=project_id, user_id=user_id
    )
    return tenant_id, project_id, user_id, task_id


# ---------------------------------------------------------------------------
# seed: storage chain ending in evidence_span
# ---------------------------------------------------------------------------
def _create_evidence_span(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    created_by: uuid.UUID,
) -> uuid.UUID:
    """Create the full storage chain ending in an evidence_spans row
    and return just the evidence_span_id.

    Order of inserts (to honor every FK and the storage_blobs unique
    partial index sb_global_uq):
      storage_blobs -> storage_objects -> uploaded_documents
        -> document_versions (kind='parsed') -> document_chunks
        -> evidence_spans
    """
    marker = uuid.uuid4().hex[:12]
    quote = f"quotable span {marker}"
    chunk_text = (
        f"Source quality orchestrator test marker {marker}. "
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
    return evidence_span_id


# ---------------------------------------------------------------------------
# seed: claim + claim_evidence_link
# ---------------------------------------------------------------------------
def _create_logical_claim_with_v1(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert one logical_claims row + one v1 claim_ledger_entries row
    in state 'verified_fact'.

    Returns (claim_logical_id, claim_ledger_entry_id_v1).
    """
    claim_logical_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO logical_claims
                        (id, tenant_id, project_id, task_id,
                         canonical_claim_text, canonical_claim_hash)
                    VALUES (:id, :t, :p, :tid, :ct, :ch)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": tenant_id,
                    "p": project_id,
                    "tid": task_id,
                    "ct": f"canonical-{uuid.uuid4()}",
                    "ch": _unique_hex(),
                },
            ).first()[0]
        )
    )
    claim_ledger_entry_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO claim_ledger_entries
                        (id, claim_logical_id, version_no, state,
                         support_scope, user_provided_dependency,
                         transition_reason)
                    VALUES (:id, :lc, 1, 'verified_fact',
                            'supported_by_user_corpus_only',
                            'supported_by_user_corpus_only',
                            'seeded_for_test')
                    RETURNING id
                    """
                ),
                {"id": uuid.uuid4(), "lc": claim_logical_id},
            ).first()[0]
        )
    )
    return claim_logical_id, claim_ledger_entry_id


def _link_claim_to_span(
    conn: Connection,
    *,
    claim_logical_id: uuid.UUID,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
) -> None:
    """Insert one claim_evidence_links row connecting (logical, entry)
    to the given evidence_span. Honors cel_origin_xor.
    """
    conn.execute(
        text(
            """
            INSERT INTO claim_evidence_links
                (id, claim_logical_id, claim_ledger_entry_id,
                 evidence_span_id, retrieved_source_span_id, link_role)
            VALUES (:id, :lc, :le, :es, NULL, 'primary_support')
            """
        ),
        {
            "id": uuid.uuid4(),
            "lc": claim_logical_id,
            "le": claim_ledger_entry_id,
            "es": evidence_span_id,
        },
    )


# ---------------------------------------------------------------------------
# count helpers
# ---------------------------------------------------------------------------
def _count_sqa_for_span(
    conn: Connection, *, evidence_span_id: uuid.UUID
) -> int:
    return int(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM source_quality_assessments "
                "WHERE evidence_span_id = :tid"
            ),
            {"tid": evidence_span_id},
        ).scalar_one()
    )


def _table_count(conn: Connection, *, table: str) -> int:
    """Return COUNT(*) of ``table``.

    Correction 5 (8.7E micro-fix): ``table`` is interpolated into the
    SQL text because SQLAlchemy ``text()`` cannot parameterize
    identifiers. We validate the input against ``_ALLOWED_COUNT_TABLES``
    to keep the seam typo-safe and injection-safe.
    """
    if table not in _ALLOWED_COUNT_TABLES:
        raise ValueError(
            f"_table_count: table {table!r} is not in the allowed "
            f"whitelist {_ALLOWED_COUNT_TABLES!r}"
        )
    return int(
        conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
    )


def _fetch_sqa_idempotency_keys_for_span(
    conn: Connection, *, evidence_span_id: uuid.UUID
) -> list[str]:
    rows = conn.execute(
        text(
            """
            SELECT idempotency_key
            FROM source_quality_assessments
            WHERE evidence_span_id = :tid
            """
        ),
        {"tid": evidence_span_id},
    ).fetchall()
    return [str(r[0]) for r in rows]


# ===========================================================================
# 1) orchestrator assesses each evidence_span linked to the task
# ===========================================================================
def test_orchestrator_assesses_each_evidence_span_linked_to_task():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        # Two evidence_spans, each linked to its own logical_claim.
        span_a = _create_evidence_span(
            conn, tenant_id=tenant_id, project_id=project_id, created_by=user_id
        )
        span_b = _create_evidence_span(
            conn, tenant_id=tenant_id, project_id=project_id, created_by=user_id
        )
        lc_a, le_a = _create_logical_claim_with_v1(
            conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
        )
        lc_b, le_b = _create_logical_claim_with_v1(
            conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_a,
            claim_ledger_entry_id=le_a,
            evidence_span_id=span_a,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_b,
            claim_ledger_entry_id=le_b,
            evidence_span_id=span_b,
        )

    with engine.begin() as conn:
        result = run_source_quality_assessment(conn, task_id=task_id)

    assert result["status"] == "completed"
    assert result["spans_total"] == 2
    assert result["assessed_count"] == 2
    assert result["already_assessed_count"] == 0
    assert result["not_found_count"] == 0
    assert result["invalid_target_count"] == 0
    assert result["error_count"] == 0

    with engine.connect() as conn:
        # One row per span in source_quality_assessments.
        assert _count_sqa_for_span(conn, evidence_span_id=span_a) == 1
        assert _count_sqa_for_span(conn, evidence_span_id=span_b) == 1


# ===========================================================================
# 2) task without claim links -> zero spans, no insert
# ===========================================================================
def test_orchestrator_returns_zero_for_task_without_claim_links():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)
        before = _table_count(conn, table="source_quality_assessments")

    with engine.begin() as conn:
        result = run_source_quality_assessment(conn, task_id=task_id)

    assert result["status"] == "completed"
    assert result["spans_total"] == 0
    assert result["assessed_count"] == 0
    assert result["already_assessed_count"] == 0
    assert result["not_found_count"] == 0
    assert result["invalid_target_count"] == 0
    assert result["error_count"] == 0

    with engine.connect() as conn:
        after = _table_count(conn, table="source_quality_assessments")
    # No insert at all.
    assert after == before


# ===========================================================================
# 3) idempotent on redelivery
# ===========================================================================
def test_orchestrator_is_idempotent_on_redelivery():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        span_a = _create_evidence_span(
            conn, tenant_id=tenant_id, project_id=project_id, created_by=user_id
        )
        span_b = _create_evidence_span(
            conn, tenant_id=tenant_id, project_id=project_id, created_by=user_id
        )
        lc_a, le_a = _create_logical_claim_with_v1(
            conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
        )
        lc_b, le_b = _create_logical_claim_with_v1(
            conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_a,
            claim_ledger_entry_id=le_a,
            evidence_span_id=span_a,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_b,
            claim_ledger_entry_id=le_b,
            evidence_span_id=span_b,
        )

    # First run.
    with engine.begin() as conn:
        result_1 = run_source_quality_assessment(conn, task_id=task_id)
    assert result_1["status"] == "completed"
    assert result_1["spans_total"] == 2
    assert result_1["assessed_count"] == 2
    assert result_1["already_assessed_count"] == 0

    # Second run: same task, idempotency_keys are deterministic, so
    # every span collapses to 'already_assessed' at the service level.
    with engine.begin() as conn:
        result_2 = run_source_quality_assessment(conn, task_id=task_id)
    assert result_2["status"] == "completed"
    assert result_2["spans_total"] == 2
    assert result_2["assessed_count"] == 0
    assert result_2["already_assessed_count"] == 2

    # Row count per span unchanged.
    with engine.connect() as conn:
        assert _count_sqa_for_span(conn, evidence_span_id=span_a) == 1
        assert _count_sqa_for_span(conn, evidence_span_id=span_b) == 1


# ===========================================================================
# 4) deterministic idempotency_key format
# ===========================================================================
def test_orchestrator_uses_deterministic_idempotency_key():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        span_id = _create_evidence_span(
            conn, tenant_id=tenant_id, project_id=project_id, created_by=user_id
        )
        lc, le = _create_logical_claim_with_v1(
            conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc,
            claim_ledger_entry_id=le,
            evidence_span_id=span_id,
        )

    with engine.begin() as conn:
        result = run_source_quality_assessment(conn, task_id=task_id)
    assert result["status"] == "completed"
    assert result["assessed_count"] == 1

    expected_key = f"task:{task_id}:span:{span_id}:v1"

    with engine.connect() as conn:
        keys = _fetch_sqa_idempotency_keys_for_span(
            conn, evidence_span_id=span_id
        )
    assert keys == [expected_key]


# ===========================================================================
# 5) does NOT assess spans orphan from this task
# ===========================================================================
def test_orchestrator_does_not_assess_spans_orphan_from_task():
    """Two tasks within the SAME tenant + project, each with its own
    claim_evidence_link pointing to its own evidence_span. Running the
    orchestrator for task A MUST evaluate only span A — never span B.

    Correction 4 (8.7E micro-fix): the prior version created task_b
    under a different project and then inserted logical_claims with
    project_id from task_a's project, which was schema-inconsistent.
    This version keeps both tasks under the same tenant/project, so
    every logical_claims row uses a project_id that matches its
    task's project — and the scoping invariant is still verified
    purely by ``logical_claims.task_id``.
    """
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _ensure_tenant_and_user(conn)
        # Single project shared by task A and task B.
        project_id = _create_project(
            conn, tenant_id=tenant_id, name_suffix="orphan"
        )
        task_a = _create_task(
            conn, tenant_id=tenant_id, project_id=project_id, user_id=user_id
        )
        task_b = _create_task(
            conn, tenant_id=tenant_id, project_id=project_id, user_id=user_id
        )
        assert task_a != task_b

        # Two distinct evidence_spans.
        span_a = _create_evidence_span(
            conn, tenant_id=tenant_id, project_id=project_id, created_by=user_id
        )
        span_b = _create_evidence_span(
            conn, tenant_id=tenant_id, project_id=project_id, created_by=user_id
        )

        # Span A linked to a claim of task A.
        lc_a, le_a = _create_logical_claim_with_v1(
            conn, tenant_id=tenant_id, project_id=project_id, task_id=task_a
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_a,
            claim_ledger_entry_id=le_a,
            evidence_span_id=span_a,
        )

        # Span B linked to a claim of task B.
        lc_b, le_b = _create_logical_claim_with_v1(
            conn, tenant_id=tenant_id, project_id=project_id, task_id=task_b
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_b,
            claim_ledger_entry_id=le_b,
            evidence_span_id=span_b,
        )

    # Run for task A only.
    with engine.begin() as conn:
        result = run_source_quality_assessment(conn, task_id=task_a)

    assert result["status"] == "completed"
    assert result["spans_total"] == 1
    assert result["assessed_count"] == 1

    with engine.connect() as conn:
        # Only span A was assessed; span B remains untouched.
        assert _count_sqa_for_span(conn, evidence_span_id=span_a) == 1
        assert _count_sqa_for_span(conn, evidence_span_id=span_b) == 0


# ===========================================================================
# 6) does NOT mutate any other domain table; does NOT emit audit
# ===========================================================================
def test_orchestrator_does_not_mutate_other_domain_tables():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        span_id = _create_evidence_span(
            conn, tenant_id=tenant_id, project_id=project_id, created_by=user_id
        )
        lc, le = _create_logical_claim_with_v1(
            conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc,
            claim_ledger_entry_id=le,
            evidence_span_id=span_id,
        )

    snapshot_tables = (
        "claim_ledger_entries",
        "logical_claims",
        "verification_records",
        "final_gate_reports",
        "published_answers",
        "source_loss_events",
        "source_loss_propagation_records",
        "audit_records",
    )

    with engine.connect() as conn:
        before = {t: _table_count(conn, table=t) for t in snapshot_tables}

    with engine.begin() as conn:
        result = run_source_quality_assessment(conn, task_id=task_id)
    assert result["status"] == "completed"
    assert result["assessed_count"] == 1

    with engine.connect() as conn:
        after = {t: _table_count(conn, table=t) for t in snapshot_tables}

    for t in snapshot_tables:
        assert before[t] == after[t], (
            f"orchestrator must NOT mutate {t}: "
            f"before={before[t]} after={after[t]}"
        )


# ===========================================================================
# 7) unknown task_id -> status='not_found', no insert
# ===========================================================================
def test_orchestrator_unknown_task_returns_not_found():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.connect() as conn:
        before = _table_count(conn, table="source_quality_assessments")

    bogus_task_id = uuid.uuid4()
    with engine.begin() as conn:
        result = run_source_quality_assessment(conn, task_id=bogus_task_id)

    assert result["status"] == "not_found"
    assert result["spans_total"] == 0
    assert result["assessed_count"] == 0
    assert result["already_assessed_count"] == 0
    assert result["not_found_count"] == 0
    assert result["invalid_target_count"] == 0
    assert result["error_count"] == 0

    with engine.connect() as conn:
        after = _table_count(conn, table="source_quality_assessments")
    assert after == before
