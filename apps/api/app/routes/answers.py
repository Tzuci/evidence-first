"""Read-only API routes for Phase 8.4 answers (draft, gate report, published).

Endpoints:
  GET /api/v1/tasks/{task_id}/draft
  GET /api/v1/tasks/{task_id}/final-gate-report
  GET /api/v1/tasks/{task_id}/published-answer
  GET /api/v1/published-answers/{published_answer_id}

Invariants:
  - All endpoints are strictly read-only: no INSERT / UPDATE / DELETE.
  - 404 errors are normalized via NormalizedError + ErrorCode.RESOURCE_NOT_FOUND.
  - When the task itself does not exist, we return RESOURCE_NOT_FOUND with
    details.resource = "task_masters", regardless of the sub-resource.
  - When the task exists but the sub-resource does not (no draft yet, no gate
    report yet, not yet published), we return RESOURCE_NOT_FOUND with the
    appropriate sub-resource hint in details.resource so the client can
    distinguish:
        details.resource = "draft_final_answers"
        details.resource = "final_gate_reports"
        details.resource = "published_answers"
  - In MVP-0 the ErrorCode enum does NOT contain a NOT_PUBLISHED variant
    (verified against packages/shared/evidencefirst_shared/errors.py); we
    therefore use RESOURCE_NOT_FOUND with details.resource="published_answers"
    for the not-yet-published case. If a NOT_PUBLISHED code is added in a
    later phase (e.g. lifecycle), only the call site for that endpoint needs
    to change.
  - Composite FK constraints in 0005 guarantee that if a final_gate_report or
    published_answer exists for a task, it is consistent with the underlying
    draft. This module does NOT duplicate those checks.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evidencefirst_shared.errors import ErrorCode, NormalizedError
from evidencefirst_shared.schemas import (
    CoverageGapStatementRead,
    DraftFinalAnswerRead,
    DraftFinalAnswerWithSpansRead,
    FinalAnswerSpanRead,
    FinalGateReportRead,
    PublishedAnswerRead,
)

from ..db import get_conn


router = APIRouter()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _task_exists(conn: Connection, task_id: uuid.UUID) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM task_masters WHERE id = :tid LIMIT 1"),
        {"tid": task_id},
    ).first()
    return row is not None


def _raise_not_found(resource: str, identifier: str) -> None:
    raise NormalizedError(
        code=ErrorCode.RESOURCE_NOT_FOUND,
        message=f"{resource} not found",
        details={"resource": resource, "id": identifier},
        http_status=404,
    )


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


# ---------------------------------------------------------------------------
# read queries
# ---------------------------------------------------------------------------
def _select_latest_draft_for_task(
    conn: Connection, task_id: uuid.UUID
) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """
            SELECT
              id, task_id, version_no,
              compiler_name, compiler_version,
              summary_text, payload, created_at
            FROM draft_final_answers
            WHERE task_id = :tid
            ORDER BY version_no DESC
            LIMIT 1
            """
        ),
        {"tid": task_id},
    ).first()
    return _row_to_dict(row) if row is not None else None


def _select_spans_for_draft(
    conn: Connection, draft_id: uuid.UUID
) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT
              id, draft_final_answer_id, span_index,
              char_start, char_end, span_text, span_hash, created_at
            FROM final_answer_spans
            WHERE draft_final_answer_id = :did
            ORDER BY span_index ASC
            """
        ),
        {"did": draft_id},
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _select_gate_report_for_task(
    conn: Connection, task_id: uuid.UUID
) -> dict[str, Any] | None:
    """Latest final_gate_reports for the task. UNIQUE on draft_final_answer_id
    plus a single draft v1 per task in 8.4 imply at most one report; the ORDER
    BY is defensive for future phases.
    """
    row = conn.execute(
        text(
            """
            SELECT
              id, task_id, draft_final_answer_id,
              decision, reason_code, payload, created_at
            FROM final_gate_reports
            WHERE task_id = :tid
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ),
        {"tid": task_id},
    ).first()
    return _row_to_dict(row) if row is not None else None


def _select_coverage_gaps_for_draft(
    conn: Connection, draft_id: uuid.UUID
) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT
              id, draft_final_answer_id, kind, severity,
              gap_key, details, created_at
            FROM coverage_gap_statements
            WHERE draft_final_answer_id = :did
            ORDER BY created_at ASC, id ASC
            """
        ),
        {"did": draft_id},
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _select_published_answer_for_task(
    conn: Connection, task_id: uuid.UUID
) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """
            SELECT
              id, task_id, draft_final_answer_id, final_gate_report_id,
              version_no, content_hash, payload, status,
              published_at, withdrawn_at, superseded_at, superseded_by_id
            FROM published_answers
            WHERE task_id = :tid
            ORDER BY version_no DESC
            LIMIT 1
            """
        ),
        {"tid": task_id},
    ).first()
    return _row_to_dict(row) if row is not None else None


def _select_published_answer_by_id(
    conn: Connection, published_answer_id: uuid.UUID
) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """
            SELECT
              id, task_id, draft_final_answer_id, final_gate_report_id,
              version_no, content_hash, payload, status,
              published_at, withdrawn_at, superseded_at, superseded_by_id
            FROM published_answers
            WHERE id = :pid
            """
        ),
        {"pid": published_answer_id},
    ).first()
    return _row_to_dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------
@router.get(
    "/api/v1/tasks/{task_id}/draft",
    response_model=DraftFinalAnswerWithSpansRead,
    tags=["answers"],
)
def get_task_draft(
    task_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
) -> DraftFinalAnswerWithSpansRead:
    """Return the latest draft_final_answers for the task, with its spans
    ordered by span_index ASC.
    """
    if not _task_exists(conn, task_id):
        _raise_not_found("task_masters", str(task_id))

    draft = _select_latest_draft_for_task(conn, task_id)
    if draft is None:
        _raise_not_found("draft_final_answers", str(task_id))

    draft_id = uuid.UUID(str(draft["id"]))
    spans = _select_spans_for_draft(conn, draft_id)

    return DraftFinalAnswerWithSpansRead(
        draft=DraftFinalAnswerRead(**draft),
        spans=[FinalAnswerSpanRead(**s) for s in spans],
    )


@router.get(
    "/api/v1/tasks/{task_id}/final-gate-report",
    response_model=FinalGateReportRead,
    tags=["answers"],
)
def get_task_final_gate_report(
    task_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
) -> FinalGateReportRead:
    """Return the latest final_gate_reports for the task with the
    coverage_gap_statements rows attached to the same draft.

    For an approved decision the coverage_gap_statements list is empty.
    """
    if not _task_exists(conn, task_id):
        _raise_not_found("task_masters", str(task_id))

    report = _select_gate_report_for_task(conn, task_id)
    if report is None:
        _raise_not_found("final_gate_reports", str(task_id))

    draft_id = uuid.UUID(str(report["draft_final_answer_id"]))
    gaps = _select_coverage_gaps_for_draft(conn, draft_id)

    return FinalGateReportRead(
        **report,
        coverage_gap_statements=[CoverageGapStatementRead(**g) for g in gaps],
    )


@router.get(
    "/api/v1/tasks/{task_id}/published-answer",
    response_model=PublishedAnswerRead,
    tags=["answers"],
)
def get_task_published_answer(
    task_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
) -> PublishedAnswerRead:
    """Return the latest published_answers for the task.

    NOTE: the MVP-0 ErrorCode enum does not contain NOT_PUBLISHED. We therefore
    return RESOURCE_NOT_FOUND with details.resource="published_answers" when
    the task exists but is not yet published. A future phase may introduce
    NOT_PUBLISHED; only this call site would need to change.
    """
    if not _task_exists(conn, task_id):
        _raise_not_found("task_masters", str(task_id))

    pa = _select_published_answer_for_task(conn, task_id)
    if pa is None:
        _raise_not_found("published_answers", str(task_id))

    return PublishedAnswerRead(**pa)


@router.get(
    "/api/v1/published-answers/{published_answer_id}",
    response_model=PublishedAnswerRead,
    tags=["answers"],
)
def get_published_answer_by_id(
    published_answer_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
) -> PublishedAnswerRead:
    """Single-row view of a published_answers entity by its id."""
    pa = _select_published_answer_by_id(conn, published_answer_id)
    if pa is None:
        _raise_not_found("published_answers", str(published_answer_id))

    return PublishedAnswerRead(**pa)
