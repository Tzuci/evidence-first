"""API tests for the published_answer.withdrawal_requested producer endpoint
(Phase 8.5 — Block 4A-2).

Endpoint exercised:
  POST /api/v1/published-answers/{published_answer_id}/withdrawal-requests

Coverage map (9 scenarios required by the block prompt):

  1. test_withdrawal_request_happy_path_queues_event
  2. test_withdrawal_request_defaults_idempotency_and_reason
  3. test_withdrawal_request_uses_provided_idempotency_key_and_default_lifecycle_key
  4. test_withdrawal_request_404_for_missing_published_answer_and_no_xadd
  5. test_withdrawal_request_redis_failure_returns_500
  6. test_withdrawal_request_does_not_mutate_published_answer_or_lifecycle_tables
  7. test_withdrawal_request_event_payload_json_is_compact_sorted_json
  8. test_withdrawal_request_rejects_invalid_requested_by
  9. test_withdrawal_request_empty_strings_default_correctly

Design notes:
  - This file lives under apps/api/tests/. The Python package `app`
    resolves to apps/api/app, so we can import the route module
    directly to monkeypatch its bound symbols.
  - We do NOT use the session-scoped `client` fixture from conftest.py:
    each test instantiates its own TestClient(app) so a function-scoped
    monkeypatch on app.routes.answers.get_redis cannot leak across
    tests. This is the same pattern adopted by
    apps/api/tests/test_answers_endpoints.py.
  - We monkeypatch the get_redis SYMBOL bound inside
    app.routes.answers, not the symbol in app.redis. The route module
    imports it via `from ..redis import get_redis` at module load time;
    patching app.redis.get_redis would have no effect.
  - We do NOT spin up a real Redis. FakeRedis records every xadd call
    so we can assert against the exact stream + fields the producer
    emits.
  - We do NOT seed published_answer_lifecycle_events anywhere: the
    endpoint contract guarantees no lifecycle row is written, and one
    of the tests verifies that count remains zero.
  - We do NOT call apply_withdrawal or handle_published_answer_withdrawal
    here: the consumer is exercised by its own dedicated test file
    (apps/worker/tests/test_published_answer_withdrawal_consumer.py).
  - All identifiers / hashes are unique per invocation (rerun-safe).
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db import get_engine
from app.main import app
from app.routes import answers as answers_route


# ---------------------------------------------------------------------------
# constants under test
# ---------------------------------------------------------------------------
EXPECTED_STREAM = "app.events.published_answer_withdrawal_requested"
EXPECTED_EVENT_TYPE = "published_answer.withdrawal_requested"
EXPECTED_DEFAULT_REASON = "withdrawal_requested_via_api"


# ---------------------------------------------------------------------------
# environment guard
# ---------------------------------------------------------------------------
def _skip_if_db_unreachable() -> None:
    """Skip the test if Postgres is not reachable.

    Mirrors conftest.py's gating, but localized to the DB only: Redis
    is monkeypatched in every test of this module, so its availability
    is irrelevant. We still need the DB to seed the published_answer
    chain that the endpoint resolves.
    """
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set; bring up the stack first.")
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("DB unreachable; run `make up` and `make migrate && make seed`.")


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------
def _unique_hex() -> str:
    """Return a rerun-safe sha256 hex string unique per call."""
    return hashlib.sha256(uuid.uuid4().bytes + uuid.uuid4().bytes).hexdigest()


def _err(resp_json: dict[str, Any]) -> dict[str, Any]:
    """Extract the normalized error envelope from a NormalizedError response.

    Envelope shape (from packages/shared/evidencefirst_shared/errors.py):
        {"error": {"code": "...", "message": "...", "details": {...}, ...}}
    """
    err = resp_json.get("error")
    assert err is not None, f"missing 'error' envelope in response: {resp_json}"
    assert isinstance(err, dict), f"'error' is not a dict: {err!r}"
    return err


# ---------------------------------------------------------------------------
# DB seeding helpers (no consumer, no Redis)
# ---------------------------------------------------------------------------
def _seeded_dev(
    conn: Connection,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ensure tenant + user; create a FRESH project and task per invocation.

    Returns (tenant_id, project_id, user_id, task_id).
    """
    row = conn.execute(
        text(
            """
            INSERT INTO tenants (name, slug, status) VALUES ('Dev','dev','active')
            ON CONFLICT (slug) DO NOTHING
            RETURNING id
            """
        )
    ).first()
    if row is None:
        row = conn.execute(text("SELECT id FROM tenants WHERE slug = 'dev'")).one()
    tenant_id = uuid.UUID(str(row[0]))

    row = conn.execute(
        text(
            """
            INSERT INTO users (tenant_id, email, display_name, status)
            VALUES (:t, 'dev@local', 'Dev', 'active')
            ON CONFLICT (tenant_id, email) DO NOTHING
            RETURNING id
            """
        ),
        {"t": tenant_id},
    ).first()
    if row is None:
        row = conn.execute(
            text("SELECT id FROM users WHERE tenant_id = :t AND email = 'dev@local'"),
            {"t": tenant_id},
        ).one()
    user_id = uuid.UUID(str(row[0]))

    project_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO projects (tenant_id, name, mode_default)
                    VALUES (:t, :n, 'closed_corpus')
                    RETURNING id
                    """
                ),
                {"t": tenant_id, "n": f"withdraw-api-test-{uuid.uuid4()}"},
            ).first()[0]
        )
    )

    task_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO task_masters
                        (tenant_id, project_id, created_by, mode, objective, status)
                    VALUES (:t, :p, :u, 'closed_corpus', :o, 'created')
                    RETURNING id
                    """
                ),
                {
                    "t": tenant_id,
                    "p": project_id,
                    "u": user_id,
                    "o": f"obj-{uuid.uuid4()}",
                },
            ).first()[0]
        )
    )
    return tenant_id, project_id, user_id, task_id


def _create_published_answer(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    status: str = "published",
) -> uuid.UUID:
    """Create draft_final_answers + final_gate_reports + published_answers v1
    for the given task. Returns the published_answer id.

    The FK chain task -> draft -> gate -> published is locked down by the
    composite constraints declared in 0005_answers_gate.sql:
      - draft_final_answers UNIQUE (id, task_id)
      - final_gate_reports.(draft_final_answer_id, task_id) -> draft.(id, task_id)
      - final_gate_reports UNIQUE (id, task_id, draft_final_answer_id)
      - published_answers.(draft_final_answer_id, task_id) -> draft.(id, task_id)
      - published_answers.(final_gate_report_id, task_id, draft_final_answer_id)
            -> final_gate_reports.(id, task_id, draft_final_answer_id)

    For the producer endpoint under test we need only the resolution
    path (published_answers JOIN task_masters); spans / claim links are
    not required, so we keep the chain minimal.
    """
    draft_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO draft_final_answers
                        (id, task_id, version_no,
                         compiler_name, compiler_version, summary_text)
                    VALUES (:id, :t, 1,
                            'mvp0_compiler_v1', '0.1.0', :st)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": task_id,
                    "st": f"summary-{uuid.uuid4()}",
                },
            ).first()[0]
        )
    )

    gate_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO final_gate_reports
                        (id, task_id, draft_final_answer_id, decision, reason_code)
                    VALUES (:id, :t, :d, 'approved', 'all_spans_verified')
                    RETURNING id
                    """
                ),
                {"id": uuid.uuid4(), "t": task_id, "d": draft_id},
            ).first()[0]
        )
    )

    if status == "published":
        pa_id = uuid.UUID(
            str(
                conn.execute(
                    text(
                        """
                        INSERT INTO published_answers
                            (id, task_id, draft_final_answer_id, final_gate_report_id,
                             version_no, content_hash, status)
                        VALUES (:id, :t, :d, :g, 1, :h, 'published')
                        RETURNING id
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "t": task_id,
                        "d": draft_id,
                        "g": gate_id,
                        "h": _unique_hex(),
                    },
                ).first()[0]
            )
        )
    else:
        raise ValueError(f"unsupported status for this test module: {status!r}")

    return pa_id


# ---------------------------------------------------------------------------
# DB inspection helpers
# ---------------------------------------------------------------------------
def _fetch_published_answer(
    conn: Connection, *, published_answer_id: uuid.UUID
) -> dict[str, Any]:
    row = conn.execute(
        text(
            """
            SELECT status, withdrawn_at, superseded_at, superseded_by_id
            FROM published_answers
            WHERE id = :pid
            """
        ),
        {"pid": published_answer_id},
    ).one()
    return dict(row._mapping)


def _count_lifecycle_events(
    conn: Connection, *, published_answer_id: uuid.UUID
) -> int:
    return int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM published_answer_lifecycle_events
                WHERE published_answer_id = :pid
                """
            ),
            {"pid": published_answer_id},
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# FakeRedis
# ---------------------------------------------------------------------------
class FakeRedis:
    """Minimal Redis stub recording xadd calls.

    Only the surface area used by ``answers.request_published_answer_withdrawal``
    is implemented. Other Redis methods (ping, xreadgroup, ...) are not
    exposed: this stub is meant to be installed exclusively as the
    return value of ``answers_route.get_redis``, never as a global
    Redis replacement.

    When ``fail=True``, ``xadd`` raises RuntimeError to exercise the
    endpoint's error-handling path.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.xadd_calls: list[dict[str, Any]] = []

    def xadd(
        self,
        stream: str,
        fields: dict[str, str],
        maxlen: int | None = None,
        approximate: bool | None = None,
    ) -> str:
        if self.fail:
            raise RuntimeError("redis down")
        # Defensive copy of fields: tests assert against the snapshot,
        # not against an alias the endpoint might still hold.
        self.xadd_calls.append(
            {
                "stream": stream,
                "fields": dict(fields),
                "maxlen": maxlen,
                "approximate": approximate,
            }
        )
        return "1700000000000-0"


# ---------------------------------------------------------------------------
# common patch helper
# ---------------------------------------------------------------------------
def _install_fake_redis(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: bool = False,
) -> FakeRedis:
    """Patch ``answers.get_redis`` with a fresh FakeRedis instance.

    Returns the FakeRedis so tests can assert on its xadd_calls. The
    patched name lives on ``app.routes.answers``, not on
    ``app.redis``: the route module captured ``get_redis`` at import
    time via ``from ..redis import get_redis``, so the bound name must
    be replaced on the route module itself.
    """
    fake = FakeRedis(fail=fail)
    monkeypatch.setattr(answers_route, "get_redis", lambda: fake)
    return fake


def _withdrawal_url(pa_id: uuid.UUID) -> str:
    return f"/api/v1/published-answers/{pa_id}/withdrawal-requests"


# ===========================================================================
# 1 — happy path: full body, single xadd, all fields propagated correctly
# ===========================================================================
def test_withdrawal_request_happy_path_queues_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        pa_id = _create_published_answer(conn, task_id=task_id)

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    body = {
        "reason": "user requested withdrawal",
        "idempotency_key": "idem-test",
        "lifecycle_idempotency_key": "life-test",
        "requested_by": str(user_id),
        "event_payload": {"source": "test", "n": 1},
    }
    resp = client.post(_withdrawal_url(pa_id), json=body)

    # ---- HTTP envelope ----
    assert resp.status_code == 202, resp.text
    rb = resp.json()
    assert rb["status"] == "queued"
    assert rb["event_type"] == EXPECTED_EVENT_TYPE
    assert rb["published_answer_id"] == str(pa_id)
    assert rb["stream"] == EXPECTED_STREAM
    assert rb["idempotency_key"] == "idem-test"
    assert rb["lifecycle_idempotency_key"] == "life-test"
    # event_id is a UUID returned as a string by the response model.
    response_event_id = uuid.UUID(rb["event_id"])

    # ---- Redis side effect ----
    assert len(fake.xadd_calls) == 1, fake.xadd_calls
    call = fake.xadd_calls[0]
    assert call["stream"] == EXPECTED_STREAM

    fields = call["fields"]
    # All values on the wire are strings (Redis Streams contract).
    for k, v in fields.items():
        assert isinstance(k, str), f"field key not str: {k!r}"
        assert isinstance(v, str), f"field value for {k!r} not str: {v!r}"

    # Required fields on every withdrawal event.
    assert fields["event_id"] == str(response_event_id)
    assert fields["event_type"] == EXPECTED_EVENT_TYPE
    assert fields["published_answer_id"] == str(pa_id)
    assert fields["idempotency_key"] == "idem-test"
    assert fields["lifecycle_idempotency_key"] == "life-test"
    assert fields["event_reason"] == "user requested withdrawal"
    assert fields["tenant_id"] == str(tenant_id)
    assert fields["task_id"] == str(task_id)
    assert fields["project_id"] == str(project_id)

    # Optional fields, present in this scenario.
    assert fields["requested_by"] == str(user_id)
    assert fields["event_payload_json"] == json.dumps(
        body["event_payload"], separators=(",", ":"), sort_keys=True
    )


# ===========================================================================
# 2 — defaults: empty body -> generated key, default reason, no optional fields
# ===========================================================================
def test_withdrawal_request_defaults_idempotency_and_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)
        pa_id = _create_published_answer(conn, task_id=task_id)

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    # Empty body: tests the "all defaults" branch of the endpoint.
    resp = client.post(_withdrawal_url(pa_id), json={})

    assert resp.status_code == 202, resp.text
    rb = resp.json()
    assert rb["status"] == "queued"
    assert rb["event_type"] == EXPECTED_EVENT_TYPE
    assert rb["stream"] == EXPECTED_STREAM

    # Generated idempotency_key: non-empty, defaults to lifecycle key.
    generated_idem = rb["idempotency_key"]
    assert isinstance(generated_idem, str)
    assert generated_idem != ""
    assert rb["lifecycle_idempotency_key"] == generated_idem

    # Exactly one xadd, with the correct defaults on the stream.
    assert len(fake.xadd_calls) == 1
    fields = fake.xadd_calls[0]["fields"]
    assert fields["idempotency_key"] == generated_idem
    assert fields["lifecycle_idempotency_key"] == generated_idem
    assert fields["event_reason"] == EXPECTED_DEFAULT_REASON

    # Optional fields are OMITTED entirely (not set to "" or "null").
    assert "requested_by" not in fields
    assert "event_payload_json" not in fields


# ===========================================================================
# 3 — partial body: idempotency_key only, lifecycle defaults to it
# ===========================================================================
def test_withdrawal_request_uses_provided_idempotency_key_and_default_lifecycle_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)
        pa_id = _create_published_answer(conn, task_id=task_id)

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    resp = client.post(
        _withdrawal_url(pa_id),
        json={"idempotency_key": "idem-only"},
    )

    assert resp.status_code == 202, resp.text
    rb = resp.json()
    assert rb["idempotency_key"] == "idem-only"
    # Default fallback: lifecycle_idempotency_key collapses onto idempotency_key
    # when the body does not specify one.
    assert rb["lifecycle_idempotency_key"] == "idem-only"

    assert len(fake.xadd_calls) == 1
    fields = fake.xadd_calls[0]["fields"]
    assert fields["idempotency_key"] == "idem-only"
    assert fields["lifecycle_idempotency_key"] == "idem-only"
    # Reason still falls back to the API default.
    assert fields["event_reason"] == EXPECTED_DEFAULT_REASON
    # Optional fields still omitted.
    assert "requested_by" not in fields
    assert "event_payload_json" not in fields


# ===========================================================================
# 4 — 404 on unknown published_answer; no xadd
# ===========================================================================
def test_withdrawal_request_404_for_missing_published_answer_and_no_xadd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_if_db_unreachable()

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    bogus = uuid.uuid4()
    resp = client.post(_withdrawal_url(bogus), json={})

    assert resp.status_code == 404, resp.text
    err = _err(resp.json())
    assert err["code"] == "RESOURCE_NOT_FOUND"
    details = err.get("details") or {}
    assert details.get("resource") == "published_answers"
    assert details.get("id") == str(bogus)

    # The endpoint must short-circuit BEFORE any Redis interaction.
    assert fake.xadd_calls == []


# ===========================================================================
# 5 — Redis xadd raises -> 500 INTERNAL_ERROR with stream in details
# ===========================================================================
def test_withdrawal_request_redis_failure_returns_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)
        pa_id = _create_published_answer(conn, task_id=task_id)

    # FakeRedis configured to raise on xadd.
    _install_fake_redis(monkeypatch, fail=True)
    client = TestClient(app)

    resp = client.post(_withdrawal_url(pa_id), json={})

    assert resp.status_code == 500, resp.text
    err = _err(resp.json())
    assert err["code"] == "INTERNAL_ERROR"
    details = err.get("details") or {}
    assert details.get("stream") == EXPECTED_STREAM


# ===========================================================================
# 6 — endpoint does NOT mutate published_answers or write lifecycle rows
# ===========================================================================
def test_withdrawal_request_does_not_mutate_published_answer_or_lifecycle_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)
        pa_id = _create_published_answer(conn, task_id=task_id)

    # Snapshot BEFORE the POST.
    with engine.connect() as conn:
        pa_before = _fetch_published_answer(conn, published_answer_id=pa_id)
        events_before = _count_lifecycle_events(conn, published_answer_id=pa_id)
    assert pa_before["status"] == "published"
    assert pa_before["withdrawn_at"] is None
    assert pa_before["superseded_at"] is None
    assert pa_before["superseded_by_id"] is None
    assert events_before == 0

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    resp = client.post(_withdrawal_url(pa_id), json={"reason": "no mutation test"})
    assert resp.status_code == 202, resp.text
    assert len(fake.xadd_calls) == 1  # XADD happened, but DB must be untouched.

    # Snapshot AFTER the POST: every observable on published_answers is
    # exactly as it was, and the lifecycle log is still empty for this row.
    with engine.connect() as conn:
        pa_after = _fetch_published_answer(conn, published_answer_id=pa_id)
        events_after = _count_lifecycle_events(conn, published_answer_id=pa_id)

    assert pa_after["status"] == "published"
    assert pa_after["withdrawn_at"] is None
    assert pa_after["superseded_at"] is None
    assert pa_after["superseded_by_id"] is None
    assert events_after == 0


# ===========================================================================
# 7 — event_payload_json is compact + sort_keys
# ===========================================================================
def test_withdrawal_request_event_payload_json_is_compact_sorted_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)
        pa_id = _create_published_answer(conn, task_id=task_id)

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    # Intentionally insert keys in non-sorted order so that a producer
    # that did NOT sort would emit '{"z":2,"a":1}'.
    resp = client.post(
        _withdrawal_url(pa_id),
        json={"event_payload": {"z": 2, "a": 1}},
    )
    assert resp.status_code == 202, resp.text

    assert len(fake.xadd_calls) == 1
    fields = fake.xadd_calls[0]["fields"]
    # Compact (no spaces around separators) AND sorted keys.
    assert fields["event_payload_json"] == '{"a":1,"z":2}'


# ===========================================================================
# 8 — invalid requested_by -> validation error, no xadd
# ===========================================================================
def test_withdrawal_request_rejects_invalid_requested_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)
        pa_id = _create_published_answer(conn, task_id=task_id)

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    resp = client.post(
        _withdrawal_url(pa_id),
        json={"requested_by": "not-a-uuid"},
    )

    # The repo wires a normalized handler for RequestValidationError that
    # returns 400 VALIDATION_ERROR (see install_normalized_error_handler
    # in evidencefirst_shared/errors.py). We accept 422 as well in case
    # the handler wiring is bypassed in some future configuration; in
    # both cases the body must NOT have produced a Redis publish.
    assert resp.status_code in (400, 422), resp.text
    if resp.status_code == 400:
        err = _err(resp.json())
        assert err["code"] == "VALIDATION_ERROR"

    # No xadd happened: Pydantic validation runs BEFORE the route body.
    assert fake.xadd_calls == []


# ===========================================================================
# 9 — empty strings on optional fields default correctly
# ===========================================================================
def test_withdrawal_request_empty_strings_default_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)
        pa_id = _create_published_answer(conn, task_id=task_id)

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    # All three optional string fields explicitly set to "" — the
    # endpoint treats empty strings as "absent" and applies the
    # documented defaults.
    resp = client.post(
        _withdrawal_url(pa_id),
        json={
            "reason": "",
            "idempotency_key": "",
            "lifecycle_idempotency_key": "",
        },
    )

    assert resp.status_code == 202, resp.text
    rb = resp.json()

    generated_idem = rb["idempotency_key"]
    assert isinstance(generated_idem, str)
    assert generated_idem != ""
    assert rb["lifecycle_idempotency_key"] == generated_idem

    assert len(fake.xadd_calls) == 1
    fields = fake.xadd_calls[0]["fields"]
    assert fields["idempotency_key"] == generated_idem
    assert fields["lifecycle_idempotency_key"] == generated_idem
    assert fields["event_reason"] == EXPECTED_DEFAULT_REASON
    # No optional fields slipped in via empty strings.
    assert "requested_by" not in fields
    assert "event_payload_json" not in fields
