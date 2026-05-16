-- ============================================================================
-- 0010_coverage_gap_entailment.sql
-- Evidence-First MVP-0 — Phase 8.8A-GATE-SCHEMA Block.
--
-- Scope:
--   - Extend the CHECK constraint on coverage_gap_statements.kind to include
--     two new kinds tied to Claim Entailment:
--       * 'entailment_block'
--       * 'entailment_warning'
--
-- Why this migration exists:
--   PHASE_8_8A_GATE_PRE.md §6, §8 decided that the Final Answer Gate, when
--   consuming claim_entailment_checks in the upcoming 8.8A-GATE-CODE block,
--   must emit coverage gaps that are semantically distinct from CVE-lite
--   ('unverified_claim'), Source Quality ('source_quality_block' /
--   'source_quality_warning'), and the legacy kinds 'missing_evidence',
--   'out_of_scope', 'source_loss'. Re-using any of those kinds would
--   conflate orthogonal anti-hallucination axes and force consumers (UI,
--   downstream eval, audit) to inspect 'details' to disambiguate the
--   reason, undermining the dimensional value of 'kind'.
--
-- Semantic invariants reinforced here:
--   - claim entailment != claim correctness. An 'entailed' verdict means
--     the quote supports the claim, NOT that the claim is true.
--   - claim entailment != evidence support. The structural link
--     claim_evidence_links is necessary but not sufficient.
--   - claim entailment != CVE-lite verification. CVE-lite proves textual
--     presence and quote_hash match; entailment proves semantic support.
--   - claim entailment != source quality. The quality of the SOURCE
--     hosting the quote is a separate axis from whether the quote
--     supports the claim.
--   - 'entailment_block' is a hard rejection: the latest entailment check
--     for the (entry, span) pair returned verdict='contradicted'. The
--     Gate decision becomes 'rejected'.
--   - 'entailment_warning' is non-blocking disclosure: the latest verdict
--     is 'not_supported', 'partially_supported', 'uncertain', or the
--     check is missing. The Gate decision is NOT changed by warnings
--     alone; the task can still reach 'published'. Warnings exist so
--     that consumers can observe the residual semantic uncertainty
--     rather than have it silently hidden.
--   - These two new kinds belong to the same idempotency surface as the
--     existing kinds: the UNIQUE (draft_final_answer_id, kind, gap_key)
--     constraint on coverage_gap_statements remains the sole idempotency
--     key. A double invocation of the Gate on the same draft will not
--     duplicate entailment gaps.
--
-- Out of scope (explicitly NOT done here):
--   - No new tables.
--   - No changes to claim_entailment_checks.
--   - No changes to source_quality_assessments.
--   - No changes to final_gate_reports.
--   - No append-only trigger on coverage_gap_statements (consistent with
--     0008; insert-only is enforced operationally by the Final Answer
--     Gate, not by DB trigger).
--   - No changes to coverage_gap_statements.severity codomain
--     ({info, warn, block} from 0005 is sufficient).
--   - No backfill: existing rows already have kinds from the post-0008
--     codomain and are unaffected.
--   - No modification to migrations 0001..0009.
--   - No code change in apps/* or packages/*.
--
-- Constraint handling:
--   0008 already dropped the inline CHECK declared by 0005 and re-created
--   it under the explicit name 'coverage_gap_statements_kind_check'. We
--   re-use the same robust discovery pattern of 0008 (pg_constraint JOIN
--   pg_attribute on conkey/attname) rather than relying on the known
--   explicit name, so that this migration is independent of how 0008
--   stored the constraint and would work even if a future block renames
--   it. We then re-create the CHECK with the SAME explicit name
--   ('coverage_gap_statements_kind_check'), preserving 0008's discipline
--   of keeping the constraint nameable.
--
--   Discovery strategy: identical to 0008. We do NOT pattern-match on
--   pg_get_constraintdef() text because Postgres can internally rewrite
--   `kind IN (...)` as `kind = ANY (ARRAY[...])`. Instead we inspect the
--   constraint's `conkey` column-attribute array directly.
--
-- Dependencies: 0001..0009.
-- ============================================================================

DO $$
DECLARE
    v_conname TEXT;
BEGIN
    -- Locate the CHECK constraint on coverage_gap_statements that targets
    -- the 'kind' column. After 0008, exactly one such CHECK exists,
    -- nominally named 'coverage_gap_statements_kind_check'. We resolve it
    -- structurally via pg_constraint.conkey JOIN pg_attribute on attnum
    -- so this migration does not depend on the textual definition or on
    -- the constraint name being a specific string.
    SELECT c.conname
      INTO v_conname
      FROM pg_constraint c
      JOIN pg_attribute a
        ON a.attrelid = c.conrelid
       AND a.attnum   = ANY(c.conkey)
     WHERE c.conrelid = 'coverage_gap_statements'::regclass
       AND c.contype  = 'c'
       AND a.attname  = 'kind'
     ORDER BY c.conname
     LIMIT 1;

    IF v_conname IS NULL THEN
        RAISE EXCEPTION
          '0010_coverage_gap_entailment: could not locate the CHECK constraint on coverage_gap_statements.kind; expected one declared by 0005_answers_gate.sql and re-created by 0008_coverage_gap_source_quality.sql';
    END IF;

    EXECUTE format(
      'ALTER TABLE coverage_gap_statements DROP CONSTRAINT %I',
      v_conname
    );
END
$$;

ALTER TABLE coverage_gap_statements
  ADD CONSTRAINT coverage_gap_statements_kind_check CHECK (kind IN (
    -- pre-existing from 0005
    'unverified_claim',
    'missing_evidence',
    'out_of_scope',
    'source_loss',
    -- added by 0008
    'source_quality_block',
    'source_quality_warning',
    -- added by 0010 (this migration)
    'entailment_block',
    'entailment_warning'
  ));

-- ============================================================================
-- END 0010_coverage_gap_entailment.sql
-- ============================================================================
