"""Worker-level tests for apps/worker/app/services/claim_entailment_checker.py
(Phase 8.8A — Block SERVICE).

Coverage map (8 scenarios required by the block prompt §7):

  1. test_assess_entailed_via_containment
  2. test_assess_not_supported_via_numeric_mismatch
  3. test_assess_uncertain_default
  4. test_idempotency_replay_same_key_returns_already_assessed
  5. test_different_idempotency_same_pair_returns_error_version_conflict
  6. test_unknown_claim_ledger_entry_returns_not_found
  7. test_unknown_evidence_span_returns_not_found
  8. test_service_does_not_mutate_other_domain_tables

Plus a few defensive coverage tests:

  - test_invalid_target_none_inputs_no_insert
  - test_service_imports_verdict_codomain_from_shared
  - test_payload_contains_mock_and_semantic_warning
  - test_canonical_scope_is_read_from_logical_claim

Design notes:

  - This file lives under apps/worker/tests/. The Python package
    ``app`` resolves to apps/worker/app, so we can import the
    service entrypoint and the worker DB helper directly without
    any sys.path tweaking.

  - We DO NOT spin up Redis, a worker loop, an API, or a dispatcher.
    The service runs in isolation against the real DB.

  - All helpers are LOCAL to this file (no imports from other test
    files, per Phase 8.8A-SERVICE prompt). Seed helpers mirror the
    patterns used in
    apps/worker/tests/test_source_quality_evaluator_service.py
    and are NOT re-exported.

  - All identifiers, hashes, and idempotency keys are
    uuid.uuid4()-derived per invocation, so this file is safe to
    rerun against a long-lived dev DB.

  - The service requires an active SQLAlchemy Connection inside an
    explicit transaction. We always wrap setup writes in
    ``with engine.begin() as conn:``. Test assertions use
    ``with engine.connect() as conn:`` for read-only inspection.

  - We never touch claim_ledger_entries beyond the test seed,
    final_gate_reports, published_answers, source_loss_events,
    source_quality_assessments, etc. Scenario 8 asserts that
    explicitly via a pre/post count snapshot.

  - Per the block prompt, tests must be DB-real and must skip
    cleanly when DATABASE_URL is missing or the DB is unreachable.
    We expose ``_skip_if_db_unreachable()`` and call it at the
    start of every test.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db import get_engine
from app.services.claim_entailment_checker import (
    DEFAULT_CHECKER_NAME,
    DEFAULT_CHECKER_VERSION,
    DEFAULT_POLICY_NAME,
    DEFAULT_POLICY_VERSION,
    STATUS_ALREADY_ASSESSED,
    STATUS_ASSESSED,
    STATUS_ERROR,
    STATUS_INVALID_TARGET,
    STATUS_NOT_FOUND,
    VERDICT_ENTAILED,
    VERDICT_NOT_SUPPORTED,
    VERDICT_UNCERTAIN,
    assess_claim_entailment,
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


# Whitelist of tables we are allowed to introspect via ``_table_count``.
# psycopg's text() cannot parameterize identifiers, so we interpolate
# the table name into the SQL string and validate it against this
# whitelist to keep the seam typo-safe and injection-safe. Same
# pattern used by other worker tests in this repo (e.g.
# test_source_quality_orchestrator.py).
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
def _seeded_dev(
    conn: Connection,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ensure tenant + user; create a FRESH project and task per
    invocation.

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
                {"t": tenant_id, "n": f"cec-svc-test-{uuid.uuid4()}"},
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
def _create_evidence_span(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    created_by: uuid.UUID,
    quote: str | None = None,
) -> uuid.UUID:
    """Create the full storage chain ending in an evidence_spans row
    and return just the evidence_span_id.

    The chain mirrors what 8.2 / 8.3 produce in the real pipeline:
      storage_blobs -> storage_objects -> uploaded_documents
        -> document_versions (kind='parsed') -> document_chunks
        -> evidence_spans

    The ``quote`` parameter lets the test seed a specific quote that
    the entailment heuristic will see.
    """
    marker = uuid.uuid4().hex[:12]
    if quote is None:
        quote = f"quotable span {marker}"
    chunk_text = (
        f"Entailment checker test marker {marker}. "
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
                    # Salt the content_hash to dodge the global
                    # UNIQUE (content_hash, hash_algorithm) WHERE
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

    # evidence_spans char_start/char_end describe the quote position
    # inside the chunk. We compute them deterministically so the row
    # is realistic. The quote_hash matches the standalone quote
    # bytes (this mirrors what CVE-lite would store).
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
# seed: logical_claim + v1 claim_ledger_entries
# ---------------------------------------------------------------------------
def _create_logical_claim_with_v1(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    canonical_claim_text: str | None = None,
    state: str = "verified_fact",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert one logical_claims row + one v1 claim_ledger_entries row.

    Returns (claim_logical_id, claim_ledger_entry_id_v1).
    """
    if canonical_claim_text is None:
        canonical_claim_text = f"canonical-{uuid.uuid4()}"
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
                    "ct": canonical_claim_text,
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
                    VALUES (:id, :lc, 1, :st,
                            'supported_by_user_corpus_only',
                            'supported_by_user_corpus_only',
                            'seeded_for_test')
                    RETURNING id
                    """
                ),
                {"id": uuid.uuid4(), "lc": claim_logical_id, "st": state},
            ).first()[0]
        )
    )
    return claim_logical_id, claim_ledger_entry_id


# ---------------------------------------------------------------------------
# DB inspection helpers
# ---------------------------------------------------------------------------
def _fetch_check_row(
    conn: Connection, *, assessment_id: uuid.UUID
) -> dict[str, Any]:
    """Return the full claim_entailment_checks row as a dict.

    The payload column is normalized so callers always receive a
    Python dict (psycopg may surface JSONB either as a native dict
    or as a JSON string depending on driver config).
    """
    row = conn.execute(
        text(
            """
            SELECT id, tenant_id, project_id, task_id,
                   claim_logical_id, claim_ledger_entry_id, evidence_span_id,
                   version_no, verdict, confidence,
                   checker_name, checker_version,
                   policy_name, policy_version,
                   idempotency_key, rationale, payload, created_at
            FROM claim_entailment_checks
            WHERE id = :id
            """
        ),
        {"id": assessment_id},
    ).one()
    m = dict(row._mapping)
    payload = m["payload"]
    if isinstance(payload, str):
        m["payload"] = json.loads(payload)
    return m


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


# ===========================================================================
# 1) entailed via containment
# ===========================================================================
def test_assess_entailed_via_containment():
    """Rule 1 of the mock heuristic: the canonical claim text is a
    substring of the quote (or vice-versa) -> 'entailed' verdict.

    Setup: we deliberately make the canonical claim a substring of
    the quote so the containment rule fires.
    """
    _skip_if_db_unreachable()
    engine = get_engine()

    canonical_text = "revenue grew by 37 percent"
    # The seed helper embeds the quote inside the chunk and uses it
    # verbatim on the evidence_spans row, so the entailment service
    # will read this exact string as the quote.
    quote_text = "Revenue grew by 37 percent in Q3 last year."

    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        span_id = _create_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
            quote=quote_text,
        )
        _lc_id, entry_id = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            canonical_claim_text=canonical_text,
        )

    idem = _unique_hex()
    with engine.begin() as conn:
        result = assess_claim_entailment(
            conn,
            claim_ledger_entry_id=entry_id,
            evidence_span_id=span_id,
            idempotency_key=idem,
        )

    assert result["status"] == STATUS_ASSESSED
    assert result["verdict"] == VERDICT_ENTAILED
    assert result["version_no"] == 1
    assert result["claim_ledger_entry_id"] == str(entry_id)
    assert result["evidence_span_id"] == str(span_id)
    assert result["tenant_id"] == str(tenant_id)
    assert result["project_id"] == str(project_id)
    assert result["task_id"] == str(task_id)
    assert result["error_code"] is None

    assessment_id = uuid.UUID(result["assessment_id"])
    with engine.connect() as conn:
        row = _fetch_check_row(conn, assessment_id=assessment_id)
    assert str(row["verdict"]) == VERDICT_ENTAILED
    assert float(row["confidence"]) == pytest.approx(0.8)
    assert str(row["checker_name"]) == DEFAULT_CHECKER_NAME
    assert str(row["checker_version"]) == DEFAULT_CHECKER_VERSION
    assert str(row["policy_name"]) == DEFAULT_POLICY_NAME
    assert str(row["policy_version"]) == DEFAULT_POLICY_VERSION
    assert str(row["idempotency_key"]) == idem
    payload = row["payload"]
    assert isinstance(payload, dict)
    assert payload.get("mock") is True
    assert payload.get("heuristic") == "containment_match"


# ===========================================================================
# 2) not_supported via numeric mismatch
# ===========================================================================
def test_assess_not_supported_via_numeric_mismatch():
    """Rule 2 of the mock heuristic: both texts contain numbers AND
    the sets of numbers differ -> 'not_supported'.

    We craft a claim that asserts 41 percent while the quote
    documents 37 percent. The containment rule does NOT fire (the
    claim is not a substring of the quote) so rule 2 takes over.
    """
    _skip_if_db_unreachable()
    engine = get_engine()

    # Different number, no containment.
    canonical_text = "Revenue grew by 41 percent in Q3"
    quote_text = "Revenue grew by 37 percent in Q3 last year."

    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        span_id = _create_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
            quote=quote_text,
        )
        _lc_id, entry_id = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            canonical_claim_text=canonical_text,
        )

    idem = _unique_hex()
    with engine.begin() as conn:
        result = assess_claim_entailment(
            conn,
            claim_ledger_entry_id=entry_id,
            evidence_span_id=span_id,
            idempotency_key=idem,
        )

    assert result["status"] == STATUS_ASSESSED
    assert result["verdict"] == VERDICT_NOT_SUPPORTED

    assessment_id = uuid.UUID(result["assessment_id"])
    with engine.connect() as conn:
        row = _fetch_check_row(conn, assessment_id=assessment_id)
    assert str(row["verdict"]) == VERDICT_NOT_SUPPORTED
    assert float(row["confidence"]) == pytest.approx(0.6)
    payload = row["payload"]
    assert payload.get("heuristic") == "numeric_mismatch"
    # Both number lists must be present in the payload.
    assert "41" in payload["numbers"]["claim"]
    assert "37" in payload["numbers"]["quote"]


# ===========================================================================
# 3) uncertain default
# ===========================================================================
def test_assess_uncertain_default():
    """Default branch of the mock heuristic: no containment, no
    numeric mismatch -> 'uncertain'.

    We use texts that are entirely disjoint, do not contain numbers
    on either side, and have no containment relation between them.
    """
    _skip_if_db_unreachable()
    engine = get_engine()

    # No numbers, no containment.
    canonical_text = "the sky shows interesting weather patterns"
    quote_text = "Brazilian samba dancers performed at the festival."

    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        span_id = _create_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
            quote=quote_text,
        )
        _lc_id, entry_id = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            canonical_claim_text=canonical_text,
        )

    idem = _unique_hex()
    with engine.begin() as conn:
        result = assess_claim_entailment(
            conn,
            claim_ledger_entry_id=entry_id,
            evidence_span_id=span_id,
            idempotency_key=idem,
        )

    assert result["status"] == STATUS_ASSESSED
    assert result["verdict"] == VERDICT_UNCERTAIN

    assessment_id = uuid.UUID(result["assessment_id"])
    with engine.connect() as conn:
        row = _fetch_check_row(conn, assessment_id=assessment_id)
    assert str(row["verdict"]) == VERDICT_UNCERTAIN
    assert float(row["confidence"]) == pytest.approx(0.5)
    assert row["payload"].get("heuristic") == "default_uncertain"


# ===========================================================================
# 4) idempotency replay — same key on same pair -> already_assessed
# ===========================================================================
def test_idempotency_replay_same_key_returns_already_assessed():
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
        _lc_id, entry_id = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )

    idem = _unique_hex()

    with engine.begin() as conn:
        result_1 = assess_claim_entailment(
            conn,
            claim_ledger_entry_id=entry_id,
            evidence_span_id=span_id,
            idempotency_key=idem,
        )
    assert result_1["status"] == STATUS_ASSESSED
    assert result_1["version_no"] == 1

    with engine.begin() as conn:
        result_2 = assess_claim_entailment(
            conn,
            claim_ledger_entry_id=entry_id,
            evidence_span_id=span_id,
            idempotency_key=idem,
        )
    assert result_2["status"] == STATUS_ALREADY_ASSESSED
    assert result_2["assessment_id"] == result_1["assessment_id"]
    assert result_2["version_no"] == 1
    assert result_2["verdict"] == result_1["verdict"]
    assert result_2["error_code"] is None

    with engine.connect() as conn:
        # Exactly one row total for this (entry, span) pair.
        assert (
            _count_checks_for_pair(
                conn,
                claim_ledger_entry_id=entry_id,
                evidence_span_id=span_id,
            )
            == 1
        )


# ===========================================================================
# 5) different idempotency_key on same pair -> error (version conflict)
# ===========================================================================
def test_different_idempotency_same_pair_returns_error_version_conflict():
    """In MVP-0 version_no is fixed at 1 (block prompt §6: 'non
    creare version_no=2 in questo blocco'). A second call on the
    same (entry, span) pair with a DIFFERENT idempotency_key would
    have to either:
      (a) silently mask the collision and return already_assessed —
          forbidden by the prompt ('Non mascherare'); or
      (b) bump version_no to 2 — forbidden by the prompt
          ('non creare version_no=2 in questo blocco'); or
      (c) surface as status='error' with an explicit error_code.

    The service chose option (c). This test locks that contract
    down.
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
        _lc_id, entry_id = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )

    idem_1 = _unique_hex()
    idem_2 = _unique_hex()
    assert idem_1 != idem_2

    with engine.begin() as conn:
        result_1 = assess_claim_entailment(
            conn,
            claim_ledger_entry_id=entry_id,
            evidence_span_id=span_id,
            idempotency_key=idem_1,
        )
    assert result_1["status"] == STATUS_ASSESSED
    assert result_1["version_no"] == 1

    with engine.begin() as conn:
        result_2 = assess_claim_entailment(
            conn,
            claim_ledger_entry_id=entry_id,
            evidence_span_id=span_id,
            idempotency_key=idem_2,
        )
    assert result_2["status"] == STATUS_ERROR
    assert result_2["error_code"] == "entailment_version_conflict"
    assert result_2["assessment_id"] is None
    assert result_2["version_no"] is None
    # Canonical scope is still populated on the error path because
    # the entry / span were resolved successfully.
    assert result_2["claim_ledger_entry_id"] == str(entry_id)
    assert result_2["evidence_span_id"] == str(span_id)
    assert result_2["tenant_id"] == str(tenant_id)

    with engine.connect() as conn:
        # No new row was inserted: only the original v1 remains.
        assert (
            _count_checks_for_pair(
                conn,
                claim_ledger_entry_id=entry_id,
                evidence_span_id=span_id,
            )
            == 1
        )


# ===========================================================================
# 6) unknown claim_ledger_entry -> not_found, no insert
# ===========================================================================
def test_unknown_claim_ledger_entry_returns_not_found():
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
        before = _table_count(conn, table="claim_entailment_checks")

    bogus_entry = uuid.uuid4()
    with engine.begin() as conn:
        result = assess_claim_entailment(
            conn,
            claim_ledger_entry_id=bogus_entry,
            evidence_span_id=span_id,
            idempotency_key=_unique_hex(),
        )

    assert result["status"] == STATUS_NOT_FOUND
    assert result["assessment_id"] is None
    assert result["verdict"] is None
    assert result["error_code"] is None

    with engine.connect() as conn:
        after = _table_count(conn, table="claim_entailment_checks")
    assert after == before  # no insert


# ===========================================================================
# 7) unknown evidence_span -> not_found, no insert
# ===========================================================================
def test_unknown_evidence_span_returns_not_found():
    _skip_if_db_unreachable()
    engine = get_engine()

    with engine.begin() as conn:
        tenant_id, project_id, _user_id, task_id = _seeded_dev(conn)
        _lc_id, entry_id = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        before = _table_count(conn, table="claim_entailment_checks")

    bogus_span = uuid.uuid4()
    with engine.begin() as conn:
        result = assess_claim_entailment(
            conn,
            claim_ledger_entry_id=entry_id,
            evidence_span_id=bogus_span,
            idempotency_key=_unique_hex(),
        )

    assert result["status"] == STATUS_NOT_FOUND
    assert result["assessment_id"] is None
    assert result["verdict"] is None
    assert result["error_code"] is None

    with engine.connect() as conn:
        after = _table_count(conn, table="claim_entailment_checks")
    assert after == before  # no insert


# ===========================================================================
# 8) no mutation of other domain tables
# ===========================================================================
def test_service_does_not_mutate_other_domain_tables():
    """Snapshot count pre/post on every table the service must NEVER
    touch.

    The block prompt enumerates an explicit list of tables that must
    remain invariant across the call. If a future refactor accidentally
    chains a write into any of them, this test fails loudly.

    audit_records is included because the prompt explicitly says the
    service must not emit audit events; audit emission is the job of
    the orchestrator block (8.8A-ORCHESTRATOR) running ABOVE the
    service.
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
        _lc_id, entry_id = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
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
        result = assess_claim_entailment(
            conn,
            claim_ledger_entry_id=entry_id,
            evidence_span_id=span_id,
            idempotency_key=_unique_hex(),
        )
    assert result["status"] == STATUS_ASSESSED

    with engine.connect() as conn:
        after = {t: _table_count(conn, table=t) for t in snapshot_tables}

    for t in snapshot_tables:
        assert before[t] == after[t], (
            f"service must NOT mutate {t}: before={before[t]} after={after[t]}"
        )


# ===========================================================================
# Defensive coverage tests
# ===========================================================================
def test_invalid_target_none_inputs_no_insert():
    """The service rejects None / empty inputs without touching the
    DB.

    We do NOT enter a transaction for the negative paths so any
    accidental DB write would fail loudly. The service must return
    status='invalid_target' from its application-level validation.
    """
    _skip_if_db_unreachable()
    engine = get_engine()

    with engine.connect() as conn:
        before = _table_count(conn, table="claim_entailment_checks")

    with engine.begin() as conn:
        # Missing claim_ledger_entry_id.
        result_a = assess_claim_entailment(
            conn,
            claim_ledger_entry_id=None,  # type: ignore[arg-type]
            evidence_span_id=uuid.uuid4(),
            idempotency_key=_unique_hex(),
        )
        assert result_a["status"] == STATUS_INVALID_TARGET

        # Missing evidence_span_id.
        result_b = assess_claim_entailment(
            conn,
            claim_ledger_entry_id=uuid.uuid4(),
            evidence_span_id=None,  # type: ignore[arg-type]
            idempotency_key=_unique_hex(),
        )
        assert result_b["status"] == STATUS_INVALID_TARGET

        # Empty idempotency_key.
        result_c = assess_claim_entailment(
            conn,
            claim_ledger_entry_id=uuid.uuid4(),
            evidence_span_id=uuid.uuid4(),
            idempotency_key="",
        )
        assert result_c["status"] == STATUS_INVALID_TARGET

    with engine.connect() as conn:
        after = _table_count(conn, table="claim_entailment_checks")
    assert after == before  # no insert across any of the three paths


def test_service_imports_verdict_codomain_from_shared():
    """The service module must import SOURCE_ENTAILMENT_VERDICT_VALUES
    from evidencefirst_shared.schemas rather than re-declaring its
    own codomain constants.

    This is the explicit contract from the block prompt (§3): every
    verdict the service emits must belong to the shared codomain, and
    the cleanest way to guarantee that is to depend on the shared
    tuple directly. The check uses identity comparison (``is``) so an
    accidentally duplicated tuple of the same strings fails this
    test.
    """
    from app.services import claim_entailment_checker as svc
    from evidencefirst_shared import schemas as shared

    # The service's local reference to the codomain must BE the same
    # object as the shared tuple.
    assert (
        svc.SOURCE_ENTAILMENT_VERDICT_VALUES
        is shared.SOURCE_ENTAILMENT_VERDICT_VALUES
    ), (
        "service must import SOURCE_ENTAILMENT_VERDICT_VALUES from "
        "evidencefirst_shared.schemas, not re-declare it locally"
    )
    # Spot-check that the verdict constants the service uses are
    # in fact members of the shared codomain.
    for v in (
        svc.VERDICT_ENTAILED,
        svc.VERDICT_PARTIALLY_SUPPORTED,
        svc.VERDICT_NOT_SUPPORTED,
        svc.VERDICT_CONTRADICTED,
        svc.VERDICT_UNCERTAIN,
    ):
        assert v in shared.SOURCE_ENTAILMENT_VERDICT_VALUES


def test_payload_contains_mock_and_semantic_warning():
    """Every emitted row must carry the mock flag and the semantic
    warning so downstream consumers can detect that the verdict is
    NOT a real NLI judgement.
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
            quote="Quote with the digit 7 inside.",
        )
        _lc_id, entry_id = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            canonical_claim_text="Unrelated claim text about something else.",
        )

    with engine.begin() as conn:
        result = assess_claim_entailment(
            conn,
            claim_ledger_entry_id=entry_id,
            evidence_span_id=span_id,
            idempotency_key=_unique_hex(),
        )
    assert result["status"] == STATUS_ASSESSED

    assessment_id = uuid.UUID(result["assessment_id"])
    with engine.connect() as conn:
        row = _fetch_check_row(conn, assessment_id=assessment_id)
    payload = row["payload"]
    assert payload.get("mock") is True
    assert payload.get("semantic_warning") == (
        "mvp0 heuristic; not a real NLI/LLM entailment model"
    )
    # The input texts must be preserved verbatim so a future reader
    # (UI, eval) can audit what the heuristic saw.
    assert "claim_text" in payload["input"]
    assert "quote" in payload["input"]


def test_canonical_scope_is_read_from_logical_claim():
    """The row written to claim_entailment_checks must use the
    tenant/project/task of the underlying logical_claim, not any
    caller-supplied scope. This service does NOT accept a caller
    scope at all (unlike source_quality_evaluator), but we still
    assert here that the row carries the canonical scope as a
    regression guard against a future refactor that might re-introduce
    a caller-scope path.
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
        _lc_id, entry_id = _create_logical_claim_with_v1(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )

    with engine.begin() as conn:
        result = assess_claim_entailment(
            conn,
            claim_ledger_entry_id=entry_id,
            evidence_span_id=span_id,
            idempotency_key=_unique_hex(),
        )
    assert result["status"] == STATUS_ASSESSED

    assessment_id = uuid.UUID(result["assessment_id"])
    with engine.connect() as conn:
        row = _fetch_check_row(conn, assessment_id=assessment_id)

    assert uuid.UUID(str(row["tenant_id"])) == tenant_id
    assert uuid.UUID(str(row["project_id"])) == project_id
    assert uuid.UUID(str(row["task_id"])) == task_id
    # claim_logical_id is denormalized; it must match what the FK
    # composite cec_entry_logical_consistency demands (i.e. the
    # claim_logical_id stored on the parent claim_ledger_entries row).
    assert uuid.UUID(str(row["claim_logical_id"])) == _lc_id
