"""Worker-level tests for apps/worker/app/services/claim_entailment_orchestrator.py
(Phase 8.8A — Block ORCHESTRATOR).

Coverage map (9 scenarios required by the block prompt §7):

  1. test_assesses_each_claim_evidence_pair_linked_to_task
  2. test_returns_zero_for_task_without_claim_evidence_links
  3. test_idempotent_on_redelivery
  4. test_deterministic_idempotency_key
  5. test_does_not_assess_pairs_orphan_from_task
  6. test_does_not_mutate_other_domain_tables
  7. test_unknown_task_returns_not_found
  8. test_duplicate_claim_evidence_links_are_deduplicated
  9. test_service_error_is_counted_not_raised

Design notes:

  - This file lives under apps/worker/tests/. The Python package
    ``app`` resolves to apps/worker/app, so we can import the
    orchestrator entry point and the worker DB helper directly
    without any sys.path tweaking.

  - We DO NOT spin up Redis, a worker loop, an API, or a
    dispatcher. The orchestrator runs in isolation against the
    real DB.

  - All helpers are LOCAL to this file (no imports from other
    test files, per the Phase 8.8A-ORCHESTRATOR prompt). Seed
    helpers mirror the patterns used in
    apps/worker/tests/test_claim_entailment_checker_service.py
    and apps/worker/tests/test_source_quality_orchestrator.py,
    and are NOT re-exported.

  - All identifiers, hashes, and quote texts are uuid.uuid4()-derived
    per invocation, so this file is safe to rerun against a
    long-lived dev DB.

  - The orchestrator MUST NOT touch any table other than
    claim_entailment_checks (via assess_claim_entailment) and
    MUST NOT emit audit. Scenario 6 asserts that explicitly via
    a pre/post snapshot of every relevant table.

  - The ``_table_count`` helper interpolates the table name into
    the SQL string (no bind params allowed for identifiers in
    SQLAlchemy text()). To stay safe against accidental injection
    via test typos, ``_table_count`` validates ``table`` against
    a whitelist of allowed table names (same pattern used by
    test_source_quality_orchestrator.py and
    test_claim_entailment_checker_service.py).

  - Per the block prompt, tests must be DB-real and must skip
    cleanly when DATABASE_URL is missing or the DB is unreachable.
    We expose ``_skip_if_db_unreachable()`` and call it at the
    start of every test.

  - Note on schema constraint cel_entry_span_uq (UNIQUE on
    claim_evidence_links.(claim_ledger_entry_id, evidence_span_id)
    declared in 0004): this UNIQUE makes it IMPOSSIBLE to insert
    two identical (entry, span) pairs via two distinct
    claim_evidence_links rows. The dedup test (scenario 8)
    therefore exercises the DISTINCT in the orchestrator's
    discovery query via a slightly different angle — see the
    test's docstring for the rationale.
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
from app.services import claim_entailment_orchestrator as orchestrator_module
from app.services.claim_entailment_orchestrator import (
    run_claim_entailment_checks,
)


# ---------------------------------------------------------------------------
# environment guard
# ---------------------------------------------------------------------------
def _skip_if_db_unreachable() -> None:
    """Skip the test if Postgres is not reachable.

    The worker test suite normally assumes ``make up`` has been
    run; we add this guard so a stray invocation in a stripped
    environment skips cleanly rather than crashing on connection
    errors.
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


# Whitelist of tables we are allowed to introspect via ``_table_count``.
# psycopg's text() cannot parameterize identifiers, so we interpolate
# the table name into the SQL string and validate it against this
# whitelist to keep the seam typo-safe and injection-safe. Same
# pattern used by other worker tests in this repo.
_ALLOWED_COUNT_TABLES: frozenset[str] = frozenset(
    {
        "claim_entailment_checks",
        "claim_ledger_entries",
        "logical_claims",
        "verification_records",
        "source_quality_assessments",
        "final_gate_reports",
        "published_answers",
        "audit_records",
    }
)


def _table_count(conn: Connection, *, table: str) -> int:
    if table not in _ALLOWED_COUNT_TABLES:
        raise ValueError(
            f"_table_count: table {table!r} is not in the allowed "
            f"whitelist {_ALLOWED_COUNT_TABLES!r}"
        )
    return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


# ---------------------------------------------------------------------------
# seed: tenant / user / project / task
# ---------------------------------------------------------------------------
def _ensure_tenant_and_user(
    conn: Connection,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Idempotently ensure the dev tenant + user; return (tenant_id,
    user_id)."""
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
                {
                    "t": tenant_id,
                    "n": f"cec-orch-test-{name_suffix}-{uuid.uuid4()}",
                },
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
        f"Claim entailment orchestrator test marker {marker}. "
        f"This sentence contains a {quote} for the test."
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
                    # Salt the content_hash to dodge the global UNIQUE
                    # (content_hash, hash_algorithm) WHERE
                    # tenant_namespace_id IS NULL on a long-running
                    # dev DB.
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
) -> uuid.UUID:
    """Insert one claim_evidence_links row connecting
    (logical, entry) to the given evidence_span.

    Honors ``cel_origin_xor``: ``evidence_span_id`` is non-null and
    ``retrieved_source_span_id`` is null. Returns the link id.
    """
    link_id = uuid.uuid4()
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
            "id": link_id,
            "lc": claim_logical_id,
            "le": claim_ledger_entry_id,
            "es": evidence_span_id,
        },
    )
    return link_id


# ---------------------------------------------------------------------------
# inspection helpers
# ---------------------------------------------------------------------------
def _count_checks_for_pair(
    conn: Connection,
    *,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
) -> int:
    return int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM claim_entailment_checks
                WHERE claim_ledger_entry_id = :e
                  AND evidence_span_id      = :s
                """
            ),
            {"e": claim_ledger_entry_id, "s": evidence_span_id},
        ).scalar_one()
    )


def _fetch_idempotency_key_for_pair(
    conn: Connection,
    *,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
) -> str:
    """Return the idempotency_key of the SINGLE row in
    claim_entailment_checks for the (entry, span) pair.

    Caller MUST have already verified that exactly one row exists.
    """
    row = conn.execute(
        text(
            """
            SELECT idempotency_key FROM claim_entailment_checks
            WHERE claim_ledger_entry_id = :e
              AND evidence_span_id      = :s
            """
        ),
        {"e": claim_ledger_entry_id, "s": evidence_span_id},
    ).one()
    return str(row[0])


# ===========================================================================
# 1) orchestrator assesses each (entry, span) pair linked to the task
# ===========================================================================
def test_assesses_each_claim_evidence_pair_linked_to_task():
    """Two distinct logical_claims, two distinct evidence_spans, two
    distinct claim_evidence_links — the orchestrator must produce
    two new rows in claim_entailment_checks.
    """
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        span_a = _create_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        span_b = _create_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        lc_a, le_a = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        lc_b, le_b = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
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
        result = run_claim_entailment_checks(conn, task_id=task_id)

    assert result["status"] == "completed"
    assert result["pairs_total"] == 2
    assert result["assessed_count"] == 2
    assert result["already_assessed_count"] == 0
    assert result["not_found_count"] == 0
    assert result["invalid_target_count"] == 0
    assert result["error_count"] == 0

    with engine.connect() as conn:
        assert (
            _count_checks_for_pair(
                conn,
                claim_ledger_entry_id=le_a,
                evidence_span_id=span_a,
            )
            == 1
        )
        assert (
            _count_checks_for_pair(
                conn,
                claim_ledger_entry_id=le_b,
                evidence_span_id=span_b,
            )
            == 1
        )


# ===========================================================================
# 2) task without claim_evidence_links -> zero pairs, no insert
# ===========================================================================
def test_returns_zero_for_task_without_claim_evidence_links():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)
        before = _table_count(conn, table="claim_entailment_checks")

    with engine.begin() as conn:
        result = run_claim_entailment_checks(conn, task_id=task_id)

    assert result["status"] == "completed"
    assert result["pairs_total"] == 0
    assert result["assessed_count"] == 0
    assert result["already_assessed_count"] == 0
    assert result["not_found_count"] == 0
    assert result["invalid_target_count"] == 0
    assert result["error_count"] == 0

    with engine.connect() as conn:
        after = _table_count(conn, table="claim_entailment_checks")
    assert after == before


# ===========================================================================
# 3) idempotent on redelivery
# ===========================================================================
def test_idempotent_on_redelivery():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        span_a = _create_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        span_b = _create_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        lc_a, le_a = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        lc_b, le_b = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
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
        result_1 = run_claim_entailment_checks(conn, task_id=task_id)
    assert result_1["status"] == "completed"
    assert result_1["pairs_total"] == 2
    assert result_1["assessed_count"] == 2
    assert result_1["already_assessed_count"] == 0

    # Second run with the SAME task: every pair collapses to
    # 'already_assessed' at the service level because the
    # idempotency_key is deterministic per (task, entry, span).
    with engine.begin() as conn:
        result_2 = run_claim_entailment_checks(conn, task_id=task_id)
    assert result_2["status"] == "completed"
    assert result_2["pairs_total"] == 2
    assert result_2["assessed_count"] == 0
    assert result_2["already_assessed_count"] == 2

    with engine.connect() as conn:
        # Row count per pair unchanged.
        assert (
            _count_checks_for_pair(
                conn,
                claim_ledger_entry_id=le_a,
                evidence_span_id=span_a,
            )
            == 1
        )
        assert (
            _count_checks_for_pair(
                conn,
                claim_ledger_entry_id=le_b,
                evidence_span_id=span_b,
            )
            == 1
        )


# ===========================================================================
# 4) deterministic idempotency_key format
# ===========================================================================
def test_deterministic_idempotency_key():
    """The orchestrator must store rows in claim_entailment_checks
    under the exact deterministic key format documented in
    PHASE_8_8A_PRE.md §7.2 and re-affirmed by the orchestrator's
    docstring:

        task:{task_id}:entry:{claim_ledger_entry_id}:span:{evidence_span_id}:v1

    A regression in this key format would break idempotency on
    redelivery (since the service identifies replays by this exact
    string).
    """
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        span_id = _create_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        lc, le = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc,
            claim_ledger_entry_id=le,
            evidence_span_id=span_id,
        )

    with engine.begin() as conn:
        result = run_claim_entailment_checks(conn, task_id=task_id)
    assert result["status"] == "completed"
    assert result["assessed_count"] == 1

    expected_key = f"task:{task_id}:entry:{le}:span:{span_id}:v1"

    with engine.connect() as conn:
        actual_key = _fetch_idempotency_key_for_pair(
            conn,
            claim_ledger_entry_id=le,
            evidence_span_id=span_id,
        )
    assert actual_key == expected_key


# ===========================================================================
# 5) does NOT assess pairs orphan from this task
# ===========================================================================
def test_does_not_assess_pairs_orphan_from_task():
    """Two tasks within the SAME tenant + project, each with its own
    claim_evidence_link pointing to its own evidence_span. Running
    the orchestrator for task A MUST evaluate only the pair of task
    A — never the pair of task B.

    Task A and task B live in the same project so that every
    logical_claim row has a project_id that matches its task's
    project (the orchestrator scopes by lc.task_id, not by
    project_id, so this is the right invariant to test).
    """
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _ensure_tenant_and_user(conn)
        project_id = _create_project(
            conn, tenant_id=tenant_id, name_suffix="orphan"
        )
        task_a = _create_task(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        task_b = _create_task(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        assert task_a != task_b

        # Two distinct evidence_spans.
        span_a = _create_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        span_b = _create_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )

        # Pair A on task A.
        lc_a, le_a = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_a,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_a,
            claim_ledger_entry_id=le_a,
            evidence_span_id=span_a,
        )

        # Pair B on task B.
        lc_b, le_b = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_b,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_b,
            claim_ledger_entry_id=le_b,
            evidence_span_id=span_b,
        )

    # Run for task A only.
    with engine.begin() as conn:
        result = run_claim_entailment_checks(conn, task_id=task_a)

    assert result["status"] == "completed"
    assert result["pairs_total"] == 1
    assert result["assessed_count"] == 1

    with engine.connect() as conn:
        # Only pair A was assessed; pair B remains untouched.
        assert (
            _count_checks_for_pair(
                conn,
                claim_ledger_entry_id=le_a,
                evidence_span_id=span_a,
            )
            == 1
        )
        assert (
            _count_checks_for_pair(
                conn,
                claim_ledger_entry_id=le_b,
                evidence_span_id=span_b,
            )
            == 0
        )


# ===========================================================================
# 6) does NOT mutate any other domain table; does NOT emit audit
# ===========================================================================
def test_does_not_mutate_other_domain_tables():
    """Snapshot count pre/post on every table the orchestrator must
    NEVER touch.

    The block prompt enumerates an explicit list of tables that must
    remain invariant across the call. audit_records is included
    because the orchestrator must not emit audit events; audit
    emission is the responsibility of the future 8.8A-WORKER block.
    """
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        span_id = _create_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        lc, le = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
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
        "source_quality_assessments",
        "final_gate_reports",
        "published_answers",
        "audit_records",
    )

    with engine.connect() as conn:
        before = {t: _table_count(conn, table=t) for t in snapshot_tables}

    with engine.begin() as conn:
        result = run_claim_entailment_checks(conn, task_id=task_id)
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
def test_unknown_task_returns_not_found():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.connect() as conn:
        before = _table_count(conn, table="claim_entailment_checks")

    bogus_task_id = uuid.uuid4()
    with engine.begin() as conn:
        result = run_claim_entailment_checks(conn, task_id=bogus_task_id)

    assert result["status"] == "not_found"
    assert result["pairs_total"] == 0
    assert result["assessed_count"] == 0
    assert result["already_assessed_count"] == 0
    assert result["not_found_count"] == 0
    assert result["invalid_target_count"] == 0
    assert result["error_count"] == 0

    with engine.connect() as conn:
        after = _table_count(conn, table="claim_entailment_checks")
    assert after == before


# ===========================================================================
# 8) duplicate claim_evidence_links are deduplicated
# ===========================================================================
def test_duplicate_claim_evidence_links_are_deduplicated():
    """Scenario 8 of the block prompt §7.

    Schema constraint cel_entry_span_uq (declared in
    migrations/0004_claim_ledger.sql) imposes
    ``UNIQUE (claim_ledger_entry_id, evidence_span_id)`` on
    claim_evidence_links. That UNIQUE makes it IMPOSSIBLE to insert
    two rows with identical (entry, span) pairs — even with different
    link_role values — so the "naive" version of this test would be
    rejected by the DB.

    The block prompt anticipates this and asks: "se schema ha
    unique che impedisce duplicati, documenta e adatta il test a
    deduplicare via due logical paths se possibile. L'obiettivo
    e' verificare SELECT DISTINCT / no doppia valutazione."

    Two logical paths converging on the same evidence_span:

      We create TWO distinct logical_claims for the same task, each
      with its own v1 entry. Both entries link to the SAME
      evidence_span via two distinct claim_evidence_links rows that
      DO have different (claim_ledger_entry_id, evidence_span_id)
      pairs (the entry id differs), so the UNIQUE is not violated.

      From the orchestrator's perspective these are TWO DIFFERENT
      pairs: (entry_a, span) and (entry_b, span). The orchestrator
      should evaluate both — once each. We assert:
        - pairs_total == 2 (the DISTINCT SELECT preserves both);
        - exactly two rows are inserted into claim_entailment_checks
          (one per pair);
        - no row is inserted twice for the SAME pair (which would
          violate cec_entry_span_idem_uq and surface as an
          IntegrityError if the orchestrator's DISTINCT clause were
          accidentally producing duplicates).

    This exercises the DISTINCT projection in the orchestrator's
    discovery query: a regression that collapsed the projection to
    just evidence_span_id would only see ONE pair (and
    assessed_count would drop to 1, which the test catches).
    """
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        span_id = _create_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        # Two distinct logical_claims + v1 entries, both linked to
        # the same evidence_span. Honors cel_entry_span_uq because
        # claim_ledger_entry_id differs between the two links.
        lc_a, le_a = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        lc_b, le_b = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        assert le_a != le_b
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_a,
            claim_ledger_entry_id=le_a,
            evidence_span_id=span_id,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_b,
            claim_ledger_entry_id=le_b,
            evidence_span_id=span_id,
        )

    with engine.begin() as conn:
        result = run_claim_entailment_checks(conn, task_id=task_id)

    assert result["status"] == "completed"
    # Two genuinely distinct pairs: (le_a, span) and (le_b, span).
    assert result["pairs_total"] == 2
    assert result["assessed_count"] == 2
    assert result["already_assessed_count"] == 0
    assert result["error_count"] == 0

    with engine.connect() as conn:
        # Each pair has exactly ONE row (the DISTINCT projection
        # did not double-count either pair).
        assert (
            _count_checks_for_pair(
                conn,
                claim_ledger_entry_id=le_a,
                evidence_span_id=span_id,
            )
            == 1
        )
        assert (
            _count_checks_for_pair(
                conn,
                claim_ledger_entry_id=le_b,
                evidence_span_id=span_id,
            )
            == 1
        )


# ===========================================================================
# 9) service error is counted, not raised
# ===========================================================================
def test_service_error_is_counted_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 9 of the block prompt §7.

    The block prompt requires that a service-level ``status='error'``
    response is aggregated into ``error_count`` and does NOT cause
    the orchestrator to raise.

    We monkeypatch the symbol ``assess_claim_entailment`` ON THE
    ORCHESTRATOR MODULE (not on the service module), because
    claim_entailment_orchestrator imports the function at module
    load time:

        from .claim_entailment_checker import assess_claim_entailment

    so the orchestrator's local name is what gets called. Patching
    the service module would leave the orchestrator's binding
    intact.

    The stub returns an ``error`` result and does NOT INSERT any
    row, so the test asserts both:
      - the orchestrator completes (does not raise);
      - error_count == pairs_total (every pair was an error);
      - assessed_count == 0;
      - no row was created in claim_entailment_checks (because the
        stub never inserts).
    """
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        span_id = _create_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        lc, le = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc,
            claim_ledger_entry_id=le,
            evidence_span_id=span_id,
        )
        before = _table_count(conn, table="claim_entailment_checks")

    def _stub_returns_error(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        """Stub: surface an 'error' status with the same shape the
        real service returns. Does NOT insert into
        claim_entailment_checks.
        """
        return {
            "status": "error",
            "assessment_id": None,
            "version_no": None,
            "verdict": None,
            "claim_ledger_entry_id": str(
                kwargs.get("claim_ledger_entry_id")
            ),
            "evidence_span_id": str(kwargs.get("evidence_span_id")),
            "tenant_id": None,
            "project_id": None,
            "task_id": None,
            "error_code": "entailment_version_conflict",
        }

    # Patch the orchestrator's local binding, NOT the service module.
    monkeypatch.setattr(
        orchestrator_module,
        "assess_claim_entailment",
        _stub_returns_error,
    )

    # The orchestrator must complete WITHOUT raising.
    with engine.begin() as conn:
        result = run_claim_entailment_checks(conn, task_id=task_id)

    assert result["status"] == "completed"
    assert result["pairs_total"] == 1
    assert result["assessed_count"] == 0
    assert result["already_assessed_count"] == 0
    assert result["not_found_count"] == 0
    assert result["invalid_target_count"] == 0
    assert result["error_count"] == 1

    with engine.connect() as conn:
        after = _table_count(conn, table="claim_entailment_checks")
    # Stub did not INSERT — table row count is unchanged.
    assert after == before
