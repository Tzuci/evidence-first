-- ============================================================================
-- 0005_answers_gate.sql
-- Evidence-First MVP-0 — Sprint 3 — Answers compilation, Final Answer Gate,
-- first published_answers.
--
-- Contenuto:
--   - agent_runs (tracking compile_draft / final_answer_gate)
--   - agent_outputs (placeholder, vuota in 8.4)
--   - truncation_events (placeholder, vuota in 8.4)
--   - continuation_attempts (placeholder, vuota in 8.4)
--   - coverage_gap_statements (con chiave idempotente gap_key)
--   - draft_final_answers (UNIQUE composito (id, task_id) per FK composite)
--   - final_answer_spans (APPEND-ONLY)
--   - final_answer_span_claim_links (FK composita verso claim_ledger_entries)
--   - final_gate_reports (APPEND-ONLY, UNIQUE per draft, FK composita verso draft)
--   - published_answers (FK composite verso draft e gate, no-self-supersede)
--   - lc_block_delete_if_published trigger (rinviato da 0004)
--   - estensione CHECK su task_masters.status per includere 'compiling' e 'published'
--
-- Dipendenze: 0001..0004.
--
-- Note di scope (Fase 8.4):
--   - agent_outputs/truncation_events/continuation_attempts ESISTONO ma restano
--     VUOTE in 8.4. Il compiler e il gate mock-driven non passano per "agent
--     completions": la tracciabilità minima è agent_runs -> draft_final_answers
--     (per compile_draft) e agent_runs -> final_gate_reports (per final_answer_gate).
--     Le tabelle placeholder esistono per non dover ALTERARE 0005 in fasi
--     successive quando entreranno provider reali con limiti di output.
--
--   - Stato terminale 8.4 (gestito dal worker, non a livello DB):
--       blocked                                          -> terminale
--       published                                        -> terminale
--       analyzed_partial AND final_gate_report esistente -> terminale (rejected scenario)
--       analyzed_partial AND nessun final_gate_report    -> NON terminale, prosegue
--
-- Coerenza referenziale stretta (introdotta dopo correzione del Blocco 1):
--   draft_final_answers UNIQUE (id, task_id)
--     |
--     +-- final_gate_reports.(draft_final_answer_id, task_id) FK -> draft.(id, task_id)
--     |   con UNIQUE composito final_gate_reports(id, task_id, draft_final_answer_id)
--     |
--     +-- published_answers.(draft_final_answer_id, task_id)    FK -> draft.(id, task_id)
--         published_answers.(final_gate_report_id, task_id, draft_final_answer_id)
--                                                              FK -> final_gate_reports.(id, task_id, draft_final_answer_id)
--   Conseguenza: e' impossibile avere un final_gate_report o un published_answer
--   il cui task_id non corrisponde al task_id del draft sottostante. Coerenza
--   garantita a DB, non solo a livello applicativo.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- AGENT_RUNS
-- ---------------------------------------------------------------------------
CREATE TABLE agent_runs (
  id           UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id    UUID        NOT NULL REFERENCES tenants(id)      ON DELETE RESTRICT,
  project_id   UUID        NOT NULL REFERENCES projects(id)     ON DELETE RESTRICT,
  task_id      UUID        NOT NULL REFERENCES task_masters(id) ON DELETE RESTRICT,
  run_kind     TEXT        NOT NULL CHECK (run_kind IN ('compile_draft','final_answer_gate')),
  attempt_no   INTEGER     NOT NULL CHECK (attempt_no >= 1),
  status       TEXT        NOT NULL CHECK (status IN ('running','succeeded','failed')),
  started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ended_at     TIMESTAMPTZ,
  payload      JSONB       NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT agent_runs_attempt_uq UNIQUE (task_id, run_kind, attempt_no)
);

CREATE INDEX agent_runs_task_idx ON agent_runs (task_id, run_kind);

-- ---------------------------------------------------------------------------
-- AGENT_OUTPUTS (placeholder, vuota in 8.4)
-- ---------------------------------------------------------------------------
CREATE TABLE agent_outputs (
  id            UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  agent_run_id  UUID        NOT NULL REFERENCES agent_runs(id) ON DELETE RESTRICT,
  sequence_no   INTEGER     NOT NULL CHECK (sequence_no >= 0),
  role          TEXT        NOT NULL CHECK (role IN ('assistant','tool','gate')),
  content_text  TEXT,
  content_hash  TEXT,
  payload       JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT agent_outputs_seq_uq UNIQUE (agent_run_id, sequence_no)
);

-- ---------------------------------------------------------------------------
-- TRUNCATION_EVENTS (placeholder, vuota in 8.4)
-- ---------------------------------------------------------------------------
CREATE TABLE truncation_events (
  id            UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  agent_run_id  UUID        NOT NULL REFERENCES agent_runs(id) ON DELETE RESTRICT,
  kind          TEXT        NOT NULL CHECK (kind IN ('output_truncated','input_truncated','tool_truncated')),
  details       JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- CONTINUATION_ATTEMPTS (placeholder, vuota in 8.4)
-- ---------------------------------------------------------------------------
CREATE TABLE continuation_attempts (
  id            UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  agent_run_id  UUID        NOT NULL REFERENCES agent_runs(id) ON DELETE RESTRICT,
  attempt_no    INTEGER     NOT NULL CHECK (attempt_no >= 1),
  started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ended_at      TIMESTAMPTZ,
  status        TEXT        NOT NULL CHECK (status IN ('running','succeeded','failed')),
  payload       JSONB       NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT continuation_attempts_uq UNIQUE (agent_run_id, attempt_no)
);

-- ---------------------------------------------------------------------------
-- DRAFT_FINAL_ANSWERS
-- In 8.4: SOLO version_no=1 per task. Nessun retry/versioning.
-- UNIQUE composito (id, task_id) per supportare FK composite a livello DB.
-- ---------------------------------------------------------------------------
CREATE TABLE draft_final_answers (
  id                  UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  task_id             UUID        NOT NULL REFERENCES task_masters(id) ON DELETE RESTRICT,
  version_no          INTEGER     NOT NULL CHECK (version_no >= 1),
  compiler_name       TEXT        NOT NULL,
  compiler_version    TEXT        NOT NULL,
  summary_text        TEXT        NOT NULL,
  payload             JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT draft_final_answers_version_uq UNIQUE (task_id, version_no),
  CONSTRAINT draft_final_answers_id_task_uq UNIQUE (id, task_id)
);

CREATE INDEX draft_final_answers_task_idx ON draft_final_answers (task_id, version_no DESC);

-- ---------------------------------------------------------------------------
-- COVERAGE_GAP_STATEMENTS
-- Popolata dal gate quando rifiuta. Idempotente per (draft, kind, gap_key).
-- gap_key e' una stringa stabile generata dal gate per identificare la causa
-- specifica del gap. Esempi 8.4:
--   - 'no_verified_claims' (caso zero verified)
--   - 'span:<final_answer_span_id>' (caso unverified spans present)
-- ---------------------------------------------------------------------------
CREATE TABLE coverage_gap_statements (
  id                       UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  draft_final_answer_id    UUID        NOT NULL REFERENCES draft_final_answers(id) ON DELETE RESTRICT,
  kind                     TEXT        NOT NULL CHECK (kind IN
                                       ('unverified_claim','missing_evidence','out_of_scope','source_loss')),
  severity                 TEXT        NOT NULL CHECK (severity IN ('info','warn','block')),
  gap_key                  TEXT        NOT NULL,
  details                  JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT coverage_gap_statements_idem_uq UNIQUE (draft_final_answer_id, kind, gap_key)
);

CREATE INDEX coverage_gap_statements_draft_idx ON coverage_gap_statements (draft_final_answer_id);

-- ---------------------------------------------------------------------------
-- FINAL_ANSWER_SPANS (APPEND-ONLY)
-- 1:1 con claim_ledger_entries con state='verified_fact' (solo verified).
-- Il compiler NON crea spans per claim unverifiable/disputed/rejected.
-- ---------------------------------------------------------------------------
CREATE TABLE final_answer_spans (
  id                       UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  draft_final_answer_id    UUID        NOT NULL REFERENCES draft_final_answers(id) ON DELETE RESTRICT,
  span_index               INTEGER     NOT NULL CHECK (span_index >= 0),
  char_start               INTEGER     NOT NULL CHECK (char_start >= 0),
  char_end                 INTEGER     NOT NULL CHECK (char_end >= char_start),
  span_text                TEXT        NOT NULL,
  span_hash                TEXT        NOT NULL,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT final_answer_spans_index_uq UNIQUE (draft_final_answer_id, span_index)
);

CREATE INDEX final_answer_spans_draft_idx ON final_answer_spans (draft_final_answer_id, span_index);

CREATE TRIGGER final_answer_spans_append_only
BEFORE UPDATE OR DELETE ON final_answer_spans
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ---------------------------------------------------------------------------
-- FINAL_ANSWER_SPAN_CLAIM_LINKS
-- FK composita verso claim_ledger_entries(id, claim_logical_id) (UNIQUE in 0004).
-- ---------------------------------------------------------------------------
CREATE TABLE final_answer_span_claim_links (
  id                       UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  final_answer_span_id     UUID        NOT NULL REFERENCES final_answer_spans(id) ON DELETE RESTRICT,
  claim_ledger_entry_id    UUID        NOT NULL,
  claim_logical_id         UUID        NOT NULL REFERENCES logical_claims(id) ON DELETE RESTRICT,
  link_role                TEXT        NOT NULL CHECK (link_role IN
                                       ('primary_support','supporting_context','counter_evidence')),
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT fasc_span_entry_uq UNIQUE (final_answer_span_id, claim_ledger_entry_id),
  CONSTRAINT fasc_entry_logical_consistency
    FOREIGN KEY (claim_ledger_entry_id, claim_logical_id)
    REFERENCES claim_ledger_entries(id, claim_logical_id)
);

CREATE INDEX fasc_span_idx          ON final_answer_span_claim_links (final_answer_span_id);
CREATE INDEX fasc_logical_claim_idx ON final_answer_span_claim_links (claim_logical_id);

-- ---------------------------------------------------------------------------
-- FINAL_GATE_REPORTS (APPEND-ONLY)
-- Un solo report per draft (UNIQUE su draft_final_answer_id).
-- UNIQUE composito (id, task_id, draft_final_answer_id) per FK composite di
-- published_answers.
-- FK composita (draft_final_answer_id, task_id) -> draft_final_answers(id, task_id):
-- garantisce DB-level che il report non possa avere un task_id diverso da
-- quello del draft sottostante.
-- ---------------------------------------------------------------------------
CREATE TABLE final_gate_reports (
  id                       UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  task_id                  UUID        NOT NULL REFERENCES task_masters(id) ON DELETE RESTRICT,
  draft_final_answer_id    UUID        NOT NULL,
  decision                 TEXT        NOT NULL CHECK (decision IN
                                       ('approved','rejected','held_for_review')),
  reason_code              TEXT        NOT NULL,
  payload                  JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT final_gate_reports_draft_uq UNIQUE (draft_final_answer_id),
  CONSTRAINT final_gate_reports_id_task_draft_uq UNIQUE (id, task_id, draft_final_answer_id),
  CONSTRAINT final_gate_reports_draft_consistency
    FOREIGN KEY (draft_final_answer_id, task_id)
    REFERENCES draft_final_answers (id, task_id)
);

CREATE INDEX final_gate_reports_task_idx ON final_gate_reports (task_id);

CREATE TRIGGER final_gate_reports_append_only
BEFORE UPDATE OR DELETE ON final_gate_reports
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ---------------------------------------------------------------------------
-- PUBLISHED_ANSWERS
-- Solo status='published' viene inserito direttamente in 8.4. Le transizioni
-- a withdrawn/superseded sono fuori scope per 8.4.
-- FK composite garantiscono DB-level che:
--   - il draft del published appartiene allo stesso task del published;
--   - il gate report del published appartiene allo stesso task e allo stesso
--     draft del published.
-- ---------------------------------------------------------------------------
CREATE TABLE published_answers (
  id                       UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  task_id                  UUID        NOT NULL REFERENCES task_masters(id) ON DELETE RESTRICT,
  draft_final_answer_id    UUID        NOT NULL,
  final_gate_report_id     UUID        NOT NULL,
  version_no               INTEGER     NOT NULL CHECK (version_no >= 1),
  content_hash             TEXT        NOT NULL,
  payload                  JSONB       NOT NULL DEFAULT '{}'::jsonb,
  status                   TEXT        NOT NULL CHECK (status IN ('published','withdrawn','superseded')),
  published_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  withdrawn_at             TIMESTAMPTZ,
  superseded_at            TIMESTAMPTZ,
  superseded_by_id         UUID,
  CONSTRAINT published_answers_version_uq UNIQUE (task_id, version_no),
  CONSTRAINT published_answers_id_task_uq UNIQUE (id, task_id),
  CONSTRAINT published_answers_no_self_supersede
    CHECK (superseded_by_id IS NULL OR superseded_by_id <> id),
  CONSTRAINT published_answers_supersede_self
    FOREIGN KEY (superseded_by_id) REFERENCES published_answers(id) ON DELETE RESTRICT,
  CONSTRAINT published_answers_draft_consistency
    FOREIGN KEY (draft_final_answer_id, task_id)
    REFERENCES draft_final_answers (id, task_id),
  CONSTRAINT published_answers_gate_consistency
    FOREIGN KEY (final_gate_report_id, task_id, draft_final_answer_id)
    REFERENCES final_gate_reports (id, task_id, draft_final_answer_id)
);

CREATE INDEX published_answers_task_idx ON published_answers (task_id, version_no DESC);

-- ---------------------------------------------------------------------------
-- TRIGGER lc_block_delete_if_published
-- Blocca DELETE su logical_claims se esiste una published_answers in stato
-- 'published' la cui catena draft -> spans -> span_claim_links la referenzia.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION lc_block_delete_if_published() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM final_answer_span_claim_links fascl
    JOIN final_answer_spans       fas  ON fas.id  = fascl.final_answer_span_id
    JOIN draft_final_answers      dfa  ON dfa.id  = fas.draft_final_answer_id
    JOIN published_answers        pa   ON pa.draft_final_answer_id = dfa.id
    WHERE fascl.claim_logical_id = OLD.id
      AND pa.status = 'published'
    LIMIT 1
  ) THEN
    RAISE EXCEPTION
      'lc_block_delete_if_published: logical_claim % is referenced by an active published_answers; cannot DELETE',
      OLD.id;
  END IF;
  RETURN OLD;
END;
$$;

CREATE TRIGGER lc_block_delete_if_published_trg
BEFORE DELETE ON logical_claims
FOR EACH ROW EXECUTE FUNCTION lc_block_delete_if_published();

-- ---------------------------------------------------------------------------
-- Estensione del CHECK su task_masters.status per includere 'compiling' e 'published'.
-- task_masters.status e' governato da un CHECK constraint nominato
-- 'task_masters_status_check' (creato/ricreato in 0003).
-- ---------------------------------------------------------------------------
ALTER TABLE task_masters DROP CONSTRAINT task_masters_status_check;

ALTER TABLE task_masters
  ADD CONSTRAINT task_masters_status_check CHECK (status IN (
    'created','ingesting','analyzing','verifying',
    'compiling','published','blocked','failed','cancelled','archived',
    'analyzed_partial'
  ));

-- ============================================================================
-- FINE 0005_answers_gate.sql
-- ============================================================================
