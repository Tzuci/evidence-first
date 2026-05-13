"""API route for Phase 8.6A — published_answer lifecycle events (read-only).

Endpoint exposed:
  GET /api/v1/published-answers/{published_answer_id}/lifecycle-events

Strict invariants (Phase 8.6A — read-only observability):

  - This endpoint is COMPLETELY read-only. It MUST NOT:
      * INSERT / UPDATE / DELETE any row in any table;
      * call ``apply_withdrawal`` or any other worker service;
      * backfill missing lifecycle events (e.g. ``published`` on
        published_answers created before Phase 8.5);
      * read or verify ``audit_records``;
      * mutate ``published_answers`` or ``task_masters.status``;
      * use Redis;
      * trigger the worker in any way.

  - The endpoint surfaces exactly the rows present in
    ``published_answer_lifecycle_events`` for a given published_answer,
    ordered ASC by (created_at, id) for replay-friendliness, with
    optional filtering by ``event_type`` and a bounded ``limit``.

  - 404 RESOURCE_NOT_FOUND with details.resource="published_answers"
    and details.id=<published_answer_id> is returned when the
    published_answer does not exist. When the published_answer exists
    but has no lifecycle rows, the endpoint returns 200 with
    ``items=[]``. This is by design: published_answers created before
    the Phase 8.5 lifecycle pipeline shipped have no ``published``
    backfill event, and the GET reflects DB state truthfully without
    fabricating history.

  - All JSONB columns (``event_payload``) are returned verbatim. MVP-0
    does not yet apply RBAC redaction; this is acknowledged in
    PHASE_8_6_PLAN.md §9 as a known debt.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evidencefirst_shared.errors import ErrorCode, NormalizedError
from evidencefirst_shared.schemas import PublishedAnswerLifecycleEventRead

from ..db import get_conn


router = APIRouter(prefix="/api/v1", tags=["lifecycle-events"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _published_answer_exists(
    conn: Connection, published_answer_id: uuid.UUID
) -> bool:
    """Return True iff a published_answers row with the given id exists.

    Read-only: a plain ``SELECT 1 ... LIMIT 1`` is sufficient — this
    endpoint never mutates DB state, so row-level locking would be
    wasteful here.
    """
    row = conn.execute(
        text("SELECT 1 FROM published_answers WHERE id = :pid LIMIT 1"),
        {"pid": published_answer_id},
    ).first()
    return row is not None


def _raise_published_answer_not_found(published_answer_id: uuid.UUID) -> None:
    """Raise the normalized 404 envelope expected by callers.

    Envelope shape mirrors the helper used in routes/answers.py so that
    clients can rely on the same ``details.resource``/``details.id``
    contract across all published_answer-scoped endpoints.
    """
    raise NormalizedError(
        code=ErrorCode.RESOURCE_NOT_FOUND,
        message="published_answers not found",
        details={
            "resource": "published_answers",
            "id": str(published_answer_id),
        },
        http_status=404,
    )


def _row_to_lifecycle_event(row: Any) -> PublishedAnswerLifecycleEventRead:
    """Map a SQLAlchemy row to the shared PublishedAnswerLifecycleEventRead.

    Field coercion notes:
      - UUID columns may surface as ``uuid.UUID`` or as ``str`` depending
        on the driver / pool combination; we normalize via
        ``uuid.UUID(str(...))`` defensively.
      - ``event_payload`` is JSONB: psycopg 3 returns it as a native
        dict, but we coerce a stray ``None`` (defensive — the column is
        NOT NULL DEFAULT '{}'::jsonb so this should not occur) to an
        empty dict to keep the pydantic model happy.
    """
    m = row._mapping
    payload = m["event_payload"]
    if payload is None:
        payload = {}
    requested_by_raw = m["requested_by"]
    requested_by = (
        uuid.UUID(str(requested_by_raw)) if requested_by_raw is not None else None
    )
    return PublishedAnswerLifecycleEventRead(
        id=uuid.UUID(str(m["id"])),
        published_answer_id=uuid.UUID(str(m["published_answer_id"])),
        task_id=uuid.UUID(str(m["task_id"])),
        event_type=str(m["event_type"]),
        event_reason=str(m["event_reason"]),
        event_payload=payload,
        requested_by=requested_by,
        idempotency_key=str(m["idempotency_key"]),
        created_at=m["created_at"],
    )


# ---------------------------------------------------------------------------
# endpoint
# ---------------------------------------------------------------------------
@router.get(
    "/published-answers/{published_answer_id}/lifecycle-events",
)
def list_published_answer_lifecycle_events(
    published_answer_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
    limit: int = Query(default=200, ge=1, le=2000),
    event_type: (
        Literal["published", "withdrawal_requested", "withdrawn", "superseded"]
        | None
    ) = Query(default=None),
) -> dict[str, Any]:
    """List lifecycle events for a published_answer (read-only).

    Behavior:
      - 404 RESOURCE_NOT_FOUND with details.resource="published_answers"
        if the published_answer does not exist.
      - 200 with ``items=[]`` if the published_answer exists but has no
        lifecycle rows (including the legitimate case of published_answers
        created before Phase 8.5; no ``published`` backfill is performed
        here).
      - 200 with the list of matching rows, ordered ASC by
        (created_at, id), filtered by ``event_type`` if provided, and
        truncated to ``limit`` rows.

    The wrapper shape is inline ``{"published_answer_id": <uuid>,
    "items": [PublishedAnswerLifecycleEventRead, ...]}``. We do not bind
    a Pydantic ``response_model`` here because the wrapper is purely a
    response shape (mirroring the pattern in routes/claims.py); the
    items themselves are serialized via the shared
    ``PublishedAnswerLifecycleEventRead`` model.
    """
    if not _published_answer_exists(conn, published_answer_id):
        _raise_published_answer_not_found(published_answer_id)
        # _raise_published_answer_not_found never returns; the explicit
        # ``raise`` keeps static analyzers happy without `# type: ignore`.
        raise AssertionError("unreachable")

    # Build the SELECT with optional event_type filter. We avoid string
    # interpolation entirely: the optional clause is a fixed fragment
    # toggled by Python control flow, and every value is bound.
    params: dict[str, Any] = {
        "pid": published_answer_id,
        "limit": limit,
    }
    if event_type is None:
        sql = text(
            """
            SELECT
              id, published_answer_id, task_id,
              event_type, event_reason, event_payload,
              requested_by, idempotency_key, created_at
            FROM published_answer_lifecycle_events
            WHERE published_answer_id = :pid
            ORDER BY created_at ASC, id ASC
            LIMIT :limit
            """
        )
    else:
        sql = text(
            """
            SELECT
              id, published_answer_id, task_id,
              event_type, event_reason, event_payload,
              requested_by, idempotency_key, created_at
            FROM published_answer_lifecycle_events
            WHERE published_answer_id = :pid
              AND event_type          = :et
            ORDER BY created_at ASC, id ASC
            LIMIT :limit
            """
        )
        params["et"] = event_type

    rows = conn.execute(sql, params).fetchall()
    items = [_row_to_lifecycle_event(r).model_dump(mode="json") for r in rows]

    return {
        "published_answer_id": str(published_answer_id),
        "items": items,
    }
