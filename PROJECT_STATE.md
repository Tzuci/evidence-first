# PROJECT_STATE — Evidence-First MVP-0

Documento di onboarding tecnico, una pagina, leggibile dal collaboratore al primo accesso senza dover leggere il codice. Riflette lo stato del repo al commit **Fase 8.5**: `03c418693f4eb8ba7019c9785d149dbad83b87fe` ("Add realistic source loss flow test").

---

## Cosa è il progetto

Piattaforma multi-AI **evidence-first** ed **evidence-gated**. Nessun claim fattuale può finire nella risposta finale se non è collegato a evidenze tracciabili, registrate nel Claim Ledger, verificate e approvate dal Final Answer Gate. La verità non è ciò che dice un modello AI: è ciò che le evidenze recuperate, archiviate, tracciate e verificate dal sistema supportano.

In MVP-0 il nucleo evidence-gated è costruito **prima** della visione multi-AI. Provider AI reali, Verified Web Mode, Hybrid Mode, consensus engine, contradiction detector avanzato, source quality evaluator e critical reviewer sono fasi future. Il claim "evidence-gated" qui significa: esiste una base append-only verificabile end-to-end per draft/gate/published, più una propagazione lifecycle e source-loss minimale per MVP-0. Non è una soluzione completa al problema delle allucinazioni.

---

## Stato migration

| Migration | Stato |
|---|---|
| `0001_foundation.sql` | applicata, immutabile |
| `0002_storage.sql` | applicata, immutabile |
| `0003_documents.sql` | applicata, immutabile |
| `0004_claim_ledger.sql` | applicata, immutabile |
| `0005_answers_gate.sql` | applicata, immutabile |
| `0006_lifecycle.sql` | applicata (Fase 8.5) |
| `0007_evaluation_retention.sql` | da scrivere |

---

## Cosa esiste oggi (Fasi 8.4 + 8.5)

### Base 8.4 (invariata)

- **DB foundation multi-tenant**: `tenants`, `users`, `projects`, `sessions`, `task_masters`, `event_processing_records`, `policy_versions`.
- **Audit chain hash-linked, append-only, verificabile end-to-end** via `verify_audit_chain` / `verify_task_audit_chain`. Append-only enforced a DB tramite trigger comune `reject_modify_append_only`.
- **Storage layer content-addressed, deduplicato, refcount-based**: `storage_blobs`, `storage_objects`. Dedup global concorrenza-safe via `INSERT ... ON CONFLICT DO NOTHING` sull'indice parziale `sb_global_uq`.
- **Document store** con upload reale `.txt`/`.md`, chunking deterministico, `evidence_spans` minimali, `task_documents`. `evidence_spans` append-only.
- **Claim Ledger append-only stretto**: `logical_claims`, `raw_claims`, `classified_claims`, `claim_ledger_entries` (append-only via trigger), `claim_lineage`, `claim_evidence_links`, `verification_records`. Supersede esclusivamente via `claim_lineage.relation_kind='supersedes'`.
- **Extractor mock-driven**, **CVE-lite mock-driven**, **Compiler mock-driven**, **Final Answer Gate mock-driven**.
- **Worker single-consumer 8.4** per `task.created` (`apps/worker/app/consumers/task_created.py`), FK-safe, resume-safe, idempotente.
- **Coerenza referenziale stretta a DB** tra `task_masters` ↔ `draft_final_answers` ↔ `final_gate_reports` ↔ `published_answers` via UNIQUE composite e FK composite.

### Cosa esiste oggi — Fase 8.5

Tutto quanto segue è nel repo al commit `03c418693f4eb8ba7019c9785d149dbad83b87fe` ed è verificabile leggendo i file indicati.

**Schema (migration `0006_lifecycle.sql`).** Tre tabelle append-only, append-only enforced via trigger comune `reject_modify_append_only`:

- `published_answer_lifecycle_events`. FK composita `(published_answer_id, task_id) → published_answers(id, task_id)` con `ON DELETE RESTRICT`. `event_type ∈ {published, withdrawal_requested, withdrawn, superseded}`. UNIQUE `(published_answer_id, event_type, idempotency_key)` come `pale_idempotency_uq`. Indici per lettura cronologica per published_answer e per task.
- `source_loss_events`. FK `evidence_span_id → evidence_spans(id)` ON DELETE RESTRICT come granularità canonica. Colonne reporting opzionali: `document_chunk_id`, `document_version_id`, `document_id`. `loss_kind ∈ {source_deleted, source_access_lost, quote_mismatch, document_replaced, policy_retraction}`. UNIQUE `(evidence_span_id, loss_kind, idempotency_key)` come `sle_idempotency_uq`. `tenant_id` NOT NULL; `project_id` e `task_id` NULL-ammessi.
- `source_loss_propagation_records`. FK `source_loss_event_id → source_loss_events(id)`. FK opzionali a `logical_claims`, `claim_ledger_entries` (old/new), `published_answers`. `propagation_kind ∈ {claim_marked_unverifiable, published_answer_impacted, no_claims_impacted, no_active_published_answers_impacted}`. `status ∈ {recorded, skipped, failed}`. Idempotenza tramite quattro partial unique indexes, uno per `propagation_kind`, ristretti a `status IN ('recorded','skipped')` in modo che le righe `failed` restino come storia append-only senza consumare la slot.

**Servizi worker.**

- `apps/worker/app/services/published_answer_lifecycle.py`. Funzione `apply_withdrawal(conn, ...)`. È l'unico scrittore autorizzato dei campi lifecycle di `published_answers` (`status`, `withdrawn_at`) per il path di withdrawal. Acquisisce `SELECT ... FOR UPDATE OF pa` sulla riga, inserisce in modo idempotente gli eventi `withdrawal_requested` e `withdrawn` (ON CONFLICT DO NOTHING sul vincolo `pale_idempotency_uq`), esegue l'UPDATE status-guarded `WHERE status='published'`, ed emette `published_answer.withdrawn` su `chain_scope='task'` soltanto quando l'UPDATE muta effettivamente una riga. Outcome: `withdrawn`, `already_withdrawn`, `already_superseded`, `unsupported_status`, `not_found`.
- `apps/worker/app/services/source_loss_propagator.py`. Funzione `propagate_source_loss(conn, ...)`. Risolve l'impact set partendo da `evidence_span_id` su `claim_evidence_links`; per ogni claim impattato acquisisce `FOR UPDATE` su `logical_claims` e, se la head è `verified_fact`, appende una `claim_ledger_entries v(N+1)` con `state='unverifiable'`, `support_scope='unsupported'`, `user_provided_dependency='unsupported'`, `transition_reason='source_lost'`; inserisce `claim_lineage` con `relation_kind='supersedes'` (ON CONFLICT DO NOTHING); registra `source_loss_propagation_records` `claim_marked_unverifiable` come `recorded` o `skipped`; emette audit `source_loss.propagated_to_claim` solo se la propagation row è stata effettivamente inserita su questa chiamata. Calcola le published_answers attive (`status='published'`) impattate via join `published_answers → draft_final_answers → final_answer_spans → final_answer_span_claim_links → logical_claims` e registra `published_answer_impacted` + audit `source_loss.propagated_to_published_answer` per ognuna. Il propagator **non** muta `published_answers.status` né scrive in `published_answer_lifecycle_events`. Esistono inoltre le righe `no_claims_impacted` e `no_active_published_answers_impacted` per i casi degeneri.

**Consumer worker.**

- `apps/worker/app/consumers/published_answer_withdrawal.py`. Entry point `handle_published_answer_withdrawal(event, *, consumer_name=CONSUMER_NAME_DEFAULT="published_answer_withdrawal")`. Risolve `(tenant_id, project_id, task_id)` dalla riga `published_answers` joinata con `task_masters`, apre EPR consumer-level (`event_processing_records`), delega al servizio, classifica l'outcome (`processed`, `skipped_already_succeeded`, `failed`). Branch FK-safe per published_answer non visibile (`WORKER_PUBLISHED_ANSWER_NOT_VISIBLE`), rifiuto hard pre-transaction per campi obbligatori o `requested_by` malformati.
- `apps/worker/app/consumers/source_loss.py`. Entry point `handle_source_loss(event, *, consumer_name=CONSUMER_NAME_DEFAULT="source_loss")`. Risolve scope dalla riga `source_loss_events` via LEFT JOIN con `task_masters` (`COALESCE` su tenant/project, `task_id` può restare NULL). Apre EPR consumer-level, delega al propagator, classifica l'outcome. Branch FK-safe `WORKER_SOURCE_LOSS_EVENT_NOT_VISIBLE`.

**Dispatcher e loop multi-stream worker.**

- `apps/worker/app/consumers/dispatch.py`. `handle_event(event, *, redis_consumer_name=None)` instrada in base a `event_type`:
  - `task.created` / alias legacy `task_created` → `handle_task_created` con `consumer_name = redis_consumer_name or "worker_dispatch"`;
  - `published_answer.withdrawal_requested` → `handle_published_answer_withdrawal` senza forwardare `redis_consumer_name`;
  - `source_loss.detected` → `handle_source_loss` senza forwardare `redis_consumer_name`.
  Per i due nuovi consumer la scelta di non forwardare la consumer name per-istanza è deliberata: la UNIQUE EPR `(consumer_name, idempotency_key)` deve restare globale tra worker instances.
- `apps/worker/app/main.py`. `xreadgroup` su tre stream con consumer group condiviso `worker_default`:
  - `app.events.task_created`
  - `app.events.published_answer_withdrawal_requested`
  - `app.events.source_loss_detected`
  `xack` viene chiamato sullo stream concreto restituito dalla risposta `xreadgroup`. ACK solo per statuses `{processed, skipped_already_succeeded, skipped_in_flight, skipped_terminal}`; `failed` lascia l'entry pending. Il loop tollera nomi di stream e entry-id sia `bytes` che `str`.

**API.**

- `POST /api/v1/published-answers/{published_answer_id}/withdrawal-requests` in `apps/api/app/routes/answers.py`. Risolve `(task_id, tenant_id, project_id)` con un SELECT read-only, **non muta** `published_answers` né `published_answer_lifecycle_events`, pubblica un evento `published_answer.withdrawal_requested` su `app.events.published_answer_withdrawal_requested`. Ritorna `202 Accepted` con envelope contenente `event_id`, `idempotency_key`, `lifecycle_idempotency_key`, `stream`. `event_payload` opzionale viene serializzato come JSON compatto sort_keys sotto la chiave Redis `event_payload_json` (il consumer lo ignora; resta per replay/forensic). Errori: `404 RESOURCE_NOT_FOUND` con `details.resource="published_answers"`, `500 INTERNAL_ERROR` se l'XADD fallisce.
- `POST /api/v1/source-loss-events` in `apps/api/app/routes/source_loss.py`. Risolve scope canonico (`tenant_id`, `project_id`, `document_chunk_id`, `document_version_id`, `document_id`) dalla catena `evidence_spans → document_chunks → document_versions → uploaded_documents`. `task_id` viene volutamente lasciato NULL: una span può supportare claim di task diversi. INSERT in `source_loss_events` e XADD su `app.events.source_loss_detected` **nella stessa transazione** (rollback se XADD fallisce, niente righe orfane). Errori: `404 RESOURCE_NOT_FOUND` (`details.resource="evidence_spans"`), `409 RESOURCE_CONFLICT` su collisione UNIQUE `(evidence_span_id, loss_kind, idempotency_key)`, `500 INTERNAL_ERROR` su fallimento XADD.

**Endpoint API attivi (tabella aggiornata).**

| Endpoint | Descrizione |
|---|---|
| `POST /api/v1/projects` | Crea progetto |
| `GET /api/v1/projects` | Lista progetti |
| `GET /api/v1/projects/{id}` | Dettaglio progetto |
| `POST /api/v1/projects/{id}/documents` | Upload documento `.txt`/`.md` |
| `GET /api/v1/projects/{id}/documents` | Lista documenti progetto |
| `GET /api/v1/documents/{id}` | Dettaglio documento |
| `GET /api/v1/documents/{id}/chunks` | Chunks del documento |
| `POST /api/v1/tasks` | Crea task closed_corpus |
| `GET /api/v1/tasks/{id}` | Dettaglio task |
| `GET /api/v1/tasks/{id}/documents` | Documenti collegati al task |
| `GET /api/v1/tasks/{id}/audit` | Catena audit del task |
| `GET /api/v1/tasks/{id}/raw-claims` | Raw claims del task |
| `GET /api/v1/tasks/{id}/classified-claims` | Classified claims del task |
| `GET /api/v1/tasks/{id}/claims` | Latest ledger entry per logical claim |
| `GET /api/v1/claims/{logical_id}/history` | Storia di un logical claim |
| `GET /api/v1/claims/{logical_id}/evidence` | Aggregato latest + links + verifications |
| `GET /api/v1/tasks/{id}/draft` | Draft v1 + spans (8.4) |
| `GET /api/v1/tasks/{id}/final-gate-report` | Gate report + coverage gaps (8.4) |
| `GET /api/v1/tasks/{id}/published-answer` | Published answer per task (8.4) |
| `GET /api/v1/published-answers/{id}` | Published answer per id (8.4) |
| `POST /api/v1/published-answers/{published_answer_id}/withdrawal-requests` | Producer asincrono di withdrawal (8.5) |
| `POST /api/v1/source-loss-events` | Producer asincrono di source loss (8.5) |
| `GET /health/live` / `/health/db` / `/health/queue` / `/health/storage` / `/health/ready` | Health checks |

**Schemi shared aggiunti in 8.5** (`packages/shared/evidencefirst_shared/schemas.py`): `PublishedAnswerLifecycleEventRead`, `SourceLossEventRead`, `SourceLossPropagationRecordRead`.

**Test realistici cross-component (`tests/`)**:

- `tests/test_phase_8_5_withdrawal_flow.py`. Realistic flow API → FakeRedis → dispatcher → withdrawal consumer → lifecycle service → DB + audit. Esercita prima delivery, redelivery con stessa idempotency_key, e variante con fresh consumer-level key e stesso lifecycle key (idempotency stratificata).
- `tests/test_phase_8_5_source_loss_flow.py`. Realistic flow API → FakeRedis → dispatcher → source_loss consumer → propagator → DB + audit. Esercita propagazione del claim a `unverifiable/source_lost`, lineage `supersedes`, `no_active_published_answers_impacted`, e due redelivery con esiti `skipped_already_succeeded` e `processed` no-op.

Entrambi caricano il package worker sotto alias `_wapp` via `importlib.util` per evitare la collisione di nome con il package `app` dell'API.

---

## Pipeline 8.4 (sintesi, invariata)

### Task con documenti, approved scenario

`task.created` → `task.docs_attached` (API) → `task.analyzing` → `task.docs_loaded` → `task.claims_extracted` → `task.claims_classified` → `task.claims_ledger_initialized` → `task.cve_lite_started` → `task.cve_lite_completed` → `task.analyzed_partial` → `task.compiling` → `task.draft_compiled` → `task.final_gate_started` → `task.final_gate_completed` → `task.published`.

### Task con documenti, rejected zero-verified

Sequenza identica fino a `task.final_gate_completed`, poi `task.publication_held` (evento audit-only, lo `status` resta `analyzed_partial`).

### Task senza documenti

`task.created` (API) → `task.analyzing` → `task.blocked`.

---

## Semantica lifecycle e source loss (Fase 8.5)

**Withdrawal è asincrona.** L'API pubblica un evento `published_answer.withdrawal_requested` su Redis e ritorna `202`. Il consumer worker `published_answer_withdrawal` legge l'evento e delega ad `apply_withdrawal`, che è l'unica entità che muta `published_answers.status` da `published` a `withdrawn`. L'API non scrive `published_answer_lifecycle_events`; lo fa il servizio dentro la stessa transazione del consumer.

**Source loss è asincrona ma con un INSERT immediato lato API.** L'endpoint `POST /api/v1/source-loss-events` inserisce la riga `source_loss_events` (append-only) e pubblica l'evento `source_loss.detected` nella stessa transazione DB. Il consumer worker `source_loss` legge l'evento, delega al propagator, e questo registra le righe in `source_loss_propagation_records` e gli effetti sul Claim Ledger.

**Source loss NON ritira automaticamente published_answers.** È una scelta esplicita di cascade soft: la perdita di una fonte è registrata e propagata al ledger; la lista delle published_answers impattate viene tracciata in `source_loss_propagation_records` con `propagation_kind='published_answer_impacted'`, ma il loro `status` non viene cambiato. La withdrawal resta operazione separata.

**`task_masters.status` non viene esteso e non viene usato per withdrawal/source loss.** Il lifecycle vive su `published_answers.status` e nello storico append-only `published_answer_lifecycle_events`. La source loss propagation usa `claim_ledger_entries` append-only e `source_loss_propagation_records`. Né withdrawal né source loss aggiungono status nuovi a `task_masters`.

**Audit chain resta verificabile.** Tutti i nuovi audit event (`published_answer.withdrawn`, `source_loss.propagated_to_claim`, `source_loss.propagated_to_published_answer`) sono emessi su `chain_scope='task'` via `audit_append`, e `verify_task_audit_chain` continua a ritornare `ok=True` dopo le transizioni 8.5 (proprietà verificata nei realistic flow tests e nei test unitari di consumer/service).

**Idempotenza.** Stratificata su due livelli:

- **Consumer-level**: `event_processing_records` UNIQUE `(consumer_name, idempotency_key)`. I due nuovi consumer usano `consumer_name` stabile (`published_answer_withdrawal` e `source_loss`), mai la consumer name per-istanza del worker.
- **Domain-level**: vincoli UNIQUE su `published_answer_lifecycle_events`, `source_loss_events`, e partial unique indexes su `source_loss_propagation_records`. `published_answers` ha UPDATE status-guarded `WHERE status='published'`. `claim_ledger_entries` è append-only stretto, idempotente via `cle_logical_version_uq` con lock `FOR UPDATE` su `logical_claims`.

---

## Final Answer Gate — regola di verifica (8.4, invariata)

Uno span è **verified-backed** se e solo se esiste almeno un `final_answer_span_claim_links` tale che:

````
link.claim_ledger_entry_id == latest_entry_id_for(claim_logical_id)
AND latest_entry_state_for(claim_logical_id) == 'verified_fact'
````

Branch decisionali (8.4):

| Condizione del draft | `decision` | `reason_code` | Coverage gap | `published_answers` |
|---|---|---|---|---|
| Zero spans | `rejected` | `no_verified_claims` | `kind='missing_evidence'`, `gap_key='no_verified_claims'` | assente |
| Tutti gli spans verified-backed | `approved` | `all_spans_verified` | nessuno | v1 con `status='published'` |
| Almeno uno span non verified-backed | `rejected` | `unverified_spans_present` | un gap per ogni span scoperto | assente |

### Convenzione errori (invariata)

`ErrorCode.NOT_PUBLISHED` non esiste in MVP-0. Per le GET su un task esistente non ancora pubblicato si restituisce `RESOURCE_NOT_FOUND` con `details.resource='published_answers'`. Per task inesistente: `details.resource='task_masters'`. Per draft/gate non ancora prodotti: `details.resource='draft_final_answers'` o `'final_gate_reports'`. In 8.5 si aggiunge `details.resource='evidence_spans'` per il `404` del POST `source-loss-events` e `details.resource='source_loss_events'` per il `409` di conflitto idempotency.

---

## Cosa è ancora rinviato (non implementato)

- **Provider AI reali** (Claude, ChatGPT, Gemini o equivalenti). MVP-0 gira con `PROVIDERS_ENABLED=mock` e `MAX_COST_PER_TASK=0`.
- **Web retrieval, Verified Web Mode, Hybrid Mode.**
- **Consensus engine**, **contradiction detector** avanzato, **source quality evaluator** avanzato, **critical reviewer**.
- **Renderer ed export** Markdown/HTML/PDF/DOCX/JSON-LD verso filesystem o storage cloud.
- **Auth e RBAC reali.** In MVP-0 non c'è autenticazione produttiva.
- **Retention reale distruttiva.** `0007_evaluation_retention.sql` non esiste ancora. Niente cleanup blob orfani, niente retention pass.
- **UI completa.** Esiste solo un'app web minimale già nota da 8.1.
- **OCR / parsing PDF, vector store cloud, storage S3 / GCS / Azure operativo.**

---

## Vincoli sempre validi (MVP-0)

- Nessun provider AI reale, nessun riferimento operativo a OpenAI, Anthropic, Google o altri provider esterni nel codice di MVP-0.
- `PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0`.
- Closed Corpus only.
- SQLAlchemy 2.0 Core: `Connection`, non `Engine.execute`.
- Migration applicate (0001–0006) sono immutabili. Modifiche schema solo via nuove migration.
- Test rerun-safe con UUID/hash/marker unici per invocazione.
- Append-only enforced a DB su `audit_records`, `evidence_spans`, `claim_ledger_entries`, `final_answer_spans`, `final_gate_reports`, `published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records`.

---

## Prossimo passo

Le possibili direzioni naturali, da decidere con prompt operativo separato, includono:
- read API per i nuovi domini lifecycle e source loss (lifecycle-events, source-loss-events, propagation, impact);
- `0007_evaluation_retention.sql` per retention reale, una volta deciso il perimetro;
- estensione dei test realistici a uno smoke test che usi Redis reale + worker loop.

Nessuna di queste è scritta nel repo al commit corrente.
