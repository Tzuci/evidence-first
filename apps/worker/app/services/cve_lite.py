"""Deterministic, mock-driven Citation Span Verifier (CVE-lite, Phase 8.3).

For each claim_ledger_entries v1 'candidate' belonging to the task, run a
deterministic check:
  - PASS if the evidence_spans.quote is a substring of document_chunks.inline_text
    AND sha256(quote utf-8) == evidence_spans.quote_hash;
  - FAIL otherwise.

Outputs per claim:
  - one verification_records (UNIQUE on (claim_ledger_entry_id, check_kind, check_name))
  - on PASS:    one claim_ledger_entries v2 with state='verified_fact'
  - on FAIL:    one claim_ledger_entries v2 with state='unverifiable'
  - one claim_lineage(parent=v1, child=v2, relation_kind='supersedes')

Append-only invariant respected: v1 is NEVER updated.
Idempotent under redelivery: every INSERT uses ON CONFLICT DO NOTHING.
"""
from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection


CHECK_NAME = "quote_hash_and_substring_v1"
EVALUATOR_ID = "mvp0_cve_lite_v1"


def _candidate_v1_entries(conn: Connection, task_id: uuid.UUID) -> list[dict]:
    rows = conn.execute(
        text(
            """
            SELECT
              cle.id              AS entry_id,
              cle.claim_logical_id AS logical_id,
              cel.evidence_span_id AS span_id,
              es.quote            AS quote,
              es.quote_hash       AS quote_hash,
              es.document_chunk_id AS chunk_id,
              dc.inline_text       AS chunk_text
            FROM claim_ledger_entries cle
            JOIN logical_claims lc        ON lc.id = cle.claim_logical_id
            JOIN claim_evidence_links cel ON cel.claim_ledger_entry_id = cle.id
            JOIN evidence_spans es        ON es.id  = cel.evidence_span_id
            JOIN document_chunks dc       ON dc.id  = es.document_chunk_id
            WHERE lc.task_id = :tid
              AND cle.version_no = 1
              AND cle.state = 'candidate'
            """
        ),
        {"tid": task_id},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _outcome_for(quote: str, quote_hash: str, chunk_text: str) -> str:
    if quote is None or chunk_text is None:
        return "fail"
    if quote not in chunk_text:
        return "fail"
    recomputed = hashlib.sha256(quote.encode("utf-8")).hexdigest()
    return "pass" if recomputed == quote_hash else "fail"


def _v2_already_present(conn: Connection, claim_logical_id: uuid.UUID) -> bool:
    row = conn.execute(
        text(
            "SELECT 1 FROM claim_ledger_entries WHERE claim_logical_id = :lc AND version_no = 2 LIMIT 1"
        ),
        {"lc": claim_logical_id},
    ).first()
    return row is not None


def _record_verification(
    conn: Connection,
    *,
    claim_logical_id: uuid.UUID,
    claim_ledger_entry_id: uuid.UUID,
    outcome: str,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO verification_records (
              id, claim_logical_id, claim_ledger_entry_id,
              check_kind, check_name, outcome, score, evaluator_id, payload
            ) VALUES (
              :id, :lc, :le,
              'cve_lite', :cn, :outcome, NULL, :ev, '{}'::jsonb
            )
            ON CONFLICT (claim_ledger_entry_id, check_kind, check_name) DO NOTHING
            """
        ),
        {
            "id": uuid.uuid4(),
            "lc": claim_logical_id,
            "le": claim_ledger_entry_id,
            "cn": CHECK_NAME,
            "outcome": outcome,
            "ev": EVALUATOR_ID,
        },
    )


def _insert_ledger_v2(
    conn: Connection,
    *,
    claim_logical_id: uuid.UUID,
    state: str,
    transition_reason: str,
) -> uuid.UUID | None:
    candidate_id = uuid.uuid4()
    inserted = conn.execute(
        text(
            """
            INSERT INTO claim_ledger_entries (
              id, claim_logical_id, version_no, state,
              support_scope, user_provided_dependency,
              transition_reason, payload
            ) VALUES (
              :id, :lc, 2, :st,
              'supported_by_user_corpus_only', 'supported_by_user_corpus_only',
              :tr, '{}'::jsonb
            )
            ON CONFLICT (claim_logical_id, version_no) DO NOTHING
            RETURNING id
            """
        ),
        {"id": candidate_id, "lc": claim_logical_id, "st": state, "tr": transition_reason},
    ).first()
    if inserted is None:
        return None
    return uuid.UUID(str(inserted[0]))


def _insert_lineage(
    conn: Connection, *, parent_entry_id: uuid.UUID, child_entry_id: uuid.UUID
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO claim_lineage (
              id, parent_entry_id, child_entry_id, relation_kind
            ) VALUES (
              :id, :p, :c, 'supersedes'
            )
            ON CONFLICT (parent_entry_id, child_entry_id, relation_kind) DO NOTHING
            """
        ),
        {"id": uuid.uuid4(), "p": parent_entry_id, "c": child_entry_id},
    )


def _link_v2_to_evidence(
    conn: Connection,
    *,
    claim_logical_id: uuid.UUID,
    v2_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO claim_evidence_links (
              id, claim_logical_id, claim_ledger_entry_id, evidence_span_id, link_role
            ) VALUES (
              :id, :lc, :le, :es, 'primary_support'
            )
            ON CONFLICT (claim_ledger_entry_id, evidence_span_id) DO NOTHING
            """
        ),
        {"id": uuid.uuid4(), "lc": claim_logical_id, "le": v2_entry_id, "es": evidence_span_id},
    )


def run_cve_lite(conn: Connection, *, task_id: uuid.UUID) -> dict[str, int]:
    """Run CVE-lite over the v1 candidate ledger entries of `task_id`. Idempotent.

    Returns counts dict:
      checked, pass, fail, v2_created
    """
    candidates = _candidate_v1_entries(conn, task_id)
    n_checked = 0
    n_pass = 0
    n_fail = 0
    n_v2_created = 0

    for c in candidates:
        n_checked += 1
        v1_id = uuid.UUID(str(c["entry_id"]))
        logical_id = uuid.UUID(str(c["logical_id"]))
        span_id = uuid.UUID(str(c["span_id"]))
        outcome = _outcome_for(c["quote"], c["quote_hash"], c["chunk_text"])
        _record_verification(
            conn,
            claim_logical_id=logical_id,
            claim_ledger_entry_id=v1_id,
            outcome=outcome,
        )
        if outcome == "pass":
            n_pass += 1
            state = "verified_fact"
            reason = "cve_lite_pass"
        else:
            n_fail += 1
            state = "unverifiable"
            reason = "cve_lite_quote_mismatch"

        if _v2_already_present(conn, logical_id):
            continue

        v2_id = _insert_ledger_v2(
            conn,
            claim_logical_id=logical_id,
            state=state,
            transition_reason=reason,
        )
        if v2_id is None:
            # Lost a race with a concurrent worker; the row exists now.
            continue
        n_v2_created += 1
        _insert_lineage(conn, parent_entry_id=v1_id, child_entry_id=v2_id)
        _link_v2_to_evidence(
            conn,
            claim_logical_id=logical_id,
            v2_entry_id=v2_id,
            evidence_span_id=span_id,
        )

    return {
        "checked": n_checked,
        "pass": n_pass,
        "fail": n_fail,
        "v2_created": n_v2_created,
    }