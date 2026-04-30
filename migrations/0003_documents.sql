-- ============================================================================
-- 0003_documents.sql
-- Evidence-First MVP-0 — Sprint 1 — Document foundation.
--
-- Contenuto:
--   - uploaded_documents
--   - document_versions
--   - document_chunks (con CHECK dc_origin_xor)
--   - evidence_spans (append-only via trigger)
--   - prompt_injection_flags
--   - task_documents (mappa task -> documenti)
--   - estensione del CHECK su task_masters.status per includere 'analyzed_partial'
--
-- Dipendenze: 0001_foundation.sql, 0002_storage.sql.
--
-- Non incluso (riservato a 0004_claim_ledger.sql / Fase 8.3):
--   - raw_claims, classified_claims, logical_claims, claim_ledger_entries,
--     claim_evidence_links, verification_records, contradiction_records, ecc.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- UPLOADED_DOCUMENTS
-- ---------------------------------------------------------------------------
CREATE TABLE uploaded_documents (
  id                 UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id          UUID        NOT NULL REFERENCES tenants(id)  ON DELETE RESTRICT,
  project_id         UUID        NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  storage_object_id  UUID        NOT NULL REFERENCES storage_objects(id) ON DELETE RESTRICT,
  filename           TEXT        NOT NULL,
  content_hash       TEXT        NOT NULL,
  mime_type          TEXT,
  size_bytes         BIGINT      NOT NULL CHECK (size_bytes >= 0),
  tier               TEXT        NOT NULL CHECK (tier IN ('user_provided','system_generated')),
  language           TEXT        NOT NULL DEFAULT 'und',
  created_by         UUID                 REFERENCES users(id) ON DELETE RESTRICT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX uploaded_documents_project_idx ON uploaded_documents (project_id, created_at DESC);
CREATE INDEX uploaded_documents_hash_idx    ON uploaded_documents (content_hash);

-- ---------------------------------------------------------------------------
-- DOCUMENT_VERSIONS
-- inline_text per testi piccoli (<= 64 KiB), altrimenti riferimento allo
-- storage_object_id (lo stesso del documento o un derivato parsed).
-- ---------------------------------------------------------------------------
CREATE TABLE document_versions (
  id                 UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  document_id        UUID        NOT NULL REFERENCES uploaded_documents(id) ON DELETE CASCADE,
  version_no         INTEGER     NOT NULL CHECK (version_no >= 1),
  version_kind       TEXT        NOT NULL CHECK (version_kind IN ('original','parsed','normalized')),
  storage_object_id  UUID                 REFERENCES storage_objects(id) ON DELETE RESTRICT,
  inline_text        TEXT,
  text_hash          TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT dv_doc_version_uq UNIQUE (document_id, version_no),
  CONSTRAINT dv_inline_size_check CHECK (
    inline_text IS NULL OR octet_length(inline_text) <= 65536
  )
);

CREATE INDEX document_versions_document_idx ON document_versions (document_id, version_no);

-- ---------------------------------------------------------------------------
-- DOCUMENT_CHUNKS
-- In MVP-0 i chunk derivano sempre da document_version_id (corpus utente).
-- source_version_id riservato a future versioni di "retrieved sources".
-- ---------------------------------------------------------------------------
CREATE TABLE document_chunks (
  id                  UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  document_version_id UUID                 REFERENCES document_versions(id) ON DELETE CASCADE,
  source_version_id   UUID,
  chunk_index         INTEGER     NOT NULL CHECK (chunk_index >= 0),
  char_start          INTEGER     NOT NULL CHECK (char_start >= 0),
  char_end            INTEGER     NOT NULL CHECK (char_end >= char_start),
  inline_text         TEXT        NOT NULL,
  text_hash           TEXT        NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT dc_origin_xor CHECK (
    document_version_id IS NOT NULL AND source_version_id IS NULL
  ),
  CONSTRAINT dc_chunk_index_uq UNIQUE (document_version_id, chunk_index)
);

CREATE INDEX document_chunks_dv_idx ON document_chunks (document_version_id, chunk_index);

-- ---------------------------------------------------------------------------
-- EVIDENCE_SPANS (append-only via trigger)
-- ---------------------------------------------------------------------------
CREATE TABLE evidence_spans (
  id                 UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  document_chunk_id  UUID        NOT NULL REFERENCES document_chunks(id) ON DELETE CASCADE,
  char_start         INTEGER     NOT NULL CHECK (char_start >= 0),
  char_end           INTEGER     NOT NULL CHECK (char_end >= char_start),
  quote              TEXT        NOT NULL,
  quote_hash         TEXT        NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX evidence_spans_chunk_idx ON evidence_spans (document_chunk_id);

CREATE TRIGGER evidence_spans_append_only
BEFORE UPDATE OR DELETE ON evidence_spans
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ---------------------------------------------------------------------------
-- PROMPT_INJECTION_FLAGS
-- ---------------------------------------------------------------------------
CREATE TABLE prompt_injection_flags (
  id           UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  document_id  UUID        NOT NULL REFERENCES uploaded_documents(id) ON DELETE CASCADE,
  flag_type    TEXT        NOT NULL,
  severity     TEXT        NOT NULL CHECK (severity IN ('low','medium','high')),
  details      JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX prompt_injection_flags_doc_idx ON prompt_injection_flags (document_id);

-- ---------------------------------------------------------------------------
-- TASK_DOCUMENTS
-- ---------------------------------------------------------------------------
CREATE TABLE task_documents (
  task_id     UUID        NOT NULL REFERENCES task_masters(id)      ON DELETE CASCADE,
  document_id UUID        NOT NULL REFERENCES uploaded_documents(id) ON DELETE RESTRICT,
  role        TEXT        NOT NULL DEFAULT 'source'
                          CHECK (role IN ('source','reference','attachment')),
  position    INTEGER     NOT NULL DEFAULT 0 CHECK (position >= 0),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (task_id, document_id)
);

CREATE INDEX task_documents_doc_idx  ON task_documents (document_id);
CREATE INDEX task_documents_task_idx ON task_documents (task_id, position);

-- ---------------------------------------------------------------------------
-- Estensione CHECK su task_masters.status per 'analyzed_partial'.
-- task_masters.status è un CHECK constraint anonimo creato in 0001;
-- lo rimuoviamo e lo ricreiamo includendo 'analyzed_partial'.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  conname text;
BEGIN
  SELECT c.conname INTO conname
  FROM pg_constraint c
  JOIN pg_class t ON t.oid = c.conrelid
  WHERE t.relname = 'task_masters'
    AND c.contype = 'c'
    AND pg_get_constraintdef(c.oid) ILIKE '%status%'
  LIMIT 1;
  IF conname IS NOT NULL THEN
    EXECUTE format('ALTER TABLE task_masters DROP CONSTRAINT %I', conname);
  END IF;
END$$;

ALTER TABLE task_masters
  ADD CONSTRAINT task_masters_status_check CHECK (status IN (
    'created','ingesting','analyzing','verifying',
    'compiling','published','blocked','failed','cancelled','archived',
    'analyzed_partial'
  ));

-- ============================================================================
-- FINE 0003_documents.sql
-- ============================================================================