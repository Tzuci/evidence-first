"""Published answer lifecycle service (Phase 8.5 — Block 2A).

This module implements the worker-side application of a withdrawal request to
an existing published_answers row. It is the only authorized writer of the
lifecycle fields on published_answers (status, withdrawn_at) for the
withdrawal path. The historical record of every lifecycle transition lives in
published_answer_lifecycle_events (append-only).

Phase 8.5 invariants honored:
  - task_masters.status is NOT extended. The lifecycle lives on
    published_answers.status only. No 'withdrawn' / 'superseded' /
    'publication_held' status is ever assigned to task_masters.
  - lc_block_delete_if_published is NOT modified. Withdrawn or superseded
    published_answers do NOT block DELETE on logical_claims; only
    status='published' does (consistent decision documented in
    PHASE_8_5_PLAN.md).
  - published_answer_lifecycle_events is append-only via trigger. All inserts
    here use ON CONFLICT DO NOTHING on the UNIQUE constraint
    pale_idempotency_uq (published_answer_id, event_type, idempotency_key).
  - published_answers itself is NOT append-only at the schema level: the
    lifecycle fields are designed to be mutated by this service (the unique
    writer). Mutations are status-guarded: the UPDATE only fires on rows
    currently in status='published', so a redelivery of the same idempotency
    key cannot mutate the row twice nor emit a duplicate audit event.
  - No DB trigger performs lifecycle propagation. The withdrawal pipeline is
    purely application-driven.

Concurrency contract:
  - apply_withdrawal acquires a row-level lock on the target published_answers
    row at the start of the transaction (SELECT ... FOR UPDATE OF pa). This
    serializes concurrent withdrawal attempts on the same row: two parallel
    callers will each open their own transaction, but the second one will
    block on the SELECT until the first one commits, then will read the
    post-transition status='withdrawn' and short-circuit to the
    'already_withdrawn' branch with no further writes.
  - This contract REQUIRES the caller to have opened an explicit transaction
    on the supplied Connection (e.g. via engine.begin()). Calling this
    function on a Connection in autocommit mode would degrade the locking
    semantics and is unsupported.

Idempotency contract:
  - Calling apply_withdrawal twice with the SAME idempotency_key on the same
    published_answer_id MUST be safe:
      * lifecycle events are not duplicated (UNIQUE constraint);
      * the published_answers row is not mutated a second time
        (status-guarded UPDATE);
      * no second audit event 'published_answer.withdrawn' is emitted
        (we only audit when the UPDATE actually changes a row).

Audit:
  - Emits 'published_answer.withdrawn' on chain_scope='task' ONLY when the
    UPDATE successfully transitions status='published' -> status='withdrawn'.
  - Does NOT emit a 'task.withdrawn' audit-only event in this block (deferred
    to a later block).

Scope:
  - This service does NOT consume Redis events. The Redis consumer that
    invokes apply_withdrawal will be added in a separate block.
  - This service does NOT expose an HTTP endpoint. The API endpoint that
    enqueues a withdrawal will be added in a separate block.

Transaction model:
  - The caller passes an active SQLAlchemy Connection inside an explicit
    transaction. This module never calls get_engine(), never opens its own
    connection, and never calls commit() or rollback().
"""
from __future__ import annotations

import datetime
import json
import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evidencefirst_shared.db.audit import audit_append


logger = structlog.get_logger(__name__)


SERVICE_NAME = "mvp0_lifecycle_v1"
SERVICE_VERSION = "0.1.0"

EVENT_TYPE_WITHDRAWAL_REQUESTED = "withdrawal_requested"
EVENT_TYPE_WITHDRAWN = "withdrawn"

AUDIT_EVENT_PUBLISHED_ANSWER_WITHDRAWN = "published_answer.withdrawn"


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------
def _payload_default(o: Any) -> Any:
    """JSON encoder fallback for types commonly found in event payloads.

    Mirrors the convention used by evidencefirst_shared.db.audit so that
    payloads written here remain comparable to audit redacted payloads:
      - uuid.UUID         -> canonical string form;
      - bytes / bytearray -> hex string;
      - datetime.datetime -> ISO8601 in UTC with the 'Z' suffix; naive
                             timestamps are assumed to already be in UTC;
      - datetime.date     -> ISO8601 date string.

    Any other unsupported type raises TypeError, so a malformed payload is
    surfaced loudly rather than silently corrupting the JSONB column.
    """
    if isinstance(o, uuid.UUID):
        return str(o)
    if isinstance(o, (bytes, bytearray)):
        return o.hex()
    if isinstance(o, datetime.datetime):
        if o.tzinfo is None:
            o = o.replace(tzinfo=datetime.timezone.utc)
        return o.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(o, datetime.date):
        return o.isoformat()
    raise TypeError(
        f"Object of type {type(o).__name__} is not JSON serializable "
        f"in published_answer_lifecycle event payloads"
    )


def _serialize_payload(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        default=_payload_default,
    )


# ---------------------------------------------------------------------------
# read helpers
# ---------------------------------------------------------------------------
def _select_published_answer_with_task_scope(
    conn: Connection, *, published_answer_id: uuid.UUID
) -> dict[str, Any] | None:
    """Return (id, task_id, status, withdrawn_at, superseded_at, superseded_by_id,
    tenant_id, project_id) joined with task_masters, or None if not found.

    Acquires a row-level lock on the published_answers row only (FOR UPDATE
    OF pa) to serialize concurrent withdrawal attempts on the same row.
    task_masters is left unlocked because nothing on that table is mutated
    by this service.
    """
    row = conn.execute(
        text(
            """
            SELECT
              pa.id                AS id,
              pa.task_id           AS task_id,
              pa.status            AS status,
              pa.withdrawn_at      AS withdrawn_at,
              pa.superseded_at     AS superseded_at,
              pa.superseded_by_id  AS superseded_by_id,
              tm.tenant_id         AS tenant_id,
              tm.project_id        AS project_id
            FROM published_answers pa
            JOIN task_masters       tm ON tm.id = pa.task_id
            WHERE pa.id = :pid
            FOR UPDATE OF pa
            """
        ),
        {"pid": published_answer_id},
    ).first()
    if row is None:
        return None
    return dict(row._mapping)


# ---------------------------------------------------------------------------
# write helpers
# ---------------------------------------------------------------------------
def _insert_lifecycle_event(
    conn: Connection,
    *,
    published_answer_id: uuid.UUID,
    task_id: uuid.UUID,
    event_type: str,
    event_reason: str,
    event_payload: dict[str, Any],
    requested_by: uuid.UUID | None,
    idempotency_key: str,
) -> bool:
    """Idempotent insert into published_answer_lifecycle_events.

    Returns True if a new row was inserted, False if the row already existed
    (conflict on pale_idempotency_uq).
    """
    candidate_id = uuid.uuid4()
    inserted = conn.execute(
        text(
            """
            INSERT INTO published_answer_lifecycle_events (
                id, published_answer_id, task_id,
                event_type, event_reason, event_payload,
                requested_by, idempotency_key
            ) VALUES (
                :id, :pid, :tid,
                :etype, :ereason, CAST(:epayload AS JSONB),
                :rby, :ikey
            )
            ON CONFLICT (published_answer_id, event_type, idempotency_key) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": candidate_id,
            "pid": published_answer_id,
            "tid": task_id,
            "etype": event_type,
            "ereason": event_reason,
            "epayload": _serialize_payload(event_payload),
            "rby": requested_by,
            "ikey": idempotency_key,
        },
    ).first()
    return inserted is not None


def _update_published_answer_to_withdrawn(
    conn: Connection, *, published_answer_id: uuid.UUID
) -> bool:
    """Status-guarded UPDATE: only flips status from 'published' to 'withdrawn'.

    Returns True if exactly one row was updated, False otherwise (e.g. the row
    is already withdrawn or superseded — a redelivery case). The COALESCE on
    withdrawn_at ensures the timestamp is set on the first transition only;
    subsequent attempts are blocked by the WHERE clause anyway.
    """
    row = conn.execute(
        text(
            """
            UPDATE published_answers
            SET status = 'withdrawn',
                withdrawn_at = COALESCE(withdrawn_at, NOW())
            WHERE id = :pid
              AND status = 'published'
            RETURNING id
            """
        ),
        {"pid": published_answer_id},
    ).first()
    return row is not None


# ---------------------------------------------------------------------------
# public entrypoint
# ---------------------------------------------------------------------------
def apply_withdrawal(
    conn: Connection,
    *,
    published_answer_id: uuid.UUID,
    event_reason: str,
    idempotency_key: str,
    requested_by: uuid.UUID | None = None,
    event_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a withdrawal request to an existing published_answers row.

    Outcomes (encoded in the returned dict's ``status`` field):
      - "not_found":          the published_answers row does not exist.
      - "withdrawn":          the row was successfully transitioned from
                              'published' to 'withdrawn' on this call.
      - "already_withdrawn":  the row was already in status='withdrawn' (no-op,
                              no new lifecycle event, no audit).
      - "already_superseded": the row was in status='superseded' (no-op,
                              no new lifecycle event, no audit).

    Behavior on already_withdrawn / already_superseded:
      The service does NOT insert a new 'withdrawal_requested' or 'withdrawn'
      lifecycle event in these cases. Rationale: a request received after the
      published_answer is no longer 'published' is logically a no-op, and we
      do not want to invent a withdrawal history for an answer whose state is
      not driven by the current idempotency_key. This is the conservative
      default documented in the Phase 8.5 plan.

    Concurrency:
      The function acquires a row-level lock on the target published_answers
      row at the start of the transaction. Two concurrent invocations on the
      same row are serialized: the second one blocks on the SELECT, then
      observes the post-transition status and returns 'already_withdrawn'.
      This requires the caller to have opened an explicit transaction (e.g.
      via engine.begin()).

    Idempotency:
      Calling this function twice with the same (published_answer_id,
      idempotency_key) is safe. The lifecycle events are not duplicated
      (UNIQUE constraint pale_idempotency_uq), the published_answers row is
      not mutated twice (status-guarded UPDATE), and the audit event
      'published_answer.withdrawn' is emitted at most once (only when the
      UPDATE actually changes a row).
    """
    payload = dict(event_payload or {})

    pa = _select_published_answer_with_task_scope(
        conn, published_answer_id=published_answer_id
    )
    if pa is None:
        return {
            "status": "not_found",
            "published_answer_id": str(published_answer_id),
        }

    task_id = uuid.UUID(str(pa["task_id"]))
    tenant_id = uuid.UUID(str(pa["tenant_id"]))
    project_id = uuid.UUID(str(pa["project_id"]))
    current_status = str(pa["status"])

    # Branch A: already in a terminal lifecycle state.
    if current_status == "withdrawn":
        return {
            "status": "already_withdrawn",
            "published_answer_id": str(published_answer_id),
            "task_id": str(task_id),
        }
    if current_status == "superseded":
        return {
            "status": "already_superseded",
            "published_answer_id": str(published_answer_id),
            "task_id": str(task_id),
        }

    # Branch B: status='published'. We proceed with the withdrawal pipeline.
    if current_status != "published":
        # Defensive: published_answers.status CHECK already restricts the value
        # set, so reaching this branch would mean a future schema extension.
        # Treat as no-op to keep the service forward-compatible.
        logger.warning(
            "published_answer_lifecycle.unexpected_status",
            published_answer_id=str(published_answer_id),
            status=current_status,
        )
        return {
            "status": "unsupported_status",
            "published_answer_id": str(published_answer_id),
            "task_id": str(task_id),
            "current_status": current_status,
        }

    # 1) Append-only insert of the 'withdrawal_requested' event (idempotent).
    _insert_lifecycle_event(
        conn,
        published_answer_id=published_answer_id,
        task_id=task_id,
        event_type=EVENT_TYPE_WITHDRAWAL_REQUESTED,
        event_reason=event_reason,
        event_payload=payload,
        requested_by=requested_by,
        idempotency_key=idempotency_key,
    )

    # 2) Append-only insert of the 'withdrawn' event (idempotent).
    _insert_lifecycle_event(
        conn,
        published_answer_id=published_answer_id,
        task_id=task_id,
        event_type=EVENT_TYPE_WITHDRAWN,
        event_reason=event_reason,
        event_payload=payload,
        requested_by=requested_by,
        idempotency_key=idempotency_key,
    )

    # 3) Status-guarded UPDATE: only fires on the first effective transition.
    transitioned = _update_published_answer_to_withdrawn(
        conn, published_answer_id=published_answer_id
    )

    # 4) Audit event: emitted ONLY when the UPDATE actually changed a row.
    #    This is what makes the audit emission idempotent under redelivery:
    #    on a second call with the same idempotency_key the UPDATE no-ops
    #    (status is already 'withdrawn'), so we do not append a duplicate
    #    audit event.
    if transitioned:
        actor_type = "user" if requested_by is not None else "system"
        actor_id = str(requested_by) if requested_by is not None else "system"
        audit_append(
            conn,
            chain_scope="task",
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            session_id=None,
            event_type=AUDIT_EVENT_PUBLISHED_ANSWER_WITHDRAWN,
            actor_type=actor_type,
            actor_id=actor_id,
            redacted_payload={
                "service_name": SERVICE_NAME,
                "service_version": SERVICE_VERSION,
                "published_answer_id": str(published_answer_id),
                "previous_status": "published",
                "new_status": "withdrawn",
                "lifecycle_idempotency_key": idempotency_key,
                "event_reason": event_reason,
            },
            related_entity_type="published_answers",
            related_entity_id=published_answer_id,
        )

    return {
        "status": "withdrawn",
        "published_answer_id": str(published_answer_id),
        "task_id": str(task_id),
        "transitioned": bool(transitioned),
    }
