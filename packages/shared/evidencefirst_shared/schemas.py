"""Minimal pydantic schemas shared by api/worker."""
from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
class HealthReady(BaseModel):
    db: Literal["ok", "fail"]
    redis: Literal["ok", "fail"]
    storage: Literal["ok", "fail"]


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------
class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    mode_default: Literal["closed_corpus", "verified_web", "hybrid"] = "closed_corpus"


class ProjectRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    mode_default: str | None
    created_by: uuid.UUID | None
    created_at: _dt.datetime


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
class TaskCreate(BaseModel):
    project_id: uuid.UUID
    objective: str = Field(min_length=1)
    mode: Literal["closed_corpus"] = "closed_corpus"
    policy: dict[str, Any] = Field(default_factory=dict)
    document_ids: list[uuid.UUID] = Field(default_factory=list)


class TaskRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    mode: str
    objective: str
    status: str
    policy: dict[str, Any]
    created_at: _dt.datetime
    updated_at: _dt.datetime


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
class DocumentRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    storage_object_id: uuid.UUID
    filename: str
    content_hash: str
    mime_type: str | None
    size_bytes: int
    tier: str
    language: str
    created_by: uuid.UUID | None
    created_at: _dt.datetime


class DocumentVersionRead(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    version_no: int
    version_kind: str
    has_inline_text: bool
    text_hash: str | None
    created_at: _dt.datetime


class DocumentChunkRead(BaseModel):
    id: uuid.UUID
    document_version_id: uuid.UUID
    chunk_index: int
    char_start: int
    char_end: int
    text_hash: str
    inline_text: str
    created_at: _dt.datetime


class TaskDocumentRead(BaseModel):
    task_id: uuid.UUID
    document_id: uuid.UUID
    role: str
    position: int
    filename: str
    content_hash: str


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
class AuditEventRead(BaseModel):
    id: uuid.UUID
    chain_scope: str
    scope_id: uuid.UUID
    chain_seq: int
    event_type: str
    actor_type: str
    actor_id: str
    related_entity_type: str | None
    related_entity_id: uuid.UUID | None
    redacted_payload: dict[str, Any]
    event_hash_hex: str
    previous_event_hash_hex: str | None
    created_at: _dt.datetime


# ---------------------------------------------------------------------------
# Claims (Fase 8.3)
# ---------------------------------------------------------------------------
class RawClaimRead(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    logical_claim_id: uuid.UUID
    document_chunk_id: uuid.UUID
    evidence_span_id: uuid.UUID
    raw_text: str
    extractor_name: str
    extractor_version: str
    created_at: _dt.datetime


class ClassifiedClaimRead(BaseModel):
    id: uuid.UUID
    raw_claim_id: uuid.UUID
    logical_claim_id: uuid.UUID
    claim_type: str
    domain_tag: str
    qualifiers: dict[str, Any]
    classifier_name: str
    classifier_version: str
    created_at: _dt.datetime


class ClaimLedgerEntryRead(BaseModel):
    id: uuid.UUID
    claim_logical_id: uuid.UUID
    version_no: int
    state: str
    support_scope: str
    user_provided_dependency: str
    human_review_required: bool
    human_review_status: str | None
    transition_reason: str | None
    payload: dict[str, Any]
    created_at: _dt.datetime


class VerificationRecordRead(BaseModel):
    id: uuid.UUID
    claim_logical_id: uuid.UUID
    claim_ledger_entry_id: uuid.UUID
    check_kind: str
    check_name: str
    outcome: str
    score: float | None
    evaluator_id: str
    payload: dict[str, Any]
    created_at: _dt.datetime


class ClaimEvidenceLinkRead(BaseModel):
    id: uuid.UUID
    claim_logical_id: uuid.UUID
    claim_ledger_entry_id: uuid.UUID
    evidence_span_id: uuid.UUID | None
    retrieved_source_span_id: uuid.UUID | None
    link_role: str
    created_at: _dt.datetime


class ClaimEvidenceRead(BaseModel):
    """Aggregate read returned by GET /api/v1/claims/{logical_id}/evidence."""
    claim_logical_id: uuid.UUID
    latest_entry: ClaimLedgerEntryRead | None
    evidence_links: list[ClaimEvidenceLinkRead]
    verification_records: list[VerificationRecordRead]