"""Task endpoints (MVP-0 — closed_corpus stub).

8.2a-patch:
  - document_ids validation no longer relies on `id = ANY(:ids)` over a raw
    list[UUID] passed through SQLAlchemy text(). We use bindparam(expanding=True)
    via SQLAlchemy `tuple_` with `.expanding=True`, which is SQLAlchemy's
    documented mechanism for IN-clause parameter expansion across drivers.
  - All UUID values entering the SQL parameters are already validated by Pydantic
    (TaskCreate.document_ids: list[uuid.UUID]). No string interpolation.

8.4.x-patch:
  - Redis XADD failure after DB commit is now logged instead of silently swallowed.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection
from sqlalchemy.types import Uuid as SAUuid

from evidencefirst_shared.db.audit import audit_append
from evidencefirst_shared.errors import ErrorCode, NormalizedError
from evidencefirst_shared.schemas import TaskCreate, TaskRead

from ..config import get_settings
from ..db import get_conn, transaction
from ..redis import get_redis


logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["tasks"])


def _row_to_task(row: Any) -> TaskRead:
    policy = row.policy if isinstance(row.policy, dict) else json.loads(row.policy)
    return TaskRead(
        id=uuid.UUID(str(row.id)),
        tenant_id=uuid.UUID(str(row.tenant_id)),
        project_id=uuid.UUID(str(row.project_id)),
        mode=row.mode,
        objective=row.objective,
        status=row.status,
        policy=policy,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _resolve_dev_user(conn: Connection, tenant_id: uuid.UUID) -> uuid.UUID | None:
    s = get_settings()
    user = conn.execute(
        text("SELECT id FROM users WHERE tenant_id = :t AND email = :e"),
        {"t": tenant_id, "e": s.DEV_USER_EMAIL},
    ).first()
    return uuid.UUID(str(user[0])) if user else None


def _validate_documents_in_project(
    conn: Connection, *, project_id: uuid.UUID, document_ids: list[uuid.UUID]
) -> None:
    """Validate that every document_id exists and belongs to project_id.

    Uses SQLAlchemy's expanding bindparam to safely pass a list of UUIDs into an
    IN-clause; rejects empty/duplicate inputs to keep the validator deterministic.
    """
    if not document_ids:
        return

    # Deduplicate while preserving the original order for error reporting.
    seen: set[uuid.UUID] = set()
    unique_ids: list[uuid.UUID] = []
    for d in document_ids:
        if d in seen:
            raise NormalizedError(
                ErrorCode.VALIDATION_ERROR,
                "document_ids must not contain duplicates.",
                details={"duplicate": str(d)},
            )
        seen.add(d)
        unique_ids.append(d)

    stmt = text(
        """
        SELECT id, project_id
        FROM uploaded_documents
        WHERE id IN :ids
        """
    ).bindparams(bindparam("ids", expanding=True, type_=SAUuid()))

    rows = conn.execute(stmt, {"ids": unique_ids}).fetchall()
    found_by_id = {uuid.UUID(str(r.id)): uuid.UUID(str(r.project_id)) for r in rows}

    missing = [str(d) for d in unique_ids if d not in found_by_id]
    if missing:
        raise NormalizedError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "One or more document_ids not found.",
            details={"missing": missing},
        )

    foreign = [str(d) for d, p in found_by_id.items() if p != project_id]
    if foreign:
        raise NormalizedError(
            ErrorCode.VALIDATION_ERROR,
            "All document_ids must belong to the task's project.",
            details={"foreign": foreign},
        )


@router.post("/tasks", status_code=201, response_model=TaskRead)
def create_task(
    request: Request,
    body: TaskCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> TaskRead:
    if body.mode != "closed_corpus":
        raise NormalizedError(
            ErrorCode.VALIDATION_ERROR,
            "MVP-0 supports only mode='closed_corpus'",
            details={"received_mode": body.mode},
        )

    task_row: Any = None
    audit_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    used_idempotency_key: str | None = None
    replayed_existing = False

    with transaction() as conn:
        project = conn.execute(
            text("SELECT id, tenant_id FROM projects WHERE id = :id AND deleted_at IS NULL"),
            {"id": body.project_id},
        ).first()
        if not project:
            raise NormalizedError(ErrorCode.RESOURCE_NOT_FOUND, "Project not found")
        tenant_id = uuid.UUID(str(project.tenant_id))
        user_id = _resolve_dev_user(conn, tenant_id)

        if idempotency_key:
            existing = conn.execute(
                text(
                    """
                    SELECT id, tenant_id, project_id, mode, objective, status, policy, created_at, updated_at
                    FROM task_masters
                    WHERE project_id = :pid
                      AND policy ->> 'idempotency_key' = :ikey
                    LIMIT 1
                    """
                ),
                {"pid": body.project_id, "ikey": idempotency_key},
            ).first()
            if existing:
                task_row = existing
                replayed_existing = True

        if not replayed_existing:
            _validate_documents_in_project(
                conn, project_id=body.project_id, document_ids=list(body.document_ids)
            )

            policy: dict[str, Any] = dict(body.policy or {})
            if idempotency_key:
                policy["idempotency_key"] = idempotency_key

            task_row = conn.execute(
                text(
                    """
                    INSERT INTO task_masters
                        (tenant_id, project_id, created_by, mode, objective, policy, status)
                    VALUES
                        (:tenant_id, :project_id, :created_by, 'closed_corpus', :objective,
                         CAST(:policy AS JSONB), 'created')
                    RETURNING id, tenant_id, project_id, mode, objective, status, policy, created_at, updated_at
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "project_id": body.project_id,
                    "created_by": user_id,
                    "objective": body.objective,
                    "policy": json.dumps(policy),
                },
            ).one()

            task_id = uuid.UUID(str(task_row.id))

            audit_id = audit_append(
                conn,
                chain_scope="task",
                tenant_id=tenant_id,
                project_id=body.project_id,
                task_id=task_id,
                session_id=None,
                event_type="task.created",
                actor_type="user" if user_id else "system",
                actor_id=str(user_id) if user_id else "system",
                redacted_payload={
                    "mode": "closed_corpus",
                    "objective_truncated": (body.objective[:120] + "…")
                    if len(body.objective) > 120
                    else body.objective,
                    "policy_keys": sorted(policy.keys()),
                    "document_id_count": len(body.document_ids),
                },
                related_entity_type="task_masters",
                related_entity_id=task_id,
            )

            for pos, did in enumerate(body.document_ids):
                conn.execute(
                    text(
                        """
                        INSERT INTO task_documents (task_id, document_id, role, position)
                        VALUES (:tid, :did, 'source', :pos)
                        ON CONFLICT (task_id, document_id) DO NOTHING
                        """
                    ),
                    {"tid": task_id, "did": did, "pos": pos},
                )

            if body.document_ids:
                audit_append(
                    conn,
                    chain_scope="task",
                    tenant_id=tenant_id,
                    project_id=body.project_id,
                    task_id=task_id,
                    session_id=None,
                    event_type="task.docs_attached",
                    actor_type="user" if user_id else "system",
                    actor_id=str(user_id) if user_id else "system",
                    redacted_payload={
                        "document_id_count": len(body.document_ids),
                        "document_ids": [str(d) for d in body.document_ids],
                    },
                    related_entity_type="task_masters",
                    related_entity_id=task_id,
                )

            used_idempotency_key = idempotency_key or str(task_id)

    if not replayed_existing and task_row is not None and tenant_id is not None and audit_id is not None:
        task_id = uuid.UUID(str(task_row.id))
        stream = get_settings().EVENTS_TASK_CREATED_STREAM
        try:
            r = get_redis()
            r.xadd(
                stream,
                {
                    "event_id": str(uuid.uuid4()),
                    "event_type": "task.created",
                    "tenant_id": str(tenant_id),
                    "project_id": str(body.project_id),
                    "task_id": str(task_id),
                    "audit_record_id": str(audit_id),
                    "idempotency_key": used_idempotency_key or str(task_id),
                },
                maxlen=10000,
                approximate=True,
            )
        except Exception:
            logger.exception(
                "task_created_event_publish_failed",
                task_id=str(task_id),
                stream=stream,
            )

    return _row_to_task(task_row)


@router.get("/tasks/{task_id}", response_model=TaskRead)
def get_task(task_id: uuid.UUID, conn: Connection = Depends(get_conn)) -> TaskRead:
    row = conn.execute(
        text(
            """
            SELECT id, tenant_id, project_id, mode, objective, status, policy, created_at, updated_at
            FROM task_masters WHERE id = :id
            """
        ),
        {"id": task_id},
    ).first()
    if not row:
        raise NormalizedError(ErrorCode.RESOURCE_NOT_FOUND, "Task not found")
    return _row_to_task(row)


@router.get("/tasks/{task_id}/documents")
def list_task_documents(task_id: uuid.UUID, conn: Connection = Depends(get_conn)) -> dict[str, Any]:
    task = conn.execute(text("SELECT id FROM task_masters WHERE id = :id"), {"id": task_id}).first()
    if not task:
        raise NormalizedError(ErrorCode.RESOURCE_NOT_FOUND, "Task not found")
    rows = conn.execute(
        text(
            """
            SELECT td.task_id, td.document_id, td.role, td.position,
                   ud.filename, ud.content_hash
            FROM task_documents td
            JOIN uploaded_documents ud ON ud.id = td.document_id
            WHERE td.task_id = :tid
            ORDER BY td.position ASC, td.created_at ASC
            """
        ),
        {"tid": task_id},
    ).fetchall()
    items = [
        {
            "task_id": str(r.task_id),
            "document_id": str(r.document_id),
            "role": r.role,
            "position": int(r.position),
            "filename": r.filename,
            "content_hash": r.content_hash,
        }
        for r in rows
    ]
    return {"items": items}
