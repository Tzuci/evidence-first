"""API route for Phase 8.6D — task-level source-loss events listing
(read-only).

Endpoint exposed:
  GET /api/v1/tasks/{task_id}/source-loss-events

Strict invariants (Phase 8.6D — read-only observability):

  - This endpoint is COMPLETELY read-only. It MUST NOT:
      * INSERT / UPDATE / DELETE any row in any table;
      * call ``propagate_source_loss`` or any other worker service;
      * import worker code;
      * use Redis;
      * mutate ``source_loss_events``,
        ``source_loss_propagation_records``,
        ``claim_ledger_entries``, ``claim_lineage``,
        ``audit_records``, ``published_answers``, or
        ``published_answer_lifecycle_events`` in any way.

  - The endpoint first checks that the given ``task_id`` exists in
    ``task_masters``. If not, it returns 404 ``RESOURCE_NOT_FOUND``
    with ``details.resource = "task_masters"`` and
    ``details.id = <task_id>``.

  - If the task exists, the endpoint returns the union of two sets of
    ``source_loss_events`` rows:

      S1 — task_scope:
        source_loss_events.task_id = :task_id

      S2 — claim_evidence_link:
        source_loss_events.evidence_span_id
            = claim_evidence_links.evidence_span_id
        AND claim_evidence_links.claim_logical_id = logical_claims.id
        AND logical_claims.task_id = :task_id

    The result is S1 ∪ S2, distinct per source_loss_events.id. When the
    same source_loss_event row satisfies BOTH sets, ``impacted_via``
    MUST be ``"task_scope"`` (priority 0 < priority 1).

  - The response is ordered ASC by (created_at, id) for
    replay-friendliness, and truncated to ``limit`` rows.

  - Why S1 ∪ S2: Phase 8.5 leaves
    ``source_loss_events.task_id = NULL`` by design — an evidence_span
    can be referenced by claims belonging to multiple tasks via
    ``claim_evidence_links → logical_claims.task_id``. The producer
    cannot resolve a single task scope at write time. The task-centric
    view materializes that join here, at read time, without rewriting
    the source rows.

  - The ``source_loss_event.task_id`` field on the response carries
    the raw DB value, not a synthetic value derived from S2. If a row
    surfaces via S2 and the DB has ``task_id=NULL``, the response
    MUST show ``task_id: null``. This is required by the block prompt:
    we never camouflage NULL ``task_id`` on the SLE itself.

  - All JSONB columns (``event_payload``) are returned verbatim. MVP-0
    does not yet apply RBAC redaction; this is acknowledged in
    PHASE_8_6_PLAN.md §9 as a known debt.

  - The Source Quality Evaluator is out of scope for Phase 8.6 (see
    PHASE_8_6_PLAN.md strategic note). This endpoint surfaces which
    source-loss events are visible from a task; it does NOT evaluate
    whether the lost sources were authoritative, primary, independent
    or fresh.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evidencefirst_shared.errors import ErrorCode, NormalizedError
from evidencefirst_shared.schemas import SourceLossEventRead

from ..db import get_conn


router = APIRouter(prefix="/api/v1", tags=["source_loss"])


# ---------------------------------------------------------------------------
# Response models (locally defined — impacted_via is response-shape semantics
# specific to this endpoint, not a domain concept worth promoting to the
# shared schemas package).
# ---------------------------------------------------------------------------
ImpactedVia = Literal["task_scope", "claim_evidence_link"]


class TaskSourceLossEventItem(BaseModel):
    """Single item of the task-level source-loss listing.

    ``impacted_via`` indicates HOW the source_loss_event is visible
    from the queried task:
      - ``"task_scope"``: the SLE row itself carries
        ``task_id = :task_id`` (set S1);
      - ``"claim_evidence_link"``: the SLE is linked to a
        ``logical_claims`` row belonging to the task via
        ``claim_evidence_links`` (set S2).

    If a row satisfies both S1 and S2, ``impacted_via`` is
    ``"task_scope"`` (deduplication precedence).
    """

    source_loss_event: SourceLossEventRead
    impacted_via: ImpactedVia


class TaskSourceLossEventsResponse(BaseModel):
    """Wrapper for the task-level source-loss listing."""

    task_id: uuid.UUID
    items: list[TaskSourceLossEventItem]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _task_exists(conn: Connection, task_id: uuid.UUID) -> bool:
    """Return True iff a task_masters row with the given id exists.

    Read-only: a plain ``SELECT 1 ... LIMIT 1`` is sufficient — this
    endpoint never mutates DB state, so row-level locking would be
    wasteful here. Mirrors the convention adopted by
    ``lifecycle_events.py::_published_answer_exists`` and
    ``source_loss.py::_source_loss_event_exists``.
    """
    row = conn.execute(
        text("SELECT 1 FROM task_masters WHERE id = :tid LIMIT 1"),
        {"tid": task_id},
    ).first()
    return row is not None


def _raise_task_not_found(task_id: uuid.UUID) -> None:
    """Raise the normalized 404 envelope expected by callers.

    Envelope shape mirrors the helpers in routes/lifecycle_events.py
    and routes/source_loss.py so clients can rely on the same
    ``details.resource``/``details.id`` contract across all 8.6
    endpoints.
    """
    raise NormalizedError(
        code=ErrorCode.RESOURCE_NOT_FOUND,
        message="task_masters not found",
        details={
            "resource": "task_masters",
            "id": str(task_id),
        },
        http_status=404,
    )


def _row_to_source_loss_event_read(row: Any) -> SourceLossEventRead:
    """Map a SQLAlchemy row to the shared SourceLossEventRead.

    Field coercion notes:
      - UUID columns may surface as ``uuid.UUID`` or ``str`` depending
        on the driver / pool combination; we normalize via
        ``uuid.UUID(str(...))`` defensively, mirroring the convention
        adopted in ``source_loss.py``.
      - ``event_payload`` is JSONB: psycopg 3 returns it as a native
        dict, but we coerce a stray string (some driver/pool
        combinations) via ``json.loads`` and a stray ``None``
        (defensive — the column is NOT NULL DEFAULT '{}'::jsonb so
        this should not occur in practice) to an empty dict.
      - ``task_id`` MUST surface as ``None`` when the DB row has NULL,
        regardless of which set (S1 or S2) produced the row. See the
        module docstring for the rationale.
    """
    m = row._mapping

    raw_payload = m["event_payload"]
    if raw_payload is None:
        event_payload: dict[str, Any] = {}
    elif isinstance(raw_payload, str):
        event_payload = json.loads(raw_payload)
    else:
        event_payload = dict(raw_payload)

    def _opt_uuid(value: Any) -> uuid.UUID | None:
        return uuid.UUID(str(value)) if value is not None else None

    return SourceLossEventRead(
        id=uuid.UUID(str(m["id"])),
        tenant_id=uuid.UUID(str(m["tenant_id"])),
        project_id=_opt_uuid(m["project_id"]),
        task_id=_opt_uuid(m["task_id"]),
        evidence_span_id=uuid.UUID(str(m["evidence_span_id"])),
        document_chunk_id=_opt_uuid(m["document_chunk_id"]),
        document_version_id=_opt_uuid(m["document_version_id"]),
        document_id=_opt_uuid(m["document_id"]),
        loss_kind=str(m["loss_kind"]),
        loss_reason=str(m["loss_reason"]),
        detected_by=str(m["detected_by"]),
        event_payload=event_payload,
        idempotency_key=str(m["idempotency_key"]),
        created_at=m["created_at"],
    )


# ---------------------------------------------------------------------------
# endpoint
# ---------------------------------------------------------------------------
@router.get("/tasks/{task_id}/source-loss-events")
def list_task_source_loss_events(
    task_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    """List source_loss_events visible from a task (read-only).

    Behavior:
      - 404 RESOURCE_NOT_FOUND with details.resource="task_masters"
        if the task does not exist. The check is performed BEFORE the
        union query, so a client probing for a bogus id receives an
        immediate not-found rather than a misleading empty list.
      - 200 with ``items=[]`` if the task exists but no source_loss
        events are visible (neither via S1 nor via S2).
      - 200 with the list of matching items, ordered ASC by
        (created_at, id), and truncated to ``limit`` rows.

    Wrapper shape::

        {
          "task_id": "<uuid>",
          "items": [
            {
              "source_loss_event": SourceLossEventRead,
              "impacted_via": "task_scope" | "claim_evidence_link"
            },
            ...
          ]
        }

    Query design:
      - Two CTEs compute S1 and S2 with a ``priority`` column
        (0 for task_scope, 1 for claim_evidence_link).
      - UNION ALL preserves duplicates so the precedence step can pick
        the right row.
      - The inner SELECT uses ``DISTINCT ON (id)`` ordered by
        ``id, priority ASC`` to collapse duplicates and retain the
        task_scope variant when both apply.
      - The outer SELECT re-orders by ``(created_at, id)`` ASC for
        replay-friendliness, and ``LIMIT :limit`` is applied to the
        final ordered set.
      - All values are bound parameters; no string interpolation.

    Strict scope reminder:
      - This endpoint is read-only end-to-end (see module docstring).
      - The Source Quality Evaluator is out of scope: this endpoint
        surfaces which source-loss events are visible from a task; it
        does NOT evaluate the lost sources' authority, primaryness,
        independence or freshness.
    """
    if not _task_exists(conn, task_id):
        _raise_task_not_found(task_id)
        # _raise_task_not_found never returns; the explicit ``raise``
        # keeps static analyzers happy without ``# type: ignore``.
        raise AssertionError("unreachable")

    # Single SQL with two CTEs and a DISTINCT-ON dedup pass. Every
    # value is bound; no f-string SQL, no concatenation.
    #
    # Note on the ORDER BY in the inner ranked subquery: we need
    # ``DISTINCT ON (sle_id)`` to collapse rows by source_loss_event id
    # while keeping the lowest priority (task_scope wins). PostgreSQL
    # requires the DISTINCT ON expression to be the leftmost in the
    # ORDER BY. We then re-sort by (created_at, id) in the outer SELECT.
    sql = text(
        """
        WITH task_scope AS (
            SELECT
              sle.id                  AS sle_id,
              sle.tenant_id,
              sle.project_id,
              sle.task_id,
              sle.evidence_span_id,
              sle.document_chunk_id,
              sle.document_version_id,
              sle.document_id,
              sle.loss_kind,
              sle.loss_reason,
              sle.detected_by,
              sle.event_payload,
              sle.idempotency_key,
              sle.created_at,
              0::INTEGER              AS priority,
              'task_scope'::TEXT      AS impacted_via
            FROM source_loss_events sle
            WHERE sle.task_id = :tid
        ),
        claim_scope AS (
            SELECT
              sle.id                  AS sle_id,
              sle.tenant_id,
              sle.project_id,
              sle.task_id,
              sle.evidence_span_id,
              sle.document_chunk_id,
              sle.document_version_id,
              sle.document_id,
              sle.loss_kind,
              sle.loss_reason,
              sle.detected_by,
              sle.event_payload,
              sle.idempotency_key,
              sle.created_at,
              1::INTEGER              AS priority,
              'claim_evidence_link'::TEXT AS impacted_via
            FROM source_loss_events sle
            JOIN claim_evidence_links cel
              ON cel.evidence_span_id = sle.evidence_span_id
            JOIN logical_claims lc
              ON lc.id = cel.claim_logical_id
            WHERE lc.task_id = :tid
        ),
        unioned AS (
            SELECT * FROM task_scope
            UNION ALL
            SELECT * FROM claim_scope
        ),
        deduped AS (
            SELECT DISTINCT ON (sle_id)
              sle_id,
              tenant_id,
              project_id,
              task_id,
              evidence_span_id,
              document_chunk_id,
              document_version_id,
              document_id,
              loss_kind,
              loss_reason,
              detected_by,
              event_payload,
              idempotency_key,
              created_at,
              priority,
              impacted_via
            FROM unioned
            ORDER BY sle_id, priority ASC
        )
        SELECT
          sle_id           AS id,
          tenant_id,
          project_id,
          task_id,
          evidence_span_id,
          document_chunk_id,
          document_version_id,
          document_id,
          loss_kind,
          loss_reason,
          detected_by,
          event_payload,
          idempotency_key,
          created_at,
          impacted_via
        FROM deduped
        ORDER BY created_at ASC, sle_id ASC
        LIMIT :limit
        """
    )

    rows = conn.execute(sql, {"tid": task_id, "limit": limit}).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        sle = _row_to_source_loss_event_read(row)
        impacted_via = str(row._mapping["impacted_via"])
        item = TaskSourceLossEventItem(
            source_loss_event=sle,
            impacted_via=impacted_via,  # type: ignore[arg-type]
        )
        items.append(item.model_dump(mode="json"))

    return {
        "task_id": str(task_id),
        "items": items,
    }
