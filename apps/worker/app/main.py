"""Worker entrypoint: Redis Streams consumer loop.

Stream: app.events.task_created
Group:  worker_default

Phase 8.5 — Block 3D-1 integration:
  The Redis loop now delegates event handling to
  ``apps.worker.app.consumers.dispatch.handle_event`` instead of calling
  ``handle_task_created`` directly. ``handle_event`` performs event-type
  routing internally (task.created / task_created legacy alias /
  published_answer.withdrawal_requested / source_loss.detected) and
  returns the same status taxonomy used by the underlying handlers
  (``processed``, ``skipped_already_succeeded``, ``skipped_terminal``,
  ``failed``).

  We pass the per-instance Redis consumer name as ``redis_consumer_name``
  to preserve the historical behavior of this loop for ``task.created``:
  the dispatcher forwards it ONLY to ``handle_task_created`` and
  deliberately withholds it from the lifecycle / source-loss consumers
  (their EPR UNIQUE (consumer_name, idempotency_key) must remain global
  across worker instances). The invariant is enforced by the dispatcher
  itself; this loop just supplies the value.

  Scope of this block: single-stream integration only. We deliberately do
  NOT add new Redis streams, new consumer groups, or any new
  xreadgroup call. Routing is currently a no-op for non-task.created
  event types unless they happen to land on this stream — multi-stream
  fan-out is deferred to a later block.
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


def _install_signal_handlers() -> None:
    def _handler(signum, frame):  # noqa: ARG001
        global _shutdown
        _shutdown = True
        logger.info("worker.shutdown_requested", signal=signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _ensure_group(r, stream: str, group: str) -> None:
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

    logger.info(
        "worker.starting",
        consumer=consumer_name,
        stream=settings.EVENTS_TASK_CREATED_STREAM,
        group=settings.CONSUMER_GROUP_NAME,
    )

    _install_signal_handlers()
    r = get_redis()
    _ensure_group(r, settings.EVENTS_TASK_CREATED_STREAM, settings.CONSUMER_GROUP_NAME)

    while not _shutdown:
        try:
            resp = r.xreadgroup(
                groupname=settings.CONSUMER_GROUP_NAME,
                consumername=consumer_name,
                streams={settings.EVENTS_TASK_CREATED_STREAM: ">"},
                count=10,
                block=2000,  # ms
            )
        except Exception as exc:
            logger.exception("worker.xreadgroup_error", error=str(exc))
            time.sleep(1)
            continue

        if not resp:
            continue

        for _stream_name, entries in resp:
            for entry_id, fields in entries:
                eid = entry_id.decode("utf-8") if isinstance(entry_id, (bytes, bytearray)) else str(entry_id)
                event = _decode_event(fields)
                # Route through the dispatcher. The per-instance worker
                # name is forwarded as redis_consumer_name; the dispatcher
                # decides which handlers receive it (task.created only)
                # vs. which keep their stable logical consumer_name
                # (withdrawal, source_loss). See dispatch.handle_event
                # docstring for the full rationale.
                status = handle_event(event, redis_consumer_name=consumer_name)
                if status in (
                    "processed",
                    "skipped_already_succeeded",
                    "skipped_in_flight",
                    "skipped_terminal",
                ):
                    try:
                        r.xack(settings.EVENTS_TASK_CREATED_STREAM, settings.CONSUMER_GROUP_NAME, eid)
                    except Exception:
                        logger.exception("worker.xack_failed", entry_id=eid)
                else:
                    # 'failed': leave the entry pending so it can be retried/inspected.
                    logger.warning("worker.entry_left_pending", entry_id=eid, event=event)

    logger.info("worker.exited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
