"""Worker entrypoint: Redis Streams consumer loop.

Stream: app.events.task_created
Group:  worker_default
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from typing import Any

import structlog
from redis.exceptions import ResponseError

from .config import get_settings
from .consumers.task_created import handle_task_created
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
                status = handle_task_created(event, consumer_name=consumer_name)
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
