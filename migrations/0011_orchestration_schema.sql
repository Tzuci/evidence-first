-- ============================================================================
-- 0011_orchestration_schema.sql
-- Evidence-First MVP-0 — Phase ORCH-SCHEMA-A.
--
-- Scope:
--   Persistent, minimal schema foundation for the future multi-AI
--   orchestration core. This migration is DDL-ONLY and strictly ADDITIVE:
--   it introduces 19 NEW tables and touches nothing that already exists.
--
--   It does NOT introduce any service, worker, API route, UI surface, real
--   AI provider, local LLM, web retrieval, or parallel gate. Every table
--   created here is EMPTY after the migration: no runner populates it. The
--   phase remains "minimal" because it adds schema only — no application code.
--
-- What this migration is the storage layer for (design: PHASE_ORCH_SCHEMA_PRE.md):
--   - the Master Prompt and its immutable snapshots;
--   - configurable AI agents (role prompts, configs, config snapshots);
--   - orchestration runs and their append-only event log;
--   - concrete agent executions, their messages and outputs;
--   - source candidates proposed/cited by agents, and their resolution and
--     verification toward real evidence spans;
--   - token budgets (pre-run configuration) and real token usage records;
--   - provider invocations as auditable facts;
--   - the candidate synthesis and its join links toward agent outputs,
--     evidence spans and the existing Claim Ledger.
--
-- ----------------------------------------------------------------------------
-- ADDITIVITY AND IMMUTABILITY (PHASE_ORCH_SCHEMA_PRE.md §17.3, §18.1;
-- docs/migration_plan.md "Regola d'oro"):
--   - Migrations 0001..0010 are applied and immutable. This migration does
--     NOT modify any of them.
--   - The placeholder tables created by 0005 (agent_runs, agent_outputs,
--     truncation_events, continuation_attempts) are NOT reused, NOT
--     redefined, NOT removed. agent_runs (0005) keeps its compiler/gate
--     semantics (CHECK run_kind IN ('compile_draft','final_answer_gate'))
--     untouched. The multi-AI agent family uses NEW names prefixed
--     'orchestration_agent_*' to avoid any semantic collision.
--   - No new function is created: the append-only trigger reuses the shared
--     reject_modify_append_only() defined in 0001_foundation.sql, and the
--     set_updated_at() trigger reuses the shared function from 0001.
--   - No PostgreSQL ENUM type is created: codomains are CHECK constraints on
--     TEXT, consistent with every prior migration.
--
-- ----------------------------------------------------------------------------
-- SEMANTIC INVARIANTS (read before extending or consuming these tables):
--   - A source_candidate is NOT an evidence_span. A source proposed or cited
--     by an agent is a CANDIDATE that must be resolved and verified before it
--     can contribute to the Claim Ledger and the Final Answer Gate.
--     source_candidates therefore carries NO foreign key to evidence_spans,
--     claim_evidence_links or logical_claims. The bridge into real evidence
--     runs exclusively through source_verifications.evidence_span_id.
--   - source_verifications checks textual presence / quote / hash of a
--     resolved source; it can produce or attach an evidence_span. It does NOT
--     judge semantic support of a claim (that axis is claim_entailment_checks,
--     0009, further downstream) and does NOT judge source quality.
--   - A candidate_synthesis is NOT a published_answers row. It is a candidate
--     answer; it becomes publishable only after Claim Extraction, Evidence
--     Binding and the existing Final Answer Gate. No FK or column here allows
--     publication while skipping the gate.
--   - This migration introduces NO parallel gate. Any (nullable) reference to
--     final_gate_reports is a link to the existing gate's output, never a new
--     decision authority.
--   - provider_invocations has NO column for API keys, secrets, tokens of
--     authentication or any credential. Authentication lives inside the
--     provider implementation, never in this audit table.
--   - token_budgets is PRE-RUN configuration: it can reference tenant,
--     master_prompt and agent_config, but deliberately carries NO reference
--     to orchestration_runs. The real per-run consumption is represented by
--     token_usage_records (an append-only fact table).
--
-- ----------------------------------------------------------------------------
-- APPEND-ONLY MODEL:
--   14 fact tables receive the shared reject_modify_append_only() trigger
--   (UPDATE/DELETE rejected): master_prompt_versions, agent_config_snapshots,
--   orchestration_events, orchestration_agent_runs,
--   orchestration_agent_messages, orchestration_agent_outputs,
--   source_candidates, source_resolutions, source_verifications,
--   provider_invocations, token_usage_records, candidate_syntheses,
--   synthesis_source_links, synthesis_claim_links.
--
--   4 configuration tables are mutable and do NOT receive the append-only
--   trigger: master_prompts, agent_configs, token_budgets,
--   agent_role_prompts.
--
--   Note on agent_role_prompts: it carries a version_no and is conceptually a
--   versioned catalogue (a new revision is a new row). It is nonetheless left
--   WITHOUT the append-only trigger and modelled as mutable configuration:
--   PHASE_ORCH_SCHEMA_PRE.md §18.2 Gruppo 1 places it among the configuration
--   tables and notes that its pre-consumption mutability is governed at the
--   application layer, while a UNIQUE on (tenant_id, name, version_no)
--   provides the catalogue versioning. Keeping it mutable lets a draft role
--   prompt be corrected before any run consumes it; the immutable record of
--   what a run actually used is the agent_config_snapshots payload, which is
--   append-only.
--
--   orchestration_runs is NOT given an append-only trigger: it carries a
--   materialized 'status' column (and the terminal fields completed_at /
--   failure_reason / started_at) that must be updatable. This is the single
--   admitted exception (PHASE_ORCH_SCHEMA_PRE.md §8). EVERY status transition
--   of an orchestration_runs row MUST be accompanied by a corresponding
--   append-only row in orchestration_events: the events are the source of
--   truth, the status column is an ergonomic materialized view of the latest
--   transition. No custom trigger is created to enforce this exception; it is
--   an operational invariant honoured by the future worker.
--
-- ----------------------------------------------------------------------------
-- COMMON CONVENTIONS:
--   - Primary keys: id UUID DEFAULT app_new_uuid() (helper from 0001).
--   - created_at TIMESTAMPTZ NOT NULL DEFAULT NOW().
--   - updated_at only on the configuration tables, with the shared
--     set_updated_at() trigger.
--   - All foreign keys are ON DELETE RESTRICT (no ON DELETE CASCADE on fact
--     tables), consistent with the discipline of 0001..0010.
--   - CHECK and UNIQUE constraints are named.
--   - CREATE TABLE order follows FK dependencies so that no table is created
--     before a table it references; there is no real cyclic dependency.
--
-- Dependencies: 0001..0010.
-- ============================================================================


-- ============================================================================
-- AREA 1 — CONFIGURATION (mutable; no append-only trigger)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- MASTER_PROMPTS (configuration, mutable)
-- The primary product input: the user's question / problem / objective.
-- ---------------------------------------------------------------------------
CREATE TABLE master_prompts (
  id          UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id   UUID        NOT NULL REFERENCES tenants(id)  ON DELETE RESTRICT,
  project_id  UUID                 REFERENCES projects(id) ON DELETE RESTRICT,
  created_by  UUID                 REFERENCES users(id)    ON DELETE RESTRICT,
  prompt_text TEXT        NOT NULL,
  title       TEXT,
  status      TEXT        NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT master_prompts_status_chk
    CHECK (status IN ('draft', 'ready', 'archived'))
);

CREATE INDEX master_prompts_tenant_idx ON master_prompts (tenant_id);

CREATE TRIGGER master_prompts_set_updated_at
BEFORE UPDATE ON master_prompts
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- AGENT_ROLE_PROMPTS (configuration, mutable; versioned catalogue)
-- Role and prompt assignable to an agent. version_no provides catalogue
-- versioning via UNIQUE; pre-consumption mutability is application-governed.
-- ---------------------------------------------------------------------------
CREATE TABLE agent_role_prompts (
  id                  UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  name                TEXT        NOT NULL,
  role_category       TEXT        NOT NULL,
  system_prompt_text  TEXT        NOT NULL,
  task_prompt_text    TEXT        NOT NULL,
  version_no          INTEGER     NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT agent_role_prompts_role_category_chk
    CHECK (role_category IN ('researcher', 'critic', 'synthesizer', 'generic')),
  CONSTRAINT agent_role_prompts_version_no_chk
    CHECK (version_no >= 1),
  CONSTRAINT agent_role_prompts_name_version_uq
    UNIQUE (tenant_id, name, version_no)
);

CREATE INDEX agent_role_prompts_tenant_idx ON agent_role_prompts (tenant_id);

-- ---------------------------------------------------------------------------
-- AGENT_CONFIGS (configuration, mutable)
-- The "who" and "how" of a participant in the orchestration.
-- ---------------------------------------------------------------------------
CREATE TABLE agent_configs (
  id                    UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id             UUID        NOT NULL REFERENCES tenants(id)            ON DELETE RESTRICT,
  master_prompt_id      UUID        NOT NULL REFERENCES master_prompts(id)     ON DELETE RESTRICT,
  agent_role_prompt_id  UUID        NOT NULL REFERENCES agent_role_prompts(id) ON DELETE RESTRICT,
  name                  TEXT        NOT NULL,
  provider              TEXT        NOT NULL,
  model                 TEXT        NOT NULL,
  task_summary          TEXT,
  output_contract       JSONB       NOT NULL DEFAULT '{}'::jsonb,
  constraints           JSONB       NOT NULL DEFAULT '{}'::jsonb,
  temperature_config    JSONB       NOT NULL DEFAULT '{}'::jsonb,
  retry_policy          JSONB       NOT NULL DEFAULT '{}'::jsonb,
  source_access         JSONB       NOT NULL DEFAULT '{}'::jsonb,
  reviewer_flag         BOOLEAN     NOT NULL DEFAULT FALSE,
  synthesizer_flag      BOOLEAN     NOT NULL DEFAULT FALSE,
  order_index           INTEGER     NOT NULL DEFAULT 0,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT agent_configs_order_index_chk
    CHECK (order_index >= 0)
);

CREATE INDEX agent_configs_tenant_idx        ON agent_configs (tenant_id);
CREATE INDEX agent_configs_master_prompt_idx ON agent_configs (master_prompt_id);

CREATE TRIGGER agent_configs_set_updated_at
BEFORE UPDATE ON agent_configs
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- TOKEN_BUDGETS (configuration, mutable; pre-run only)
-- Pre-run token/cost limit. Deliberately carries NO orchestration_run_id:
-- the real per-run consumption is represented by token_usage_records.
-- ---------------------------------------------------------------------------
CREATE TABLE token_budgets (
  id                UUID             PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id         UUID             NOT NULL REFERENCES tenants(id)        ON DELETE RESTRICT,
  master_prompt_id  UUID                      REFERENCES master_prompts(id) ON DELETE RESTRICT,
  agent_config_id   UUID                      REFERENCES agent_configs(id)  ON DELETE RESTRICT,
  budget_level      TEXT             NOT NULL,
  token_limit       BIGINT           NOT NULL,
  cost_limit        DOUBLE PRECISION,
  overflow_policy   TEXT             NOT NULL,
  created_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
  CONSTRAINT token_budgets_budget_level_chk
    CHECK (budget_level IN ('per_orchestration', 'per_agent', 'per_pass')),
  CONSTRAINT token_budgets_overflow_policy_chk
    CHECK (overflow_policy IN ('hard_stop', 'warn')),
  CONSTRAINT token_budgets_token_limit_chk
    CHECK (token_limit >= 0),
  -- Conditional CHECK on the sqa_target_xor model (0007): a per_agent budget
  -- must name the agent it constrains.
  CONSTRAINT token_budgets_tb_level_target
    CHECK (budget_level <> 'per_agent' OR agent_config_id IS NOT NULL)
);

CREATE INDEX token_budgets_tenant_idx ON token_budgets (tenant_id);

CREATE TRIGGER token_budgets_set_updated_at
BEFORE UPDATE ON token_budgets
FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ============================================================================
-- AREA 2 — SNAPSHOT (append-only)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- MASTER_PROMPT_VERSIONS (append-only snapshot)
-- Immutable version of a master prompt's text, consumed by a run.
-- ---------------------------------------------------------------------------
CREATE TABLE master_prompt_versions (
  id                UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  master_prompt_id  UUID        NOT NULL REFERENCES master_prompts(id) ON DELETE RESTRICT,
  version_no        INTEGER     NOT NULL,
  prompt_text       TEXT        NOT NULL,
  prompt_text_hash  TEXT        NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT master_prompt_versions_version_no_chk
    CHECK (version_no >= 1),
  CONSTRAINT master_prompt_versions_version_uq
    UNIQUE (master_prompt_id, version_no)
);

CREATE INDEX master_prompt_versions_prompt_idx
  ON master_prompt_versions (master_prompt_id);

CREATE TRIGGER master_prompt_versions_append_only
BEFORE UPDATE OR DELETE ON master_prompt_versions
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();


-- ============================================================================
-- AREA 3 — RUN ROOT (orchestration_runs created before tables that
-- reference it; created after master_prompt_versions which it references)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- ORCHESTRATION_RUNS (root; append-only in fact fields, materialized status)
-- The root of a single multi-AI orchestration execution.
--
-- NO append-only trigger: the status / started_at / completed_at /
-- failure_reason fields must be updatable as the run progresses. This is the
-- single admitted exception to the append-only model for this migration
-- (PHASE_ORCH_SCHEMA_PRE.md §8). Every status transition MUST also append a
-- corresponding orchestration_events row — that append-only event log is the
-- source of truth, the status column is a materialized convenience. No custom
-- trigger enforces this; it is an operational invariant of the future worker.
-- ---------------------------------------------------------------------------
CREATE TABLE orchestration_runs (
  id                        UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id                 UUID        NOT NULL REFERENCES tenants(id)                ON DELETE RESTRICT,
  project_id                UUID                 REFERENCES projects(id)               ON DELETE RESTRICT,
  master_prompt_version_id  UUID        NOT NULL REFERENCES master_prompt_versions(id) ON DELETE RESTRICT,
  final_gate_report_id      UUID                 REFERENCES final_gate_reports(id)      ON DELETE RESTRICT,
  mode                      TEXT        NOT NULL,
  execution_mode            TEXT        NOT NULL,
  status                    TEXT        NOT NULL,
  master_prompt_text_hash   TEXT        NOT NULL,
  bounding_parameters       JSONB       NOT NULL DEFAULT '{}'::jsonb,
  idempotency_key           TEXT        NOT NULL,
  policy_name               TEXT        NOT NULL,
  policy_version            TEXT        NOT NULL,
  is_mock                   BOOLEAN     NOT NULL DEFAULT TRUE,
  started_at                TIMESTAMPTZ,
  completed_at              TIMESTAMPTZ,
  failure_reason            TEXT,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT orchestration_runs_mode_chk
    CHECK (mode IN ('multi_ai_orchestration', 'local_evidence', 'hybrid')),
  CONSTRAINT orchestration_runs_execution_mode_chk
    CHECK (execution_mode IN ('independent', 'coordinated')),
  CONSTRAINT orchestration_runs_status_chk
    CHECK (status IN (
      'pending',
      'running',
      'waiting_source_resolution',
      'synthesizing',
      'submitted_to_gate',
      'completed',
      'failed',
      'cancelled'
    )),
  CONSTRAINT orchestration_runs_idempotency_uq
    UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX orchestration_runs_tenant_created_idx
  ON orchestration_runs (tenant_id, created_at);
CREATE INDEX orchestration_runs_master_prompt_version_idx
  ON orchestration_runs (master_prompt_version_id);


-- ============================================================================
-- AREA 4 — SNAPSHOT depending on the run; EVENTS
-- ============================================================================

-- ---------------------------------------------------------------------------
-- AGENT_CONFIG_SNAPSHOTS (append-only snapshot)
-- Immutable snapshot of one agent's configuration at run start.
-- ---------------------------------------------------------------------------
CREATE TABLE agent_config_snapshots (
  id                          UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  orchestration_run_id        UUID        NOT NULL REFERENCES orchestration_runs(id) ON DELETE RESTRICT,
  agent_config_id             UUID        NOT NULL REFERENCES agent_configs(id)      ON DELETE RESTRICT,
  snapshot_payload            JSONB       NOT NULL,
  agent_role_prompt_text_hash TEXT        NOT NULL,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT agent_config_snapshots_run_config_uq
    UNIQUE (orchestration_run_id, agent_config_id)
);

CREATE INDEX agent_config_snapshots_run_idx
  ON agent_config_snapshots (orchestration_run_id);
CREATE INDEX agent_config_snapshots_config_idx
  ON agent_config_snapshots (agent_config_id);

CREATE TRIGGER agent_config_snapshots_append_only
BEFORE UPDATE OR DELETE ON agent_config_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ---------------------------------------------------------------------------
-- ORCHESTRATION_EVENTS (append-only fact)
-- The append-only log of run transitions and events.
-- ---------------------------------------------------------------------------
CREATE TABLE orchestration_events (
  id                    UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  orchestration_run_id  UUID        NOT NULL REFERENCES orchestration_runs(id) ON DELETE RESTRICT,
  event_type            TEXT        NOT NULL,
  sequence_no           INTEGER     NOT NULL,
  related_entity_type   TEXT,
  related_entity_id     UUID,
  event_payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
  idempotency_key       TEXT        NOT NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT orchestration_events_event_type_chk
    CHECK (event_type IN (
      'run_created',
      'agent_run_started',
      'agent_run_completed',
      'agent_run_failed',
      'source_candidate_created',
      'source_resolution_started',
      'source_resolution_completed',
      'source_verification_completed',
      'synthesis_created',
      'submitted_to_gate',
      'gate_completed',
      'token_budget_exceeded',
      'run_cancelled',
      'run_failed'
    )),
  CONSTRAINT orchestration_events_sequence_no_chk
    CHECK (sequence_no >= 0),
  CONSTRAINT orchestration_events_run_sequence_uq
    UNIQUE (orchestration_run_id, sequence_no),
  CONSTRAINT orchestration_events_run_type_idem_uq
    UNIQUE (orchestration_run_id, event_type, idempotency_key)
);

CREATE TRIGGER orchestration_events_append_only
BEFORE UPDATE OR DELETE ON orchestration_events
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();


-- ============================================================================
-- AREA 5 — AGENT FACTS (append-only)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- ORCHESTRATION_AGENT_RUNS (append-only fact)
-- Concrete execution of a single agent inside a run. Prefixed name: it does
-- NOT reuse and does NOT alter the 0005 placeholder agent_runs.
-- ---------------------------------------------------------------------------
CREATE TABLE orchestration_agent_runs (
  id                        UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  orchestration_run_id      UUID        NOT NULL REFERENCES orchestration_runs(id)     ON DELETE RESTRICT,
  agent_config_snapshot_id  UUID        NOT NULL REFERENCES agent_config_snapshots(id) ON DELETE RESTRICT,
  status                    TEXT        NOT NULL,
  attempt_no                INTEGER     NOT NULL DEFAULT 1,
  is_mock                   BOOLEAN     NOT NULL DEFAULT TRUE,
  error_code                TEXT,
  failure_reason            TEXT,
  started_at                TIMESTAMPTZ,
  completed_at              TIMESTAMPTZ,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- QA correction (PARTE 2/2 §1): status includes 'cancelled'.
  CONSTRAINT orchestration_agent_runs_status_chk
    CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
  CONSTRAINT orchestration_agent_runs_attempt_no_chk
    CHECK (attempt_no >= 1),
  CONSTRAINT orchestration_agent_runs_attempt_uq
    UNIQUE (orchestration_run_id, agent_config_snapshot_id, attempt_no)
);

CREATE INDEX orchestration_agent_runs_run_idx
  ON orchestration_agent_runs (orchestration_run_id);

CREATE TRIGGER orchestration_agent_runs_append_only
BEFORE UPDATE OR DELETE ON orchestration_agent_runs
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ---------------------------------------------------------------------------
-- ORCHESTRATION_AGENT_MESSAGES (append-only fact)
-- Provider-level messages exchanged/produced during an agent run.
-- ---------------------------------------------------------------------------
CREATE TABLE orchestration_agent_messages (
  id                    UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  agent_run_id          UUID        NOT NULL REFERENCES orchestration_agent_runs(id) ON DELETE RESTRICT,
  orchestration_run_id  UUID                 REFERENCES orchestration_runs(id)       ON DELETE RESTRICT,
  message_role          TEXT        NOT NULL,
  content_text          TEXT,
  content_hash          TEXT,
  sequence_no           INTEGER     NOT NULL,
  tokens                INTEGER,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT orchestration_agent_messages_role_chk
    CHECK (message_role IN ('system', 'user', 'assistant', 'review', 'tool')),
  CONSTRAINT orchestration_agent_messages_sequence_no_chk
    CHECK (sequence_no >= 0),
  CONSTRAINT orchestration_agent_messages_run_sequence_uq
    UNIQUE (agent_run_id, sequence_no)
);

CREATE INDEX orchestration_agent_messages_run_idx
  ON orchestration_agent_messages (agent_run_id);

CREATE TRIGGER orchestration_agent_messages_append_only
BEFORE UPDATE OR DELETE ON orchestration_agent_messages
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ---------------------------------------------------------------------------
-- ORCHESTRATION_AGENT_OUTPUTS (append-only fact)
-- Structured, consumable output produced by an agent run.
-- ---------------------------------------------------------------------------
CREATE TABLE orchestration_agent_outputs (
  id                  UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  agent_run_id        UUID        NOT NULL REFERENCES orchestration_agent_runs(id) ON DELETE RESTRICT,
  output_kind         TEXT        NOT NULL,
  content_text        TEXT,
  content_hash        TEXT,
  structured_payload  JSONB,
  tokens              INTEGER,
  sequence_no         INTEGER     NOT NULL DEFAULT 0,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT orchestration_agent_outputs_sequence_no_chk
    CHECK (sequence_no >= 0),
  CONSTRAINT orchestration_agent_outputs_run_sequence_uq
    UNIQUE (agent_run_id, sequence_no)
);

CREATE INDEX orchestration_agent_outputs_run_idx
  ON orchestration_agent_outputs (agent_run_id);

CREATE TRIGGER orchestration_agent_outputs_append_only
BEFORE UPDATE OR DELETE ON orchestration_agent_outputs
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();


-- ============================================================================
-- AREA 6 — SOURCE CANDIDATE FLOW (append-only)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- SOURCE_CANDIDATES (append-only fact)
-- A source proposed/cited by an agent, supplied by the user, or retrieved by
-- the system. A source_candidate is NOT an evidence_span: this table carries
-- NO FK to evidence_spans, claim_evidence_links or logical_claims. The bridge
-- into evidence runs exclusively through source_verifications.
-- ---------------------------------------------------------------------------
CREATE TABLE source_candidates (
  id                    UUID             PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id             UUID             NOT NULL REFERENCES tenants(id)                     ON DELETE RESTRICT,
  orchestration_run_id  UUID                      REFERENCES orchestration_runs(id)          ON DELETE RESTRICT,
  master_prompt_id      UUID                      REFERENCES master_prompts(id)              ON DELETE RESTRICT,
  agent_output_id       UUID                      REFERENCES orchestration_agent_outputs(id) ON DELETE RESTRICT,
  candidate_type        TEXT             NOT NULL,
  status                TEXT             NOT NULL,
  title                 TEXT,
  url                   TEXT,
  citation_text         TEXT,
  quoted_text           TEXT,
  declared_confidence   DOUBLE PRECISION,
  provenance            JSONB            NOT NULL DEFAULT '{}'::jsonb,
  created_by            TEXT,
  raw_citation_payload  JSONB            NOT NULL DEFAULT '{}'::jsonb,
  created_at            TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
  CONSTRAINT source_candidates_candidate_type_chk
    CHECK (candidate_type IN (
      'agent_cited',
      'user_supplied',
      'system_retrieved',
      'internal',
      'future_web'
    )),
  CONSTRAINT source_candidates_status_chk
    CHECK (status IN (
      'proposed',
      'resolution_pending',
      'resolved',
      'resolution_failed',
      'verification_pending',
      'verified_as_retrieved',
      'rejected',
      'insufficient_metadata'
    )),
  CONSTRAINT source_candidates_declared_confidence_range
    CHECK (declared_confidence IS NULL
           OR (declared_confidence >= 0.0 AND declared_confidence <= 1.0))
);

CREATE INDEX source_candidates_run_idx
  ON source_candidates (orchestration_run_id);
CREATE INDEX source_candidates_master_prompt_idx
  ON source_candidates (master_prompt_id);

CREATE TRIGGER source_candidates_append_only
BEFORE UPDATE OR DELETE ON source_candidates
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ---------------------------------------------------------------------------
-- SOURCE_RESOLUTIONS (append-only fact)
-- An attempt to recover/resolve the real source of a source_candidate.
-- ---------------------------------------------------------------------------
CREATE TABLE source_resolutions (
  id                       UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  source_candidate_id      UUID        NOT NULL REFERENCES source_candidates(id)  ON DELETE RESTRICT,
  orchestration_run_id     UUID                 REFERENCES orchestration_runs(id) ON DELETE RESTRICT,
  resolution_target_kind   TEXT        NOT NULL,
  outcome                  TEXT        NOT NULL,
  failure_reason           TEXT,
  retrieved_artifact_ref   UUID,
  retrieved_artifact_hash  TEXT,
  idempotency_key          TEXT        NOT NULL,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT source_resolutions_target_kind_chk
    CHECK (resolution_target_kind IN (
      'url',
      'web_page',
      'internal_document',
      'uploaded_document',
      'retrieved_document'
    )),
  -- QA correction (PARTE 2/2 §3): outcome must include resolved / failed /
  -- insufficient_metadata. The additional values partial / unreachable /
  -- not_found are kept as more specific motivated outcomes of a resolution
  -- attempt (PHASE_ORCH_SCHEMA_PRE.md §12.1).
  CONSTRAINT source_resolutions_outcome_chk
    CHECK (outcome IN (
      'resolved',
      'failed',
      'insufficient_metadata',
      'partial',
      'unreachable',
      'not_found'
    )),
  CONSTRAINT source_resolutions_idem_uq
    UNIQUE (source_candidate_id, idempotency_key)
);

CREATE INDEX source_resolutions_candidate_idx
  ON source_resolutions (source_candidate_id);

CREATE TRIGGER source_resolutions_append_only
BEFORE UPDATE OR DELETE ON source_resolutions
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ---------------------------------------------------------------------------
-- SOURCE_VERIFICATIONS (append-only fact)
-- Verification of a resolved source: it can attach/produce an evidence_span.
-- This is the ONLY bridge from a source_candidate into real evidence_spans.
-- It checks presence/quote/hash; it does NOT judge semantic support.
-- QA correction (PARTE 2/2 §4): the principal column is 'outcome' with values
-- verified_as_retrieved / rejected / inconclusive. Quote-present / quote-absent
-- / hash-mismatch detail lives in failure_reason and verification_payload, it
-- does not replace the principal states.
-- ---------------------------------------------------------------------------
CREATE TABLE source_verifications (
  id                    UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  source_candidate_id   UUID        NOT NULL REFERENCES source_candidates(id)  ON DELETE RESTRICT,
  source_resolution_id  UUID        NOT NULL REFERENCES source_resolutions(id) ON DELETE RESTRICT,
  evidence_span_id      UUID                 REFERENCES evidence_spans(id)     ON DELETE RESTRICT,
  document_version_id   UUID                 REFERENCES document_versions(id)  ON DELETE RESTRICT,
  document_chunk_id     UUID                 REFERENCES document_chunks(id)    ON DELETE RESTRICT,
  outcome               TEXT        NOT NULL,
  quote_hash_checked    TEXT,
  failure_reason        TEXT,
  verification_payload  JSONB       NOT NULL DEFAULT '{}'::jsonb,
  idempotency_key       TEXT        NOT NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT source_verifications_outcome_chk
    CHECK (outcome IN ('verified_as_retrieved', 'rejected', 'inconclusive')),
  CONSTRAINT source_verifications_idem_uq
    UNIQUE (source_resolution_id, idempotency_key)
);

CREATE INDEX source_verifications_candidate_idx
  ON source_verifications (source_candidate_id);
CREATE INDEX source_verifications_evidence_span_idx
  ON source_verifications (evidence_span_id);

CREATE TRIGGER source_verifications_append_only
BEFORE UPDATE OR DELETE ON source_verifications
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();


-- ============================================================================
-- AREA 7 — PROVIDER / TOKEN (append-only)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- PROVIDER_INVOCATIONS (append-only fact)
-- Each call to the provider abstraction as an auditable fact. Carries NO
-- column for API keys, secrets, tokens of authentication or credentials.
-- QA correction (PARTE 2/2 §2): status includes pending / succeeded / failed /
-- cancelled. timeout / rate_limited are NOT principal statuses: they are
-- represented through error_code / error_message on a 'failed' invocation.
-- ---------------------------------------------------------------------------
CREATE TABLE provider_invocations (
  id                    UUID             PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id             UUID             NOT NULL REFERENCES tenants(id)                  ON DELETE RESTRICT,
  agent_run_id          UUID             NOT NULL REFERENCES orchestration_agent_runs(id) ON DELETE RESTRICT,
  orchestration_run_id  UUID                      REFERENCES orchestration_runs(id)       ON DELETE RESTRICT,
  provider_name         TEXT             NOT NULL,
  model                 TEXT             NOT NULL,
  request_hash          TEXT,
  response_hash         TEXT,
  status                TEXT             NOT NULL,
  error_code            TEXT,
  error_message         TEXT,
  tokens_input          BIGINT,
  tokens_output         BIGINT,
  cost_estimate         DOUBLE PRECISION,
  latency_ms            INTEGER,
  attempt_no            INTEGER          NOT NULL DEFAULT 1,
  is_mock               BOOLEAN          NOT NULL DEFAULT TRUE,
  redaction_strategy    TEXT,
  idempotency_key       TEXT             NOT NULL,
  created_at            TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
  CONSTRAINT provider_invocations_status_chk
    CHECK (status IN ('pending', 'succeeded', 'failed', 'cancelled')),
  CONSTRAINT provider_invocations_attempt_no_chk
    CHECK (attempt_no >= 1),
  CONSTRAINT provider_invocations_attempt_idem_uq
    UNIQUE (agent_run_id, attempt_no, idempotency_key)
);

CREATE INDEX provider_invocations_agent_run_idx
  ON provider_invocations (agent_run_id);
CREATE INDEX provider_invocations_run_idx
  ON provider_invocations (orchestration_run_id);

CREATE TRIGGER provider_invocations_append_only
BEFORE UPDATE OR DELETE ON provider_invocations
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ---------------------------------------------------------------------------
-- TOKEN_USAGE_RECORDS (append-only fact)
-- The real token/cost consumption recorded after it happened.
-- ---------------------------------------------------------------------------
CREATE TABLE token_usage_records (
  id                      UUID             PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id               UUID             NOT NULL REFERENCES tenants(id)                  ON DELETE RESTRICT,
  orchestration_run_id    UUID             NOT NULL REFERENCES orchestration_runs(id)       ON DELETE RESTRICT,
  agent_run_id            UUID                      REFERENCES orchestration_agent_runs(id) ON DELETE RESTRICT,
  provider_invocation_id  UUID                      REFERENCES provider_invocations(id)     ON DELETE RESTRICT,
  pass_kind               TEXT,
  tokens_input            BIGINT           NOT NULL,
  tokens_output           BIGINT           NOT NULL,
  cost_estimate           DOUBLE PRECISION,
  attempt_no              INTEGER          NOT NULL DEFAULT 1,
  is_mock                 BOOLEAN          NOT NULL,
  idempotency_key         TEXT             NOT NULL,
  recorded_at             TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
  created_at              TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
  CONSTRAINT token_usage_records_pass_kind_chk
    CHECK (pass_kind IS NULL OR pass_kind IN (
      'independent_answer',
      'reviewer',
      'critic',
      'synthesis',
      'second_check',
      'source_resolution'
    )),
  CONSTRAINT token_usage_records_tokens_input_chk
    CHECK (tokens_input >= 0),
  CONSTRAINT token_usage_records_tokens_output_chk
    CHECK (tokens_output >= 0),
  CONSTRAINT token_usage_records_attempt_no_chk
    CHECK (attempt_no >= 1)
);

-- Idempotency via two PARTIAL UNIQUE indexes (pattern of the partial UNIQUEs
-- in 0006/0007). provider_invocation_id is nullable: a plain UNIQUE over a
-- nullable column would let NULL rows duplicate freely in PostgreSQL, so the
-- idempotency surface is split by whether a provider invocation is attributed.
CREATE UNIQUE INDEX token_usage_records_provider_idem_uq
  ON token_usage_records (orchestration_run_id, provider_invocation_id, idempotency_key)
  WHERE provider_invocation_id IS NOT NULL;

CREATE UNIQUE INDEX token_usage_records_no_provider_idem_uq
  ON token_usage_records (orchestration_run_id, idempotency_key)
  WHERE provider_invocation_id IS NULL;

CREATE INDEX token_usage_records_run_idx
  ON token_usage_records (orchestration_run_id);
CREATE INDEX token_usage_records_agent_run_idx
  ON token_usage_records (agent_run_id);

CREATE TRIGGER token_usage_records_append_only
BEFORE UPDATE OR DELETE ON token_usage_records
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();


-- ============================================================================
-- AREA 8 — SYNTHESIS AND JOINS (append-only)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- CANDIDATE_SYNTHESES (append-only fact, versioned per run)
-- The candidate multi-AI answer. NOT a published_answers row.
-- QA correction (PARTE 2/2 §5): a 'status' column with values draft /
-- ready_for_claim_extraction / submitted_to_gate / superseded.
-- ---------------------------------------------------------------------------
CREATE TABLE candidate_syntheses (
  id                       UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id                UUID        NOT NULL REFERENCES tenants(id)                  ON DELETE RESTRICT,
  orchestration_run_id     UUID        NOT NULL REFERENCES orchestration_runs(id)       ON DELETE RESTRICT,
  synthesizer_agent_run_id UUID                 REFERENCES orchestration_agent_runs(id) ON DELETE RESTRICT,
  version_no               INTEGER     NOT NULL,
  synthesis_text           TEXT        NOT NULL,
  synthesis_text_hash      TEXT        NOT NULL,
  status                   TEXT        NOT NULL,
  output_kind              TEXT,
  is_mock                  BOOLEAN     NOT NULL,
  idempotency_key          TEXT        NOT NULL,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT candidate_syntheses_version_no_chk
    CHECK (version_no >= 1),
  CONSTRAINT candidate_syntheses_status_chk
    CHECK (status IN (
      'draft',
      'ready_for_claim_extraction',
      'submitted_to_gate',
      'superseded'
    )),
  -- Two distinct UNIQUE purposes on candidate_syntheses:
  --   * _run_version_uq : append-only versioning of the synthesis per run.
  --   * _run_idem_uq    : consumer-level idempotency against event redelivery.
  CONSTRAINT candidate_syntheses_run_version_uq
    UNIQUE (orchestration_run_id, version_no),
  CONSTRAINT candidate_syntheses_run_idem_uq
    UNIQUE (orchestration_run_id, idempotency_key)
);

CREATE INDEX candidate_syntheses_run_idx
  ON candidate_syntheses (orchestration_run_id);

CREATE TRIGGER candidate_syntheses_append_only
BEFORE UPDATE OR DELETE ON candidate_syntheses
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ---------------------------------------------------------------------------
-- SYNTHESIS_SOURCE_LINKS (append-only join)
-- Links a candidate synthesis to the agent outputs and/or evidence spans it
-- used. At least one of (agent_output_id, evidence_span_id) is NOT NULL.
-- ---------------------------------------------------------------------------
CREATE TABLE synthesis_source_links (
  id                    UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  candidate_synthesis_id UUID       NOT NULL REFERENCES candidate_syntheses(id)         ON DELETE RESTRICT,
  agent_output_id       UUID                 REFERENCES orchestration_agent_outputs(id) ON DELETE RESTRICT,
  evidence_span_id      UUID                 REFERENCES evidence_spans(id)              ON DELETE RESTRICT,
  link_role             TEXT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT synthesis_source_links_slk_target_present
    CHECK (agent_output_id IS NOT NULL OR evidence_span_id IS NOT NULL)
);

CREATE INDEX synthesis_source_links_synthesis_idx
  ON synthesis_source_links (candidate_synthesis_id);

-- Partial UNIQUE indexes (pattern of the partial UNIQUEs in 0007): one link
-- per (synthesis, target) per target kind.
CREATE UNIQUE INDEX synthesis_source_links_evidence_uq
  ON synthesis_source_links (candidate_synthesis_id, evidence_span_id)
  WHERE evidence_span_id IS NOT NULL;

CREATE UNIQUE INDEX synthesis_source_links_output_uq
  ON synthesis_source_links (candidate_synthesis_id, agent_output_id)
  WHERE agent_output_id IS NOT NULL;

CREATE TRIGGER synthesis_source_links_append_only
BEFORE UPDATE OR DELETE ON synthesis_source_links
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ---------------------------------------------------------------------------
-- SYNTHESIS_CLAIM_LINKS (append-only join; bridge toward the Claim Ledger)
-- Links a candidate synthesis to the logical claims extracted from it.
--
-- FK target: logical_claims (0004). PHASE_ORCH_SCHEMA_PRE.md §17.4 leaves the
-- granularity (logical_claims vs claim_ledger_entries) formally open;
-- logical_claims is the most stable choice and the one the plan and the
-- ORCH-SCHEMA-A QA corrections both lean toward. Known tension, documented as
-- an open decision: logical_claims is scoped to a task_id (0004), whereas the
-- orchestration line has no task_masters row — an orchestration_run is the
-- multi-AI analogue of a task. The FK is purely additive and does not bypass
-- the gate; a different granularity would be a future additive migration.
-- This table carries NO FK toward published_answers or final_gate_reports:
-- it does not bypass and does not duplicate the Final Answer Gate.
-- ---------------------------------------------------------------------------
CREATE TABLE synthesis_claim_links (
  id                     UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  candidate_synthesis_id UUID        NOT NULL REFERENCES candidate_syntheses(id) ON DELETE RESTRICT,
  logical_claim_id       UUID        NOT NULL REFERENCES logical_claims(id)      ON DELETE RESTRICT,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT synthesis_claim_links_synthesis_claim_uq
    UNIQUE (candidate_synthesis_id, logical_claim_id)
);

CREATE INDEX synthesis_claim_links_synthesis_idx
  ON synthesis_claim_links (candidate_synthesis_id);
CREATE INDEX synthesis_claim_links_logical_claim_idx
  ON synthesis_claim_links (logical_claim_id);

CREATE TRIGGER synthesis_claim_links_append_only
BEFORE UPDATE OR DELETE ON synthesis_claim_links
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ============================================================================
-- END 0011_orchestration_schema.sql
-- ============================================================================
