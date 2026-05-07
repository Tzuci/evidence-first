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
# Claims (Phase 8.3, unchanged)
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


# ---------------------------------------------------------------------------
# Answers / Gate / Published (Phase 8.4)
# ---------------------------------------------------------------------------
class AgentRunRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    task_id: uuid.UUID
    run_kind: str
    attempt_no: int
    status: str
    started_at: _dt.datetime
    ended_at: _dt.datetime | None
    payload: dict[str, Any]


class FinalAnswerSpanRead(BaseModel):
    id: uuid.UUID
    draft_final_answer_id: uuid.UUID
    span_index: int
    char_start: int
    char_end: int
    span_text: str
    span_hash: str
    created_at: _dt.datetime


class FinalAnswerSpanClaimLinkRead(BaseModel):
    id: uuid.UUID
    final_answer_span_id: uuid.UUID
    claim_ledger_entry_id: uuid.UUID
    claim_logical_id: uuid.UUID
    link_role: str
    created_at: _dt.datetime


class CoverageGapStatementRead(BaseModel):
    id: uuid.UUID
    draft_final_answer_id: uuid.UUID
    kind: str
    severity: str
    gap_key: str
    details: dict[str, Any]
    created_at: _dt.datetime


class DraftFinalAnswerRead(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    version_no: int
    compiler_name: str
    compiler_version: str
    summary_text: str
    payload: dict[str, Any]
    created_at: _dt.datetime


class DraftFinalAnswerWithSpansRead(BaseModel):
    """Aggregate returned by GET /api/v1/tasks/{task_id}/draft."""
    draft: DraftFinalAnswerRead
    spans: list[FinalAnswerSpanRead]


class FinalGateReportRead(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    draft_final_answer_id: uuid.UUID
    decision: str
    reason_code: str
    payload: dict[str, Any]
    created_at: _dt.datetime
    coverage_gap_statements: list[CoverageGapStatementRead] = Field(default_factory=list)


class PublishedAnswerRead(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    draft_final_answer_id: uuid.UUID
    final_gate_report_id: uuid.UUID
    version_no: int
    content_hash: str
    payload: dict[str, Any]
    status: str
    published_at: _dt.datetime
    withdrawn_at: _dt.datetime | None
    superseded_at: _dt.datetime | None
    superseded_by_id: uuid.UUID | None


# ---------------------------------------------------------------------------
# Lifecycle / Source Loss (Phase 8.5 — Block 1)
# ---------------------------------------------------------------------------
class PublishedAnswerLifecycleEventRead(BaseModel):
    """Single published_answer_lifecycle_events row.

    Append-only by trigger. event_type is one of:
      - 'published'
      - 'withdrawal_requested'
      - 'withdrawn'
      - 'superseded'
    """
    id: uuid.UUID
    published_answer_id: uuid.UUID
    task_id: uuid.UUID
    event_type: str
    event_reason: str
    event_payload: dict[str, Any]
    requested_by: uuid.UUID | None
    idempotency_key: str
    created_at: _dt.datetime


class SourceLossEventRead(BaseModel):
    """Single source_loss_events row.

    Append-only by trigger. The canonical propagation granularity is
    evidence_span_id; document_chunk_id, document_version_id and document_id
    are reporting context only.

    loss_kind is one of:
      - 'source_deleted'
      - 'source_access_lost'
      - 'quote_mismatch'
      - 'document_replaced'
      - 'policy_retraction'
    """
    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID | None
    task_id: uuid.UUID | None
    evidence_span_id: uuid.UUID
    document_chunk_id: uuid.UUID | None
    document_version_id: uuid.UUID | None
    document_id: uuid.UUID | None
    loss_kind: str
    loss_reason: str
    detected_by: str
    event_payload: dict[str, Any]
    idempotency_key: str
    created_at: _dt.datetime


class SourceLossPropagationRecordRead(BaseModel):
    """Single source_loss_propagation_records row.

    Append-only by trigger. propagation_kind is one of:
      - 'claim_marked_unverifiable'
      - 'published_answer_impacted'
      - 'no_claims_impacted'
      - 'no_active_published_answers_impacted'

    status is one of:
      - 'recorded'
      - 'skipped'
      - 'failed'
    """
    id: uuid.UUID
    source_loss_event_id: uuid.UUID
    claim_logical_id: uuid.UUID | None
    old_claim_ledger_entry_id: uuid.UUID | None
    new_claim_ledger_entry_id: uuid.UUID | None
    published_answer_id: uuid.UUID | None
    propagation_kind: str
    status: str
    details: dict[str, Any]
    created_at: _dt.datetime
