-- ============================================================================
-- 0009_claim_entailment_checks.sql
-- Evidence-First MVP-0 — Phase 8.8A Block SCHEMA.
--
-- Scope:
--   - claim_entailment_checks (APPEND-ONLY)
--
-- This migration introduces a NEW semantic axis in the anti-hallucination
-- stack: the verification of whether an evidence quote SEMANTICALLY ENTAILS
-- (or is compatible with) a claim, beyond mere textual presence.
--
-- Strict semantic invariants — read carefully before extending or consuming
-- this table (see PHASE_8_8A_PRE.md §3, §4):
--   - claim entailment != claim correctness.
--     An 'entailed' verdict means the quote supports the claim, NOT that
--     the claim is true in the world.
--   - claim entailment != evidence support.
--     A claim_evidence_links row is a structural link; this table evaluates
--     whether the link is semantically justified by the quote.
--   - claim entailment != CVE-lite verification.
--     CVE-lite (verification_records, check_kind='cve_lite') checks that
--     the quote is textually present in the document chunk and that the
--     quote_hash matches. This table answers the next question: GIVEN
--     that the quote is present, does the quote IMPLY the claim?
--   - claim entailment != source quality.
--     source_quality_assessments (0007) judges the SOURCE that hosts the
--     quote (authority, freshness, independence). This table judges the
--     RELATION between the claim and the quote. An 'entailed' verdict
--     with overall_quality='unsuitable' is a real and distinguishable
--     situation that the Final Answer Gate (in 8.8A-CODE, future block)
--     will handle on two separate axes.
--   - claim entailment != contradiction detection.
--     A 'contradicted' verdict here is a LOCAL signal on a single
--     (claim, evidence_span) pair. Cross-source contradictions belong to
--     a future Contradiction Detector (8.8C); they are NOT this table's
--     responsibility.
--   - claim entailment is RECORDED here but NOT enforced.
--     This migration does not modify the Final Answer Gate, the Claim
--     Ledger, source_quality_assessments, verification_records, or
--     coverage_gap_statements. It only creates the storage layer.
--
-- Phase 8.8A-SCHEMA invariants honored:
--   - Migrations 0001..0008 are NOT modified.
--   - task_masters.status is NOT extended.
--   - claim_lineage.relation_kind is NOT extended.
--   - verification_records.check_kind is NOT extended.
--   - coverage_gap_statements.kind is NOT extended (deferred to 0010 in a
--     future block).
--   - The Final Answer Gate is NOT modified.
--   - The Source Quality service is NOT modified.
--   - The CVE-lite service is NOT modified.
--   - No service, no orchestrator, no API, no consumer integration is
--     introduced by this block (deferred to 8.8A-CODE).
--   - No retention/cleanup logic is introduced.
--   - No FK to policy_versions: policy provenance is captured by the
--     opaque textual pair (policy_name, policy_version), mirroring the
--     pattern used by 0007_source_quality.sql.
--
-- Append-only invariant:
--   claim_entailment_checks is append-only via the shared
--   reject_modify_append_only() function defined in 0001_foundation.sql.
--   No row may be UPDATEd or DELETEd after INSERT. Re-evaluation (e.g.
--   when a future real entailment checker re-runs against the historical
--   corpus) is achieved by inserting a NEW row with the same target
--   pair and an incremented version_no.
--
-- Referential integrity:
--   - All FKs use ON DELETE RESTRICT.
--   - Granularity is the pair (claim_ledger_entry_id, evidence_span_id).
--   - claim_logical_id is denormalized so that the composite FK
--     cec_entry_logical_consistency can target the UNIQUE
--     cle_id_logical_uq (id, claim_logical_id) declared in 0004. This
--     prevents any row from referencing a (ledger_entry_id,
--     claim_logical_id) pair that belongs to two different logical
--     claims — same pattern used by cel_entry_logical_consistency in
--     0004 and fasc_entry_logical_consistency in 0005.
--
-- Idempotency and versioning:
--   - version_no is monotonically increasing per (claim_ledger_entry_id,
--     evidence_span_id), enforced by the UNIQUE index
--     cec_entry_span_version_uq.
--   - idempotency_key is unique per (claim_ledger_entry_id,
--     evidence_span_id), enforced by the UNIQUE index
--     cec_entry_span_idem_uq. Unlike 0007 (which needed PARTIAL UNIQUE
--     indexes because of its XOR-three-targets shape), this table has a
--     single target dimension so a plain UNIQUE index suffices.
--
-- Dependencies: 0001..0008.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- CLAIM_ENTAILMENT_CHECKS (APPEND-ONLY)
-- ---------------------------------------------------------------------------
CREATE TABLE claim_entailment_checks (
  id                       UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id                UUID        NOT NULL REFERENCES tenants(id)            ON DELETE RESTRICT,
  project_id               UUID                 REFERENCES projects(id)           ON DELETE RESTRICT,
  task_id                  UUID        NOT NULL REFERENCES task_masters(id)       ON DELETE RESTRICT,

  -- Granularity: each check is per (claim_ledger_entry, evidence_span) pair.
  -- claim_logical_id is denormalized to allow the composite FK
  -- cec_entry_logical_consistency to target the UNIQUE cle_id_logical_uq
  -- declared in 0004 on claim_ledger_entries(id, claim_logical_id). Same
  -- pattern as cel_entry_logical_consistency (0004) and
  -- fasc_entry_logical_consistency (0005).
  claim_logical_id         UUID        NOT NULL REFERENCES logical_claims(id)     ON DELETE RESTRICT,
  claim_ledger_entry_id    UUID        NOT NULL,
  evidence_span_id         UUID        NOT NULL REFERENCES evidence_spans(id)     ON DELETE RESTRICT,

  -- Monotonically increasing per (claim_ledger_entry_id, evidence_span_id).
  -- A re-run by a future real entailment checker appends a new version
  -- rather than mutating the previous one.
  version_no               INTEGER     NOT NULL,

  -- Semantic verdict (see PHASE_8_8A_PRE.md §3.1).
  --   'entailed':             quote semantically entails (or is equivalent to)
  --                           the claim.
  --   'partially_supported':  quote supports part of the claim but not all of
  --                           it.
  --   'not_supported':        quote does not entail the claim and does not
  --                           contradict it.
  --   'contradicted':         quote directly contradicts the claim on a
  --                           single (claim, quote) pair (cross-source
  --                           contradictions are out of scope).
  --   'uncertain':            checker cannot decide; typical default for
  --                           the mock checker MVP-0.
  verdict                  TEXT        NOT NULL,

  -- Internal confidence score in [0.0, 1.0]. NULL allowed when the
  -- checker declines to assert a confidence. NEVER intended as a
  -- single-number truth score.
  confidence               DOUBLE PRECISION,

  -- Provenance of the check.
  checker_name             TEXT        NOT NULL,
  checker_version          TEXT        NOT NULL,

  -- Policy provenance as opaque textual pair. A future block may upgrade
  -- this to a structured FK to a policy table without rewriting history.
  policy_name              TEXT        NOT NULL,
  policy_version           TEXT        NOT NULL,

  -- Consumer-level idempotency key (per target pair).
  idempotency_key          TEXT        NOT NULL,

  -- Human-readable rationale (optional; intended for UI/eval, not for
  -- gate logic). Length-bound enforcement, if needed, is the service's
  -- responsibility — not the schema's.
  rationale                TEXT,

  -- Opaque payload for checker-specific intermediate scoring, alignment
  -- tokens, prompt hashes, etc.
  payload                  JSONB       NOT NULL DEFAULT '{}'::jsonb,

  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- ----- CHECK constraints --------------------------------------------------

  CONSTRAINT cec_verdict_chk CHECK (verdict IN (
    'entailed',
    'partially_supported',
    'not_supported',
    'contradicted',
    'uncertain'
  )),

  CONSTRAINT cec_version_no_chk CHECK (version_no >= 1),

  CONSTRAINT cec_confidence_range CHECK (
    confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)
  ),

  -- Composite FK against claim_ledger_entries(id, claim_logical_id). The
  -- target UNIQUE cle_id_logical_uq is declared in 0004. This prevents
  -- a row from referencing a ledger entry whose own claim_logical_id
  -- does not match the claim_logical_id stored on this row.
  CONSTRAINT cec_entry_logical_consistency
    FOREIGN KEY (claim_ledger_entry_id, claim_logical_id)
    REFERENCES claim_ledger_entries(id, claim_logical_id)
);

-- ---------------------------------------------------------------------------
-- UNIQUE indexes — versioning + idempotency per (entry, span)
-- ---------------------------------------------------------------------------
-- One row per (claim_ledger_entry_id, evidence_span_id, version_no): a
-- re-evaluation against the same pair MUST bump version_no.
CREATE UNIQUE INDEX cec_entry_span_version_uq
  ON claim_entailment_checks (claim_ledger_entry_id, evidence_span_id, version_no);

-- One row per (claim_ledger_entry_id, evidence_span_id, idempotency_key).
-- Redelivery of the same consumer-level idempotency key against the same
-- target pair is absorbed by ON CONFLICT (caller side, in 8.8A-CODE).
CREATE UNIQUE INDEX cec_entry_span_idem_uq
  ON claim_entailment_checks (claim_ledger_entry_id, evidence_span_id, idempotency_key);

-- ---------------------------------------------------------------------------
-- Lookup indexes
-- ---------------------------------------------------------------------------
CREATE INDEX cec_task_idx
  ON claim_entailment_checks (task_id);

CREATE INDEX cec_claim_logical_idx
  ON claim_entailment_checks (claim_logical_id);

CREATE INDEX cec_evidence_span_idx
  ON claim_entailment_checks (evidence_span_id);

CREATE INDEX cec_verdict_idx
  ON claim_entailment_checks (verdict);

-- ---------------------------------------------------------------------------
-- Append-only trigger
-- ---------------------------------------------------------------------------
-- Uses the shared reject_modify_append_only() function defined in
-- 0001_foundation.sql. Identical pattern to audit_records, evidence_spans,
-- claim_ledger_entries, final_answer_spans, final_gate_reports,
-- published_answer_lifecycle_events, source_loss_events,
-- source_loss_propagation_records, and source_quality_assessments.
CREATE TRIGGER claim_entailment_checks_append_only
BEFORE UPDATE OR DELETE ON claim_entailment_checks
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ============================================================================
-- END 0009_claim_entailment_checks.sql
-- ============================================================================
