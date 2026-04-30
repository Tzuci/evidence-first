"""Idempotency, terminal-state and FK-safety tests for the task.created consumer."""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

from evidencefirst_shared.db.audit import verify_task_audit_chain

from app.consumers.task_created import handle_task_created
from app.db import get_engine, transaction


def _seeded_dev() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(text(
            """
            SELECT t.id AS tenant_id, p.id AS project_id, u.id AS user_id
            FROM tenants t
            JOIN projects p ON p.tenant_id = t.id AND p.name = 'default'
            LEFT JOIN users u ON u.tenant_id = t.id AND u.email = 'dev@local'
            WHERE t.slug = 'dev'
            """
        )).first()
    assert row is not None, "Run `make seed` first."
    return uuid.UUID(str(row.tenant_id)), uuid.UUID(str(row.project_id)), uuid.UUID(str(row.user_id))


def _create_task(tenant_id, project_id, user_id) -> uuid.UUID:
    with transaction() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO task_masters (tenant_id, project_id, created_by, mode, objective, status)
                VALUES (:t, :p, :u, 'closed_corpus', 'idem-test', 'created')
                RETURNING id
                """
            ),
            {"t": tenant_id, "p": project_id, "u": user_id},
        ).one()
    return uuid.UUID(str(row[0]))


def _count_audit(task_id: uuid.UUID) -> int:
    with get_engine().connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM audit_records WHERE task_id = :t AND chain_scope = 'task'"),
                {"t": task_id},
            ).scalar_one()
        )


def _count_epr_succeeded(idempotency_key: str) -> int:
    with get_engine().connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM event_processing_records WHERE idempotency_key = :k AND processing_status = 'succeeded'"
                ),
                {"k": idempotency_key},
            ).scalar_one()
        )


def _fetch_epr(idempotency_key: str) -> dict | None:
    with get_engine().connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, processing_status, task_id, error_code
                FROM event_processing_records
                WHERE idempotency_key = :k
                LIMIT 1
                """
            ),
            {"k": idempotency_key},
        ).first()
    if row is None:
        return None
    return {
        "id": uuid.UUID(str(row.id)),
        "processing_status": str(row.processing_status),
        "task_id": uuid.UUID(str(row.task_id)) if row.task_id else None,
        "error_code": row.error_code,
    }


def _task_status(task_id: uuid.UUID) -> str:
    with get_engine().connect() as conn:
        return str(conn.execute(text("SELECT status FROM task_masters WHERE id = :id"), {"id": task_id}).scalar_one())


def test_no_docs_double_delivery_is_idempotent():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    tenant_id, project_id, user_id = _seeded_dev()
    task_id = _create_task(tenant_id, project_id, user_id)
    idem = f"idem-{task_id}"
    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "task.created",
        "tenant_id": str(tenant_id),
        "project_id": str(project_id),
        "task_id": str(task_id),
        "idempotency_key": idem,
    }
    assert handle_task_created(event, consumer_name="worker_test_nd") == "processed"
    assert handle_task_created(event, consumer_name="worker_test_nd") == "skipped_already_succeeded"
    assert _task_status(task_id) == "blocked"
    assert _count_audit(task_id) == 2  # analyzing, blocked
    assert _count_epr_succeeded(idem) == 1
    with get_engine().begin() as conn:
        assert verify_task_audit_chain(conn, task_id=task_id)["ok"] is True


def test_consumer_returns_failed_when_task_does_not_exist_fk_safe():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    tenant_id, project_id, _u = _seeded_dev()
    bogus = uuid.uuid4()
    idem = f"idem-bogus-{bogus}"
    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "task.created",
        "tenant_id": str(tenant_id),
        "project_id": str(project_id),
        "task_id": str(bogus),
        "idempotency_key": idem,
    }
    assert handle_task_created(event, consumer_name="worker_test_bogus") == "failed"
    assert _count_audit(bogus) == 0
    assert _count_epr_succeeded(idem) == 0
    epr = _fetch_epr(idem)
    assert epr and epr["processing_status"] == "failed" and epr["task_id"] is None
    assert epr["error_code"] == "WORKER_TASK_NOT_VISIBLE"


def test_consumer_skips_terminal_when_blocked():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    tenant_id, project_id, user_id = _seeded_dev()
    task_id = _create_task(tenant_id, project_id, user_id)
    e1 = {
        "event_id": str(uuid.uuid4()),
        "event_type": "task.created",
        "tenant_id": str(tenant_id),
        "project_id": str(project_id),
        "task_id": str(task_id),
        "idempotency_key": f"first-{task_id}",
    }
    assert handle_task_created(e1, consumer_name="w") == "processed"
    e2 = {**e1, "event_id": str(uuid.uuid4()), "idempotency_key": f"second-{task_id}"}
    assert handle_task_created(e2, consumer_name="w") == "skipped_terminal"
    assert _count_audit(task_id) == 2