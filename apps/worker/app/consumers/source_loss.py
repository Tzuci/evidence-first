"""Source loss consumer (Phase 8.5 — Block 3B-1).

This module exposes a single entry point, ``handle_source_loss``, that
processes a ``source_loss.detected`` event by delegating the actual
propagation work to
``apps.worker.app.services.source_loss_propagator.propagate_source_loss``.

Scope of this block:
  - Handler-only. The handler is invoked directly with a decoded event dict;
    it does NOT read from Redis and is NOT registered in
    ``apps/worker/app/main.py``. Wiring into ``main.py`` is deferred to a
    later block.
  - No HTTP endpoint. The API surface that produces these events (or the
    job that scans for source losses) is also deferred to a later block.
  - No new migration. Schema is fixed at 0006_lifecycle.sql.
  - No changes to ``published_answer_withdrawal.py`` or
    ``source_loss_propagator.py``.
  - No mutations to ``published_answers.status``: the propagator is
    explicitly a "soft cascade" pipeline, and this consumer does not invoke
    ``apply_withdrawal`` either.

Invariants honored (Phase 8.5):
  - ``task_masters.status`` is NOT touched in this code path.
  - ``published_answers.status`` is NEVER mutated by this pipeline. Source
    loss propagation is registered against the claim ledger and against
    ``source_loss_propagation_records``, but never withdraws an answer.
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
       short-circuit (``"skipped_already_succeeded"``), aligned with both
       ``apps/worker/app/consumers/task_created.py`` and
       ``apps/worker/app/consumers/published_answer_withdrawal.py``.
     - On a previous attempt that failed (status ``"failed"`` or
       ``"started"``), the handler proceeds and lets the underlying
       service-level idempotency take over.

  2. **Service-level** via ``propagate_source_loss``:
     - The propagator is fully idempotent on its own: claim ledger appends
       are guarded by detection of a ``source_lost`` head, the
       ``supersedes`` lineage edge uses ``ON CONFLICT DO NOTHING``, and
       ``source_loss_propagation_records`` is protected by four partial
       UNIQUE indexes (one per ``propagation_kind``, restricted to
       ``status IN ('recorded','skipped')``). Audit emission is gated on
       the propagation record actually being inserted on the current call.
     - As a result, even if the consumer-level guard is bypassed (for
       example, two distinct consumer-level idempotency keys racing on
       the same ``source_loss_event_id``), the propagator will not
       duplicate any state.

  The two layers are intentionally redundant: either one alone would
  prevent duplicates, but together they make the handler safe against
  partial failures, redelivery storms, and concurrent invocations.

FK-safety:
  ``event_processing_records.task_id`` has ``ON DELETE RESTRICT`` to
  ``task_masters(id)``, and ``event_processing_records.tenant_id`` is
  ``NOT NULL``. We resolve scope by SELECTing the ``source_loss_events``
  row joined with ``task_masters`` (when ``task_id`` is set on the source
  loss event) so the EPR row carries the correct (tenant, project, task)
  triple. The pattern mirrors the FK-safe approach in
  ``published_answer_withdrawal.py``:

    - If ``source_loss_events`` cannot be resolved up front, ``begin_processing``
      is called with ``project_id=None`` and ``task_id=None`` using the
      ``tenant_id`` provided by the producer (when available); the
      resulting EPR is then marked as ``failed``
      (``WORKER_SOURCE_LOSS_EVENT_NOT_VISIBLE``) and the handler returns
      ``"failed"``. A subsequent redelivery can resume cleanly once the
      row becomes visible.

    - If the producer provided neither a resolvable ``source_loss_event_id``
      nor a ``tenant_id`` fallback, the handler returns ``"failed"``
      WITHOUT writing any EPR row, since
      ``event_processing_records.tenant_id`` is ``NOT NULL``.

    - If the resolution succeeds, ``begin_processing`` is called with the
      resolved scope and the service is invoked. The terminal classification
      of the EPR (``succeeded`` vs ``failed``) is driven by the service
      outcome.

Note on ``source_loss_events.task_id``:
  The schema in 0006_lifecycle.sql declares ``task_id`` as NULLABLE on
  ``source_loss_events`` (a source loss may be cross-project or detected
  before any task is associated). When ``task_id`` is NULL on the source
  loss row, the EPR is opened with ``task_id=None``: the resolved scope
  carries only ``tenant_id`` (and ``project_id`` if non-null). This keeps
  EPR creation FK-safe even when the source loss was registered without a
  specific task.

  When ``task_id`` IS NOT NULL, the resolution helper LEFT JOINs
  ``task_masters`` and prefers the canonical ``(tenant_id, project_id)``
  read from the task row over the values copied onto the source loss row.
  ``COALESCE`` keeps the task-derived values when present and falls back
  to the source loss row values when the join misses (task deleted,
  task_id NULL, etc.). Since ``source_loss_events.tenant_id`` is NOT NULL
  by schema, the resolved tenant_id is always non-null whenever the
  source loss row exists.

Status strings returned to the caller:
  - ``"processed"``                 — service ran, EPR marked ``succeeded``.
                                      The propagator outcomes
                                      ``"propagated"`` and
                                      ``"no_claims_impacted"`` both map
                                      here: they are valid no-op-or-progress
                                      results that should not be retried.
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
  ``task_created.py`` and ``published_answer_withdrawal.py``). It does NOT
  receive a Connection from the caller. The service ``propagate_source_loss``
  does NOT call ``commit()`` / ``rollback()`` itself; the transaction
  context manager flushes the writes on a clean return and rolls back on
  any uncaught exception.

Event shape (consumer contract):
  The handler accepts a decoded ``dict[str, Any]`` (or a string-keyed dict
  like the one produced by the Redis Streams decoder in ``main.py``) with
  the following fields::

      event_id              (str / UUID, REQUIRED)
      event_type            (str, must be 'source_loss.detected')
      source_loss_event_id  (str / UUID, REQUIRED — primary reference;
                             the propagator uses this id to load the row
                             from source_loss_events and compute its
                             impact set)
      idempotency_key       (str, REQUIRED — consumer-level key for EPR;
                             also threaded into the propagator as a call
                             idempotency key for traceability inside the
                             propagation_records details / audit payloads)
      tenant_id             (str / UUID, OPTIONAL — used only as a fallback
                             when ``source_loss_event_id`` is not
                             resolvable, so the handler can still record a
                             failed EPR for operator inspection. If absent
                             AND the source loss row cannot be resolved,
                             the handler returns "failed" without writing
                             any EPR row.)
      event_payload         (dict, OPTIONAL — opaque, ignored by this
                             consumer; the propagator does not accept a
                             pass-through payload field, so any payload
                             metadata supplied here is dropped on purpose)

  The event MAY carry additional fields; the handler ignores them.

  Note on ``tenant_id``: ``event_processing_records.tenant_id`` is
  ``NOT NULL``. We resolve it from ``source_loss_events.tenant_id``
  whenever the source loss row is visible. The optional ``tenant_id``
  event field exists so the handler can still create an EPR row in the
  rare case where the ``source_loss_event_id`` is not visible at the time
  of processing (e.g. delayed visibility); without it, no EPR can be
  persisted and the handler returns ``"failed"`` directly.
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
from ..services.source_loss_propagator import propagate_source_loss


logger = structlog.get_logger(__name__)


# Stable identity of this consumer. Used by ``begin_processing`` to scope the
# UNIQUE (consumer_name, idempotency_key) constraint on event_processing_records.
# Matches the naming convention used by ``task_created.py`` and
# ``published_answer_withdrawal.py`` (consumer_name is free-form text, no enum
# at DB level). NOT a per-instance worker identity such as 'worker_1'.
CONSUMER_NAME_DEFAULT = "source_loss"

EVENT_TYPE_SOURCE_LOSS_DETECTED = "source_loss.detected"

# Error codes used on event_processing_records.error_code. Naming mirrors the
# WORKER_* prefix convention adopted by task_created.py and
# published_answer_withdrawal.py so dashboards can group worker-level errors
# uniformly.
ERR_SOURCE_LOSS_EVENT_NOT_VISIBLE = "WORKER_SOURCE_LOSS_EVENT_NOT_VISIBLE"
ERR_HANDLER_FAIL = "WORKER_SOURCE_LOSS_FAIL"


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
    value that cannot be parsed as a UUID raises ``ValueError`` and is
    handled by the caller.
    """
    if value is None:
        return None
    if isinstance(value, str) and value == "":
        return None
    return _coerce_uuid(value)


# ---------------------------------------------------------------------------
# DB read helpers
# ---------------------------------------------------------------------------
def _resolve_source_loss_event_scope(
    conn: Connection, *, source_loss_event_id: uuid.UUID
) -> dict[str, uuid.UUID | None] | None:
    """Resolve (tenant_id, project_id, task_id) for a source_loss_events row.

    Returns None when the source_loss_events row does not exist. The lookup
    is a plain SELECT (no FOR UPDATE): the propagator acquires its own
    row-level locks on logical_claims as needed inside the same
    transaction. ``source_loss_events`` is append-only, so a shared
    snapshot read is sufficient here.

    Schema notes (0006_lifecycle.sql):
      - ``tenant_id``  NOT NULL — always present on a resolved row.
      - ``project_id`` NULL OK — may be NULL for cross-project losses.
      - ``task_id``    NULL OK — may be NULL when the loss is detected
                                  outside of a specific task scope.

    Resolution policy:
      We LEFT JOIN ``task_masters`` on ``sle.task_id`` and prefer the
      canonical ``(tenant_id, project_id)`` derived from the task row
      whenever the join hits. This avoids EPR rows whose tenant/project
      drifts from the canonical task scope when the source loss row was
      written with stale or denormalized scope columns.

      ``COALESCE`` falls back to the source loss row values when the join
      misses (task_id is NULL, or the task is no longer visible). Because
      ``source_loss_events.tenant_id`` is NOT NULL, the resolved
      ``tenant_id`` is guaranteed non-null whenever the source loss row
      exists; ``project_id`` and ``task_id`` may legitimately be None.
    """
    row = conn.execute(
        text(
            """
            SELECT
              COALESCE(tm.tenant_id,  sle.tenant_id)  AS tenant_id,
              COALESCE(tm.project_id, sle.project_id) AS project_id,
              sle.task_id                             AS task_id
            FROM source_loss_events sle
            LEFT JOIN task_masters tm ON tm.id = sle.task_id
            WHERE sle.id = :sle_id
            """
        ),
        {"sle_id": source_loss_event_id},
    ).first()
    if row is None:
        return None
    m = row._mapping
    return {
        "tenant_id": uuid.UUID(str(m["tenant_id"])),
        "project_id": uuid.UUID(str(m["project_id"])) if m["project_id"] is not None else None,
        "task_id": uuid.UUID(str(m["task_id"])) if m["task_id"] is not None else None,
    }


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def handle_source_loss(
    event: dict[str, Any],
    *,
    consumer_name: str = CONSUMER_NAME_DEFAULT,
) -> str:
    """Process a single ``source_loss.detected`` event.

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
        source_loss_event_id = _coerce_uuid(event["source_loss_event_id"])
        idempotency_key = str(event["idempotency_key"])
    except (KeyError, ValueError, TypeError) as exc:
        logger.error(
            "source_loss.malformed_event",
            error=str(exc),
            raw_event=event,
        )
        return "failed"

    if event_type != EVENT_TYPE_SOURCE_LOSS_DETECTED:
        logger.error(
            "source_loss.unexpected_event_type",
            event_id=str(event_id),
            event_type=event_type,
            expected=EVENT_TYPE_SOURCE_LOSS_DETECTED,
        )
        return "failed"

    # Optional tenant_id from the event, used only as a fallback when the
    # source_loss_events row cannot be resolved (so we still get an EPR row
    # to record the failure for operator inspection). A present-but-malformed
    # value is treated as absent: we degrade gracefully rather than rejecting
    # the event for a fallback-only field.
    try:
        event_tenant_id = _coerce_optional_uuid(event.get("tenant_id"))
    except ValueError:
        event_tenant_id = None

    # 2) Open a transaction and run the consumer-level + service-level pipeline.
    with transaction() as conn:
        scope = _resolve_source_loss_event_scope(
            conn, source_loss_event_id=source_loss_event_id
        )

        if scope is None:
            # FK-safe branch: cannot create an EPR row with a non-resolvable
            # tenant_id (NOT NULL). We still want to record the failure if
            # the producer provided a tenant_id fallback.
            if event_tenant_id is None:
                logger.error(
                    "source_loss.event_not_visible_no_tenant",
                    event_id=str(event_id),
                    source_loss_event_id=str(source_loss_event_id),
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
                # A previous attempt completed before the source_loss_events
                # row disappeared. Honor the previous outcome.
                return "skipped_already_succeeded"
            mark_failed(
                conn,
                record_id=record_id,
                error_code=ERR_SOURCE_LOSS_EVENT_NOT_VISIBLE,
                error_message=(
                    f"source_loss_event {source_loss_event_id} not visible at "
                    "processing time; redelivery can resume."
                ),
            )
            return "failed"

        # Resolution succeeded: scope carries the canonical
        # (tenant, project?, task?) for this source loss. Open the EPR row
        # with that scope. begin_processing accepts NULL project_id / task_id,
        # which is what we need when the source loss was not bound to a task.
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

        # 3) Delegate to the propagator. The service is fully idempotent at
        #    the schema level (partial UNIQUE indexes on
        #    source_loss_propagation_records, ledger head detection, ON
        #    CONFLICT DO NOTHING on claim_lineage). We just need to surface
        #    its outcome on the EPR row so retries observe the right state.
        try:
            result = propagate_source_loss(
                conn,
                source_loss_event_id=source_loss_event_id,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001 — capture any unhandled error
            logger.exception(
                "source_loss.service_failed",
                event_id=str(event_id),
                source_loss_event_id=str(source_loss_event_id),
            )
            mark_failed(
                conn,
                record_id=record_id,
                error_code=ERR_HANDLER_FAIL,
                error_message=str(exc)[:500],
            )
            return "failed"

        service_status = str(result.get("status", ""))

        # The service reports three outcomes (see propagate_source_loss
        # docstring):
        #   - "propagated"          — claims and/or published_answers were
        #                              evaluated; lineage / propagation
        #                              records / audits were emitted as
        #                              appropriate.
        #   - "no_claims_impacted"  — no claim_evidence_links pointed at the
        #                              lost evidence_span; the dedicated
        #                              propagation row was recorded.
        #   - "not_found"           — race against a concurrent DELETE; we
        #                              already resolved scope from the same
        #                              source_loss_events row, so this
        #                              branch is essentially impossible
        #                              within one transaction. We treat it
        #                              defensively as a soft failure on the
        #                              EPR.
        #
        # The first two outcomes are valid no-op-or-progress results and the
        # EPR is marked succeeded. "not_found" is logged as a failure on the
        # EPR so an operator can inspect it.
        if service_status in ("propagated", "no_claims_impacted"):
            mark_succeeded(conn, record_id=record_id)
            return "processed"

        if service_status == "not_found":
            # The source_loss_events row existed when we resolved its scope
            # and disappeared before propagate_source_loss could read it.
            # Race window is essentially zero (same transaction) but we
            # guard it defensively. We classify this as a failed EPR so an
            # operator is alerted; the redelivery path will return "failed"
            # again until the row reappears or the alert is cleared.
            mark_failed(
                conn,
                record_id=record_id,
                error_code=ERR_SOURCE_LOSS_EVENT_NOT_VISIBLE,
                error_message=(
                    f"source_loss_event {source_loss_event_id} disappeared "
                    "between scope resolution and service call."
                ),
            )
            return "failed"

        # Unknown service status: surface as failed for safety. The service
        # contract is small and stable, so this branch is purely defensive.
        logger.error(
            "source_loss.unknown_service_status",
            event_id=str(event_id),
            source_loss_event_id=str(source_loss_event_id),
            service_status=service_status,
        )
        mark_failed(
            conn,
            record_id=record_id,
            error_code=ERR_HANDLER_FAIL,
            error_message=f"unknown service status: {service_status!r}",
        )
        return "failed"
