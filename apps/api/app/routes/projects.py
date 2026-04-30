"""Project endpoints (MVP-0)."""
from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evidencefirst_shared.errors import ErrorCode, NormalizedError
from evidencefirst_shared.schemas import ProjectCreate, ProjectRead

from ..config import get_settings
from ..db import get_conn


router = APIRouter(prefix="/api/v1", tags=["projects"])


def _dev_tenant_user(conn: Connection) -> tuple[uuid.UUID, uuid.UUID | None]:
    """Resolve tenant_id and dev user_id from seeded data."""
    s = get_settings()
    tenant = conn.execute(
        text("SELECT id FROM tenants WHERE slug = :slug"), {"slug": s.DEV_TENANT_SLUG}
    ).first()
    if not tenant:
        raise NormalizedError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "Dev tenant missing. Did you run `make seed`?",
            remediation_hint="Run `make seed` to populate the dev tenant/user/project.",
        )
    tenant_id = uuid.UUID(str(tenant[0]))
    user = conn.execute(
        text("SELECT id FROM users WHERE tenant_id = :t AND email = :e"),
        {"t": tenant_id, "e": s.DEV_USER_EMAIL},
    ).first()
    user_id = uuid.UUID(str(user[0])) if user else None
    return tenant_id, user_id


def _row_to_project(row: Any) -> ProjectRead:
    return ProjectRead(
        id=uuid.UUID(str(row.id)),
        tenant_id=uuid.UUID(str(row.tenant_id)),
        name=row.name,
        mode_default=row.mode_default,
        created_by=uuid.UUID(str(row.created_by)) if row.created_by else None,
        created_at=row.created_at,
    )


def _encode_cursor(created_at, project_id: uuid.UUID) -> str:
    payload = {"created_at": created_at.isoformat(), "id": str(project_id)}
    return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, uuid.UUID]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        return payload["created_at"], uuid.UUID(payload["id"])
    except Exception:
        raise NormalizedError(ErrorCode.VALIDATION_ERROR, "Invalid cursor")


@router.post("/projects", status_code=201, response_model=ProjectRead)
def create_project(
    request: Request,
    body: ProjectCreate,
    conn: Connection = Depends(get_conn),
) -> ProjectRead:
    tenant_id, user_id = _dev_tenant_user(conn)

    # Conflict on (tenant_id, name)
    existing = conn.execute(
        text("SELECT id FROM projects WHERE tenant_id = :t AND name = :n"),
        {"t": tenant_id, "n": body.name},
    ).first()
    if existing:
        raise NormalizedError(
            ErrorCode.RESOURCE_CONFLICT,
            f"Project with name {body.name!r} already exists in this tenant",
        )

    row = conn.execute(
        text(
            """
            INSERT INTO projects (tenant_id, name, mode_default, created_by)
            VALUES (:tenant_id, :name, :mode_default, :created_by)
            RETURNING id, tenant_id, name, mode_default, created_by, created_at
            """
        ),
        {
            "tenant_id": tenant_id,
            "name": body.name,
            "mode_default": body.mode_default,
            "created_by": user_id,
        },
    ).one()
    return _row_to_project(row)


@router.get("/projects/{project_id}", response_model=ProjectRead)
def get_project(project_id: uuid.UUID, conn: Connection = Depends(get_conn)) -> ProjectRead:
    row = conn.execute(
        text(
            """
            SELECT id, tenant_id, name, mode_default, created_by, created_at
            FROM projects WHERE id = :id
            """
        ),
        {"id": project_id},
    ).first()
    if not row:
        raise NormalizedError(ErrorCode.RESOURCE_NOT_FOUND, "Project not found")
    return _row_to_project(row)


@router.get("/projects")
def list_projects(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    tenant_id, _ = _dev_tenant_user(conn)

    if cursor:
        c_created, c_id = _decode_cursor(cursor)
        rows = conn.execute(
            text(
                """
                SELECT id, tenant_id, name, mode_default, created_by, created_at
                FROM projects
                WHERE tenant_id = :tenant_id
                  AND deleted_at IS NULL
                  AND (created_at, id) < (:c_created, :c_id)
                ORDER BY created_at DESC, id DESC
                LIMIT :limit
                """
            ),
            {"tenant_id": tenant_id, "c_created": c_created, "c_id": c_id, "limit": limit + 1},
        ).fetchall()
    else:
        rows = conn.execute(
            text(
                """
                SELECT id, tenant_id, name, mode_default, created_by, created_at
                FROM projects
                WHERE tenant_id = :tenant_id AND deleted_at IS NULL
                ORDER BY created_at DESC, id DESC
                LIMIT :limit
                """
            ),
            {"tenant_id": tenant_id, "limit": limit + 1},
        ).fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = _encode_cursor(last.created_at, uuid.UUID(str(last.id)))

    return {
        "items": [_row_to_project(r).model_dump(mode="json") for r in page],
        "next_cursor": next_cursor,
    }