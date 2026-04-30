"""Claim read-only endpoints (Phase 8.3).

No side effects. Pagination is intentionally minimal (limit only) since the
read patterns are dashboard-oriented and the bounded ledger size makes cursor
pagination unnecessary in MVP-0.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evidencefirst_shared.errors import ErrorCode, NormalizedError
from evidencefirst_shared.schemas import (
    ClaimEvidenceLinkRead,
    ClaimEvidenceRead,
    ClaimLedgerEntryRead,
    ClassifiedClaimRead,
    RawClaimRead,
    VerificationRecordRead,
)

from ..db import get_conn


router = APIRouter(prefix="/api/v1", tags=["claims"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _ensure_task_exists(conn: Connection, task_id: uuid.UUID) -> None:
    row = conn.execute(
        text("SELECT id FROM task_masters WHERE id = :id"), {"id": task_id}
    ).first()
    if not row:
        raise NormalizedError(ErrorCode.RESOURCE_NOT_FOUND, "Task not found")


def _ensure_logical_claim_exists(conn: Connection, claim_logical_id: uuid.UUID) -> None:
    row = conn.execute(
        text("SELECT id FROM logical_claims WHERE id = :id"), {"id": claim_logical_id}
    ).first()
    if not row:
        raise NormalizedError(ErrorCode.RESOURCE_NOT_FOUND, "Logical claim not found")


def _row_raw(r: Any) -> RawClaimRead:
    return RawClaimRead(
        id=uuid.UUID(str(r.id)),
        task_id=uuid.UUID(str(r.task_id)),
        logical_claim_id=uuid.UUID(str(r.logical_claim_id)),
        document_chunk_id=uuid.UUID(str(r.document_chunk_id)),
        evidence_span_id=uuid.UUID(str(r.evidence_span_id)),
        raw_text=r.raw_text,
        extractor_name=r.extractor_name,
        extractor_version=r.extractor_version,
        created_at=r.created_at,
    )


def _row_cls(r: Any) -> ClassifiedClaimRead:
    return ClassifiedClaimRead(
        id=uuid.UUID(str(r.id)),
        raw_claim_id=uuid.UUID(str(r.raw_claim_id)),
        logical_claim_id=uuid.UUID(str(r.logical_claim_id)),
        claim_type=r.claim_type,
        domain_tag=r.domain_tag,
        qualifiers=r.qualifiers if isinstance(r.qualifiers, dict) else {},
        classifier_name=r.classifier_name,
        classifier_version=r.classifier_version,
        created_at=r.created_at,
    )


def _row_entry(r: Any) -> ClaimLedgerEntryRead:
    return ClaimLedgerEntryRead(
        id=uuid.UUID(str(r.id)),
        claim_logical_id=uuid.UUID(str(r.claim_logical_id)),
        version_no=int(r.version_no),
        state=r.state,
        support_scope=r.support_scope,
        user_provided_dependency=r.user_provided_dependency,
        human_review_required=bool(r.human_review_required),
        human_review_status=r.human_review_status,
        transition_reason=r.transition_reason,
        payload=r.payload if isinstance(r.payload, dict) else {},
        created_at=r.created_at,
    )


def _row_link(r: Any) -> ClaimEvidenceLinkRead:
    return ClaimEvidenceLinkRead(
        id=uuid.UUID(str(r.id)),
        claim_logical_id=uuid.UUID(str(r.claim_logical_id)),
        claim_ledger_entry_id=uuid.UUID(str(r.claim_ledger_entry_id)),
        evidence_span_id=uuid.UUID(str(r.evidence_span_id)) if r.evidence_span_id else None,
        retrieved_source_span_id=(
            uuid.UUID(str(r.retrieved_source_span_id)) if r.retrieved_source_span_id else None
        ),
        link_role=r.link_role,
        created_at=r.created_at,
    )


def _row_vr(r: Any) -> VerificationRecordRead:
    return VerificationRecordRead(
        id=uuid.UUID(str(r.id)),
        claim_logical_id=uuid.UUID(str(r.claim_logical_id)),
        claim_ledger_entry_id=uuid.UUID(str(r.claim_ledger_entry_id)),
        check_kind=r.check_kind,
        check_name=r.check_name,
        outcome=r.outcome,
        score=float(r.score) if r.score is not None else None,
        evaluator_id=r.evaluator_id,
        payload=r.payload if isinstance(r.payload, dict) else {},
        created_at=r.created_at,
    )


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------
@router.get("/tasks/{task_id}/raw-claims")
def list_raw_claims(
    task_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    _ensure_task_exists(conn, task_id)
    rows = conn.execute(
        text(
            """
            SELECT id, task_id, logical_claim_id, document_chunk_id, evidence_span_id,
                   raw_text, extractor_name, extractor_version, created_at
            FROM raw_claims
            WHERE task_id = :tid
            ORDER BY created_at ASC, id ASC
            LIMIT :limit
            """
        ),
        {"tid": task_id, "limit": limit},
    ).fetchall()
    return {"items": [_row_raw(r).model_dump(mode="json") for r in rows]}


@router.get("/tasks/{task_id}/classified-claims")
def list_classified_claims(
    task_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    _ensure_task_exists(conn, task_id)
    rows = conn.execute(
        text(
            """
            SELECT cc.id, cc.raw_claim_id, cc.logical_claim_id, cc.claim_type,
                   cc.domain_tag, cc.qualifiers, cc.classifier_name, cc.classifier_version,
                   cc.created_at
            FROM classified_claims cc
            JOIN raw_claims rc ON rc.id = cc.raw_claim_id
            WHERE rc.task_id = :tid
            ORDER BY cc.created_at ASC, cc.id ASC
            LIMIT :limit
            """
        ),
        {"tid": task_id, "limit": limit},
    ).fetchall()
    return {"items": [_row_cls(r).model_dump(mode="json") for r in rows]}


@router.get("/tasks/{task_id}/claims")
def list_task_claims_latest(
    task_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, Any]:
    """Return the latest claim_ledger_entries per claim_logical_id for this task."""
    _ensure_task_exists(conn, task_id)
    rows = conn.execute(
        text(
            """
            SELECT cle.id, cle.claim_logical_id, cle.version_no, cle.state,
                   cle.support_scope, cle.user_provided_dependency,
                   cle.human_review_required, cle.human_review_status,
                   cle.transition_reason, cle.payload, cle.created_at
            FROM claim_ledger_entries cle
            JOIN logical_claims lc ON lc.id = cle.claim_logical_id
            JOIN (
              SELECT claim_logical_id, MAX(version_no) AS max_v
              FROM claim_ledger_entries
              GROUP BY claim_logical_id
            ) latest ON latest.claim_logical_id = cle.claim_logical_id
                   AND latest.max_v            = cle.version_no
            WHERE lc.task_id = :tid
            ORDER BY cle.created_at ASC, cle.id ASC
            LIMIT :limit
            """
        ),
        {"tid": task_id, "limit": limit},
    ).fetchall()
    return {"items": [_row_entry(r).model_dump(mode="json") for r in rows]}


@router.get("/claims/{claim_logical_id}/history")
def claim_history(
    claim_logical_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    _ensure_logical_claim_exists(conn, claim_logical_id)
    rows = conn.execute(
        text(
            """
            SELECT id, claim_logical_id, version_no, state,
                   support_scope, user_provided_dependency,
                   human_review_required, human_review_status,
                   transition_reason, payload, created_at
            FROM claim_ledger_entries
            WHERE claim_logical_id = :lc
            ORDER BY version_no ASC
            """
        ),
        {"lc": claim_logical_id},
    ).fetchall()
    return {"items": [_row_entry(r).model_dump(mode="json") for r in rows]}


@router.get("/claims/{claim_logical_id}/evidence")
def claim_evidence(
    claim_logical_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    _ensure_logical_claim_exists(conn, claim_logical_id)

    latest_row = conn.execute(
        text(
            """
            SELECT id, claim_logical_id, version_no, state,
                   support_scope, user_provided_dependency,
                   human_review_required, human_review_status,
                   transition_reason, payload, created_at
            FROM claim_ledger_entries
            WHERE claim_logical_id = :lc
            ORDER BY version_no DESC
            LIMIT 1
            """
        ),
        {"lc": claim_logical_id},
    ).first()
    latest = _row_entry(latest_row) if latest_row else None

    link_rows = conn.execute(
        text(
            """
            SELECT id, claim_logical_id, claim_ledger_entry_id,
                   evidence_span_id, retrieved_source_span_id,
                   link_role, created_at
            FROM claim_evidence_links
            WHERE claim_logical_id = :lc
            ORDER BY created_at ASC, id ASC
            """
        ),
        {"lc": claim_logical_id},
    ).fetchall()
    links = [_row_link(r) for r in link_rows]

    vr_rows = conn.execute(
        text(
            """
            SELECT id, claim_logical_id, claim_ledger_entry_id,
                   check_kind, check_name, outcome, score, evaluator_id,
                   payload, created_at
            FROM verification_records
            WHERE claim_logical_id = :lc
            ORDER BY created_at ASC, id ASC
            """
        ),
        {"lc": claim_logical_id},
    ).fetchall()
    vrs = [_row_vr(r) for r in vr_rows]

    aggregate = ClaimEvidenceRead(
        claim_logical_id=claim_logical_id,
        latest_entry=latest,
        evidence_links=links,
        verification_records=vrs,
    )
    return aggregate.model_dump(mode="json")