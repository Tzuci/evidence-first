"""API tests for the published_answer lifecycle events read endpoint
(Phase 8.6A).

Endpoint exercised:
  GET /api/v1/published-answers/{published_answer_id}/lifecycle-events

Coverage map (7 scenarios required by the block prompt):

  1. test_lifecycle_events_happy_path_two_events_asc_ordering
  2. test_lifecycle_events_filter_event_type_returns_only_matching
  3. test_lifecycle_events_no_events_returns_empty_items
  4. test_lifecycle_events_404_for_missing_published_answer
  5. test_lifecycle_events_limit_truncates_response
  6. test_lifecycle_events_invalid_event_type_returns_validation_error
  7. test_lifecycle_events_endpoint_is_read_only

Design notes:
  - This file lives under apps/api/tests/. The Python package ``app``
    resolves to apps/api/app, so ``from app.main import app`` and
    ``from app.db import get_engine`` are the canonical imports — same
    pattern used by test_answers_endpoints.py and
    test_published_answer_withdrawal_request.py.
  - We do NOT touch Redis: the endpoint is strictly read-only and does
    not call ``get_redis()``. No FakeRedis is needed.
  - We do NOT import any worker code: this is a pure API test module.
  - We seed directly into the DB the minimal rows needed for each
    scenario (no consumer, no compiler, no gate, no propagator).
  - The lifecycle table is APPEND-ONLY via trigger, but the trigger
    rejects UPDATE / DELETE only — plain INSERTs are allowed and are
    exactly how the worker writes rows in production. Seeding via
    INSERT is therefore legitimate.
  - All identifiers / hashes / idempotency keys are unique per
    invocation (rerun-safe).
"""
from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db import get_engine
from app.main import app


# ---------------------------------------------------------------------------
# environment guard
# ---------------------------------------------------------------------------
def _skip_if_db_unreachable() -> None:
    """Skip the test if Postgres is not reachable.

    Mirrors the gating used by other 8.5 API test modules: the endpoint
    needs a real DB to seed the published_answer chain and to read
    lifecycle_events rows back. No Redis is required for this endpoint.
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


def _endpoint(published_answer_id: uuid.UUID) -> str:
    return f"/api/v1/published-answers/{published_answer_id}/lifecycle-events"


# ---------------------------------------------------------------------------
# DB seeding helpers (no consumer, no Redis, no worker modules)
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
                {"t": tenant_id, "n": f"lifecycle-events-test-{uuid.uuid4()}"},
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
) -> uuid.UUID:
    """Build the minimal 8.4 chain so a published_answer exists for the
    given task: draft v1 -> approved gate -> published v1.

    The FK chain is locked down by the composite constraints declared in
    0005_answers_gate.sql; spans / claim links are not required for the
    lifecycle events endpoint, so we keep the chain minimal.

    Returns the published_answer id.
    """
    summary_text = f"summary-{uuid.uuid4()}\n"

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
                {"id": uuid.uuid4(), "t": task_id, "st": summary_text},
            ).first()[0]
        )
    )

    gate_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO final_gate_reports
                        (id, task_id, draft_final_answer_id,
                         decision, reason_code)
                    VALUES (:id, :t, :d, 'approved', 'all_spans_verified')
                    RETURNING id
                    """
                ),
                {"id": uuid.uuid4(), "t": task_id, "d": draft_id},
            ).first()[0]
        )
    )

    content_hash = hashlib.sha256(summary_text.encode("utf-8")).hexdigest()
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
                    "h": content_hash,
                },
            ).first()[0]
        )
    )
    return pa_id


def _insert_lifecycle_event(
    conn: Connection,
    *,
    published_answer_id: uuid.UUID,
    task_id: uuid.UUID,
    event_type: str,
    event_reason: str,
    idempotency_key: str,
    requested_by: uuid.UUID | None = None,
    event_payload: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Insert a single row into published_answer_lifecycle_events.

    The table is APPEND-ONLY via the shared ``reject_modify_append_only``
    trigger, which only blocks UPDATE / DELETE — plain INSERTs are the
    normal write path (it is exactly how the worker writes rows in
    production). The composite FK
    (published_answer_id, task_id) -> published_answers(id, task_id)
    means callers must pass a consistent (pa_id, task_id) pair.
    """
    payload = event_payload if event_payload is not None else {}
    # Use SQLAlchemy parameter binding for the JSONB payload via the
    # CAST trick (the same idiom used elsewhere in the API test suite):
    # we serialize the dict in Python and let psycopg parse it as JSONB
    # server-side.
    import json as _json

    row = conn.execute(
        text(
            """
            INSERT INTO published_answer_lifecycle_events
                (id, published_answer_id, task_id,
                 event_type, event_reason, event_payload,
                 requested_by, idempotency_key)
            VALUES
                (:id, :pid, :tid,
                 :et, :er, CAST(:ep AS JSONB),
                 :rb, :ik)
            RETURNING id
            """
        ),
        {
            "id": uuid.uuid4(),
            "pid": published_answer_id,
            "tid": task_id,
            "et": event_type,
            "er": event_reason,
            "ep": _json.dumps(payload),
            "rb": requested_by,
            "ik": idempotency_key,
        },
    ).first()
    return uuid.UUID(str(row[0]))


# ---------------------------------------------------------------------------
# DB inspection helpers
# ---------------------------------------------------------------------------
# Whitelist of tables we are willing to count via _count_table.
# Hardcoded to avoid any SQL injection vector even though this is test
# code: the table name is interpolated into the query, but only from
# this fixed set. Mirrors the pattern adopted by
# test_source_loss_endpoint.py::_count_table.
_COUNTABLE_TABLES = frozenset(
    {
        "published_answer_lifecycle_events",
        "published_answers",
        "source_loss_events",
        "source_loss_propagation_records",
        "claim_ledger_entries",
        "claim_lineage",
        "audit_records",
    }
)


def _count_table(conn: Connection, table_name: str) -> int:
    """Return a global COUNT(*) of the named table.

    Only accepts a hardcoded whitelist of table names to keep the SQL
    construction safe.
    """
    if table_name not in _COUNTABLE_TABLES:
        raise ValueError(f"refusing to count unknown table: {table_name!r}")
    return int(
        conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
    )


def _snapshot_all_counts(conn: Connection) -> dict[str, int]:
    return {t: _count_table(conn, t) for t in _COUNTABLE_TABLES}


def _fetch_published_answer_status(
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


# ===========================================================================
# 1 — happy path: two events, ASC ordering, full PublishedAnswerLifecycleEventRead fields
# ===========================================================================
def test_lifecycle_events_happy_path_two_events_asc_ordering() -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, user_id, task_id = _seeded_dev(conn)
        pa_id = _create_published_answer(conn, task_id=task_id)

    # Seed the two events in separate transactions so that NOW() yields
    # distinct created_at values. This is the same approach the worker
    # uses in production (two service-level inserts) and makes the
    # ASC ordering test robust against same-microsecond timestamps.
    idem_req = f"idem-req-{_unique_hex()}"
    idem_done = f"idem-done-{_unique_hex()}"

    with engine.begin() as conn:
        first_event_id = _insert_lifecycle_event(
            conn,
            published_answer_id=pa_id,
            task_id=task_id,
            event_type="withdrawal_requested",
            event_reason="user requested withdrawal",
            idempotency_key=idem_req,
            requested_by=user_id,
            event_payload={"source": "test", "step": 1},
        )
    with engine.begin() as conn:
        second_event_id = _insert_lifecycle_event(
            conn,
            published_answer_id=pa_id,
            task_id=task_id,
            event_type="withdrawn",
            event_reason="apply_withdrawal succeeded",
            idempotency_key=idem_done,
            requested_by=None,
            event_payload={"step": 2},
        )

    client = TestClient(app)
    resp = client.get(_endpoint(pa_id))
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["published_answer_id"] == str(pa_id)
    items = body["items"]
    assert isinstance(items, list)
    assert len(items) == 2

    # ASC ordering on (created_at, id): the first inserted row is first.
    assert items[0]["id"] == str(first_event_id)
    assert items[1]["id"] == str(second_event_id)

    # Field-level coherence with PublishedAnswerLifecycleEventRead.
    first, second = items
    for it in (first, second):
        # Every field declared on the shared schema must be present in
        # the serialized payload (model_dump(mode="json")).
        for f in (
            "id",
            "published_answer_id",
            "task_id",
            "event_type",
            "event_reason",
            "event_payload",
            "requested_by",
            "idempotency_key",
            "created_at",
        ):
            assert f in it, f"missing field {f!r} in item: {it!r}"
        assert it["published_answer_id"] == str(pa_id)
        assert it["task_id"] == str(task_id)
        assert isinstance(it["event_payload"], dict)

    assert first["event_type"] == "withdrawal_requested"
    assert first["event_reason"] == "user requested withdrawal"
    assert first["idempotency_key"] == idem_req
    assert first["requested_by"] == str(user_id)
    assert first["event_payload"] == {"source": "test", "step": 1}

    assert second["event_type"] == "withdrawn"
    assert second["event_reason"] == "apply_withdrawal succeeded"
    assert second["idempotency_key"] == idem_done
    assert second["requested_by"] is None
    assert second["event_payload"] == {"step": 2}


# ===========================================================================
# 2 — filter event_type=withdrawn returns only the matching row
# ===========================================================================
def test_lifecycle_events_filter_event_type_returns_only_matching() -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)
        pa_id = _create_published_answer(conn, task_id=task_id)

    idem_req = f"filter-req-{_unique_hex()}"
    idem_done = f"filter-done-{_unique_hex()}"

    with engine.begin() as conn:
        _insert_lifecycle_event(
            conn,
            published_answer_id=pa_id,
            task_id=task_id,
            event_type="withdrawal_requested",
            event_reason="reason-1",
            idempotency_key=idem_req,
        )
    with engine.begin() as conn:
        withdrawn_id = _insert_lifecycle_event(
            conn,
            published_answer_id=pa_id,
            task_id=task_id,
            event_type="withdrawn",
            event_reason="reason-2",
            idempotency_key=idem_done,
        )

    client = TestClient(app)

    # Without the filter we see both.
    full = client.get(_endpoint(pa_id))
    assert full.status_code == 200
    assert len(full.json()["items"]) == 2

    # With event_type=withdrawn, only the matching row.
    filtered = client.get(_endpoint(pa_id), params={"event_type": "withdrawn"})
    assert filtered.status_code == 200, filtered.text
    body = filtered.json()
    assert body["published_answer_id"] == str(pa_id)
    items = body["items"]
    assert len(items) == 1
    assert items[0]["id"] == str(withdrawn_id)
    assert items[0]["event_type"] == "withdrawn"


# ===========================================================================
# 3 — published_answer exists but no lifecycle rows -> 200 items=[]
# ===========================================================================
def test_lifecycle_events_no_events_returns_empty_items() -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)
        pa_id = _create_published_answer(conn, task_id=task_id)

    client = TestClient(app)
    resp = client.get(_endpoint(pa_id))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["published_answer_id"] == str(pa_id)
    assert body["items"] == []


# ===========================================================================
# 4 — unknown published_answer -> 404 RESOURCE_NOT_FOUND with full details
# ===========================================================================
def test_lifecycle_events_404_for_missing_published_answer() -> None:
    _skip_if_db_unreachable()

    client = TestClient(app)
    bogus = uuid.uuid4()
    resp = client.get(_endpoint(bogus))

    assert resp.status_code == 404, resp.text
    err = _err(resp.json())
    assert err["code"] == "RESOURCE_NOT_FOUND"
    details = err.get("details") or {}
    assert details.get("resource") == "published_answers"
    assert details.get("id") == str(bogus)


# ===========================================================================
# 5 — limit=1 truncates the response to one item
# ===========================================================================
def test_lifecycle_events_limit_truncates_response() -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)
        pa_id = _create_published_answer(conn, task_id=task_id)

    idem_a = f"limit-a-{_unique_hex()}"
    idem_b = f"limit-b-{_unique_hex()}"
    with engine.begin() as conn:
        first_id = _insert_lifecycle_event(
            conn,
            published_answer_id=pa_id,
            task_id=task_id,
            event_type="withdrawal_requested",
            event_reason="r1",
            idempotency_key=idem_a,
        )
    with engine.begin() as conn:
        _insert_lifecycle_event(
            conn,
            published_answer_id=pa_id,
            task_id=task_id,
            event_type="withdrawn",
            event_reason="r2",
            idempotency_key=idem_b,
        )

    client = TestClient(app)
    resp = client.get(_endpoint(pa_id), params={"limit": 1})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["items"]
    assert len(items) == 1
    # ASC ordering -> the first inserted event is the one returned.
    assert items[0]["id"] == str(first_id)


# ===========================================================================
# 6 — invalid event_type rejected by validation (400 or 422)
# ===========================================================================
def test_lifecycle_events_invalid_event_type_returns_validation_error() -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)
        pa_id = _create_published_answer(conn, task_id=task_id)

    client = TestClient(app)
    resp = client.get(_endpoint(pa_id), params={"event_type": "not_a_real_kind"})

    # The repo wires a normalized handler for RequestValidationError
    # that returns 400 VALIDATION_ERROR (see
    # install_normalized_error_handler in
    # evidencefirst_shared/errors.py). We accept 422 as well in case
    # the handler wiring is bypassed in some future configuration; in
    # both cases the route body must NOT have executed and no row
    # mutation may have occurred (validated implicitly by the read-only
    # invariant test below).
    assert resp.status_code in (400, 422), resp.text
    if resp.status_code == 400:
        err = _err(resp.json())
        assert err["code"] == "VALIDATION_ERROR"


# ===========================================================================
# 7 — read-only invariant: no count drift on any 8.5 / 8.4 / audit table,
#     and published_answers.status unchanged
# ===========================================================================
def test_lifecycle_events_endpoint_is_read_only() -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)
        pa_id = _create_published_answer(conn, task_id=task_id)

    # Seed one lifecycle event so the GET has data to read; the test is
    # about the GET not mutating anything, not about the seed.
    idem = f"readonly-{_unique_hex()}"
    with engine.begin() as conn:
        _insert_lifecycle_event(
            conn,
            published_answer_id=pa_id,
            task_id=task_id,
            event_type="withdrawal_requested",
            event_reason="r1",
            idempotency_key=idem,
        )

    # Snapshot BEFORE the GET.
    with engine.connect() as conn:
        before = _snapshot_all_counts(conn)
        pa_before = _fetch_published_answer_status(conn, published_answer_id=pa_id)
    assert pa_before["status"] == "published"
    assert pa_before["withdrawn_at"] is None
    assert pa_before["superseded_at"] is None
    assert pa_before["superseded_by_id"] is None

    # Hit the endpoint a few times, including a filter variant.
    client = TestClient(app)
    r1 = client.get(_endpoint(pa_id))
    assert r1.status_code == 200, r1.text
    r2 = client.get(_endpoint(pa_id), params={"event_type": "withdrawn"})
    assert r2.status_code == 200, r2.text
    r3 = client.get(_endpoint(pa_id), params={"limit": 5})
    assert r3.status_code == 200, r3.text

    # Snapshot AFTER the GETs: every counter must be identical, and the
    # published_answer row must be byte-for-byte the same.
    with engine.connect() as conn:
        after = _snapshot_all_counts(conn)
        pa_after = _fetch_published_answer_status(conn, published_answer_id=pa_id)

    assert after == before, (
        "row counts drifted after read-only GETs; "
        f"before={before!r}, after={after!r}"
    )
    assert pa_after == pa_before, (
        "published_answers row mutated after read-only GETs; "
        f"before={pa_before!r}, after={pa_after!r}"
    )
