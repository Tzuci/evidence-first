"""Claim entailment orchestrator (Phase 8.8A — Block ORCHESTRATOR).

This module bridges a future task.created pipeline integration (deferred
to the next block, 8.8A-WORKER) and the mock claim entailment checker
service (8.8A-SERVICE). Its single job is: for every (claim_ledger_entry,
evidence_span) pair linked to claims of a given task via
``claim_evidence_links``, call ``assess_claim_entailment`` exactly once
with a deterministic idempotency_key.

NOTE: this block does NOT integrate the orchestrator into the
``task.created`` consumer pipeline. That is the responsibility of the
8.8A-WORKER block, which will wrap the call in a SAVEPOINT (mirroring
the 8.7E pattern) and emit a ``task.entailment_checked`` audit event.

Strict scope (Phase 8.8A-ORCHESTRATOR invariants — see
PHASE_8_8A_PRE.md §3, §4):

  - The orchestrator ONLY:
      * SELECTs from task_masters (to detect missing tasks);
      * SELECTs DISTINCT (claim_ledger_entry_id, evidence_span_id)
        from claim_evidence_links JOIN logical_claims for the task;
      * calls assess_claim_entailment(...) for each pair.
  - The orchestrator does NOT:
      * write to ANY table other than via assess_claim_entailment
        (which writes only to claim_entailment_checks);
      * emit audit_records (audit emission is the responsibility of
        the future 8.8A-WORKER block with a single aggregated
        task-scoped event);
      * mutate task_masters.status, claim_ledger_entries,
        claim_lineage, claim_evidence_links, verification_records,
        logical_claims, final_gate_reports, draft_final_answers,
        final_answer_spans, final_answer_span_claim_links,
        coverage_gap_statements, published_answers,
        published_answer_lifecycle_events, source_loss_events,
        source_loss_propagation_records, source_quality_assessments;
      * call Redis;
      * import FastAPI / API modules;
      * import the task.created consumer;
      * raise on per-pair service-level statuses ('assessed',
        'already_assessed', 'not_found', 'invalid_target', 'error').
        These are normal outcomes and are simply aggregated into the
        returned counts.

Semantic invariants (from PHASE_8_8A_PRE.md §3, §4):

  - claim entailment != claim correctness;
  - claim entailment != evidence support;
  - claim entailment != CVE-lite verification;
  - claim entailment != source quality;
  - claim entailment != contradiction detection.

  This orchestrator records the SEMANTIC RELATION between a claim and
  the quote that supports it, on a per-pair basis. It does NOT judge
  the claim, the source, or the verification outcome. The Final
  Answer Gate is NOT affected by this orchestrator in 8.8A-ORCHESTRATOR.

Transaction and savepoint model:

  The caller passes an active SQLAlchemy ``Connection`` inside an
  explicit transaction. This module never opens its own connection,
  never commits, never rolls back. It does NOT acquire its own
  savepoints: ``assess_claim_entailment`` already wraps the INSERT
  in ``conn.begin_nested()`` to absorb race-time UNIQUE-violation
  IntegrityErrors. If an unexpected exception escapes from the
  service (e.g. a programming error or a DB error not handled by
  the service's own SAVEPOINT), the orchestrator lets it propagate;
  the future 8.8A-WORKER block will wrap THIS call in an outer
  SAVEPOINT so that such a failure cannot poison the consumer's
  pipeline.

Idempotency contract:

  Each pair is assessed with the deterministic key
  ``f"task:{task_id}:entry:{claim_ledger_entry_id}:span:{evidence_span_id}:v1"``.
  A re-run of the orchestrator (e.g. on event redelivery) collapses
  every pair call into ``already_assessed`` at the service level,
  with no duplicate row in ``claim_entailment_checks``. The ``:v1``
  suffix is a forward-compat marker reserved for future schema
  changes to the key format. The key format includes BOTH
  ``entry_id`` and ``span_id`` because the entailment check
  granularity is the pair (NOT the span alone, unlike Source Quality
  in 8.7E).

Return contract:

  Always a dict with the same shape, even on the ``not_found`` branch:

      {
        "status":                   "completed" | "not_found",
        "pairs_total":              int,
        "assessed_count":           int,
        "already_assessed_count":   int,
        "not_found_count":          int,
        "invalid_target_count":     int,
        "error_count":              int,
      }

  - ``not_found`` is returned ONLY when the task_id itself does not
    resolve in task_masters; all per-pair service statuses are
    counted and ``status`` stays ``completed`` even if every pair
    returned ``error``.
  - ``pairs_total`` is the number of DISTINCT (entry, span) rows
    discovered at call time from claim_evidence_links.

Discovery query rationale:

  The orchestrator scopes pairs to a task by joining
  ``claim_evidence_links cel`` against ``logical_claims lc`` on
  ``cel.claim_logical_id = lc.id`` and filtering ``lc.task_id``.
  This is the canonical scoping path because every logical_claim
  is owned by exactly one task in 0004's design. We do NOT consult
  ``claim_ledger_entries`` directly to derive a "latest" entry: the
  link itself in ``claim_evidence_links`` points to a specific
  ``claim_ledger_entry_id``, and that is the entry the service must
  evaluate (per PHASE_8_8A_PRE.md §4: "if claim_evidence_links
  points already to cle.id, use that entry; do not invent
  latest_entry_id if the link is explicit").

  The filter ``cel.evidence_span_id IS NOT NULL`` honors the
  ``cel_origin_xor`` CHECK declared in 0004 (in MVP-0 only
  evidence_span_id is exercised; retrieved_source_span_id is
  reserved for future blocks).

  Ordering by ``(claim_ledger_entry_id ASC, evidence_span_id ASC)``
  makes the iteration deterministic so redeliveries hit the same
  per-pair idempotency keys in the same order — useful for
  diagnostic log inspection.
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.engine import Connection

# IMPORTANT: import the service symbol locally so tests can patch the
# orchestrator's reference (apps.worker.app.services.claim_entailment_orchestrator
# .assess_claim_entailment) rather than the service module itself.
# This mirrors the pattern used in apps/worker/app/services/source_quality_orchestrator.py
# (which imports assess_source_quality from .source_quality_evaluator).
from .claim_entailment_checker import assess_claim_entailment


logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module identity
# ---------------------------------------------------------------------------
SERVICE_NAME = "claim_entailment_orchestrator"
SERVICE_VERSION = "0.1.0"

# Forward-compat marker baked into the idempotency_key format. Bumping
# this would force a fresh check to be appended (as a new row with a
# different idempotency_key) on the next redelivery — useful when the
# checker's mock policy changes meaning and we want a clean history.
# We do NOT bump it in 8.8A-ORCHESTRATOR.
_IDEMPOTENCY_KEY_VERSION = "v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_idempotency_key(
    *,
    task_id: uuid.UUID,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
) -> str:
    """Return the deterministic idempotency key for a (task, entry, span)
    triple.

    Format: ``task:{task_id}:entry:{entry_id}:span:{span_id}:v1``.
    Identity is derived from the lowercase canonical string form of
    each UUID, which is what ``str(uuid.UUID)`` already returns. The
    version suffix is global to the format (not per-target) so a
    future bump affects every task uniformly.
    """
    return (
        f"task:{task_id}"
        f":entry:{claim_ledger_entry_id}"
        f":span:{evidence_span_id}"
        f":{_IDEMPOTENCY_KEY_VERSION}"
    )


def _task_exists(conn: Connection, *, task_id: uuid.UUID) -> bool:
    """Return True iff a task with ``task_id`` exists in task_masters.

    This is a plain snapshot read (no FOR UPDATE). We never mutate
    task_masters here. The check exists only to distinguish "task
    does not exist" (-> status='not_found') from "task exists but
    has no claim_evidence_links" (-> status='completed' with zero
    counts).
    """
    row = conn.execute(
        text(
            """
            SELECT 1
            FROM task_masters
            WHERE id = :tid
            LIMIT 1
            """
        ),
        {"tid": task_id},
    ).first()
    return row is not None


def _select_distinct_pairs(
    conn: Connection, *, task_id: uuid.UUID
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Return DISTINCT (claim_ledger_entry_id, evidence_span_id) pairs
    linked to ``task_id``.

    Pairs are derived from ``claim_evidence_links cel`` JOIN
    ``logical_claims lc`` on ``cel.claim_logical_id = lc.id``,
    filtered by ``lc.task_id = :task_id`` and
    ``cel.evidence_span_id IS NOT NULL`` (honors ``cel_origin_xor``).

    The DISTINCT projection is explicit even though the existing
    UNIQUE ``cel_entry_span_uq (claim_ledger_entry_id, evidence_span_id)``
    on ``claim_evidence_links`` makes exact-pair duplicates impossible
    at DB level: the DISTINCT makes the orchestrator's contract
    robust to a future schema change that loosens that UNIQUE, and
    documents intent. ``ORDER BY`` makes the per-pair iteration
    deterministic across redeliveries.
    """
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT
                cel.claim_ledger_entry_id AS entry_id,
                cel.evidence_span_id      AS span_id
            FROM claim_evidence_links cel
            JOIN logical_claims        lc ON lc.id = cel.claim_logical_id
            WHERE lc.task_id            = :task_id
              AND cel.evidence_span_id IS NOT NULL
            ORDER BY cel.claim_ledger_entry_id ASC,
                     cel.evidence_span_id      ASC
            """
        ),
        {"task_id": task_id},
    ).fetchall()
    return [
        (
            uuid.UUID(str(r._mapping["entry_id"])),
            uuid.UUID(str(r._mapping["span_id"])),
        )
        for r in rows
    ]


def _empty_counts(status: str) -> dict[str, Any]:
    """Return a fully-populated counts dict with ``status`` set as given.

    The shape is stable across all return paths so the caller can read
    every key without conditional checks. This mirrors the
    ``_empty_counts`` helper of source_quality_orchestrator.py with
    ``spans_total`` replaced by ``pairs_total`` (the granularity here
    is the pair, not the span alone).
    """
    return {
        "status": status,
        "pairs_total": 0,
        "assessed_count": 0,
        "already_assessed_count": 0,
        "not_found_count": 0,
        "invalid_target_count": 0,
        "error_count": 0,
    }


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------
def run_claim_entailment_checks(
    conn: Connection,
    *,
    task_id: uuid.UUID,
) -> dict[str, Any]:
    """Run the mock claim entailment check for every
    (claim_ledger_entry, evidence_span) pair linked to ``task_id``.

    Steps:
      1. Verify that the task exists. If not, return
         ``status='not_found'`` with zero counts. No DB write happens.
      2. Compute the impact set: DISTINCT (entry, span) pairs linked
         to the task via claim_evidence_links JOIN logical_claims.
         If the set is empty, return ``status='completed'`` with
         ``pairs_total=0`` and zero per-status counts.
      3. For each pair (in deterministic order):
           - build the deterministic idempotency_key;
           - call ``assess_claim_entailment`` with the pair and the
             key;
           - aggregate the service-level status into the matching
             counter.
      4. Return the aggregated counts with ``status='completed'``.

    Per-pair status policy:
      The five service-level statuses ('assessed', 'already_assessed',
      'not_found', 'invalid_target', 'error') are normal outcomes and
      are simply counted. Any UNEXPECTED raised exception is allowed
      to propagate to the caller — the future 8.8A-WORKER block will
      wrap this call in an outer SAVEPOINT so that programming
      errors cannot poison the consumer's transaction.

    Side effects:
      None directly. Indirectly, via ``assess_claim_entailment``,
      AT MOST one row MAY be inserted into ``claim_entailment_checks``
      per pair (skipped on idempotency replay, skipped on
      version-conflict error, skipped on not_found / invalid_target).
      No audit, no other table.

    Args:
      conn:
        SQLAlchemy Connection inside an active transaction. Must NOT
        commit or rollback on behalf of the caller.
      task_id:
        UUID of the task whose claim_evidence_links shall drive the
        check.
    """
    # Step 1: detect missing task. Cheap snapshot read; no lock.
    if not _task_exists(conn, task_id=task_id):
        logger.info(
            "claim_entailment_orchestrator.task_not_found",
            task_id=str(task_id),
        )
        return _empty_counts("not_found")

    # Step 2: discovery.
    pairs = _select_distinct_pairs(conn, task_id=task_id)

    counts = _empty_counts("completed")
    counts["pairs_total"] = len(pairs)

    if not pairs:
        logger.info(
            "claim_entailment_orchestrator.no_pairs",
            task_id=str(task_id),
        )
        return counts

    # Step 3: per-pair assessment.
    for entry_id, span_id in pairs:
        idempotency_key = _build_idempotency_key(
            task_id=task_id,
            claim_ledger_entry_id=entry_id,
            evidence_span_id=span_id,
        )
        result = assess_claim_entailment(
            conn,
            claim_ledger_entry_id=entry_id,
            evidence_span_id=span_id,
            idempotency_key=idempotency_key,
        )
        status = str(result.get("status", ""))
        if status == "assessed":
            counts["assessed_count"] += 1
        elif status == "already_assessed":
            counts["already_assessed_count"] += 1
        elif status == "not_found":
            # The entry or the span vanished between our SELECT
            # DISTINCT and the service's FOR UPDATE lock. Extremely
            # narrow race (same transaction) but possible if a
            # future change ever introduces a DELETE on either
            # parent table. We do NOT raise.
            counts["not_found_count"] += 1
            logger.warning(
                "claim_entailment_orchestrator.pair_not_found",
                task_id=str(task_id),
                claim_ledger_entry_id=str(entry_id),
                evidence_span_id=str(span_id),
            )
        elif status == "invalid_target":
            # Cannot happen given how we call the service (we always
            # pass two well-formed UUIDs and a non-empty key).
            # Counted defensively so a future programming error
            # surfaces visibly via this counter rather than via a
            # silent assertion failure.
            counts["invalid_target_count"] += 1
            logger.error(
                "claim_entailment_orchestrator.invalid_target_unexpected",
                task_id=str(task_id),
                claim_ledger_entry_id=str(entry_id),
                evidence_span_id=str(span_id),
                service_result=result,
            )
        elif status == "error":
            # MVP-0 fixes version_no=1; the service surfaces a
            # 'entailment_version_conflict' as status='error' when
            # a v1 already exists for the pair under a DIFFERENT
            # idempotency_key. We count it without raising — by
            # construction with the deterministic key above this
            # cannot happen on a clean redelivery, but a misuse of
            # the underlying service from a different caller could
            # produce it, and the test suite for this orchestrator
            # exercises the path via a monkeypatched stub.
            counts["error_count"] += 1
            logger.warning(
                "claim_entailment_orchestrator.pair_error",
                task_id=str(task_id),
                claim_ledger_entry_id=str(entry_id),
                evidence_span_id=str(span_id),
                error_code=result.get("error_code"),
            )
        else:
            # Unknown service status: account for it as an error
            # condition but still do NOT raise (mirroring the
            # source_quality_orchestrator policy). Log loudly so a
            # regression is noticed.
            counts["error_count"] += 1
            logger.error(
                "claim_entailment_orchestrator.unknown_service_status",
                task_id=str(task_id),
                claim_ledger_entry_id=str(entry_id),
                evidence_span_id=str(span_id),
                service_status=status,
                service_result=result,
            )

    logger.info(
        "claim_entailment_orchestrator.completed",
        task_id=str(task_id),
        pairs_total=counts["pairs_total"],
        assessed_count=counts["assessed_count"],
        already_assessed_count=counts["already_assessed_count"],
        not_found_count=counts["not_found_count"],
        invalid_target_count=counts["invalid_target_count"],
        error_count=counts["error_count"],
    )
    return counts
