"""Test audit_append service behavior (uses a real DB)."""
import datetime as dt
import os
import uuid

import pytest
from sqlalchemy import text

from app.db import get_engine
from evidencefirst_shared.db.audit import audit_append, verify_task_audit_chain


def _seeded_dev(conn) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    row = conn.execute(text(
        """
        SELECT t.id AS tenant_id, p.id AS project_id, u.id AS user_id
        FROM tenants t
        JOIN projects p ON p.tenant_id = t.id AND p.name = 'default'
        LEFT JOIN users u ON u.tenant_id = t.id AND u.email = 'dev@local'
        WHERE t.slug = 'dev'
        """
    )).first()
    assert row is not None, "Run `make seed` before tests."
    return uuid.UUID(str(row.tenant_id)), uuid.UUID(str(row.project_id)), uuid.UUID(str(row.user_id))


def _create_task(conn, tenant_id, project_id, user_id) -> uuid.UUID:
    row = conn.execute(
        text(
            """
            INSERT INTO task_masters
                (tenant_id, project_id, created_by, mode, objective, status)
            VALUES (:t, :p, :u, 'closed_corpus', 'audit-test', 'created')
            RETURNING id
            """
        ),
        {"t": tenant_id, "p": project_id, "u": user_id},
    ).one()
    return uuid.UUID(str(row[0]))


def test_audit_chain_two_events_have_consistent_links_and_verify_ok():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    eng = get_engine()
    with eng.begin() as conn:
        tenant_id, project_id, user_id = _seeded_dev(conn)
        task_id = _create_task(conn, tenant_id, project_id, user_id)

        audit_append(
            conn,
            chain_scope="task",
            tenant_id=tenant_id, project_id=project_id, task_id=task_id, session_id=None,
            event_type="task.created", actor_type="system", actor_id="test",
            redacted_payload={"k": "v"},
        )
        audit_append(
            conn,
            chain_scope="task",
            tenant_id=tenant_id, project_id=project_id, task_id=task_id, session_id=None,
            event_type="task.analyzing", actor_type="system", actor_id="test",
            redacted_payload={"step": "stub"},
        )

        rows = conn.execute(
            text(
                """
                SELECT chain_seq, event_hash, previous_event_hash, scope_id
                FROM audit_records
                WHERE chain_scope = 'task' AND task_id = :tid
                ORDER BY chain_seq ASC
                """
            ),
            {"tid": task_id},
        ).fetchall()

        assert len(rows) == 2
        assert rows[0].chain_seq == 1
        assert rows[1].chain_seq == 2
        assert rows[0].previous_event_hash is None
        assert bytes(rows[1].previous_event_hash) == bytes(rows[0].event_hash)
        assert uuid.UUID(str(rows[0].scope_id)) == task_id
        assert uuid.UUID(str(rows[1].scope_id)) == task_id

        verification = verify_task_audit_chain(conn, task_id=task_id)
        assert verification["ok"] is True
        assert verification["checked_count"] == 2
        assert verification["discrepancies"] == []


def test_audit_chain_with_non_primitive_payload_verifies_ok():
    """payload con UUID, datetime aware/naive, bytes, lista annidata."""
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    eng = get_engine()
    with eng.begin() as conn:
        tenant_id, project_id, user_id = _seeded_dev(conn)
        task_id = _create_task(conn, tenant_id, project_id, user_id)

        nested_payload = {
            "an_uuid": uuid.uuid4(),
            "a_datetime_utc": dt.datetime(2026, 4, 28, 13, 45, 0, 123000, tzinfo=dt.timezone.utc),
            "a_datetime_naive": dt.datetime(2026, 4, 28, 13, 45, 0),
            "some_bytes": bytes.fromhex("DEADBEEF"),
            "nested": [
                {"k": "v", "u": uuid.uuid4()},
                [1, 2, 3],
                None,
                "string",
                42,
                3.14,
            ],
        }

        audit_append(
            conn,
            chain_scope="task",
            tenant_id=tenant_id, project_id=project_id, task_id=task_id, session_id=None,
            event_type="task.with_complex_payload", actor_type="system", actor_id="test",
            redacted_payload=nested_payload,
        )

        verification = verify_task_audit_chain(conn, task_id=task_id)
        assert verification["ok"] is True, verification
        assert verification["checked_count"] == 1
        assert verification["discrepancies"] == []