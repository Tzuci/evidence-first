"""Source quality orchestrator (Phase 8.7 — Block E).

This module bridges the task.created pipeline (8.3/8.4) and the mock
source quality evaluator service (8.7D). It is invoked from
``apps/worker/app/consumers/task_created.py`` AFTER ``task.analyzed_partial``
and BEFORE ``task.compiling``, with one job: for each evidence_span_id
linked to any claim of the task via ``claim_evidence_links``, call
``assess_source_quality`` exactly once with a deterministic
idempotency_key.

Strict scope (Phase 8.7E invariants — see PHASE_8_7_PLAN.md §9.5,
Option W-A):

  - The orchestrator ONLY:
      * SELECTs from task_masters (to resolve tenant/project scope);
      * SELECTs DISTINCT evidence_span_id from
        claim_evidence_links JOIN logical_claims for the task;
      * calls assess_source_quality(...) for each span.
  - The orchestrator does NOT:
      * write to ANY table other than via assess_source_quality
        (which writes only to source_quality_assessments);
      * emit audit_records (audit emission is the responsibility of
        task_created.py with a single aggregated task-scoped event);
      * mutate task_masters.status, claim_ledger_entries,
        claim_lineage, claim_evidence_links, verification_records,
        final_gate_reports, draft_final_answers, final_answer_spans,
        final_answer_span_claim_links, coverage_gap_statements,
        published_answers, published_answer_lifecycle_events,
        source_loss_events, source_loss_propagation_records;
      * call Redis;
      * import FastAPI / API modules;
      * evaluate document_chunk_id or document_id targets
        (deferred to a later block — only span-grain assessment is
        chained into the task pipeline in 8.7E);
      * raise on per-span service-level failures classified as
        ``not_found`` / ``invalid_target`` (these are counted and
        returned, NOT propagated).

Semantic invariants (from PHASE_8_7_PLAN.md §3):

  - source quality is NOT claim correctness;
  - source quality is NOT evidence support;
  - source quality is NOT verification outcome;
  - source quality is NOT source loss;
  - source quality is NOT final publication eligibility.

  This orchestrator records the QUALITY of the sources that back the
  claims of the task; it does NOT judge the claims and does NOT
  reflect anything on the Final Answer Gate. The Gate continues to
  operate on the "verified-backed" rule defined in 8.4 — unchanged.

Transaction and savepoint model:

  The caller passes an active SQLAlchemy Connection inside an
  explicit transaction. This module never opens its own connection,
  never commits, never rolls back. It does NOT acquire savepoints
  itself: if an unexpected exception escapes from assess_source_quality
  (e.g. an unhandled DB exception that would abort the outer
  transaction), the orchestrator lets it propagate. The protective
  ``conn.begin_nested()`` SAVEPOINT is acquired by the CALLER in
  ``task_created.py``, precisely so that the failure of this step does
  NOT poison the 8.4 pipeline that runs after.

  Rationale for that division of responsibility:
    - keeping the orchestrator simple (no savepoint dance, no broad
      ``except``) preserves a clean contract for tests;
    - the savepoint properly belongs to the CALLER because only the
      caller can decide what "failure of source quality must not block
      8.4" means in terms of audit emission and pipeline continuation.

  Per-span service-level outcomes (``assessed``, ``already_assessed``,
  ``not_found``, ``invalid_target``) are NOT exceptional and are
  aggregated into the returned counts; the orchestrator continues to
  the next span on any of these. Only programming errors (raised
  exceptions) bubble up.

Idempotency contract:

  Each span is assessed with the deterministic key
  ``f"task:{task_id}:span:{evidence_span_id}:v1"``. A re-run of the
  task.created consumer (redelivery) collapses every span call into
  ``already_assessed`` at the service level, with no duplicate row in
  ``source_quality_assessments``. The ``:v1`` suffix is a forward-compat
  marker reserved for future schema changes to the key format.

Return contract:

  Always a dict with the same shape, even on the not_found branch:

      {
        "status":                   "completed" | "not_found",
        "spans_total":              int,
        "assessed_count":           int,
        "already_assessed_count":   int,
        "not_found_count":          int,
        "invalid_target_count":     int,
        "error_count":              int,
      }

  - ``not_found`` is returned ONLY when the task_id itself does not
    resolve in task_masters; all per-span statuses are counted and
    ``status`` stays ``completed``.
  - ``spans_total`` is the number of DISTINCT evidence_span_id rows
    linked to the task via claim_evidence_links at call time.
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.engine import Connection

from .source_quality_evaluator import assess_source_quality


logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module identity
# ---------------------------------------------------------------------------
SERVICE_NAME = "source_quality_orchestrator"
SERVICE_VERSION = "0.1.0"

# Forward-compat marker baked into the idempotency_key format. Bumping
# this would force a fresh assessment to be appended (as version_no+1)
# on the next redelivery — useful when the evaluator's mock policy
# changes meaning and we want a clean history. We do NOT bump it in
# 8.7E.
_IDEMPOTENCY_KEY_VERSION = "v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_idempotency_key(
    *, task_id: uuid.UUID, evidence_span_id: uuid.UUID
) -> str:
    """Return the deterministic idempotency key for a (task, span) pair.

    Format: ``task:{task_id}:span:{evidence_span_id}:v1``. Identity is
    derived from the lowercase canonical string form of each UUID,
    which is what ``str(uuid.UUID)`` already returns. The version
    suffix is global to the format (not per-target) so a future bump
    affects every task uniformly.
    """
    return (
        f"task:{task_id}:span:{evidence_span_id}:{_IDEMPOTENCY_KEY_VERSION}"
    )


def _fetch_task_scope(
    conn: Connection, *, task_id: uuid.UUID
) -> dict[str, uuid.UUID] | None:
    """Return (tenant_id, project_id) for the task, or None if not found.

    The SELECT is a plain snapshot read (no FOR UPDATE): the source
    quality evaluator acquires its own row-level lock on the target's
    parent row, and there is no need to lock task_masters here since
    the orchestrator does not mutate it.
    """
    row = conn.execute(
        text(
            """
            SELECT tenant_id, project_id
            FROM task_masters
            WHERE id = :tid
            """
        ),
        {"tid": task_id},
    ).first()
    if row is None:
        return None
    m = row._mapping
    return {
        "tenant_id": uuid.UUID(str(m["tenant_id"])),
        "project_id": uuid.UUID(str(m["project_id"])),
    }


def _select_distinct_evidence_span_ids(
    conn: Connection, *, task_id: uuid.UUID
) -> list[uuid.UUID]:
    """Return DISTINCT evidence_span_id rows linked to the task.

    Only spans linked via ``claim_evidence_links`` JOIN ``logical_claims``
    (where ``lc.task_id = :task_id`` and ``cel.evidence_span_id IS NOT
    NULL``) are returned. The filter on ``evidence_span_id IS NOT NULL``
    honors the ``cel_origin_xor`` CHECK declared in 0004_claim_ledger.sql:
    a link can carry either an evidence_span_id or a
    retrieved_source_span_id (the latter is out of scope for the
    closed-corpus pipeline in MVP-0, but we still filter defensively).

    Ordering by ``evidence_span_id`` makes the iteration deterministic
    so a redelivery hits the same per-span idempotency keys in the same
    order, which is useful for debugging and audit log inspection.
    """
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT cel.evidence_span_id
            FROM claim_evidence_links cel
            JOIN logical_claims lc ON lc.id = cel.claim_logical_id
            WHERE lc.task_id = :task_id
              AND cel.evidence_span_id IS NOT NULL
            ORDER BY cel.evidence_span_id
            """
        ),
        {"task_id": task_id},
    ).fetchall()
    return [uuid.UUID(str(r._mapping["evidence_span_id"])) for r in rows]


def _empty_counts(status: str) -> dict[str, Any]:
    """Return a fully-populated counts dict with ``status`` set as given.

    The shape is stable across all return paths so the caller can read
    every key without conditional checks.
    """
    return {
        "status": status,
        "spans_total": 0,
        "assessed_count": 0,
        "already_assessed_count": 0,
        "not_found_count": 0,
        "invalid_target_count": 0,
        "error_count": 0,
    }


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------
def run_source_quality_assessment(
    conn: Connection,
    *,
    task_id: uuid.UUID,
) -> dict[str, Any]:
    """Run mock source quality assessment for every evidence_span linked
    to claims of ``task_id``.

    Steps:
      1. Resolve (tenant_id, project_id) from task_masters. If the task
         does not exist, return ``status='not_found'`` with zero counts.
         No DB write happens.
      2. Compute the impact set: DISTINCT evidence_span_id rows linked
         to the task via claim_evidence_links JOIN logical_claims.
         If the set is empty, return ``status='completed'`` with
         ``spans_total=0`` and zero per-status counts.
      3. For each span:
           - build the deterministic idempotency_key;
           - call ``assess_source_quality`` with target
             ``evidence_span_id=<span>``, ``tenant_id``, ``project_id``,
             ``idempotency_key`` as above, and a minimal payload
             documenting the call context (trigger, task, span);
           - aggregate the service-level status into the matching
             counter.
      4. Return the aggregated counts with ``status='completed'``.

    Per-span error policy:
      The four service-level statuses (``assessed``,
      ``already_assessed``, ``not_found``, ``invalid_target``) are
      normal outcomes and are simply counted. Any raised exception
      (a programming error or an unhandled DB error from inside the
      evaluator) is allowed to propagate to the caller. This is by
      design: the caller (``task_created.py``) wraps the whole
      orchestrator call in a SAVEPOINT precisely to prevent such an
      exception from poisoning the outer transaction, and to record a
      failed audit aggregate.

    Side effects:
      None directly. Indirectly, via ``assess_source_quality``, one row
      MAY be inserted into ``source_quality_assessments`` per span
      (skipped on idempotency replay). No audit, no other table.
    """
    # 1) Resolve task scope.
    scope = _fetch_task_scope(conn, task_id=task_id)
    if scope is None:
        logger.info(
            "source_quality_orchestrator.task_not_found",
            task_id=str(task_id),
        )
        return _empty_counts("not_found")

    tenant_id = scope["tenant_id"]
    project_id = scope["project_id"]

    # 2) Compute the evidence-span impact set.
    span_ids = _select_distinct_evidence_span_ids(conn, task_id=task_id)

    counts = _empty_counts("completed")
    counts["spans_total"] = len(span_ids)

    if not span_ids:
        logger.info(
            "source_quality_orchestrator.no_spans",
            task_id=str(task_id),
        )
        return counts

    # 3) Per-span assessment.
    for span_id in span_ids:
        idempotency_key = _build_idempotency_key(
            task_id=task_id, evidence_span_id=span_id
        )
        # Minimal traceability payload. The evaluator preserves the
        # caller payload verbatim under ``payload.input_payload`` so a
        # future reader can correlate the row with the originating
        # task.created pipeline run. We deliberately keep this payload
        # small: it is shared across redeliveries and we do NOT want
        # to record per-redelivery metadata (event_id, attempt_no) here
        # since that would make different replays look different in the
        # JSONB while the rest of the row is idempotent.
        call_payload = {
            "trigger": "task_created_pipeline",
            "task_id": str(task_id),
            "evidence_span_id": str(span_id),
        }
        result = assess_source_quality(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=span_id,
            idempotency_key=idempotency_key,
            payload=call_payload,
        )
        status = str(result.get("status", ""))
        if status == "assessed":
            counts["assessed_count"] += 1
        elif status == "already_assessed":
            counts["already_assessed_count"] += 1
        elif status == "not_found":
            # The span vanished between our SELECT DISTINCT and the
            # evaluator's FOR UPDATE lock. Extremely narrow race
            # (same transaction) but possible if a future change ever
            # introduces a DELETE on evidence_spans. We do NOT raise.
            counts["not_found_count"] += 1
            logger.warning(
                "source_quality_orchestrator.span_not_found",
                task_id=str(task_id),
                evidence_span_id=str(span_id),
            )
        elif status == "invalid_target":
            # Cannot happen given how we call the evaluator (we always
            # pass exactly one target). Counted defensively so a future
            # programming error surfaces visibly via this counter
            # rather than via a silent assertion failure.
            counts["invalid_target_count"] += 1
            logger.error(
                "source_quality_orchestrator.invalid_target_unexpected",
                task_id=str(task_id),
                evidence_span_id=str(span_id),
                evaluator_result=result,
            )
        else:
            # Unknown service status: treat as an error condition for
            # accounting purposes, but still do NOT raise (the
            # task-pipeline savepoint in the caller would roll back
            # the whole step otherwise, which is over-reaction for a
            # single weird row). Log loudly so a regression is
            # noticed.
            counts["error_count"] += 1
            logger.error(
                "source_quality_orchestrator.unknown_service_status",
                task_id=str(task_id),
                evidence_span_id=str(span_id),
                service_status=status,
                evaluator_result=result,
            )

    logger.info(
        "source_quality_orchestrator.completed",
        task_id=str(task_id),
        spans_total=counts["spans_total"],
        assessed_count=counts["assessed_count"],
        already_assessed_count=counts["already_assessed_count"],
        not_found_count=counts["not_found_count"],
        invalid_target_count=counts["invalid_target_count"],
        error_count=counts["error_count"],
    )
    return counts
