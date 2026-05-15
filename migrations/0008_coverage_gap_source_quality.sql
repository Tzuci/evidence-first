-- ============================================================================
-- 0008_coverage_gap_source_quality.sql
-- Evidence-First MVP-0 — Phase 8.7G Block 1.
--
-- Scope:
--   - Extend the CHECK constraint on coverage_gap_statements.kind to include
--     two new kinds:
--       * 'source_quality_block'
--       * 'source_quality_warning'
--
-- Why this migration exists:
--   PHASE_8_7G_PRE.md §7 decided that the Final Answer Gate, when consuming
--   source_quality_assessments in 8.7G-CODE, must emit coverage gaps that are
--   semantically distinct from the existing 'unverified_claim' /
--   'missing_evidence' / 'out_of_scope' / 'source_loss' kinds.
--
--   Re-using 'unverified_claim' for a source-quality-driven rejection would
--   conflate two orthogonal axes:
--     * unverified_claim         -> CVE-lite (text-level verification) failed
--                                   or no link to the latest verified entry.
--     * source_quality_block     -> CVE-lite passed AND the link is current,
--                                   but the source supporting the claim is
--                                   structurally inadequate (e.g.
--                                   overall_quality='unsuitable') or has been
--                                   contradicted by a stronger source.
--   These are two different reasons to reject publication. Mixing them on the
--   same 'kind' would force consumers (UI, downstream tools, evaluations) to
--   inspect 'details' to disambiguate, undermining the value of 'kind' as a
--   dimensional classifier.
--
-- Semantic invariants reinforced here:
--   - source quality != claim correctness. A claim can be false with a strong
--     source, or true with a weak one. 'source_quality_block' / '..._warning'
--     express a judgement on the SOURCE supporting the claim, not on the
--     truth of the claim itself.
--   - source quality != claim verification. CVE-lite verification (8.4) and
--     source quality (8.7) are computed by different services, written to
--     different tables (verification_records vs source_quality_assessments),
--     and consumed by different branches of the Final Answer Gate. The CVE
--     branch (priority) emits 'unverified_claim'; the source quality branch
--     (after CVE passes) emits 'source_quality_block' or 'source_quality_warning'.
--   - 'source_quality_block' is a hard rejection: the supporting source is
--     inadequate (overall_quality='unsuitable') or contradicted/conflicting
--     (contradiction_status in 'contradicted_by_stronger_source',
--     'conflicting_sources'). The Gate decision becomes 'rejected'.
--   - 'source_quality_warning' is non-blocking disclosure: the supporting
--     source is weak/unknown/unchecked, or the assessment is missing. The
--     Gate decision is NOT changed by warnings alone; the task can still
--     reach 'published'. Warnings exist so that consumers can observe the
--     residual uncertainty rather than have it silently hidden.
--   - These two new kinds belong to the same idempotency surface as the
--     existing kinds: the UNIQUE (draft_final_answer_id, kind, gap_key)
--     constraint on coverage_gap_statements remains the sole idempotency
--     key. A double invocation of the Gate on the same draft will not
--     duplicate source-quality gaps.
--
-- Out of scope (explicitly NOT done here):
--   - No new tables.
--   - No changes to source_quality_assessments.
--   - No changes to final_gate_reports.
--   - No append-only trigger on coverage_gap_statements (insert-only is
--     enforced operationally by the Final Answer Gate, not by DB trigger;
--     introducing a trigger is a separate concern outside this block).
--   - No changes to coverage_gap_statements.severity codomain
--     ({info, warn, block} from 0005 is sufficient).
--   - No backfill: existing rows already have kinds from the legacy codomain
--     and are unaffected.
--   - No modification to migration 0005_answers_gate.sql.
--
-- Constraint handling:
--   The original CHECK on coverage_gap_statements.kind was declared INLINE
--   in 0005_answers_gate.sql (not as a named table-level constraint).
--   Postgres auto-names such inline CHECKs as
--     '<table>_<column>_check' = 'coverage_gap_statements_kind_check'.
--   We use a DO block to robustly detect and DROP the actual CHECK that
--   constrains the 'kind' column, regardless of whether the name was
--   auto-generated or explicit, then re-create the CHECK with an explicit
--   name and the extended codomain. This pattern mirrors the defensive
--   redefinition of task_masters_status_check in 0005.
--
--   Discovery strategy: instead of pattern-matching the textual
--   pg_get_constraintdef() output (which is fragile because Postgres can
--   internally rewrite `kind IN (...)` as `kind = ANY (ARRAY[...])`, making
--   substring matches on "IN (" miss the constraint), we look up the
--   CHECK constraints whose `conkey` column-attribute array references the
--   `kind` column directly. This is exact, version-independent, and
--   independent of any internal textual representation.
--
-- Dependencies: 0001..0007.
-- ============================================================================

DO $$
DECLARE
    v_conname TEXT;
BEGIN
    -- Locate the CHECK constraint on coverage_gap_statements that targets
    -- the 'kind' column. With the schema defined in 0005, exactly one such
    -- CHECK exists. We resolve it via pg_constraint.conkey JOIN
    -- pg_attribute on attnum, which inspects the *structural* column
    -- association of the constraint rather than its textual definition.
    -- This is robust against Postgres' internal rewriting of
    -- `kind IN (...)` into `kind = ANY (ARRAY[...])`.
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
          '0008_coverage_gap_source_quality: could not locate the CHECK constraint on coverage_gap_statements.kind; expected one declared by 0005_answers_gate.sql';
    END IF;

    EXECUTE format(
      'ALTER TABLE coverage_gap_statements DROP CONSTRAINT %I',
      v_conname
    );
END
$$;

ALTER TABLE coverage_gap_statements
  ADD CONSTRAINT coverage_gap_statements_kind_check CHECK (kind IN (
    'unverified_claim',
    'missing_evidence',
    'out_of_scope',
    'source_loss',
    'source_quality_block',
    'source_quality_warning'
  ));

-- ============================================================================
-- END 0008_coverage_gap_source_quality.sql
-- ============================================================================
