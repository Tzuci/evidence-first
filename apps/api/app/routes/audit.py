"""Audit read endpoint (redacted)."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evidencefirst_shared.errors import ErrorCode, NormalizedError
from evidencefirst_shared.schemas import AuditEventRead

from ..db import get_conn


router = APIRouter(prefix="/api/v1", tags=["audit"])


def _row_to_event(row: Any) -> AuditEventRead:
    return AuditEventRead(
        id=uuid.UUID(str(row.id)),
        chain_scope=row.chain_scope,
        scope_id=uuid.UUID(str(row.scope_id)),
        chain_seq=int(row.chain_seq),
        event_type=row.event_type,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        related_entity_type=row.related_entity_type,
        related_entity_id=uuid.UUID(str(row.related_entity_id)) if row.related_entity_id else None,
        redacted_payload=row.redacted_payload if isinstance(row.redacted_payload, dict) else {},
        event_hash_hex=bytes(row.event_hash).hex(),
        previous_event_hash_hex=bytes(row.previous_event_hash).hex() if row.previous_event_hash else None,
        created_at=row.created_at,
    )


@router.get("/tasks/{task_id}/audit")
def list_task_audit(
    task_id: uuid.UUID,
    after_seq: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    task = conn.execute(
        text("SELECT id FROM task_masters WHERE id = :id"), {"id": task_id}
    ).first()
    if not task:
        raise NormalizedError(ErrorCode.RESOURCE_NOT_FOUND, "Task not found")

    if after_seq is None:
        rows = conn.execute(
            text(
                """
                SELECT id, chain_scope, scope_id, chain_seq, event_type, actor_type, actor_id,
                       related_entity_type, related_entity_id, redacted_payload,
                       event_hash, previous_event_hash, created_at
                FROM audit_records
                WHERE chain_scope = 'task' AND task_id = :task_id
                ORDER BY chain_seq ASC
                LIMIT :limit
                """
            ),
            {"task_id": task_id, "limit": limit + 1},
        ).fetchall()
    else:
        rows = conn.execute(
            text(
                """
                SELECT id, chain_scope, scope_id, chain_seq, event_type, actor_type, actor_id,
                       related_entity_type, related_entity_id, redacted_payload,
                       event_hash, previous_event_hash, created_at
                FROM audit_records
                WHERE chain_scope = 'task' AND task_id = :task_id AND chain_seq > :after
                ORDER BY chain_seq ASC
                LIMIT :limit
                """
            ),
            {"task_id": task_id, "after": after_seq, "limit": limit + 1},
        ).fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    next_after_seq = int(page[-1].chain_seq) if has_more and page else None

    return {
        "items": [_row_to_event(r).model_dump(mode="json") for r in page],
        "next_after_seq": next_after_seq,
    }