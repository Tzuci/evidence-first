"""Deterministic vectors for canonical_dumps and canonical_sha256."""
from __future__ import annotations

import datetime as dt
import uuid

import pytest

from evidencefirst_shared.canonical_json import canonical_dumps, canonical_sha256


def test_keys_are_sorted_recursively():
    obj = {"b": 2, "a": {"y": 1, "x": [{"q": 4, "p": 3}]}}
    expected = '{"a":{"x":[{"p":3,"q":4}],"y":1},"b":2}'
    assert canonical_dumps(obj) == expected


def test_null_is_preserved():
    obj = {"a": None, "b": 1}
    assert canonical_dumps(obj) == '{"a":null,"b":1}'


def test_datetime_is_iso_z_with_milliseconds():
    d = dt.datetime(2026, 4, 28, 13, 45, 0, 123000, tzinfo=dt.timezone.utc)
    assert canonical_dumps({"t": d}) == '{"t":"2026-04-28T13:45:00.123Z"}'


def test_naive_datetime_treated_as_utc():
    d = dt.datetime(2026, 4, 28, 13, 45, 0, 0)
    assert canonical_dumps({"t": d}) == '{"t":"2026-04-28T13:45:00.000Z"}'


def test_bytes_are_lowercase_hex():
    obj = {"h": bytes.fromhex("deadBEEF")}
    assert canonical_dumps(obj) == '{"h":"deadbeef"}'


def test_uuid_is_lowercase():
    u = uuid.UUID("ABCDEF01-2345-6789-ABCD-EF0123456789")
    assert canonical_dumps({"u": u}) == '{"u":"abcdef01-2345-6789-abcd-ef0123456789"}'


def test_no_whitespace():
    obj = {"a": [1, 2, 3], "b": {"c": True}}
    out = canonical_dumps(obj)
    assert " " not in out
    assert "\n" not in out


def test_stable_across_runs():
    obj = {"z": [3, 2, 1], "a": {"k2": "v", "k1": None}}
    runs = {canonical_dumps(obj) for _ in range(5)}
    assert len(runs) == 1


def test_sha256_is_stable_for_fixed_input():
    obj = {"event": "task.created", "seq": 1, "task_id": "11111111-1111-1111-1111-111111111111"}
    h1 = canonical_sha256(obj)
    h2 = canonical_sha256(obj)
    assert h1 == h2
    # Idempotency across literal reordering
    obj2 = {"task_id": "11111111-1111-1111-1111-111111111111", "seq": 1, "event": "task.created"}
    assert canonical_sha256(obj2) == h1


def test_rejects_nan():
    with pytest.raises(ValueError):
        canonical_dumps({"x": float("nan")})


def test_rejects_inf():
    with pytest.raises(ValueError):
        canonical_dumps({"x": float("inf")})


def test_rejects_unsupported_type():
    class Foo:
        pass

    with pytest.raises(TypeError):
        canonical_dumps({"x": Foo()})