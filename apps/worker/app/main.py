"""Worker entrypoint: Redis Streams consumer loop.

Streams (Phase 8.5 — Block 3E-1, multi-stream wiring):
    - app.events.task_created                            (task.created)
    - app.events.published_answer_withdrawal_requested   (published_answer.withdrawal_requested)
    - app.events.source_loss_detected                    (source_loss.detected)

All three streams share the SAME consumer group (default ``worker_default``).
This is intentional: the streams are disjoint at the application layer
(different event_type values, different downstream consumers), so a single
shared group keeps PEL and redelivery semantics uniform without any risk of
cross-talk. Stream names and group name are configurable via ``WorkerSettings``;
defaults are sensible for dev and prod, and ``.env.example`` is unchanged.

Routing:
    The Redis loop delegates event handling to
    ``apps.worker.app.consumers.dispatch.handle_event``. The dispatcher
    inspects ``event_type`` and routes to the correct handler:

        - ``task.created`` / ``task_created`` (legacy alias)
              → ``handle_task_created`` with the per-instance worker name
                forwarded as ``consumer_name`` (preserves historical
                behavior; idempotency_key on the task.created EPR defaults
                to task_id, so the per-instance scoping does not break
                idempotency).
        - ``published_answer.withdrawal_requested``
              → ``handle_published_answer_withdrawal`` with its STABLE
                logical consumer_name (the per-instance worker name is
                NEVER forwarded — the EPR UNIQUE for this consumer must
                stay global across worker instances).
        - ``source_loss.detected``
              → ``handle_source_loss`` with the same stable-logical
                consumer_name pattern. Same rationale as above.

    The invariant "redis_consumer_name must NEVER become consumer_name EPR
    for withdrawal / source_loss" is enforced inside the dispatcher itself.
    This loop just supplies ``redis_consumer_name`` and lets the dispatcher
    decide which handlers receive it. See ``dispatch.handle_event`` for
    the full rationale.

ACK semantics (unchanged):
    ACK only when the dispatcher returns one of:
        - "processed"
        - "skipped_already_succeeded"
        - "skipped_in_flight"
        - "skipped_terminal"
    On "failed" the entry is left pending so it can be retried or inspected
    via ``XPENDING`` / ``XCLAIM``. ``xack`` is performed against the
    concrete stream name returned by ``xreadgroup``, NOT against a
    hardcoded stream constant.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from typing import Any

import structlog
from redis.exceptions import ResponseError

from .config import get_settings
from .consumers.dispatch import handle_event
from .redis import get_redis


logger = structlog.get_logger(__name__)


_shutdown = False

# Statuses on which the consumer ACKs the Redis entry. Anything else
# (notably "failed") leaves the entry pending so the operator can decide
# whether to retry, claim, or drop it. This taxonomy is unchanged from
# the single-stream loop and matches the values returned by the three
# underlying handlers.
_ACK_STATUSES: frozenset[str] = frozenset(
    {
        "processed",
        "skipped_already_succeeded",
        "skipped_in_flight",
        "skipped_terminal",
    }
)


def _install_signal_handlers() -> None:
    def _handler(signum, frame):  # noqa: ARG001
        global _shutdown
        _shutdown = True
        logger.info("worker.shutdown_requested", signal=signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _ensure_group(r, stream: str, group: str) -> None:
    """Create the consumer group on ``stream`` if it does not exist yet.

    Tolerates BUSYGROUP (group already present, possibly from a previous
    worker instance). All other ``ResponseError`` instances propagate so
    that a misconfigured Redis surfaces immediately at boot.
    """
    try:
        r.xgroup_create(name=stream, groupname=group, id="$", mkstream=True)
        logger.info("worker.group_created", stream=stream, group=group)
    except ResponseError as exc:
        if "BUSYGROUP" in str(exc):
            return
        raise


def _decode_event(fields: dict[Any, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in fields.items():
        kk = k.decode("utf-8") if isinstance(k, (bytes, bytearray)) else str(k)
        vv = v.decode("utf-8") if isinstance(v, (bytes, bytearray)) else str(v)
        out[kk] = vv
    return out


def _decode_stream_name(value: Any) -> str:
    """Normalize the stream name returned by ``xreadgroup``.

    redis-py with ``decode_responses=True`` (the worker's default; see
    ``apps/worker/app/redis.py``) returns ``str``, but we keep the
    bytes-safe branch for parity with ``_decode_event`` and to remain
    defensive against future changes to the Redis client configuration.
    """
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    return str(value)


def _decode_entry_id(value: Any) -> str:
    """Normalize the entry id returned by ``xreadgroup``.

    Same rationale as ``_decode_stream_name``: bytes-safe even though the
    pool is configured with ``decode_responses=True`` today.
    """
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    return str(value)


def main() -> int:
    settings = get_settings()
    consumer_name = settings.WORKER_CONSUMER_NAME or f"worker_{os.getpid()}"

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )

    streams = settings.event_streams

    logger.info(
        "worker.starting",
        consumer=consumer_name,
        streams=streams,
        group=settings.CONSUMER_GROUP_NAME,
    )

    _install_signal_handlers()
    r = get_redis()

    # Ensure the consumer group exists on every configured stream. We use
    # the SAME group across all streams (see module docstring); each call
    # tolerates BUSYGROUP independently so a partial pre-existing setup
    # (e.g. one stream created on a previous deploy) is handled cleanly.
    for stream_name in streams:
        _ensure_group(r, stream_name, settings.CONSUMER_GROUP_NAME)

    # Build the streams argument for xreadgroup once: redis-py expects a
    # dict of {stream_name: last_id}, where ">" means "deliver new
    # entries that have not been delivered to any consumer in this group
    # yet". The dict ordering is preserved by xreadgroup in the response.
    xread_streams: dict[str, str] = {stream_name: ">" for stream_name in streams}

    while not _shutdown:
        try:
            resp = r.xreadgroup(
                groupname=settings.CONSUMER_GROUP_NAME,
                consumername=consumer_name,
                streams=xread_streams,
                count=10,
                block=2000,  # ms
            )
        except Exception as exc:
            logger.exception(
                "worker.xreadgroup_error",
                error=str(exc),
                streams=streams,
            )
            time.sleep(1)
            continue

        if not resp:
            continue

        # Multi-stream response shape:
        #   [(stream_name, [(entry_id, fields), ...]), ...]
        # Streams with no new entries are simply omitted from the
        # response by Redis, so we iterate over whatever came back.
        for raw_stream_name, entries in resp:
            stream_name = _decode_stream_name(raw_stream_name)
            for entry_id, fields in entries:
                eid = _decode_entry_id(entry_id)
                event = _decode_event(fields)

                # Route through the dispatcher. The per-instance worker
                # name is forwarded as redis_consumer_name; the dispatcher
                # decides which handlers receive it (task.created only)
                # vs. which keep their stable logical consumer_name
                # (withdrawal, source_loss). See dispatch.handle_event
                # docstring for the full rationale.
                status = handle_event(event, redis_consumer_name=consumer_name)

                if status in _ACK_STATUSES:
                    try:
                        r.xack(stream_name, settings.CONSUMER_GROUP_NAME, eid)
                    except Exception:
                        logger.exception(
                            "worker.xack_failed",
                            stream=stream_name,
                            entry_id=eid,
                        )
                else:
                    # 'failed' (or any unexpected non-ACK status): leave
                    # the entry pending so it can be retried or inspected
                    # via XPENDING / XCLAIM.
                    logger.warning(
                        "worker.entry_left_pending",
                        stream=stream_name,
                        entry_id=eid,
                        status=status,
                        event=event,
                    )

    logger.info("worker.exited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
