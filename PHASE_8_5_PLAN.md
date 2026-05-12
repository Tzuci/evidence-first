# PHASE_8_5_PLAN — Evidence-First MVP-0

**Stato della Fase 8.5 dopo il completamento dei blocchi DB, servizi, consumer, dispatcher, multi-stream loop, API e realistic flow tests.**

- Stato corrente: **implementata** (blocchi 1, 2, 3 e 4).
- Commit di completamento: `03c418693f4eb8ba7019c9785d149dbad83b87fe` ("Add realistic source loss flow test").
- Base commit di partenza: `a2739cd50b5d8581f4a2d3e7c0daa4e8324e4aad` ("Update root tests documentation").
- Le migration `0001_foundation.sql`, `0002_storage.sql`, `0003_documents.sql`, `0004_claim_ledger.sql`, `0005_answers_gate.sql` restano **immutabili**.
- `0006_lifecycle.sql` è stata applicata in Fase 8.5 e d'ora in poi è anch'essa immutabile.

Questo documento è ora il record di **stato/risultato** della Fase 8.5. Mantiene il piano architetturale originale dove utile, ma distingue esplicitamente tre piani: **Implementato**, **Non implementato — rinviato**, **Rischi residui**.

---

## 1. Obiettivo della Fase 8.5

La Fase 8.5 introduce il primo livello di **lifecycle delle pubblicazioni** e di **propagazione della perdita di fonte**, preservando integralmente le proprietà raggiunte in 8.4:

- evidence-first ed evidence-gated;
- append-only su `audit_records`, `evidence_spans`, `claim_ledger_entries`, `final_answer_spans`, `final_gate_reports` e su tutte le nuove tabelle di lifecycle/source loss;
- audit chain hash-linked verificabile end-to-end via `verify_audit_chain` / `verify_task_audit_chain`;
- idempotenza completa sotto redelivery, sia a livello consumer (`event_processing_records`) sia a livello dominio (UNIQUE constraints applicativi);
- pipeline mock-driven / deterministica, **nessun provider AI reale**, **costo API = 0**;
- closed corpus only, nessun retrieval esterno.

Gli obiettivi concreti perseguiti e ora soddisfatti:

1. tracciare in modo append-only gli eventi lifecycle delle `published_answers`;
2. gestire la **withdrawal asincrona** di un `published_answers` tramite richiesta API che pubblica un evento, processato da un consumer dedicato che è la sola entità autorizzata a mutare i campi lifecycle di `published_answers`;
3. registrare in modo append-only i `source_loss_events`, con granularità canonica `evidence_span_id`;
4. propagare la source loss al **Claim Ledger** in modo append-only, creando una nuova `claim_ledger_entries v(N+1)` con `state='unverifiable'`, `transition_reason='source_lost'` e una `claim_lineage` con `relation_kind='supersedes'`;
5. identificare le `published_answers` **attive** (`status='published'`) impattate da una source loss e registrarle in `source_loss_propagation_records`, **senza** ritirarle automaticamente.

Il supersede automatico di `vN` quando viene pubblicata una `v(N+1)` è stato **pianificato come effetto futuro** ma **non implementato** in 8.5 (non c'è oggi una pipeline che crei `v(N+1)` per uno stesso task).

---

## 2. Non-obiettivi (esplicitamente fuori scope, restano tali)

- Nessun provider AI reale; nessun riferimento operativo a OpenAI, Anthropic, Google o equivalenti.
- Nessun web retrieval, Verified Web Mode, Hybrid Mode.
- Nessun renderer / export Markdown / HTML / PDF / DOCX / JSON-LD.
- Nessun cleanup distruttivo, nessuna retention reale, nessun export job — tutto rinviato a `0007_evaluation_retention.sql` o oltre.
- Nessun nuovo stato `task_masters.status`.
- Nessun trigger DB di propagazione `source_loss_events → claim_ledger_entries` né `lifecycle → published_answers`.
- Nessuna modifica alle migration 0001–0005.
- Nessun endpoint manuale di supersede.
- Nessun consensus engine, contradiction detector avanzato, source quality evaluator, critical reviewer.
- Nessun OCR / parsing PDF / vector store cloud / S3 / GCS / Azure operativo.
- Nessuna estensione di `verification_records.check_kind`.
- Nessuna estensione di `claim_lineage.relation_kind`.

---

## 3. Vincoli invarianti da 8.4 (rimangono attivi)

- `PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0`.
- Closed corpus only.
- Append-only enforced via trigger.
- `Engine.execute()` non usata da codice applicativo; tutto passa per `Connection`.
- `verify_task_audit_chain(conn, task_id=...)` accetta una `Connection`.
- I test API non importano dal worker; i test worker possono importare `app.consumers.*` e `app.services.*`.
- I realistic flow test sotto `tests/` caricano il package worker via `importlib.util` sotto alias `_wapp` per evitare la collisione di nome con il package `app` dell'API.
- Audit chain hash-linked con normalizzazione del payload via `_normalize_payload` in `evidencefirst_shared.db.audit`.
- Claim ledger append-only stretto; supersede esclusivamente via `claim_lineage.relation_kind='supersedes'`.
- `published_answers.status` ammette `published`, `withdrawn`, `superseded` (definito in 0005); colonne `withdrawn_at`, `superseded_at`, `superseded_by_id` esistenti dal 0005.
- `task_masters.status` non viene esteso. Resta il codominio fissato in 0005.

---

## 4. Implementato — riepilogo dei blocchi conclusi

### Blocco 1 — DB / shared

- `migrations/0006_lifecycle.sql`:
  - `published_answer_lifecycle_events` append-only via `reject_modify_append_only`, FK composita `(published_answer_id, task_id) → published_answers(id, task_id)`, UNIQUE `pale_idempotency_uq (published_answer_id, event_type, idempotency_key)`, CHECK su `event_type ∈ {published, withdrawal_requested, withdrawn, superseded}`, indici cronologici per `published_answer_id` e per `task_id`;
  - `source_loss_events` append-only, FK `evidence_span_id → evidence_spans(id)` ON DELETE RESTRICT, FK opzionali su `document_chunk_id`, `document_version_id`, `document_id` (reporting context), CHECK su `loss_kind ∈ {source_deleted, source_access_lost, quote_mismatch, document_replaced, policy_retraction}`, UNIQUE `sle_idempotency_uq (evidence_span_id, loss_kind, idempotency_key)`;
  - `source_loss_propagation_records` append-only, FK opzionali a `logical_claims`, `claim_ledger_entries` (old/new), `published_answers`, CHECK su `propagation_kind ∈ {claim_marked_unverifiable, published_answer_impacted, no_claims_impacted, no_active_published_answers_impacted}`, CHECK su `status ∈ {recorded, skipped, failed}`. Idempotenza tramite quattro **partial unique indexes**, uno per `propagation_kind`, ristretti a `status IN ('recorded','skipped')`. Le righe `failed` non consumano la slot e restano come storia append-only.
- Estensione di `packages/shared/evidencefirst_shared/schemas.py` con `PublishedAnswerLifecycleEventRead`, `SourceLossEventRead`, `SourceLossPropagationRecordRead`.

### Blocco 2 — Servizi worker

- `apps/worker/app/services/published_answer_lifecycle.py`. Funzione `apply_withdrawal`. Unico scrittore autorizzato dei campi lifecycle di `published_answers` per il path di withdrawal. Acquisisce `SELECT ... FOR UPDATE OF pa`, inserisce idempotente gli eventi `withdrawal_requested` e `withdrawn`, esegue UPDATE status-guarded `WHERE status='published'`, emette `published_answer.withdrawn` su `chain_scope='task'` **solo** quando l'UPDATE muta una riga. Outcomes: `withdrawn`, `already_withdrawn`, `already_superseded`, `unsupported_status`, `not_found`.
- `apps/worker/app/services/source_loss_propagator.py`. Funzione `propagate_source_loss`. Risolve l'impact set da `evidence_span_id`. Per ogni claim impattato: lock `FOR UPDATE` su `logical_claims`, se head `verified_fact` → append v(N+1) `unverifiable / unsupported / source_lost`, lineage `supersedes` con ON CONFLICT DO NOTHING; se head già `unverifiable/source_lost` → `skipped`. Propagation rows in `source_loss_propagation_records` idempotenti via partial UNIQUE. Audit `source_loss.propagated_to_claim` emesso solo se la propagation row viene effettivamente inserita su questa chiamata. Per le published_answers attive impattate (join standard) → `published_answer_impacted` + audit `source_loss.propagated_to_published_answer`. Gestiti `no_claims_impacted` e `no_active_published_answers_impacted`. Non muta `published_answers.status`, non scrive in `published_answer_lifecycle_events`.

### Blocco 3 — Consumer e dispatcher

- `apps/worker/app/consumers/published_answer_withdrawal.py`. `consumer_name` stabile `"published_answer_withdrawal"`. Risolve scope da `published_answers JOIN task_masters`. EPR consumer-level con `event_processing_records`. Branch FK-safe (`WORKER_PUBLISHED_ANSWER_NOT_VISIBLE`). Rifiuto pre-transaction per campi obbligatori malformati e per `requested_by` malformato.
- `apps/worker/app/consumers/source_loss.py`. `consumer_name` stabile `"source_loss"`. Risolve scope da `source_loss_events LEFT JOIN task_masters` con `COALESCE` su tenant/project; `task_id` può restare NULL. EPR consumer-level. Branch FK-safe (`WORKER_SOURCE_LOSS_EVENT_NOT_VISIBLE`).
- `apps/worker/app/consumers/dispatch.py`. `handle_event(event, *, redis_consumer_name=None)`. Routing per `event_type`:
  - `task.created` / `task_created` → `handle_task_created` con `consumer_name = redis_consumer_name or "worker_dispatch"`;
  - `published_answer.withdrawal_requested` → `handle_published_answer_withdrawal` senza forwardare `redis_consumer_name`;
  - `source_loss.detected` → `handle_source_loss` senza forwardare `redis_consumer_name`.
  Il non-forwarding è la decisione vincolante che mantiene globale la UNIQUE EPR per i due nuovi consumer attraverso più worker instances.
- `apps/worker/app/main.py`. `xreadgroup` multi-stream su tre stream con gruppo `worker_default` condiviso. ACK sullo stream concreto restituito dalla risposta. `_ACK_STATUSES = {processed, skipped_already_succeeded, skipped_in_flight, skipped_terminal}`; `failed` lascia pending.

### Blocco 4 — API

- `POST /api/v1/published-answers/{published_answer_id}/withdrawal-requests` in `apps/api/app/routes/answers.py`. Read-only DB lato API (nessuna mutazione di `published_answers`, nessun INSERT in `published_answer_lifecycle_events`). XADD su `app.events.published_answer_withdrawal_requested`. `event_payload` opzionale serializzato JSON compatto sort_keys sotto la chiave Redis `event_payload_json`. Errori normalizzati: `404 RESOURCE_NOT_FOUND`, `500 INTERNAL_ERROR` su fallimento XADD.
- `POST /api/v1/source-loss-events` in `apps/api/app/routes/source_loss.py`. Risolve scope canonico via catena evidence_span → chunk → version → document. `task_id` lasciato NULL by-design. INSERT in `source_loss_events` e XADD su `app.events.source_loss_detected` nella **stessa transazione**: se l'XADD fallisce, la riga viene rollback. Errori normalizzati: `404 RESOURCE_NOT_FOUND` (`details.resource="evidence_spans"`), `409 RESOURCE_CONFLICT` su collisione UNIQUE `(evidence_span_id, loss_kind, idempotency_key)` (`details.resource="source_loss_events"`), `500 INTERNAL_ERROR` su fallimento XADD.
- Registrazione dei nuovi router in `apps/api/app/main.py`.

### Test scritti in Fase 8.5

Test unitari e di scenario:

- `apps/api/tests/test_published_answer_withdrawal_request.py` (9 scenari, FakeRedis monkey-patchato sul route module).
- `apps/api/tests/test_source_loss_endpoint.py` (11 scenari, FakeRedis monkey-patchato sul route module; verifica anche il rollback del DB quando l'XADD fallisce).
- `apps/worker/tests/test_published_answer_withdrawal_consumer.py` (10 scenari, includono il rifiuto pre-transaction per `requested_by` malformato).
- `apps/worker/tests/test_source_loss_consumer.py` (10 scenari).
- `apps/worker/tests/test_source_loss_propagator_service.py` (6 scenari, include not_found, idempotenza, no_claims_impacted, no_active_published_answers_impacted).
- `apps/worker/tests/test_dispatch.py` (9 scenari; copre invariante "redis_consumer_name non forwardato a withdrawal/source_loss" tramite fake handlers che rifiuterebbero `consumer_name` come kw).
- `apps/worker/tests/test_main_multistream.py` (7 scenari, FakeRedis interno al modulo `app.main`).

Realistic flow tests cross-component:

- `tests/test_phase_8_5_withdrawal_flow.py`. API → FakeRedis → dispatcher → consumer → service → DB; tre delivery che esercitano `processed`, `skipped_already_succeeded`, e variante con fresh consumer-level key + stesso lifecycle key.
- `tests/test_phase_8_5_source_loss_flow.py`. API → FakeRedis → dispatcher → consumer → propagator → DB; tre delivery con transizione `verified_fact → unverifiable/source_lost`, `no_active_published_answers_impacted`, redelivery `skipped_already_succeeded`, redelivery con fresh consumer key che esita in propagation `skipped` via partial UNIQUE.

---

## 5. Schema 0006 — riferimento sintetico

Nomi reali, dal file `migrations/0006_lifecycle.sql`.

### 5.1 `published_answer_lifecycle_events`

- PK `id UUID`.
- `published_answer_id UUID NOT NULL`, `task_id UUID NOT NULL`.
- `event_type` CHECK in `{published, withdrawal_requested, withdrawn, superseded}`.
- `event_reason TEXT NOT NULL`, `event_payload JSONB NOT NULL DEFAULT '{}'::jsonb`.
- `requested_by UUID NULL` con FK a `users(id)` ON DELETE RESTRICT.
- `idempotency_key TEXT NOT NULL`.
- FK composita `pale_published_answer_consistency (published_answer_id, task_id) → published_answers(id, task_id)` ON DELETE RESTRICT.
- UNIQUE `pale_idempotency_uq (published_answer_id, event_type, idempotency_key)`.
- Indici: `(published_answer_id, created_at)`, `(task_id, created_at)`.
- Trigger append-only `published_answer_lifecycle_events_append_only`.

### 5.2 `source_loss_events`

- PK `id UUID`.
- `tenant_id UUID NOT NULL` FK `tenants(id)` ON DELETE RESTRICT.
- `project_id UUID NULL`, `task_id UUID NULL` con FK ON DELETE RESTRICT.
- `evidence_span_id UUID NOT NULL` FK `evidence_spans(id)` ON DELETE RESTRICT.
- `document_chunk_id`, `document_version_id`, `document_id` (reporting), tutti opzionali con ON DELETE RESTRICT.
- `loss_kind` CHECK in `{source_deleted, source_access_lost, quote_mismatch, document_replaced, policy_retraction}`.
- `loss_reason TEXT NOT NULL`, `detected_by TEXT NOT NULL`, `event_payload JSONB`, `idempotency_key TEXT NOT NULL`.
- UNIQUE `sle_idempotency_uq (evidence_span_id, loss_kind, idempotency_key)`.
- Indici: `(evidence_span_id)`, `(task_id, created_at)`, `(project_id, created_at)`.
- Trigger append-only `source_loss_events_append_only`.

### 5.3 `source_loss_propagation_records`

- PK `id UUID`.
- `source_loss_event_id UUID NOT NULL` FK `source_loss_events(id)` ON DELETE RESTRICT.
- `claim_logical_id`, `old_claim_ledger_entry_id`, `new_claim_ledger_entry_id`, `published_answer_id` opzionali, tutti FK ON DELETE RESTRICT.
- `propagation_kind` CHECK in `{claim_marked_unverifiable, published_answer_impacted, no_claims_impacted, no_active_published_answers_impacted}`.
- `status` CHECK in `{recorded, skipped, failed}`.
- `details JSONB NOT NULL DEFAULT '{}'::jsonb`.
- Partial unique indexes:
  - `slpr_claim_marked_unverifiable_uq` su `(source_loss_event_id, propagation_kind, claim_logical_id) WHERE propagation_kind='claim_marked_unverifiable' AND status IN ('recorded','skipped') AND claim_logical_id IS NOT NULL`;
  - `slpr_published_answer_impacted_uq` analogo su `published_answer_id`;
  - `slpr_no_claims_impacted_uq` su `(source_loss_event_id, propagation_kind) WHERE propagation_kind='no_claims_impacted' AND status IN ('recorded','skipped')`;
  - `slpr_no_active_published_answers_impacted_uq` analogo per `no_active_published_answers_impacted`.
- Indici di lookup: `(source_loss_event_id)`, `(published_answer_id) WHERE published_answer_id IS NOT NULL`.
- Trigger append-only `source_loss_propagation_records_append_only`.

### 5.4 Cosa non è entrato in 0006

- Nessun `ALTER TABLE task_masters`.
- Nessuna modifica a `lc_block_delete_if_published` (decisione consapevole: withdrawn/superseded continuano a non bloccare il DELETE di `logical_claims`).
- Nessun trigger DB di propagazione.
- Nessuna estensione di `verification_records.check_kind` né di `claim_lineage.relation_kind`.

---

## 6. Decisioni vincolanti rispettate dal codice

1. `task_masters.status` non viene esteso in 8.5. Niente `withdrawn`, `superseded`, `publication_held` come status DB.
2. Source loss → Claim Ledger: append-only, `state='unverifiable'`, `support_scope='unsupported'`, `user_provided_dependency='unsupported'`, `transition_reason='source_lost'`, lineage `relation_kind='supersedes'`. Nessuna estensione di `relation_kind` o `check_kind`. Niente `verification_records` per source loss.
3. `source_loss_events` append-only; granularità canonica `evidence_span_id`. I campi document_* sono reporting.
4. `lc_block_delete_if_published` non modificato.
5. Propagazione source loss interamente applicativa nel propagator + consumer, mai trigger DB.
6. API withdrawal solo come richiesta asincrona; non muta `published_answers` nel request path. Supersede non è endpoint manuale.
7. Retention: non implementata. Rinviata a `0007_evaluation_retention.sql`.

---

## 7. Non implementato / rinviato

Non scritto nel repo al commit corrente:

- **Provider AI reali**, prompt template popolati.
- **Web retrieval**, **Verified Web Mode**, **Hybrid Mode**.
- **Consensus engine** avanzato, **contradiction detector** avanzato, **source quality evaluator** avanzato, **critical reviewer**.
- **Renderer ed export** Markdown/HTML/PDF/DOCX/JSON-LD.
- **Auth e RBAC reali.**
- **Retention reale distruttiva** (cleanup blob orfani, retention job): `0007_evaluation_retention.sql` ancora da scrivere.
- **UI completa.**
- **OCR / parsing PDF, vector store cloud, S3/GCS/Azure operativo.**
- **Read API** per i nuovi domini lifecycle/source loss: `GET /api/v1/published-answers/{id}/lifecycle-events`, `GET /api/v1/source-loss-events`, `GET /api/v1/source-loss-events/{id}`, `GET /api/v1/source-loss-events/{id}/propagation`, `GET /api/v1/published-answers/{id}/source-loss-impact`. Erano nel piano originale, non sono state implementate in 8.5.
- **OpenAPI manuale curato** oltre allo schema FastAPI auto-generato.
- **Backfill `published` lifecycle events** per pubblicazioni create in 8.4: nessuno script di backfill viene eseguito dalla migration; nessuna pipeline lazy lo esegue nei consumer 8.5 letti.
- **Supersede automatico** di `vN` quando viene creata una nuova `v(N+1)` per lo stesso task: non c'è pipeline che crei nuove versioni.
- **DLQ esplicita** per il worker: oggi le entry `failed` restano pending nel PEL e vanno gestite con XPENDING / XCLAIM.

---

## 8. Rischi residui (reali, verificati dai file)

1. **Redis nei realistic flow è FakeRedis.** I file `tests/test_phase_8_5_withdrawal_flow.py` e `tests/test_phase_8_5_source_loss_flow.py` installano un `FakeRedis` come ritorno di `get_redis` sui rispettivi route module. Non c'è interazione con un Redis reale.
2. **Worker main loop non viene avviato nei realistic flow.** I due test ricostruiscono l'evento dai field osservati dal `FakeRedis.xadd` e lo passano direttamente a `dispatch.handle_event`. `XREADGROUP`, `XACK`, gestione PEL e signal handlers non vengono attraversati nei realistic flow.
3. **`XREADGROUP`/`XACK` sono coperti solo da unit test dedicati.** `apps/worker/tests/test_main_multistream.py` esercita `xreadgroup`, `xack`, `xgroup_create` con un `FakeRedis` interno al modulo `app.main`. Non esiste un end-to-end con Redis reale nel repo.
4. **Nessuna auth/RBAC reale.** L'API non autentica le richieste in MVP-0. Gli endpoint producer di withdrawal e source loss sono protetti solo dai vincoli applicativi.
5. **`source_loss` `event_payload_json` non viene riletto dal consumer.** L'API serializza `event_payload` sotto la chiave Redis `event_payload_json`. Il consumer `apps/worker/app/consumers/source_loss.py` non converte questo campo in `event_payload` dict; il propagator legge `event_payload` direttamente dalla riga `source_loss_events` (JSONB) e ignora il campo di stream. Conseguenza: payload custom passati dal client non sono usati dal propagator se non già persistiti nella riga DB. È coerente con il design (il payload è preservato sul DB), ma non è simmetrico con il consumer withdrawal e va ricordato.
6. **Nessuna DLQ esplicita.** Le entry il cui handler ritorna `failed` restano pending nel PEL del consumer group e devono essere gestite a mano (XPENDING / XCLAIM). Non c'è un destination stream per dead-lettering.
7. **Nessuna OpenAPI curata.** Non esiste un file di spec manuale oltre allo schema auto-generato da FastAPI.
8. **Nessuna retention reale distruttiva.** Il volume del DB cresce con tutti i lifecycle events, source loss events e propagation records senza politiche di pruning. Accettabile per MVP-0 ma da affrontare con `0007_evaluation_retention.sql`.
9. **Concorrenza su `claim_ledger_entries.version_no`.** Il propagator acquisisce `FOR UPDATE` su `logical_claims` prima di calcolare la prossima `version_no`. Questo previene il conflitto in pratica all'interno della singola transazione, ma due propagator concorrenti su processi separati hanno una window molto stretta tra l'INSERT della propagation row e il commit della loro transazione. La UNIQUE `cle_logical_version_uq` rimane il backstop a DB.
10. **`task_masters.status` resta `published` anche dopo una withdrawal.** Decisione consapevole: il lifecycle vive su `published_answers`. È necessario ricordarlo quando si interpretano dashboard di task.

---

## 9. Command suite usate

Lista delle suite di test pertinenti alla Fase 8.5, come definite nel `Makefile` del repo:

- `make test-db` — esegue `pytest -q tests/` (include i due realistic flow test).
- `make test-shared` — esegue i test di `packages/shared/tests/`.
- `make test-api` — esegue `pytest -q tests/` da `apps/api/` (include i test 8.5 di withdrawal request e source-loss endpoint).
- `make test-worker` — esegue `pytest -q tests/` da `apps/worker/` (include i test 8.5 di consumer dispatcher, multi-stream, withdrawal consumer, source_loss consumer, source_loss_propagator service).
- `make test` — esegue tutte le suite, gate finale.

Non riportiamo conteggi numerici di test/pass/fail: questo documento non li dichiara perché non sono presenti nei file letti.

---

## 10. Smoke test 8.5 (riferimento dal piano originale)

Lo smoke test seguente era previsto dal piano. La sequenza è ancora valida concettualmente, ma in 8.5 si appoggia ai due nuovi endpoint API per i producer (`withdrawal-requests` e `source-loss-events`); il polling sui side-effects asincroni avviene leggendo i normali endpoint read-only 8.4 esistenti, dato che in 8.5 non sono stati aggiunti read endpoint dedicati per lifecycle events o source loss events.

```bash
# 1) Crea task approved fino a published (smoke 8.4 invariato).
# 2) POST /api/v1/published-answers/{PAID}/withdrawal-requests
# 3) Polling su GET /api/v1/published-answers/{PAID} fino a status='withdrawn'
# 4) POST /api/v1/source-loss-events su un evidence_span
# 5) Polling su GET /api/v1/claims/{logical_id}/history fino a vedere v(N+1) 'unverifiable / source_lost'
# 6) GET /api/v1/tasks/{TID}/audit  -> include
#    published_answer.withdrawn, source_loss.propagated_to_claim,
#    source_loss.propagated_to_published_answer (se published_answer attivo impattato)
```

---

## 11. Roadmap implicita (non scritta)

Direzioni naturali per fasi successive, da decidere con prompt operativo separato. Non sono nel codice oggi:

- read API per i domini introdotti in 8.5;
- `0007_evaluation_retention.sql`;
- estensione dei realistic flow a smoke test con Redis reale e worker loop reale;
- aggiornamento di `docs/migration_plan.md` (non incluso negli aggiornamenti correnti) per riflettere la scelta "propagator pipeline applicativa, non trigger DB".

Nessuna di queste è obbligata: è uno spazio di lavoro futuro, non un commitment.
