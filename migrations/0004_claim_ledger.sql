-- ============================================================================
-- 0004_claim_ledger.sql
-- Evidence-First MVP-0 — Sprint 2 — Claim Ledger and verification foundation.
--
-- Contenuto:
--   - logical_claims (chiave canonica per la storia di un claim, scoped al task)
--   - raw_claims (estratti deterministici da document_chunks)
--   - classified_claims (promozione a claim tipizzato)
--   - claim_ledger_entries (APPEND-ONLY via trigger, supersede via claim_lineage)
--   - claim_lineage (relazioni padre/figlio)
--   - claim_evidence_links (collegamento claim ↔ evidence_spans)
--   - verification_records (CVE-lite e futuri checks)
--   - contradiction_records (placeholder, vuota in 8.3)
--   - claim_support_links (placeholder per basis/assumption/precondition/counterposition)
--   - human_review_requests (placeholder, vuota in 8.3)
--   - publication_rules (placeholder, popolata da seed successivi)
--
-- Dipendenze: 0001_foundation.sql, 0002_storage.sql, 0003_documents.sql.
--
-- Append-only invariant:
--   claim_ledger_entries non può essere modificata né cancellata. Per rappresentare
--   che una v2 supersede una v1 si usa esclusivamente claim_lineage.relation_kind
--   = 'supersedes'. La v1 resta immutata.
--
-- Non incluso (riservato a 0005_answers_gate.sql / Fase 8.4):
--   - draft_final_answers, final_answer_spans, final_answer_span_claim_links
--   - final_gate_reports, published_answers
--   - lc_block_delete_if_published trigger (richiede published_answers)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- LOGICAL_CLAIMS
-- Chiave canonica della storia di un claim, scoped al task.
-- ---------------------------------------------------------------------------
CREATE TABLE logical_claims (
  id                    UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id             UUID        NOT NULL REFERENCES tenants(id)      ON DELETE RESTRICT,
  project_id            UUID        NOT NULL REFERENCES projects(id)     ON DELETE RESTRICT,
  task_id               UUID        NOT NULL REFERENCES task_masters(id) ON DELETE RESTRICT,
  canonical_claim_text  TEXT        NOT NULL,
  canonical_claim_hash  TEXT        NOT NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT lc_task_canonical_uq UNIQUE (task_id, canonical_claim_hash)
);

CREATE INDEX logical_claims_task_idx ON logical_claims (task_id);

-- ---------------------------------------------------------------------------
-- RAW_CLAIMS
-- Estrazione deterministica da document_chunks.
-- UNIQUE per evitare duplicati sotto redelivery: stesso (logical_claim, chunk, span, extractor).
-- ---------------------------------------------------------------------------
CREATE TABLE raw_claims (
  id                  UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id           UUID        NOT NULL REFERENCES tenants(id)      ON DELETE RESTRICT,
  project_id          UUID        NOT NULL REFERENCES projects(id)     ON DELETE RESTRICT,
  task_id             UUID        NOT NULL REFERENCES task_masters(id) ON DELETE RESTRICT,
  logical_claim_id    UUID        NOT NULL REFERENCES logical_claims(id) ON DELETE RESTRICT,
  document_chunk_id   UUID        NOT NULL REFERENCES document_chunks(id) ON DELETE RESTRICT,
  evidence_span_id    UUID        NOT NULL REFERENCES evidence_spans(id)  ON DELETE RESTRICT,
  raw_text            TEXT        NOT NULL,
  extractor_name      TEXT        NOT NULL,
  extractor_version   TEXT        NOT NULL,
  payload             JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT raw_claims_extracted_uq UNIQUE
    (logical_claim_id, document_chunk_id, evidence_span_id, extractor_name, extractor_version)
);

CREATE INDEX raw_claims_task_idx          ON raw_claims (task_id);
CREATE INDEX raw_claims_logical_claim_idx ON raw_claims (logical_claim_id);

-- ---------------------------------------------------------------------------
-- CLASSIFIED_CLAIMS
-- Promozione a claim tipizzato. UNIQUE per redelivery safety.
-- ---------------------------------------------------------------------------
CREATE TABLE classified_claims (
  id                  UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  raw_claim_id        UUID        NOT NULL REFERENCES raw_claims(id) ON DELETE RESTRICT,
  logical_claim_id    UUID        NOT NULL REFERENCES logical_claims(id) ON DELETE RESTRICT,
  claim_type          TEXT        NOT NULL CHECK (claim_type IN
                          ('factual','causal','opinion','recommendation','hypothesis','scenario')),
  domain_tag          TEXT        NOT NULL DEFAULT 'general',
  qualifiers          JSONB       NOT NULL DEFAULT '{}'::jsonb,
  classifier_name     TEXT        NOT NULL,
  classifier_version  TEXT        NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT classified_claims_uq UNIQUE
    (raw_claim_id, classifier_name, classifier_version)
);

CREATE INDEX classified_claims_logical_idx ON classified_claims (logical_claim_id);

-- ---------------------------------------------------------------------------
-- CLAIM_LEDGER_ENTRIES (APPEND-ONLY)
-- ---------------------------------------------------------------------------
CREATE TABLE claim_ledger_entries (
  id                        UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  claim_logical_id          UUID        NOT NULL REFERENCES logical_claims(id) ON DELETE RESTRICT,
  version_no                INTEGER     NOT NULL CHECK (version_no >= 1),
  state                     TEXT        NOT NULL CHECK (state IN (
                              'candidate','verified_fact','disputed_fact','inference',
                              'hypothesis','opinion','scenario','recommendation',
                              'unverifiable','insufficient_data','rejected'
                            )),
  support_scope             TEXT        NOT NULL CHECK (support_scope IN (
                              'supported_by_user_corpus_only',
                              'corroborated_by_external',
                              'independently_verified',
                              'unsupported'
                            )),
  user_provided_dependency  TEXT        NOT NULL CHECK (user_provided_dependency IN (
                              'supported_by_user_corpus_only',
                              'corroborated_by_external',
                              'independently_verified',
                              'unsupported'
                            )),
  human_review_required     BOOLEAN     NOT NULL DEFAULT FALSE,
  human_review_status       TEXT,
  transition_reason         TEXT,
  payload                   JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT cle_logical_version_uq UNIQUE (claim_logical_id, version_no),
  CONSTRAINT cle_id_logical_uq      UNIQUE (id, claim_logical_id)
);

CREATE INDEX cle_logical_idx ON claim_ledger_entries (claim_logical_id, version_no DESC);
CREATE INDEX cle_state_idx   ON claim_ledger_entries (state);

-- Append-only enforcement.
CREATE TRIGGER claim_ledger_entries_append_only
BEFORE UPDATE OR DELETE ON claim_ledger_entries
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ---------------------------------------------------------------------------
-- CLAIM_LINEAGE
-- Relazioni padre/figlio. Per supersede: parent = vN, child = v(N+1).
-- ---------------------------------------------------------------------------
CREATE TABLE claim_lineage (
  id              UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  parent_entry_id UUID        NOT NULL REFERENCES claim_ledger_entries(id) ON DELETE RESTRICT,
  child_entry_id  UUID        NOT NULL REFERENCES claim_ledger_entries(id) ON DELETE RESTRICT,
  relation_kind   TEXT        NOT NULL CHECK (relation_kind IN
                                ('supersedes','derived_from','refines','contradicts','supports')),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT claim_lineage_no_self CHECK (parent_entry_id <> child_entry_id),
  CONSTRAINT claim_lineage_uq UNIQUE (parent_entry_id, child_entry_id, relation_kind)
);

CREATE INDEX claim_lineage_parent_idx ON claim_lineage (parent_entry_id);
CREATE INDEX claim_lineage_child_idx  ON claim_lineage (child_entry_id);

-- ---------------------------------------------------------------------------
-- CLAIM_EVIDENCE_LINKS
-- ---------------------------------------------------------------------------
CREATE TABLE claim_evidence_links (
  id                          UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  claim_logical_id            UUID        NOT NULL REFERENCES logical_claims(id) ON DELETE RESTRICT,
  claim_ledger_entry_id       UUID        NOT NULL REFERENCES claim_ledger_entries(id) ON DELETE RESTRICT,
  evidence_span_id            UUID                 REFERENCES evidence_spans(id)  ON DELETE RESTRICT,
  retrieved_source_span_id    UUID,
  link_role                   TEXT        NOT NULL CHECK (link_role IN
                                          ('primary_support','supporting_context','counter_evidence')),
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT cel_origin_xor CHECK (
    evidence_span_id IS NOT NULL AND retrieved_source_span_id IS NULL
  ),
  CONSTRAINT cel_entry_span_uq UNIQUE (claim_ledger_entry_id, evidence_span_id),
  CONSTRAINT cel_entry_logical_consistency
    FOREIGN KEY (claim_ledger_entry_id, claim_logical_id)
    REFERENCES claim_ledger_entries(id, claim_logical_id)
);

CREATE INDEX cel_logical_idx ON claim_evidence_links (claim_logical_id);
CREATE INDEX cel_entry_idx   ON claim_evidence_links (claim_ledger_entry_id);

-- ---------------------------------------------------------------------------
-- VERIFICATION_RECORDS
-- ---------------------------------------------------------------------------
CREATE TABLE verification_records (
  id                       UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  claim_logical_id         UUID        NOT NULL REFERENCES logical_claims(id) ON DELETE RESTRICT,
  claim_ledger_entry_id    UUID        NOT NULL REFERENCES claim_ledger_entries(id) ON DELETE RESTRICT,
  check_kind               TEXT        NOT NULL CHECK (check_kind IN
                                       ('csv','cve_lite','nli','judge')),
  check_name               TEXT        NOT NULL,
  outcome                  TEXT        NOT NULL CHECK (outcome IN ('pass','fail','inconclusive')),
  score                    DOUBLE PRECISION,
  evaluator_id             TEXT        NOT NULL,
  payload                  JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT verification_records_uq UNIQUE
    (claim_ledger_entry_id, check_kind, check_name)
);

CREATE INDEX vr_logical_idx ON verification_records (claim_logical_id);
CREATE INDEX vr_entry_idx   ON verification_records (claim_ledger_entry_id);

-- ---------------------------------------------------------------------------
-- CONTRADICTION_RECORDS (placeholder)
-- Vuota in 8.3. Popolata in fasi successive con detector dedicato.
-- ---------------------------------------------------------------------------
CREATE TABLE contradiction_records (
  id                  UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  task_id             UUID        NOT NULL REFERENCES task_masters(id) ON DELETE RESTRICT,
  claim_logical_id_a  UUID        NOT NULL REFERENCES logical_claims(id) ON DELETE RESTRICT,
  claim_logical_id_b  UUID        NOT NULL REFERENCES logical_claims(id) ON DELETE RESTRICT,
  severity            TEXT        NOT NULL CHECK (severity IN ('low','medium','high','critical')),
  payload             JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT cr_pair_distinct CHECK (claim_logical_id_a <> claim_logical_id_b)
);

-- ---------------------------------------------------------------------------
-- CLAIM_SUPPORT_LINKS (placeholder)
-- Riservato a basis/assumption/precondition/counterposition.
-- ---------------------------------------------------------------------------
CREATE TABLE claim_support_links (
  id                       UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  claim_ledger_entry_id    UUID        NOT NULL REFERENCES claim_ledger_entries(id) ON DELETE RESTRICT,
  related_logical_claim_id UUID        NOT NULL REFERENCES logical_claims(id) ON DELETE RESTRICT,
  link_kind                TEXT        NOT NULL CHECK (link_kind IN
                                       ('basis','assumption','precondition','counterposition')),
  payload                  JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT csl_uq UNIQUE (claim_ledger_entry_id, related_logical_claim_id, link_kind)
);

-- ---------------------------------------------------------------------------
-- HUMAN_REVIEW_REQUESTS (placeholder)
-- ---------------------------------------------------------------------------
CREATE TABLE human_review_requests (
  id                       UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  claim_logical_id         UUID        NOT NULL REFERENCES logical_claims(id) ON DELETE RESTRICT,
  claim_ledger_entry_id    UUID        NOT NULL REFERENCES claim_ledger_entries(id) ON DELETE RESTRICT,
  status                   TEXT        NOT NULL DEFAULT 'proposed'
                                       CHECK (status IN ('proposed','open','approved','rejected','expired')),
  proposed_state           TEXT,
  trigger_reason           TEXT,
  payload                  JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  decided_at               TIMESTAMPTZ
);

CREATE INDEX hrr_status_idx ON human_review_requests (status);

-- ---------------------------------------------------------------------------
-- PUBLICATION_RULES (placeholder)
-- Popolata via seed in fasi successive.
-- ---------------------------------------------------------------------------
CREATE TABLE publication_rules (
  id           UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  state        TEXT        NOT NULL UNIQUE,
  publishable  BOOLEAN     NOT NULL DEFAULT FALSE,
  notes        TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- FINE 0004_claim_ledger.sql
-- ============================================================================