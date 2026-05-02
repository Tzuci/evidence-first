"""Deterministic, mock-driven Final Answer Gate (Phase 8.4).

After the compiler has produced draft_final_answers v1 with its spans, this
module decides whether to publish.

Span verification rule (CORRECTED in Block 2 realignment):

  A final_answer_span is "verified-backed" if and only if there exists at
  least one final_answer_span_claim_links row such that:

      link.claim_ledger_entry_id == latest_entry_id_for(claim_logical_id)
      AND latest_entry_state_for(claim_logical_id) == 'verified_fact'

  In other words: it is NOT enough that the latest entry of the linked logical
  claim is in state 'verified_fact'. We also require that the link points
  EXACTLY to that latest entry. If the link points to an older entry (e.g. v1
  candidate) and the latest is v2 verified, the span is treated as NOT
  verified-backed: the gate must reject and emit a coverage gap. This is more
  conservative and aligns with the invariant "what the compiler signed off on
  is what we publish".

Decision rules:

  - If the draft has zero final_answer_spans (i.e. the compiler found no
    'verified_fact' claims for the task):
      decision = 'rejected'
      reason_code = 'no_verified_claims'
      coverage_gap_statements row:
          kind = 'missing_evidence'
          severity = 'block'
          gap_key = 'no_verified_claims'
          details = {'reason': 'no verified claims to publish'}

  - Otherwise, apply the verification rule above to every span.

      If every span is verified-backed:
          decision = 'approved'
          reason_code = 'all_spans_verified'
          A published_answers v1 row is inserted with status='published',
          content_hash = sha256(summary_text utf-8).

      Otherwise:
          decision = 'rejected'
          reason_code = 'unverified_spans_present'
          For each uncovered span, a coverage_gap_statements row is emitted:
              kind = 'unverified_claim'
              severity = 'block'
              gap_key = f'span:{final_answer_span_id}'

Phase 8.4 invariants honored:

  - The gate NEVER mutates claim_ledger_entries.
  - The gate NEVER mutates draft_final_answers.
  - All INSERTs use ON CONFLICT DO NOTHING on UNIQUE constraints declared in
    0005_answers_gate.sql:
        agent_runs: (task_id, run_kind, attempt_no)
        coverage_gap_statements: (draft_final_answer_id, kind, gap_key)
        final_gate_reports: (draft_final_answer_id)
        published_answers: (task_id, version_no)
  - Composite FK constraints in 0005 (final_gate_reports_draft_consistency,
    published_answers_draft_consistency, published_answers_gate_consistency)
    enforce DB-level coherence between task_id, draft_id and gate_report_id.
    The gate code does not duplicate those checks.

Returns: dict with the gate outcome:
  {
    "decision": "approved" | "rejected" | "no_draft",
    "reason_code": str,
    "draft_id": str | None,
    "final_gate_report_id": str | None,
    "published_answer_id": str | None,
    "spans_total": int,
    "spans_verified": int,
    "spans_unverified": int,
    "coverage_gaps_emitted": int,
    "agent_run_id": str | None,
  }
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection


GATE_NAME = "mvp0_gate_v1"
GATE_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _select_latest_draft_for_task(
    conn: Connection, task_id: uuid.UUID
) -> dict[str, Any] | None:
    """Return the latest draft_final_answers for the task, or None."""
    row = conn.execute(
        text(
            """
            SELECT id, task_id, version_no, summary_text
            FROM draft_final_answers
            WHERE task_id = :tid
            ORDER BY version_no DESC
            LIMIT 1
            """
        ),
        {"tid": task_id},
    ).first()
    if row is None:
        return None
    return dict(row._mapping)


def _select_spans_with_links(
    conn: Connection, draft_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Return one row per (span, link) pair with the linked entry id, the
    latest entry id for the same claim_logical_id, and the latest state.

    A span without any link will appear with NULL link fields.
    """
    rows = conn.execute(
        text(
            """
            SELECT
              fas.id                AS span_id,
              fas.span_index        AS span_index,
              fascl.id              AS link_id,
              fascl.claim_ledger_entry_id AS linked_entry_id,
              fascl.claim_logical_id AS claim_logical_id,
              latest.id             AS latest_entry_id,
              latest.state          AS latest_entry_state,
              latest.version_no     AS latest_entry_version
            FROM final_answer_spans fas
            LEFT JOIN final_answer_span_claim_links fascl
              ON fascl.final_answer_span_id = fas.id
            LEFT JOIN LATERAL (
              SELECT cle.id, cle.state, cle.version_no
              FROM claim_ledger_entries cle
              WHERE cle.claim_logical_id = fascl.claim_logical_id
              ORDER BY cle.version_no DESC
              LIMIT 1
            ) latest ON fascl.claim_logical_id IS NOT NULL
            WHERE fas.draft_final_answer_id = :did
            ORDER BY fas.span_index ASC, fascl.id ASC
            """
        ),
        {"did": draft_id},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _upsert_gate_run(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
) -> uuid.UUID:
    """Idempotent upsert of an agent_runs row for run_kind='final_answer_gate', attempt_no=1."""
    candidate_id = uuid.uuid4()
    inserted = conn.execute(
        text(
            """
            INSERT INTO agent_runs (
              id, tenant_id, project_id, task_id,
              run_kind, attempt_no, status, payload
            ) VALUES (
              :id, :t, :p, :tid,
              'final_answer_gate', 1, 'succeeded',
              jsonb_build_object('gate_name', CAST(:gn AS TEXT), 'gate_version', CAST(:gv AS TEXT))
            )
            ON CONFLICT (task_id, run_kind, attempt_no) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": candidate_id,
            "t": tenant_id,
            "p": project_id,
            "tid": task_id,
            "gn": GATE_NAME,
            "gv": GATE_VERSION,
        },
    ).first()
    if inserted is not None:
        return uuid.UUID(str(inserted[0]))
    row = conn.execute(
        text(
            """
            SELECT id FROM agent_runs
            WHERE task_id = :tid AND run_kind = 'final_answer_gate' AND attempt_no = 1
            """
        ),
        {"tid": task_id},
    ).one()
    return uuid.UUID(str(row[0]))


def _upsert_coverage_gap(
    conn: Connection,
    *,
    draft_id: uuid.UUID,
    kind: str,
    severity: str,
    gap_key: str,
    details: dict[str, Any],
) -> bool:
    """Idempotent insert of a coverage_gap_statements row.

    UNIQUE constraint coverage_gap_statements_idem_uq is on
    (draft_final_answer_id, kind, gap_key).
    """
    candidate_id = uuid.uuid4()
    inserted = conn.execute(
        text(
            """
            INSERT INTO coverage_gap_statements (
              id, draft_final_answer_id, kind, severity, gap_key, details
            ) VALUES (
              :id, :did, :kind, :sev, :gk, CAST(:dt AS JSONB)
            )
            ON CONFLICT (draft_final_answer_id, kind, gap_key) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": candidate_id,
            "did": draft_id,
            "kind": kind,
            "sev": severity,
            "gk": gap_key,
            "dt": json.dumps(details, sort_keys=True, ensure_ascii=False),
        },
    ).first()
    return inserted is not None


def _upsert_gate_report(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    draft_id: uuid.UUID,
    decision: str,
    reason_code: str,
    payload: dict[str, Any],
) -> tuple[uuid.UUID, bool]:
    """Idempotent insert of final_gate_reports.

    UNIQUE on (draft_final_answer_id) ensures a single report per draft. The
    composite FK final_gate_reports_draft_consistency in 0005 ensures that
    task_id is consistent with the draft.
    """
    candidate_id = uuid.uuid4()
    inserted = conn.execute(
        text(
            """
            INSERT INTO final_gate_reports (
              id, task_id, draft_final_answer_id,
              decision, reason_code, payload
            ) VALUES (
              :id, :tid, :did,
              :dec, :rc, CAST(:pl AS JSONB)
            )
            ON CONFLICT (draft_final_answer_id) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": candidate_id,
            "tid": task_id,
            "did": draft_id,
            "dec": decision,
            "rc": reason_code,
            "pl": json.dumps(payload, sort_keys=True, ensure_ascii=False),
        },
    ).first()
    if inserted is not None:
        return uuid.UUID(str(inserted[0])), True
    row = conn.execute(
        text(
            "SELECT id FROM final_gate_reports WHERE draft_final_answer_id = :did"
        ),
        {"did": draft_id},
    ).one()
    return uuid.UUID(str(row[0])), False


def _upsert_published_answer(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    draft_id: uuid.UUID,
    final_gate_report_id: uuid.UUID,
    summary_text: str,
) -> tuple[uuid.UUID, bool]:
    """Idempotent insert of published_answers v1 with status='published'.

    UNIQUE on (task_id, version_no) ensures a single v1 per task. Composite FKs
    published_answers_draft_consistency and published_answers_gate_consistency
    enforce coherence among task / draft / gate report.
    """
    candidate_id = uuid.uuid4()
    content_hash = hashlib.sha256(summary_text.encode("utf-8")).hexdigest()
    inserted = conn.execute(
        text(
            """
            INSERT INTO published_answers (
              id, task_id, draft_final_answer_id, final_gate_report_id,
              version_no, content_hash, status
            ) VALUES (
              :id, :tid, :did, :fgr,
              1, :ch, 'published'
            )
            ON CONFLICT (task_id, version_no) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": candidate_id,
            "tid": task_id,
            "did": draft_id,
            "fgr": final_gate_report_id,
            "ch": content_hash,
        },
    ).first()
    if inserted is not None:
        return uuid.UUID(str(inserted[0])), True
    row = conn.execute(
        text(
            "SELECT id FROM published_answers WHERE task_id = :tid AND version_no = 1"
        ),
        {"tid": task_id},
    ).one()
    return uuid.UUID(str(row[0])), False


# ---------------------------------------------------------------------------
# public entrypoint
# ---------------------------------------------------------------------------
def run_final_answer_gate(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
) -> dict[str, Any]:
    """Run the deterministic mock-driven gate over the latest draft of `task_id`.

    Idempotent. Returns a dict with the gate outcome (see module docstring).

    Verification rule:
      span verified-backed <=> exists a link with linked_entry_id == latest_entry_id
                               AND latest_entry_state == 'verified_fact'.
    """
    draft = _select_latest_draft_for_task(conn, task_id)
    if draft is None:
        return {
            "decision": "no_draft",
            "reason_code": "draft_missing",
            "draft_id": None,
            "final_gate_report_id": None,
            "published_answer_id": None,
            "spans_total": 0,
            "spans_verified": 0,
            "spans_unverified": 0,
            "coverage_gaps_emitted": 0,
            "agent_run_id": None,
        }

    draft_id = uuid.UUID(str(draft["id"]))
    summary_text: str = str(draft["summary_text"])

    agent_run_id = _upsert_gate_run(
        conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )

    rows = _select_spans_with_links(conn, draft_id)

    # Group rows by span_id (a span may have multiple links).
    # A span is verified-backed only if one of its links satisfies BOTH:
    #   - linked_entry_id == latest_entry_id
    #   - latest_entry_state == 'verified_fact'
    spans: dict[uuid.UUID, dict[str, Any]] = {}
    for r in rows:
        span_id = uuid.UUID(str(r["span_id"]))
        bucket = spans.setdefault(
            span_id,
            {
                "span_id": span_id,
                "span_index": int(r["span_index"]),
                "verified": False,
            },
        )

        linked_entry_id = r.get("linked_entry_id")
        latest_entry_id = r.get("latest_entry_id")
        latest_entry_state = r.get("latest_entry_state")

        # A span without any link cannot be verified-backed.
        if linked_entry_id is None or latest_entry_id is None:
            continue

        # The link must point exactly to the latest entry.
        if str(linked_entry_id) != str(latest_entry_id):
            continue

        # And that latest entry must be in state 'verified_fact'.
        if latest_entry_state == "verified_fact":
            bucket["verified"] = True

    spans_total = len(spans)
    spans_verified = sum(1 for s in spans.values() if s["verified"])
    spans_unverified = spans_total - spans_verified
    coverage_gaps_emitted = 0

    # ----- Branch A: zero spans (zero verified claims at compile time). -----
    if spans_total == 0:
        decision = "rejected"
        reason_code = "no_verified_claims"
        if _upsert_coverage_gap(
            conn,
            draft_id=draft_id,
            kind="missing_evidence",
            severity="block",
            gap_key="no_verified_claims",
            details={"reason": "no verified claims to publish"},
        ):
            coverage_gaps_emitted += 1

        report_id, _ = _upsert_gate_report(
            conn,
            task_id=task_id,
            draft_id=draft_id,
            decision=decision,
            reason_code=reason_code,
            payload={
                "gate_name": GATE_NAME,
                "gate_version": GATE_VERSION,
                "spans_total": 0,
                "spans_verified": 0,
                "spans_unverified": 0,
            },
        )
        return {
            "decision": decision,
            "reason_code": reason_code,
            "draft_id": str(draft_id),
            "final_gate_report_id": str(report_id),
            "published_answer_id": None,
            "spans_total": 0,
            "spans_verified": 0,
            "spans_unverified": 0,
            "coverage_gaps_emitted": coverage_gaps_emitted,
            "agent_run_id": str(agent_run_id),
        }

    # ----- Branch B: at least one span, all verified-backed. -----
    if spans_unverified == 0:
        decision = "approved"
        reason_code = "all_spans_verified"
        report_id, _ = _upsert_gate_report(
            conn,
            task_id=task_id,
            draft_id=draft_id,
            decision=decision,
            reason_code=reason_code,
            payload={
                "gate_name": GATE_NAME,
                "gate_version": GATE_VERSION,
                "spans_total": spans_total,
                "spans_verified": spans_verified,
                "spans_unverified": 0,
            },
        )
        published_id, _ = _upsert_published_answer(
            conn,
            task_id=task_id,
            draft_id=draft_id,
            final_gate_report_id=report_id,
            summary_text=summary_text,
        )
        return {
            "decision": decision,
            "reason_code": reason_code,
            "draft_id": str(draft_id),
            "final_gate_report_id": str(report_id),
            "published_answer_id": str(published_id),
            "spans_total": spans_total,
            "spans_verified": spans_verified,
            "spans_unverified": 0,
            "coverage_gaps_emitted": 0,
            "agent_run_id": str(agent_run_id),
        }

    # ----- Branch C: at least one span, but some are not verified-backed. -----
    decision = "rejected"
    reason_code = "unverified_spans_present"
    for span in spans.values():
        if span["verified"]:
            continue
        gap_key = f"span:{span['span_id']}"
        if _upsert_coverage_gap(
            conn,
            draft_id=draft_id,
            kind="unverified_claim",
            severity="block",
            gap_key=gap_key,
            details={
                "span_id": str(span["span_id"]),
                "span_index": int(span["span_index"]),
                "reason": (
                    "no link of this span points to the latest "
                    "claim_ledger_entries with state='verified_fact'"
                ),
            },
        ):
            coverage_gaps_emitted += 1

    report_id, _ = _upsert_gate_report(
        conn,
        task_id=task_id,
        draft_id=draft_id,
        decision=decision,
        reason_code=reason_code,
        payload={
            "gate_name": GATE_NAME,
            "gate_version": GATE_VERSION,
            "spans_total": spans_total,
            "spans_verified": spans_verified,
            "spans_unverified": spans_unverified,
        },
    )
    return {
        "decision": decision,
        "reason_code": reason_code,
        "draft_id": str(draft_id),
        "final_gate_report_id": str(report_id),
        "published_answer_id": None,
        "spans_total": spans_total,
        "spans_verified": spans_verified,
        "spans_unverified": spans_unverified,
        "coverage_gaps_emitted": coverage_gaps_emitted,
        "agent_run_id": str(agent_run_id),
    }
