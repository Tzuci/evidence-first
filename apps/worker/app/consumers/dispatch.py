"""Worker event dispatcher (Phase 8.5 — Block 3C-1).

This module exposes a single entry point, ``handle_event``, that routes an
already-decoded event dict to the correct consumer handler based on the
event's ``event_type`` field.

Scope of this block:
  - Dispatcher-only. The function is invoked directly with a decoded event
    dict; it does NOT read from Redis and is NOT registered in
    ``apps/worker/app/main.py``. Wiring into ``main.py`` is deferred to a
    later block.
  - No test file in this block.
  - No HTTP endpoint, no migration, no doc changes.
  - No mutations to ``task_created.py``, ``published_answer_withdrawal.py``,
    or ``source_loss.py``.

Routing table:
  - ``"task.created"`` and ``"task_created"`` (legacy alias) →
    ``handle_task_created`` with ``consumer_name=redis_consumer_name``
    (or a stable fallback). The ``task.created`` consumer is keyed on
    ``(consumer_name, idempotency_key)`` where the idempotency key
    defaults to the ``task_id``; passing the per-instance worker
    consumer name here matches the existing behavior of
    ``apps/worker/app/main.py`` and does not break idempotency because
    the idempotency_key is task-scoped.
  - ``"published_answer.withdrawal_requested"`` →
    ``handle_published_answer_withdrawal`` with the consumer's stable
    logical default (``"published_answer_withdrawal"``). The
    per-instance worker name is NEVER forwarded: the consumer-level
    UNIQUE on ``event_processing_records`` for this consumer must remain
    global and stable across worker instances, otherwise a redelivery
    routed to a different worker would mistakenly create a fresh EPR
    slot and re-run the lifecycle service.
  - ``"source_loss.detected"`` →
    ``handle_source_loss`` with the consumer's stable logical default
    (``"source_loss"``). Same rationale as above: the EPR uniqueness
    must be global across worker instances.

Unknown / missing / malformed event_type:
  The dispatcher logs a structured error and returns ``"failed"``. The
  underlying handlers' return contract is preserved end-to-end: callers
  (i.e. the future ``main.py`` integration) can use the same status
  taxonomy regardless of which event_type came through.

Return values:
  Pass-through of the underlying handler's return value, or ``"failed"``
  when the event_type cannot be routed. The full taxonomy used by the
  three consumers is::

      "processed"
      "skipped_already_succeeded"
      "skipped_terminal"           (only emitted by handle_task_created)
      "failed"

  This module does NOT introduce any new status string.
"""
from __future__ import annotations

from typing import Any

import structlog

from .published_answer_withdrawal import handle_published_answer_withdrawal
from .source_loss import handle_source_loss
from .task_created import handle_task_created


logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# event_type constants
# ---------------------------------------------------------------------------
# Canonical names. The first three match the EVENT_TYPE_* constants declared
# inside the corresponding consumer modules; the legacy alias exists because
# early producers / fixtures sometimes serialized "task_created" without the
# dot. We accept both for task.created only — the two newer event types do
# not have a legacy form.
EVENT_TYPE_TASK_CREATED = "task.created"
EVENT_TYPE_TASK_CREATED_LEGACY = "task_created"
EVENT_TYPE_WITHDRAWAL_REQUESTED = "published_answer.withdrawal_requested"
EVENT_TYPE_SOURCE_LOSS_DETECTED = "source_loss.detected"


# Fallback consumer_name used for handle_task_created when the caller does
# not provide a per-instance worker name. The task.created consumer's EPR
# idempotency_key defaults to task_id, so the consumer_name component of
# the UNIQUE constraint is effectively per-worker; a stable fallback keeps
# tests and direct invocations deterministic without disturbing prod.
TASK_CREATED_CONSUMER_NAME_FALLBACK = "worker_dispatch"


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def handle_event(
    event: dict[str, Any],
    *,
    redis_consumer_name: str | None = None,
) -> str:
    """Route a decoded event to the correct consumer handler.

    Parameters
    ----------
    event:
        Already-decoded event payload (native dict or Redis-decoded
        ``dict[str, str]``). The dispatcher only inspects ``event_type``;
        all other field validation is delegated to the underlying handler.

    redis_consumer_name:
        Per-instance worker identity (e.g. ``"worker_1234"``). Forwarded
        ONLY to ``handle_task_created`` to preserve the historical
        behavior of ``apps/worker/app/main.py``. Deliberately NOT
        forwarded to the lifecycle / source-loss consumers: those keep a
        stable, logical consumer_name so their EPR-level idempotency
        remains global across worker instances. A redelivery routed to a
        different worker MUST land on the same EPR row, otherwise the
        consumer-level guard would be silently bypassed.

    Returns
    -------
    str
        One of the status strings produced by the underlying handlers
        (``"processed"``, ``"skipped_already_succeeded"``,
        ``"skipped_terminal"``, ``"failed"``), or ``"failed"`` when the
        event_type is missing, non-string, empty, or unknown.
    """
    raw_event_type = event.get("event_type")

    # Reject missing / non-string / empty event_type before reaching any
    # handler. The three consumers all perform their own event_type
    # validation, but the dispatcher has to make a routing decision FIRST,
    # so a malformed event_type is a dispatcher-level failure.
    if not isinstance(raw_event_type, str) or raw_event_type == "":
        logger.error(
            "dispatch.missing_or_malformed_event_type",
            event_type=raw_event_type,
            event_type_python_type=type(raw_event_type).__name__,
        )
        return "failed"

    event_type = raw_event_type

    if event_type in (EVENT_TYPE_TASK_CREATED, EVENT_TYPE_TASK_CREATED_LEGACY):
        # task.created keeps the per-instance worker name when available:
        # this matches the historical behavior of main.py and does not
        # affect idempotency because the EPR idempotency_key for this
        # consumer defaults to task_id (see handle_task_created).
        consumer_name = redis_consumer_name or TASK_CREATED_CONSUMER_NAME_FALLBACK
        return handle_task_created(event, consumer_name=consumer_name)

    if event_type == EVENT_TYPE_WITHDRAWAL_REQUESTED:
        # Stable, logical consumer_name. We deliberately do NOT pass
        # redis_consumer_name: the EPR UNIQUE (consumer_name,
        # idempotency_key) for this consumer must remain global across
        # worker instances so a redelivery routed to a different worker
        # collapses onto the same idempotency slot.
        return handle_published_answer_withdrawal(event)

    if event_type == EVENT_TYPE_SOURCE_LOSS_DETECTED:
        # Same rationale as above. The EPR UNIQUE for source_loss must
        # stay global; per-instance worker names would shard the
        # idempotency space and silently re-run the propagator on
        # cross-worker redelivery.
        return handle_source_loss(event)

    # Unknown event_type: log structured error and return failed.
    logger.error(
        "dispatch.unknown_event_type",
        event_type=event_type,
        event_id=event.get("event_id"),
    )
    return "failed"
