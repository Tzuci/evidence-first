"""Worker-level tests for apps/worker/app/main.py multi-stream loop
(Phase 8.5 — Block 3E-2).

Coverage map (7 scenarios required by the block prompt):

  1. test_main_creates_group_for_all_configured_streams
  2. test_main_xreadgroup_reads_all_streams
  3. test_main_routes_event_with_redis_consumer_name
  4. test_main_acks_on_concrete_stream_returned_by_xreadgroup
  5. test_main_leaves_failed_entry_pending
  6. test_main_handles_multiple_streams_in_one_response
  7. test_decode_helpers_accept_bytes

Design notes:
  - These tests are pure unit tests of the worker loop:
      * NO Redis connection (FakeRedis stub patched onto the module);
      * NO Docker, NO real DB, NO migrations;
      * NO real consumer invocations (handle_event is monkeypatched);
      * NO real time.sleep (patched out so any error-handling fallback
        cannot stall the test).
  - We patch symbols on ``app.main`` itself (NOT on their source
    modules), because ``main`` imports them at module load time via
    ``from .config import get_settings`` etc., binding those names into
    its own namespace. Patching the source module would have no effect
    on what ``main`` actually calls.
  - ``app.main._shutdown`` is module-level state. We reset it before
    AND after every test via an autouse fixture so a forgotten
    ``_shutdown = True`` from one test cannot leak into another.
  - Loop termination strategy: FakeRedis.xreadgroup serves a single
    pre-programmed response on its first invocation, then flips
    ``app.main._shutdown = True`` and returns an empty list on every
    subsequent call. This guarantees the ``while not _shutdown`` loop
    processes the planned entries exactly once and then exits cleanly,
    regardless of internal control flow.
"""
from __future__ import annotations

from typing import Any, Iterable

import pytest

from app import main as worker_main


# ---------------------------------------------------------------------------
# fake settings
# ---------------------------------------------------------------------------
class FakeSettings:
    """Minimal stand-in for ``WorkerSettings`` used by the loop.

    Mirrors the attributes ``main.main()`` reads at runtime, plus the
    ``event_streams`` property that drives the multi-stream wiring.
    Defaults match the production stream names so the assertions in the
    tests can be expressed against literals (which also acts as a
    contract test: a future rename of the streams would break here
    intentionally).
    """

    DATABASE_URL = "postgresql://unused"
    REDIS_URL = "redis://unused"
    LOG_LEVEL = "info"
    WORKER_CONCURRENCY = 1
    WORKER_CONSUMER_NAME = "worker_test"
    EVENTS_TASK_CREATED_STREAM = "app.events.task_created"
    EVENTS_PUBLISHED_ANSWER_WITHDRAWAL_STREAM = (
        "app.events.published_answer_withdrawal_requested"
    )
    EVENTS_SOURCE_LOSS_STREAM = "app.events.source_loss_detected"
    CONSUMER_GROUP_NAME = "worker_default"

    @property
    def event_streams(self) -> list[str]:
        return [
            self.EVENTS_TASK_CREATED_STREAM,
            self.EVENTS_PUBLISHED_ANSWER_WITHDRAWAL_STREAM,
            self.EVENTS_SOURCE_LOSS_STREAM,
        ]


# ---------------------------------------------------------------------------
# fake redis
# ---------------------------------------------------------------------------
class FakeRedis:
    """Minimal Redis stub exposing only the surface area used by the loop.

    Records every call to ``xgroup_create``, ``xreadgroup``, and
    ``xack`` so tests can assert against them. ``xreadgroup`` serves a
    pre-programmed list of responses (one per call); once the responses
    are exhausted, it flips ``app.main._shutdown = True`` and returns
    an empty list, which mirrors the real Redis semantics for an
    idle stream and lets the worker loop exit cleanly.
    """

    def __init__(self, responses: Iterable[Any] | None = None) -> None:
        # Replay queue for xreadgroup. Each element is whatever a real
        # xreadgroup call would return: typically a list of
        # ``(stream_name, [(entry_id, fields), ...])`` tuples, or an
        # empty list to simulate "no new entries within block timeout".
        self._responses: list[Any] = list(responses or [])
        self.xgroup_create_calls: list[dict[str, Any]] = []
        self.xreadgroup_calls: list[dict[str, Any]] = []
        self.xack_calls: list[tuple[str, str, str]] = []

    # ------------------------------------------------------------------
    # XGROUP CREATE
    # ------------------------------------------------------------------
    def xgroup_create(
        self,
        name: str,
        groupname: str,
        id: str = "$",
        mkstream: bool = False,
    ) -> bool:
        self.xgroup_create_calls.append(
            {
                "name": name,
                "groupname": groupname,
                "id": id,
                "mkstream": mkstream,
            }
        )
        return True

    # ------------------------------------------------------------------
    # XREADGROUP
    # ------------------------------------------------------------------
    def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> Any:
        self.xreadgroup_calls.append(
            {
                "groupname": groupname,
                "consumername": consumername,
                # Copy the dict so a later mutation by main() (it
                # shouldn't, but defensive) cannot retroactively change
                # what the test observed.
                "streams": dict(streams),
                "count": count,
                "block": block,
            }
        )
        if self._responses:
            return self._responses.pop(0)
        # No more programmed responses: signal shutdown and return an
        # empty result so the worker loop falls through its
        # ``if not resp: continue`` branch and re-checks ``_shutdown``.
        worker_main._shutdown = True
        return []

    # ------------------------------------------------------------------
    # XACK
    # ------------------------------------------------------------------
    def xack(self, stream: str, group: str, entry_id: str) -> int:
        self.xack_calls.append((stream, group, entry_id))
        return 1


# ---------------------------------------------------------------------------
# autouse fixture: reset module-level _shutdown around every test
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_shutdown_flag():
    """Guarantee a clean slate for ``app.main._shutdown``.

    Without this, a test that fails between programming the FakeRedis
    response and asserting could leave ``_shutdown = True`` set, which
    would cause every subsequent test's ``main()`` call to exit before
    the very first ``xreadgroup`` invocation — producing very
    confusing failures.
    """
    worker_main._shutdown = False
    try:
        yield
    finally:
        worker_main._shutdown = False


# ---------------------------------------------------------------------------
# helper: install the standard set of patches on app.main
# ---------------------------------------------------------------------------
def _install_main_patches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_redis: FakeRedis,
    handle_event_impl,
    settings: FakeSettings | None = None,
) -> FakeSettings:
    """Patch the symbols ``main()`` reaches for at runtime.

    - ``get_settings`` -> returns our FakeSettings.
    - ``get_redis`` -> returns the FakeRedis instance under test.
    - ``handle_event`` -> the test-supplied callable; signature must
      match the dispatcher (``event, *, redis_consumer_name``).
    - ``_install_signal_handlers`` -> no-op (signal handlers cannot be
      installed safely in a non-main thread, and pytest may run tests
      under one).
    - ``time.sleep`` -> no-op (defensive: if any unexpected error path
      reaches the sleep fallback, it must not actually wait).
    """
    s = settings or FakeSettings()

    monkeypatch.setattr(worker_main, "get_settings", lambda: s)
    monkeypatch.setattr(worker_main, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(worker_main, "handle_event", handle_event_impl)
    monkeypatch.setattr(worker_main, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(worker_main.time, "sleep", lambda *_a, **_kw: None)

    return s


# ---------------------------------------------------------------------------
# 1 — main() creates the consumer group on every configured stream
# ---------------------------------------------------------------------------
def test_main_creates_group_for_all_configured_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRedis()  # no programmed entries; main() will exit immediately

    def _handle_event(event, *, redis_consumer_name=None):  # noqa: ARG001
        return "processed"

    settings = _install_main_patches(
        monkeypatch, fake_redis=fake, handle_event_impl=_handle_event
    )

    rc = worker_main.main()
    assert rc == 0

    # One xgroup_create call per configured stream, in the order
    # returned by settings.event_streams.
    streams_called = [c["name"] for c in fake.xgroup_create_calls]
    assert streams_called == [
        "app.events.task_created",
        "app.events.published_answer_withdrawal_requested",
        "app.events.source_loss_detected",
    ]

    # Every call must use the shared group name and create the stream
    # if it does not exist yet.
    for call in fake.xgroup_create_calls:
        assert call["groupname"] == settings.CONSUMER_GROUP_NAME == "worker_default"
        assert call["mkstream"] is True


# ---------------------------------------------------------------------------
# 2 — xreadgroup is invoked with all three streams and the right group/consumer
# ---------------------------------------------------------------------------
def test_main_xreadgroup_reads_all_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRedis()  # empty: first xreadgroup call triggers shutdown

    def _handle_event(event, *, redis_consumer_name=None):  # noqa: ARG001
        return "processed"

    _install_main_patches(
        monkeypatch, fake_redis=fake, handle_event_impl=_handle_event
    )

    rc = worker_main.main()
    assert rc == 0

    # The loop must have called xreadgroup at least once (the very first
    # iteration), and that call must request entries from all three
    # configured streams with last-id ">" (i.e. only undelivered
    # entries).
    assert len(fake.xreadgroup_calls) >= 1
    first = fake.xreadgroup_calls[0]
    assert first["groupname"] == "worker_default"
    assert first["consumername"] == "worker_test"
    assert first["streams"] == {
        "app.events.task_created": ">",
        "app.events.published_answer_withdrawal_requested": ">",
        "app.events.source_loss_detected": ">",
    }


# ---------------------------------------------------------------------------
# 3 — handle_event receives the decoded event dict and the consumer name
# ---------------------------------------------------------------------------
def test_main_routes_event_with_redis_consumer_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FakeRedis returns one entry on the task_created stream. _decode_event
    # decodes both keys and values from bytes/str to str, so we feed the
    # bytes shape that a real Redis client would return when
    # decode_responses=False — the worker must tolerate either. We use
    # bytes here to also exercise the bytes-tolerant branches.
    response = [
        (
            b"app.events.task_created",
            [
                (
                    b"1700000000000-0",
                    {b"event_type": b"task.created", b"event_id": b"evt-1"},
                ),
            ],
        ),
    ]
    fake = FakeRedis(responses=[response])

    captured: list[tuple[dict[str, str], str | None]] = []

    def _handle_event(event, *, redis_consumer_name=None):
        captured.append((event, redis_consumer_name))
        return "processed"

    _install_main_patches(
        monkeypatch, fake_redis=fake, handle_event_impl=_handle_event
    )

    rc = worker_main.main()
    assert rc == 0

    # handle_event was called exactly once with the decoded event dict
    # and the per-instance worker name from settings.WORKER_CONSUMER_NAME.
    assert len(captured) == 1
    event, consumer_name = captured[0]
    assert event == {"event_type": "task.created", "event_id": "evt-1"}
    assert consumer_name == "worker_test"


# ---------------------------------------------------------------------------
# 4 — xack targets the concrete stream returned by xreadgroup, not a constant
# ---------------------------------------------------------------------------
def test_main_acks_on_concrete_stream_returned_by_xreadgroup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Entry served on the source_loss stream. The loop must ACK against
    # that stream, NOT against the task_created stream (which would be
    # the case if the previous single-stream code path had been left
    # in place by mistake).
    response = [
        (
            "app.events.source_loss_detected",
            [
                (
                    "1700000000001-0",
                    {"event_type": "source_loss.detected", "event_id": "evt-2"},
                ),
            ],
        ),
    ]
    fake = FakeRedis(responses=[response])

    def _handle_event(event, *, redis_consumer_name=None):  # noqa: ARG001
        return "processed"

    _install_main_patches(
        monkeypatch, fake_redis=fake, handle_event_impl=_handle_event
    )

    rc = worker_main.main()
    assert rc == 0

    # Exactly one ACK, against the source_loss stream.
    assert len(fake.xack_calls) == 1
    stream, group, entry_id = fake.xack_calls[0]
    assert stream == "app.events.source_loss_detected"
    assert stream != "app.events.task_created"
    assert group == "worker_default"
    assert entry_id == "1700000000001-0"


# ---------------------------------------------------------------------------
# 5 — "failed" status leaves the entry pending (no xack)
# ---------------------------------------------------------------------------
def test_main_leaves_failed_entry_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = [
        (
            "app.events.task_created",
            [
                (
                    "1700000000002-0",
                    {"event_type": "task.created", "event_id": "evt-3"},
                ),
            ],
        ),
    ]
    fake = FakeRedis(responses=[response])

    def _handle_event(event, *, redis_consumer_name=None):  # noqa: ARG001
        return "failed"

    _install_main_patches(
        monkeypatch, fake_redis=fake, handle_event_impl=_handle_event
    )

    rc = worker_main.main()
    assert rc == 0

    # No ACK because handle_event returned "failed".
    assert fake.xack_calls == []


# ---------------------------------------------------------------------------
# 6 — multi-stream response: ACK once per entry on the correct concrete stream
# ---------------------------------------------------------------------------
def test_main_handles_multiple_streams_in_one_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two streams in a single xreadgroup response. The loop must
    # iterate per-stream and ACK each entry against the stream it
    # actually came from.
    response = [
        (
            "app.events.task_created",
            [
                (
                    "1700000000003-0",
                    {"event_type": "task.created", "event_id": "evt-4"},
                ),
            ],
        ),
        (
            "app.events.published_answer_withdrawal_requested",
            [
                (
                    "1700000000004-0",
                    {
                        "event_type": "published_answer.withdrawal_requested",
                        "event_id": "evt-5",
                    },
                ),
            ],
        ),
    ]
    fake = FakeRedis(responses=[response])

    def _handle_event(event, *, redis_consumer_name=None):  # noqa: ARG001
        return "processed"

    _install_main_patches(
        monkeypatch, fake_redis=fake, handle_event_impl=_handle_event
    )

    rc = worker_main.main()
    assert rc == 0

    # Exactly one ACK per entry, on the stream the entry came from.
    assert len(fake.xack_calls) == 2
    by_stream = {call[0]: call for call in fake.xack_calls}
    assert "app.events.task_created" in by_stream
    assert "app.events.published_answer_withdrawal_requested" in by_stream

    task_ack = by_stream["app.events.task_created"]
    withdrawal_ack = by_stream["app.events.published_answer_withdrawal_requested"]

    assert task_ack[1] == "worker_default"
    assert task_ack[2] == "1700000000003-0"
    assert withdrawal_ack[1] == "worker_default"
    assert withdrawal_ack[2] == "1700000000004-0"


# ---------------------------------------------------------------------------
# 7 — decode helpers / loop tolerate bytes for stream name and entry id
# ---------------------------------------------------------------------------
def test_decode_helpers_accept_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct check of the decode helpers, plus an end-to-end check that
    the loop ACKs against the *decoded* stream and entry id when
    FakeRedis returns bytes (simulating a Redis client configured with
    decode_responses=False). This covers both the unit-level contract
    and the integration of the helpers into the loop.
    """
    # Direct unit checks: bytes -> str, str -> str (idempotent).
    assert worker_main._decode_stream_name(b"app.events.source_loss_detected") == (
        "app.events.source_loss_detected"
    )
    assert worker_main._decode_stream_name("app.events.task_created") == (
        "app.events.task_created"
    )
    assert worker_main._decode_entry_id(b"1700000000005-0") == "1700000000005-0"
    assert worker_main._decode_entry_id("1700000000006-0") == "1700000000006-0"

    # End-to-end: FakeRedis returns the stream name and entry id as
    # bytes; the loop must decode them before ACK-ing.
    response = [
        (
            b"app.events.source_loss_detected",
            [
                (
                    b"1700000000007-0",
                    {b"event_type": b"source_loss.detected", b"event_id": b"evt-6"},
                ),
            ],
        ),
    ]
    fake = FakeRedis(responses=[response])

    def _handle_event(event, *, redis_consumer_name=None):  # noqa: ARG001
        return "processed"

    _install_main_patches(
        monkeypatch, fake_redis=fake, handle_event_impl=_handle_event
    )

    rc = worker_main.main()
    assert rc == 0

    assert len(fake.xack_calls) == 1
    stream, group, entry_id = fake.xack_calls[0]
    # Crucially, both must be decoded str, NOT bytes.
    assert isinstance(stream, str)
    assert isinstance(entry_id, str)
    assert stream == "app.events.source_loss_detected"
    assert entry_id == "1700000000007-0"
    assert group == "worker_default"
