"""Worker-level tests for apps/worker/app/services/published_answer_lifecycle.py
(Phase 8.5 — Block 2A).

Coverage:
  1. apply_withdrawal: published -> withdrawn
       - lifecycle events 'withdrawal_requested' and 'withdrawn' present;
       - published_answers.status = 'withdrawn', withdrawn_at NOT NULL;
       - audit 'published_answer.withdrawn' emitted once;
       - verify_task_audit_chain ok=True.
  2. apply_withdrawal: idempotency under redelivery with the SAME idempotency_key
       - lifecycle events count remains 2;
       - audit 'published_answer.withdrawn' count remains 1;
       - withdrawn_at unchanged after the second call.
  3. apply_withdrawal on already-withdrawn published_answer
       - returns {"status": "already_withdrawn", ...};
       - no new lifecycle event is created;
       - no new 'published_answer.withdrawn' audit is emitted.
  4. apply_withdrawal on superseded published_answer
       - returns {"status": "already_superseded", ...};
       - no new audit is emitted.
  5. apply_withdrawal on unknown published_answer_id
       - returns {"status": "not_found", ...}.

Design notes:
  - This file lives under apps/worker/tests/. The Python package `app`
    resolves to the worker app (apps/worker/app), so we can freely import
    `app.services.published_answer_lifecycle` and `app.db`.
  - All identifiers/hashes are unique per invocation (rerun-safe).
  - We do NOT exercise the full 8.4 pipeline (no compiler, no gate, no
    consumer). Instead, we directly seed the rows required by the Block 2A
    contract: task_masters + draft_final_answers + final_gate_reports +
    published_answers. The composite FKs declared in 0005 keep the data
    referentially consistent with no extra effort.
  - verify_task_audit_chain expects a Connection, not an Engine. Always
    wrap the call in a `with engine.connect() as conn:` block.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db import get_engine
from app.services.published_answer_lifecycle import apply_withdrawal
from evidencefirst_shared.db.audit import verify_task_audit_chain


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _unique_hex() -> str:
    return hashlib.sha256(uuid.uuid4().bytes + uuid.uuid4().bytes).hexdigest()


def _seeded_dev(conn: Connection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
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
                {"t": tenant_id, "n": f"lifecycle-svc-test-{uuid.uuid4()}"},
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
    initial_status: str = "published",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create draft_final_answers + final_gate_reports + published_answers v1
    for the given task. Returns (draft_id, gate_report_id, published_answer_id).

    The initial status is set directly on the published_answers row. For
    'withdrawn' we also set withdrawn_at to NOW(); for 'superseded' we set
    superseded_at to NOW(). This is test scaffolding to bring the row into a
    given lifecycle state without running the full 8.4 pipeline.
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

    if initial_status == "published":
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
    elif initial_status == "withdrawn":
        pa_id = uuid.UUID(
            str(
                conn.execute(
                    text(
                        """
                        INSERT INTO published_answers
                            (id, task_id, draft_final_answer_id, final_gate_report_id,
                             version_no, content_hash, status, withdrawn_at)
                        VALUES (:id, :t, :d, :g, 1, :h, 'withdrawn', NOW())
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
    elif initial_status == "superseded":
        pa_id = uuid.UUID(
            str(
                conn.execute(
                    text(
                        """
                        INSERT INTO published_answers
                            (id, task_id, draft_final_answer_id, final_gate_report_id,
                             version_no, content_hash, status, superseded_at)
                        VALUES (:id, :t, :d, :g, 1, :h, 'superseded', NOW())
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
        raise ValueError(f"unsupported initial_status: {initial_status!r}")

    return draft_id, gate_id, pa_id


def _count_lifecycle_events(
    conn: Connection,
    *,
    published_answer_id: uuid.UUID,
    event_type: str | None = None,
) -> int:
    if event_type is None:
        result = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM published_answer_lifecycle_events
                WHERE published_answer_id = :pid
                """
            ),
            {"pid": published_answer_id},
        ).scalar_one()
    else:
        result = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM published_answer_lifecycle_events
                WHERE published_answer_id = :pid AND event_type = :etype
                """
            ),
            {"pid": published_answer_id, "etype": event_type},
        ).scalar_one()
    return int(result)


def _count_audit_event(
    conn: Connection, *, task_id: uuid.UUID, event_type: str
) -> int:
    return int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM audit_records
                WHERE chain_scope = 'task'
                  AND scope_id    = :t
                  AND event_type  = :etype
                """
            ),
            {"t": task_id, "etype": event_type},
        ).scalar_one()
    )


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


# ---------------------------------------------------------------------------
# Test 1 — apply_withdrawal marks published_answer as withdrawn
# ---------------------------------------------------------------------------
def test_apply_withdrawal_marks_published_answer_withdrawn():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        _draft, _gate, pa_id = _create_published_answer(
            conn, task_id=task_id, initial_status="published"
        )

    idem = _unique_hex()
    with engine.begin() as conn:
        result = apply_withdrawal(
            conn,
            published_answer_id=pa_id,
            event_reason="user requested withdrawal",
            idempotency_key=idem,
            requested_by=user_id,
            event_payload={"source": "test_block_2a"},
        )

    assert result["status"] == "withdrawn"
    assert result["published_answer_id"] == str(pa_id)
    assert result["task_id"] == str(task_id)
    assert result["transitioned"] is True

    # Inspect post-conditions in a fresh connection (the service committed via
    # the engine.begin() context manager above).
    with engine.connect() as conn:
        pa = _fetch_published_answer(conn, published_answer_id=pa_id)
        assert pa["status"] == "withdrawn"
        assert pa["withdrawn_at"] is not None
        assert pa["superseded_at"] is None
        assert pa["superseded_by_id"] is None

        assert _count_lifecycle_events(
            conn, published_answer_id=pa_id, event_type="withdrawal_requested"
        ) == 1
        assert _count_lifecycle_events(
            conn, published_answer_id=pa_id, event_type="withdrawn"
        ) == 1

        assert _count_audit_event(
            conn, task_id=task_id, event_type="published_answer.withdrawn"
        ) == 1

        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True


# ---------------------------------------------------------------------------
# Test 2 — apply_withdrawal is idempotent under redelivery with the SAME key
# ---------------------------------------------------------------------------
def test_apply_withdrawal_is_idempotent_for_same_key():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        _draft, _gate, pa_id = _create_published_answer(
            conn, task_id=task_id, initial_status="published"
        )

    idem = _unique_hex()

    # First call: real transition.
    with engine.begin() as conn:
        result_1 = apply_withdrawal(
            conn,
            published_answer_id=pa_id,
            event_reason="first call",
            idempotency_key=idem,
            requested_by=None,
        )
    assert result_1["status"] == "withdrawn"
    assert result_1["transitioned"] is True

    with engine.connect() as conn:
        pa_after_1 = _fetch_published_answer(conn, published_answer_id=pa_id)
    withdrawn_at_first = pa_after_1["withdrawn_at"]
    assert withdrawn_at_first is not None

    # Second call: same idempotency_key. The published_answer is already
    # 'withdrawn', so the service short-circuits to 'already_withdrawn' and
    # MUST NOT re-emit any lifecycle event or audit row.
    with engine.begin() as conn:
        result_2 = apply_withdrawal(
            conn,
            published_answer_id=pa_id,
            event_reason="second call",
            idempotency_key=idem,
            requested_by=None,
        )
    assert result_2["status"] == "already_withdrawn"

    with engine.connect() as conn:
        pa_after_2 = _fetch_published_answer(conn, published_answer_id=pa_id)
        # withdrawn_at must be unchanged after the second call.
        assert pa_after_2["withdrawn_at"] == withdrawn_at_first

        # Lifecycle events: exactly one withdrawal_requested + one withdrawn,
        # both inserted by the first call.
        assert _count_lifecycle_events(
            conn, published_answer_id=pa_id, event_type="withdrawal_requested"
        ) == 1
        assert _count_lifecycle_events(
            conn, published_answer_id=pa_id, event_type="withdrawn"
        ) == 1
        assert _count_lifecycle_events(conn, published_answer_id=pa_id) == 2

        # Audit: exactly one 'published_answer.withdrawn' from the first call.
        assert _count_audit_event(
            conn, task_id=task_id, event_type="published_answer.withdrawn"
        ) == 1

        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True


# ---------------------------------------------------------------------------
# Test 3 — apply_withdrawal on already-withdrawn is a no-op
# ---------------------------------------------------------------------------
def test_apply_withdrawal_on_already_withdrawn_is_noop():
    """The service does NOT insert lifecycle events when the published_answer
    is already 'withdrawn' before this call (initial state set in test setup,
    not by a previous apply_withdrawal). This is the conservative default
    documented in the service docstring: we do not invent a withdrawal history
    for a row whose state was not driven by the current idempotency_key.
    """
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        _draft, _gate, pa_id = _create_published_answer(
            conn, task_id=task_id, initial_status="withdrawn"
        )

    with engine.connect() as conn:
        events_before = _count_lifecycle_events(conn, published_answer_id=pa_id)
        audit_before = _count_audit_event(
            conn, task_id=task_id, event_type="published_answer.withdrawn"
        )
        pa_before = _fetch_published_answer(conn, published_answer_id=pa_id)

    with engine.begin() as conn:
        result = apply_withdrawal(
            conn,
            published_answer_id=pa_id,
            event_reason="late request on withdrawn",
            idempotency_key=_unique_hex(),
            requested_by=None,
        )

    assert result["status"] == "already_withdrawn"
    assert result["published_answer_id"] == str(pa_id)
    assert result["task_id"] == str(task_id)

    with engine.connect() as conn:
        events_after = _count_lifecycle_events(conn, published_answer_id=pa_id)
        audit_after = _count_audit_event(
            conn, task_id=task_id, event_type="published_answer.withdrawn"
        )
        pa_after = _fetch_published_answer(conn, published_answer_id=pa_id)

        assert events_after == events_before
        assert audit_after == audit_before
        assert pa_after["status"] == "withdrawn"
        # withdrawn_at must not be touched by the no-op call.
        assert pa_after["withdrawn_at"] == pa_before["withdrawn_at"]


# ---------------------------------------------------------------------------
# Test 4 — apply_withdrawal on superseded is a no-op
# ---------------------------------------------------------------------------
def test_apply_withdrawal_on_superseded_is_noop():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        _draft, _gate, pa_id = _create_published_answer(
            conn, task_id=task_id, initial_status="superseded"
        )

    with engine.connect() as conn:
        events_before = _count_lifecycle_events(conn, published_answer_id=pa_id)
        audit_before = _count_audit_event(
            conn, task_id=task_id, event_type="published_answer.withdrawn"
        )
        pa_before = _fetch_published_answer(conn, published_answer_id=pa_id)

    with engine.begin() as conn:
        result = apply_withdrawal(
            conn,
            published_answer_id=pa_id,
            event_reason="late request on superseded",
            idempotency_key=_unique_hex(),
            requested_by=None,
        )

    assert result["status"] == "already_superseded"
    assert result["published_answer_id"] == str(pa_id)
    assert result["task_id"] == str(task_id)

    with engine.connect() as conn:
        events_after = _count_lifecycle_events(conn, published_answer_id=pa_id)
        audit_after = _count_audit_event(
            conn, task_id=task_id, event_type="published_answer.withdrawn"
        )
        pa_after = _fetch_published_answer(conn, published_answer_id=pa_id)

        assert events_after == events_before
        assert audit_after == audit_before
        assert pa_after["status"] == "superseded"
        assert pa_after["superseded_at"] == pa_before["superseded_at"]


# ---------------------------------------------------------------------------
# Test 5 — apply_withdrawal on unknown id returns not_found
# ---------------------------------------------------------------------------
def test_apply_withdrawal_not_found():
    engine = get_engine()
    bogus_id = uuid.uuid4()

    with engine.begin() as conn:
        result = apply_withdrawal(
            conn,
            published_answer_id=bogus_id,
            event_reason="should not match",
            idempotency_key=_unique_hex(),
            requested_by=None,
        )

    assert result == {
        "status": "not_found",
        "published_answer_id": str(bogus_id),
    }
