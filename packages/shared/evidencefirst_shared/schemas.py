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


# ---------------------------------------------------------------------------
# Source Quality (Phase 8.7 — Block C, Shared schemas)
# ---------------------------------------------------------------------------
#
# These types describe the source_quality_assessments table introduced by
# migration 0007_source_quality.sql. The DB remains the source of truth for
# every constraint (XOR target, partial UNIQUE indexes, CHECK enums,
# append-only trigger, FK ON DELETE RESTRICT). The shared types below serve
# two purposes:
#
#   1. The constants ``SOURCE_QUALITY_*_VALUES`` are tuples that future
#      consumers (evaluator service in 8.7D, read API in 8.7F, frontend
#      bindings) can import to enumerate the codomain of each quality
#      dimension WITHOUT duplicating the strings. They mirror exactly the
#      CHECK constraints in 0007_source_quality.sql; any change to the
#      DB codomains MUST also be reflected here.
#
#   2. The ``SourceQuality*`` ``Literal`` aliases provide optional strict
#      typing for those consumers that want it (e.g. a future
#      SourceQualityAssessmentCreate in 8.7D). They are NOT used inside
#      ``SourceQualityAssessmentRead``, which keeps ``str`` for the
#      quality fields to remain consistent with all other Read models in
#      this file (ClaimLedgerEntryRead.state, VerificationRecordRead.outcome,
#      SourceLossEventRead.loss_kind, etc.). Forcing Literal on Read would
#      cause Pydantic to reject otherwise-valid DB rows if a future block
#      ever extends the codomain at DB level without first updating these
#      types.
#
# Semantic invariants (see PHASE_8_7_PLAN.md §3):
#   - source quality ≠ claim correctness
#   - source quality ≠ evidence support
#   - source quality ≠ verification outcome
#   - source quality ≠ source loss
#   - source quality ≠ final publication eligibility
#
# This is the FIRST shared schemas block for Source Quality. No Create
# model is introduced here on purpose: the producer side (evaluator
# service) does not yet exist, and the public API of 8.7F is read-only.
# A future block may add SourceQualityAssessmentCreate when (and only
# when) there is a real consumer for it.

# --- Codomain constants ----------------------------------------------------
# Mirror exactly the CHECK constraints in migrations/0007_source_quality.sql.
# Order is documentation only; tuples are unordered semantically.

SOURCE_QUALITY_SOURCE_TYPE_VALUES: tuple[str, ...] = (
    "user_document",
    "web_page",
    "academic_paper",
    "official_document",
    "database_record",
    "news_article",
    "blog",
    "forum",
    "unknown",
)

SOURCE_QUALITY_SOURCE_ROLE_VALUES: tuple[str, ...] = (
    "primary",
    "secondary",
    "tertiary",
    "unclear",
)

SOURCE_QUALITY_AUTHORITY_LEVEL_VALUES: tuple[str, ...] = (
    "high",
    "medium",
    "low",
    "unknown",
)

SOURCE_QUALITY_INDEPENDENCE_LEVEL_VALUES: tuple[str, ...] = (
    "independent",
    "affiliated",
    "self_reported",
    "unknown",
)

SOURCE_QUALITY_FRESHNESS_VALUES: tuple[str, ...] = (
    "current",
    "recent",
    "stale",
    "undated",
    "not_time_sensitive",
)

SOURCE_QUALITY_RELEVANCE_VALUES: tuple[str, ...] = (
    "direct_support",
    "contextual_support",
    "weak_support",
    "irrelevant",
)

SOURCE_QUALITY_EXTRACT_QUALITY_VALUES: tuple[str, ...] = (
    "exact_quote_match",
    "paraphrase_match",
    "partial_match",
    "quote_mismatch",
)

SOURCE_QUALITY_CONTRADICTION_STATUS_VALUES: tuple[str, ...] = (
    "no_known_contradiction",
    "contradicted_by_stronger_source",
    "conflicting_sources",
    "unchecked",
)

SOURCE_QUALITY_OVERALL_QUALITY_VALUES: tuple[str, ...] = (
    "strong",
    "adequate",
    "weak",
    "unsuitable",
    "unknown",
)


# --- Literal type aliases (optional strict typing for future consumers) ----
# NOT used inside SourceQualityAssessmentRead (which keeps ``str`` to remain
# consistent with the rest of the Read models in this file). Provided for
# future Create models, evaluator service signatures, and frontend bindings.

SourceQualitySourceType = Literal[
    "user_document",
    "web_page",
    "academic_paper",
    "official_document",
    "database_record",
    "news_article",
    "blog",
    "forum",
    "unknown",
]

SourceQualitySourceRole = Literal[
    "primary",
    "secondary",
    "tertiary",
    "unclear",
]

SourceQualityAuthorityLevel = Literal[
    "high",
    "medium",
    "low",
    "unknown",
]

SourceQualityIndependenceLevel = Literal[
    "independent",
    "affiliated",
    "self_reported",
    "unknown",
]

SourceQualityFreshness = Literal[
    "current",
    "recent",
    "stale",
    "undated",
    "not_time_sensitive",
]

SourceQualityRelevance = Literal[
    "direct_support",
    "contextual_support",
    "weak_support",
    "irrelevant",
]

SourceQualityExtractQuality = Literal[
    "exact_quote_match",
    "paraphrase_match",
    "partial_match",
    "quote_mismatch",
]

SourceQualityContradictionStatus = Literal[
    "no_known_contradiction",
    "contradicted_by_stronger_source",
    "conflicting_sources",
    "unchecked",
]

SourceQualityOverallQuality = Literal[
    "strong",
    "adequate",
    "weak",
    "unsuitable",
    "unknown",
]


# --- Read model ------------------------------------------------------------
class SourceQualityAssessmentRead(BaseModel):
    """Single source_quality_assessments row.

    Append-only by trigger. Each row evaluates EXACTLY ONE of
    (evidence_span_id, document_chunk_id, document_id); the other two
    are NULL. This XOR is enforced at DB level by the CHECK
    constraint sqa_target_xor.

    The quality dimensions surface as ``str`` to remain consistent with
    the rest of the Read models in this file. Their codomains are
    enforced at DB level by CHECK constraints and exposed at the Python
    level by the SOURCE_QUALITY_*_VALUES tuples above. Consumers that
    want strict typing may use the SourceQuality* Literal aliases.

    Semantic boundary (see PHASE_8_7_PLAN.md §3):
      - This row records the QUALITY of a source, not the truth of a
        claim, not the success of a CVE-lite verification, and not
        a source loss event.
      - confidence is an internal score in [0.0, 1.0] (or NULL); it is
        NEVER intended as a single-number reputation score and MUST NOT
        be consumed by the Final Answer Gate as a unique decision key.
    """
    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID | None
    evidence_span_id: uuid.UUID | None
    document_chunk_id: uuid.UUID | None
    document_id: uuid.UUID | None
    version_no: int
    source_type: str
    source_role: str
    authority_level: str
    independence_level: str
    freshness: str
    relevance: str
    extract_quality: str
    contradiction_status: str
    overall_quality: str
    confidence: float | None
    evaluator_name: str
    evaluator_version: str
    policy_name: str
    policy_version: str
    idempotency_key: str
    payload: dict[str, Any]
    created_at: _dt.datetime


# ---------------------------------------------------------------------------
# Claim Entailment (Phase 8.8A — Block SCHEMA, Shared schemas)
# ---------------------------------------------------------------------------
#
# These types describe the claim_entailment_checks table introduced by
# migration 0009_claim_entailment_checks.sql. The DB remains the source of
# truth for every constraint (CHECK enum on verdict, CHECK range on
# confidence, CHECK on version_no, composite FK against
# claim_ledger_entries(id, claim_logical_id), UNIQUE on
# (claim_ledger_entry_id, evidence_span_id, version_no), UNIQUE on
# (claim_ledger_entry_id, evidence_span_id, idempotency_key), append-only
# trigger, FK ON DELETE RESTRICT).
#
# Semantic invariants — read carefully before extending or consuming these
# types (see PHASE_8_8A_PRE.md §3, §4). These boundaries are NOT the same
# as those of Source Quality and MUST NOT be conflated:
#
#   - claim entailment ≠ claim correctness.
#     A verdict of 'entailed' means the quote supports the claim, NOT that
#     the claim is true in the world.
#
#   - claim entailment ≠ evidence support.
#     A claim_evidence_links row is a structural link; this table evaluates
#     whether the link is semantically justified by the quote.
#
#   - claim entailment ≠ CVE-lite verification.
#     CVE-lite (verification_records, check_kind='cve_lite') checks that
#     the quote is textually present in the document chunk and that the
#     quote_hash matches. Claim entailment answers a separate question:
#     given that the quote is present, does the quote IMPLY the claim?
#
#   - claim entailment ≠ source quality.
#     SourceQualityAssessmentRead judges the SOURCE that hosts the quote
#     (authority, freshness, independence). This Read model judges the
#     RELATION between the claim and the quote. An 'entailed' verdict
#     with overall_quality='unsuitable' is a real and distinguishable
#     situation; the two axes are orthogonal and consumed separately by
#     the Final Answer Gate.
#
#   - claim entailment ≠ contradiction detection.
#     A 'contradicted' verdict here is a LOCAL signal on a single
#     (claim, evidence_span) pair. Cross-source contradictions belong to
#     a future Contradiction Detector (Phase 8.8C) and are NOT this
#     table's responsibility.
#
# This is the FIRST shared schemas block for Claim Entailment. No Create
# model is introduced here on purpose: the producer side (mock entailment
# checker service in 8.8A-CODE) does not yet exist, and no API is yet
# exposed. A future block may add ClaimEntailmentCheckCreate when (and
# only when) there is a real consumer for it.

# --- Codomain constants ----------------------------------------------------
# Mirror exactly the CHECK constraint cec_verdict_chk in
# migrations/0009_claim_entailment_checks.sql. Any change to the DB
# codomain MUST also be reflected here.
#
# Note on naming: this tuple is named SOURCE_ENTAILMENT_VERDICT_VALUES to
# match the public name fixed by the Phase 8.8A-SHARED specification. The
# semantic content is claim ↔ quote entailment (see invariants above);
# the prefix 'SOURCE_' refers to the evidence quote acting as the source
# of entailment for the claim, NOT to source-quality concepts (which are
# evaluated separately by SourceQualityAssessmentRead).

SOURCE_ENTAILMENT_VERDICT_VALUES: tuple[str, ...] = (
    "entailed",
    "partially_supported",
    "not_supported",
    "contradicted",
    "uncertain",
)


# --- Literal type alias ----------------------------------------------------
# Strict alias for the entailment verdict. Used directly inside
# ClaimEntailmentCheckRead.verdict (deliberately stricter than the Source
# Quality Read model, which keeps ``str`` for its quality dimensions): the
# verdict codomain is fixed by migration 0009 and is invariant for the
# 8.8A phase.

ClaimEntailmentVerdict = Literal[
    "entailed",
    "partially_supported",
    "not_supported",
    "contradicted",
    "uncertain",
]


# --- Read model ------------------------------------------------------------
class ClaimEntailmentCheckRead(BaseModel):
    """Single claim_entailment_checks row.

    Append-only by trigger. Each row records one semantic-entailment
    judgement for the pair (claim_ledger_entry_id, evidence_span_id), at
    a given version_no.

    Semantic boundary (see PHASE_8_8A_PRE.md §3, §4):
      - 'entailed':              the quote semantically entails (or is
                                  equivalent to) the claim.
      - 'partially_supported':   the quote supports part of the claim but
                                  not all of it.
      - 'not_supported':         the quote does not entail the claim and
                                  does not contradict it.
      - 'contradicted':          the quote directly contradicts the claim
                                  on a single (claim, quote) pair; cross-
                                  source contradictions are out of scope.
      - 'uncertain':             the checker cannot decide.

    confidence is an internal score in [0.0, 1.0] (or NULL); it is
    NEVER intended as a single-number truth score and MUST NOT be
    consumed by the Final Answer Gate as a unique decision key.

    claim_logical_id is denormalized to match the composite FK on the
    underlying table (cec_entry_logical_consistency); it MUST equal the
    claim_logical_id of the referenced claim_ledger_entry row.
    """
    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID | None
    task_id: uuid.UUID
    claim_logical_id: uuid.UUID
    claim_ledger_entry_id: uuid.UUID
    evidence_span_id: uuid.UUID
    version_no: int
    verdict: ClaimEntailmentVerdict
    confidence: float | None
    checker_name: str
    checker_version: str
    policy_name: str
    policy_version: str
    idempotency_key: str
    rationale: str | None
    payload: dict[str, Any]
    created_at: _dt.datetime
