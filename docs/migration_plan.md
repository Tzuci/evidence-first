# Piano migrazioni — Evidence-First MVP-0

## Stato corrente

| Migration | Stato | Fase di applicazione |
|---|---|---|
| `0001_foundation.sql` | applicata e immutabile | 8.1a (corretta in 8.1a-patch) |
| `0002_storage.sql` | applicata e immutabile | 8.2 |
| `0003_documents.sql` | applicata e immutabile | 8.2 |
| `0004_claim_ledger.sql` | applicata e immutabile | 8.3 |
| `0005_answers_gate.sql` | **applicata e immutabile** | **8.4** |
| `0006_lifecycle.sql` | da scrivere | Sprint 4 (Fase 8.5) |
| `0007_evaluation_retention.sql` | da scrivere | Sprint 4–5 |

Le migration applicate sono immutabili. Ogni correzione successiva passa da una nuova migration o da una *foundation patch* documentata, ricostruita via `make clean` in dev. Nessuna delle fasi 8.2, 8.2a-patch, 8.3, 8.4 modifica `0001`, `0002`, `0003`, `0004`.

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
- Esteso il CHECK su `task_masters.status` per includere `analyzed_partial` (drop del CHECK anonimo originale e ricreato come `task_masters_status_check`). Codominio dopo 0003: `created`, `ingesting`, `analyzing`, `verifying`, `compiling`, `published`, `blocked`, `failed`, `cancelled`, `archived`, `analyzed_partial`.

## 0004_claim_ledger.sql (Sprint 2)

Claim Ledger e foundation di verifica. Append-only stretto su `claim_ledger_entries`.

Tabelle:

- **`logical_claims`** — chiave canonica della storia di un claim, scoped al task.
  - UNIQUE `(task_id, canonical_claim_hash)`.
  - FK su `tenants`, `projects`, `task_masters`.
- **`raw_claims`** — estrazione deterministica da `document_chunks`.
  - UNIQUE `(logical_claim_id, document_chunk_id, evidence_span_id, extractor_name, extractor_version)`.
- **`classified_claims`** — promozione a claim tipizzato.
  - CHECK `claim_type ∈ {factual, causal, opinion, recommendation, hypothesis, scenario}`.
  - UNIQUE `(raw_claim_id, classifier_name, classifier_version)`.
- **`claim_ledger_entries`** — **APPEND-ONLY**.
  - Trigger `claim_ledger_entries_append_only` rifiuta UPDATE e DELETE.
  - CHECK `state ∈ {candidate, verified_fact, disputed_fact, inference, hypothesis, opinion, scenario, recommendation, unverifiable, insufficient_data, rejected}`.
  - CHECK `support_scope ∈ {supported_by_user_corpus_only, corroborated_by_external, independently_verified, unsupported}`.
  - CHECK `user_provided_dependency` sullo stesso dominio di `support_scope`.
  - UNIQUE `cle_logical_version_uq (claim_logical_id, version_no)`.
  - UNIQUE composito `cle_id_logical_uq (id, claim_logical_id)` per FK composite future.
  - Nessuna colonna `superseded_by_id`. Il superseding è espresso esclusivamente tramite `claim_lineage`.
- **`claim_lineage`** — relazioni padre/figlio fra ledger entries.
  - CHECK `claim_lineage_no_self` (`parent_entry_id <> child_entry_id`).
  - UNIQUE `claim_lineage_uq (parent_entry_id, child_entry_id, relation_kind)`.
  - CHECK `relation_kind ∈ {supersedes, derived_from, refines, contradicts, supports}`.
- **`claim_evidence_links`** — collegamento claim ↔ evidence_spans.
  - CHECK `cel_origin_xor` (in MVP-0 sempre `evidence_span_id NOT NULL` e `retrieved_source_span_id IS NULL`).
  - UNIQUE `cel_entry_span_uq (claim_ledger_entry_id, evidence_span_id)`.
  - FK composita `cel_entry_logical_consistency` su `(claim_ledger_entry_id, claim_logical_id) → claim_ledger_entries(id, claim_logical_id)`.
- **`verification_records`** — registrazione esiti di check.
  - CHECK `check_kind ∈ {csv, cve_lite, nli, judge}`.
  - CHECK `outcome ∈ {pass, fail, inconclusive}`.
  - UNIQUE `verification_records_uq (claim_ledger_entry_id, check_kind, check_name)`.
- **`contradiction_records`** — placeholder, vuota in 8.3 e 8.4.
- **`claim_support_links`** — placeholder per `basis/assumption/precondition/counterposition`.
- **`human_review_requests`** — placeholder.
- **`publication_rules`** — placeholder seedabile.

### Nota su `lc_block_delete_if_published`

Il trigger `lc_block_delete_if_published`, che blocca DELETE su `logical_claims` quando esiste una `published_answers` attiva che li referenzia, **non è installato in 0004** perché `published_answers` non esiste ancora a quel punto. Viene installato in 0005, dopo la creazione di `published_answers`. La spiegazione storica resta valida: 0004 contiene solo le foundation del Claim Ledger.

---

## 0005_answers_gate.sql (Sprint 3, applicata in 8.4)

Compilazione, Final Answer Gate, primo `published_answers`. Coerenza referenziale stretta a livello DB tra task ↔ draft ↔ gate ↔ published. Trigger append-only su `final_answer_spans` e `final_gate_reports`. Trigger `lc_block_delete_if_published` installato (rinviato da 0004).

### Tabelle introdotte

- **`agent_runs`** — tracking di compilation/gate. CHECK `run_kind ∈ {compile_draft, final_answer_gate}`. CHECK `status ∈ {running, succeeded, failed}`. CHECK `attempt_no >= 1`. UNIQUE `agent_runs_attempt_uq (task_id, run_kind, attempt_no)`.
- **`agent_outputs`** — placeholder. Esiste ma **resta vuota in 8.4**: il pipeline mock-driven non passa per agent completions. UNIQUE `(agent_run_id, sequence_no)`. CHECK `role ∈ {assistant, tool, gate}`.
- **`truncation_events`** — placeholder, **resta vuota in 8.4**.
- **`continuation_attempts`** — placeholder, **resta vuota in 8.4**. UNIQUE `(agent_run_id, attempt_no)`.
- **`draft_final_answers`** — in 8.4 solo `version_no=1` per task.
  - CHECK `version_no >= 1`.
  - UNIQUE `draft_final_answers_version_uq (task_id, version_no)`.
  - **UNIQUE composito `draft_final_answers_id_task_uq (id, task_id)`** — target di FK composite di consistenza.
- **`coverage_gap_statements`** — popolata dal gate quando rifiuta.
  - CHECK `kind ∈ {unverified_claim, missing_evidence, out_of_scope, source_loss}`.
  - CHECK `severity ∈ {info, warn, block}`.
  - **UNIQUE `coverage_gap_statements_idem_uq (draft_final_answer_id, kind, gap_key)`** — chiave di idempotenza per gap riconducibili allo stesso draft. `gap_key` è una stringa stabile generata dal gate. Esempi 8.4: `'no_verified_claims'`, `'span:<final_answer_span_id>'`.
- **`final_answer_spans`** — **APPEND-ONLY**.
  - Trigger `final_answer_spans_append_only` (basato sul comune `reject_modify_append_only`) rifiuta UPDATE e DELETE.
  - UNIQUE `final_answer_spans_index_uq (draft_final_answer_id, span_index)`.
  - CHECK `span_index >= 0`, `char_start >= 0`, `char_end >= char_start`.
- **`final_answer_span_claim_links`** — FK composita verso `claim_ledger_entries(id, claim_logical_id)` (UNIQUE in 0004).
  - UNIQUE `fasc_span_entry_uq (final_answer_span_id, claim_ledger_entry_id)`.
  - CHECK `link_role ∈ {primary_support, supporting_context, counter_evidence}`.
  - FK composita `fasc_entry_logical_consistency`.
- **`final_gate_reports`** — **APPEND-ONLY**, un solo report per draft.
  - Trigger `final_gate_reports_append_only` rifiuta UPDATE e DELETE.
  - CHECK `decision ∈ {approved, rejected, held_for_review}`.
  - **UNIQUE `final_gate_reports_draft_uq (draft_final_answer_id)`** — un report per draft.
  - **UNIQUE composito `final_gate_reports_id_task_draft_uq (id, task_id, draft_final_answer_id)`** — target di FK composite di consistenza per `published_answers`.
  - **FK composita `final_gate_reports_draft_consistency` su `(draft_final_answer_id, task_id) → draft_final_answers(id, task_id)`** — il `task_id` del report è obbligato a coincidere con il `task_id` del draft sottostante. Verificato a livello DB, non solo applicativo.
- **`published_answers`** — in 8.4 solo `status='published'` viene inserito direttamente.
  - CHECK `status ∈ {published, withdrawn, superseded}`.
  - **UNIQUE `published_answers_version_uq (task_id, version_no)`**.
  - **UNIQUE composito `published_answers_id_task_uq (id, task_id)`**.
  - CHECK `published_answers_no_self_supersede` (`superseded_by_id IS NULL OR superseded_by_id <> id`).
  - **FK composita `published_answers_draft_consistency` su `(draft_final_answer_id, task_id) → draft_final_answers(id, task_id)`**.
  - **FK composita `published_answers_gate_consistency` su `(final_gate_report_id, task_id, draft_final_answer_id) → final_gate_reports(id, task_id, draft_final_answer_id)`**.
  - Conseguenza congiunta delle tre FK composite: a DB è impossibile avere un `published_answers` il cui `task_id` non corrisponde al `task_id` del draft sottostante e il cui `final_gate_report_id` non sia coerente con quel draft e quel task.

### Trigger introdotti

- **`final_answer_spans_append_only`** su `final_answer_spans`.
- **`final_gate_reports_append_only`** su `final_gate_reports`.
- **`lc_block_delete_if_published`** su `logical_claims`. Rifiuta DELETE quando esiste una `published_answers` attiva (`status='published'`) la cui catena draft → spans → span_claim_links referenzia il `logical_claims` cancellando.

### Modifica difensiva di `task_masters_status_check` in 0005

0005 esegue `ALTER TABLE task_masters DROP CONSTRAINT task_masters_status_check` e poi `ADD CONSTRAINT` con il **medesimo codominio** già presente dopo 0003: `created`, `ingesting`, `analyzing`, `verifying`, `compiling`, `published`, `blocked`, `failed`, `cancelled`, `archived`, `analyzed_partial`. È una **ricreazione difensiva** del CHECK, **non un'estensione**. Gli stati `compiling` e `published` esistevano già nel CHECK originale di 0001 e nel CHECK ricreato in 0003. **Nessuno status `publication_held` è stato introdotto** né in 0005 né in nessun'altra migration applicata: a livello DB lo status di `task_masters` non ammette `publication_held`.

### Stato `analyzed_partial` come terminale condizionato (semantica gestita dal worker)

Lo schema DB non distingue tra "task in `analyzed_partial` con gate report" e "task in `analyzed_partial` senza gate report": il vincolo è applicativo, gestito dal worker single-consumer. La presenza o assenza di una riga in `final_gate_reports` per il `task_id` determina la terminalità della task per il consumer:
- `analyzed_partial` con `final_gate_reports` esistente per il task → terminale (rejected scenario).
- `analyzed_partial` senza `final_gate_reports` → in attesa, prosegue verso `compiling`.

### Idempotenza sotto redelivery (8.4)

Tutti i vincoli UNIQUE elencati sopra (`agent_runs_attempt_uq`, `draft_final_answers_version_uq`, `coverage_gap_statements_idem_uq`, `final_answer_spans_index_uq`, `fasc_span_entry_uq`, `final_gate_reports_draft_uq`, `published_answers_version_uq`) consentono al worker di usare `INSERT ... ON CONFLICT DO NOTHING` su ogni scrittura. Un doppio delivery dello stesso `task.created` non duplica righe in nessuna tabella 8.4 né eventi audit.

### Evento audit `task.publication_held`

`task.publication_held` viene emesso dal consumer quando, dopo `task.final_gate_completed` con `decision='rejected'`, la task viene riportata da `compiling` a `analyzed_partial`. È **esclusivamente un evento audit**: non corrisponde ad alcuno status di `task_masters` e non è ammesso dal CHECK constraint.

### Cosa NON è installato in 0005

- **Nessun trigger di propagazione** su `published_answers` per le transizioni `published → withdrawn` o `published → superseded`. I campi `withdrawn_at`, `superseded_at`, `superseded_by_id` esistono ma sono guidati esclusivamente da pipeline applicativa che in 8.4 non è ancora scritta.
- **Nessuna tabella `published_answer_lifecycle_events`**. È rinviata a `0006_lifecycle.sql` in 8.5.
- **Nessuna tabella `source_loss_events`**. Rinviata a 0006 in 8.5.
- **Nessun renderer** o tabella di rendering. Fuori scope per MVP-0.

---

## 0006_lifecycle.sql (Sprint 4 — Fase 8.5, da scrivere)

Lifecycle pubblicazione e source loss. Tabelle previste:

- `published_answer_lifecycle_events` — append-only. Eventi: `published`, `withdrawn`, `superseded`.
- `source_loss_events` — registra perdita di accessibilità di una fonte (chunk/evidence_span). Propagator marca i `claim_ledger_entries` impattati e le `published_answers` dipendenti.

Trigger previsti:
- propagatore `source_loss_events → claim_ledger_entries` (nuova versione `disputed_fact` o `unverifiable` con `claim_lineage` `supersedes`).
- propagatore `lifecycle → published_answers` per le transizioni `withdrawn`/`superseded`.

Da scrivere e applicare insieme alla pipeline worker corrispondente.

## 0007_evaluation_retention.sql (Sprint 4–5, da scrivere)

`retention_policies`, `cleanup_jobs`, `storage_usage`, `quota_events`, `eval_runs`, `export_jobs`. Da scrivere insieme alla prima retention pass operativa.

---

## Regola d'oro

Le migration applicate sono immutabili. Le incompatibilità rilevate dopo l'applicazione vengono gestite:
- in **dev** con `make clean` + nuova applicazione delle migration corrette, eventualmente promosse via "foundation patch" documentata;
- in **futuro** (post-MVP-0) con migration additive successive che recuperano l'incoerenza.

Nessuna fase 8.2, 8.2a-patch, 8.3, 8.4 modifica `0001`, `0002`, `0003`, `0004`. Nessuna fase introduce dipendenze AI o riferimenti a provider esterni.
