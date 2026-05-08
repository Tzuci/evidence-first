"""Worker-level tests for apps/worker/app/consumers/published_answer_withdrawal.py
(Phase 8.5 — Block 3A-2).

Coverage map (all 10 scenarios required by the block prompt):

  1. test_withdrawal_consumer_happy_path
  2. test_withdrawal_consumer_redelivery_same_consumer_key_skips
  3. test_withdrawal_consumer_different_consumer_key_same_lifecycle_key_is_service_idempotent
  4. test_withdrawal_consumer_missing_published_answer_with_tenant_records_failed_epr
  5. test_withdrawal_consumer_missing_published_answer_without_tenant_writes_no_epr
  6. test_withdrawal_consumer_malformed_required_field_writes_no_epr
  7. test_withdrawal_consumer_bad_event_type_writes_no_epr
  8. test_withdrawal_consumer_bad_requested_by_writes_no_epr  (verifies the
     hard-fail fix: a syntactically invalid requested_by must short-circuit
     before any DB write — no EPR row, no transaction.)
  9. test_withdrawal_consumer_already_withdrawn_noop_succeeds
 10. test_withdrawal_consumer_already_superseded_noop_succeeds

Design notes:
  - This file lives under apps/worker/tests/. The Python package `app`
    resolves to apps/worker/app, so we can import the consumer entry point
    and the worker DB helper directly without any sys.path tweaking.
  - We DO NOT call apply_withdrawal directly. The contract under test is
    the consumer handler (handle_published_answer_withdrawal) end-to-end:
    its event parsing rules, its EPR bookkeeping, and its delegation to
    the lifecycle service.
  - We DO NOT spin up Redis or the worker loop. The handler is invoked
    with a plain Python dict (the same shape produced by the Redis decoder
    in apps/worker/app/main.py, but with native types — the consumer
    accepts both forms).
  - We DO NOT seed published_answer_lifecycle_events manually. Every
    lifecycle row observed by these tests is produced by the service via
    the consumer call.
  - All identifiers / hashes / idempotency keys are unique per invocation
    (rerun-safe).
  - verify_task_audit_chain expects a Connection, not an Engine; we
    always wrap the call in `with engine.connect() as conn:`.
  - The consumer_name used in tests is a stable, logical identifier
    (`test_published_answer_withdrawal`), not `worker_1`.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.consumers.published_answer_withdrawal import (
    handle_published_answer_withdrawal,
)
from app.db import get_engine
from evidencefirst_shared.db.audit import verify_task_audit_chain


CONSUMER_NAME = "test_published_answer_withdrawal"

EVENT_TYPE = "published_answer.withdrawal_requested"

AUDIT_EVENT_WITHDRAWN = "published_answer.withdrawn"


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------
def _unique_hex() -> str:
    """Return a rerun-safe sha256 hex string unique per call."""
    return hashlib.sha256(uuid.uuid4().bytes + uuid.uuid4().bytes).hexdigest()


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
                {"t": tenant_id, "n": f"withdraw-consumer-test-{uuid.uuid4()}"},
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

    initial_status governs the lifecycle state of the published_answers row:
      - 'published'  -> normal happy-path setup; lifecycle untouched.
      - 'withdrawn'  -> withdrawn_at is set to NOW(); the consumer should
                        then short-circuit via apply_withdrawal's
                        'already_withdrawn' branch.
      - 'superseded' -> superseded_at is set to NOW(); the consumer should
                        short-circuit via 'already_superseded'.

    The composite UNIQUE / FK declared in 0005 keep referential integrity
    intact across the chain task -> draft -> gate -> published.
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


def _event_for(
    pa_id: uuid.UUID | str,
    *,
    event_id: uuid.UUID | str | None = None,
    idempotency_key: str | None = None,
    lifecycle_idempotency_key: str | None = None,
    tenant_id: uuid.UUID | str | None = None,
    requested_by: uuid.UUID | str | None = None,
    event_reason: str = "test withdrawal",
    event_type: str = EVENT_TYPE,
) -> dict[str, Any]:
    """Build a well-shaped withdrawal event for the consumer.

    The handler accepts both native (uuid.UUID) and string-encoded UUID
    fields, mirroring the Redis-decoded event shape produced by main.py.
    Tests use native types for clarity and only switch to strings when
    they explicitly want to validate the malformed-event branches.

    Optional fields default to None, which the consumer correctly treats
    as "absent" rather than "empty string".
    """
    payload: dict[str, Any] = {
        "event_id": event_id if event_id is not None else uuid.uuid4(),
        "event_type": event_type,
        "published_answer_id": pa_id,
        "idempotency_key": idempotency_key if idempotency_key is not None else _unique_hex(),
        "event_reason": event_reason,
    }
    if lifecycle_idempotency_key is not None:
        payload["lifecycle_idempotency_key"] = lifecycle_idempotency_key
    if tenant_id is not None:
        payload["tenant_id"] = tenant_id
    if requested_by is not None:
        payload["requested_by"] = requested_by
    return payload


# ---------------------------------------------------------------------------
# count / fetch helpers
# ---------------------------------------------------------------------------
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


def _count_epr(
    conn: Connection, *, consumer_name: str, idempotency_key: str
) -> int:
    return int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM event_processing_records
                WHERE consumer_name = :c AND idempotency_key = :k
                """
            ),
            {"c": consumer_name, "k": idempotency_key},
        ).scalar_one()
    )


def _fetch_epr(
    conn: Connection, *, consumer_name: str, idempotency_key: str
) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """
            SELECT id, processing_status, error_code, error_message,
                   tenant_id, project_id, task_id
            FROM event_processing_records
            WHERE consumer_name = :c AND idempotency_key = :k
            LIMIT 1
            """
        ),
        {"c": consumer_name, "k": idempotency_key},
    ).first()
    if row is None:
        return None
    m = row._mapping
    return {
        "id": uuid.UUID(str(m["id"])),
        "processing_status": str(m["processing_status"]),
        "error_code": m["error_code"],
        "error_message": m["error_message"],
        "tenant_id": uuid.UUID(str(m["tenant_id"])) if m["tenant_id"] is not None else None,
        "project_id": uuid.UUID(str(m["project_id"])) if m["project_id"] is not None else None,
        "task_id": uuid.UUID(str(m["task_id"])) if m["task_id"] is not None else None,
    }


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


def _fetch_task_status(conn: Connection, *, task_id: uuid.UUID) -> str:
    """task_masters.status MUST stay invariant across withdrawal: the
    lifecycle lives on published_answers, not on the task. We assert this
    explicitly in the happy-path test to lock the invariant down.
    """
    return str(
        conn.execute(
            text("SELECT status FROM task_masters WHERE id = :t"),
            {"t": task_id},
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# 1 — happy path
# ---------------------------------------------------------------------------
def test_withdrawal_consumer_happy_path():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        _draft, _gate, pa_id = _create_published_answer(
            conn, task_id=task_id, initial_status="published"
        )
        task_status_before = _fetch_task_status(conn, task_id=task_id)

    event = _event_for(
        pa_id,
        tenant_id=tenant_id,
        requested_by=user_id,
    )
    idem = str(event["idempotency_key"])

    rc = handle_published_answer_withdrawal(event, consumer_name=CONSUMER_NAME)
    assert rc == "processed"

    with engine.connect() as conn:
        pa = _fetch_published_answer(conn, published_answer_id=pa_id)
        assert pa["status"] == "withdrawn"
        assert pa["withdrawn_at"] is not None
        assert pa["superseded_at"] is None
        assert pa["superseded_by_id"] is None

        # Lifecycle events: exactly one withdrawal_requested + one withdrawn.
        assert _count_lifecycle_events(
            conn, published_answer_id=pa_id, event_type="withdrawal_requested"
        ) == 1
        assert _count_lifecycle_events(
            conn, published_answer_id=pa_id, event_type="withdrawn"
        ) == 1

        # Audit: exactly one published_answer.withdrawn event for this task.
        assert _count_audit_event(
            conn, task_id=task_id, event_type=AUDIT_EVENT_WITHDRAWN
        ) == 1

        # EPR: row exists, scope is fully populated, status is succeeded.
        epr = _fetch_epr(conn, consumer_name=CONSUMER_NAME, idempotency_key=idem)
        assert epr is not None
        assert epr["processing_status"] == "succeeded"
        assert epr["tenant_id"] == tenant_id
        assert epr["project_id"] == project_id
        assert epr["task_id"] == task_id

        # task_masters.status MUST NOT have been changed.
        assert _fetch_task_status(conn, task_id=task_id) == task_status_before

        # Audit chain integrity verifies end-to-end.
        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True


# ---------------------------------------------------------------------------
# 2 — redelivery with same consumer key short-circuits
# ---------------------------------------------------------------------------
def test_withdrawal_consumer_redelivery_same_consumer_key_skips():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, _project_id, user_id, task_id = _seeded_dev(conn)
        _draft, _gate, pa_id = _create_published_answer(
            conn, task_id=task_id, initial_status="published"
        )

    idem = _unique_hex()
    # Reuse the SAME idempotency_key on both deliveries. event_id can change
    # (Redis assigns a new entry id on every retry), but the consumer keys
    # off idempotency_key for EPR uniqueness.
    event_1 = _event_for(
        pa_id, idempotency_key=idem, tenant_id=tenant_id, requested_by=user_id
    )
    event_2 = _event_for(
        pa_id, idempotency_key=idem, tenant_id=tenant_id, requested_by=user_id
    )

    rc_1 = handle_published_answer_withdrawal(event_1, consumer_name=CONSUMER_NAME)
    assert rc_1 == "processed"

    rc_2 = handle_published_answer_withdrawal(event_2, consumer_name=CONSUMER_NAME)
    assert rc_2 == "skipped_already_succeeded"

    with engine.connect() as conn:
        # Lifecycle stays at the post-first-call snapshot.
        assert _count_lifecycle_events(conn, published_answer_id=pa_id) == 2
        assert _count_lifecycle_events(
            conn, published_answer_id=pa_id, event_type="withdrawal_requested"
        ) == 1
        assert _count_lifecycle_events(
            conn, published_answer_id=pa_id, event_type="withdrawn"
        ) == 1

        # Audit emitted exactly once.
        assert _count_audit_event(
            conn, task_id=task_id, event_type=AUDIT_EVENT_WITHDRAWN
        ) == 1

        # Exactly one EPR row keyed on (consumer, idempotency_key).
        assert _count_epr(
            conn, consumer_name=CONSUMER_NAME, idempotency_key=idem
        ) == 1
        epr = _fetch_epr(conn, consumer_name=CONSUMER_NAME, idempotency_key=idem)
        assert epr is not None
        assert epr["processing_status"] == "succeeded"

        pa = _fetch_published_answer(conn, published_answer_id=pa_id)
        assert pa["status"] == "withdrawn"


# ---------------------------------------------------------------------------
# 3 — different consumer keys, same lifecycle key -> service-level idempotency
# ---------------------------------------------------------------------------
def test_withdrawal_consumer_different_consumer_key_same_lifecycle_key_is_service_idempotent():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, _project_id, user_id, task_id = _seeded_dev(conn)
        _draft, _gate, pa_id = _create_published_answer(
            conn, task_id=task_id, initial_status="published"
        )

    consumer_key_a = f"consumer-a-{_unique_hex()}"
    consumer_key_b = f"consumer-b-{_unique_hex()}"
    lifecycle_key = f"lifecycle-x-{_unique_hex()}"

    event_a = _event_for(
        pa_id,
        idempotency_key=consumer_key_a,
        lifecycle_idempotency_key=lifecycle_key,
        tenant_id=tenant_id,
        requested_by=user_id,
    )
    event_b = _event_for(
        pa_id,
        idempotency_key=consumer_key_b,
        lifecycle_idempotency_key=lifecycle_key,
        tenant_id=tenant_id,
        requested_by=user_id,
    )

    rc_a = handle_published_answer_withdrawal(event_a, consumer_name=CONSUMER_NAME)
    assert rc_a == "processed"

    # Second call passes through a fresh consumer-level idempotency slot
    # (so begin_processing returns 'started', not 'succeeded'), but the
    # service short-circuits on 'already_withdrawn' (or, theoretically, on
    # the lifecycle UNIQUE if the published_answer were still 'published';
    # in this scenario it is already 'withdrawn'). The consumer maps both
    # outcomes to "processed".
    rc_b = handle_published_answer_withdrawal(event_b, consumer_name=CONSUMER_NAME)
    assert rc_b == "processed"

    with engine.connect() as conn:
        # Lifecycle remains at exactly 2 because the lifecycle key is shared.
        assert _count_lifecycle_events(conn, published_answer_id=pa_id) == 2
        assert _count_lifecycle_events(
            conn, published_answer_id=pa_id, event_type="withdrawal_requested"
        ) == 1
        assert _count_lifecycle_events(
            conn, published_answer_id=pa_id, event_type="withdrawn"
        ) == 1

        # Audit emitted exactly once: only the first call actually
        # transitioned the row from published to withdrawn.
        assert _count_audit_event(
            conn, task_id=task_id, event_type=AUDIT_EVENT_WITHDRAWN
        ) == 1

        # Two EPR rows, one per consumer key, both succeeded.
        assert _count_epr(
            conn, consumer_name=CONSUMER_NAME, idempotency_key=consumer_key_a
        ) == 1
        assert _count_epr(
            conn, consumer_name=CONSUMER_NAME, idempotency_key=consumer_key_b
        ) == 1
        epr_a = _fetch_epr(
            conn, consumer_name=CONSUMER_NAME, idempotency_key=consumer_key_a
        )
        epr_b = _fetch_epr(
            conn, consumer_name=CONSUMER_NAME, idempotency_key=consumer_key_b
        )
        assert epr_a is not None and epr_a["processing_status"] == "succeeded"
        assert epr_b is not None and epr_b["processing_status"] == "succeeded"

        pa = _fetch_published_answer(conn, published_answer_id=pa_id)
        assert pa["status"] == "withdrawn"


# ---------------------------------------------------------------------------
# 4 — missing published_answer + tenant_id provided -> failed EPR is recorded
# ---------------------------------------------------------------------------
def test_withdrawal_consumer_missing_published_answer_with_tenant_records_failed_epr():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, _project_id, _user_id, _task_id = _seeded_dev(conn)

    bogus_pa_id = uuid.uuid4()
    event = _event_for(bogus_pa_id, tenant_id=tenant_id)
    idem = str(event["idempotency_key"])

    rc = handle_published_answer_withdrawal(event, consumer_name=CONSUMER_NAME)
    assert rc == "failed"

    with engine.connect() as conn:
        epr = _fetch_epr(conn, consumer_name=CONSUMER_NAME, idempotency_key=idem)
        assert epr is not None
        assert epr["processing_status"] == "failed"
        assert epr["error_code"] == "WORKER_PUBLISHED_ANSWER_NOT_VISIBLE"
        # tenant_id is the one supplied by the producer; project/task remain
        # NULL because the published_answer could not be resolved.
        assert epr["tenant_id"] == tenant_id
        assert epr["project_id"] is None
        assert epr["task_id"] is None


# ---------------------------------------------------------------------------
# 5 — missing published_answer + no tenant_id -> no EPR row written
# ---------------------------------------------------------------------------
def test_withdrawal_consumer_missing_published_answer_without_tenant_writes_no_epr():
    engine = get_engine()

    bogus_pa_id = uuid.uuid4()
    # Deliberately omit tenant_id: the consumer cannot persist an EPR row
    # without it (event_processing_records.tenant_id is NOT NULL).
    event = _event_for(bogus_pa_id)
    idem = str(event["idempotency_key"])

    rc = handle_published_answer_withdrawal(event, consumer_name=CONSUMER_NAME)
    assert rc == "failed"

    with engine.connect() as conn:
        assert _count_epr(
            conn, consumer_name=CONSUMER_NAME, idempotency_key=idem
        ) == 0


# ---------------------------------------------------------------------------
# 6 — malformed required field -> failed pre-transaction, no EPR row
# ---------------------------------------------------------------------------
def test_withdrawal_consumer_malformed_required_field_writes_no_epr():
    """We exercise two malformed shapes covered by the same pre-transaction
    branch in the consumer:

      a) missing event_id (KeyError on the required-field block);
      b) syntactically invalid published_answer_id (ValueError on UUID parse).

    Both must short-circuit BEFORE any DB write: no event_processing_records
    row, no transaction opened.
    """
    engine = get_engine()

    # Sub-case (a): missing event_id.
    event_a: dict[str, Any] = {
        # 'event_id' deliberately absent
        "event_type": EVENT_TYPE,
        "published_answer_id": uuid.uuid4(),
        "idempotency_key": _unique_hex(),
    }
    rc_a = handle_published_answer_withdrawal(event_a, consumer_name=CONSUMER_NAME)
    assert rc_a == "failed"
    with engine.connect() as conn:
        assert _count_epr(
            conn,
            consumer_name=CONSUMER_NAME,
            idempotency_key=str(event_a["idempotency_key"]),
        ) == 0

    # Sub-case (b): published_answer_id is not a valid UUID.
    event_b: dict[str, Any] = {
        "event_id": uuid.uuid4(),
        "event_type": EVENT_TYPE,
        "published_answer_id": "not-a-uuid",
        "idempotency_key": _unique_hex(),
    }
    rc_b = handle_published_answer_withdrawal(event_b, consumer_name=CONSUMER_NAME)
    assert rc_b == "failed"
    with engine.connect() as conn:
        assert _count_epr(
            conn,
            consumer_name=CONSUMER_NAME,
            idempotency_key=str(event_b["idempotency_key"]),
        ) == 0


# ---------------------------------------------------------------------------
# 7 — wrong event_type -> failed pre-transaction, no EPR row
# ---------------------------------------------------------------------------
def test_withdrawal_consumer_bad_event_type_writes_no_epr():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)
        _draft, _gate, pa_id = _create_published_answer(
            conn, task_id=task_id, initial_status="published"
        )

    event = _event_for(
        pa_id,
        tenant_id=tenant_id,
        event_type="published_answer.something_else",
    )
    idem = str(event["idempotency_key"])

    rc = handle_published_answer_withdrawal(event, consumer_name=CONSUMER_NAME)
    assert rc == "failed"

    with engine.connect() as conn:
        assert _count_epr(
            conn, consumer_name=CONSUMER_NAME, idempotency_key=idem
        ) == 0
        # And of course the published_answer must remain untouched.
        pa = _fetch_published_answer(conn, published_answer_id=pa_id)
        assert pa["status"] == "published"
        assert pa["withdrawn_at"] is None
        assert _count_lifecycle_events(conn, published_answer_id=pa_id) == 0
        assert _count_audit_event(
            conn, task_id=task_id, event_type=AUDIT_EVENT_WITHDRAWN
        ) == 0


# ---------------------------------------------------------------------------
# 8 — malformed requested_by -> hard fail, no EPR row (verifies the fix)
# ---------------------------------------------------------------------------
def test_withdrawal_consumer_bad_requested_by_writes_no_epr():
    """Verifies the hard-fail policy on a present-but-malformed requested_by.

    Per the consumer contract:
      - a missing or empty requested_by is treated as None (system actor);
      - a present BUT syntactically invalid UUID rejects the event as
        malformed: the handler returns "failed" without opening a
        transaction and without writing any event_processing_records row.

    This is a regression test for the Block 3A-1 fix that converted the
    earlier permissive behavior into a hard pre-transaction failure.
    """
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)
        _draft, _gate, pa_id = _create_published_answer(
            conn, task_id=task_id, initial_status="published"
        )

    event = _event_for(
        pa_id,
        tenant_id=tenant_id,
        requested_by="not-a-uuid",
    )
    idem = str(event["idempotency_key"])

    rc = handle_published_answer_withdrawal(event, consumer_name=CONSUMER_NAME)
    assert rc == "failed"

    with engine.connect() as conn:
        # No EPR row was written: the failure happened BEFORE the transaction.
        assert _count_epr(
            conn, consumer_name=CONSUMER_NAME, idempotency_key=idem
        ) == 0

        # The published_answer was never touched.
        pa = _fetch_published_answer(conn, published_answer_id=pa_id)
        assert pa["status"] == "published"
        assert pa["withdrawn_at"] is None

        # Lifecycle log untouched, audit untouched.
        assert _count_lifecycle_events(conn, published_answer_id=pa_id) == 0
        assert _count_audit_event(
            conn, task_id=task_id, event_type=AUDIT_EVENT_WITHDRAWN
        ) == 0


# ---------------------------------------------------------------------------
# 9 — already withdrawn -> processed no-op, no new lifecycle / audit
# ---------------------------------------------------------------------------
def test_withdrawal_consumer_already_withdrawn_noop_succeeds():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, _project_id, user_id, task_id = _seeded_dev(conn)
        _draft, _gate, pa_id = _create_published_answer(
            conn, task_id=task_id, initial_status="withdrawn"
        )
        # Snapshot pre-call so we can assert nothing was added.
        events_before = _count_lifecycle_events(conn, published_answer_id=pa_id)
        audit_before = _count_audit_event(
            conn, task_id=task_id, event_type=AUDIT_EVENT_WITHDRAWN
        )
        pa_before = _fetch_published_answer(conn, published_answer_id=pa_id)

    event = _event_for(
        pa_id,
        tenant_id=tenant_id,
        requested_by=user_id,
    )
    idem = str(event["idempotency_key"])

    rc = handle_published_answer_withdrawal(event, consumer_name=CONSUMER_NAME)
    assert rc == "processed"

    with engine.connect() as conn:
        # Lifecycle log unchanged.
        assert _count_lifecycle_events(
            conn, published_answer_id=pa_id
        ) == events_before
        # Audit count unchanged.
        assert _count_audit_event(
            conn, task_id=task_id, event_type=AUDIT_EVENT_WITHDRAWN
        ) == audit_before

        # published_answers row unchanged: status, withdrawn_at preserved.
        pa_after = _fetch_published_answer(conn, published_answer_id=pa_id)
        assert pa_after["status"] == "withdrawn"
        assert pa_after["withdrawn_at"] == pa_before["withdrawn_at"]
        assert pa_after["superseded_at"] is None
        assert pa_after["superseded_by_id"] is None

        # EPR row exists and is succeeded.
        epr = _fetch_epr(conn, consumer_name=CONSUMER_NAME, idempotency_key=idem)
        assert epr is not None
        assert epr["processing_status"] == "succeeded"


# ---------------------------------------------------------------------------
# 10 — already superseded -> processed no-op, no new audit
# ---------------------------------------------------------------------------
def test_withdrawal_consumer_already_superseded_noop_succeeds():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, _project_id, user_id, task_id = _seeded_dev(conn)
        _draft, _gate, pa_id = _create_published_answer(
            conn, task_id=task_id, initial_status="superseded"
        )
        events_before = _count_lifecycle_events(conn, published_answer_id=pa_id)
        audit_before = _count_audit_event(
            conn, task_id=task_id, event_type=AUDIT_EVENT_WITHDRAWN
        )
        pa_before = _fetch_published_answer(conn, published_answer_id=pa_id)

    event = _event_for(
        pa_id,
        tenant_id=tenant_id,
        requested_by=user_id,
    )
    idem = str(event["idempotency_key"])

    rc = handle_published_answer_withdrawal(event, consumer_name=CONSUMER_NAME)
    assert rc == "processed"

    with engine.connect() as conn:
        # Lifecycle log unchanged: the service does NOT insert any event
        # for an already-superseded row.
        assert _count_lifecycle_events(
            conn, published_answer_id=pa_id
        ) == events_before
        # No new published_answer.withdrawn audit.
        assert _count_audit_event(
            conn, task_id=task_id, event_type=AUDIT_EVENT_WITHDRAWN
        ) == audit_before

        # published_answers row unchanged: status, superseded_at preserved.
        pa_after = _fetch_published_answer(conn, published_answer_id=pa_id)
        assert pa_after["status"] == "superseded"
        assert pa_after["superseded_at"] == pa_before["superseded_at"]
        assert pa_after["withdrawn_at"] is None

        # EPR row exists and is succeeded.
        epr = _fetch_epr(conn, consumer_name=CONSUMER_NAME, idempotency_key=idem)
        assert epr is not None
        assert epr["processing_status"] == "succeeded"
