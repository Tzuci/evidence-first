"""API routes for Phase 8.4 answers (read-only) and Phase 8.5 lifecycle producers.

Read-only endpoints (Phase 8.4):
  GET  /api/v1/tasks/{task_id}/draft
  GET  /api/v1/tasks/{task_id}/final-gate-report
  GET  /api/v1/tasks/{task_id}/published-answer
  GET  /api/v1/published-answers/{published_answer_id}

Lifecycle producer endpoints (Phase 8.5 — Block 4A-1):
  POST /api/v1/published-answers/{published_answer_id}/withdrawal-requests

Invariants honored across this module:
  - Read endpoints are strictly read-only: no INSERT / UPDATE / DELETE.
  - The withdrawal-request producer is also strictly DB-read-only on the
    application schema: it MUST NOT modify ``published_answers.status``,
    MUST NOT call the lifecycle service, and MUST NOT write any row in
    ``published_answer_lifecycle_events``. Its sole side effect is an
    XADD on the dedicated Redis stream. The actual lifecycle transition
    is performed by the worker consumer (see
    ``apps/worker/app/consumers/published_answer_withdrawal.py``).
  - 404 errors are normalized via ``NormalizedError`` +
    ``ErrorCode.RESOURCE_NOT_FOUND``.
  - When a task does not exist, GET endpoints return RESOURCE_NOT_FOUND
    with details.resource = "task_masters", regardless of the
    sub-resource. When the task exists but the sub-resource does not,
    the appropriate sub-resource hint is set in details.resource:
        details.resource = "draft_final_answers"
        details.resource = "final_gate_reports"
        details.resource = "published_answers"
  - In MVP-0 the ErrorCode enum does NOT contain a NOT_PUBLISHED variant;
    we therefore use RESOURCE_NOT_FOUND with
    details.resource="published_answers" for the not-yet-published case.
  - Composite FK constraints in 0005 guarantee that if a
    final_gate_report or published_answer exists for a task, it is
    consistent with the underlying draft. This module does NOT
    duplicate those checks.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
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

from ..config import get_settings
from ..db import get_conn, get_engine
from ..redis import get_redis


logger = structlog.get_logger(__name__)


router = APIRouter()


# ---------------------------------------------------------------------------
# constants — withdrawal producer
# ---------------------------------------------------------------------------
EVENT_TYPE_WITHDRAWAL_REQUESTED = "published_answer.withdrawal_requested"

# Default ``event_reason`` recorded on the lifecycle row when the API
# request body omits one. Kept short and machine-friendly; the
# human-readable reason belongs to the producer (this endpoint), not to
# the worker. Mirrors the convention of
# ``DEFAULT_EVENT_REASON`` in the consumer module, but uses a
# differentiated suffix so logs can tell apart events generated via API
# vs. events generated via internal sources.
DEFAULT_EVENT_REASON_API = "withdrawal_requested_via_api"


# ---------------------------------------------------------------------------
# helpers — read-only endpoints
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
# helpers — withdrawal producer
# ---------------------------------------------------------------------------
def _resolve_published_answer_scope(
    conn: Connection, *, published_answer_id: uuid.UUID
) -> dict[str, uuid.UUID] | None:
    """Resolve (task_id, tenant_id, project_id) for a published_answer.

    Returns None when the published_answer does not exist. Plain SELECT,
    no FOR UPDATE: this endpoint does not perform any DB mutation, so
    row-level locking would be wasteful. The actual lifecycle transition
    happens later in the worker's ``apply_withdrawal``, which acquires
    its own ``SELECT ... FOR UPDATE OF pa`` inside the consumer
    transaction.
    """
    row = conn.execute(
        text(
            """
            SELECT
              pa.task_id    AS task_id,
              tm.tenant_id  AS tenant_id,
              tm.project_id AS project_id
            FROM published_answers pa
            JOIN task_masters       tm ON tm.id = pa.task_id
            WHERE pa.id = :pid
            """
        ),
        {"pid": published_answer_id},
    ).first()
    if row is None:
        return None
    m = row._mapping
    return {
        "task_id": uuid.UUID(str(m["task_id"])),
        "tenant_id": uuid.UUID(str(m["tenant_id"])),
        "project_id": uuid.UUID(str(m["project_id"])),
    }


# ---------------------------------------------------------------------------
# Pydantic models — withdrawal producer
# ---------------------------------------------------------------------------
class WithdrawalRequestCreate(BaseModel):
    """Optional request body for POST .../withdrawal-requests.

    All fields are optional. When the client posts an empty body (or no
    body at all), the endpoint generates a fresh ``idempotency_key`` and
    falls back to a default ``event_reason``.

    - ``reason``: human-readable reason; recorded as ``event_reason`` on
      the lifecycle row.
    - ``idempotency_key``: consumer-level key used by the worker's
      ``event_processing_records`` UNIQUE
      ``(consumer_name, idempotency_key)``. If omitted, the API generates
      a fresh ``uuid4().hex``. Two distinct API requests with no
      idempotency_key are intentionally treated as two distinct
      requests; the worker's lifecycle service will normalize them when
      the row is already in a terminal state.
    - ``requested_by``: optional UUID of the human/user actor; pass-through
      to the lifecycle row (column ``requested_by``). May be omitted in
      MVP-0 since the API does not yet enforce real auth.
    - ``lifecycle_idempotency_key``: optional override for the
      service-level UNIQUE
      ``(published_answer_id, event_type, idempotency_key)`` on
      ``published_answer_lifecycle_events``. Defaults (in the consumer)
      to ``idempotency_key`` when omitted; we forward it only if
      explicitly provided by the client.
    - ``event_payload``: optional opaque dict. Per the Block 4A-1
      contract, Redis stream fields are strings; we serialize this as
      ``event_payload_json`` to avoid ambiguity with the consumer's
      ``event_payload`` dict-typed branch (which is reachable only
      via direct in-process invocation, not via the Redis loop). The
      consumer ignores unknown fields, so this is a no-op for
      ``apply_withdrawal`` while still preserving the payload in the
      Redis entry for forensic / replay purposes.
    """

    reason: str | None = Field(default=None, max_length=2000)
    idempotency_key: str | None = Field(default=None, max_length=200)
    requested_by: uuid.UUID | None = None
    lifecycle_idempotency_key: str | None = Field(default=None, max_length=200)
    event_payload: dict[str, Any] | None = None


class WithdrawalRequestQueued(BaseModel):
    """Response body for an accepted withdrawal request.

    The endpoint always returns 202 Accepted: the actual lifecycle
    transition is asynchronous. Clients can poll the
    GET /api/v1/published-answers/{id} endpoint to observe the
    transition from ``published`` to ``withdrawn``.
    """

    status: str
    event_type: str
    event_id: uuid.UUID
    published_answer_id: uuid.UUID
    stream: str
    idempotency_key: str
    lifecycle_idempotency_key: str


# ---------------------------------------------------------------------------
# read endpoints
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


# ---------------------------------------------------------------------------
# withdrawal producer endpoint (Phase 8.5 — Block 4A-1)
# ---------------------------------------------------------------------------
@router.post(
    "/api/v1/published-answers/{published_answer_id}/withdrawal-requests",
    response_model=WithdrawalRequestQueued,
    status_code=202,
    tags=["answers"],
)
def request_published_answer_withdrawal(
    published_answer_id: uuid.UUID,
    body: WithdrawalRequestCreate | None = None,
) -> WithdrawalRequestQueued:
    """Enqueue a published_answer withdrawal request on the dedicated Redis stream.

    Behavior contract (Phase 8.5 — Block 4A-1):

      - This endpoint NEVER mutates ``published_answers.status``.
      - This endpoint NEVER writes any row in
        ``published_answer_lifecycle_events``.
      - This endpoint NEVER calls the worker's ``apply_withdrawal``
        service; the lifecycle transition is performed asynchronously by
        the worker consumer
        (``apps/worker/app/consumers/published_answer_withdrawal.py``)
        after it picks up the event from
        ``app.events.published_answer_withdrawal_requested``.

    Resolution & validation (DB read-only):
      - Resolves ``(task_id, tenant_id, project_id)`` from
        ``published_answers JOIN task_masters``. If the published_answer
        does not exist, returns 404 RESOURCE_NOT_FOUND with
        ``details.resource="published_answers"`` and does NOT publish
        any Redis event.

    Idempotency keys:
      - ``idempotency_key`` (consumer-level): if the body provides one,
        it is forwarded verbatim. Otherwise the endpoint generates a
        fresh ``uuid4().hex``. Two API calls without an idempotency_key
        are intentionally treated as two distinct requests; the worker
        lifecycle service will normalize them when the row is already
        in a terminal state.
      - ``lifecycle_idempotency_key`` (service-level): if the body
        provides one, it is forwarded as a separate field. Otherwise
        the consumer defaults it to ``idempotency_key`` (we do NOT
        duplicate that fallback here to keep the consumer as the single
        source of truth for the default).

    Event payload:
      - Redis stream fields are strings. ``event_payload``, when
        provided, is JSON-serialized and published under
        ``event_payload_json`` to avoid ambiguity with the consumer's
        dict-typed ``event_payload`` branch (reachable only via direct
        in-process invocation). The consumer ignores unknown stream
        fields; this preserves the payload in the Redis entry for
        forensic / replay purposes without requiring a consumer change.

    Failure modes:
      - 404 if the published_answer cannot be resolved.
      - 500 INTERNAL_ERROR if the Redis ``XADD`` itself fails. Unlike
        the task.created producer (where the task is already committed
        to the DB and the Redis publish merely starts the pipeline),
        here the Redis publish is the ENTIRE side effect of the
        request, so a publish failure means the client's request was
        not accepted at all and must be surfaced.
    """
    body = body or WithdrawalRequestCreate()

    # Resolve scope on a short-lived, read-only connection. We use the
    # engine directly (rather than the get_conn dependency) because the
    # request flow needs the connection to be closed BEFORE the Redis
    # XADD: holding a transaction open across an external network call
    # would only waste a pool slot here, since this endpoint performs
    # no DB writes.
    eng = get_engine()
    with eng.connect() as conn:
        scope = _resolve_published_answer_scope(
            conn, published_answer_id=published_answer_id
        )

    if scope is None:
        # 404 with the same envelope shape used by the GET endpoints in
        # this module. No Redis publish in this branch.
        _raise_not_found("published_answers", str(published_answer_id))
        # _raise_not_found never returns; the explicit `raise` keeps mypy
        # happy without needing a `# type: ignore` comment.
        raise AssertionError("unreachable")

    # Build the event. All field values are normalized to strings, since
    # Redis Streams expects a string -> string mapping. Optional fields
    # that are absent are simply omitted from the dict (the consumer's
    # _coerce_optional_uuid treats both missing keys and empty strings
    # as None, but omitting is more explicit and reduces Redis storage).
    event_id = uuid.uuid4()

    if body.idempotency_key is not None and body.idempotency_key != "":
        idempotency_key = body.idempotency_key
    else:
        idempotency_key = uuid.uuid4().hex

    if (
        body.lifecycle_idempotency_key is not None
        and body.lifecycle_idempotency_key != ""
    ):
        lifecycle_idempotency_key = body.lifecycle_idempotency_key
    else:
        # Mirror the consumer's default (idempotency_key) so the
        # response reflects the value the worker will actually use.
        # We still forward it explicitly on the stream so consumers
        # do not have to re-derive it; the consumer code accepts both
        # explicit and missing forms.
        lifecycle_idempotency_key = idempotency_key

    if body.reason is not None and body.reason != "":
        event_reason = body.reason
    else:
        event_reason = DEFAULT_EVENT_REASON_API

    settings = get_settings()
    stream = settings.EVENTS_PUBLISHED_ANSWER_WITHDRAWAL_STREAM

    fields: dict[str, str] = {
        "event_id": str(event_id),
        "event_type": EVENT_TYPE_WITHDRAWAL_REQUESTED,
        "published_answer_id": str(published_answer_id),
        "idempotency_key": idempotency_key,
        "lifecycle_idempotency_key": lifecycle_idempotency_key,
        "event_reason": event_reason,
        "tenant_id": str(scope["tenant_id"]),
        "task_id": str(scope["task_id"]),
        "project_id": str(scope["project_id"]),
    }

    if body.requested_by is not None:
        fields["requested_by"] = str(body.requested_by)

    if body.event_payload is not None:
        # Serialize as JSON under a distinct key. The consumer reads
        # ``event_payload`` (dict-typed); via Redis it would receive a
        # string and silently fall back to ``{}`` in
        # ``_extract_event_payload``. Using ``event_payload_json``
        # avoids that ambiguity: the consumer ignores it, the field is
        # preserved on the stream entry for replay/forensic use, and we
        # do NOT modify the consumer in this block.
        try:
            fields["event_payload_json"] = json.dumps(
                body.event_payload, separators=(",", ":"), sort_keys=True
            )
        except (TypeError, ValueError) as exc:
            raise NormalizedError(
                ErrorCode.VALIDATION_ERROR,
                "event_payload is not JSON-serializable",
                details={"exception_type": exc.__class__.__name__},
            )

    try:
        r = get_redis()
        r.xadd(
            stream,
            fields,
            maxlen=10000,
            approximate=True,
        )
    except Exception as exc:  # noqa: BLE001 — log and surface to the client
        # Unlike the task.created producer in tasks.py (where the task
        # row is already committed and a publish failure only delays
        # the pipeline), here the publish IS the whole side effect of
        # the request: failing silently would silently drop the
        # client's intent. We log + raise INTERNAL_ERROR.
        logger.exception(
            "published_answer_withdrawal_event_publish_failed",
            published_answer_id=str(published_answer_id),
            stream=stream,
            event_id=str(event_id),
        )
        raise NormalizedError(
            ErrorCode.INTERNAL_ERROR,
            "Failed to publish withdrawal request event",
            details={
                "stream": stream,
                "exception_type": exc.__class__.__name__,
            },
        )

    return WithdrawalRequestQueued(
        status="queued",
        event_type=EVENT_TYPE_WITHDRAWAL_REQUESTED,
        event_id=event_id,
        published_answer_id=published_answer_id,
        stream=stream,
        idempotency_key=idempotency_key,
        lifecycle_idempotency_key=lifecycle_idempotency_key,
    )
