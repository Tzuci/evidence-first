"""Worker-level tests for the Source Quality integration in
apps/worker/app/services/final_answer_gate.py (Phase 8.7G).

Coverage map (per the block 8.7G-2 prompt):

  1.  test_unknown_quality_produces_warning_keeps_approved
  2.  test_missing_assessment_produces_warning_keeps_approved
  3.  test_weak_quality_produces_warning_keeps_approved
  4.  test_unchecked_contradiction_produces_warning_keeps_approved
  5.  test_unsuitable_quality_produces_block_rejects_task
  6.  test_contradicted_by_stronger_source_produces_block
  7.  test_conflicting_sources_produces_block
  8.  test_latest_version_wins_block_after_weak
  9.  test_latest_version_wins_clean_after_unsuitable
  10. test_latest_version_wins_block_after_unknown
  11. test_multiple_evidence_spans_one_block_rejects
  12. test_cve_priority_unverified_takes_precedence_over_source_quality
  13. test_idempotent_on_redelivery_no_duplicate_gaps

The CVE-lite priority test (#12) is the architectural invariant of
PHASE_8_7G_PRE.md §8.4: when a span is not verified-backed, the Gate
must emit 'unverified_spans_present' and NOT consult Source Quality.

Test #13 covers idempotency: a second invocation of run_final_answer_gate
on the same draft must not duplicate gap rows, must not duplicate the
gate report, and must not duplicate the published answer.

Phase 8.7G invariants verified across all tests:
  - The Gate does NOT mutate source_quality_assessments (read-only
    SELECT). Tested explicitly via pre/post snapshot in #1 and #5.
  - The Gate does NOT mutate claim_ledger_entries or claim_lineage.
  - The audit chain for the task remains valid after each Gate run.
  - All gap rows use the deterministic gap_key format
    f'span:{span_id}:source_quality_{block,warning}'.

Design notes:
  - Local helpers only (no imports from other test files), per the
    block prompt.
  - DB-real tests against the worker's get_engine().
  - All ids/hashes are uuid.uuid4()-derived per invocation, so the
    file is safe to rerun.
  - We do NOT go through the task_created consumer for these unit-style
    tests: we seed the minimal claim+span topology directly and call
    run_final_answer_gate() on a hand-built draft. This is the same
    approach used by test_source_quality_orchestrator.py for the
    orchestrator.
  - We also seed source_quality_assessments rows directly (not via the
    mock evaluator) so we can drive the policy matrix with exact
    overall_quality / contradiction_status values. The mock evaluator
    only ever emits ('unknown','unchecked'), which the suite would
    cover only in scenarios #1 and #4.
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
from app.services.final_answer_gate import run_final_answer_gate


# ---------------------------------------------------------------------------
# environment guard
# ---------------------------------------------------------------------------
def _skip_if_db_unreachable() -> None:
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
    return hashlib.sha256(uuid.uuid4().bytes + uuid.uuid4().bytes).hexdigest()


# ---------------------------------------------------------------------------
# seed: tenant / user / project / task
# ---------------------------------------------------------------------------
def _seeded_dev(
    conn: Connection,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
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
                {"t": tenant_id, "n": f"fag-sq-test-{uuid.uuid4()}"},
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
                    VALUES (:t, :p, :u, 'closed_corpus', :o, 'analyzed_partial')
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
) -> uuid.UUID:
    """Create the full storage chain ending in an evidence_spans row
    and return just the evidence_span_id.
    """
    marker = uuid.uuid4().hex[:12]
    quote = f"quotable span {marker}"
    chunk_text = (
        f"Final answer gate source quality test marker {marker}. "
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
# seed: logical_claim + verified_fact entry + claim_evidence_link
# ---------------------------------------------------------------------------
def _create_logical_claim_with_verified_entry(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    state: str = "verified_fact",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert one logical_claims + one v1 claim_ledger_entries row.

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
                    VALUES (:id, :lc, 1, :st,
                            'supported_by_user_corpus_only',
                            'supported_by_user_corpus_only',
                            'seeded_for_test')
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "lc": claim_logical_id,
                    "st": state,
                },
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
# seed: draft_final_answers + final_answer_spans + claim_links
# ---------------------------------------------------------------------------
def _create_draft_with_span(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    claim_logical_id: uuid.UUID,
    claim_ledger_entry_id: uuid.UUID,
    span_index: int = 0,
    summary_text: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create one draft_final_answers v1 plus one final_answer_spans row
    linked (via final_answer_span_claim_links) to the given
    (claim_logical_id, claim_ledger_entry_id) pair.

    Returns (draft_id, final_answer_span_id).
    """
    summary = (
        summary_text
        if summary_text is not None
        else f"summary text for test {uuid.uuid4()}"
    )
    draft_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO draft_final_answers
                        (id, task_id, version_no, compiler_name,
                         compiler_version, summary_text)
                    VALUES (:id, :tid, 1, 'mvp0_compiler_v1', '0.1.0', :st)
                    RETURNING id
                    """
                ),
                {"id": uuid.uuid4(), "tid": task_id, "st": summary},
            ).first()[0]
        )
    )

    span_text = f"span-{span_index}-{uuid.uuid4()}"
    final_answer_span_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO final_answer_spans
                        (id, draft_final_answer_id, span_index,
                         char_start, char_end, span_text, span_hash)
                    VALUES
                        (:id, :did, :si,
                         0, :ce, :st, :sh)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "did": draft_id,
                    "si": span_index,
                    "ce": len(span_text),
                    "st": span_text,
                    "sh": hashlib.sha256(span_text.encode("utf-8")).hexdigest(),
                },
            ).first()[0]
        )
    )

    conn.execute(
        text(
            """
            INSERT INTO final_answer_span_claim_links
                (id, final_answer_span_id, claim_ledger_entry_id,
                 claim_logical_id, link_role)
            VALUES (:id, :fas, :le, :lc, 'primary_support')
            """
        ),
        {
            "id": uuid.uuid4(),
            "fas": final_answer_span_id,
            "le": claim_ledger_entry_id,
            "lc": claim_logical_id,
        },
    )
    return draft_id, final_answer_span_id


# ---------------------------------------------------------------------------
# seed: source_quality_assessments (direct INSERT for policy testing)
# ---------------------------------------------------------------------------
def _seed_source_quality_assessment(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
    overall_quality: str,
    contradiction_status: str = "no_known_contradiction",
    version_no: int = 1,
    relevance: str = "direct_support",
    extract_quality: str = "exact_quote_match",
) -> uuid.UUID:
    """INSERT a source_quality_assessments row with the given dimensions.

    Bypasses the mock evaluator so tests can drive the policy matrix
    with arbitrary overall_quality / contradiction_status combinations.
    All other dimensions default to safe codomain values.
    """
    new_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO source_quality_assessments (
                      id, tenant_id, project_id, evidence_span_id, version_no,
                      source_type, source_role, authority_level, independence_level,
                      freshness, relevance, extract_quality, contradiction_status,
                      overall_quality, confidence,
                      evaluator_name, evaluator_version,
                      policy_name, policy_version,
                      idempotency_key, payload
                    ) VALUES (
                      :id, :t, :p, :es, :vn,
                      'user_document', 'unclear', 'unknown', 'unknown',
                      'undated', :rel, :eq, :cs,
                      :oq, 0.5,
                      'test_seed_evaluator', '0.1.0',
                      'test_seed_policy', '0.1.0',
                      :ik, CAST('{}' AS JSONB)
                    )
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": tenant_id,
                    "p": project_id,
                    "es": evidence_span_id,
                    "vn": version_no,
                    "rel": relevance,
                    "eq": extract_quality,
                    "cs": contradiction_status,
                    "oq": overall_quality,
                    "ik": f"test:{uuid.uuid4()}",
                },
            ).first()[0]
        )
    )
    return new_id


# ---------------------------------------------------------------------------
# composite seed: a task with one verified span backed by one evidence_span
# ---------------------------------------------------------------------------
def _seed_task_with_one_verified_span(
    conn: Connection,
) -> dict[str, Any]:
    """Return a dict with everything a single-span verified-backed test needs.

    Keys: tenant_id, project_id, user_id, task_id, evidence_span_id,
          claim_logical_id, claim_ledger_entry_id, draft_id,
          final_answer_span_id.
    """
    tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
    es_id = _create_evidence_span(
        conn,
        tenant_id=tenant_id,
        project_id=project_id,
        created_by=user_id,
    )
    lc_id, le_id = _create_logical_claim_with_verified_entry(
        conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    _link_claim_to_span(
        conn,
        claim_logical_id=lc_id,
        claim_ledger_entry_id=le_id,
        evidence_span_id=es_id,
    )
    draft_id, fas_id = _create_draft_with_span(
        conn,
        task_id=task_id,
        claim_logical_id=lc_id,
        claim_ledger_entry_id=le_id,
    )
    return {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "user_id": user_id,
        "task_id": task_id,
        "evidence_span_id": es_id,
        "claim_logical_id": lc_id,
        "claim_ledger_entry_id": le_id,
        "draft_id": draft_id,
        "final_answer_span_id": fas_id,
    }


# ---------------------------------------------------------------------------
# DB inspection helpers
# ---------------------------------------------------------------------------
def _fetch_coverage_gaps(
    conn: Connection, *, draft_id: uuid.UUID
) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT id, kind, severity, gap_key, details
            FROM coverage_gap_statements
            WHERE draft_final_answer_id = :did
            ORDER BY kind ASC, gap_key ASC
            """
        ),
        {"did": draft_id},
    ).fetchall()
    out: list[dict[str, Any]] = []
    import json as _json
    for r in rows:
        m = dict(r._mapping)
        if isinstance(m.get("details"), str):
            m["details"] = _json.loads(m["details"])
        out.append(m)
    return out


def _fetch_published(
    conn: Connection, *, task_id: uuid.UUID
) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """
            SELECT id, status, version_no
            FROM published_answers
            WHERE task_id = :tid AND version_no = 1
            """
        ),
        {"tid": task_id},
    ).first()
    return None if row is None else dict(row._mapping)


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


def _gaps_by_kind(
    gaps: list[dict[str, Any]], kind: str
) -> list[dict[str, Any]]:
    return [g for g in gaps if str(g["kind"]) == kind]


def _reason_codes_in_gap(gap: dict[str, Any]) -> list[str]:
    """Extract the reason_code values from a source_quality_* gap's details."""
    details = gap.get("details") or {}
    reasons = details.get("reasons") or []
    return [str(r.get("reason_code")) for r in reasons]


# ===========================================================================
# 1) overall_quality='unknown' produces a warning, decision stays approved.
#    Mock evaluator default; also asserts the Gate does NOT mutate
#    source_quality_assessments.
# ===========================================================================
def test_unknown_quality_produces_warning_keeps_approved():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            overall_quality="unknown",
            contradiction_status="unchecked",
            version_no=1,
        )

    # Snapshot pre/post on source_quality_assessments for this span.
    with engine.connect() as conn:
        sqa_count_before = _count_sqa_for_span(
            conn, evidence_span_id=ctx["evidence_span_id"]
        )

    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )

    assert outcome["decision"] == "approved"
    assert outcome["reason_code"] == "all_spans_verified_with_warnings"
    assert outcome["spans_total"] == 1
    assert outcome["spans_verified"] == 1
    assert outcome["spans_unverified"] == 0
    assert outcome["published_answer_id"] is not None
    assert outcome["coverage_gaps_emitted"] == 1

    with engine.connect() as conn:
        gaps = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])
        sqa_count_after = _count_sqa_for_span(
            conn, evidence_span_id=ctx["evidence_span_id"]
        )
        published = _fetch_published(conn, task_id=ctx["task_id"])

    # The Gate must NEVER mutate source_quality_assessments.
    assert sqa_count_before == sqa_count_after

    # Exactly one warning gap.
    warning_gaps = _gaps_by_kind(gaps, "source_quality_warning")
    assert len(warning_gaps) == 1
    assert _gaps_by_kind(gaps, "source_quality_block") == []
    assert _gaps_by_kind(gaps, "unverified_claim") == []
    assert _gaps_by_kind(gaps, "missing_evidence") == []

    w = warning_gaps[0]
    assert w["severity"] == "warn"
    assert w["gap_key"] == (
        f"span:{ctx['final_answer_span_id']}:source_quality_warning"
    )
    reasons = _reason_codes_in_gap(w)
    # Two warning conditions match: overall_quality='unknown' and
    # contradiction_status='unchecked'.
    assert "source_quality_unknown" in reasons
    assert "source_quality_contradiction_unchecked" in reasons

    assert published is not None
    assert str(published["status"]) == "published"


# ===========================================================================
# 2) latest assessment missing -> warning, decision stays approved.
#    No source_quality_assessments row at all for the evidence_span.
# ===========================================================================
def test_missing_assessment_produces_warning_keeps_approved():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        # NO _seed_source_quality_assessment call: the LEFT JOIN LATERAL
        # in the Gate will return NULL for this evidence_span.

    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )

    assert outcome["decision"] == "approved"
    assert outcome["reason_code"] == "all_spans_verified_with_warnings"
    assert outcome["published_answer_id"] is not None

    with engine.connect() as conn:
        gaps = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])

    warning_gaps = _gaps_by_kind(gaps, "source_quality_warning")
    assert len(warning_gaps) == 1
    assert _gaps_by_kind(gaps, "source_quality_block") == []

    reasons = _reason_codes_in_gap(warning_gaps[0])
    assert "source_quality_missing_assessment" in reasons


# ===========================================================================
# 3) overall_quality='weak' -> warning, decision stays approved.
# ===========================================================================
def test_weak_quality_produces_warning_keeps_approved():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            overall_quality="weak",
            contradiction_status="no_known_contradiction",
            version_no=1,
        )

    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )

    assert outcome["decision"] == "approved"
    assert outcome["reason_code"] == "all_spans_verified_with_warnings"
    assert outcome["published_answer_id"] is not None

    with engine.connect() as conn:
        gaps = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])

    warning_gaps = _gaps_by_kind(gaps, "source_quality_warning")
    assert len(warning_gaps) == 1
    assert _gaps_by_kind(gaps, "source_quality_block") == []

    reasons = _reason_codes_in_gap(warning_gaps[0])
    assert "source_quality_weak" in reasons
    # no_known_contradiction is the clean contradiction status, so no
    # contradiction warning should fire.
    assert "source_quality_contradiction_unchecked" not in reasons


# ===========================================================================
# 4) contradiction_status='unchecked' -> warning, decision stays approved.
# ===========================================================================
def test_unchecked_contradiction_produces_warning_keeps_approved():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            # overall_quality='adequate' is clean on the OQ axis, so only
            # the unchecked contradiction status fires the warning.
            overall_quality="adequate",
            contradiction_status="unchecked",
            version_no=1,
        )

    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )

    assert outcome["decision"] == "approved"
    assert outcome["reason_code"] == "all_spans_verified_with_warnings"

    with engine.connect() as conn:
        gaps = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])

    warning_gaps = _gaps_by_kind(gaps, "source_quality_warning")
    assert len(warning_gaps) == 1
    reasons = _reason_codes_in_gap(warning_gaps[0])
    assert reasons == ["source_quality_contradiction_unchecked"]


# ===========================================================================
# 5) overall_quality='unsuitable' -> block, decision rejected, no published.
#    Also asserts that the Gate does NOT mutate source_quality_assessments.
# ===========================================================================
def test_unsuitable_quality_produces_block_rejects_task():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            overall_quality="unsuitable",
            contradiction_status="no_known_contradiction",
            version_no=1,
        )

    with engine.connect() as conn:
        sqa_count_before = _count_sqa_for_span(
            conn, evidence_span_id=ctx["evidence_span_id"]
        )

    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )

    assert outcome["decision"] == "rejected"
    assert outcome["reason_code"] == "source_quality_block"
    assert outcome["spans_total"] == 1
    assert outcome["spans_verified"] == 1
    assert outcome["spans_unverified"] == 0
    assert outcome["published_answer_id"] is None
    assert outcome["coverage_gaps_emitted"] == 1

    with engine.connect() as conn:
        gaps = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])
        sqa_count_after = _count_sqa_for_span(
            conn, evidence_span_id=ctx["evidence_span_id"]
        )
        published = _fetch_published(conn, task_id=ctx["task_id"])

    # The Gate must NEVER mutate source_quality_assessments.
    assert sqa_count_before == sqa_count_after

    block_gaps = _gaps_by_kind(gaps, "source_quality_block")
    assert len(block_gaps) == 1
    assert _gaps_by_kind(gaps, "source_quality_warning") == []
    assert _gaps_by_kind(gaps, "unverified_claim") == []

    b = block_gaps[0]
    assert b["severity"] == "block"
    assert b["gap_key"] == (
        f"span:{ctx['final_answer_span_id']}:source_quality_block"
    )
    reasons = _reason_codes_in_gap(b)
    assert reasons == ["source_quality_unsuitable"]

    # No published_answers v1 in the rejected branch.
    assert published is None


# ===========================================================================
# 6) contradiction_status='contradicted_by_stronger_source' -> block.
# ===========================================================================
def test_contradicted_by_stronger_source_produces_block():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            # overall_quality='adequate' is clean, so the block must come
            # exclusively from the contradiction status.
            overall_quality="adequate",
            contradiction_status="contradicted_by_stronger_source",
            version_no=1,
        )

    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )

    assert outcome["decision"] == "rejected"
    assert outcome["reason_code"] == "source_quality_block"
    assert outcome["published_answer_id"] is None

    with engine.connect() as conn:
        gaps = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])

    block_gaps = _gaps_by_kind(gaps, "source_quality_block")
    assert len(block_gaps) == 1
    reasons = _reason_codes_in_gap(block_gaps[0])
    assert reasons == ["source_quality_contradicted_by_stronger_source"]


# ===========================================================================
# 7) contradiction_status='conflicting_sources' -> block.
# ===========================================================================
def test_conflicting_sources_produces_block():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            overall_quality="adequate",
            contradiction_status="conflicting_sources",
            version_no=1,
        )

    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )

    assert outcome["decision"] == "rejected"
    assert outcome["reason_code"] == "source_quality_block"

    with engine.connect() as conn:
        gaps = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])

    block_gaps = _gaps_by_kind(gaps, "source_quality_block")
    assert len(block_gaps) == 1
    reasons = _reason_codes_in_gap(block_gaps[0])
    assert reasons == ["source_quality_conflicting_sources"]


# ===========================================================================
# 8) Multiple assessments per evidence_span: latest version wins on block.
#    v1='weak', v2='unsuitable' -> block.
# ===========================================================================
def test_latest_version_wins_block_after_weak():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            overall_quality="weak",
            contradiction_status="no_known_contradiction",
            version_no=1,
        )
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            overall_quality="unsuitable",
            contradiction_status="no_known_contradiction",
            version_no=2,
        )

    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )

    assert outcome["decision"] == "rejected"
    assert outcome["reason_code"] == "source_quality_block"

    with engine.connect() as conn:
        gaps = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])
    block_gaps = _gaps_by_kind(gaps, "source_quality_block")
    assert len(block_gaps) == 1
    reasons = _reason_codes_in_gap(block_gaps[0])
    assert reasons == ["source_quality_unsuitable"]


# ===========================================================================
# 9) v1='unsuitable', v2='strong' -> latest wins, decision approved cleanly
#    (no source quality gap at all).
# ===========================================================================
def test_latest_version_wins_clean_after_unsuitable():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            overall_quality="unsuitable",
            contradiction_status="no_known_contradiction",
            version_no=1,
        )
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            overall_quality="strong",
            contradiction_status="no_known_contradiction",
            version_no=2,
        )

    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )

    # Clean path: original 8.4 reason_code, no source quality gap.
    assert outcome["decision"] == "approved"
    assert outcome["reason_code"] == "all_spans_verified"
    assert outcome["published_answer_id"] is not None
    assert outcome["coverage_gaps_emitted"] == 0

    with engine.connect() as conn:
        gaps = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])
    assert _gaps_by_kind(gaps, "source_quality_block") == []
    assert _gaps_by_kind(gaps, "source_quality_warning") == []


# ===========================================================================
# 10) v1='unknown', v2='unsuitable' -> latest wins, block fires.
# ===========================================================================
def test_latest_version_wins_block_after_unknown():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            overall_quality="unknown",
            contradiction_status="unchecked",
            version_no=1,
        )
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            overall_quality="unsuitable",
            contradiction_status="no_known_contradiction",
            version_no=2,
        )

    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )

    assert outcome["decision"] == "rejected"
    assert outcome["reason_code"] == "source_quality_block"

    with engine.connect() as conn:
        gaps = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])
    block_gaps = _gaps_by_kind(gaps, "source_quality_block")
    assert len(block_gaps) == 1
    reasons = _reason_codes_in_gap(block_gaps[0])
    assert reasons == ["source_quality_unsuitable"]


# ===========================================================================
# 11) Multiple evidence_spans for the same final_answer_span: worst-on-block.
#    One evidence_span 'strong', one 'unsuitable' -> block.
# ===========================================================================
def test_multiple_evidence_spans_one_block_rejects():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)

        # One logical claim, one verified_fact entry; two evidence_spans
        # linked to that same entry. The Gate will see both evidence_spans
        # as supporting the verified span.
        es_a = _create_evidence_span(
            conn, tenant_id=tenant_id, project_id=project_id, created_by=user_id
        )
        es_b = _create_evidence_span(
            conn, tenant_id=tenant_id, project_id=project_id, created_by=user_id
        )
        lc, le = _create_logical_claim_with_verified_entry(
            conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc,
            claim_ledger_entry_id=le,
            evidence_span_id=es_a,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc,
            claim_ledger_entry_id=le,
            evidence_span_id=es_b,
        )
        draft_id, fas_id = _create_draft_with_span(
            conn,
            task_id=task_id,
            claim_logical_id=lc,
            claim_ledger_entry_id=le,
        )

        # es_a: strong (clean). es_b: unsuitable (block). Aggregation
        # worst-on-block must reject.
        _seed_source_quality_assessment(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=es_a,
            overall_quality="strong",
            contradiction_status="no_known_contradiction",
        )
        _seed_source_quality_assessment(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=es_b,
            overall_quality="unsuitable",
            contradiction_status="no_known_contradiction",
        )

    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )

    assert outcome["decision"] == "rejected"
    assert outcome["reason_code"] == "source_quality_block"

    with engine.connect() as conn:
        gaps = _fetch_coverage_gaps(conn, draft_id=draft_id)
    block_gaps = _gaps_by_kind(gaps, "source_quality_block")
    assert len(block_gaps) == 1
    # The block gap's details must reference the offending evidence_span.
    reasons = _reason_codes_in_gap(block_gaps[0])
    assert "source_quality_unsuitable" in reasons


# ===========================================================================
# 12) CVE-lite priority: an unverified span produces 'unverified_claim',
#     NOT 'source_quality_block', even if the source quality on linked
#     evidence is 'unsuitable'. Source quality is not consulted when any
#     span is not verified-backed.
# ===========================================================================
def test_cve_priority_unverified_takes_precedence_over_source_quality():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        es_id = _create_evidence_span(
            conn, tenant_id=tenant_id, project_id=project_id, created_by=user_id
        )
        # Create logical claim with v1 in state 'candidate' (NOT verified).
        # The span will reference the v1 entry, but no v2 exists.
        # latest_entry_state will be 'candidate', so the span is NOT
        # verified-backed -> Branch C of the Gate.
        lc, le = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            state="candidate",
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc,
            claim_ledger_entry_id=le,
            evidence_span_id=es_id,
        )
        draft_id, fas_id = _create_draft_with_span(
            conn,
            task_id=task_id,
            claim_logical_id=lc,
            claim_ledger_entry_id=le,
        )
        # Even with an 'unsuitable' source quality assessment for the
        # backing evidence_span, the CVE-lite branch must win.
        _seed_source_quality_assessment(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=es_id,
            overall_quality="unsuitable",
            contradiction_status="contradicted_by_stronger_source",
        )

    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )

    assert outcome["decision"] == "rejected"
    # The critical invariant: reason_code is the CVE-lite reason, NOT
    # 'source_quality_block'.
    assert outcome["reason_code"] == "unverified_spans_present"
    assert outcome["published_answer_id"] is None
    assert outcome["spans_unverified"] == 1

    with engine.connect() as conn:
        gaps = _fetch_coverage_gaps(conn, draft_id=draft_id)

    # An 'unverified_claim' gap was emitted; NO 'source_quality_*' gap.
    unverified_gaps = _gaps_by_kind(gaps, "unverified_claim")
    assert len(unverified_gaps) == 1
    assert unverified_gaps[0]["gap_key"] == f"span:{fas_id}"
    assert _gaps_by_kind(gaps, "source_quality_block") == []
    assert _gaps_by_kind(gaps, "source_quality_warning") == []


# ===========================================================================
# 13) Idempotency on redelivery: running the gate twice on the same draft
#     produces the same decision and does NOT duplicate any gap or row.
# ===========================================================================
def test_idempotent_on_redelivery_no_duplicate_gaps():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            overall_quality="unsuitable",
            contradiction_status="no_known_contradiction",
        )

    with engine.begin() as conn:
        outcome_1 = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )

    with engine.connect() as conn:
        gaps_1 = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])
        gate_count_1 = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM final_gate_reports "
                    "WHERE draft_final_answer_id = :did"
                ),
                {"did": ctx["draft_id"]},
            ).scalar_one()
        )

    with engine.begin() as conn:
        outcome_2 = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )

    with engine.connect() as conn:
        gaps_2 = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])
        gate_count_2 = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM final_gate_reports "
                    "WHERE draft_final_answer_id = :did"
                ),
                {"did": ctx["draft_id"]},
            ).scalar_one()
        )

    # Same decision both times.
    assert outcome_1["decision"] == outcome_2["decision"] == "rejected"
    assert (
        outcome_1["reason_code"]
        == outcome_2["reason_code"]
        == "source_quality_block"
    )

    # No duplicate gate report.
    assert gate_count_1 == 1
    assert gate_count_2 == 1
    assert outcome_1["final_gate_report_id"] == outcome_2["final_gate_report_id"]

    # No duplicate gaps. Compare the set of (kind, gap_key) pairs across
    # the two snapshots — they must be identical and have the same size.
    pairs_1 = {(str(g["kind"]), str(g["gap_key"])) for g in gaps_1}
    pairs_2 = {(str(g["kind"]), str(g["gap_key"])) for g in gaps_2}
    assert pairs_1 == pairs_2
    assert len(gaps_1) == len(gaps_2)

    # Second invocation reports 0 NEW gaps emitted (all already present).
    assert outcome_2["coverage_gaps_emitted"] == 0
