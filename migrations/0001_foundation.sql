-- ============================================================================
-- 0001_foundation.sql
-- Evidence-First Multi-AI Platform — MVP-0 — Sprint 0
--
-- Contenuto:
--   - Estensioni: pgcrypto, citext.
--   - Funzioni: app_new_uuid(), set_updated_at(), reject_modify_append_only().
--   - Tabelle: tenants, users, projects, project_members, sessions,
--              policy_versions, task_masters, audit_chain_heads, audit_records,
--              event_processing_records.
--   - Vincoli: PK UUID, FK, CHECK enum, UNIQUE, append-only su audit_records,
--              coerenza scope su audit_records.
--
-- NON incluso (verrà aggiunto da migration successive):
--   - storage_blobs, storage_objects
--   - uploaded_documents, document_versions, document_chunks, evidence_spans
--   - claim ledger, verification_records, contradiction_records
--   - draft_final_answers, final_answer_spans, final_answer_span_claim_links
--   - final_gate_reports, published_answers
--   - source_loss_events, published_answer_lifecycle_events
--   - human_review_requests, retention_policies, cleanup_jobs
--
-- IMPORTANTE: una volta applicata, questa migration è immutabile (checksum).
-- ============================================================================

-- ---------------------------------------------------------------------------
-- ESTENSIONI
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

-- ---------------------------------------------------------------------------
-- FUNZIONI HELPER
-- ---------------------------------------------------------------------------

-- Generatore UUID astratto. In MVP-0 ritorna gen_random_uuid() (UUID v4).
-- In produzione (Postgres >= 18) verrà sostituita con uuidv7() senza modifiche allo schema.
-- VOLATILE PARALLEL UNSAFE: necessario perché gen_random_uuid() è volatile.
CREATE OR REPLACE FUNCTION app_new_uuid() RETURNS uuid
LANGUAGE sql
VOLATILE
PARALLEL UNSAFE
AS $$
  SELECT gen_random_uuid()
$$;

-- Trigger function generica per aggiornare updated_at su UPDATE.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$;

-- Trigger function generica per rifiutare UPDATE/DELETE su tabelle append-only.
CREATE OR REPLACE FUNCTION reject_modify_append_only() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'append-only table: % not allowed on %', TG_OP, TG_TABLE_NAME;
END;
$$;

-- ---------------------------------------------------------------------------
-- TENANTS
-- ---------------------------------------------------------------------------
CREATE TABLE tenants (
  id         UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  name       TEXT        NOT NULL,
  slug       TEXT        NOT NULL,
  status     TEXT        NOT NULL CHECK (status IN ('active','suspended','deleted')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT tenants_slug_uq UNIQUE (slug)
);

CREATE TRIGGER tenants_set_updated_at
BEFORE UPDATE ON tenants
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- USERS
-- ---------------------------------------------------------------------------
CREATE TABLE users (
  id            UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id     UUID        NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  email         CITEXT      NOT NULL,
  display_name  TEXT,
  password_hash TEXT,
  oidc_subject  TEXT,
  status        TEXT        NOT NULL CHECK (status IN ('active','disabled','deleted')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  deleted_at    TIMESTAMPTZ,
  CONSTRAINT users_tenant_email_uq UNIQUE (tenant_id, email)
);

CREATE INDEX users_tenant_idx ON users (tenant_id);
CREATE INDEX users_oidc_idx   ON users (oidc_subject) WHERE oidc_subject IS NOT NULL;

CREATE TRIGGER users_set_updated_at
BEFORE UPDATE ON users
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- POLICY_VERSIONS
-- Tabella minimale per consentire i riferimenti da task_masters.policy.policy_version.
-- Verrà estesa in migration successive con campi configurazione (soglie, weights, ecc.).
-- ---------------------------------------------------------------------------
CREATE TABLE policy_versions (
  id         UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id  UUID        REFERENCES tenants(id) ON DELETE RESTRICT,
  name       TEXT        NOT NULL,
  is_default BOOLEAN     NOT NULL DEFAULT FALSE,
  metadata   JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT policy_versions_name_uq UNIQUE (tenant_id, name)
);

CREATE INDEX policy_versions_default_idx
  ON policy_versions (tenant_id) WHERE is_default = TRUE;

-- ---------------------------------------------------------------------------
-- PROJECTS
-- ---------------------------------------------------------------------------
CREATE TABLE projects (
  id              UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  name            TEXT        NOT NULL,
  mode_default    TEXT        CHECK (mode_default IN ('closed_corpus','verified_web','hybrid')),
  quota_overrides JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_by      UUID        REFERENCES users(id) ON DELETE RESTRICT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  deleted_at      TIMESTAMPTZ,
  CONSTRAINT projects_tenant_name_uq UNIQUE (tenant_id, name)
);

CREATE INDEX projects_tenant_idx ON projects (tenant_id);

CREATE TRIGGER projects_set_updated_at
BEFORE UPDATE ON projects
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- PROJECT_MEMBERS
-- ---------------------------------------------------------------------------
CREATE TABLE project_members (
  project_id UUID        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id    UUID        NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
  role       TEXT        NOT NULL CHECK (role IN ('owner','admin','editor','reader','reviewer','compliance_officer')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (project_id, user_id)
);

CREATE INDEX project_members_user_idx ON project_members (user_id);

-- ---------------------------------------------------------------------------
-- SESSIONS
-- ---------------------------------------------------------------------------
CREATE TABLE sessions (
  id              UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  project_id      UUID        NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  user_id         UUID        REFERENCES users(id) ON DELETE RESTRICT,
  started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ended_at        TIMESTAMPTZ,
  client_metadata JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX sessions_project_started_idx ON sessions (project_id, started_at DESC);

-- ---------------------------------------------------------------------------
-- TASK_MASTERS
-- ---------------------------------------------------------------------------
CREATE TABLE task_masters (
  id              UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  project_id      UUID        NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  session_id      UUID        REFERENCES sessions(id) ON DELETE SET NULL,
  created_by      UUID        REFERENCES users(id) ON DELETE RESTRICT,
  mode            TEXT        NOT NULL CHECK (mode IN ('closed_corpus','verified_web','hybrid')),
  objective       TEXT        NOT NULL,
  policy          JSONB       NOT NULL DEFAULT '{}'::jsonb,
  status          TEXT        NOT NULL CHECK (status IN (
                    'created','ingesting','analyzing','verifying',
                    'compiling','published','blocked','failed','cancelled','archived'
                  )),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  published_at    TIMESTAMPTZ,
  archived_at     TIMESTAMPTZ
);

CREATE INDEX task_masters_project_status_idx ON task_masters (project_id, status);
CREATE INDEX task_masters_created_by_idx     ON task_masters (created_by);

CREATE TRIGGER task_masters_set_updated_at
BEFORE UPDATE ON task_masters
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- AUDIT_CHAIN_HEADS
-- Testa della hash chain per scope (in MVP-0 lo scope canonico è 'task').
-- ---------------------------------------------------------------------------
CREATE TABLE audit_chain_heads (
  scope           TEXT        NOT NULL CHECK (scope IN ('task','project','tenant','global')),
  scope_id        UUID        NOT NULL,
  next_seq        BIGINT      NOT NULL DEFAULT 1 CHECK (next_seq >= 1),
  last_event_id   UUID,
  last_event_hash BYTEA,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (scope, scope_id)
);

-- ---------------------------------------------------------------------------
-- AUDIT_RECORDS (append-only)
-- Hash chain canonicalizzata service-side (vedi packages/shared/canonical_json.py
-- in Fase 8.1b). Ogni record include event_hash = sha256(canonical_payload) con
-- previous_event_hash incluso nel payload.
--
-- scope_id è la chiave di ancoraggio della chain: per chain_scope='task' coincide
-- con task_id, per 'project' con project_id, per 'tenant' con tenant_id, per
-- 'global' con UUID zero. Questa coerenza è enforced da CHECK a livello tabella.
-- ---------------------------------------------------------------------------
CREATE TABLE audit_records (
  id                            UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id                     UUID        NOT NULL REFERENCES tenants(id)      ON DELETE RESTRICT,
  project_id                    UUID                 REFERENCES projects(id)     ON DELETE RESTRICT,
  session_id                    UUID                 REFERENCES sessions(id)     ON DELETE SET NULL,
  task_id                       UUID                 REFERENCES task_masters(id) ON DELETE RESTRICT,
  chain_scope                   TEXT        NOT NULL DEFAULT 'task'
                                            CHECK (chain_scope IN ('task','project','tenant','global')),
  scope_id                      UUID        NOT NULL,
  chain_seq                     BIGINT      NOT NULL CHECK (chain_seq >= 1),
  previous_event_id             UUID        REFERENCES audit_records(id) ON DELETE RESTRICT,
  previous_event_hash           BYTEA,
  event_hash                    BYTEA       NOT NULL,
  event_type                    TEXT        NOT NULL,
  actor_type                    TEXT        NOT NULL CHECK (actor_type IN
                                            ('system','module','user','provider','scheduler','admin','job','propagator')),
  actor_id                      TEXT        NOT NULL,
  related_entity_type           TEXT,
  related_entity_id             UUID,
  redacted_payload              JSONB       NOT NULL DEFAULT '{}'::jsonb,
  sensitive_payload_object_id   UUID,
  created_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT audit_chain_seq_uq UNIQUE (chain_scope, scope_id, chain_seq),
  CONSTRAINT audit_scope_consistency CHECK (
    (chain_scope = 'task'    AND task_id    IS NOT NULL AND scope_id = task_id)
    OR (chain_scope = 'project' AND project_id IS NOT NULL AND scope_id = project_id)
    OR (chain_scope = 'tenant'                                AND scope_id = tenant_id)
    OR (chain_scope = 'global'                                AND scope_id = '00000000-0000-0000-0000-000000000000'::uuid)
  )
);

-- Indici di lettura
CREATE INDEX audit_records_task_seq_idx
  ON audit_records (task_id, chain_seq) WHERE chain_scope = 'task';
CREATE INDEX audit_records_chain_idx
  ON audit_records (chain_scope, scope_id, chain_seq);
CREATE INDEX audit_records_event_type_idx ON audit_records (event_type);
CREATE INDEX audit_records_related_idx
  ON audit_records (related_entity_type, related_entity_id);

-- Append-only enforcement
CREATE TRIGGER audit_records_append_only
BEFORE UPDATE OR DELETE ON audit_records
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ---------------------------------------------------------------------------
-- EVENT_PROCESSING_RECORDS
-- Fonte di verità persistente per idempotenza dei job critici.
-- I campi event_id/event_type/consumer_name/idempotency_key/first_seen_at sono
-- immutabili dopo l'INSERT iniziale. Verifica enforced via trigger.
-- ---------------------------------------------------------------------------
CREATE TABLE event_processing_records (
  id                  UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  event_id            UUID        NOT NULL,
  event_type          TEXT        NOT NULL,
  consumer_name       TEXT        NOT NULL,
  idempotency_key     TEXT        NOT NULL,
  tenant_id           UUID        NOT NULL REFERENCES tenants(id)      ON DELETE RESTRICT,
  project_id          UUID                 REFERENCES projects(id)     ON DELETE RESTRICT,
  task_id             UUID                 REFERENCES task_masters(id) ON DELETE RESTRICT,
  processing_status   TEXT        NOT NULL CHECK (processing_status IN
                                  ('started','succeeded','failed','dead_lettered')),
  first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at        TIMESTAMPTZ,
  attempt_count       INTEGER     NOT NULL DEFAULT 1 CHECK (attempt_count >= 1),
  result_hash         BYTEA,
  error_code          TEXT,
  error_message       TEXT,
  audit_record_id     UUID        REFERENCES audit_records(id) ON DELETE RESTRICT,
  CONSTRAINT epr_consumer_idemp_uq UNIQUE (consumer_name, idempotency_key)
);

CREATE INDEX epr_event_idx  ON event_processing_records (event_id);
CREATE INDEX epr_status_idx ON event_processing_records (processing_status, last_attempt_at);
CREATE INDEX epr_task_idx   ON event_processing_records (task_id, completed_at DESC);

-- Trigger: rifiuta modifiche ai campi immutabili dopo INSERT.
CREATE OR REPLACE FUNCTION epr_protect_immutable_fields() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.event_id        IS DISTINCT FROM OLD.event_id        OR
     NEW.event_type      IS DISTINCT FROM OLD.event_type      OR
     NEW.consumer_name   IS DISTINCT FROM OLD.consumer_name   OR
     NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key OR
     NEW.first_seen_at   IS DISTINCT FROM OLD.first_seen_at   OR
     NEW.tenant_id       IS DISTINCT FROM OLD.tenant_id THEN
    RAISE EXCEPTION 'event_processing_records: immutable field change rejected';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER epr_immutable_fields
BEFORE UPDATE ON event_processing_records
FOR EACH ROW EXECUTE FUNCTION epr_protect_immutable_fields();

-- ============================================================================
-- FINE 0001_foundation.sql
-- ============================================================================