"""task.created consumer (Phase 8.4, realigned + Phase 8.7E source quality).

Pipeline single-consumer. The 8.3 pipeline (extractor + CVE-lite -> analyzed_partial)
is preserved. After 'task.analyzed_partial', a mock source quality assessment step
(Phase 8.7E) is run, wrapped in a SAVEPOINT so its failure cannot block 8.4. Then
the consumer continues in the same event loop with the 8.4 stage (compiler +
final answer gate -> published or publication_held).

Terminality (Phase 8.4, FINAL):
  - blocked                                                             -> terminal
  - published                                                            -> terminal
  - analyzed_partial AND final_gate_reports exists for task              -> terminal (rejected scenario)
  - analyzed_partial AND no final_gate_reports                           -> NOT terminal, proceeds to compiling
  - compiling AND no final_gate_reports                                  -> NOT terminal, RESUMES compiler+gate
  - compiling AND final_gate_reports exists                              -> finalize: drive task to
                                                                            'published' (if approved) or
                                                                            back to 'analyzed_partial' (if rejected),
                                                                            using the existing report. NEVER mark
                                                                            succeeded leaving the task in 'compiling'
                                                                            without a final_gate_report.
  - created/analyzing                                                    -> normal pipeline

Audit sequence (with documents, approved scenario):
  task.analyzing
  task.docs_loaded
  task.claims_extracted
  task.claims_classified
  task.claims_ledger_initialized
  task.cve_lite_started
  task.cve_lite_completed
  task.analyzed_partial
  task.source_quality_assessed
  task.compiling
  task.draft_compiled
  task.final_gate_started
  task.final_gate_completed
  task.published

Audit sequence (with documents, rejected scenario):
  ... up to task.final_gate_completed, then:
  task.publication_held

Resume cases (NOT a fresh run): when the consumer enters with the task already
in 'compiling' or 'analyzed_partial', the corresponding state-transition audit
events are NOT re-emitted (they were emitted on the original run). The
sub-pipeline functions are guarded by status conditions to keep audit history
free of duplicates. The 8.7E step lives inside _run_8_3_extract_and_verify
(fresh-run path only), so task.source_quality_assessed is not re-emitted on
resume either.

Phase 8.7E invariants:
  - The source quality step is invoked ONLY in the fresh-run path
    (_run_8_3_extract_and_verify, AFTER the 'analyzed_partial' transition audit).
  - Its failure is NEVER allowed to break the 8.4 pipeline: the call is wrapped
    in conn.begin_nested() so DB exceptions roll back the savepoint and leave the
    outer transaction usable. The aggregated audit event
    task.source_quality_assessed is emitted with status='completed' on success
    and status='failed' on failure.
  - No new Redis stream, no new consumer, no new dispatcher entry, no change to
    main.py, no change to Final Answer Gate, no change to Claim Ledger.

FK-safety (8.1d-patch1) and post-commit publish (8.1d) preserved.
Idempotent under redelivery: every state transition is guarded by the current
status; every INSERT in services uses ON CONFLICT DO NOTHING on UNIQUE
constraints declared in 0004 / 0005 / 0007.
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evidencefirst_shared.db.audit import audit_append
from evidencefirst_shared.db.idempotency import begin_processing, mark_failed, mark_succeeded

from ..db import transaction
from ..services.compiler import run_compilation
from ..services.cve_lite import run_cve_lite
from ..services.extractor import run_extraction
from ..services.final_answer_gate import run_final_answer_gate
from ..services.source_quality_evaluator import (
    DEFAULT_POLICY_NAME as SOURCE_QUALITY_POLICY_NAME,
    DEFAULT_POLICY_VERSION as SOURCE_QUALITY_POLICY_VERSION,
    SERVICE_NAME as SOURCE_QUALITY_SERVICE_NAME,
    SERVICE_VERSION as SOURCE_QUALITY_SERVICE_VERSION,
)
from ..services.source_quality_orchestrator import run_source_quality_assessment


logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# read helpers
# ---------------------------------------------------------------------------
def _fetch_task_status(conn: Connection, task_id: uuid.UUID) -> str | None:
    row = conn.execute(
        text("SELECT status FROM task_masters WHERE id = :id"),
        {"id": task_id},
    ).first()
    return None if row is None else str(row[0])


def _has_documents(conn: Connection, task_id: uuid.UUID) -> bool:
    n = conn.execute(
        text("SELECT COUNT(*) FROM task_documents WHERE task_id = :id"),
        {"id": task_id},
    ).scalar_one()
    return int(n) > 0


def _fetch_existing_gate_report_for_task(
    conn: Connection, task_id: uuid.UUID
) -> dict[str, Any] | None:
    """Return the existing final_gate_reports row for the task, or None.

    Thanks to the composite FK final_gate_reports_draft_consistency in 0005,
    final_gate_reports.task_id is guaranteed to match the underlying draft's
    task_id, so we can query final_gate_reports directly.
    """
    row = conn.execute(
        text(
            """
            SELECT id, draft_final_answer_id, decision, reason_code
            FROM final_gate_reports
            WHERE task_id = :tid
            LIMIT 1
            """
        ),
        {"tid": task_id},
    ).first()
    if row is None:
        return None
    return dict(row._mapping)


def _fetch_existing_published_answer_for_task(
    conn: Connection, task_id: uuid.UUID
) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """
            SELECT id
            FROM published_answers
            WHERE task_id = :tid AND version_no = 1
            LIMIT 1
            """
        ),
        {"tid": task_id},
    ).first()
    if row is None:
        return None
    return dict(row._mapping)


def _docs_load_counts(conn: Connection, task_id: uuid.UUID) -> dict[str, int]:
    row = conn.execute(
        text(
            """
            SELECT
              COUNT(DISTINCT td.document_id) AS doc_count,
              COUNT(DISTINCT dv.id)          AS parsed_version_count,
              COUNT(DISTINCT dc.id)          AS chunk_count
            FROM task_documents td
            JOIN uploaded_documents ud ON ud.id = td.document_id
            LEFT JOIN document_versions dv
              ON dv.document_id = ud.id AND dv.version_kind = 'parsed'
            LEFT JOIN document_chunks dc
              ON dc.document_version_id = dv.id
            WHERE td.task_id = :tid
            """
        ),
        {"tid": task_id},
    ).one()
    return {
        "document_count": int(row[0] or 0),
        "parsed_version_count": int(row[1] or 0),
        "chunk_count": int(row[2] or 0),
    }


# ---------------------------------------------------------------------------
# state transitions (8.3)
# ---------------------------------------------------------------------------
def _advance_to_analyzing(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    consumer_name: str,
) -> None:
    r = conn.execute(
        text(
            """
            UPDATE task_masters SET status = 'analyzing'
            WHERE id = :id AND status = 'created'
            RETURNING id
            """
        ),
        {"id": task_id},
    ).first()
    if r is None:
        return
    audit_append(
        conn,
        chain_scope="task",
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        session_id=None,
        event_type="task.analyzing",
        actor_type="job",
        actor_id=consumer_name,
        redacted_payload={"transition": "created->analyzing"},
        related_entity_type="task_masters",
        related_entity_id=task_id,
    )


def _advance_to_blocked_no_docs(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    consumer_name: str,
) -> None:
    r = conn.execute(
        text(
            """
            UPDATE task_masters SET status = 'blocked'
            WHERE id = :id AND status = 'analyzing'
            RETURNING id
            """
        ),
        {"id": task_id},
    ).first()
    if r is None:
        return
    audit_append(
        conn,
        chain_scope="task",
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        session_id=None,
        event_type="task.blocked",
        actor_type="job",
        actor_id=consumer_name,
        redacted_payload={
            "transition": "analyzing->blocked",
            "reason": "mvp0_stub_pipeline_not_implemented_no_documents",
        },
        related_entity_type="task_masters",
        related_entity_id=task_id,
    )


# ---------------------------------------------------------------------------
# Phase 8.7E — source quality step (savepoint-wrapped)
# ---------------------------------------------------------------------------
def _run_8_7_source_quality(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    consumer_name: str,
) -> None:
    """Run the mock source quality assessment step for the task.

    Phase 8.7E (Block E) integration. The orchestrator call is wrapped
    in a SAVEPOINT (``conn.begin_nested()``) so that any exception
    raised inside the orchestrator or by the underlying evaluator does
    NOT abort the caller's outer transaction and does NOT break the
    8.4 pipeline that runs after this step.

    Try/except scoping (Correction 2 of 8.7E micro-fix):
      The ``try`` covers ONLY the savepoint-wrapped call to
      ``run_source_quality_assessment``. The success ``audit_append``
      is emitted OUTSIDE the try: a failure there is a separate fault
      and must NOT be reclassified as a source-quality failure audit.
      If the success audit itself raises, the exception propagates to
      the caller of this function (handle_task_created), which already
      classifies any pipeline exception as ``WORKER_TASK_CREATED_FAIL``
      on the EPR row.

    Side effects:
      - On success: at most one INSERT per evidence_span_id linked to
        the task via claim_evidence_links into
        ``source_quality_assessments`` (idempotent across redelivery),
        plus exactly one ``task.source_quality_assessed`` audit event
        on the task's audit chain with ``status='completed'`` and
        aggregated counts.
      - On orchestrator failure: the SAVEPOINT is rolled back, so any
        partial INSERT in ``source_quality_assessments`` from this
        call is discarded. The outer transaction is left usable, and
        exactly one ``task.source_quality_assessed`` audit event is
        emitted with ``status='failed'``. The exception is NOT
        re-raised; the pipeline continues to 8.4.

    Invariants:
      - This function does NOT mutate ``task_masters.status``,
        ``claim_ledger_entries``, ``final_gate_reports``,
        ``published_answers``, ``source_loss_*``, or any other domain
        table outside of ``source_quality_assessments`` (via the
        orchestrator).
      - The audit payload exposes counts, NOT individual scores or
        per-span identifiers. Detailed inspection is via the (future)
        8.7F read API.
      - Stack traces are NEVER included in the audit payload on
        failure: only ``error_type`` (the exception class name) is
        recorded. Full diagnostics live in the structured log.
    """
    try:
        with conn.begin_nested():
            counts = run_source_quality_assessment(conn, task_id=task_id)
    except Exception as exc:
        # SAVEPOINT auto-rolled-back by the context manager: any partial
        # source_quality_assessments INSERT from this call is gone, and
        # the outer transaction is usable again. We log the failure with
        # full stack trace, then emit a structured 'failed' audit so
        # the operator can observe the incident via the audit chain.
        #
        # We deliberately catch BLE001 (broad except): the savepoint is
        # precisely the mechanism that lets us safely absorb ANY
        # exception here without poisoning the pipeline.
        logger.exception(
            "task_created.source_quality_failed",
            task_id=str(task_id),
            error_type=type(exc).__name__,
        )
        audit_append(
            conn,
            chain_scope="task",
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            session_id=None,
            event_type="task.source_quality_assessed",
            actor_type="job",
            actor_id=consumer_name,
            redacted_payload={
                "service_name": SOURCE_QUALITY_SERVICE_NAME,
                "service_version": SOURCE_QUALITY_SERVICE_VERSION,
                "policy_name": SOURCE_QUALITY_POLICY_NAME,
                "policy_version": SOURCE_QUALITY_POLICY_VERSION,
                "evaluated_target_kind": "evidence_span",
                "status": "failed",
                "error_type": type(exc).__name__,
                "counts": {
                    "status": "failed",
                    "spans_total": 0,
                    "assessed_count": 0,
                    "already_assessed_count": 0,
                    "not_found_count": 0,
                    "invalid_target_count": 0,
                    "error_count": 1,
                },
            },
            related_entity_type="task_masters",
            related_entity_id=task_id,
        )
        # Do NOT re-raise: the savepoint + audit pair exists precisely
        # to keep 8.4 running even if 8.7E fails. The pipeline
        # continues from the caller's site.
        return

    # Success path: emit the aggregated audit OUTSIDE the try/except.
    # A failure here is NOT a source-quality failure (the orchestrator
    # already completed), so we deliberately let it propagate to
    # handle_task_created, which will classify it as
    # WORKER_TASK_CREATED_FAIL on the EPR row.
    audit_append(
        conn,
        chain_scope="task",
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        session_id=None,
        event_type="task.source_quality_assessed",
        actor_type="job",
        actor_id=consumer_name,
        redacted_payload={
            "service_name": SOURCE_QUALITY_SERVICE_NAME,
            "service_version": SOURCE_QUALITY_SERVICE_VERSION,
            "policy_name": SOURCE_QUALITY_POLICY_NAME,
            "policy_version": SOURCE_QUALITY_POLICY_VERSION,
            "evaluated_target_kind": "evidence_span",
            "status": "completed",
            "counts": counts,
        },
        related_entity_type="task_masters",
        related_entity_id=task_id,
    )


# ---------------------------------------------------------------------------
# 8.3 sub-pipeline (extract + cve_lite + analyzed_partial)
# ---------------------------------------------------------------------------
def _run_8_3_extract_and_verify(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    consumer_name: str,
) -> None:
    counts_loaded = _docs_load_counts(conn, task_id)
    audit_append(
        conn,
        chain_scope="task",
        tenant_id=tenant_id, project_id=project_id, task_id=task_id, session_id=None,
        event_type="task.docs_loaded",
        actor_type="job", actor_id=consumer_name,
        redacted_payload={"counts": counts_loaded},
        related_entity_type="task_masters",
        related_entity_id=task_id,
    )

    counts_extr = run_extraction(
        conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    audit_append(
        conn,
        chain_scope="task",
        tenant_id=tenant_id, project_id=project_id, task_id=task_id, session_id=None,
        event_type="task.claims_extracted",
        actor_type="job", actor_id=consumer_name,
        redacted_payload={
            "raw_claims_created": counts_extr["raw_claims_created"],
            "raw_claims_total": counts_extr["raw_claims_total"],
            "logical_claims_total": counts_extr["logical_claims_total"],
        },
        related_entity_type="task_masters",
        related_entity_id=task_id,
    )
    audit_append(
        conn,
        chain_scope="task",
        tenant_id=tenant_id, project_id=project_id, task_id=task_id, session_id=None,
        event_type="task.claims_classified",
        actor_type="job", actor_id=consumer_name,
        redacted_payload={
            "classified_claims_created": counts_extr["classified_claims_created"],
            "classified_claims_total": counts_extr["classified_claims_total"],
        },
        related_entity_type="task_masters",
        related_entity_id=task_id,
    )
    audit_append(
        conn,
        chain_scope="task",
        tenant_id=tenant_id, project_id=project_id, task_id=task_id, session_id=None,
        event_type="task.claims_ledger_initialized",
        actor_type="job", actor_id=consumer_name,
        redacted_payload={
            "ledger_v1_created": counts_extr["ledger_v1_created"],
            "ledger_v1_total": counts_extr["ledger_v1_total"],
        },
        related_entity_type="task_masters",
        related_entity_id=task_id,
    )

    audit_append(
        conn,
        chain_scope="task",
        tenant_id=tenant_id, project_id=project_id, task_id=task_id, session_id=None,
        event_type="task.cve_lite_started",
        actor_type="job", actor_id=consumer_name,
        redacted_payload={"checker": "mvp0_cve_lite_v1"},
        related_entity_type="task_masters",
        related_entity_id=task_id,
    )
    cve_counts = run_cve_lite(conn, task_id=task_id)
    audit_append(
        conn,
        chain_scope="task",
        tenant_id=tenant_id, project_id=project_id, task_id=task_id, session_id=None,
        event_type="task.cve_lite_completed",
        actor_type="job", actor_id=consumer_name,
        redacted_payload={
            "checked": cve_counts["checked"],
            "pass": cve_counts["pass"],
            "fail": cve_counts["fail"],
            "v2_created": cve_counts["v2_created"],
        },
        related_entity_type="task_masters",
        related_entity_id=task_id,
    )

    r = conn.execute(
        text(
            """
            UPDATE task_masters SET status = 'analyzed_partial'
            WHERE id = :id AND status = 'analyzing'
            RETURNING id
            """
        ),
        {"id": task_id},
    ).first()
    if r is None:
        # The 'analyzing -> analyzed_partial' transition did not happen
        # on this call (concurrent execution / unexpected state). Do NOT
        # emit the analyzed_partial audit and do NOT run the 8.7E step:
        # both belong to the fresh run that produced the transition.
        return
    audit_append(
        conn,
        chain_scope="task",
        tenant_id=tenant_id, project_id=project_id, task_id=task_id, session_id=None,
        event_type="task.analyzed_partial",
        actor_type="job", actor_id=consumer_name,
        redacted_payload={
            "transition": "analyzing->analyzed_partial",
            "reason": "claims_verified_by_cve_lite_compilation_pending",
            "counts": {
                "documents": counts_loaded,
                "claims": {
                    "raw_claims_total": counts_extr["raw_claims_total"],
                    "classified_claims_total": counts_extr["classified_claims_total"],
                    "ledger_v1_total": counts_extr["ledger_v1_total"],
                    "logical_claims_total": counts_extr["logical_claims_total"],
                },
                "cve_lite": cve_counts,
            },
        },
        related_entity_type="task_masters",
        related_entity_id=task_id,
    )

    # Phase 8.7E: mock source quality assessment, AFTER task.analyzed_partial
    # and BEFORE the 8.4 stage (which is invoked by the orchestrator
    # _run_pipeline_with_docs after this function returns). The call is
    # SAVEPOINT-wrapped so its failure does not abort the outer
    # transaction; the aggregated task.source_quality_assessed audit is
    # emitted either way.
    #
    # Resume safety: this point is reached ONLY when the
    # 'analyzing -> analyzed_partial' transition succeeded on THIS call
    # (the early return above guards against everything else). A resume
    # from an already-analyzed_partial or compiling task does NOT enter
    # _run_8_3_extract_and_verify (see _run_pipeline_with_docs), so the
    # 8.7E audit is never re-emitted on redelivery.
    _run_8_7_source_quality(
        conn,
        task_id=task_id,
        tenant_id=tenant_id,
        project_id=project_id,
        consumer_name=consumer_name,
    )


# ---------------------------------------------------------------------------
# 8.4 sub-pipeline (compiler + gate)
# ---------------------------------------------------------------------------
def _advance_to_compiling(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    consumer_name: str,
) -> bool:
    """Try to transition analyzed_partial -> compiling. Audit on success.

    Returns True if the transition happened, False otherwise.
    """
    r = conn.execute(
        text(
            """
            UPDATE task_masters SET status = 'compiling'
            WHERE id = :id AND status = 'analyzed_partial'
            RETURNING id
            """
        ),
        {"id": task_id},
    ).first()
    if r is None:
        return False
    audit_append(
        conn,
        chain_scope="task",
        tenant_id=tenant_id, project_id=project_id, task_id=task_id, session_id=None,
        event_type="task.compiling",
        actor_type="job", actor_id=consumer_name,
        redacted_payload={"transition": "analyzed_partial->compiling"},
        related_entity_type="task_masters",
        related_entity_id=task_id,
    )
    return True


def _advance_to_published(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    consumer_name: str,
    final_gate_report_id: str,
    published_answer_id: str,
    reason_code: str,
) -> bool:
    """Try to transition compiling -> published. Audit on success.

    Returns True if the transition happened, False otherwise.
    """
    r = conn.execute(
        text(
            """
            UPDATE task_masters SET status = 'published'
            WHERE id = :id AND status = 'compiling'
            RETURNING id
            """
        ),
        {"id": task_id},
    ).first()
    if r is None:
        return False
    audit_append(
        conn,
        chain_scope="task",
        tenant_id=tenant_id, project_id=project_id, task_id=task_id, session_id=None,
        event_type="task.published",
        actor_type="job", actor_id=consumer_name,
        redacted_payload={
            "transition": "compiling->published",
            "reason_code": reason_code,
            "final_gate_report_id": final_gate_report_id,
            "published_answer_id": published_answer_id,
        },
        related_entity_type="task_masters",
        related_entity_id=task_id,
    )
    return True


def _revert_to_analyzed_partial_with_held(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    consumer_name: str,
    final_gate_report_id: str,
    reason_code: str,
    coverage_gaps_emitted: int,
) -> bool:
    """Try to transition compiling -> analyzed_partial with publication_held audit.

    Returns True if the transition happened, False otherwise.
    """
    r = conn.execute(
        text(
            """
            UPDATE task_masters SET status = 'analyzed_partial'
            WHERE id = :id AND status = 'compiling'
            RETURNING id
            """
        ),
        {"id": task_id},
    ).first()
    if r is None:
        return False
    audit_append(
        conn,
        chain_scope="task",
        tenant_id=tenant_id, project_id=project_id, task_id=task_id, session_id=None,
        event_type="task.publication_held",
        actor_type="job", actor_id=consumer_name,
        redacted_payload={
            "transition": "compiling->analyzed_partial",
            "reason_code": reason_code,
            "final_gate_report_id": final_gate_report_id,
            "coverage_gaps_emitted": int(coverage_gaps_emitted),
        },
        related_entity_type="task_masters",
        related_entity_id=task_id,
    )
    return True


def _finalize_from_existing_gate_report(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    consumer_name: str,
    gate_report: dict[str, Any],
) -> None:
    """Drive the task to a coherent terminal status based on an existing
    final_gate_reports row.

    Used when the consumer enters with status='compiling' AND a final_gate_report
    already exists (e.g. crash between gate INSERT and task_masters UPDATE on a
    previous attempt). Idempotent: if the task is already in the target status,
    the guarded UPDATE is a no-op and no audit is emitted.
    """
    decision = str(gate_report["decision"])
    reason_code = str(gate_report["reason_code"])
    report_id = str(gate_report["id"])

    if decision == "approved":
        published = _fetch_existing_published_answer_for_task(conn, task_id)
        published_answer_id = str(published["id"]) if published is not None else ""
        _advance_to_published(
            conn,
            task_id=task_id,
            tenant_id=tenant_id,
            project_id=project_id,
            consumer_name=consumer_name,
            final_gate_report_id=report_id,
            published_answer_id=published_answer_id,
            reason_code=reason_code,
        )
    elif decision == "rejected":
        _revert_to_analyzed_partial_with_held(
            conn,
            task_id=task_id,
            tenant_id=tenant_id,
            project_id=project_id,
            consumer_name=consumer_name,
            final_gate_report_id=report_id,
            reason_code=reason_code,
            coverage_gaps_emitted=0,
        )
    else:
        # 'held_for_review' or any other future decision: leave the task in
        # 'compiling' for an operator to inspect. Future phases may add a
        # specific state for this.
        logger.warning(
            "task_created.gate_report_unknown_decision",
            task_id=str(task_id),
            decision=decision,
        )


def _run_8_4_compile_and_gate(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    consumer_name: str,
) -> None:
    """Run the 8.4 stage: compiler -> gate -> publish/hold.

    Pre-condition: the task is in status 'analyzed_partial' or 'compiling',
    and NO final_gate_report exists yet for this task. The orchestrator
    `_run_pipeline_with_docs` checks these conditions before calling.

    Idempotent w.r.t. database side-effects via ON CONFLICT DO NOTHING. Audit
    side-effects are guarded by status-based UPDATEs: re-running this function
    on a task already in 'compiling' (because a previous attempt crashed after
    the compiling transition) will NOT re-emit task.compiling but WILL run
    compiler+gate (idempotently) and emit the remaining 8.4 audit events.
    """
    # Try to make the analyzed_partial -> compiling transition. If we are already
    # in 'compiling' (resume case), this is a no-op and we skip the audit.
    _advance_to_compiling(
        conn,
        task_id=task_id,
        tenant_id=tenant_id,
        project_id=project_id,
        consumer_name=consumer_name,
    )

    # Run the compiler (idempotent).
    compile_counts = run_compilation(
        conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    audit_append(
        conn,
        chain_scope="task",
        tenant_id=tenant_id, project_id=project_id, task_id=task_id, session_id=None,
        event_type="task.draft_compiled",
        actor_type="job", actor_id=consumer_name,
        redacted_payload={
            "verified_claim_count": compile_counts["verified_claim_count"],
            "spans_inserted": compile_counts["spans_inserted"],
            "links_inserted": compile_counts["links_inserted"],
            "summary_chars": compile_counts["summary_chars"],
            "draft_id": compile_counts["draft_id"],
        },
        related_entity_type="task_masters",
        related_entity_id=task_id,
    )

    audit_append(
        conn,
        chain_scope="task",
        tenant_id=tenant_id, project_id=project_id, task_id=task_id, session_id=None,
        event_type="task.final_gate_started",
        actor_type="job", actor_id=consumer_name,
        redacted_payload={"gate": "mvp0_gate_v1"},
        related_entity_type="task_masters",
        related_entity_id=task_id,
    )
    gate_outcome = run_final_answer_gate(
        conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    audit_append(
        conn,
        chain_scope="task",
        tenant_id=tenant_id, project_id=project_id, task_id=task_id, session_id=None,
        event_type="task.final_gate_completed",
        actor_type="job", actor_id=consumer_name,
        redacted_payload={
            "decision": gate_outcome["decision"],
            "reason_code": gate_outcome["reason_code"],
            "spans_total": gate_outcome["spans_total"],
            "spans_verified": gate_outcome["spans_verified"],
            "spans_unverified": gate_outcome["spans_unverified"],
            "coverage_gaps_emitted": gate_outcome["coverage_gaps_emitted"],
            "final_gate_report_id": gate_outcome["final_gate_report_id"],
        },
        related_entity_type="task_masters",
        related_entity_id=task_id,
    )

    if gate_outcome["decision"] == "approved":
        published_answer_id = gate_outcome["published_answer_id"] or ""
        _advance_to_published(
            conn,
            task_id=task_id,
            tenant_id=tenant_id,
            project_id=project_id,
            consumer_name=consumer_name,
            final_gate_report_id=str(gate_outcome["final_gate_report_id"]),
            published_answer_id=str(published_answer_id),
            reason_code=str(gate_outcome["reason_code"]),
        )
    elif gate_outcome["decision"] == "rejected":
        _revert_to_analyzed_partial_with_held(
            conn,
            task_id=task_id,
            tenant_id=tenant_id,
            project_id=project_id,
            consumer_name=consumer_name,
            final_gate_report_id=str(gate_outcome["final_gate_report_id"]),
            reason_code=str(gate_outcome["reason_code"]),
            coverage_gaps_emitted=int(gate_outcome["coverage_gaps_emitted"]),
        )
    else:
        # 'no_draft' is defensive and not expected in 8.4: the compiler always
        # produces a v1 draft. If we ever hit this, leave the task in 'compiling'
        # for diagnosis on the next redelivery.
        logger.warning(
            "task_created.gate_decision_unexpected",
            task_id=str(task_id),
            decision=gate_outcome["decision"],
            reason_code=gate_outcome["reason_code"],
        )


# ---------------------------------------------------------------------------
# top-level pipeline orchestrator (8.3 + 8.4)
# ---------------------------------------------------------------------------
def _run_pipeline_with_docs(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    consumer_name: str,
) -> None:
    """Run the 8.3 stage (if needed) followed by the 8.4 stage."""
    current_status = _fetch_task_status(conn, task_id)

    # 8.3 stage runs from analyzing -> analyzed_partial. It also runs
    # the 8.7E source quality step internally, AFTER task.analyzed_partial
    # and BEFORE returning here.
    if current_status == "analyzing":
        _run_8_3_extract_and_verify(
            conn,
            task_id=task_id,
            tenant_id=tenant_id,
            project_id=project_id,
            consumer_name=consumer_name,
        )
        current_status = _fetch_task_status(conn, task_id)

    # 8.4 stage starts from analyzed_partial or RESUMES from compiling.
    # Resume runs do NOT enter _run_8_3_extract_and_verify above, so the
    # 8.7E source quality audit is emitted exactly once per task lifetime
    # in the fresh-run path.
    if current_status in ("analyzed_partial", "compiling"):
        existing_report = _fetch_existing_gate_report_for_task(conn, task_id)
        if existing_report is None:
            # No gate report yet: run (or resume) compiler + gate end-to-end.
            _run_8_4_compile_and_gate(
                conn,
                task_id=task_id,
                tenant_id=tenant_id,
                project_id=project_id,
                consumer_name=consumer_name,
            )
        else:
            # A gate report already exists. Two sub-cases:
            #   - status == 'analyzed_partial': caller-side terminality check
            #     should have caught this before calling us. Defensive no-op.
            #   - status == 'compiling': finalize the task using the existing
            #     report (drive to 'published' or back to 'analyzed_partial'),
            #     so we never leave it in 'compiling' without a coherent state.
            if current_status == "compiling":
                _finalize_from_existing_gate_report(
                    conn,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    consumer_name=consumer_name,
                    gate_report=existing_report,
                )


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def handle_task_created(event: dict[str, Any], *, consumer_name: str) -> str:
    """Process a single task.created event.

    Returns one of:
      'processed', 'skipped_already_succeeded', 'skipped_terminal', 'failed'.
    """
    try:
        event_id = uuid.UUID(event["event_id"])
        tenant_id = uuid.UUID(event["tenant_id"])
        project_id = uuid.UUID(event["project_id"])
        task_id = uuid.UUID(event["task_id"])
        idempotency_key = event.get("idempotency_key") or str(task_id)
    except (KeyError, ValueError) as exc:
        logger.error("task_created.malformed_event", error=str(exc), event=event)
        return "failed"

    with transaction() as conn:
        # FK-safety branch (8.1d-patch1): task must be visible BEFORE begin_processing
        # is called with task_id != None.
        current_status_pre = _fetch_task_status(conn, task_id)
        if current_status_pre is None:
            record_id, status = begin_processing(
                conn,
                event_id=event_id,
                event_type="task.created",
                consumer_name=consumer_name,
                idempotency_key=idempotency_key,
                tenant_id=tenant_id,
                project_id=project_id,
                task_id=None,
            )
            if status == "succeeded":
                return "skipped_already_succeeded"
            mark_failed(
                conn,
                record_id=record_id,
                error_code="WORKER_TASK_NOT_VISIBLE",
                error_message=f"Task {task_id} not visible at processing time; will retry.",
            )
            return "failed"

        record_id, status = begin_processing(
            conn,
            event_id=event_id,
            event_type="task.created",
            consumer_name=consumer_name,
            idempotency_key=idempotency_key,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        if status == "succeeded":
            return "skipped_already_succeeded"

        current_status = _fetch_task_status(conn, task_id)
        if current_status is None:
            mark_failed(
                conn,
                record_id=record_id,
                error_code="WORKER_TASK_NOT_VISIBLE",
                error_message=f"Task {task_id} disappeared between checks.",
            )
            return "failed"

        try:
            # ---- Terminality checks (Phase 8.4, FINAL) ----
            if current_status in ("blocked", "published"):
                mark_succeeded(conn, record_id=record_id)
                return "skipped_terminal"

            if current_status == "analyzed_partial":
                if _fetch_existing_gate_report_for_task(conn, task_id) is not None:
                    # Rejected scenario terminale: gate has already produced a
                    # report and the task has been brought back to
                    # analyzed_partial. We do NOT recompile.
                    mark_succeeded(conn, record_id=record_id)
                    return "skipped_terminal"
                # else: not terminal, proceeds to compiling (handled below).

            # 'compiling' is NEVER terminal on its own. We will resume or finalize
            # via _run_pipeline_with_docs below. Even if a final_gate_report
            # already exists, we MUST drive the status forward so we never
            # mark_succeeded with the task stuck in 'compiling'.

            if current_status not in ("created", "analyzing", "analyzed_partial", "compiling"):
                mark_failed(
                    conn,
                    record_id=record_id,
                    error_code="WORKER_UNEXPECTED_STATUS",
                    error_message=f"Task {task_id} in unexpected status {current_status!r}.",
                )
                return "failed"

            has_docs = _has_documents(conn, task_id)

            # 'created' -> 'analyzing' (audit included).
            if current_status == "created":
                _advance_to_analyzing(
                    conn,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    consumer_name=consumer_name,
                )

            if has_docs:
                _run_pipeline_with_docs(
                    conn,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    consumer_name=consumer_name,
                )
            else:
                _advance_to_blocked_no_docs(
                    conn,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    consumer_name=consumer_name,
                )

            # Final defensive check: if the pipeline left the task in 'compiling'
            # without a final_gate_report (compiler crashed between INSERT and
            # later steps in a previous attempt), do NOT mark_succeeded. Mark
            # the EPR as failed so a redelivery can resume.
            final_status = _fetch_task_status(conn, task_id)
            if final_status == "compiling" and _fetch_existing_gate_report_for_task(conn, task_id) is None:
                mark_failed(
                    conn,
                    record_id=record_id,
                    error_code="WORKER_PIPELINE_INCOMPLETE",
                    error_message=(
                        f"Task {task_id} left in 'compiling' without a final_gate_report; "
                        "redelivery will resume."
                    ),
                )
                return "failed"

            mark_succeeded(conn, record_id=record_id)
            return "processed"
        except Exception as exc:
            logger.exception("task_created.failed", task_id=str(task_id))
            mark_failed(
                conn,
                record_id=record_id,
                error_code="WORKER_TASK_CREATED_FAIL",
                error_message=str(exc)[:500],
            )
            return "failed"
