"""event_processing_records helpers (single source of truth).

Idempotency contract:
- begin_processing: tries to insert a 'started' record; if a 'succeeded' record
  already exists, returns its id with status='succeeded' so the caller can skip.
- mark_succeeded / mark_failed / increment_attempt: post-processing transitions.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection


def begin_processing(
    conn: Connection,
    *,
    event_id: uuid.UUID,
    event_type: str,
    consumer_name: str,
    idempotency_key: str,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, str]:
    """Acquire processing slot for (consumer_name, idempotency_key).

    Returns (record_id, processing_status). The caller must:
      - skip if status == 'succeeded'
      - process otherwise (status == 'started' or rare 'failed' from a previous attempt)
    """
    conn.execute(
        text(
            """
            INSERT INTO event_processing_records (
                event_id, event_type, consumer_name, idempotency_key,
                tenant_id, project_id, task_id, processing_status
            ) VALUES (
                :event_id, :event_type, :consumer_name, :idempotency_key,
                :tenant_id, :project_id, :task_id, 'started'
            )
            ON CONFLICT (consumer_name, idempotency_key) DO NOTHING
            """
        ),
        {
            "event_id": event_id,
            "event_type": event_type,
            "consumer_name": consumer_name,
            "idempotency_key": idempotency_key,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "task_id": task_id,
        },
    )
    row = conn.execute(
        text(
            """
            SELECT id, processing_status
            FROM event_processing_records
            WHERE consumer_name = :c AND idempotency_key = :k
            FOR UPDATE
            """
        ),
        {"c": consumer_name, "k": idempotency_key},
    ).one()
    return uuid.UUID(str(row[0])), str(row[1])


def mark_succeeded(
    conn: Connection,
    *,
    record_id: uuid.UUID,
    audit_record_id: uuid.UUID | None = None,
    result_hash: bytes | None = None,
) -> None:
    conn.execute(
        text(
            """
            UPDATE event_processing_records
            SET processing_status = 'succeeded',
                completed_at = NOW(),
                last_attempt_at = NOW(),
                audit_record_id = :audit,
                result_hash = :rhash
            WHERE id = :id
            """
        ),
        {"id": record_id, "audit": audit_record_id, "rhash": result_hash},
    )


def mark_failed(
    conn: Connection,
    *,
    record_id: uuid.UUID,
    error_code: str,
    error_message: str,
) -> None:
    conn.execute(
        text(
            """
            UPDATE event_processing_records
            SET processing_status = 'failed',
                last_attempt_at = NOW(),
                attempt_count = attempt_count + 1,
                error_code = :code,
                error_message = :msg
            WHERE id = :id
            """
        ),
        {"id": record_id, "code": error_code, "msg": error_message},
    )


def increment_attempt(conn: Connection, *, record_id: uuid.UUID) -> None:
    conn.execute(
        text(
            """
            UPDATE event_processing_records
            SET last_attempt_at = NOW(),
                attempt_count = attempt_count + 1
            WHERE id = :id
            """
        ),
        {"id": record_id},
    )