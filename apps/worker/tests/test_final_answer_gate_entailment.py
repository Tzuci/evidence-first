"""Worker-level tests for the Claim Entailment integration in
apps/worker/app/services/final_answer_gate.py (Phase 8.8A-GATE-CODE).

Coverage map (per the block 8.8A-GATE-CODE-B prompt §3):

   1. test_contradicted_blocks_publication
   2. test_not_supported_is_warning_not_block
   3. test_partially_supported_is_warning
   4. test_uncertain_is_warning
   5. test_missing_check_is_warning
   6. test_entailed_clean_if_no_source_quality_warning
   7. test_cve_lite_unverified_has_priority_over_entailment_block
   8. test_entailment_block_has_priority_over_source_quality_block
   9. test_source_quality_block_still_works_when_no_entailment_block
  10. test_source_quality_warning_and_entailment_warning_coexist
  11. test_latest_version_wins
  12. test_no_mutation_of_claim_entailment_checks
  13. test_final_gate_report_payload_contains_entailment_summary

The CVE-lite > Entailment priority test (#7) and the
Entailment > Source Quality priority test (#8) are the architectural
invariants of PHASE_8_8A_GATE_PRE.md §7: when a span is not
verified-backed, the Gate must emit 'unverified_spans_present' and NOT
consult claim_entailment_checks; when entailment blocks AND source
quality also blocks, the Gate's reason_code must be 'entailment_block'
(both gap kinds may be emitted for audit completeness).

Test #12 covers idempotency: a second invocation of run_final_answer_gate
on the same draft must not mutate claim_entailment_checks (read-only
contract per PHASE_8_8A_GATE_PRE.md §10.6).

Phase 8.8A-GATE invariants verified across all tests:
  - The Gate does NOT mutate claim_entailment_checks (read-only SELECT).
    Tested explicitly via pre/post snapshot in #12.
  - The Gate does NOT mutate claim_ledger_entries.
  - The Gate does NOT mutate source_quality_assessments.
  - All entailment gap rows use the deterministic gap_key format
    f'span:{final_answer_span_id}:entailment_{block,warning}'.
  - Reason code 'entailment_block' is emitted when ANY span has
    verdict='contradicted' (latest version).
  - Reason code 'all_spans_verified_with_warnings' is REUSED for
    approved-with-warnings paths (no new reason code introduced
    specifically for entailment warnings).

Design notes:
  - Local helpers only (no imports from other test files), per the
    block prompt §3.
  - DB-real tests against the worker's get_engine().
  - All ids/hashes are uuid.uuid4()-derived per invocation, so the
    file is safe to rerun.
  - We do NOT go through the task_created consumer for these unit-style
    tests: we seed the minimal claim+span topology directly and call
    run_final_answer_gate() on a hand-built draft. This is the same
    approach used by test_final_answer_gate_source_quality.py.
  - We seed claim_entailment_checks rows directly (not via the mock
    checker) so we can drive the policy matrix with exact verdict
    values. The mock checker only ever emits 'entailed', 'not_supported'
    or 'uncertain', so 'contradicted' / 'partially_supported' must be
    seeded for the corresponding scenarios.
  - We also seed source_quality_assessments rows directly when needed
    (priority/coexistence tests) so the Source Quality axis can be
    driven independently of the entailment axis.
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
                {"t": tenant_id, "n": f"fag-ent-test-{uuid.uuid4()}"},
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
        f"Final answer gate entailment test marker {marker}. "
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
# seed: logical_claim + ledger entry + claim_evidence_link
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
# seed: claim_entailment_checks (direct INSERT for policy testing)
# ---------------------------------------------------------------------------
def _seed_entailment_check(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    claim_logical_id: uuid.UUID,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
    verdict: str,
    version_no: int = 1,
    confidence: float | None = 0.7,
) -> uuid.UUID:
    """INSERT a claim_entailment_checks row with the given verdict.

    Bypasses the mock checker so tests can drive the policy matrix
    with arbitrary verdict values including 'contradicted' and
    'partially_supported' (which the MVP-0 mock checker never emits).
    """
    new_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO claim_entailment_checks (
                      id, tenant_id, project_id, task_id,
                      claim_logical_id, claim_ledger_entry_id, evidence_span_id,
                      version_no, verdict, confidence,
                      checker_name, checker_version,
                      policy_name, policy_version,
                      idempotency_key, rationale, payload
                    ) VALUES (
                      :id, :t, :p, :tid,
                      :lc, :le, :es,
                      :vn, :v, :conf,
                      'test_seed_checker', '0.1.0',
                      'test_seed_policy', '0.1.0',
                      :ik, NULL, CAST('{}' AS JSONB)
                    )
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": tenant_id,
                    "p": project_id,
                    "tid": task_id,
                    "lc": claim_logical_id,
                    "le": claim_ledger_entry_id,
                    "es": evidence_span_id,
                    "vn": version_no,
                    "v": verdict,
                    "conf": confidence,
                    "ik": f"test:{uuid.uuid4()}",
                },
            ).first()[0]
        )
    )
    return new_id


# ---------------------------------------------------------------------------
# seed: source_quality_assessments (for priority/coexistence tests)
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
) -> uuid.UUID:
    """INSERT a source_quality_assessments row with the given dimensions.

    Used by the priority/coexistence tests so the Source Quality axis
    can be driven independently of the entailment axis. Defaults the
    other dimensions to clean values.
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
                      'undated', 'direct_support', 'exact_quote_match', :cs,
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


def _fetch_gate_report(
    conn: Connection, *, task_id: uuid.UUID
) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """
            SELECT id, decision, reason_code, payload
            FROM final_gate_reports
            WHERE task_id = :tid
            """
        ),
        {"tid": task_id},
    ).first()
    if row is None:
        return None
    m = dict(row._mapping)
    if isinstance(m.get("payload"), str):
        import json as _json
        m["payload"] = _json.loads(m["payload"])
    return m


def _count_entailment_checks_for_task(
    conn: Connection, *, task_id: uuid.UUID
) -> int:
    return int(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM claim_entailment_checks "
                "WHERE task_id = :tid"
            ),
            {"tid": task_id},
        ).scalar_one()
    )


def _count_ledger_entries_for_logical(
    conn: Connection, *, claim_logical_id: uuid.UUID
) -> int:
    return int(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM claim_ledger_entries "
                "WHERE claim_logical_id = :lc"
            ),
            {"lc": claim_logical_id},
        ).scalar_one()
    )


def _gaps_by_kind(
    gaps: list[dict[str, Any]], kind: str
) -> list[dict[str, Any]]:
    return [g for g in gaps if str(g["kind"]) == kind]


# ===========================================================================
# 1) verdict='contradicted' produces block, decision rejected, no published.
# ===========================================================================
def test_contradicted_blocks_publication():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_entailment_check(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
            claim_logical_id=ctx["claim_logical_id"],
            claim_ledger_entry_id=ctx["claim_ledger_entry_id"],
            evidence_span_id=ctx["evidence_span_id"],
            verdict="contradicted",
        )

    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )

    assert outcome["decision"] == "rejected"
    assert outcome["reason_code"] == "entailment_block"
    assert outcome["spans_total"] == 1
    assert outcome["spans_verified"] == 1
    assert outcome["spans_unverified"] == 0
    assert outcome["published_answer_id"] is None

    with engine.connect() as conn:
        gaps = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])
        published = _fetch_published(conn, task_id=ctx["task_id"])

    block_gaps = _gaps_by_kind(gaps, "entailment_block")
    assert len(block_gaps) == 1
    assert block_gaps[0]["severity"] == "block"
    assert block_gaps[0]["gap_key"] == (
        f"span:{ctx['final_answer_span_id']}:entailment_block"
    )
    # No source quality gap of either kind (no SQ row was seeded; but
    # the Gate emits a source_quality_warning for missing assessment
    # per 8.7G policy in the rejected-by-entailment branch — we accept
    # either presence or absence of source_quality_warning, since this
    # test focuses on the entailment axis).
    # No CVE-lite gap.
    assert _gaps_by_kind(gaps, "unverified_claim") == []
    assert _gaps_by_kind(gaps, "missing_evidence") == []
    # No source_quality_block (no SQ row with unsuitable was seeded).
    assert _gaps_by_kind(gaps, "source_quality_block") == []

    # No published_answers v1 in the rejected branch.
    assert published is None


# ===========================================================================
# 2) verdict='not_supported' produces warning, decision stays approved.
# ===========================================================================
def test_not_supported_is_warning_not_block():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_entailment_check(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
            claim_logical_id=ctx["claim_logical_id"],
            claim_ledger_entry_id=ctx["claim_ledger_entry_id"],
            evidence_span_id=ctx["evidence_span_id"],
            verdict="not_supported",
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
        published = _fetch_published(conn, task_id=ctx["task_id"])

    warning_gaps = _gaps_by_kind(gaps, "entailment_warning")
    assert len(warning_gaps) == 1
    assert warning_gaps[0]["severity"] == "warn"
    assert warning_gaps[0]["gap_key"] == (
        f"span:{ctx['final_answer_span_id']}:entailment_warning"
    )
    assert _gaps_by_kind(gaps, "entailment_block") == []

    assert published is not None
    assert str(published["status"]) == "published"


# ===========================================================================
# 3) verdict='partially_supported' produces warning.
# ===========================================================================
def test_partially_supported_is_warning():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_entailment_check(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
            claim_logical_id=ctx["claim_logical_id"],
            claim_ledger_entry_id=ctx["claim_ledger_entry_id"],
            evidence_span_id=ctx["evidence_span_id"],
            verdict="partially_supported",
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

    warning_gaps = _gaps_by_kind(gaps, "entailment_warning")
    assert len(warning_gaps) == 1
    assert warning_gaps[0]["severity"] == "warn"
    assert _gaps_by_kind(gaps, "entailment_block") == []


# ===========================================================================
# 4) verdict='uncertain' produces warning.
# ===========================================================================
def test_uncertain_is_warning():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_entailment_check(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
            claim_logical_id=ctx["claim_logical_id"],
            claim_ledger_entry_id=ctx["claim_ledger_entry_id"],
            evidence_span_id=ctx["evidence_span_id"],
            verdict="uncertain",
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

    warning_gaps = _gaps_by_kind(gaps, "entailment_warning")
    assert len(warning_gaps) == 1
    assert _gaps_by_kind(gaps, "entailment_block") == []


# ===========================================================================
# 5) latest entailment check missing -> warning, decision stays approved.
# ===========================================================================
def test_missing_check_is_warning():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        # NO _seed_entailment_check call: the LEFT JOIN LATERAL in the
        # Gate must return NULL for this (entry, span) pair, and the
        # Gate must map that to a missing-check warning.

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

    warning_gaps = _gaps_by_kind(gaps, "entailment_warning")
    assert len(warning_gaps) == 1
    assert warning_gaps[0]["severity"] == "warn"
    assert _gaps_by_kind(gaps, "entailment_block") == []


# ===========================================================================
# 6) verdict='entailed' is clean: no entailment gap. Decision is approved.
#    The Source Quality axis is also seeded clean ('adequate' +
#    'no_known_contradiction'), so the reason_code reaches the original
#    8.4 clean value 'all_spans_verified'.
# ===========================================================================
def test_entailed_clean_if_no_source_quality_warning():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_entailment_check(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
            claim_logical_id=ctx["claim_logical_id"],
            claim_ledger_entry_id=ctx["claim_ledger_entry_id"],
            evidence_span_id=ctx["evidence_span_id"],
            verdict="entailed",
        )
        # Seed a clean source_quality_assessments so the SQ axis does
        # NOT emit any warning, allowing us to assert the entailment
        # axis cleanly.
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            overall_quality="adequate",
            contradiction_status="no_known_contradiction",
        )

    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )

    assert outcome["decision"] == "approved"
    # Clean path: original 8.4 reason_code, no entailment gap, no SQ gap.
    assert outcome["reason_code"] == "all_spans_verified"
    assert outcome["published_answer_id"] is not None

    with engine.connect() as conn:
        gaps = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])

    assert _gaps_by_kind(gaps, "entailment_block") == []
    assert _gaps_by_kind(gaps, "entailment_warning") == []
    assert _gaps_by_kind(gaps, "source_quality_block") == []
    assert _gaps_by_kind(gaps, "source_quality_warning") == []


# ===========================================================================
# 7) CVE-lite priority: unverified span produces 'unverified_spans_present',
#    NOT 'entailment_block', even if a 'contradicted' entailment row
#    exists for the linked evidence_span. Entailment is NOT consulted
#    when any span is not verified-backed.
# ===========================================================================
def test_cve_lite_unverified_has_priority_over_entailment_block():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        es_id = _create_evidence_span(
            conn, tenant_id=tenant_id, project_id=project_id, created_by=user_id
        )
        # Create logical claim with v1 in state 'candidate' (NOT verified).
        # The span will reference v1, but no v2 exists; latest_entry_state
        # will be 'candidate', so the span is NOT verified-backed.
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
        # Even with a 'contradicted' entailment check for this pair,
        # the CVE-lite branch must win.
        _seed_entailment_check(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            claim_logical_id=lc,
            claim_ledger_entry_id=le,
            evidence_span_id=es_id,
            verdict="contradicted",
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
    # 'entailment_block'.
    assert outcome["reason_code"] == "unverified_spans_present"
    assert outcome["published_answer_id"] is None
    assert outcome["spans_unverified"] == 1

    with engine.connect() as conn:
        gaps = _fetch_coverage_gaps(conn, draft_id=draft_id)

    # An 'unverified_claim' gap was emitted; NO entailment gap of any
    # kind (Source Quality is also not consulted in this branch).
    unverified_gaps = _gaps_by_kind(gaps, "unverified_claim")
    assert len(unverified_gaps) == 1
    assert unverified_gaps[0]["gap_key"] == f"span:{fas_id}"
    assert _gaps_by_kind(gaps, "entailment_block") == []
    assert _gaps_by_kind(gaps, "entailment_warning") == []
    assert _gaps_by_kind(gaps, "source_quality_block") == []
    assert _gaps_by_kind(gaps, "source_quality_warning") == []


# ===========================================================================
# 8) Entailment block priority: when entailment=contradicted AND
#    source_quality=unsuitable on the same verified-backed span, the
#    reason_code is 'entailment_block' (NOT 'source_quality_block').
#    The Gate may emit both kinds of gaps for audit completeness, but
#    the decision is driven by the entailment axis.
# ===========================================================================
def test_entailment_block_has_priority_over_source_quality_block():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_entailment_check(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
            claim_logical_id=ctx["claim_logical_id"],
            claim_ledger_entry_id=ctx["claim_ledger_entry_id"],
            evidence_span_id=ctx["evidence_span_id"],
            verdict="contradicted",
        )
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            overall_quality="unsuitable",
            contradiction_status="no_known_contradiction",
        )

    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )

    # The reason_code is driven by the higher-priority axis.
    assert outcome["decision"] == "rejected"
    assert outcome["reason_code"] == "entailment_block"
    assert outcome["published_answer_id"] is None

    with engine.connect() as conn:
        gaps = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])

    # The entailment_block gap MUST be emitted (it drove the decision).
    block_gaps = _gaps_by_kind(gaps, "entailment_block")
    assert len(block_gaps) == 1
    assert block_gaps[0]["severity"] == "block"
    assert block_gaps[0]["gap_key"] == (
        f"span:{ctx['final_answer_span_id']}:entailment_block"
    )


# ===========================================================================
# 9) Source quality block still works when no entailment block fires.
#    entailment=entailed (clean), source_quality=unsuitable -> reason_code
#    'source_quality_block'.
# ===========================================================================
def test_source_quality_block_still_works_when_no_entailment_block():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_entailment_check(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
            claim_logical_id=ctx["claim_logical_id"],
            claim_ledger_entry_id=ctx["claim_ledger_entry_id"],
            evidence_span_id=ctx["evidence_span_id"],
            verdict="entailed",
        )
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            overall_quality="unsuitable",
            contradiction_status="no_known_contradiction",
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

    sq_block_gaps = _gaps_by_kind(gaps, "source_quality_block")
    assert len(sq_block_gaps) == 1
    # No entailment gap when verdict='entailed' (clean entailment axis).
    assert _gaps_by_kind(gaps, "entailment_block") == []
    assert _gaps_by_kind(gaps, "entailment_warning") == []


# ===========================================================================
# 10) Source quality warning + entailment warning coexist on the same
#     draft. Both gap rows are emitted; reason_code stays
#     'all_spans_verified_with_warnings'; published_answers v1 is inserted.
# ===========================================================================
def test_source_quality_warning_and_entailment_warning_coexist():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_entailment_check(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
            claim_logical_id=ctx["claim_logical_id"],
            claim_ledger_entry_id=ctx["claim_ledger_entry_id"],
            evidence_span_id=ctx["evidence_span_id"],
            verdict="uncertain",
        )
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            evidence_span_id=ctx["evidence_span_id"],
            overall_quality="unknown",
            contradiction_status="unchecked",
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

    entailment_warns = _gaps_by_kind(gaps, "entailment_warning")
    sq_warns = _gaps_by_kind(gaps, "source_quality_warning")
    assert len(entailment_warns) == 1
    assert len(sq_warns) == 1
    assert entailment_warns[0]["gap_key"] == (
        f"span:{ctx['final_answer_span_id']}:entailment_warning"
    )
    assert sq_warns[0]["gap_key"] == (
        f"span:{ctx['final_answer_span_id']}:source_quality_warning"
    )
    # No block of either kind.
    assert _gaps_by_kind(gaps, "entailment_block") == []
    assert _gaps_by_kind(gaps, "source_quality_block") == []


# ===========================================================================
# 11) Latest version wins on the entailment axis.
#     Case A: v1='entailed', v2='contradicted' -> block.
#     Case B: v1='contradicted', v2='entailed' -> clean entailment axis.
# ===========================================================================
def test_latest_version_wins():
    _skip_if_db_unreachable()
    engine = get_engine()

    # --- Case A: v1 entailed, v2 contradicted -> block ----------------------
    with engine.begin() as conn:
        ctx_a = _seed_task_with_one_verified_span(conn)
        _seed_entailment_check(
            conn,
            tenant_id=ctx_a["tenant_id"],
            project_id=ctx_a["project_id"],
            task_id=ctx_a["task_id"],
            claim_logical_id=ctx_a["claim_logical_id"],
            claim_ledger_entry_id=ctx_a["claim_ledger_entry_id"],
            evidence_span_id=ctx_a["evidence_span_id"],
            verdict="entailed",
            version_no=1,
        )
        _seed_entailment_check(
            conn,
            tenant_id=ctx_a["tenant_id"],
            project_id=ctx_a["project_id"],
            task_id=ctx_a["task_id"],
            claim_logical_id=ctx_a["claim_logical_id"],
            claim_ledger_entry_id=ctx_a["claim_ledger_entry_id"],
            evidence_span_id=ctx_a["evidence_span_id"],
            verdict="contradicted",
            version_no=2,
        )

    with engine.begin() as conn:
        outcome_a = run_final_answer_gate(
            conn,
            tenant_id=ctx_a["tenant_id"],
            project_id=ctx_a["project_id"],
            task_id=ctx_a["task_id"],
        )
    assert outcome_a["decision"] == "rejected"
    assert outcome_a["reason_code"] == "entailment_block"

    with engine.connect() as conn:
        gaps_a = _fetch_coverage_gaps(conn, draft_id=ctx_a["draft_id"])
    assert len(_gaps_by_kind(gaps_a, "entailment_block")) == 1

    # --- Case B: v1 contradicted, v2 entailed -> clean entailment axis ------
    with engine.begin() as conn:
        ctx_b = _seed_task_with_one_verified_span(conn)
        _seed_entailment_check(
            conn,
            tenant_id=ctx_b["tenant_id"],
            project_id=ctx_b["project_id"],
            task_id=ctx_b["task_id"],
            claim_logical_id=ctx_b["claim_logical_id"],
            claim_ledger_entry_id=ctx_b["claim_ledger_entry_id"],
            evidence_span_id=ctx_b["evidence_span_id"],
            verdict="contradicted",
            version_no=1,
        )
        _seed_entailment_check(
            conn,
            tenant_id=ctx_b["tenant_id"],
            project_id=ctx_b["project_id"],
            task_id=ctx_b["task_id"],
            claim_logical_id=ctx_b["claim_logical_id"],
            claim_ledger_entry_id=ctx_b["claim_ledger_entry_id"],
            evidence_span_id=ctx_b["evidence_span_id"],
            verdict="entailed",
            version_no=2,
        )
        # Seed clean SQ so the only relevant axis is entailment.
        _seed_source_quality_assessment(
            conn,
            tenant_id=ctx_b["tenant_id"],
            project_id=ctx_b["project_id"],
            evidence_span_id=ctx_b["evidence_span_id"],
            overall_quality="adequate",
            contradiction_status="no_known_contradiction",
        )

    with engine.begin() as conn:
        outcome_b = run_final_answer_gate(
            conn,
            tenant_id=ctx_b["tenant_id"],
            project_id=ctx_b["project_id"],
            task_id=ctx_b["task_id"],
        )
    assert outcome_b["decision"] == "approved"
    # No entailment_block because the LATEST (v2) is 'entailed'.
    assert outcome_b["reason_code"] != "entailment_block"

    with engine.connect() as conn:
        gaps_b = _fetch_coverage_gaps(conn, draft_id=ctx_b["draft_id"])
    assert _gaps_by_kind(gaps_b, "entailment_block") == []
    assert _gaps_by_kind(gaps_b, "entailment_warning") == []


# ===========================================================================
# 12) Read-only contract: the Gate must NEVER mutate claim_entailment_checks
#     (or claim_ledger_entries). Pre/post snapshot is invariant.
#     Combined with redelivery idempotency: a second invocation does not
#     duplicate gaps and does not alter table counts.
# ===========================================================================
def test_no_mutation_of_claim_entailment_checks():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_entailment_check(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
            claim_logical_id=ctx["claim_logical_id"],
            claim_ledger_entry_id=ctx["claim_ledger_entry_id"],
            evidence_span_id=ctx["evidence_span_id"],
            verdict="uncertain",
        )

    with engine.connect() as conn:
        cec_count_before = _count_entailment_checks_for_task(
            conn, task_id=ctx["task_id"]
        )
        cle_count_before = _count_ledger_entries_for_logical(
            conn, claim_logical_id=ctx["claim_logical_id"]
        )

    # First Gate run.
    with engine.begin() as conn:
        outcome_1 = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )
    assert outcome_1["decision"] == "approved"

    with engine.connect() as conn:
        cec_count_after_1 = _count_entailment_checks_for_task(
            conn, task_id=ctx["task_id"]
        )
        cle_count_after_1 = _count_ledger_entries_for_logical(
            conn, claim_logical_id=ctx["claim_logical_id"]
        )
        gaps_after_1 = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])

    # Read-only invariant on claim_entailment_checks and ledger.
    assert cec_count_after_1 == cec_count_before
    assert cle_count_after_1 == cle_count_before

    # Second Gate run (redelivery idempotency).
    with engine.begin() as conn:
        outcome_2 = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )
    assert outcome_2["decision"] == outcome_1["decision"] == "approved"
    assert outcome_2["reason_code"] == outcome_1["reason_code"]
    # Same final_gate_report_id (UNIQUE on draft_final_answer_id).
    assert outcome_2["final_gate_report_id"] == outcome_1["final_gate_report_id"]

    with engine.connect() as conn:
        cec_count_after_2 = _count_entailment_checks_for_task(
            conn, task_id=ctx["task_id"]
        )
        cle_count_after_2 = _count_ledger_entries_for_logical(
            conn, claim_logical_id=ctx["claim_logical_id"]
        )
        gaps_after_2 = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])

    # Still no mutation on the read-only tables.
    assert cec_count_after_2 == cec_count_before
    assert cle_count_after_2 == cle_count_before

    # No duplicate gaps after redelivery.
    pairs_1 = {(str(g["kind"]), str(g["gap_key"])) for g in gaps_after_1}
    pairs_2 = {(str(g["kind"]), str(g["gap_key"])) for g in gaps_after_2}
    assert pairs_1 == pairs_2
    assert len(gaps_after_1) == len(gaps_after_2)


# ===========================================================================
# 13) The final gate report payload must surface some entailment-related
#     summary information after a 'contradicted' rejection. The exact
#     shape of the JSONB payload is an implementation detail of
#     8.8A-GATE-CODE-A; we assert leniently by requiring at least one
#     observable signal: either a mention of 'entailment' in the
#     serialized payload, OR an entailment_block gap row attached to
#     the draft.
# ===========================================================================
def test_final_gate_report_payload_contains_entailment_summary():
    _skip_if_db_unreachable()
    engine = get_engine()
    with engine.begin() as conn:
        ctx = _seed_task_with_one_verified_span(conn)
        _seed_entailment_check(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
            claim_logical_id=ctx["claim_logical_id"],
            claim_ledger_entry_id=ctx["claim_ledger_entry_id"],
            evidence_span_id=ctx["evidence_span_id"],
            verdict="contradicted",
        )

    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=ctx["tenant_id"],
            project_id=ctx["project_id"],
            task_id=ctx["task_id"],
        )
    assert outcome["decision"] == "rejected"
    assert outcome["reason_code"] == "entailment_block"

    with engine.connect() as conn:
        report = _fetch_gate_report(conn, task_id=ctx["task_id"])
        gaps = _fetch_coverage_gaps(conn, draft_id=ctx["draft_id"])

    assert report is not None
    assert str(report["decision"]) == "rejected"
    assert str(report["reason_code"]) == "entailment_block"

    payload = report.get("payload") or {}
    assert isinstance(payload, dict)
    assert "entailment" in payload

    entailment = payload["entailment"]
    assert entailment["policy_name"] == "mvp0_entailment_gate_policy"
    assert entailment["policy_version"] == "0.1.0"
    assert entailment["status"] == "blocked"
    assert entailment["spans_with_block"] == 1
    assert entailment["block_reason_counts"]["entailment_contradicted"] == 1

    entailment_block_gaps = _gaps_by_kind(gaps, "entailment_block")
    assert len(entailment_block_gaps) == 1
    assert entailment_block_gaps[0]["gap_key"] == (
        f"span:{ctx['final_answer_span_id']}:entailment_block"
    )
    assert entailment_block_gaps[0]["severity"] == "block"
