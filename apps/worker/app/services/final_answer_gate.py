"""Deterministic, mock-driven Final Answer Gate (Phase 8.4 + Phase 8.7G).

After the compiler has produced draft_final_answers v1 with its spans, this
module decides whether to publish.

Span verification rule (Phase 8.4, invariata):

  A final_answer_span is "verified-backed" if and only if there exists at
  least one final_answer_span_claim_links row such that:

      link.claim_ledger_entry_id == latest_entry_id_for(claim_logical_id)
      AND latest_entry_state_for(claim_logical_id) == 'verified_fact'

  In other words: it is NOT enough that the latest entry of the linked logical
  claim is in state 'verified_fact'. We also require that the link points
  EXACTLY to that latest entry. If the link points to an older entry (e.g. v1
  candidate) and the latest is v2 verified, the span is treated as NOT
  verified-backed: the gate must reject and emit a coverage gap.

Decision rules (8.4 invariate + 8.7G aggiunta):

  - If the draft has zero final_answer_spans:
      decision = 'rejected', reason_code = 'no_verified_claims',
      coverage_gap_statements: kind='missing_evidence', severity='block',
      gap_key='no_verified_claims'.

  - If at least one span is NOT verified-backed (CVE-lite priority):
      decision = 'rejected', reason_code = 'unverified_spans_present',
      one coverage_gap_statements per uncovered span:
        kind='unverified_claim', severity='block', gap_key=f'span:{id}'.
      *** Source Quality is NOT consulted in this branch. ***

  - Otherwise (all spans verified-backed), Phase 8.7G applies:
      Per each verified-backed span, the Gate consults
      source_quality_assessments for the latest assessment of each
      evidence_span supporting the span (via claim_evidence_links from
      the latest claim_ledger_entries with state='verified_fact').

      Aggregation rule per span (worst-on-block, any-on-warn):
        - if ANY supporting evidence_span has a BLOCK condition -> block;
        - else if ANY supporting evidence_span has a WARNING condition -> warn;
        - else -> clean.

      Block conditions on the LATEST assessment:
        - overall_quality = 'unsuitable'                           (reason: 'source_quality_unsuitable')
        - contradiction_status = 'contradicted_by_stronger_source' (reason: 'source_quality_contradicted_by_stronger_source')
        - contradiction_status = 'conflicting_sources'             (reason: 'source_quality_conflicting_sources')

      Warning conditions (only if no block condition fires on the same
      evidence_span):
        - overall_quality = 'weak'                  (reason: 'source_quality_weak')
        - overall_quality = 'unknown'               (reason: 'source_quality_unknown')
        - contradiction_status = 'unchecked'        (reason: 'source_quality_contradiction_unchecked')
        - latest assessment missing                  (reason: 'source_quality_missing_assessment')

      Decision:
        - if ANY span is block -> decision='rejected',
          reason_code='source_quality_block', emit kind='source_quality_block'
          (severity='block') per blocked span; emit kind='source_quality_warning'
          (severity='warn') per warning span (audit value).
          NO published_answers v1 is inserted.
        - else if ANY span has a warning (and NO span is blocked) ->
          decision='approved', reason_code='all_spans_verified_with_warnings',
          emit kind='source_quality_warning' (severity='warn') per warning span,
          published_answers v1 inserted (decision is NOT changed by warnings).
        - else -> decision='approved', reason_code='all_spans_verified'
          (original 8.4 path, unchanged).

Phase 8.7G priority invariant:
  CVE-lite (verified-backed) > Source Quality.
  The Source Quality branch runs ONLY when every span is verified-backed.
  A span that is not verified-backed produces 'unverified_spans_present'
  and the gate rejects WITHOUT consulting source_quality_assessments.

Phase 8.7G invariants honored:
  - The gate NEVER mutates claim_ledger_entries.
  - The gate NEVER mutates draft_final_answers.
  - The gate NEVER mutates source_quality_assessments (read-only SELECT only).
  - All INSERTs use ON CONFLICT DO NOTHING on UNIQUE constraints declared
    in 0005_answers_gate.sql and the extended kind codomain from
    0008_coverage_gap_source_quality.sql:
        agent_runs:               (task_id, run_kind, attempt_no)
        coverage_gap_statements:  (draft_final_answer_id, kind, gap_key)
        final_gate_reports:       (draft_final_answer_id)
        published_answers:        (task_id, version_no)
  - Idempotent under redelivery: a second invocation on the same draft
    yields the same decision and does not duplicate gap/report/published
    rows.
  - latest assessment per evidence_span resolved via
    ORDER BY version_no DESC, created_at DESC, id DESC LIMIT 1
    (DB-level absolute latest, not API-level slice latest).
  - details JSONB never contains stack traces. It contains motivation,
    evidence_span_id, assessment_id (when present), overall_quality,
    contradiction_status, and policy classification.

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
# Source Quality policy classification (Phase 8.7G, P1+P3+P4)
# ---------------------------------------------------------------------------
# Block reasons fire when the LATEST assessment for an evidence_span exhibits
# any of these conditions. They cause the Gate to reject the publication of
# the span supported by that evidence_span.
_SOURCE_QUALITY_BLOCK_REASONS_BY_OVERALL_QUALITY = {
    "unsuitable": "source_quality_unsuitable",
}
_SOURCE_QUALITY_BLOCK_REASONS_BY_CONTRADICTION_STATUS = {
    "contradicted_by_stronger_source": "source_quality_contradicted_by_stronger_source",
    "conflicting_sources": "source_quality_conflicting_sources",
}

# Warning reasons fire when the LATEST assessment exists but the source is
# weak/unknown/unchecked, OR when no assessment exists at all. Warnings are
# non-blocking: the Gate's decision is not changed by warnings alone.
_SOURCE_QUALITY_WARNING_REASONS_BY_OVERALL_QUALITY = {
    "weak": "source_quality_weak",
    "unknown": "source_quality_unknown",
}
_SOURCE_QUALITY_WARNING_REASONS_BY_CONTRADICTION_STATUS = {
    "unchecked": "source_quality_contradiction_unchecked",
}
# Sentinel reason for the "no latest assessment exists" case. The Gate emits
# a warning (not a block) so a task that was processed before the 8.7E
# integration, or one whose 8.7E run failed, can still reach 'published'
# (coherent with §6 of PHASE_8_7G_PRE.md).
_SOURCE_QUALITY_REASON_MISSING_ASSESSMENT = "source_quality_missing_assessment"

# Policy identity stamped into coverage_gap_statements.details so a future
# audit can correlate the gap with the exact policy that produced it. We
# bump these when the classification matrix changes (e.g. P2 is enabled).
_SOURCE_QUALITY_POLICY_NAME = "mvp0_source_quality_gate_policy"
_SOURCE_QUALITY_POLICY_VERSION = "0.1.0"


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


def _select_source_quality_per_span(
    conn: Connection, draft_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Return one row per (span, evidence_span_id) pair joined with the
    LATEST source_quality_assessments for that evidence_span.

    "Latest" is the absolute latest at DB level, resolved via
    ``ORDER BY version_no DESC, created_at DESC, id DESC LIMIT 1`` over
    source_quality_assessments for the given evidence_span_id. This is
    intentionally different from the API-level "latest in slice"
    semantics exposed by the 8.7F read endpoints.

    Only links that point to the LATEST claim_ledger_entries entry with
    state='verified_fact' participate: this matches the regola 8.4
    "verified-backed" rule, so the source quality policy only applies
    to evidence that effectively backs a verified span. Links pointing
    to older entries are silently filtered out — those spans are
    already rejected upstream by the 'unverified_spans_present'
    branch.

    A span supported by multiple evidence_span ids will appear in
    multiple rows; the caller aggregates them via worst-on-block,
    any-on-warn (see _classify_source_quality_per_span).

    A span with no supporting evidence_span returns no rows in this
    result set. The caller decides the policy for such spans; in 8.7G
    we treat them as a missing-assessment warning so the Gate is
    consistent with the "no source data implies uncertainty"
    interpretation.

    If a source_quality_assessments row exists but the LATERAL join
    returns no match (no assessment for that evidence_span), both
    sqa_* columns are NULL: the caller maps this to the
    'source_quality_missing_assessment' warning.
    """
    rows = conn.execute(
        text(
            """
            SELECT
              fas.id                       AS span_id,
              fas.span_index               AS span_index,
              cel.evidence_span_id         AS evidence_span_id,
              sqa_latest.id                AS sqa_id,
              sqa_latest.overall_quality   AS sqa_overall_quality,
              sqa_latest.contradiction_status AS sqa_contradiction_status,
              sqa_latest.version_no        AS sqa_version_no
            FROM final_answer_spans fas
            JOIN final_answer_span_claim_links fascl
              ON fascl.final_answer_span_id = fas.id
            JOIN claim_ledger_entries cle
              ON cle.id = fascl.claim_ledger_entry_id
            JOIN LATERAL (
              SELECT cle_latest.id, cle_latest.state, cle_latest.version_no
              FROM claim_ledger_entries cle_latest
              WHERE cle_latest.claim_logical_id = fascl.claim_logical_id
              ORDER BY cle_latest.version_no DESC
              LIMIT 1
            ) latest_entry ON TRUE
            JOIN claim_evidence_links cel
              ON cel.claim_ledger_entry_id = cle.id
            LEFT JOIN LATERAL (
              SELECT sqa.id,
                     sqa.overall_quality,
                     sqa.contradiction_status,
                     sqa.version_no
              FROM source_quality_assessments sqa
              WHERE sqa.evidence_span_id = cel.evidence_span_id
              ORDER BY sqa.version_no DESC, sqa.created_at DESC, sqa.id DESC
              LIMIT 1
            ) sqa_latest ON TRUE
            WHERE fas.draft_final_answer_id = :did
              AND cel.evidence_span_id IS NOT NULL
              AND cle.id = latest_entry.id
              AND latest_entry.state = 'verified_fact'
            ORDER BY fas.span_index ASC, cel.evidence_span_id ASC
            """
        ),
        {"did": draft_id},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _classify_source_quality_per_span(
    sq_rows: list[dict[str, Any]],
    verified_span_ids: set[uuid.UUID],
) -> dict[uuid.UUID, dict[str, Any]]:
    """Classify each verified-backed span into clean / warning / block
    based on the LATEST source_quality_assessments rows of its
    supporting evidence_span ids.

    Aggregation rules (Phase 8.7G, P1+P3+P4):
      - worst-on-block: if ANY supporting evidence_span produces a block
        condition, the span is BLOCKED;
      - any-on-warn:    else if ANY produces a warning condition (or has
        no latest assessment), the span carries a WARNING;
      - else:           clean.

    Each evidence_span contributes either to block_reasons or to
    warning_reasons, never both: block conditions take precedence over
    warning conditions on the same evidence_span.

    A verified-backed span with no rows in sq_rows (no supporting
    evidence_span at all) is treated as a missing-assessment WARNING.
    This is conservative and consistent with §6 of PHASE_8_7G_PRE.md:
    "non bloccare ma loggare quando manca informazione strutturale".

    Returns a dict keyed by span_id, with values shaped as:
      {
        "block_reasons":   [{reason_code, evidence_span_id, ...}, ...],
        "warning_reasons": [{reason_code, evidence_span_id, ...}, ...],
        "per_evidence":    [{evidence_span_id, sqa_id, overall_quality,
                             contradiction_status, classification}, ...],
      }

    Determinism: the order of reason entries within a span follows the
    natural order of sq_rows (already sorted by span_index then
    evidence_span_id at SQL level), so the gap.details JSONB is
    stable across redeliveries.
    """
    result: dict[uuid.UUID, dict[str, Any]] = {}

    # Index sq_rows by span_id, in deterministic order.
    rows_by_span: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for r in sq_rows:
        span_id = uuid.UUID(str(r["span_id"]))
        rows_by_span.setdefault(span_id, []).append(r)

    for span_id in verified_span_ids:
        spans_rows = rows_by_span.get(span_id, [])

        block_reasons: list[dict[str, Any]] = []
        warning_reasons: list[dict[str, Any]] = []
        per_evidence: list[dict[str, Any]] = []

        if not spans_rows:
            # A verified-backed span with NO supporting evidence_span.
            # This is anomalous given the 8.4 invariants (a span is
            # verified-backed only via its claim's evidence) but we
            # treat it defensively as a missing-assessment warning
            # rather than crashing or silently approving.
            warning_reasons.append(
                {
                    "reason_code": _SOURCE_QUALITY_REASON_MISSING_ASSESSMENT,
                    "evidence_span_id": None,
                    "assessment_id": None,
                    "overall_quality": None,
                    "contradiction_status": None,
                }
            )
            result[span_id] = {
                "block_reasons": block_reasons,
                "warning_reasons": warning_reasons,
                "per_evidence": per_evidence,
            }
            continue

        for r in spans_rows:
            ev_id = r["evidence_span_id"]
            ev_id_str = str(ev_id) if ev_id is not None else None
            sqa_id = r["sqa_id"]
            sqa_id_str = str(sqa_id) if sqa_id is not None else None
            oq = r["sqa_overall_quality"]
            cs = r["sqa_contradiction_status"]

            classification: str  # "clean" | "warning" | "block"

            if sqa_id is None:
                # No latest source_quality_assessments row exists for
                # this evidence_span (LEFT JOIN LATERAL returned NULL).
                # Maps to a missing-assessment WARNING.
                warning_reasons.append(
                    {
                        "reason_code": _SOURCE_QUALITY_REASON_MISSING_ASSESSMENT,
                        "evidence_span_id": ev_id_str,
                        "assessment_id": None,
                        "overall_quality": None,
                        "contradiction_status": None,
                    }
                )
                classification = "warning"
            else:
                # An assessment exists: walk the policy matrix. Block
                # conditions are checked FIRST so a single
                # evidence_span never appears in both bucket lists.
                oq_str = str(oq) if oq is not None else None
                cs_str = str(cs) if cs is not None else None

                block_reason_code: str | None = None
                if oq_str in _SOURCE_QUALITY_BLOCK_REASONS_BY_OVERALL_QUALITY:
                    block_reason_code = (
                        _SOURCE_QUALITY_BLOCK_REASONS_BY_OVERALL_QUALITY[oq_str]
                    )
                elif (
                    cs_str
                    in _SOURCE_QUALITY_BLOCK_REASONS_BY_CONTRADICTION_STATUS
                ):
                    block_reason_code = (
                        _SOURCE_QUALITY_BLOCK_REASONS_BY_CONTRADICTION_STATUS[cs_str]
                    )

                if block_reason_code is not None:
                    block_reasons.append(
                        {
                            "reason_code": block_reason_code,
                            "evidence_span_id": ev_id_str,
                            "assessment_id": sqa_id_str,
                            "overall_quality": oq_str,
                            "contradiction_status": cs_str,
                        }
                    )
                    classification = "block"
                else:
                    # Not blocked: check for warning conditions. Note
                    # that a single evidence_span can match multiple
                    # warning conditions (e.g. weak + unchecked); we
                    # emit one warning entry per condition matched so
                    # the audit captures the full picture.
                    matched_warning = False
                    if (
                        oq_str
                        in _SOURCE_QUALITY_WARNING_REASONS_BY_OVERALL_QUALITY
                    ):
                        warning_reasons.append(
                            {
                                "reason_code": (
                                    _SOURCE_QUALITY_WARNING_REASONS_BY_OVERALL_QUALITY[
                                        oq_str
                                    ]
                                ),
                                "evidence_span_id": ev_id_str,
                                "assessment_id": sqa_id_str,
                                "overall_quality": oq_str,
                                "contradiction_status": cs_str,
                            }
                        )
                        matched_warning = True
                    if (
                        cs_str
                        in _SOURCE_QUALITY_WARNING_REASONS_BY_CONTRADICTION_STATUS
                    ):
                        warning_reasons.append(
                            {
                                "reason_code": (
                                    _SOURCE_QUALITY_WARNING_REASONS_BY_CONTRADICTION_STATUS[
                                        cs_str
                                    ]
                                ),
                                "evidence_span_id": ev_id_str,
                                "assessment_id": sqa_id_str,
                                "overall_quality": oq_str,
                                "contradiction_status": cs_str,
                            }
                        )
                        matched_warning = True
                    classification = "warning" if matched_warning else "clean"

            per_evidence.append(
                {
                    "evidence_span_id": ev_id_str,
                    "assessment_id": sqa_id_str,
                    "overall_quality": (
                        str(oq) if oq is not None else None
                    ),
                    "contradiction_status": (
                        str(cs) if cs is not None else None
                    ),
                    "classification": classification,
                }
            )

        result[span_id] = {
            "block_reasons": block_reasons,
            "warning_reasons": warning_reasons,
            "per_evidence": per_evidence,
        }

    return result


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
    """Idempotent insert of final_gate_reports."""
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
    """Idempotent insert of published_answers v1 with status='published'."""
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
# Source Quality emission helpers (Phase 8.7G)
# ---------------------------------------------------------------------------
def _emit_source_quality_block_gap(
    conn: Connection,
    *,
    draft_id: uuid.UUID,
    span_id: uuid.UUID,
    span_index: int,
    block_reasons: list[dict[str, Any]],
    per_evidence: list[dict[str, Any]],
) -> bool:
    """Insert one coverage_gap_statements row of kind='source_quality_block'.

    gap_key is deterministic and idempotent under redelivery:
      f'span:{span_id}:source_quality_block'

    details JSONB never includes stack traces. It contains:
      - span_id (string, for cross-reference);
      - span_index (integer);
      - reasons (list of structured dicts: reason_code, evidence_span_id,
        assessment_id, overall_quality, contradiction_status);
      - per_evidence (list of full per-evidence_span classifications);
      - policy (name + version of the classifier matrix).

    Returns True if a new gap row was inserted, False if the gap was
    already present (idempotent re-run).
    """
    return _upsert_coverage_gap(
        conn,
        draft_id=draft_id,
        kind="source_quality_block",
        severity="block",
        gap_key=f"span:{span_id}:source_quality_block",
        details={
            "span_id": str(span_id),
            "span_index": int(span_index),
            "reasons": block_reasons,
            "per_evidence": per_evidence,
            "policy": {
                "name": _SOURCE_QUALITY_POLICY_NAME,
                "version": _SOURCE_QUALITY_POLICY_VERSION,
            },
        },
    )


def _emit_source_quality_warning_gap(
    conn: Connection,
    *,
    draft_id: uuid.UUID,
    span_id: uuid.UUID,
    span_index: int,
    warning_reasons: list[dict[str, Any]],
    per_evidence: list[dict[str, Any]],
) -> bool:
    """Insert one coverage_gap_statements row of kind='source_quality_warning'.

    gap_key is deterministic and idempotent under redelivery:
      f'span:{span_id}:source_quality_warning'

    severity is 'warn': the Gate decision is NOT changed by this gap.
    """
    return _upsert_coverage_gap(
        conn,
        draft_id=draft_id,
        kind="source_quality_warning",
        severity="warn",
        gap_key=f"span:{span_id}:source_quality_warning",
        details={
            "span_id": str(span_id),
            "span_index": int(span_index),
            "reasons": warning_reasons,
            "per_evidence": per_evidence,
            "policy": {
                "name": _SOURCE_QUALITY_POLICY_NAME,
                "version": _SOURCE_QUALITY_POLICY_VERSION,
            },
        },
    )


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

    Verification rule (8.4 invariata):
      span verified-backed <=> exists a link with linked_entry_id == latest_entry_id
                               AND latest_entry_state == 'verified_fact'.

    Source Quality policy (Phase 8.7G, P1+P3+P4): applied AFTER the 8.4
    verification rule, ONLY when all spans are verified-backed. See module
    docstring for the full decision table and PHASE_8_7G_PRE.md §6 for the
    rationale.
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

    # ----- Branch C: at least one span, but some are not verified-backed. -----
    #
    # CRITICAL PRIORITY (Phase 8.7G, §8.4 of PHASE_8_7G_PRE.md):
    # CVE-lite > Source Quality. When any span is not verified-backed, the
    # Gate rejects with 'unverified_spans_present' WITHOUT consulting
    # source_quality_assessments. This invariant must be preserved.
    if spans_unverified > 0:
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

    # ----- Phase 8.7G: all spans are verified-backed. Consult Source Quality. -----
    #
    # We now ask: for each verified-backed span, are its supporting
    # evidence_span sources structurally adequate? The policy classifies
    # each span as clean / warning / block based on the LATEST source
    # quality assessment of each supporting evidence_span.
    verified_span_ids = {sid for sid, s in spans.items() if s["verified"]}
    sq_rows = _select_source_quality_per_span(conn, draft_id)
    sq_per_span = _classify_source_quality_per_span(sq_rows, verified_span_ids)

    blocked_spans: list[uuid.UUID] = []
    warning_spans: list[uuid.UUID] = []
    for sid in verified_span_ids:
        classification = sq_per_span.get(sid, {})
        if classification.get("block_reasons"):
            blocked_spans.append(sid)
        elif classification.get("warning_reasons"):
            warning_spans.append(sid)

    # Deterministic ordering for downstream emission (smaller span_index first).
    blocked_spans.sort(key=lambda sid: spans[sid]["span_index"])
    warning_spans.sort(key=lambda sid: spans[sid]["span_index"])

    # ----- Branch C' (8.7G): at least one span is BLOCKED by source quality. -----
    if blocked_spans:
        decision = "rejected"
        reason_code = "source_quality_block"

        # Emit block gaps for blocked spans.
        for sid in blocked_spans:
            cls = sq_per_span[sid]
            if _emit_source_quality_block_gap(
                conn,
                draft_id=draft_id,
                span_id=sid,
                span_index=spans[sid]["span_index"],
                block_reasons=cls["block_reasons"],
                per_evidence=cls["per_evidence"],
            ):
                coverage_gaps_emitted += 1

        # Also emit warning gaps for non-blocked spans that carry warnings:
        # they document the residual uncertainty even though the report is
        # rejected, and they keep the audit trail uniform with the approved
        # branch below.
        for sid in warning_spans:
            cls = sq_per_span[sid]
            if _emit_source_quality_warning_gap(
                conn,
                draft_id=draft_id,
                span_id=sid,
                span_index=spans[sid]["span_index"],
                warning_reasons=cls["warning_reasons"],
                per_evidence=cls["per_evidence"],
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
                "spans_unverified": 0,
                "source_quality_summary": {
                    "blocked_spans": len(blocked_spans),
                    "warning_spans": len(warning_spans),
                    "clean_spans": (
                        spans_verified - len(blocked_spans) - len(warning_spans)
                    ),
                    "policy_name": _SOURCE_QUALITY_POLICY_NAME,
                    "policy_version": _SOURCE_QUALITY_POLICY_VERSION,
                },
            },
        )
        # No published_answers v1 in the rejected branch.
        return {
            "decision": decision,
            "reason_code": reason_code,
            "draft_id": str(draft_id),
            "final_gate_report_id": str(report_id),
            "published_answer_id": None,
            "spans_total": spans_total,
            "spans_verified": spans_verified,
            "spans_unverified": 0,
            "coverage_gaps_emitted": coverage_gaps_emitted,
            "agent_run_id": str(agent_run_id),
        }

    # ----- Branch B' (8.7G): only warnings, no blocks. Approved with warnings. -----
    if warning_spans:
        decision = "approved"
        reason_code = "all_spans_verified_with_warnings"

        for sid in warning_spans:
            cls = sq_per_span[sid]
            if _emit_source_quality_warning_gap(
                conn,
                draft_id=draft_id,
                span_id=sid,
                span_index=spans[sid]["span_index"],
                warning_reasons=cls["warning_reasons"],
                per_evidence=cls["per_evidence"],
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
                "spans_unverified": 0,
                "source_quality_summary": {
                    "blocked_spans": 0,
                    "warning_spans": len(warning_spans),
                    "clean_spans": spans_verified - len(warning_spans),
                    "policy_name": _SOURCE_QUALITY_POLICY_NAME,
                    "policy_version": _SOURCE_QUALITY_POLICY_VERSION,
                },
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
            "coverage_gaps_emitted": coverage_gaps_emitted,
            "agent_run_id": str(agent_run_id),
        }

    # ----- Branch B (8.4 invariata): all spans clean, no source quality issues. -----
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
