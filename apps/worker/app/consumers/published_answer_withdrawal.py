"""Published answer withdrawal consumer (Phase 8.5 — Block 3A-1).

This module exposes a single entry point, ``handle_published_answer_withdrawal``,
that processes a ``published_answer.withdrawal_requested`` event by delegating
the actual lifecycle transition to
``apps.worker.app.services.published_answer_lifecycle.apply_withdrawal``.

Scope of this block:
  - Handler-only. The handler is invoked directly with a decoded event dict;
    it does NOT read from Redis and is NOT registered in
    ``apps/worker/app/main.py``. Wiring into ``main.py`` is deferred to a
    later block.
  - No HTTP endpoint. The API surface that produces these events is also
    deferred to a later block.
  - No new migration. Schema is fixed at 0006_lifecycle.sql.
  - No source-loss consumer. That belongs to a separate block.
  - No mutations to ``published_answers.status`` performed in this module:
    the ONLY authorized writer of the lifecycle fields stays
    ``apply_withdrawal``. This module merely orchestrates idempotent
    consumer-level bookkeeping around the service call.

Invariants honored (Phase 8.5):
  - ``task_masters.status`` is NOT touched in this code path.
  - ``published_answers.status`` is mutated ONLY through ``apply_withdrawal``.
  - No DB trigger performs propagation; all bookkeeping is application-driven.
  - No new ``ErrorCode`` is introduced.
  - No new dependency is introduced.

Idempotency model:
  Two complementary idempotency mechanisms apply on every delivery:

  1. **Consumer-level** via ``event_processing_records``:
     - UNIQUE ``(consumer_name, idempotency_key)`` from migration 0001.
     - ``begin_processing`` returns ``"started"`` on the first attempt and
       ``"succeeded"`` on a redelivery whose previous run completed
       successfully. The handler treats ``"succeeded"`` as a hard
       short-circuit (``"skipped_already_succeeded"``), aligned with
       ``apps/worker/app/consumers/task_created.py``.
     - On a previous attempt that failed (status ``"failed"`` or
       ``"started"``), the handler proceeds and lets the underlying
       service-level idempotency take over.

  2. **Service-level** via ``apply_withdrawal``:
     - ``published_answer_lifecycle_events`` UNIQUE
       ``(published_answer_id, event_type, idempotency_key)`` (constraint
       ``pale_idempotency_uq`` from 0006) prevents duplicate lifecycle
       events even if the consumer-level guard is somehow bypassed.
     - ``published_answers.status`` UPDATE is status-guarded
       (``WHERE status = 'published'``), so a redelivery cannot mutate the
       row twice nor emit a duplicate audit event.

  The two layers are intentionally redundant: either one alone would
  prevent duplicates, but together they make the handler safe against
  partial failures, redelivery storms, and concurrent invocations.

FK-safety:
  ``event_processing_records.task_id`` has ``ON DELETE RESTRICT`` to
  ``task_masters(id)``, so calling ``begin_processing`` with a non-null
  ``task_id`` requires the row to be visible. Because the handler resolves
  ``task_id`` from ``published_answers.task_id`` only AFTER opening the
  transaction (and via a ``SELECT`` that returns nothing when the row does
  not exist), we reuse the same FK-safe pattern adopted by
  ``task_created.py``:

    - If ``published_answers`` (and therefore ``task_id``) cannot be
      resolved up front, ``begin_processing`` is called with
      ``task_id=None`` and ``project_id=None``; the resulting EPR is then
      marked as ``failed`` (``WORKER_PUBLISHED_ANSWER_NOT_VISIBLE``) and
      the handler returns ``"failed"``. A subsequent redelivery can
      resume cleanly once the row becomes visible.

    - If the resolution succeeds, ``begin_processing`` is called with the
      resolved ``task_id`` and ``project_id`` so the EPR carries the full
      task scope, and the service is invoked. The terminal classification
      of the EPR (``succeeded`` vs ``failed``) is driven by the service
      outcome.

Status strings returned to the caller:
  - ``"processed"``                 — service ran, EPR marked ``succeeded``.
  - ``"skipped_already_succeeded"`` — EPR was already in ``succeeded`` state
                                      from a previous run; no service call
                                      was made.
  - ``"failed"``                    — malformed event, missing references,
                                      or unhandled exception during the
                                      service call. EPR marked ``failed``
                                      (when an EPR row could be created;
                                      pre-transaction validation failures
                                      do not write any row).

Transaction model:
  The handler opens its own SQLAlchemy transaction via the worker's
  ``transaction()`` context manager (the same primitive used by
  ``task_created.py``). It does NOT receive a Connection from the caller.
  The service ``apply_withdrawal`` does NOT call ``commit()`` / ``rollback()``
  itself; the transaction context manager flushes the writes on a clean
  return and rolls back on any uncaught exception.

Event shape (consumer contract):
  The handler accepts a decoded ``dict[str, Any]`` (or a string-keyed dict
  like the one produced by the Redis Streams decoder in ``main.py``) with
  the following fields::

      event_id                 (str / UUID, REQUIRED)
      event_type               (str, must be 'published_answer.withdrawal_requested')
      published_answer_id      (str / UUID, REQUIRED)
      idempotency_key          (str, REQUIRED — consumer-level key for EPR)
      event_reason             (str, OPTIONAL — defaults to a stable
                                fallback if not provided)
      requested_by             (str / UUID, OPTIONAL — pass-through to the
                                lifecycle service; NULL when the request is
                                machine-driven. If present BUT malformed
                                as a UUID, the event is rejected as
                                malformed: the handler returns ``"failed"``
                                without opening a transaction and without
                                writing any event_processing_records row.
                                A missing or empty value is treated as
                                None and is NOT a failure.)
      lifecycle_idempotency_key (str, OPTIONAL — service-level key for the
                                 lifecycle UNIQUE; defaults to
                                 ``idempotency_key`` if absent so that the
                                 consumer-level and service-level keys
                                 collapse into a single value when the
                                 producer does not distinguish them)
      tenant_id                (str / UUID, OPTIONAL — used only for the
                                EPR row when ``published_answers`` is not
                                resolvable; if absent, the handler returns
                                ``"failed"`` because the EPR row requires
                                a non-null ``tenant_id``)
      event_payload            (dict, OPTIONAL — opaque pass-through to the
                                lifecycle service)

  The event MAY carry additional fields; the handler ignores them.

  Note on ``tenant_id``: ``event_processing_records.tenant_id`` is
  ``NOT NULL``. We resolve it from ``published_answers``/``task_masters``
  whenever possible. The optional ``tenant_id`` event field exists so the
  handler can still create an EPR row in the rare case where the
  ``published_answer_id`` does not resolve at all (e.g. it has been
  deleted between API enqueue and worker pickup); without the field, no
  EPR can be persisted and the handler returns ``"failed"`` directly.
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evidencefirst_shared.db.idempotency import (
    begin_processing,
    mark_failed,
    mark_succeeded,
)

from ..db import transaction
from ..services.published_answer_lifecycle import apply_withdrawal


logger = structlog.get_logger(__name__)


# Stable identity of this consumer. Used by ``begin_processing`` to scope the
# UNIQUE (consumer_name, idempotency_key) constraint on event_processing_records.
# Matches the naming convention used by ``task_created.py`` (consumer_name is
# free-form text, no enum at DB level).
CONSUMER_NAME_DEFAULT = "published_answer_withdrawal"

EVENT_TYPE_WITHDRAWAL_REQUESTED = "published_answer.withdrawal_requested"

# Default reason recorded on the lifecycle events when the producer omits one.
# Kept short and machine-friendly; the human-readable reason belongs to the
# producer (API endpoint), not to the worker.
DEFAULT_EVENT_REASON = "withdrawal_requested_via_event"

# Error codes used on event_processing_records.error_code. Naming mirrors the
# WORKER_* prefix convention adopted by task_created.py so dashboards can
# group worker-level errors uniformly.
ERR_MALFORMED_EVENT = "WORKER_MALFORMED_EVENT"
ERR_PUBLISHED_ANSWER_NOT_VISIBLE = "WORKER_PUBLISHED_ANSWER_NOT_VISIBLE"
ERR_HANDLER_FAIL = "WORKER_PUBLISHED_ANSWER_WITHDRAWAL_FAIL"


# ---------------------------------------------------------------------------
# event parsing
# ---------------------------------------------------------------------------
def _coerce_uuid(value: Any) -> uuid.UUID:
    """Convert an event field into a uuid.UUID.

    Accepts both ``uuid.UUID`` and ``str`` so the handler can be invoked
    directly with a Python dict in tests and with a Redis-decoded
    ``dict[str, str]`` in production without any wrapper layer.
    """
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _coerce_optional_uuid(value: Any) -> uuid.UUID | None:
    """Convert an optional event field into a uuid.UUID, or None.

    An empty string is treated as None: Redis decoders sometimes serialize
    a missing UUID as ``""`` rather than dropping the key. A non-empty
    value that cannot be parsed as a UUID raises ``ValueError``: callers
    decide how to react (the lifecycle consumer treats a malformed
    ``requested_by`` as a hard malformed-event failure).
    """
    if value is None:
        return None
    if isinstance(value, str) and value == "":
        return None
    return _coerce_uuid(value)


def _extract_event_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Return the opaque ``event_payload`` dict passed to the service.

    Defaults to an empty dict and tolerates a missing or malformed value
    (e.g. a string in a Redis-decoded event): in that case we discard the
    malformed payload silently rather than rejecting the event, since the
    payload is purely descriptive metadata for the lifecycle row.
    """
    raw = event.get("event_payload")
    if isinstance(raw, dict):
        return dict(raw)
    return {}


# ---------------------------------------------------------------------------
# DB read helpers
# ---------------------------------------------------------------------------
def _resolve_published_answer_scope(
    conn: Connection, *, published_answer_id: uuid.UUID
) -> dict[str, uuid.UUID] | None:
    """Resolve (tenant_id, project_id, task_id) for a published_answer.

    Returns None when the published_answer does not exist. The lookup is a
    plain SELECT (no FOR UPDATE): row-level locking is the responsibility of
    ``apply_withdrawal``, which acquires its own SELECT ... FOR UPDATE OF pa
    inside the same transaction. Doing it twice would not be incorrect but
    would generate redundant lock acquisition.
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
    return {
        "task_id": uuid.UUID(str(row._mapping["task_id"])),
        "tenant_id": uuid.UUID(str(row._mapping["tenant_id"])),
        "project_id": uuid.UUID(str(row._mapping["project_id"])),
    }


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def handle_published_answer_withdrawal(
    event: dict[str, Any],
    *,
    consumer_name: str = CONSUMER_NAME_DEFAULT,
) -> str:
    """Process a single ``published_answer.withdrawal_requested`` event.

    See module docstring for the full event contract and idempotency model.

    Returns one of:
      - ``"processed"``
      - ``"skipped_already_succeeded"``
      - ``"failed"``
    """
    # 1) Validate the minimal event shape. We do NOT yet open a transaction
    #    here: malformed events should not even touch the DB.
    try:
        event_id = _coerce_uuid(event["event_id"])
        event_type = str(event["event_type"])
        published_answer_id = _coerce_uuid(event["published_answer_id"])
        idempotency_key = str(event["idempotency_key"])
    except (KeyError, ValueError, TypeError) as exc:
        logger.error(
            "published_answer_withdrawal.malformed_event",
            error=str(exc),
            raw_event=event,
        )
        return "failed"

    if event_type != EVENT_TYPE_WITHDRAWAL_REQUESTED:
        logger.error(
            "published_answer_withdrawal.unexpected_event_type",
            event_id=str(event_id),
            event_type=event_type,
            expected=EVENT_TYPE_WITHDRAWAL_REQUESTED,
        )
        return "failed"

    # The lifecycle key may diverge from the consumer key when the producer
    # wants to keep the two layers independent; defaulting to the consumer
    # key keeps simple producers simple.
    raw_lifecycle_key = event.get("lifecycle_idempotency_key")
    if raw_lifecycle_key is None or raw_lifecycle_key == "":
        lifecycle_idempotency_key = idempotency_key
    else:
        lifecycle_idempotency_key = str(raw_lifecycle_key)

    raw_event_reason = event.get("event_reason")
    event_reason = (
        str(raw_event_reason)
        if raw_event_reason is not None and str(raw_event_reason) != ""
        else DEFAULT_EVENT_REASON
    )

    # ``requested_by`` is OPTIONAL but, when present, MUST be a syntactically
    # valid UUID. A missing or empty value is treated as None (system actor).
    # A present-but-malformed value is treated as a malformed event: the
    # handler returns "failed" WITHOUT opening a transaction and WITHOUT
    # writing any event_processing_records row. This mirrors the strict
    # validation policy applied to the other required UUID fields above and
    # avoids creating an EPR slot tied to an event that the producer would
    # have to re-emit anyway after correction.
    try:
        requested_by = _coerce_optional_uuid(event.get("requested_by"))
    except ValueError as exc:
        logger.error(
            "published_answer_withdrawal.bad_requested_by",
            error=str(exc),
            event_id=str(event_id),
            requested_by=event.get("requested_by"),
        )
        return "failed"

    event_payload = _extract_event_payload(event)

    # Optional tenant_id from the event, used only as a fallback when the
    # published_answer cannot be resolved (so we still get an EPR row).
    try:
        event_tenant_id = _coerce_optional_uuid(event.get("tenant_id"))
    except ValueError:
        event_tenant_id = None

    # 2) Open a transaction and run the consumer-level + service-level pipeline.
    with transaction() as conn:
        scope = _resolve_published_answer_scope(
            conn, published_answer_id=published_answer_id
        )

        if scope is None:
            # FK-safe branch: cannot create an EPR row pointing to a non-existent
            # task_id (FK is RESTRICT). We still want to record the failure if
            # we can — but only if the producer gave us a tenant_id, since
            # event_processing_records.tenant_id is NOT NULL.
            if event_tenant_id is None:
                logger.error(
                    "published_answer_withdrawal.published_answer_not_visible_no_tenant",
                    event_id=str(event_id),
                    published_answer_id=str(published_answer_id),
                )
                return "failed"

            record_id, status = begin_processing(
                conn,
                event_id=event_id,
                event_type=event_type,
                consumer_name=consumer_name,
                idempotency_key=idempotency_key,
                tenant_id=event_tenant_id,
                project_id=None,
                task_id=None,
            )
            if status == "succeeded":
                # A previous attempt completed before the published_answer
                # disappeared. Honor the previous outcome.
                return "skipped_already_succeeded"
            mark_failed(
                conn,
                record_id=record_id,
                error_code=ERR_PUBLISHED_ANSWER_NOT_VISIBLE,
                error_message=(
                    f"published_answer {published_answer_id} not visible at "
                    "processing time; redelivery can resume."
                ),
            )
            return "failed"

        # Resolution succeeded: scope carries the canonical (tenant, project,
        # task) for this published_answer. Open the EPR row with that scope.
        record_id, status = begin_processing(
            conn,
            event_id=event_id,
            event_type=event_type,
            consumer_name=consumer_name,
            idempotency_key=idempotency_key,
            tenant_id=scope["tenant_id"],
            project_id=scope["project_id"],
            task_id=scope["task_id"],
        )
        if status == "succeeded":
            return "skipped_already_succeeded"

        # 3) Delegate to the lifecycle service. The service is fully
        #    idempotent at the schema level; we just need to surface its
        #    outcome on the EPR row so retries observe the right state.
        try:
            result = apply_withdrawal(
                conn,
                published_answer_id=published_answer_id,
                event_reason=event_reason,
                idempotency_key=lifecycle_idempotency_key,
                requested_by=requested_by,
                event_payload=event_payload,
            )
        except Exception as exc:  # noqa: BLE001 — we want to capture any unhandled error
            logger.exception(
                "published_answer_withdrawal.service_failed",
                event_id=str(event_id),
                published_answer_id=str(published_answer_id),
            )
            mark_failed(
                conn,
                record_id=record_id,
                error_code=ERR_HANDLER_FAIL,
                error_message=str(exc)[:500],
            )
            return "failed"

        service_status = str(result.get("status", ""))

        # The service reports five outcomes (see apply_withdrawal docstring):
        #   - "withdrawn"           — first effective transition.
        #   - "already_withdrawn"   — terminal state already reached.
        #   - "already_superseded"  — terminal state already reached (other branch).
        #   - "unsupported_status"  — defensive future-compat branch.
        #   - "not_found"           — race against a concurrent DELETE; treated
        #                              as a soft failure on the EPR.
        #
        # The first four outcomes are all valid no-op-or-progress results and
        # the EPR is marked succeeded. "not_found" is logged as a failure on
        # the EPR so an operator can inspect it; it does not leave the
        # consumer-level idempotency slot in a state where a later redelivery
        # would mistakenly be classified as "skipped_already_succeeded" if
        # the published_answer is recreated.
        if service_status in ("withdrawn", "already_withdrawn", "already_superseded"):
            mark_succeeded(conn, record_id=record_id)
            return "processed"

        if service_status == "unsupported_status":
            # Forward-compat: the service detected a status it does not yet
            # know how to handle. The lifecycle log already received an audit
            # via the service path (or no-op if appropriate); we mark the
            # EPR succeeded so we do not retry indefinitely. The service
            # already logs a structured warning.
            mark_succeeded(conn, record_id=record_id)
            return "processed"

        if service_status == "not_found":
            # The published_answer existed when we resolved its scope and
            # disappeared before apply_withdrawal could lock it. Race window
            # is extremely narrow (same transaction) but theoretically
            # possible if a concurrent superuser deletes the row between the
            # two SELECTs. We classify this as a failed EPR so an operator
            # is alerted; the redelivery path will return "failed" again
            # until the row reappears or the alert is cleared.
            mark_failed(
                conn,
                record_id=record_id,
                error_code=ERR_PUBLISHED_ANSWER_NOT_VISIBLE,
                error_message=(
                    f"published_answer {published_answer_id} disappeared "
                    "between scope resolution and service call."
                ),
            )
            return "failed"

        # Unknown service status: surface as failed for safety. The service
        # contract is small and stable, so this branch is purely defensive.
        logger.error(
            "published_answer_withdrawal.unknown_service_status",
            event_id=str(event_id),
            published_answer_id=str(published_answer_id),
            service_status=service_status,
        )
        mark_failed(
            conn,
            record_id=record_id,
            error_code=ERR_HANDLER_FAIL,
            error_message=f"unknown service status: {service_status!r}",
        )
        return "failed"
