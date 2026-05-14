-- ============================================================================
-- 0007_source_quality.sql
-- Evidence-First MVP-0 — Sprint 4 — Phase 8.7 Block B.
--
-- Scope:
--   - source_quality_assessments (APPEND-ONLY)
--
-- This migration introduces the FIRST module dedicated to evaluating the
-- QUALITY OF SOURCES that support claims in the Claim Ledger. It is purely
-- a SCHEMA migration: no service, no API, no worker, no UI, no gate policy
-- logic is introduced here. Those are scheduled for blocks 8.7C..8.7H.
--
-- Semantics — read carefully before extending or consuming this table:
--   - source_quality_assessments evaluates SOURCE QUALITY, not claim truth.
--   - It does NOT replace verification_records (which capture CVE-lite and
--     future text-level checks against evidence quotes).
--   - It does NOT mutate the Claim Ledger. claim_ledger_entries remains
--     append-only and untouched by this module.
--   - It does NOT implement any gate policy: the Final Answer Gate is
--     unchanged and continues to use the "verified-backed" rule from 8.4.
--   - It does NOT represent source loss. source_loss_events captures the
--     event of a source becoming unavailable / unrecognizable; this table
--     captures the structural quality of a source that IS still present.
--   - It evaluates EXACTLY ONE of (evidence_span_id, document_chunk_id,
--     document_id) per row, enforced by sqa_target_xor.
--
-- Phase 8.7B invariants honored:
--   - Migrations 0001..0006 are NOT modified.
--   - task_masters.status is NOT extended.
--   - claim_lineage.relation_kind is NOT extended.
--   - verification_records.check_kind is NOT extended.
--   - coverage_gap_statements.kind is NOT extended.
--   - The Final Answer Gate is NOT modified.
--   - The CVE-lite service is NOT modified.
--   - No retention/cleanup logic is introduced.
--   - No FK to policy_versions: policy provenance is captured by the
--     opaque textual pair (policy_name, policy_version). A future block
--     may upgrade this to a structured FK without rewriting history.
--
-- Append-only invariant:
--   source_quality_assessments is append-only via the shared
--   reject_modify_append_only() function defined in 0001_foundation.sql.
--   No row may be UPDATEd or DELETEd after INSERT. Versioning is achieved
--   by inserting a new row with the same target and an incremented
--   version_no.
--
-- Referential integrity:
--   - All FKs use ON DELETE RESTRICT.
--   - Exactly one of evidence_span_id / document_chunk_id / document_id
--     is NOT NULL per row (sqa_target_xor).
--
-- Idempotency and versioning:
--   - version_no is monotonically increasing per target, enforced by
--     three partial unique indexes (sqa_evidence_version_uq,
--     sqa_chunk_version_uq, sqa_document_version_uq), one per target
--     kind, restricted to rows where that target is NOT NULL.
--   - idempotency_key is unique per (target, key), enforced by three
--     partial unique indexes (sqa_evidence_idem_uq, sqa_chunk_idem_uq,
--     sqa_document_idem_uq), one per target kind. This allows the same
--     idempotency_key to appear on different targets without collision.
--
-- Dependencies: 0001..0006.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- SOURCE_QUALITY_ASSESSMENTS (APPEND-ONLY)
-- ---------------------------------------------------------------------------
CREATE TABLE source_quality_assessments (
  id                    UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id             UUID        NOT NULL REFERENCES tenants(id)              ON DELETE RESTRICT,
  project_id            UUID                 REFERENCES projects(id)             ON DELETE RESTRICT,

  -- Target of the assessment: exactly ONE of the three following columns
  -- must be NOT NULL. Enforced by sqa_target_xor.
  evidence_span_id      UUID                 REFERENCES evidence_spans(id)       ON DELETE RESTRICT,
  document_chunk_id     UUID                 REFERENCES document_chunks(id)      ON DELETE RESTRICT,
  document_id           UUID                 REFERENCES uploaded_documents(id)   ON DELETE RESTRICT,

  -- Monotonically increasing per (target_kind, target_id).
  version_no            INTEGER     NOT NULL,

  -- Quality dimensions (codomains fixed by CHECK; see PHASE_8_7_PLAN.md §2.1).
  source_type           TEXT        NOT NULL,
  source_role           TEXT        NOT NULL,
  authority_level       TEXT        NOT NULL,
  independence_level    TEXT        NOT NULL,
  freshness             TEXT        NOT NULL,
  relevance             TEXT        NOT NULL,
  extract_quality       TEXT        NOT NULL,
  contradiction_status  TEXT        NOT NULL,
  overall_quality       TEXT        NOT NULL,

  -- Internal confidence score in [0.0, 1.0]. NULL allowed when the
  -- evaluator declines to assert a confidence. NEVER intended as
  -- a single-number reputation score (see PHASE_8_7_PLAN.md §13).
  confidence            DOUBLE PRECISION,

  -- Provenance of the assessment.
  evaluator_name        TEXT        NOT NULL,
  evaluator_version     TEXT        NOT NULL,

  -- Policy provenance as opaque textual pair. A future block may
  -- replace this with a structured FK to a policy table without
  -- rewriting history.
  policy_name           TEXT        NOT NULL,
  policy_version        TEXT        NOT NULL,

  -- Consumer-level idempotency key (per target).
  idempotency_key       TEXT        NOT NULL,

  -- Opaque payload for evaluator-specific sub-criteria and explanations.
  payload               JSONB       NOT NULL DEFAULT '{}'::jsonb,

  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- ----- CHECK constraints --------------------------------------------------

  -- Exactly one of (evidence_span_id, document_chunk_id, document_id) NOT NULL.
  CONSTRAINT sqa_target_xor CHECK (
    ((evidence_span_id  IS NOT NULL)::int
   + (document_chunk_id IS NOT NULL)::int
   + (document_id       IS NOT NULL)::int) = 1
  ),

  CONSTRAINT sqa_version_no_chk CHECK (version_no >= 1),

  CONSTRAINT sqa_confidence_range CHECK (
    confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)
  ),

  CONSTRAINT sqa_source_type_chk CHECK (source_type IN (
    'user_document',
    'web_page',
    'academic_paper',
    'official_document',
    'database_record',
    'news_article',
    'blog',
    'forum',
    'unknown'
  )),

  CONSTRAINT sqa_source_role_chk CHECK (source_role IN (
    'primary',
    'secondary',
    'tertiary',
    'unclear'
  )),

  CONSTRAINT sqa_authority_level_chk CHECK (authority_level IN (
    'high',
    'medium',
    'low',
    'unknown'
  )),

  CONSTRAINT sqa_independence_level_chk CHECK (independence_level IN (
    'independent',
    'affiliated',
    'self_reported',
    'unknown'
  )),

  CONSTRAINT sqa_freshness_chk CHECK (freshness IN (
    'current',
    'recent',
    'stale',
    'undated',
    'not_time_sensitive'
  )),

  CONSTRAINT sqa_relevance_chk CHECK (relevance IN (
    'direct_support',
    'contextual_support',
    'weak_support',
    'irrelevant'
  )),

  CONSTRAINT sqa_extract_quality_chk CHECK (extract_quality IN (
    'exact_quote_match',
    'paraphrase_match',
    'partial_match',
    'quote_mismatch'
  )),

  CONSTRAINT sqa_contradiction_status_chk CHECK (contradiction_status IN (
    'no_known_contradiction',
    'contradicted_by_stronger_source',
    'conflicting_sources',
    'unchecked'
  )),

  CONSTRAINT sqa_overall_quality_chk CHECK (overall_quality IN (
    'strong',
    'adequate',
    'weak',
    'unsuitable',
    'unknown'
  ))
);

-- ---------------------------------------------------------------------------
-- Partial UNIQUE indexes — versioning per target
-- ---------------------------------------------------------------------------
-- A simple UNIQUE on nullable columns does not give the semantics we want
-- here: we need (target_id, version_no) to be unique ONLY when that target
-- is the active one for the row. The three partial indexes below provide
-- exactly that. Combined with sqa_target_xor (which guarantees exactly one
-- target is non-null per row), they form a clean per-target version index.

CREATE UNIQUE INDEX sqa_evidence_version_uq
  ON source_quality_assessments (evidence_span_id, version_no)
  WHERE evidence_span_id IS NOT NULL;

CREATE UNIQUE INDEX sqa_chunk_version_uq
  ON source_quality_assessments (document_chunk_id, version_no)
  WHERE document_chunk_id IS NOT NULL;

CREATE UNIQUE INDEX sqa_document_version_uq
  ON source_quality_assessments (document_id, version_no)
  WHERE document_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Partial UNIQUE indexes — idempotency per target
-- ---------------------------------------------------------------------------
-- The same idempotency_key may legitimately appear across different targets
-- (e.g. the evaluator may use a shared key prefix). Idempotency is enforced
-- strictly per target.

CREATE UNIQUE INDEX sqa_evidence_idem_uq
  ON source_quality_assessments (evidence_span_id, idempotency_key)
  WHERE evidence_span_id IS NOT NULL;

CREATE UNIQUE INDEX sqa_chunk_idem_uq
  ON source_quality_assessments (document_chunk_id, idempotency_key)
  WHERE document_chunk_id IS NOT NULL;

CREATE UNIQUE INDEX sqa_document_idem_uq
  ON source_quality_assessments (document_id, idempotency_key)
  WHERE document_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Lookup indexes
-- ---------------------------------------------------------------------------
CREATE INDEX sqa_tenant_created_idx
  ON source_quality_assessments (tenant_id, created_at);

CREATE INDEX sqa_project_created_idx
  ON source_quality_assessments (project_id, created_at)
  WHERE project_id IS NOT NULL;

CREATE INDEX sqa_evidence_created_idx
  ON source_quality_assessments (evidence_span_id, created_at)
  WHERE evidence_span_id IS NOT NULL;

CREATE INDEX sqa_chunk_created_idx
  ON source_quality_assessments (document_chunk_id, created_at)
  WHERE document_chunk_id IS NOT NULL;

CREATE INDEX sqa_document_created_idx
  ON source_quality_assessments (document_id, created_at)
  WHERE document_id IS NOT NULL;

CREATE INDEX sqa_overall_quality_idx
  ON source_quality_assessments (overall_quality);

CREATE INDEX sqa_source_role_idx
  ON source_quality_assessments (source_role);

CREATE INDEX sqa_freshness_idx
  ON source_quality_assessments (freshness);

-- ---------------------------------------------------------------------------
-- Append-only trigger
-- ---------------------------------------------------------------------------
-- Uses the shared reject_modify_append_only() function defined in
-- 0001_foundation.sql. Identical pattern to audit_records, evidence_spans,
-- claim_ledger_entries, final_answer_spans, final_gate_reports,
-- published_answer_lifecycle_events, source_loss_events, and
-- source_loss_propagation_records.

CREATE TRIGGER source_quality_assessments_append_only
BEFORE UPDATE OR DELETE ON source_quality_assessments
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ============================================================================
-- END 0007_source_quality.sql
-- ============================================================================
