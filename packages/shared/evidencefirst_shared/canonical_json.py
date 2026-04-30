"""Canonical JSON helpers.

Used for deterministic hashing of audit payloads and immutable records.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import Any


def _format_datetime(value: dt.datetime) -> str:
    """Format datetime as UTC ISO-8601 with milliseconds and trailing Z.

    Naive datetimes are treated as UTC.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    else:
        value = value.astimezone(dt.timezone.utc)

    milliseconds = value.microsecond // 1000
    return value.strftime("%Y-%m-%dT%H:%M:%S") + f".{milliseconds:03d}Z"


def _default(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return _format_datetime(value)

    if isinstance(value, dt.date):
        return value.isoformat()

    if isinstance(value, uuid.UUID):
        return str(value).lower()

    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()

    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def canonical_dumps(value: Any) -> str:
    """Return deterministic JSON text for hashing."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_default,
    )


def canonical_sha256(value: Any) -> str:
    """Return sha256 hex digest of canonical JSON."""
    return hashlib.sha256(canonical_dumps(value).encode("utf-8")).hexdigest()


__all__ = ["canonical_dumps", "canonical_sha256"]
