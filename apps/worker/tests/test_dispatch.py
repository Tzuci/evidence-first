"""Worker-level tests for apps/worker/app/consumers/dispatch.py
(Phase 8.5 — Block 3C-2).

Coverage map (9 scenarios required by the block prompt):

  1. test_dispatch_routes_task_created_with_redis_consumer_name
  2. test_dispatch_routes_task_created_legacy_alias_with_fallback_consumer_name
  3. test_dispatch_routes_withdrawal_without_forwarding_redis_consumer_name
  4. test_dispatch_routes_source_loss_without_forwarding_redis_consumer_name
  5. test_dispatch_unknown_event_type_returns_failed
  6. test_dispatch_missing_event_type_returns_failed
  7. test_dispatch_empty_event_type_returns_failed
  8. test_dispatch_non_string_event_type_returns_failed
  9. test_dispatch_preserves_underlying_status_values

Design notes:
  - These tests are pure routing tests: NO database, NO Redis, NO real
    consumer invocations. We patch the three handler symbols on the
    ``dispatch`` module itself (not on their source modules), since
    ``dispatch`` imports them at module load time via
    ``from .task_created import handle_task_created`` etc., and Python
    binds those names into the ``dispatch`` namespace. Patching the
    source module would have no effect on what ``dispatch`` actually
    calls.
  - Fake handlers for the lifecycle and source-loss consumers are
    defined WITHOUT a ``consumer_name`` keyword. This is the load-bearing
    invariant of Block 3C-1: the dispatcher must NOT forward
    ``redis_consumer_name`` to those two consumers, because their EPR
    UNIQUE (consumer_name, idempotency_key) must remain global across
    worker instances. If a future regression makes the dispatcher
    forward ``consumer_name`` to either of them, the corresponding test
    fails with ``TypeError`` (unexpected keyword argument), which is
    exactly the desired alarm.
  - Each test uses its own ``calls`` list and its own set of fake
    handlers to keep cross-test isolation explicit. ``monkeypatch``
    automatically reverts the patches on teardown, so the dispatcher
    module is restored between tests.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.consumers import dispatch


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    calls: list[tuple[Any, ...]],
    task_created_status: str = "processed",
    withdrawal_status: str = "processed",
    source_loss_status: str = "processed",
) -> None:
    """Install fake handlers on the dispatch module.

    Note the deliberate signature asymmetry:
      - ``fake_task_created`` accepts ``consumer_name`` as a kw-only
        argument, mirroring the real handler. The dispatcher MUST pass
        it for ``task.created`` events.
      - ``fake_withdrawal`` and ``fake_source_loss`` do NOT accept
        ``consumer_name``. If the dispatcher mistakenly forwards
        ``redis_consumer_name`` to either of them, Python raises
        ``TypeError: unexpected keyword argument 'consumer_name'`` and
        the test fails with a clear stack trace.
    """

    def fake_task_created(event: dict[str, Any], *, consumer_name: str) -> str:
        calls.append(("task_created", event, consumer_name))
        return task_created_status

    def fake_withdrawal(event: dict[str, Any]) -> str:
        calls.append(("withdrawal", event))
        return withdrawal_status

    def fake_source_loss(event: dict[str, Any]) -> str:
        calls.append(("source_loss", event))
        return source_loss_status

    monkeypatch.setattr(dispatch, "handle_task_created", fake_task_created)
    monkeypatch.setattr(
        dispatch, "handle_published_answer_withdrawal", fake_withdrawal
    )
    monkeypatch.setattr(dispatch, "handle_source_loss", fake_source_loss)


# ---------------------------------------------------------------------------
# 1 — task.created routes with redis_consumer_name
# ---------------------------------------------------------------------------
def test_dispatch_routes_task_created_with_redis_consumer_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    _install_fakes(monkeypatch, calls=calls)

    event = {"event_type": "task.created", "event_id": "evt-1"}
    rc = dispatch.handle_event(event, redis_consumer_name="worker_123")

    assert rc == "processed"
    assert len(calls) == 1
    kind, received_event, consumer_name = calls[0]
    assert kind == "task_created"
    # Same dict object: dispatch must not deep-copy or mutate the event.
    assert received_event is event
    assert consumer_name == "worker_123"


# ---------------------------------------------------------------------------
# 2 — legacy "task_created" alias falls back to TASK_CREATED_CONSUMER_NAME_FALLBACK
# ---------------------------------------------------------------------------
def test_dispatch_routes_task_created_legacy_alias_with_fallback_consumer_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    _install_fakes(monkeypatch, calls=calls)

    event = {"event_type": "task_created", "event_id": "evt-2"}
    # No redis_consumer_name provided: dispatcher must use the stable
    # logical fallback.
    rc = dispatch.handle_event(event)

    assert rc == "processed"
    assert len(calls) == 1
    kind, received_event, consumer_name = calls[0]
    assert kind == "task_created"
    assert received_event is event
    # Accept the constant exposed by the dispatch module so a future
    # rename of the fallback string does not silently break this test.
    assert consumer_name == dispatch.TASK_CREATED_CONSUMER_NAME_FALLBACK
    assert consumer_name == "worker_dispatch"


# ---------------------------------------------------------------------------
# 3 — withdrawal routing must NOT forward redis_consumer_name
# ---------------------------------------------------------------------------
def test_dispatch_routes_withdrawal_without_forwarding_redis_consumer_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    # The fake_withdrawal signature defined inside _install_fakes does NOT
    # accept consumer_name. If dispatch.handle_event forwards
    # redis_consumer_name to it, this call raises TypeError and the test
    # fails — which is the intended invariant guard.
    _install_fakes(monkeypatch, calls=calls)

    event = {
        "event_type": "published_answer.withdrawal_requested",
        "event_id": "evt-3",
    }
    rc = dispatch.handle_event(event, redis_consumer_name="worker_123")

    assert rc == "processed"
    assert len(calls) == 1
    kind, received_event = calls[0]
    assert kind == "withdrawal"
    assert received_event is event
    # No other handler was called.
    assert all(c[0] == "withdrawal" for c in calls)


# ---------------------------------------------------------------------------
# 4 — source_loss routing must NOT forward redis_consumer_name
# ---------------------------------------------------------------------------
def test_dispatch_routes_source_loss_without_forwarding_redis_consumer_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    # Same reasoning as test 3: fake_source_loss does NOT accept
    # consumer_name; an accidental forward would raise TypeError.
    _install_fakes(monkeypatch, calls=calls)

    event = {"event_type": "source_loss.detected", "event_id": "evt-4"}
    rc = dispatch.handle_event(event, redis_consumer_name="worker_123")

    assert rc == "processed"
    assert len(calls) == 1
    kind, received_event = calls[0]
    assert kind == "source_loss"
    assert received_event is event
    assert all(c[0] == "source_loss" for c in calls)


# ---------------------------------------------------------------------------
# 5 — unknown event_type returns "failed", no handler called
# ---------------------------------------------------------------------------
def test_dispatch_unknown_event_type_returns_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    _install_fakes(monkeypatch, calls=calls)

    event = {"event_type": "unknown.event", "event_id": "evt-5"}
    rc = dispatch.handle_event(event, redis_consumer_name="worker_123")

    assert rc == "failed"
    assert calls == []


# ---------------------------------------------------------------------------
# 6 — missing event_type key returns "failed", no handler called
# ---------------------------------------------------------------------------
def test_dispatch_missing_event_type_returns_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    _install_fakes(monkeypatch, calls=calls)

    event: dict[str, Any] = {"event_id": "evt-6"}  # no event_type
    rc = dispatch.handle_event(event)

    assert rc == "failed"
    assert calls == []


# ---------------------------------------------------------------------------
# 7 — empty-string event_type returns "failed", no handler called
# ---------------------------------------------------------------------------
def test_dispatch_empty_event_type_returns_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    _install_fakes(monkeypatch, calls=calls)

    event = {"event_type": "", "event_id": "evt-7"}
    rc = dispatch.handle_event(event)

    assert rc == "failed"
    assert calls == []


# ---------------------------------------------------------------------------
# 8 — non-string event_type returns "failed", no handler called
# ---------------------------------------------------------------------------
def test_dispatch_non_string_event_type_returns_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    _install_fakes(monkeypatch, calls=calls)

    # event_type is an int: must be rejected before any routing.
    event: dict[str, Any] = {"event_type": 123, "event_id": "evt-8"}
    rc = dispatch.handle_event(event)

    assert rc == "failed"
    assert calls == []


# ---------------------------------------------------------------------------
# 9 — dispatcher faithfully passes through underlying status values
# ---------------------------------------------------------------------------
def test_dispatch_preserves_underlying_status_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatcher must NOT translate or sanitize handler return values.
    Each of the three real consumers may return any status from the shared
    taxonomy (``processed``, ``skipped_already_succeeded``,
    ``skipped_terminal``, ``failed``); the dispatcher's job is purely to
    route and pass-through.
    """
    # Sub-case 9a: task_created -> "skipped_terminal".
    calls_a: list[tuple[Any, ...]] = []
    _install_fakes(
        monkeypatch,
        calls=calls_a,
        task_created_status="skipped_terminal",
    )
    event_a = {"event_type": "task.created", "event_id": "evt-9a"}
    rc_a = dispatch.handle_event(event_a, redis_consumer_name="worker_123")
    assert rc_a == "skipped_terminal"
    assert len(calls_a) == 1
    assert calls_a[0][0] == "task_created"

    # Sub-case 9b: withdrawal -> "skipped_already_succeeded".
    calls_b: list[tuple[Any, ...]] = []
    _install_fakes(
        monkeypatch,
        calls=calls_b,
        withdrawal_status="skipped_already_succeeded",
    )
    event_b = {
        "event_type": "published_answer.withdrawal_requested",
        "event_id": "evt-9b",
    }
    rc_b = dispatch.handle_event(event_b, redis_consumer_name="worker_123")
    assert rc_b == "skipped_already_succeeded"
    assert len(calls_b) == 1
    assert calls_b[0][0] == "withdrawal"

    # Sub-case 9c: source_loss -> "failed".
    calls_c: list[tuple[Any, ...]] = []
    _install_fakes(
        monkeypatch,
        calls=calls_c,
        source_loss_status="failed",
    )
    event_c = {"event_type": "source_loss.detected", "event_id": "evt-9c"}
    rc_c = dispatch.handle_event(event_c, redis_consumer_name="worker_123")
    assert rc_c == "failed"
    assert len(calls_c) == 1
    assert calls_c[0][0] == "source_loss"
