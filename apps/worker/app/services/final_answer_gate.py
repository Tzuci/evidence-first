"""Deterministic, mock-driven Final Answer Gate (Phase 8.4 + Phase 8.7G + Phase 8.8A-GATE).

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

Decision rules (8.4 invariate + 8.7G + 8.8A-GATE):

  - If the draft has zero final_answer_spans:
      decision = 'rejected', reason_code = 'no_verified_claims',
      coverage_gap_statements: kind='missing_evidence', severity='block',
      gap_key='no_verified_claims'.

  - If at least one span is NOT verified-backed (CVE-lite priority):
      decision = 'rejected', reason_code = 'unverified_spans_present',
      one coverage_gap_statements per uncovered span:
        kind='unverified_claim', severity='block', gap_key=f'span:{id}'.
      *** Source Quality and Entailment are NOT consulted in this branch. ***

  - Otherwise (all spans verified-backed), Phase 8.7G + 8.8A-GATE applies.

    Per each verified-backed span, the Gate consults:
      (a) source_quality_assessments for the latest assessment of each
          evidence_span supporting the span;
      (b) claim_entailment_checks for the latest entailment check for each
          (claim_ledger_entry_id, evidence_span_id) pair supporting the span.

    Source Quality classification (8.7G, P1+P3+P4):
      Block conditions (LATEST assessment):
        - overall_quality = 'unsuitable'
        - contradiction_status = 'contradicted_by_stronger_source'
        - contradiction_status = 'conflicting_sources'
      Warning conditions:
        - overall_quality = 'weak' / 'unknown'
        - contradiction_status = 'unchecked'
        - latest assessment missing

    Claim Entailment classification (8.8A-GATE, P1 — MVP-0):
      Block conditions (LATEST entailment check):
        - verdict = 'contradicted'
      Warning conditions:
        - verdict = 'not_supported'
        - verdict = 'partially_supported'
        - verdict = 'uncertain'
        - latest entailment check missing
      Clean:
        - verdict = 'entailed'

    Aggregation rule per span (worst-on-block, any-on-warn): same pattern as
    Source Quality, applied independently to the entailment axis.

  Priority and decision (PHASE_8_8A_GATE_PRE.md §7):

      1. no_verified_claims                       (Branch A)
      2. unverified_spans_present / CVE-lite      (Branch C)
      3. entailment_block                         (Branch E — 8.8A-GATE, NEW)
      4. source_quality_block                     (Branch C' — 8.7G)
      5. approved_with_warnings                   (Branch W)
      6. approved_clean                           (Branch B)

    Decisions:
      - ANY span with entailment block -> decision='rejected',
        reason_code='entailment_block'.
        ALSO emit source_quality_block / source_quality_warning gaps for
        the same draft (audit completeness), but reason_code stays
        'entailment_block'.
      - else if ANY span with source quality block -> decision='rejected',
        reason_code='source_quality_block'.
        ALSO emit entailment_warning gaps if present (audit completeness),
        but reason_code stays 'source_quality_block'.
      - else if ANY span has any warning (entailment or SQ) ->
        decision='approved', reason_code='all_spans_verified_with_warnings'.
        Emit entailment_warning and/or source_quality_warning gaps.
        published_answers v1 inserted.
      - else -> decision='approved', reason_code='all_spans_verified'
        (original 8.4 path).

Phase 8.8A-GATE priority invariant:
  CVE-lite (verified-backed) > Claim Entailment > Source Quality.
  The entailment branch runs ONLY when every span is verified-backed.
  A span that is not verified-backed produces 'unverified_spans_present'
  and the gate rejects WITHOUT consulting either source_quality_assessments
  or claim_entailment_checks.
  Entailment block takes precedence over source_quality block when both
  fire on the same draft, but both kinds of gap are always emitted for
  full audit visibility.

Phase 8.8A-GATE invariants honored:
  - The gate NEVER mutates claim_ledger_entries.
  - The gate NEVER mutates draft_final_answers.
  - The gate NEVER mutates source_quality_assessments (read-only SELECT).
  - The gate NEVER mutates claim_entailment_checks (read-only SELECT).
  - All INSERTs use ON CONFLICT DO NOTHING on UNIQUE constraints declared
    in 0005_answers_gate.sql, 0008_coverage_gap_source_quality.sql, and
    the extended kind codomain from 0010_coverage_gap_entailment.sql:
        agent_runs:               (task_id, run_kind, attempt_no)
        coverage_gap_statements:  (draft_final_answer_id, kind, gap_key)
        final_gate_reports:       (draft_final_answer_id)
        published_answers:        (task_id, version_no)
  - Idempotent under redelivery: a second invocation on the same draft
    yields the same decision and does not duplicate gap/report/published
    rows.
  - latest entailment check per (entry, span) resolved via
    ORDER BY version_no DESC, created_at DESC, id DESC LIMIT 1
    (DB-level absolute latest, deterministic tie-breaker).
  - latest source quality assessment per evidence_span resolved via
    ORDER BY version_no DESC, created_at DESC, id DESC LIMIT 1.
  - details JSONB never contains stack traces, claim text, or quote text.
    It contains motivation, identifiers (entry_id, evidence_span_id,
    assessment_id, entailment_check_id), verdicts, confidence values,
    checker / policy provenance, and the policy stamp.

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
# Sentinel reason for the "no latest assessment exists" case.
_SOURCE_QUALITY_REASON_MISSING_ASSESSMENT = "source_quality_missing_assessment"

# Policy identity stamped into coverage_gap_statements.details so a future
# audit can correlate the gap with the exact policy that produced it.
_SOURCE_QUALITY_POLICY_NAME = "mvp0_source_quality_gate_policy"
_SOURCE_QUALITY_POLICY_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Claim Entailment policy classification (Phase 8.8A-GATE, P1 — MVP-0)
# ---------------------------------------------------------------------------
# Block reasons fire when the LATEST entailment check for a (claim_ledger_entry,
# evidence_span) pair has one of these verdicts. They cause the Gate to reject
# the publication of the span supported by that pair.
#
# MVP-0 policy P1: ONLY 'contradicted' blocks. 'not_supported',
# 'partially_supported', 'uncertain', and missing check produce warnings.
# Rationale: the mock checker is too weak to justify blocking on
# 'not_supported'; a future real NLI checker may upgrade this policy
# (bumping mvp0_entailment_gate_policy version) without altering this code's
# structure.
_ENTAILMENT_BLOCK_REASONS_BY_VERDICT = {
    "contradicted": "entailment_contradicted",
}

# Warning reasons fire when the LATEST entailment check exists with a verdict
# that does not establish support, OR when no check exists at all.
_ENTAILMENT_WARNING_REASONS_BY_VERDICT = {
    "not_supported": "entailment_not_supported",
    "partially_supported": "entailment_partially_supported",
    "uncertain": "entailment_uncertain",
}
# Sentinel reason for the "no latest entailment check exists" case. The Gate
# emits a warning (not a block) so a task processed before the 8.8A-WORKER
# integration, or one whose 8.8A run failed, can still reach 'published'.
_ENTAILMENT_REASON_MISSING_CHECK = "entailment_missing_check"

# Policy identity stamped into coverage_gap_statements.details and into
# final_gate_reports.payload so a future audit can correlate the gap with
# the exact policy that produced it. Bump these when the classification
# matrix changes (e.g. P2 'not_supported -> block' is enabled with a real
# checker).
_ENTAILMENT_POLICY_NAME = "mvp0_entailment_gate_policy"
_ENTAILMENT_POLICY_VERSION = "0.1.0"


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
    source_quality_assessments for the given evidence_span_id.

    Only links that point to the LATEST claim_ledger_entries entry with
    state='verified_fact' participate: this matches the regola 8.4
    "verified-backed" rule.
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


def _select_entailment_per_span(
    conn: Connection, draft_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Return one row per (span, claim_ledger_entry_id, evidence_span_id)
    triple joined with the LATEST claim_entailment_checks for that pair
    (Phase 8.8A-GATE).

    "Latest" is the absolute latest at DB level, resolved via
    ``ORDER BY version_no DESC, created_at DESC, id DESC LIMIT 1`` over
    claim_entailment_checks for the given (claim_ledger_entry_id,
    evidence_span_id) pair.

    Only links that point to the LATEST claim_ledger_entries entry with
    state='verified_fact' participate: this matches the regola 8.4
    "verified-backed" rule. Spans that are not verified-backed are
    already handled by the upstream Branch C (unverified_spans_present)
    and never reach the entailment branch.

    A span supported by multiple (entry, evidence_span) pairs will appear
    in multiple rows; the caller aggregates them via worst-on-block,
    any-on-warn (see _classify_entailment_per_span).

    Mirrors _select_source_quality_per_span but with two key
    differences:
      - the JOIN key is the PAIR (claim_ledger_entry_id, evidence_span_id),
        not just evidence_span_id (entailment granularity is the pair,
        per PHASE_8_8A_PRE.md §3);
      - the result row carries ``cec_checker_name``, ``cec_checker_version``,
        ``cec_policy_name``, ``cec_policy_version`` and ``cec_mock`` so the
        downstream classifier can stamp these into the gap details for
        audit / calibration purposes.

    If a claim_entailment_checks row exists but the LATERAL join returns
    no match (no check for that pair), all cec_* columns are NULL: the
    caller maps this to the 'entailment_missing_check' warning.
    """
    rows = conn.execute(
        text(
            """
            SELECT
              fas.id                       AS span_id,
              fas.span_index               AS span_index,
              cle.id                       AS claim_ledger_entry_id,
              cel.evidence_span_id         AS evidence_span_id,
              cec_latest.id                AS cec_id,
              cec_latest.verdict           AS cec_verdict,
              cec_latest.confidence        AS cec_confidence,
              cec_latest.version_no        AS cec_version_no,
              cec_latest.checker_name      AS cec_checker_name,
              cec_latest.checker_version   AS cec_checker_version,
              cec_latest.policy_name       AS cec_policy_name,
              cec_latest.policy_version    AS cec_policy_version,
              cec_latest.payload           AS cec_payload
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
              SELECT cec.id,
                     cec.verdict,
                     cec.confidence,
                     cec.version_no,
                     cec.checker_name,
                     cec.checker_version,
                     cec.policy_name,
                     cec.policy_version,
                     cec.payload
              FROM claim_entailment_checks cec
              WHERE cec.claim_ledger_entry_id = cel.claim_ledger_entry_id
                AND cec.evidence_span_id      = cel.evidence_span_id
              ORDER BY cec.version_no DESC, cec.created_at DESC, cec.id DESC
              LIMIT 1
            ) cec_latest ON TRUE
            WHERE fas.draft_final_answer_id = :did
              AND cel.evidence_span_id IS NOT NULL
              AND cle.id = latest_entry.id
              AND latest_entry.state = 'verified_fact'
            ORDER BY fas.span_index ASC,
                     cel.claim_ledger_entry_id ASC,
                     cel.evidence_span_id ASC
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

    A verified-backed span with no rows in sq_rows is treated as a
    missing-assessment WARNING (defensive).

    Returns a dict keyed by span_id, with values shaped as:
      {
        "block_reasons":   [{reason_code, evidence_span_id, ...}, ...],
        "warning_reasons": [{reason_code, evidence_span_id, ...}, ...],
        "per_evidence":    [{evidence_span_id, sqa_id, overall_quality,
                             contradiction_status, classification}, ...],
      }
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


def _classify_entailment_per_span(
    ec_rows: list[dict[str, Any]],
    verified_span_ids: set[uuid.UUID],
) -> dict[uuid.UUID, dict[str, Any]]:
    """Classify each verified-backed span into clean / warning / block
    based on the LATEST claim_entailment_checks rows of its supporting
    (claim_ledger_entry_id, evidence_span_id) pairs (Phase 8.8A-GATE).

    Aggregation rules (mirror Source Quality 8.7G):
      - worst-on-block: if ANY supporting pair produces a block condition
        (verdict='contradicted'), the span is BLOCKED;
      - any-on-warn:    else if ANY produces a warning condition (or has
        no latest entailment check), the span carries a WARNING;
      - else:           clean.

    A verified-backed span with no rows in ec_rows (no supporting pair
    at all) is treated as a missing-check WARNING — defensive and
    consistent with the corresponding Source Quality behavior.

    Returns a dict keyed by span_id, with values shaped as:
      {
        "block_reasons":   [{reason_code, claim_ledger_entry_id,
                              evidence_span_id, entailment_check_id,
                              verdict, confidence, ...}, ...],
        "warning_reasons": [{reason_code, claim_ledger_entry_id,
                              evidence_span_id, entailment_check_id,
                              verdict, confidence, ...}, ...],
        "per_evidence":    [{claim_ledger_entry_id, evidence_span_id,
                              entailment_check_id, verdict, confidence,
                              checker_name, checker_version,
                              policy_name, policy_version, mock,
                              classification}, ...],
      }

    Determinism: the order of reason entries within a span follows the
    natural order of ec_rows (already sorted by span_index then
    entry_id then evidence_span_id at SQL level), so the gap.details
    JSONB is stable across redeliveries.
    """
    result: dict[uuid.UUID, dict[str, Any]] = {}

    # Index ec_rows by span_id, in deterministic order.
    rows_by_span: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for r in ec_rows:
        span_id = uuid.UUID(str(r["span_id"]))
        rows_by_span.setdefault(span_id, []).append(r)

    for span_id in verified_span_ids:
        spans_rows = rows_by_span.get(span_id, [])

        block_reasons: list[dict[str, Any]] = []
        warning_reasons: list[dict[str, Any]] = []
        per_evidence: list[dict[str, Any]] = []

        if not spans_rows:
            # A verified-backed span with NO supporting (entry, span)
            # pair at all. This is anomalous given the 8.4 invariants
            # but we treat it defensively as a missing-check warning
            # rather than crashing or silently approving. Same
            # philosophy as the corresponding source-quality branch.
            warning_reasons.append(
                {
                    "reason_code": _ENTAILMENT_REASON_MISSING_CHECK,
                    "claim_ledger_entry_id": None,
                    "evidence_span_id": None,
                    "entailment_check_id": None,
                    "verdict": None,
                    "confidence": None,
                    "checker_name": None,
                    "checker_version": None,
                    "checker_policy_name": None,
                    "checker_policy_version": None,
                    "mock": None,
                }
            )
            result[span_id] = {
                "block_reasons": block_reasons,
                "warning_reasons": warning_reasons,
                "per_evidence": per_evidence,
            }
            continue

        for r in spans_rows:
            entry_id = r.get("claim_ledger_entry_id")
            entry_id_str = str(entry_id) if entry_id is not None else None
            ev_id = r.get("evidence_span_id")
            ev_id_str = str(ev_id) if ev_id is not None else None
            cec_id = r.get("cec_id")
            cec_id_str = str(cec_id) if cec_id is not None else None
            verdict = r.get("cec_verdict")
            verdict_str = str(verdict) if verdict is not None else None
            confidence = r.get("cec_confidence")
            confidence_f = (
                float(confidence) if confidence is not None else None
            )
            checker_name = r.get("cec_checker_name")
            checker_name_str = (
                str(checker_name) if checker_name is not None else None
            )
            checker_version = r.get("cec_checker_version")
            checker_version_str = (
                str(checker_version) if checker_version is not None else None
            )
            checker_policy_name = r.get("cec_policy_name")
            checker_policy_name_str = (
                str(checker_policy_name)
                if checker_policy_name is not None
                else None
            )
            checker_policy_version = r.get("cec_policy_version")
            checker_policy_version_str = (
                str(checker_policy_version)
                if checker_policy_version is not None
                else None
            )
            # The mock flag is stored inside payload.mock by the
            # claim_entailment_checker service. We extract it here for
            # downstream consumers without including the full payload
            # (which may contain free-form input text we deliberately
            # do NOT propagate to coverage_gap_statements.details).
            mock_flag: bool | None = None
            payload = r.get("cec_payload")
            if payload is not None:
                payload_dict: dict[str, Any] | None = None
                if isinstance(payload, dict):
                    payload_dict = payload
                elif isinstance(payload, str):
                    try:
                        decoded = json.loads(payload)
                        if isinstance(decoded, dict):
                            payload_dict = decoded
                    except (ValueError, TypeError):
                        payload_dict = None
                if payload_dict is not None:
                    raw_mock = payload_dict.get("mock")
                    if isinstance(raw_mock, bool):
                        mock_flag = raw_mock

            classification: str  # "clean" | "warning" | "block"

            if cec_id is None:
                # No latest claim_entailment_checks row exists for this
                # (entry, span) pair (LEFT JOIN LATERAL returned NULL).
                # Maps to a missing-check WARNING.
                warning_reasons.append(
                    {
                        "reason_code": _ENTAILMENT_REASON_MISSING_CHECK,
                        "claim_ledger_entry_id": entry_id_str,
                        "evidence_span_id": ev_id_str,
                        "entailment_check_id": None,
                        "verdict": None,
                        "confidence": None,
                        "checker_name": None,
                        "checker_version": None,
                        "checker_policy_name": None,
                        "checker_policy_version": None,
                        "mock": None,
                    }
                )
                classification = "warning"
            else:
                # A check exists: walk the policy matrix. Block conditions
                # are checked FIRST so a single pair never appears in both
                # bucket lists. Per MVP-0 P1 only 'contradicted' blocks.
                if verdict_str in _ENTAILMENT_BLOCK_REASONS_BY_VERDICT:
                    block_reasons.append(
                        {
                            "reason_code": _ENTAILMENT_BLOCK_REASONS_BY_VERDICT[
                                verdict_str
                            ],
                            "claim_ledger_entry_id": entry_id_str,
                            "evidence_span_id": ev_id_str,
                            "entailment_check_id": cec_id_str,
                            "verdict": verdict_str,
                            "confidence": confidence_f,
                            "checker_name": checker_name_str,
                            "checker_version": checker_version_str,
                            "checker_policy_name": checker_policy_name_str,
                            "checker_policy_version": checker_policy_version_str,
                            "mock": mock_flag,
                        }
                    )
                    classification = "block"
                elif verdict_str in _ENTAILMENT_WARNING_REASONS_BY_VERDICT:
                    warning_reasons.append(
                        {
                            "reason_code": _ENTAILMENT_WARNING_REASONS_BY_VERDICT[
                                verdict_str
                            ],
                            "claim_ledger_entry_id": entry_id_str,
                            "evidence_span_id": ev_id_str,
                            "entailment_check_id": cec_id_str,
                            "verdict": verdict_str,
                            "confidence": confidence_f,
                            "checker_name": checker_name_str,
                            "checker_version": checker_version_str,
                            "checker_policy_name": checker_policy_name_str,
                            "checker_policy_version": checker_policy_version_str,
                            "mock": mock_flag,
                        }
                    )
                    classification = "warning"
                elif verdict_str == "entailed":
                    classification = "clean"
                else:
                    warning_reasons.append(
                        {
                            "reason_code": "entailment_unrecognized_verdict",
                            "claim_ledger_entry_id": entry_id_str,
                            "evidence_span_id": ev_id_str,
                            "entailment_check_id": cec_id_str,
                            "verdict": verdict_str,
                            "confidence": confidence_f,
                            "checker_name": checker_name_str,
                            "checker_version": checker_version_str,
                            "checker_policy_name": checker_policy_name_str,
                            "checker_policy_version": checker_policy_version_str,
                            "mock": mock_flag,
                        }
                    )
                    classification = "warning"

            per_evidence.append(
                {
                    "claim_ledger_entry_id": entry_id_str,
                    "evidence_span_id": ev_id_str,
                    "entailment_check_id": cec_id_str,
                    "verdict": verdict_str,
                    "confidence": confidence_f,
                    "checker_name": checker_name_str,
                    "checker_version": checker_version_str,
                    "checker_policy_name": checker_policy_name_str,
                    "checker_policy_version": checker_policy_version_str,
                    "mock": mock_flag,
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
    """Insert one coverage_gap_statements row of kind='source_quality_warning'."""
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
# Claim Entailment emission helpers (Phase 8.8A-GATE)
# ---------------------------------------------------------------------------
def _emit_entailment_block_gap(
    conn: Connection,
    *,
    draft_id: uuid.UUID,
    span_id: uuid.UUID,
    span_index: int,
    block_reasons: list[dict[str, Any]],
    per_evidence: list[dict[str, Any]],
) -> bool:
    """Insert one coverage_gap_statements row of kind='entailment_block'.

    gap_key is deterministic and idempotent under redelivery:
      f'span:{span_id}:entailment_block'

    details JSONB never includes stack traces, claim text, or quote text.
    It contains:
      - span_id (string, for cross-reference);
      - span_index (integer);
      - reasons (list of structured dicts: reason_code,
        claim_ledger_entry_id, evidence_span_id, entailment_check_id,
        verdict, confidence, checker_name, checker_version,
        checker_policy_name, checker_policy_version, mock);
      - per_evidence (list of full per-pair classifications);
      - policy (name + version of the gate's entailment policy matrix).

    Returns True if a new gap row was inserted, False if the gap was
    already present (idempotent re-run).
    """
    return _upsert_coverage_gap(
        conn,
        draft_id=draft_id,
        kind="entailment_block",
        severity="block",
        gap_key=f"span:{span_id}:entailment_block",
        details={
            "span_id": str(span_id),
            "span_index": int(span_index),
            "reasons": block_reasons,
            "per_evidence": per_evidence,
            "policy": {
                "name": _ENTAILMENT_POLICY_NAME,
                "version": _ENTAILMENT_POLICY_VERSION,
            },
        },
    )


def _emit_entailment_warning_gap(
    conn: Connection,
    *,
    draft_id: uuid.UUID,
    span_id: uuid.UUID,
    span_index: int,
    warning_reasons: list[dict[str, Any]],
    per_evidence: list[dict[str, Any]],
) -> bool:
    """Insert one coverage_gap_statements row of kind='entailment_warning'.

    gap_key is deterministic and idempotent under redelivery:
      f'span:{span_id}:entailment_warning'

    severity is 'warn': the Gate decision is NOT changed by this gap.
    """
    return _upsert_coverage_gap(
        conn,
        draft_id=draft_id,
        kind="entailment_warning",
        severity="warn",
        gap_key=f"span:{span_id}:entailment_warning",
        details={
            "span_id": str(span_id),
            "span_index": int(span_index),
            "reasons": warning_reasons,
            "per_evidence": per_evidence,
            "policy": {
                "name": _ENTAILMENT_POLICY_NAME,
                "version": _ENTAILMENT_POLICY_VERSION,
            },
        },
    )


# ---------------------------------------------------------------------------
# Entailment summary aggregation (for final_gate_reports.payload)
# ---------------------------------------------------------------------------
def _aggregate_entailment_reason_counts(
    classifications: dict[uuid.UUID, dict[str, Any]],
    span_ids: list[uuid.UUID],
    *,
    bucket: str,
) -> dict[str, int]:
    """Aggregate reason_code -> count across the given span ids on the
    chosen bucket ('block_reasons' or 'warning_reasons').
    """
    counts: dict[str, int] = {}
    for sid in span_ids:
        cls = classifications.get(sid)
        if not cls:
            continue
        for r in cls.get(bucket, []) or []:
            code = str(r.get("reason_code") or "")
            if not code:
                continue
            counts[code] = counts.get(code, 0) + 1
    return counts


def _entailment_status_for_payload(
    *, blocked_spans: list[uuid.UUID], warning_spans: list[uuid.UUID]
) -> str:
    """Compute the summary 'status' field for the entailment section of
    final_gate_reports.payload.

    Values: 'blocked' | 'warnings' | 'clean'. Mirrors the same three
    semantic buckets used internally to drive the decision.
    """
    if blocked_spans:
        return "blocked"
    if warning_spans:
        return "warnings"
    return "clean"


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
    verification rule, ONLY when all spans are verified-backed.

    Claim Entailment policy (Phase 8.8A-GATE, P1 — MVP-0): applied AFTER
    the 8.4 verification rule, ONLY when all spans are verified-backed.
    Takes priority over Source Quality on the reason_code dimension when
    both fire on the same draft, but both kinds of gap are always
    emitted for full audit visibility.

    See module docstring for the full decision table and
    PHASE_8_8A_GATE_PRE.md §7 for the rationale.
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

        if linked_entry_id is None or latest_entry_id is None:
            continue
        if str(linked_entry_id) != str(latest_entry_id):
            continue
        if latest_entry_state == "verified_fact":
            bucket["verified"] = True

    spans_total = len(spans)
    spans_verified = sum(1 for s in spans.values() if s["verified"])
    spans_unverified = spans_total - spans_verified
    coverage_gaps_emitted = 0

    # ----- Branch A: zero spans -----
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

    # ----- Branch C: at least one span is not verified-backed (CVE-lite priority) -----
    #
    # CRITICAL PRIORITY (Phase 8.7G + 8.8A-GATE):
    # CVE-lite > Claim Entailment > Source Quality. When any span is not
    # verified-backed, the Gate rejects with 'unverified_spans_present'
    # WITHOUT consulting either claim_entailment_checks or
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

    # ----- All spans verified-backed: consult Entailment AND Source Quality -----
    #
    # We compute BOTH classifications now and decide priority + emission
    # afterward, so that a 'rejected' branch (entailment_block OR
    # source_quality_block) can still emit the full audit (gaps of the
    # losing axis) without changing the reason_code.
    verified_span_ids = {sid for sid, s in spans.items() if s["verified"]}

    sq_rows = _select_source_quality_per_span(conn, draft_id)
    sq_per_span = _classify_source_quality_per_span(sq_rows, verified_span_ids)

    ec_rows = _select_entailment_per_span(conn, draft_id)
    ec_per_span = _classify_entailment_per_span(ec_rows, verified_span_ids)

    sq_blocked_spans: list[uuid.UUID] = []
    sq_warning_spans: list[uuid.UUID] = []
    ec_blocked_spans: list[uuid.UUID] = []
    ec_warning_spans: list[uuid.UUID] = []

    for sid in verified_span_ids:
        sq_cls = sq_per_span.get(sid, {})
        if sq_cls.get("block_reasons"):
            sq_blocked_spans.append(sid)
        elif sq_cls.get("warning_reasons"):
            sq_warning_spans.append(sid)

        ec_cls = ec_per_span.get(sid, {})
        if ec_cls.get("block_reasons"):
            ec_blocked_spans.append(sid)
        elif ec_cls.get("warning_reasons"):
            ec_warning_spans.append(sid)

    # Deterministic ordering for downstream emission (smaller span_index first).
    sq_blocked_spans.sort(key=lambda sid: spans[sid]["span_index"])
    sq_warning_spans.sort(key=lambda sid: spans[sid]["span_index"])
    ec_blocked_spans.sort(key=lambda sid: spans[sid]["span_index"])
    ec_warning_spans.sort(key=lambda sid: spans[sid]["span_index"])

    # Build the entailment summary section of final_gate_reports.payload
    # eagerly: it has the same shape in every branch from here on, only
    # the 'status' value changes.
    entailment_summary = {
        "policy_name": _ENTAILMENT_POLICY_NAME,
        "policy_version": _ENTAILMENT_POLICY_VERSION,
        "status": _entailment_status_for_payload(
            blocked_spans=ec_blocked_spans, warning_spans=ec_warning_spans
        ),
        "spans_with_block": len(ec_blocked_spans),
        "spans_with_warnings": len(ec_warning_spans),
        "block_reason_counts": _aggregate_entailment_reason_counts(
            ec_per_span, ec_blocked_spans, bucket="block_reasons"
        ),
        "warning_reason_counts": _aggregate_entailment_reason_counts(
            ec_per_span, ec_warning_spans, bucket="warning_reasons"
        ),
    }

    # ----- Branch E (8.8A-GATE): at least one span has entailment BLOCK -----
    # Entailment block takes priority over source quality block on
    # reason_code, per PHASE_8_8A_GATE_PRE.md §7. Both kinds of gap are
    # still emitted (entailment block on blocked spans, entailment
    # warning on remaining warning spans, source quality block / warning
    # likewise) so the audit captures the full state of the draft.
    if ec_blocked_spans:
        decision = "rejected"
        reason_code = "entailment_block"

        # Emit entailment block gaps for blocked spans.
        for sid in ec_blocked_spans:
            cls = ec_per_span[sid]
            if _emit_entailment_block_gap(
                conn,
                draft_id=draft_id,
                span_id=sid,
                span_index=spans[sid]["span_index"],
                block_reasons=cls["block_reasons"],
                per_evidence=cls["per_evidence"],
            ):
                coverage_gaps_emitted += 1

        # Emit entailment warning gaps for non-blocked spans that have
        # entailment warnings.
        for sid in ec_warning_spans:
            cls = ec_per_span[sid]
            if _emit_entailment_warning_gap(
                conn,
                draft_id=draft_id,
                span_id=sid,
                span_index=spans[sid]["span_index"],
                warning_reasons=cls["warning_reasons"],
                per_evidence=cls["per_evidence"],
            ):
                coverage_gaps_emitted += 1

        # Audit completeness: also emit source quality gaps (block and
        # warning) for the same draft. They do NOT change the
        # reason_code, but they make the audit a full picture of every
        # axis that fired.
        for sid in sq_blocked_spans:
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
        for sid in sq_warning_spans:
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
                    "blocked_spans": len(sq_blocked_spans),
                    "warning_spans": len(sq_warning_spans),
                    "clean_spans": (
                        spans_verified
                        - len(sq_blocked_spans)
                        - len(sq_warning_spans)
                    ),
                    "policy_name": _SOURCE_QUALITY_POLICY_NAME,
                    "policy_version": _SOURCE_QUALITY_POLICY_VERSION,
                },
                "entailment": entailment_summary,
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

    # ----- Branch C' (8.7G): at least one span is BLOCKED by source quality -----
    # Reached only when no entailment block fired on this draft. We
    # still emit entailment warnings (if any) for audit completeness.
    if sq_blocked_spans:
        decision = "rejected"
        reason_code = "source_quality_block"

        for sid in sq_blocked_spans:
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

        for sid in sq_warning_spans:
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

        # Audit completeness: emit entailment warnings (no entailment
        # block can be present in this branch by construction).
        for sid in ec_warning_spans:
            cls = ec_per_span[sid]
            if _emit_entailment_warning_gap(
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
                    "blocked_spans": len(sq_blocked_spans),
                    "warning_spans": len(sq_warning_spans),
                    "clean_spans": (
                        spans_verified
                        - len(sq_blocked_spans)
                        - len(sq_warning_spans)
                    ),
                    "policy_name": _SOURCE_QUALITY_POLICY_NAME,
                    "policy_version": _SOURCE_QUALITY_POLICY_VERSION,
                },
                "entailment": entailment_summary,
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
            "spans_unverified": 0,
            "coverage_gaps_emitted": coverage_gaps_emitted,
            "agent_run_id": str(agent_run_id),
        }

    # ----- Branch W (8.7G + 8.8A-GATE): only warnings, no blocks -----
    # Reached when there is no entailment block AND no source quality
    # block. If ANY warning (entailment OR source quality) is present,
    # we approve with the existing 8.7G reason_code
    # 'all_spans_verified_with_warnings' and emit BOTH kinds of warning
    # gaps as relevant.
    if ec_warning_spans or sq_warning_spans:
        decision = "approved"
        reason_code = "all_spans_verified_with_warnings"

        # Emit entailment warnings.
        for sid in ec_warning_spans:
            cls = ec_per_span[sid]
            if _emit_entailment_warning_gap(
                conn,
                draft_id=draft_id,
                span_id=sid,
                span_index=spans[sid]["span_index"],
                warning_reasons=cls["warning_reasons"],
                per_evidence=cls["per_evidence"],
            ):
                coverage_gaps_emitted += 1

        # Emit source quality warnings.
        for sid in sq_warning_spans:
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
                    "warning_spans": len(sq_warning_spans),
                    "clean_spans": spans_verified - len(sq_warning_spans),
                    "policy_name": _SOURCE_QUALITY_POLICY_NAME,
                    "policy_version": _SOURCE_QUALITY_POLICY_VERSION,
                },
                "entailment": entailment_summary,
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

    # ----- Branch B (8.4 invariata): all spans clean on both axes -----
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
            "entailment": entailment_summary,
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
