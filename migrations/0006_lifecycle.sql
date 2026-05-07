-- ============================================================================
-- 0006_lifecycle.sql
-- Evidence-First MVP-0 — Sprint 4 — Phase 8.5 Block 1.
--
-- Scope:
--   - published_answer_lifecycle_events (APPEND-ONLY)
--   - source_loss_events (APPEND-ONLY)
--   - source_loss_propagation_records (APPEND-ONLY)
--
-- Phase 8.5 Block 1 invariants honored:
--   - task_masters.status is NOT extended. No 'withdrawn', 'superseded' or
--     'publication_held' status is introduced at DB level.
--   - lc_block_delete_if_published is NOT modified.
--   - claim_lineage.relation_kind is NOT extended.
--   - verification_records.check_kind is NOT extended.
--   - No DB trigger performs propagation: only append-only triggers on the
--     three new tables. Propagation logic is application-driven and lives
--     in the worker (Block 2/3, not part of this migration).
--   - No retention/cleanup logic. Block 1 is schema-only.
--
-- Dependencies: 0001..0005.
--
-- Append-only invariant:
--   The three lifecycle/source-loss tables are append-only via the shared
--   reject_modify_append_only() function defined in 0001_foundation.sql.
--   No row in these tables may be UPDATEd or DELETEd after INSERT.
--
-- Referential integrity:
--   - published_answer_lifecycle_events references published_answers via the
--     composite FK (published_answer_id, task_id) -> published_answers(id,
--     task_id). The composite UNIQUE published_answers_id_task_uq exists in
--     0005. This guarantees that an event's task_id always matches the task_id
--     of the published_answer it references.
--   - source_loss_events references evidence_spans(id) (granularita canonica).
--     document_chunk_id, document_version_id, document_id are reporting
--     context only.
--   - source_loss_propagation_records references source_loss_events(id) and
--     optionally references logical_claims, claim_ledger_entries, and
--     published_answers. All FKs are ON DELETE RESTRICT.
--
-- Idempotency:
--   - published_answer_lifecycle_events: UNIQUE on
--       (published_answer_id, event_type, idempotency_key)
--   - source_loss_events: UNIQUE on
--       (evidence_span_id, loss_kind, idempotency_key)
--   - source_loss_propagation_records: four UNIQUE PARTIAL indexes, one per
--     propagation_kind, restricted to status IN ('recorded','skipped'), so
--     that 'failed' attempts remain as append-only history without consuming
--     the final idempotency key for a future successful retry.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- PUBLISHED_ANSWER_LIFECYCLE_EVENTS (APPEND-ONLY)
-- ---------------------------------------------------------------------------
CREATE TABLE published_answer_lifecycle_events (
  id                    UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  published_answer_id   UUID        NOT NULL,
  task_id               UUID        NOT NULL,
  event_type            TEXT        NOT NULL CHECK (event_type IN (
                          'published',
                          'withdrawal_requested',
                          'withdrawn',
                          'superseded'
                        )),
  event_reason          TEXT        NOT NULL,
  event_payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
  requested_by          UUID                 REFERENCES users(id) ON DELETE RESTRICT,
  idempotency_key       TEXT        NOT NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT pale_published_answer_consistency
    FOREIGN KEY (published_answer_id, task_id)
    REFERENCES published_answers (id, task_id)
    ON DELETE RESTRICT,
  CONSTRAINT pale_idempotency_uq
    UNIQUE (published_answer_id, event_type, idempotency_key)
);

CREATE INDEX pale_published_answer_created_idx
  ON published_answer_lifecycle_events (published_answer_id, created_at);

CREATE INDEX pale_task_created_idx
  ON published_answer_lifecycle_events (task_id, created_at);

CREATE TRIGGER published_answer_lifecycle_events_append_only
BEFORE UPDATE OR DELETE ON published_answer_lifecycle_events
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ---------------------------------------------------------------------------
-- SOURCE_LOSS_EVENTS (APPEND-ONLY)
-- ---------------------------------------------------------------------------
-- Canonical granularity: evidence_span_id. The other document_* columns are
-- reporting context, not the basis for propagation.
-- All FKs use ON DELETE RESTRICT (never SET NULL).
CREATE TABLE source_loss_events (
  id                    UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id             UUID        NOT NULL REFERENCES tenants(id)        ON DELETE RESTRICT,
  project_id            UUID                 REFERENCES projects(id)       ON DELETE RESTRICT,
  task_id               UUID                 REFERENCES task_masters(id)   ON DELETE RESTRICT,
  evidence_span_id      UUID        NOT NULL REFERENCES evidence_spans(id) ON DELETE RESTRICT,
  document_chunk_id     UUID                 REFERENCES document_chunks(id)     ON DELETE RESTRICT,
  document_version_id   UUID                 REFERENCES document_versions(id)   ON DELETE RESTRICT,
  document_id           UUID                 REFERENCES uploaded_documents(id)  ON DELETE RESTRICT,
  loss_kind             TEXT        NOT NULL CHECK (loss_kind IN (
                          'source_deleted',
                          'source_access_lost',
                          'quote_mismatch',
                          'document_replaced',
                          'policy_retraction'
                        )),
  loss_reason           TEXT        NOT NULL,
  detected_by           TEXT        NOT NULL,
  event_payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
  idempotency_key       TEXT        NOT NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT sle_idempotency_uq
    UNIQUE (evidence_span_id, loss_kind, idempotency_key)
);

CREATE INDEX sle_evidence_span_idx
  ON source_loss_events (evidence_span_id);

CREATE INDEX sle_task_created_idx
  ON source_loss_events (task_id, created_at);

CREATE INDEX sle_project_created_idx
  ON source_loss_events (project_id, created_at);

CREATE TRIGGER source_loss_events_append_only
BEFORE UPDATE OR DELETE ON source_loss_events
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ---------------------------------------------------------------------------
-- SOURCE_LOSS_PROPAGATION_RECORDS (APPEND-ONLY)
-- ---------------------------------------------------------------------------
-- Tracks the application-level propagation of a source_loss_events row.
-- The Block 1 migration only declares the schema. The worker propagator
-- (Block 2/3) inserts rows here. Append-only: failed retries are recorded
-- as new rows, never as in-place mutations.
CREATE TABLE source_loss_propagation_records (
  id                          UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  source_loss_event_id        UUID        NOT NULL REFERENCES source_loss_events(id) ON DELETE RESTRICT,
  claim_logical_id            UUID                 REFERENCES logical_claims(id) ON DELETE RESTRICT,
  old_claim_ledger_entry_id   UUID                 REFERENCES claim_ledger_entries(id) ON DELETE RESTRICT,
  new_claim_ledger_entry_id   UUID                 REFERENCES claim_ledger_entries(id) ON DELETE RESTRICT,
  published_answer_id         UUID                 REFERENCES published_answers(id) ON DELETE RESTRICT,
  propagation_kind            TEXT        NOT NULL CHECK (propagation_kind IN (
                                'claim_marked_unverifiable',
                                'published_answer_impacted',
                                'no_claims_impacted',
                                'no_active_published_answers_impacted'
                              )),
  status                      TEXT        NOT NULL CHECK (status IN (
                                'recorded',
                                'skipped',
                                'failed'
                              )),
  details                     JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Idempotency via unique partial indexes (one per propagation_kind).
-- Failed attempts do not consume the final idempotency key; a later recorded
-- or skipped outcome can still be inserted append-only.
CREATE UNIQUE INDEX slpr_claim_marked_unverifiable_uq
  ON source_loss_propagation_records (source_loss_event_id, propagation_kind, claim_logical_id)
  WHERE propagation_kind = 'claim_marked_unverifiable'
    AND status IN ('recorded', 'skipped')
    AND claim_logical_id IS NOT NULL;

CREATE UNIQUE INDEX slpr_published_answer_impacted_uq
  ON source_loss_propagation_records (source_loss_event_id, propagation_kind, published_answer_id)
  WHERE propagation_kind = 'published_answer_impacted'
    AND status IN ('recorded', 'skipped')
    AND published_answer_id IS NOT NULL;

CREATE UNIQUE INDEX slpr_no_claims_impacted_uq
  ON source_loss_propagation_records (source_loss_event_id, propagation_kind)
  WHERE propagation_kind = 'no_claims_impacted'
    AND status IN ('recorded', 'skipped');

CREATE UNIQUE INDEX slpr_no_active_published_answers_impacted_uq
  ON source_loss_propagation_records (source_loss_event_id, propagation_kind)
  WHERE propagation_kind = 'no_active_published_answers_impacted'
    AND status IN ('recorded', 'skipped');

-- Lookup indexes.
CREATE INDEX slpr_source_loss_event_idx
  ON source_loss_propagation_records (source_loss_event_id);

CREATE INDEX slpr_published_answer_idx
  ON source_loss_propagation_records (published_answer_id)
  WHERE published_answer_id IS NOT NULL;

CREATE TRIGGER source_loss_propagation_records_append_only
BEFORE UPDATE OR DELETE ON source_loss_propagation_records
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();

-- ============================================================================
-- END 0006_lifecycle.sql
-- ============================================================================
