# PROJECT_STATE — Evidence-First MVP-0

Documento di onboarding tecnico, una pagina, leggibile dal collaboratore al primo accesso senza dover leggere il codice. Riflette lo stato del repo al commit **Fase 8.6 minima**: `7ee687b4d47d81b736c0bc0587acaa5c12bc3a24` ("Add realistic phase 8.6 read flow test").

---

## Cosa è il progetto

Piattaforma multi-AI **evidence-first** ed **evidence-gated**. Nessun claim fattuale può finire nella risposta finale se non è collegato a evidenze tracciabili, registrate nel Claim Ledger, verificate e approvate dal Final Answer Gate. La verità non è ciò che dice un modello AI: è ciò che le evidenze recuperate, archiviate, tracciate e verificate dal sistema supportano.

In MVP-0 il nucleo evidence-gated è costruito **prima** della visione multi-AI. Provider AI reali, Verified Web Mode, Hybrid Mode, consensus engine, contradiction detector avanzato, source quality evaluator e critical reviewer sono fasi future. Il claim "evidence-gated" qui significa: esiste una base append-only verificabile end-to-end per draft/gate/published, più una propagazione lifecycle e source-loss minimale per MVP-0, e una superficie di osservabilità HTTP read-only sopra di essa. Non è una soluzione completa al problema delle allucinazioni.

---

## Stato migration

| Migration | Stato |
|---|---|
| `0001_foundation.sql` | applicata, immutabile |
| `0002_storage.sql` | applicata, immutabile |
| `0003_documents.sql` | applicata, immutabile |
| `0004_claim_ledger.sql` | applicata, immutabile |
| `0005_answers_gate.sql` | applicata, immutabile |
| `0006_lifecycle.sql` | applicata (Fase 8.5), immutabile |
| `0007_evaluation_retention.sql` | da scrivere |

---

## Cosa esiste oggi (Fasi 8.4 + 8.5 + 8.6 minima)

### Base 8.4 (invariata)

- **DB foundation multi-tenant**: `tenants`, `users`, `projects`, `sessions`, `task_masters`, `event_processing_records`, `policy_versions`.
- **Audit chain hash-linked, append-only, verificabile end-to-end** via `verify_audit_chain` / `verify_task_audit_chain`. Append-only enforced a DB tramite trigger comune `reject_modify_append_only`.
- **Storage layer content-addressed, deduplicato, refcount-based**: `storage_blobs`, `storage_objects`. Dedup global concorrenza-safe via `INSERT ... ON CONFLICT DO NOTHING` sull'indice parziale `sb_global_uq`.
- **Document store** con upload reale `.txt`/`.md`, chunking deterministico, `evidence_spans` minimali, `task_documents`. `evidence_spans` append-only.
- **Claim Ledger append-only stretto**: `logical_claims`, `raw_claims`, `classified_claims`, `claim_ledger_entries`, `claim_lineage`, `claim_evidence_links`, `verification_records`. Supersede esclusivamente via `claim_lineage.relation_kind='supersedes'`.
- **Extractor mock-driven**, **CVE-lite mock-driven**, **Compiler mock-driven**, **Final Answer Gate mock-driven**.
- **Worker single-consumer 8.4** per `task.created`, FK-safe, resume-safe, idempotente.
- **Coerenza referenziale stretta a DB** tra `task_masters` ↔ `draft_final_answers` ↔ `final_gate_reports` ↔ `published_answers` via UNIQUE composite e FK composite.

### Fase 8.5 (invariata rispetto al commit `03c4186`)

**Schema (migration `0006_lifecycle.sql`).** Tre tabelle append-only:

- `published_answer_lifecycle_events`: FK composita `(published_answer_id, task_id) → published_answers(id, task_id)`, `event_type ∈ {published, withdrawal_requested, withdrawn, superseded}`, UNIQUE `(published_answer_id, event_type, idempotency_key)`.
- `source_loss_events`: FK `evidence_span_id → evidence_spans(id)` come granularità canonica, `loss_kind ∈ {source_deleted, source_access_lost, quote_mismatch, document_replaced, policy_retraction}`, UNIQUE `(evidence_span_id, loss_kind, idempotency_key)`.
- `source_loss_propagation_records`: `propagation_kind ∈ {claim_marked_unverifiable, published_answer_impacted, no_claims_impacted, no_active_published_answers_impacted}`, `status ∈ {recorded, skipped, failed}`, idempotenza via partial unique indexes ristretti a `status IN ('recorded','skipped')`.

**Servizi worker.**

- `published_answer_lifecycle.apply_withdrawal`: unico scrittore autorizzato dei campi lifecycle di `published_answers` per il path di withdrawal. Lock `FOR UPDATE`, INSERT idempotenti dei lifecycle event, UPDATE status-guarded `WHERE status='published'`, audit `published_answer.withdrawn` solo se l'UPDATE muta una riga.
- `source_loss_propagator.propagate_source_loss`: risolve l'impact set da `evidence_span_id`, append `v(N+1)` `unverifiable / unsupported / source_lost` con lineage `supersedes`, registra `source_loss_propagation_records` con audit `source_loss.propagated_to_claim` e `source_loss.propagated_to_published_answer`, gestisce `no_claims_impacted` e `no_active_published_answers_impacted`.

**Consumer e dispatcher.**

- `consumers/published_answer_withdrawal.py`: consumer_name stabile, EPR consumer-level, branch FK-safe.
- `consumers/source_loss.py`: consumer_name stabile, scope risolto via LEFT JOIN con `task_masters` (task_id può restare NULL), EPR consumer-level.
- `consumers/dispatch.py`: routing per `task.created`/`task_created`, `published_answer.withdrawal_requested`, `source_loss.detected`; `redis_consumer_name` NON forwardato ai due nuovi consumer (la UNIQUE EPR resta globale).
- `worker/app/main.py`: `xreadgroup` multi-stream su tre stream con gruppo `worker_default`, ACK sullo stream concreto, `_ACK_STATUSES = {processed, skipped_already_succeeded, skipped_in_flight, skipped_terminal}`, `failed` lascia pending.

**API producer 8.5.**

- `POST /api/v1/published-answers/{published_answer_id}/withdrawal-requests`: read-only DB lato API, XADD su `app.events.published_answer_withdrawal_requested`, ritorna 202 con envelope.
- `POST /api/v1/source-loss-events`: INSERT in `source_loss_events` + XADD su `app.events.source_loss_detected` nella stessa transazione (rollback DB se XADD fallisce), 404/409/500 normalizzati.

### Fase 8.6 minima (nuovo rispetto a 8.5)

Tutto quanto segue è nel repo al commit `7ee687b4d47d81b736c0bc0587acaa5c12bc3a24` ed è verificabile leggendo i file indicati. Endpoint API read-only di osservabilità sui domini lifecycle e source-loss introdotti in 8.5.

**Quattro endpoint GET read-only.**

- `GET /api/v1/published-answers/{published_answer_id}/lifecycle-events` — `apps/api/app/routes/lifecycle_events.py`. Lista eventi lifecycle ordinati ASC per (created_at, id), filtro opzionale per `event_type`, `limit` 1–2000 default 200. 404 con `details.resource="published_answers"`. Commit `e2b5472`.
- `GET /api/v1/source-loss-events/{source_loss_event_id}` — `apps/api/app/routes/source_loss.py`. Single-row read tramite `SourceLossEventRead`. 404 con `details.resource="source_loss_events"`. Surface `task_id=null` verbatim. Commit `dedf0ac`.
- `GET /api/v1/source-loss-events/{source_loss_event_id}/propagation` — `apps/api/app/routes/source_loss.py`. Lista propagation records ordinati ASC per (created_at, id), filtri opzionali `propagation_kind` e `status`, `limit` 1–5000 default 500. Non collassa `failed`. 404 con `details.resource="source_loss_events"`. Commit `2da610c`.
- `GET /api/v1/tasks/{task_id}/source-loss-events` — `apps/api/app/routes/task_source_loss.py`. Lista task-centric tramite union S1 ∪ S2: S1 = `source_loss_events.task_id = :task_id`, S2 = `source_loss_events.evidence_span_id` collegato a `logical_claims.task_id` via `claim_evidence_links`. Distinct per `source_loss_events.id` con precedence `task_scope` > `claim_evidence_link`. `task_id` sulla SLE resta esposto verbatim (NULL non viene camuffato). 404 con `details.resource="task_masters"`. Commit `cd26cb4`.

**Wrapper di risposta.** Locali ai route module (non aggiunti a `packages/shared/evidencefirst_shared/schemas.py`):

- lifecycle list wrapper `{published_answer_id, items}`;
- propagation list wrapper `{source_loss_event_id, items}`;
- task source-loss item con `impacted_via ∈ {task_scope, claim_evidence_link}`.

Gli item interni riutilizzano gli schemi shared già esistenti dal 8.5 (`PublishedAnswerLifecycleEventRead`, `SourceLossEventRead`, `SourceLossPropagationRecordRead`).

**Test API dedicati.**

- `apps/api/tests/test_published_answer_lifecycle_events_endpoint.py` — 7 scenari.
- `apps/api/tests/test_source_loss_events_read_endpoint.py` — 5 scenari.
- `apps/api/tests/test_source_loss_propagation_endpoint.py` — 9 scenari.
- `apps/api/tests/test_task_source_loss_events_endpoint.py` — 8 scenari.

Ogni file include un test di read-only invariant con snapshot pre/post sui count delle tabelle `published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records`, `published_answers`, `claim_ledger_entries`, `claim_lineage`, `audit_records`.

**Realistic read flow test (root tests/).**

- `tests/test_phase_8_6_read_flow.py` — due scenari cross-component:
  - withdrawal: API producer 8.5 → FakeRedis (installato sul route module) → `dispatch.handle_event(event)` → withdrawal consumer → lifecycle service → DB; poi GET 8.6A + GET single published_answer (status='withdrawn') + verify_task_audit_chain ok=True;
  - source-loss: API producer 8.5 → FakeRedis → `dispatch.handle_event(event)` → source_loss consumer → propagator → DB; poi GET 8.6B + GET 8.6C + GET 8.6D + verify_task_audit_chain ok=True + head del claim a `unverifiable / unsupported / source_lost`.

Il file carica il package worker via `importlib.util` sotto alias `_wapp` per evitare la collisione di nome con il package `app` dell'API, stessa convenzione dei realistic flow 8.5.

Risultati riportati al commit `7ee687b`:

- `tests/test_phase_8_6_read_flow.py` → 2 passed;
- `tests/` root → 70 passed.

Non si dichiara qui che `make test`, `make test-api`, `make test-worker`, `make test-shared` o `make test-db` siano stati eseguiti come gate complessivo dopo la 8.6E: vanno eseguiti separatamente.

---

## Endpoint API attivi (tabella aggiornata)

| Endpoint | Descrizione | Fase |
|---|---|---|
| `POST /api/v1/projects` | Crea progetto | 8.1+ |
| `GET /api/v1/projects` | Lista progetti | 8.1+ |
| `GET /api/v1/projects/{id}` | Dettaglio progetto | 8.1+ |
| `POST /api/v1/projects/{id}/documents` | Upload documento `.txt`/`.md` | 8.2 |
| `GET /api/v1/projects/{id}/documents` | Lista documenti progetto | 8.2 |
| `GET /api/v1/documents/{id}` | Dettaglio documento | 8.2 |
| `GET /api/v1/documents/{id}/chunks` | Chunks del documento | 8.2 |
| `POST /api/v1/tasks` | Crea task closed_corpus | 8.1+ |
| `GET /api/v1/tasks/{id}` | Dettaglio task | 8.1+ |
| `GET /api/v1/tasks/{id}/documents` | Documenti collegati al task | 8.2 |
| `GET /api/v1/tasks/{id}/audit` | Catena audit del task | 8.1+ |
| `GET /api/v1/tasks/{id}/raw-claims` | Raw claims del task | 8.3 |
| `GET /api/v1/tasks/{id}/classified-claims` | Classified claims del task | 8.3 |
| `GET /api/v1/tasks/{id}/claims` | Latest ledger entry per logical claim | 8.3 |
| `GET /api/v1/claims/{logical_id}/history` | Storia di un logical claim | 8.3 |
| `GET /api/v1/claims/{logical_id}/evidence` | Aggregato latest + links + verifications | 8.3 |
| `GET /api/v1/tasks/{id}/draft` | Draft v1 + spans | 8.4 |
| `GET /api/v1/tasks/{id}/final-gate-report` | Gate report + coverage gaps | 8.4 |
| `GET /api/v1/tasks/{id}/published-answer` | Published answer per task | 8.4 |
| `GET /api/v1/published-answers/{id}` | Published answer per id | 8.4 |
| `POST /api/v1/published-answers/{published_answer_id}/withdrawal-requests` | Producer asincrono di withdrawal | 8.5 |
| `POST /api/v1/source-loss-events` | Producer asincrono di source loss | 8.5 |
| `GET /api/v1/published-answers/{published_answer_id}/lifecycle-events` | Read lifecycle events di un published_answer | 8.6A |
| `GET /api/v1/source-loss-events/{source_loss_event_id}` | Read single source_loss_event | 8.6B |
| `GET /api/v1/source-loss-events/{source_loss_event_id}/propagation` | Read propagation records di un source_loss_event | 8.6C |
| `GET /api/v1/tasks/{task_id}/source-loss-events` | Read task-level source-loss listing (S1 ∪ S2) | 8.6D |
| `GET /health/live` / `/health/db` / `/health/queue` / `/health/storage` / `/health/ready` | Health checks | 8.1+ |

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

**Withdrawal è asincrona.** L'API pubblica un evento `published_answer.withdrawal_requested` su Redis e ritorna 202. Il consumer worker `published_answer_withdrawal` delega ad `apply_withdrawal`, unica entità che muta `published_answers.status` da `published` a `withdrawn`. L'API non scrive `published_answer_lifecycle_events`; lo fa il servizio nella stessa transazione del consumer.

**Source loss è asincrona ma con INSERT immediato lato API.** `POST /api/v1/source-loss-events` inserisce la riga `source_loss_events` e pubblica `source_loss.detected` nella stessa transazione DB. Il consumer worker `source_loss` delega al propagator, che registra `source_loss_propagation_records` e gli effetti sul Claim Ledger.

**Source loss NON ritira automaticamente published_answers.** Scelta esplicita di cascade soft: la lista delle PA impattate è tracciata in `source_loss_propagation_records` con `propagation_kind='published_answer_impacted'`, ma `status` della PA non viene cambiato. La withdrawal resta operazione separata.

**`task_masters.status` non viene esteso e non viene usato per withdrawal/source loss.** Il lifecycle vive su `published_answers.status` e nello storico append-only `published_answer_lifecycle_events`. La source loss propagation usa `claim_ledger_entries` append-only e `source_loss_propagation_records`.

**Audit chain resta verificabile.** `published_answer.withdrawn`, `source_loss.propagated_to_claim`, `source_loss.propagated_to_published_answer` sono emessi su `chain_scope='task'` via `audit_append`. `verify_task_audit_chain` ritorna `ok=True` dopo le transizioni 8.5; questa proprietà è verificata anche dai realistic flow 8.6 (sia per il withdrawal che per il source-loss).

**Idempotenza** stratificata su due livelli:

- **Consumer-level**: `event_processing_records` UNIQUE `(consumer_name, idempotency_key)`. I due nuovi consumer usano `consumer_name` stabile (`published_answer_withdrawal`, `source_loss`), mai la consumer name per-istanza del worker.
- **Domain-level**: UNIQUE su `published_answer_lifecycle_events`, `source_loss_events`, e partial unique indexes su `source_loss_propagation_records`. `published_answers` ha UPDATE status-guarded `WHERE status='published'`. `claim_ledger_entries` append-only stretto via `cle_logical_version_uq` con lock `FOR UPDATE` su `logical_claims`.

---

## Semantica read API (Fase 8.6 minima)

**Tutti gli endpoint 8.6 sono read-only end-to-end.**

- nessun INSERT/UPDATE/DELETE in alcuna tabella;
- nessuna chiamata a `apply_withdrawal` o `propagate_source_loss`;
- nessun uso di Redis;
- nessun import di codice worker dai route module;
- nessuna mutazione di `published_answers`, `task_masters.status`, `claim_ledger_entries`, `audit_records`, `published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records`.

L'invariante è verificato programmaticamente: ogni file di test API include uno scenario che fa snapshot dei COUNT(*) prima e dopo le GET su un set whitelisted di tabelle 8.4/8.5/audit e fallisce su drift.

**Lista vuota vs 404.**

- 8.6A: published_answer esistente senza lifecycle events → 200 `items=[]`. Nessun backfill di `published`.
- 8.6C: source_loss_event esistente senza propagation rows → 200 `items=[]`. Race window producer → propagator coperta da questo contratto.
- 8.6D: task esistente senza source-loss events visibili → 200 `items=[]`.

**JSONB esposti verbatim.** `event_payload` e `details` sono ritornati senza redaction. RBAC non è implementato in MVP-0; questo è il debito chiaramente registrato sui rischi residui.

**`source_loss_events.task_id` può restare NULL.** Il producer 8.5 lo lascia NULL by design (uno span può supportare claim di task diversi). 8.6B espone `task_id=null` verbatim. 8.6D risolve la vista task-centric via S1 ∪ S2 con campo `impacted_via`, ma NON camuffa il valore NULL sulla SLE stessa: il client riceve `source_loss_event.task_id=null` con `impacted_via="claim_evidence_link"`.

---

## Final Answer Gate — regola di verifica (8.4, invariata)

Uno span è **verified-backed** se e solo se esiste almeno un `final_answer_span_claim_links` tale che:

```
link.claim_ledger_entry_id == latest_entry_id_for(claim_logical_id)
AND latest_entry_state_for(claim_logical_id) == 'verified_fact'
```

Branch decisionali:

| Condizione del draft | `decision` | `reason_code` | Coverage gap | `published_answers` |
|---|---|---|---|---|
| Zero spans | `rejected` | `no_verified_claims` | `kind='missing_evidence'`, `gap_key='no_verified_claims'` | assente |
| Tutti gli spans verified-backed | `approved` | `all_spans_verified` | nessuno | v1 con `status='published'` |
| Almeno uno span non verified-backed | `rejected` | `unverified_spans_present` | un gap per ogni span scoperto | assente |

### Convenzione errori

`ErrorCode.NOT_PUBLISHED` non esiste in MVP-0. Per le GET su task esistente non ancora pubblicato si restituisce `RESOURCE_NOT_FOUND` con `details.resource='published_answers'`. Per task inesistente: `details.resource='task_masters'`. Per draft/gate non ancora prodotti: `details.resource='draft_final_answers'` o `'final_gate_reports'`. In 8.5/8.6 si usa `details.resource='evidence_spans'` per il 404 del POST `source-loss-events`, `details.resource='source_loss_events'` per il 409 di conflitto idempotency e per i 404 di 8.6B/8.6C, `details.resource='published_answers'` per 8.6A, `details.resource='task_masters'` per 8.6D.

---

## Cosa è ancora rinviato (non implementato)

- **Provider AI reali** (Claude, ChatGPT, Gemini o equivalenti). MVP-0 gira con `PROVIDERS_ENABLED=mock` e `MAX_COST_PER_TASK=0`.
- **Web retrieval, Verified Web Mode, Hybrid Mode.**
- **Consensus engine**, **contradiction detector** avanzato, **critical reviewer**.
- **Source Quality Evaluator** — debito strategico futuro. La 8.6 espone eventi e propagazioni, ma non valuta autorevolezza, indipendenza, primaryness, freschezza o coerenza delle fonti. Resta il debito più rilevante sul piano evidence-quality.
- **Renderer ed export** Markdown/HTML/PDF/DOCX/JSON-LD.
- **Auth e RBAC reali.** Gli endpoint read 8.6 espongono JSONB verbatim senza autorizzazione.
- **Retention reale distruttiva.** `0007_evaluation_retention.sql` non esiste ancora. Le tabelle 8.5 crescono senza pruning.
- **DLQ esplicita per il worker.** Le entry il cui handler ritorna `failed` restano pending nel PEL; nessun destination stream per dead-lettering.
- **UI completa.** Esiste solo un'app web minimale già nota da 8.1.
- **OCR / parsing PDF, vector store cloud, storage S3 / GCS / Azure operativo.**
- **Cursor pagination** sugli endpoint read 8.6 (solo `limit` con tetto).
- **Stretch 8.6** `GET /api/v1/published-answers/{id}/source-loss-impact` — opzionale, non implementato.
- **Backfill `published` lifecycle events** per pubblicazioni create in 8.4: nessuno script di backfill è eseguito. 8.6A ritorna `items=[]` per quei published_answer, coerente con lo stato DB.
- **Worker main loop reale negli end-to-end test.** I realistic flow 8.5 e 8.6 usano FakeRedis e invocano `dispatch.handle_event` direttamente; `XREADGROUP`/`XACK`/PEL/signal handlers non vengono attraversati. Copertura `XREADGROUP`/`XACK` resta su `apps/worker/tests/test_main_multistream.py` con FakeRedis interno.

---

## Vincoli sempre validi (MVP-0)

- Nessun provider AI reale, nessun riferimento operativo a OpenAI, Anthropic, Google o altri provider esterni nel codice di MVP-0.
- `PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0`.
- Closed Corpus only.
- SQLAlchemy 2.0 Core: `Connection`, non `Engine.execute`.
- Migration applicate (0001–0006) sono immutabili. Modifiche schema solo via nuove migration.
- Test rerun-safe con UUID/hash/marker unici per invocazione.
- Append-only enforced a DB su `audit_records`, `evidence_spans`, `claim_ledger_entries`, `final_answer_spans`, `final_gate_reports`, `published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records`.
- Endpoint API 8.6 read-only verificato da snapshot pre/post sui count delle tabelle 8.4/8.5/audit.

---

## Prossimo passo

Le possibili direzioni naturali, da decidere con prompt operativo separato, includono:

- **Source Quality Evaluator** come fase dedicata (es. 8.7 o 9.0). È oggi il debito strategico più rilevante per avvicinarsi alla promessa anti-allucinazione completa. La 8.6 ha reso osservabili eventi e propagazioni, ma il sistema non distingue ancora tra fonti forti, deboli, primarie, secondarie, indipendenti, fresche o contraddette.
- **`0007_evaluation_retention.sql`** per retention reale, una volta deciso il perimetro.
- **Stretch 8.6**: `GET /api/v1/published-answers/{id}/source-loss-impact`, se decisamente utile in operativo.
- **Cursor pagination** sugli endpoint read 8.6.
- **RBAC e redaction** dei JSONB esposti dagli endpoint read.
- **Smoke test end-to-end con Redis reale** e worker main loop reale (XREADGROUP/XACK/PEL effettivi).

Nessuna di queste è scritta nel repo al commit corrente.
