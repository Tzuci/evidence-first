# Piano migrazioni — Evidence-First MVP-0

## Stato corrente

| Migration | Stato | Fase di applicazione |
|---|---|---|
| `0001_foundation.sql` | applicata e immutabile | 8.1a (corretta in 8.1a-patch) |
| `0002_storage.sql` | applicata e immutabile | 8.2 |
| `0003_documents.sql` | applicata e immutabile | 8.2 |
| `0004_claim_ledger.sql` | **applicata in 8.3** | 8.3 |
| `0005_answers_gate.sql` | da scrivere | Sprint 3 (Fase 8.4) |
| `0006_lifecycle.sql` | da scrivere | Sprint 4 |
| `0007_evaluation_retention.sql` | da scrivere | Sprint 4–5 |

Le migration applicate sono immutabili. Ogni correzione successiva passa da una nuova migration o da una *foundation patch* documentata, ricostruita via `make clean` in dev. Nessuna delle fasi 8.2, 8.2a-patch, 8.3 modifica `0001`, `0002`, `0003`.

---

## 0001_foundation.sql (Sprint 0)

Fondazioni multi-tenant: `tenants`, `users`, `projects`, `sessions`, `task_masters`, `audit_records` (chain hash-linked, append-only via trigger), `audit_chain_heads`, `event_processing_records` (idempotency a livello di consumer), helper `app_new_uuid()`, funzione di trigger comune `reject_modify_append_only`. CHECK costraint anonimo iniziale su `task_masters.status`. FK native su `audit_records.scope_id`, UNIQUE `(chain_scope, scope_id, chain_seq)`, CHECK `audit_scope_consistency`. Trigger `epr_protect_immutable_fields` su `event_processing_records`.

## 0002_storage.sql (Sprint 1)

Storage layer.

- `storage_blobs` con `tenant_namespace_id` (NULL ⇒ DEDUP_SCOPE=`global`), `content_hash`, `hash_algorithm='sha256'`, `size_bytes`, `mime_type`, `storage_backend ∈ {local_fs, s3, gcs, azure_blob}`, `local_path`, `bucket`, `object_key`, `refcount`. CHECK `blob_location_present` accoppia coerentemente backend e colonne di posizione. UNIQUE parziale globale `sb_global_uq (content_hash, hash_algorithm) WHERE tenant_namespace_id IS NULL`. UNIQUE parziale per tenant `sb_tenant_uq` (riservato a evoluzioni future).
- `storage_objects` con UNIQUE `so_owner_uq (tenant_id, project_id, object_type, logical_owner_kind, logical_owner_id, blob_id)`.
- Trigger `storage_object_refcount_ins` / `storage_object_refcount_del` sugli INSERT/DELETE di `storage_objects`.
- Trigger `enforce_blob_tenant_scope` (no-op in MVP-0 perché tutti i blob sono globali).
- Trigger `reject_delete_blob_with_refs` impedisce DELETE su blob con `refcount > 0`.

## 0003_documents.sql (Sprint 1)

Document foundation.

- `uploaded_documents` (tier `user_provided` o `system_generated`), `document_versions` (CHECK `octet_length(inline_text) <= 65536`, UNIQUE `(document_id, version_no)`), `document_chunks` (CHECK `dc_origin_xor`: in MVP-0 sempre `document_version_id NOT NULL` e `source_version_id IS NULL`; UNIQUE `(document_version_id, chunk_index)`), `evidence_spans` con trigger `evidence_spans_append_only` (rifiuta UPDATE/DELETE), `prompt_injection_flags` (popolata in fasi successive), `task_documents (task_id, document_id, role, position)` con PK composita.
- Esteso il CHECK su `task_masters.status` per includere `analyzed_partial` (drop del CHECK anonimo originale e ricreato come `task_masters_status_check`).

## 0004_claim_ledger.sql (Sprint 2, applicata in 8.3)

Claim Ledger e foundation di verifica. Append-only stretto su `claim_ledger_entries`.

Tabelle:

- **`logical_claims`** — chiave canonica della storia di un claim, scoped al task.
  - UNIQUE `(task_id, canonical_claim_hash)`.
  - FK su `tenants`, `projects`, `task_masters`.
- **`raw_claims`** — estrazione deterministica da `document_chunks`.
  - UNIQUE `(logical_claim_id, document_chunk_id, evidence_span_id, extractor_name, extractor_version)` per evitare duplicati sotto redelivery.
  - FK su `logical_claims`, `document_chunks`, `evidence_spans`.
- **`classified_claims`** — promozione a claim tipizzato.
  - CHECK `claim_type ∈ {factual, causal, opinion, recommendation, hypothesis, scenario}`.
  - UNIQUE `(raw_claim_id, classifier_name, classifier_version)`.
- **`claim_ledger_entries`** — **APPEND-ONLY**.
  - Trigger `claim_ledger_entries_append_only` (basato su `reject_modify_append_only`) rifiuta UPDATE e DELETE.
  - CHECK `state ∈ {candidate, verified_fact, disputed_fact, inference, hypothesis, opinion, scenario, recommendation, unverifiable, insufficient_data, rejected}`.
  - CHECK `support_scope ∈ {supported_by_user_corpus_only, corroborated_by_external, independently_verified, unsupported}`.
  - CHECK `user_provided_dependency` sullo stesso dominio di `support_scope`.
  - UNIQUE `cle_logical_version_uq (claim_logical_id, version_no)`.
  - UNIQUE composito `cle_id_logical_uq (id, claim_logical_id)` per supportare FK composite future (es. `claim_evidence_links`).
  - Nessuna colonna `superseded_by_id`. Il superseding è espresso esclusivamente tramite `claim_lineage`.
- **`claim_lineage`** — relazioni padre/figlio fra ledger entries.
  - CHECK `claim_lineage_no_self` (`parent_entry_id <> child_entry_id`).
  - UNIQUE `claim_lineage_uq (parent_entry_id, child_entry_id, relation_kind)`.
  - CHECK `relation_kind ∈ {supersedes, derived_from, refines, contradicts, supports}`.
  - Per supersede: `parent = vN`, `child = v(N+1)`, `relation_kind='supersedes'`.
- **`claim_evidence_links`** — collegamento claim ↔ evidence_spans.
  - CHECK `cel_origin_xor` (in MVP-0 sempre `evidence_span_id NOT NULL` e `retrieved_source_span_id IS NULL`).
  - UNIQUE `cel_entry_span_uq (claim_ledger_entry_id, evidence_span_id)`.
  - FK composita `cel_entry_logical_consistency` su `(claim_ledger_entry_id, claim_logical_id) → claim_ledger_entries(id, claim_logical_id)`, che usa l'UNIQUE composito sopra.
- **`verification_records`** — registrazione esiti di check.
  - CHECK `check_kind ∈ {csv, cve_lite, nli, judge}`.
  - CHECK `outcome ∈ {pass, fail, inconclusive}`.
  - UNIQUE `verification_records_uq (claim_ledger_entry_id, check_kind, check_name)`.
- **`contradiction_records`** — placeholder, vuota in 8.3. CHECK `cr_pair_distinct` previene `claim_logical_id_a = claim_logical_id_b`.
- **`claim_support_links`** — placeholder per relazioni `basis/assumption/precondition/counterposition`. UNIQUE `(claim_ledger_entry_id, related_logical_claim_id, link_kind)`.
- **`human_review_requests`** — placeholder. CHECK `status ∈ {proposed, open, approved, rejected, expired}`.
- **`publication_rules`** — placeholder seedabile. UNIQUE su `state`.

### Append-only e supersede

`claim_ledger_entries` è strettamente append-only. Non vengono mai eseguiti `UPDATE` o `DELETE` su questa tabella, né dal worker né dall'API. Il trigger di append-only li rifiuta a livello DB.

Per rappresentare che una v2 supersede una v1:
- si fa `INSERT` di una nuova riga con `version_no = N+1` e lo `state` finale (`verified_fact` su PASS, `unverifiable` su FAIL del CVE-lite, ecc.);
- si fa `INSERT` in `claim_lineage` con `parent_entry_id = v1.id`, `child_entry_id = v2.id`, `relation_kind = 'supersedes'`.

La v1 resta immutata. La storia completa di un claim è ricostruibile via `claim_ledger_entries` ordinato per `version_no`, con i collegamenti di lignaggio in `claim_lineage`.

### Idempotenza sotto redelivery

Tutti i vincoli UNIQUE elencati sopra (`logical_claims`, `raw_claims`, `classified_claims`, `cle_logical_version_uq`, `claim_lineage_uq`, `cel_entry_span_uq`, `verification_records_uq`) consentono al worker di usare `INSERT ... ON CONFLICT DO NOTHING` su ogni scrittura. Un doppio delivery dello stesso `task.created` non duplica `raw_claims`, `classified_claims`, `logical_claims`, `claim_ledger_entries`, `claim_lineage`, `claim_evidence_links`, `verification_records`, né eventi audit.

### Trigger NON installato in 0004

`lc_block_delete_if_published` **non è installato** in `0004_claim_ledger.sql`. Questo trigger dovrebbe rifiutare la cancellazione di righe di `logical_claims` quando esiste una pubblicazione che le referenzia, ma `published_answers` non esiste in 8.3: viene introdotta in `0005_answers_gate.sql` (Fase 8.4). Documentato esplicitamente come scelta di scope. Verrà installato in 0005 insieme alla tabella `published_answers`.

---

## 0005_answers_gate.sql (Sprint 3, da scrivere)

Compilazione e gate finale.

Tabelle previste: `agent_runs`, `agent_outputs`, `truncation_events`, `continuation_attempts`, `coverage_gap_statements`, `draft_final_answers`, `final_answer_spans`, `final_answer_span_claim_links`, `final_gate_reports` (append-only stretto), `published_answers` con campi lifecycle (`published_at`, `withdrawn_at`, `superseded_at`, ecc.).

Trigger previsti: `lc_block_delete_if_published` (collega `logical_claims` a `published_answers`).

## 0006_lifecycle.sql (Sprint 4)

`published_answer_lifecycle_events` append-only, `source_loss_events` con propagator.

## 0007_evaluation_retention.sql (Sprint 4–5)

`retention_policies`, `cleanup_jobs`, `storage_usage`, `quota_events`, `eval_runs`, `export_jobs`.

---

## Regola d'oro

Le migration applicate sono immutabili. Le incompatibilità rilevate dopo l'applicazione vengono gestite:
- in **dev** con `make clean` + nuova applicazione delle migration corrette, eventualmente promosse via "foundation patch" documentata;
- in **futuro** (post-MVP-0) con migration additive successive che recuperano l'incoerenza.

Nessuna fase 8.2, 8.2a-patch o 8.3 modifica `0001`, `0002`, `0003`. Nessuna fase introduce dipendenze AI o riferimenti a provider esterni.