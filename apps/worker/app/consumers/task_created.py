"""task.created consumer (Phase 8.3).

If the task has attached documents:
  - created -> analyzing  (audit task.analyzing)
  - audit task.docs_loaded with counts
  - run extractor:
      audit task.claims_extracted
      audit task.claims_classified
      audit task.claims_ledger_initialized
  - run CVE-lite:
      audit task.cve_lite_started
      audit task.cve_lite_completed
  - analyzing -> analyzed_partial
      audit task.analyzed_partial
        reason='claims_verified_by_cve_lite_compilation_pending'

If the task has no attached documents:
  - created -> analyzing -> blocked (unchanged from 8.1d-patch1)

FK-safety (8.1d-patch1) and post-commit publish (8.1d) preserved.
Idempotent under redelivery: status guards on every UPDATE; extractor and
CVE-lite use ON CONFLICT DO NOTHING on every INSERT.
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
from ..services.cve_lite import run_cve_lite
from ..services.extractor import run_extraction


logger = structlog.get_logger(__name__)


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


def _run_pipeline_with_docs(
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
            if current_status in ("blocked", "analyzed_partial"):
                mark_succeeded(conn, record_id=record_id)
                return "skipped_terminal"

            if current_status not in ("created", "analyzing"):
                mark_failed(
                    conn,
                    record_id=record_id,
                    error_code="WORKER_UNEXPECTED_STATUS",
                    error_message=f"Task {task_id} in unexpected status {current_status!r}.",
                )
                return "failed"

            has_docs = _has_documents(conn, task_id)

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