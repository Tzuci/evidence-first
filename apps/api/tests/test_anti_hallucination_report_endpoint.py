"""API tests for the Anti-Hallucination Report endpoint (Phase 8.8B-REPORT-CODE-A + CODE-B).

Endpoint exercised:

  GET /api/v1/tasks/{task_id}/anti-hallucination-report

Coverage map:

  CODE-A (preserved verbatim — same 6 scenarios that shipped with CODE-A):
    1. test_get_anti_hallucination_report_returns_404_for_missing_task
    2. test_get_anti_hallucination_report_returns_not_ready_for_empty_task
    3. test_get_anti_hallucination_report_includes_gate_gaps_and_publication_held
    4. test_get_anti_hallucination_report_distinguishes_withdrawn_and_superseded
    5. test_get_anti_hallucination_report_is_read_only
    6. test_get_anti_hallucination_report_orders_coverage_gaps_severity_first

  CODE-B (this block — 6 new scenarios):
    7. test_get_anti_hallucination_report_includes_claim_evidence_source_quality_entailment
    8. test_get_anti_hallucination_report_uses_latest_source_quality_version
    9. test_get_anti_hallucination_report_uses_latest_entailment_version
   10. test_get_anti_hallucination_report_counts_missing_source_quality_and_entailment
   11. test_get_anti_hallucination_report_does_not_count_unlinked_document_spans_as_missing
   12. test_get_anti_hallucination_report_claims_and_evidence_ordering_is_deterministic

Design notes:
  - This file lives under apps/api/tests/. The Python package ``app``
    therefore resolves to apps/api/app, so ``from app.main import app``
    and ``from app.db import get_engine`` are the canonical imports.
  - We do NOT touch Redis: the endpoint is strictly read-only and does
    not call ``get_redis()``. No FakeRedis is needed.
  - We do NOT import any worker code. All rows are seeded directly via
    SQL — exactly the same pattern used by
    ``test_answers_endpoints.py``,
    ``test_source_quality_read_endpoint.py``, and
    ``test_claim_entailment_read_endpoint.py``.
  - Helpers are LOCAL to this file (per the block prompt: no imports
    from other test files).
  - Append-only tables accept INSERT — the shared
    ``reject_modify_append_only`` trigger only blocks UPDATE / DELETE.
  - All identifiers / hashes / idempotency keys are unique per
    invocation (rerun-safe).
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db import get_engine
from app.main import app


# ---------------------------------------------------------------------------
# environment guard
# ---------------------------------------------------------------------------
def _skip_if_db_unreachable() -> None:
    """Skip the test if Postgres is not reachable.

    Mirrors the gating used by every other API test module.
    """
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set; bring up the stack first.")
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip(
            "DB unreachable; run `make up` and `make migrate && make seed`."
        )


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------
def _unique_hex() -> str:
    """Return a rerun-safe sha256 hex string unique per call."""
    return hashlib.sha256(uuid.uuid4().bytes + uuid.uuid4().bytes).hexdigest()


def _err(resp_json: dict[str, Any]) -> dict[str, Any]:
    """Extract the normalized error envelope from a NormalizedError response.

    Envelope shape::

        {"error": {"code": "...", "message": "...", "details": {...}, ...}}
    """
    err = resp_json.get("error")
    assert err is not None, f"missing 'error' envelope in response: {resp_json}"
    assert isinstance(err, dict), f"'error' is not a dict: {err!r}"
    return err


def _endpoint(task_id: uuid.UUID) -> str:
    return f"/api/v1/tasks/{task_id}/anti-hallucination-report"


# ---------------------------------------------------------------------------
# DB seeding helpers — tenant / project / task
# ---------------------------------------------------------------------------
def _seed_tenant_user(conn: Connection) -> tuple[uuid.UUID, uuid.UUID]:
    """Ensure the (Dev, dev@local) tenant + user exist."""
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
        row = conn.execute(
            text("SELECT id FROM tenants WHERE slug = 'dev'")
        ).one()
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
                "SELECT id FROM users WHERE tenant_id = :t "
                "AND email = 'dev@local'"
            ),
            {"t": tenant_id},
        ).one()
    user_id = uuid.UUID(str(row[0]))
    return tenant_id, user_id


def _seed_project_and_task(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a fresh project and a task in status='created'."""
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
                {"t": tenant_id, "n": f"ahr-test-{uuid.uuid4()}"},
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
    return project_id, task_id


# ---------------------------------------------------------------------------
# DB seeding helpers — draft / gate / coverage gaps / published
# ---------------------------------------------------------------------------
def _seed_draft(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    summary_text: str = "",
    compiler_name: str = "mvp0_compiler_v1",
    compiler_version: str = "0.1.0",
) -> uuid.UUID:
    """Insert one draft_final_answers v1 for the task."""
    return uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO draft_final_answers
                        (id, task_id, version_no,
                         compiler_name, compiler_version, summary_text)
                    VALUES (:id, :t, 1,
                            :cn, :cv, :st)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": task_id,
                    "cn": compiler_name,
                    "cv": compiler_version,
                    "st": summary_text,
                },
            ).first()[0]
        )
    )


def _seed_gate_report(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    draft_id: uuid.UUID,
    decision: str,
    reason_code: str,
) -> uuid.UUID:
    """Insert one final_gate_reports row for the draft."""
    return uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO final_gate_reports
                        (id, task_id, draft_final_answer_id,
                         decision, reason_code)
                    VALUES (:id, :t, :d, :dec, :rc)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": task_id,
                    "d": draft_id,
                    "dec": decision,
                    "rc": reason_code,
                },
            ).first()[0]
        )
    )


def _seed_coverage_gap(
    conn: Connection,
    *,
    draft_id: uuid.UUID,
    kind: str,
    severity: str,
    gap_key: str,
    details_json: str = '{"reason":"seeded for test"}',
    created_at_sql: str | None = None,
) -> uuid.UUID:
    """Insert one coverage_gap_statements row.

    SECURITY note on ``created_at_sql``:
      This argument is interpolated VERBATIM into the SQL string. It
      exists ONLY so the ordering test can produce rows with
      controlled, distinct ``created_at`` values (e.g.
      ``NOW() - interval '1 hour'``). All callers in this file pass
      TRUSTED constant strings; no test-user input reaches this
      argument.
    """
    if created_at_sql is None:
        sql = text(
            """
            INSERT INTO coverage_gap_statements
                (id, draft_final_answer_id, kind, severity, gap_key, details)
            VALUES (:id, :d, :k, :sev, :gk, CAST(:dt AS JSONB))
            RETURNING id
            """
        )
    else:
        sql = text(
            f"""
            INSERT INTO coverage_gap_statements
                (id, draft_final_answer_id, kind, severity, gap_key,
                 details, created_at)
            VALUES (:id, :d, :k, :sev, :gk, CAST(:dt AS JSONB),
                    {created_at_sql})
            RETURNING id
            """
        )
    return uuid.UUID(
        str(
            conn.execute(
                sql,
                {
                    "id": uuid.uuid4(),
                    "d": draft_id,
                    "k": kind,
                    "sev": severity,
                    "gk": gap_key,
                    "dt": details_json,
                },
            ).first()[0]
        )
    )


def _seed_published_answer(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    draft_id: uuid.UUID,
    gate_report_id: uuid.UUID,
    summary_text: str,
    status: str = "published",
) -> uuid.UUID:
    """Insert one published_answers v1 row."""
    content_hash = hashlib.sha256(summary_text.encode("utf-8")).hexdigest()
    return uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO published_answers
                        (id, task_id, draft_final_answer_id,
                         final_gate_report_id,
                         version_no, content_hash, status)
                    VALUES (:id, :t, :d, :g, 1, :h, :st)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": task_id,
                    "d": draft_id,
                    "g": gate_report_id,
                    "h": content_hash,
                    "st": status,
                },
            ).first()[0]
        )
    )


# ---------------------------------------------------------------------------
# DB seeding helpers — evidence_span chain (CODE-B)
# ---------------------------------------------------------------------------
# Mirrors the helper used by test_source_quality_read_endpoint.py and
# test_claim_entailment_read_endpoint.py, but additionally inserts a
# task_documents row so the report's task-level evidence query sees
# the span. Kept LOCAL to this file (no cross-file imports).
def _create_evidence_span_chain(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    task_document_position: int = 0,
) -> dict[str, Any]:
    """Create the full storage chain ending in an evidence_spans row.

    Order of inserts (to honor every FK and the storage_blobs unique
    partial index sb_global_uq):
      storage_blobs -> storage_objects -> uploaded_documents
        -> document_versions (kind='parsed') -> document_chunks
        -> evidence_spans
      [-> task_documents if task_id is given]

    Returns a dict with the chain ids.
    """
    marker = uuid.uuid4().hex[:12]
    quote = f"quotable span {marker}"
    chunk_text = (
        f"Anti-hallucination report CODE-B test marker {marker}. "
        f"This sentence contains the digit 7 and a {quote}."
    )
    content_hash_payload = hashlib.sha256(
        chunk_text.encode("utf-8")
    ).hexdigest()
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
                    # Make the content_hash unique per invocation so
                    # the global UNIQUE (content_hash, hash_algorithm)
                    # WHERE tenant_namespace_id IS NULL never collides
                    # on a long-running dev DB.
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
                    "u": user_id,
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
                    "th": hashlib.sha256(
                        chunk_text.encode("utf-8")
                    ).hexdigest(),
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
                    "th": hashlib.sha256(
                        chunk_text.encode("utf-8")
                    ).hexdigest(),
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

    if task_id is not None:
        conn.execute(
            text(
                """
                INSERT INTO task_documents
                    (task_id, document_id, role, position)
                VALUES
                    (:tid, :did, 'source', :pos)
                ON CONFLICT (task_id, document_id) DO NOTHING
                """
            ),
            {
                "tid": task_id,
                "did": document_id,
                "pos": task_document_position,
            },
        )

    return {
        "document_id": document_id,
        "document_version_id": document_version_id,
        "document_chunk_id": document_chunk_id,
        "evidence_span_id": evidence_span_id,
        "quote": quote,
    }


# ---------------------------------------------------------------------------
# DB seeding helpers — logical_claims + claim_ledger_entries + link
# ---------------------------------------------------------------------------
def _create_logical_claim_with_verified_entry(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    canonical_text: str | None = None,
    created_at_sql: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create one logical_claims row plus a v1 ledger entry in state
    'verified_fact'. Returns ``(claim_logical_id, claim_ledger_entry_id)``.

    SECURITY note on ``created_at_sql``: same as
    ``_seed_coverage_gap`` — the value is interpolated VERBATIM into
    the SQL string. Only TRUSTED constant strings are passed by
    callers in this file.
    """
    canonical_claim_text = (
        canonical_text if canonical_text is not None else f"canonical-{uuid.uuid4()}"
    )

    if created_at_sql is None:
        lc_sql = text(
            """
            INSERT INTO logical_claims
                (id, tenant_id, project_id, task_id,
                 canonical_claim_text, canonical_claim_hash)
            VALUES (:id, :t, :p, :tid, :ct, :ch)
            RETURNING id
            """
        )
    else:
        lc_sql = text(
            f"""
            INSERT INTO logical_claims
                (id, tenant_id, project_id, task_id,
                 canonical_claim_text, canonical_claim_hash, created_at)
            VALUES (:id, :t, :p, :tid, :ct, :ch, {created_at_sql})
            RETURNING id
            """
        )

    claim_logical_id = uuid.UUID(
        str(
            conn.execute(
                lc_sql,
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
    link_role: str = "primary_support",
) -> uuid.UUID:
    """Insert one claim_evidence_links row binding a claim's ledger
    entry to an evidence_span.
    """
    new_id = uuid.uuid4()
    conn.execute(
        text(
            """
            INSERT INTO claim_evidence_links
                (id, claim_logical_id, claim_ledger_entry_id,
                 evidence_span_id, retrieved_source_span_id, link_role)
            VALUES
                (:id, :lc, :cle, :es, NULL, :role)
            """
        ),
        {
            "id": new_id,
            "lc": claim_logical_id,
            "cle": claim_ledger_entry_id,
            "es": evidence_span_id,
            "role": link_role,
        },
    )
    return new_id


# ---------------------------------------------------------------------------
# DB seeding helpers — verification_records (CVE-lite)
# ---------------------------------------------------------------------------
def _insert_cve_lite_record(
    conn: Connection,
    *,
    claim_logical_id: uuid.UUID,
    claim_ledger_entry_id: uuid.UUID,
    outcome: str = "pass",
    check_name: str = "quote_hash_and_substring_v1",
) -> uuid.UUID:
    """Insert one verification_records row of kind 'cve_lite'.

    The UNIQUE constraint on (claim_ledger_entry_id, check_kind,
    check_name) means at most one CVE-lite row per entry per
    check_name. All tests here use the mock check_name so no
    collision occurs across scenarios that target different ledger
    entries.
    """
    return uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO verification_records (
                        id, claim_logical_id, claim_ledger_entry_id,
                        check_kind, check_name, outcome, score,
                        evaluator_id, payload
                    ) VALUES (
                        :id, :lc, :cle,
                        'cve_lite', :cn, :outcome, NULL,
                        'mvp0_cve_lite_v1', '{}'::jsonb
                    )
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "lc": claim_logical_id,
                    "cle": claim_ledger_entry_id,
                    "cn": check_name,
                    "outcome": outcome,
                },
            ).first()[0]
        )
    )


# ---------------------------------------------------------------------------
# DB seeding helpers — source_quality_assessments
# ---------------------------------------------------------------------------
def _insert_source_quality_assessment(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    evidence_span_id: uuid.UUID,
    version_no: int = 1,
    overall_quality: str = "unknown",
    contradiction_status: str = "unchecked",
    confidence: float | None = 0.5,
    idempotency_key: str | None = None,
    evaluator_name: str = "mock_source_quality_evaluator",
    payload: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Insert ONE source_quality_assessments row directly via SQL.

    Defaults mirror the MVP-0 mock evaluator. Tests override
    individual fields where needed.
    """
    eff_payload = payload if payload is not None else {"mock": True}
    eff_key = idempotency_key if idempotency_key is not None else _unique_hex()
    return uuid.UUID(
        str(
            conn.execute(
                text(
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
                        :tenant_id, :project_id,
                        :evidence_span_id, NULL, NULL,
                        :version_no,
                        'user_document', 'unclear', 'unknown', 'unknown',
                        'undated', 'direct_support', 'exact_quote_match',
                        :contradiction_status,
                        :overall_quality, :confidence,
                        :evaluator_name, '0.1.0',
                        'mvp0_mock_source_quality', '0.1.0',
                        :idempotency_key, CAST(:payload AS JSONB)
                    )
                    RETURNING id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "evidence_span_id": evidence_span_id,
                    "version_no": version_no,
                    "contradiction_status": contradiction_status,
                    "overall_quality": overall_quality,
                    "confidence": confidence,
                    "evaluator_name": evaluator_name,
                    "idempotency_key": eff_key,
                    "payload": json.dumps(eff_payload, sort_keys=True),
                },
            ).first()[0]
        )
    )


# ---------------------------------------------------------------------------
# DB seeding helpers — claim_entailment_checks
# ---------------------------------------------------------------------------
def _insert_claim_entailment_check(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID,
    claim_logical_id: uuid.UUID,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
    version_no: int = 1,
    verdict: str = "entailed",
    confidence: float | None = 0.8,
    checker_name: str = "mvp0_mock_entailment_checker",
    idempotency_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Insert ONE claim_entailment_checks row directly via SQL."""
    eff_payload = payload if payload is not None else {"mock": True}
    eff_key = idempotency_key if idempotency_key is not None else _unique_hex()
    return uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO claim_entailment_checks (
                        tenant_id, project_id, task_id,
                        claim_logical_id, claim_ledger_entry_id, evidence_span_id,
                        version_no,
                        verdict, confidence,
                        checker_name, checker_version,
                        policy_name, policy_version,
                        idempotency_key, rationale, payload
                    ) VALUES (
                        :tenant_id, :project_id, :task_id,
                        :claim_logical_id, :claim_ledger_entry_id, :evidence_span_id,
                        :version_no,
                        :verdict, :confidence,
                        :checker_name, '0.1.0',
                        'mvp0_mock_entailment', '0.1.0',
                        :idempotency_key, NULL, CAST(:payload AS JSONB)
                    )
                    RETURNING id
                    """
                ),
                {
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
                    "idempotency_key": eff_key,
                    "payload": json.dumps(eff_payload, sort_keys=True),
                },
            ).first()[0]
        )
    )


# ---------------------------------------------------------------------------
# DB inspection helpers (read-only test)
# ---------------------------------------------------------------------------
_COUNTABLE_TABLES = frozenset(
    {
        "audit_records",
        "claim_ledger_entries",
        "source_quality_assessments",
        "claim_entailment_checks",
        "final_gate_reports",
        "coverage_gap_statements",
        "published_answers",
    }
)


def _count_table(conn: Connection, table_name: str) -> int:
    """Return a global COUNT(*) of the named table.

    Only accepts a hardcoded whitelist of table names to keep the SQL
    construction safe.
    """
    if table_name not in _COUNTABLE_TABLES:
        raise ValueError(f"refusing to count unknown table: {table_name!r}")
    return int(
        conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
    )


def _snapshot_all_counts(conn: Connection) -> dict[str, int]:
    return {t: _count_table(conn, t) for t in _COUNTABLE_TABLES}


# ===========================================================================
# CODE-A tests (preserved verbatim)
# ===========================================================================

# 1 — 404 for missing task
# ===========================================================================
def test_get_anti_hallucination_report_returns_404_for_missing_task() -> None:
    _skip_if_db_unreachable()

    client = TestClient(app)
    bogus = uuid.uuid4()
    resp = client.get(_endpoint(bogus))

    assert resp.status_code == 404, resp.text
    err = _err(resp.json())
    assert err["code"] == "RESOURCE_NOT_FOUND"
    details = err.get("details") or {}
    assert details.get("resource") == "task_masters"
    assert details.get("id") == str(bogus)


# 2 — Task exists but empty: not_ready, claims/evidence empty, zero counters
# ===========================================================================
def test_get_anti_hallucination_report_returns_not_ready_for_empty_task() -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Top-level identity.
    assert body["task_id"] == str(task_id)
    assert body["project_id"] == str(project_id)
    assert body["tenant_id"] == str(tenant_id)

    # task metadata.
    assert body["task"]["status"] == "created"
    assert body["task"]["mode"] == "closed_corpus"

    # No draft -> no gate -> no published -> "not_ready".
    assert body["publication"]["status"] == "not_ready"
    assert body["publication"]["published_answer_id"] is None
    assert body["publication"]["published_answer_status"] is None
    assert body["publication"]["final_gate_report_id"] is None

    assert body["gate"]["decision"] is None
    assert body["gate"]["reason_code"] is None
    assert body["gate"]["payload"] == {}
    assert body["gate"]["coverage_gaps"] == []

    # CODE-B: claims/evidence are now lists. Empty for this scenario:
    # no logical_claims and no task_documents seeded.
    assert body["claims"] == []
    assert body["evidence"] == []

    # axis_summary: final_gate derived from gaps (zero), others zeroed
    # (no claim-linked spans/pairs exist for this task).
    fg = body["axis_summary"]["final_gate"]
    assert fg["has_blocking_gaps"] is False
    assert fg["has_warnings"] is False
    assert fg["blocking_gap_count"] == 0
    assert fg["warning_gap_count"] == 0

    cve = body["axis_summary"]["cve_lite"]
    assert cve == {
        "verified_claims_count": 0,
        "unverified_claims_count": 0,
        "inconclusive_count": 0,
    }
    sq = body["axis_summary"]["source_quality"]
    assert sq == {
        "strong_count": 0,
        "adequate_count": 0,
        "weak_count": 0,
        "unsuitable_count": 0,
        "unknown_count": 0,
        "missing_count": 0,
    }
    ce = body["axis_summary"]["claim_entailment"]
    assert ce == {
        "entailed_count": 0,
        "partially_supported_count": 0,
        "not_supported_count": 0,
        "contradicted_count": 0,
        "uncertain_count": 0,
        "missing_count": 0,
    }

    # mock_indicators always present.
    mi = body["mock_indicators"]
    assert mi["uses_mock_source_quality"] is True
    assert mi["uses_mock_claim_entailment"] is True
    assert mi["uses_mock_compiler"] is True  # fallback when no draft
    assert mi["uses_mock_cve_lite"] is True
    assert isinstance(mi["notes"], list) and len(mi["notes"]) >= 4

    # limitations always present.
    assert isinstance(body["limitations"], list)
    assert len(body["limitations"]) >= 4


# 3 — Gate rejected with gaps + no published -> publication_held
# ===========================================================================
def test_get_anti_hallucination_report_includes_gate_gaps_and_publication_held() -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        _project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )
        draft_id = _seed_draft(conn, task_id=task_id, summary_text="")
        gate_id = _seed_gate_report(
            conn,
            task_id=task_id,
            draft_id=draft_id,
            decision="rejected",
            reason_code="no_verified_claims",
        )
        # Two gaps so we can count both blocking and warning buckets.
        gap_block_id = _seed_coverage_gap(
            conn,
            draft_id=draft_id,
            kind="missing_evidence",
            severity="block",
            gap_key="no_verified_claims",
        )
        gap_warn_id = _seed_coverage_gap(
            conn,
            draft_id=draft_id,
            kind="source_quality_warning",
            severity="warn",
            gap_key=f"span:{uuid.uuid4()}:source_quality_warning",
        )

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # publication_held: no published_answers + gate rejected.
    assert body["publication"]["status"] == "publication_held"
    assert body["publication"]["published_answer_id"] is None

    # gate echoes the rejected decision and the seeded reason_code.
    assert body["gate"]["decision"] == "rejected"
    assert body["gate"]["reason_code"] == "no_verified_claims"

    gaps = body["gate"]["coverage_gaps"]
    assert isinstance(gaps, list) and len(gaps) == 2

    ids_by_kind = {g["kind"]: g["id"] for g in gaps}
    assert ids_by_kind["missing_evidence"] == str(gap_block_id)
    assert ids_by_kind["source_quality_warning"] == str(gap_warn_id)

    # axis decoration is applied per gap.
    axis_by_kind = {g["kind"]: g["axis"] for g in gaps}
    assert axis_by_kind["missing_evidence"] == "coverage"
    assert axis_by_kind["source_quality_warning"] == "source_quality"

    # Severity-first ordering: block before warn.
    assert gaps[0]["severity"] == "block"
    assert gaps[1]["severity"] == "warn"

    fg = body["axis_summary"]["final_gate"]
    assert fg["blocking_gap_count"] == 1
    assert fg["warning_gap_count"] == 1
    assert fg["has_blocking_gaps"] is True
    assert fg["has_warnings"] is True

    # final_gate_report_id is referenced in publication only when a
    # published_answer exists. With no published_answer, it is None.
    assert body["publication"]["final_gate_report_id"] is None

    # mock_indicators: draft uses the mock compiler -> True.
    assert body["mock_indicators"]["uses_mock_compiler"] is True


# 4 — published with status='withdrawn' / 'superseded' are NOT flattened
# ===========================================================================
def _seed_approved_published_with_status(status: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed approved draft + gate + published_answer with the given status.

    Returns (task_id, published_answer_id).
    """
    summary_text = f"smoke-{uuid.uuid4()}"
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        _project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )
        draft_id = _seed_draft(
            conn, task_id=task_id, summary_text=summary_text
        )
        gate_id = _seed_gate_report(
            conn,
            task_id=task_id,
            draft_id=draft_id,
            decision="approved",
            reason_code="all_spans_verified",
        )
        pa_id = _seed_published_answer(
            conn,
            task_id=task_id,
            draft_id=draft_id,
            gate_report_id=gate_id,
            summary_text=summary_text,
            status=status,
        )
    return task_id, pa_id


def test_get_anti_hallucination_report_distinguishes_withdrawn_and_superseded() -> None:
    _skip_if_db_unreachable()

    client = TestClient(app)

    # withdrawn case.
    task_w, pa_w = _seed_approved_published_with_status("withdrawn")
    resp_w = client.get(_endpoint(task_w))
    assert resp_w.status_code == 200, resp_w.text
    body_w = resp_w.json()
    assert body_w["publication"]["status"] == "withdrawn"
    assert body_w["publication"]["published_answer_status"] == "withdrawn"
    assert body_w["publication"]["published_answer_id"] == str(pa_w)
    # Crucially: NOT flattened to "published".
    assert body_w["publication"]["status"] != "published"

    # superseded case.
    task_s, pa_s = _seed_approved_published_with_status("superseded")
    resp_s = client.get(_endpoint(task_s))
    assert resp_s.status_code == 200, resp_s.text
    body_s = resp_s.json()
    assert body_s["publication"]["status"] == "superseded"
    assert body_s["publication"]["published_answer_status"] == "superseded"
    assert body_s["publication"]["published_answer_id"] == str(pa_s)
    assert body_s["publication"]["status"] != "published"


# 5 — read-only: count snapshot invariant
# ===========================================================================
def test_get_anti_hallucination_report_is_read_only() -> None:
    """The endpoint MUST NOT mutate any DB row. Snapshot counts on all
    append-only / report-relevant tables AFTER seeding, hit the
    endpoint several times (happy path + 404 + repeated call), assert
    the snapshot is identical.
    """
    _skip_if_db_unreachable()

    # Seed a task with a draft + rejected gate + one block gap so the
    # endpoint exercises the heaviest read path it has in CODE-A.
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        _project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )
        draft_id = _seed_draft(conn, task_id=task_id, summary_text="")
        _seed_gate_report(
            conn,
            task_id=task_id,
            draft_id=draft_id,
            decision="rejected",
            reason_code="no_verified_claims",
        )
        _seed_coverage_gap(
            conn,
            draft_id=draft_id,
            kind="missing_evidence",
            severity="block",
            gap_key="no_verified_claims",
        )

    # Snapshot AFTER all seed transactions commit.
    with engine.connect() as conn:
        before = _snapshot_all_counts(conn)

    client = TestClient(app)

    # Happy path.
    r_ok = client.get(_endpoint(task_id))
    assert r_ok.status_code == 200, r_ok.text

    # 404 path: must also be free of side effects.
    r_404 = client.get(_endpoint(uuid.uuid4()))
    assert r_404.status_code == 404, r_404.text

    # Repeat the happy path to make sure the second GET does not
    # produce idempotent inserts behind the scenes.
    r_ok2 = client.get(_endpoint(task_id))
    assert r_ok2.status_code == 200, r_ok2.text

    with engine.connect() as conn:
        after = _snapshot_all_counts(conn)

    assert after == before, (
        "row counts drifted after read-only GETs; "
        f"before={before!r}, after={after!r}"
    )


# 6 — severity-first ordering of coverage_gaps
# ===========================================================================
def test_get_anti_hallucination_report_orders_coverage_gaps_severity_first() -> None:
    """Insert three gaps in a non-severity-first creation order
    (warn, block, then a second warn with an EARLIER created_at) and
    verify the endpoint returns them ordered:
      - block first;
      - then warn rows, in created_at ASC, then id ASC.
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        _project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )
        draft_id = _seed_draft(conn, task_id=task_id, summary_text="")
        _seed_gate_report(
            conn,
            task_id=task_id,
            draft_id=draft_id,
            decision="rejected",
            reason_code="no_verified_claims",
        )

        # Seeded in a deliberately non-severity-first creation order.
        warn_late_id = _seed_coverage_gap(
            conn,
            draft_id=draft_id,
            kind="source_quality_warning",
            severity="warn",
            gap_key=f"span:{uuid.uuid4()}:source_quality_warning",
            created_at_sql="NOW() - interval '30 minutes'",
        )
        block_id = _seed_coverage_gap(
            conn,
            draft_id=draft_id,
            kind="missing_evidence",
            severity="block",
            gap_key="no_verified_claims",
            created_at_sql="NOW() - interval '15 minutes'",
        )
        warn_early_id = _seed_coverage_gap(
            conn,
            draft_id=draft_id,
            kind="entailment_warning",
            severity="warn",
            gap_key=f"span:{uuid.uuid4()}:entailment_warning",
            created_at_sql="NOW() - interval '2 hours'",
        )

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))
    assert resp.status_code == 200, resp.text
    gaps = resp.json()["gate"]["coverage_gaps"]
    assert len(gaps) == 3

    # block first.
    assert gaps[0]["severity"] == "block"
    assert gaps[0]["id"] == str(block_id)

    # then warn rows in created_at ASC order: warn_early (-2h) before
    # warn_late (-30m).
    assert gaps[1]["severity"] == "warn"
    assert gaps[2]["severity"] == "warn"
    assert gaps[1]["id"] == str(warn_early_id)
    assert gaps[2]["id"] == str(warn_late_id)


# ===========================================================================
# CODE-B tests (new in 8.8B-REPORT-CODE-B)
# ===========================================================================

# 7 — full happy path: claim + evidence + CVE-lite + SQ + CE
# ===========================================================================
def test_get_anti_hallucination_report_includes_claim_evidence_source_quality_entailment() -> None:
    """End-to-end CODE-B happy path.

    Seed:
      - task + project + tenant + user;
      - one document linked to the task with one evidence_span;
      - one logical_claim + verified-fact ledger entry;
      - one claim_evidence_links connecting the entry to the span;
      - one CVE-lite verification_record (outcome='pass');
      - one source_quality_assessment on that span (overall_quality
        defaults to 'unknown', the MVP-0 mock value);
      - one claim_entailment_check on the (entry, span) pair
        (verdict='entailed').

    Assert the report:
      - has claims with length >= 1, each carrying evidence_links,
        cve_lite, source_quality, entailment populated;
      - has evidence with length >= 1 mirroring the seeded span;
      - source_quality.latest_assessment_id matches the seeded row;
      - entailment.latest_check_id matches the seeded row;
      - axis_summary counters match the seeded data.
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            task_id=task_id,
        )
        lc_id, cle_id = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_id,
            claim_ledger_entry_id=cle_id,
            evidence_span_id=chain["evidence_span_id"],
        )
        cve_id = _insert_cve_lite_record(
            conn,
            claim_logical_id=lc_id,
            claim_ledger_entry_id=cle_id,
            outcome="pass",
        )
        sq_id = _insert_source_quality_assessment(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=1,
            overall_quality="unknown",
        )
        ce_id = _insert_claim_entailment_check(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            claim_logical_id=lc_id,
            claim_ledger_entry_id=cle_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=1,
            verdict="entailed",
            confidence=0.8,
        )

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # claims
    claims = body["claims"]
    assert isinstance(claims, list) and len(claims) >= 1
    # Find the matching claim by logical_claim_id.
    claim_item = next(
        (c for c in claims if c["logical_claim_id"] == str(lc_id)), None
    )
    assert claim_item is not None, f"missing logical_claim {lc_id} in claims"

    assert claim_item["latest_entry_id"] == str(cle_id)
    assert claim_item["latest_state"] == "verified_fact"
    assert claim_item["support_scope"] == "supported_by_user_corpus_only"

    # evidence_links
    links = claim_item["evidence_links"]
    assert isinstance(links, list) and len(links) == 1
    assert links[0]["evidence_span_id"] == str(chain["evidence_span_id"])
    assert links[0]["link_role"] == "primary_support"

    # cve_lite
    cve_items = claim_item["cve_lite"]
    assert isinstance(cve_items, list) and len(cve_items) == 1
    assert cve_items[0]["verification_record_id"] == str(cve_id)
    assert cve_items[0]["outcome"] == "pass"
    assert cve_items[0]["check_name"] == "quote_hash_and_substring_v1"

    # source_quality
    sq_items = claim_item["source_quality"]
    assert isinstance(sq_items, list) and len(sq_items) == 1
    sq_item = sq_items[0]
    assert sq_item["evidence_span_id"] == str(chain["evidence_span_id"])
    assert sq_item["latest_assessment_id"] == str(sq_id)
    assert sq_item["overall_quality"] == "unknown"
    assert sq_item["evaluator_name"] == "mock_source_quality_evaluator"
    # mock detection via payload.mock=True
    assert sq_item["mock"] is True

    # entailment
    ce_items = claim_item["entailment"]
    assert isinstance(ce_items, list) and len(ce_items) == 1
    ce_item = ce_items[0]
    assert ce_item["claim_ledger_entry_id"] == str(cle_id)
    assert ce_item["evidence_span_id"] == str(chain["evidence_span_id"])
    assert ce_item["latest_check_id"] == str(ce_id)
    assert ce_item["verdict"] == "entailed"
    assert ce_item["confidence"] == pytest.approx(0.8)
    assert ce_item["checker_name"] == "mvp0_mock_entailment_checker"
    assert ce_item["mock"] is True

    # evidence[] non vuoto
    evidence = body["evidence"]
    assert isinstance(evidence, list) and len(evidence) >= 1
    ev_ids = [e["evidence_span_id"] for e in evidence]
    assert str(chain["evidence_span_id"]) in ev_ids
    seeded_ev = next(
        (e for e in evidence
         if e["evidence_span_id"] == str(chain["evidence_span_id"])),
        None,
    )
    assert seeded_ev is not None
    assert seeded_ev["quote"] == chain["quote"]
    assert seeded_ev["document_id"] == str(chain["document_id"])
    assert seeded_ev["document_filename"]  # non-empty

    # axis_summary
    axis = body["axis_summary"]
    assert axis["cve_lite"]["verified_claims_count"] == 1
    assert axis["cve_lite"]["unverified_claims_count"] == 0
    assert axis["cve_lite"]["inconclusive_count"] == 0

    assert axis["source_quality"]["unknown_count"] == 1
    assert axis["source_quality"]["missing_count"] == 0
    assert axis["source_quality"]["strong_count"] == 0
    assert axis["source_quality"]["unsuitable_count"] == 0

    assert axis["claim_entailment"]["entailed_count"] == 1
    assert axis["claim_entailment"]["missing_count"] == 0
    assert axis["claim_entailment"]["contradicted_count"] == 0


# 8 — latest source_quality version wins
# ===========================================================================
def test_get_anti_hallucination_report_uses_latest_source_quality_version() -> None:
    """Insert two source_quality_assessments rows for the same span,
    v1 (overall_quality='weak') and v2 (overall_quality='adequate').

    The report MUST surface the latest (v2) on the claim's
    ``source_quality`` slot AND count it as ``adequate`` (not as
    ``weak``) in the axis summary.
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            task_id=task_id,
        )
        lc_id, cle_id = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_id,
            claim_ledger_entry_id=cle_id,
            evidence_span_id=chain["evidence_span_id"],
        )
        # Two versions for the same span: latest wins.
        _v1_id = _insert_source_quality_assessment(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=1,
            overall_quality="weak",
        )
    with engine.begin() as conn:
        v2_id = _insert_source_quality_assessment(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=2,
            overall_quality="adequate",
        )

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    claim_item = next(
        c for c in body["claims"] if c["logical_claim_id"] == str(lc_id)
    )
    sq_items = claim_item["source_quality"]
    assert len(sq_items) == 1
    assert sq_items[0]["latest_assessment_id"] == str(v2_id)
    assert sq_items[0]["overall_quality"] == "adequate"

    # axis_summary mirrors the LATEST per span only.
    axis_sq = body["axis_summary"]["source_quality"]
    assert axis_sq["adequate_count"] == 1
    assert axis_sq["weak_count"] == 0
    assert axis_sq["missing_count"] == 0


# 9 — latest entailment version wins
# ===========================================================================
def test_get_anti_hallucination_report_uses_latest_entailment_version() -> None:
    """Insert two claim_entailment_checks rows for the same (entry,
    span) pair, v1 (verdict='uncertain') and v2 (verdict='entailed').

    The report MUST surface the latest (v2) on the claim's
    ``entailment`` slot AND count it as ``entailed`` (not as
    ``uncertain``) in the axis summary.
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            task_id=task_id,
        )
        lc_id, cle_id = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_id,
            claim_ledger_entry_id=cle_id,
            evidence_span_id=chain["evidence_span_id"],
        )
        _v1_id = _insert_claim_entailment_check(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            claim_logical_id=lc_id,
            claim_ledger_entry_id=cle_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=1,
            verdict="uncertain",
            confidence=0.5,
        )
    with engine.begin() as conn:
        v2_id = _insert_claim_entailment_check(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            claim_logical_id=lc_id,
            claim_ledger_entry_id=cle_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=2,
            verdict="entailed",
            confidence=0.9,
        )

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    claim_item = next(
        c for c in body["claims"] if c["logical_claim_id"] == str(lc_id)
    )
    ce_items = claim_item["entailment"]
    assert len(ce_items) == 1
    assert ce_items[0]["latest_check_id"] == str(v2_id)
    assert ce_items[0]["verdict"] == "entailed"
    assert ce_items[0]["confidence"] == pytest.approx(0.9)

    axis_ce = body["axis_summary"]["claim_entailment"]
    assert axis_ce["entailed_count"] == 1
    assert axis_ce["uncertain_count"] == 0
    assert axis_ce["missing_count"] == 0


# 10 — missing SQ and CE produce null slots and increment missing_count
# ===========================================================================
def test_get_anti_hallucination_report_counts_missing_source_quality_and_entailment() -> None:
    """Seed claim + evidence + link, but DO NOT insert any
    source_quality_assessment or claim_entailment_check.

    Expectations:
      - claim's source_quality slot has latest_assessment_id=null;
      - claim's entailment slot has latest_check_id=null;
      - axis_summary.source_quality.missing_count == 1;
      - axis_summary.claim_entailment.missing_count == 1.
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            task_id=task_id,
        )
        lc_id, cle_id = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_id,
            claim_ledger_entry_id=cle_id,
            evidence_span_id=chain["evidence_span_id"],
        )

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    claim_item = next(
        c for c in body["claims"] if c["logical_claim_id"] == str(lc_id)
    )

    sq_items = claim_item["source_quality"]
    assert len(sq_items) == 1
    assert sq_items[0]["evidence_span_id"] == str(chain["evidence_span_id"])
    assert sq_items[0]["latest_assessment_id"] is None
    assert sq_items[0]["overall_quality"] is None
    assert sq_items[0]["mock"] is None

    ce_items = claim_item["entailment"]
    assert len(ce_items) == 1
    assert ce_items[0]["claim_ledger_entry_id"] == str(cle_id)
    assert ce_items[0]["evidence_span_id"] == str(chain["evidence_span_id"])
    assert ce_items[0]["latest_check_id"] is None
    assert ce_items[0]["verdict"] is None
    assert ce_items[0]["confidence"] is None
    assert ce_items[0]["mock"] is None

    axis_sq = body["axis_summary"]["source_quality"]
    assert axis_sq["missing_count"] == 1
    # No "present" counters should be incremented.
    for k in ("strong_count", "adequate_count", "weak_count",
              "unsuitable_count", "unknown_count"):
        assert axis_sq[k] == 0

    axis_ce = body["axis_summary"]["claim_entailment"]
    assert axis_ce["missing_count"] == 1
    for k in ("entailed_count", "partially_supported_count",
              "not_supported_count", "contradicted_count",
              "uncertain_count"):
        assert axis_ce[k] == 0


# 11 — unlinked document spans must NOT count as missing
# ===========================================================================
def test_get_anti_hallucination_report_does_not_count_unlinked_document_spans_as_missing() -> None:
    """Seed a task with TWO evidence_spans attached via task_documents
    but link only ONE of them to a claim. Do NOT insert any SQ/CE.

    Expectations:
      - evidence[] surfaces BOTH spans (every span linked to the task
        via task_documents must appear);
      - axis_summary.source_quality.missing_count == 1 (only the
        linked span counts as missing; the unlinked one is ignored
        by the claim-axis aggregation);
      - axis_summary.claim_entailment.missing_count == 1 (same).
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )
        chain_linked = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            task_id=task_id,
            task_document_position=0,
        )
        chain_unlinked = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            task_id=task_id,
            task_document_position=1,
        )
        lc_id, cle_id = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        # Only one of the two spans is linked to the claim.
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_id,
            claim_ledger_entry_id=cle_id,
            evidence_span_id=chain_linked["evidence_span_id"],
        )

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # evidence[] surfaces BOTH spans (task_documents-driven).
    ev_ids = {e["evidence_span_id"] for e in body["evidence"]}
    assert str(chain_linked["evidence_span_id"]) in ev_ids
    assert str(chain_unlinked["evidence_span_id"]) in ev_ids
    assert len(body["evidence"]) >= 2

    # axis_summary: only the linked span counts as missing on each
    # claim-level axis.
    axis_sq = body["axis_summary"]["source_quality"]
    assert axis_sq["missing_count"] == 1

    axis_ce = body["axis_summary"]["claim_entailment"]
    assert axis_ce["missing_count"] == 1

    # And the claim's per-span slot list has length 1 too.
    claim_item = next(
        c for c in body["claims"] if c["logical_claim_id"] == str(lc_id)
    )
    assert len(claim_item["source_quality"]) == 1
    assert len(claim_item["entailment"]) == 1
    assert (
        claim_item["source_quality"][0]["evidence_span_id"]
        == str(chain_linked["evidence_span_id"])
    )


# 12 — deterministic ordering of claims and evidence
# ===========================================================================
def test_get_anti_hallucination_report_claims_and_evidence_ordering_is_deterministic() -> None:
    """Seed multiple logical_claims and multiple evidence_spans with
    controlled created_at values and verify the report's ordering.

    Ordering rules (PHASE_8_8B_REPORT_PRE.md §5.1, §11):
      - claims by ``logical_claims.created_at ASC, id ASC``;
      - evidence by ``document_id ASC, chunk_index ASC, char_start
        ASC, evidence_spans.id ASC``.

    We anchor the claim ordering via controlled created_at timestamps:
    earliest claim first.
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )
        # Three evidence spans, each in its own document. SQL ordering
        # on (document_id ASC, ...) sorts by UUID lex order — to make
        # the assertion stable we capture the IDs and order them in
        # Python by UUID-string, NOT by insertion order.
        chain_a = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            task_id=task_id,
            task_document_position=0,
        )
        chain_b = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            task_id=task_id,
            task_document_position=1,
        )
        chain_c = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            task_id=task_id,
            task_document_position=2,
        )

        # Three logical_claims with controlled created_at: oldest ->
        # newest = lc_old, lc_mid, lc_new. The report MUST surface
        # them in that order.
        lc_old_id, _cle_old = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            canonical_text="oldest claim",
            created_at_sql="NOW() - interval '3 hours'",
        )
        lc_mid_id, _cle_mid = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            canonical_text="middle claim",
            created_at_sql="NOW() - interval '2 hours'",
        )
        lc_new_id, _cle_new = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            canonical_text="newest claim",
            created_at_sql="NOW() - interval '1 hour'",
        )

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # claims ordering: oldest -> newest.
    claim_lids = [c["logical_claim_id"] for c in body["claims"]]
    assert claim_lids == [str(lc_old_id), str(lc_mid_id), str(lc_new_id)], (
        f"unexpected claim ordering: {claim_lids!r}"
    )

    # evidence ordering: document_id ASC (lexicographic UUID) ->
    # chunk_index ASC -> char_start ASC -> id ASC. Each chain has one
    # chunk and one span, so we sort the seeded chains by document_id
    # in Python and expect that order in the response.
    seeded_doc_ids = sorted(
        [str(chain_a["document_id"]),
         str(chain_b["document_id"]),
         str(chain_c["document_id"])]
    )
    response_doc_ids = [e["document_id"] for e in body["evidence"]]
    assert response_doc_ids == seeded_doc_ids, (
        f"unexpected evidence ordering: {response_doc_ids!r} "
        f"(expected {seeded_doc_ids!r})"
    )
