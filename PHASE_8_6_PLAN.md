# PHASE_8_6_PLAN — Evidence-First MVP-0

Documento di piano per la **Fase 8.6** dell'Evidence-First MVP-0. Definisce ambito, endpoint, schemi, errori, test, rollout e rischi residui per l'introduzione di endpoint API read-only di osservabilità sui domini lifecycle e source-loss introdotti in 8.5.

Questo file è esclusivamente di piano: non implica scrittura di codice, modifica di test, di migration, o di altri documenti di repo finché non vengono aperti i singoli blocchi.

---

## 1. Stato di partenza

La **Fase 8.5 è completata tecnicamente e documentata**. Riassunto verificato dai file presenti in repo:

- **Schema lifecycle e source-loss in `migrations/0006_lifecycle.sql`** (immutabile da qui in avanti):
  - `published_answer_lifecycle_events`, append-only via `reject_modify_append_only`, FK composita `(published_answer_id, task_id) → published_answers(id, task_id)`, UNIQUE `pale_idempotency_uq (published_answer_id, event_type, idempotency_key)`, CHECK su `event_type ∈ {published, withdrawal_requested, withdrawn, superseded}`.
  - `source_loss_events`, append-only, granularità canonica `evidence_span_id`, document_* come reporting context, CHECK su `loss_kind ∈ {source_deleted, source_access_lost, quote_mismatch, document_replaced, policy_retraction}`, UNIQUE `sle_idempotency_uq (evidence_span_id, loss_kind, idempotency_key)`.
  - `source_loss_propagation_records`, append-only, CHECK su `propagation_kind ∈ {claim_marked_unverifiable, published_answer_impacted, no_claims_impacted, no_active_published_answers_impacted}`, CHECK su `status ∈ {recorded, skipped, failed}`, quattro partial UNIQUE (uno per `propagation_kind`, ristretti a `status IN ('recorded','skipped')`).

- **Servizi worker** (`apps/worker/app/services/`):
  - `published_answer_lifecycle.apply_withdrawal` — unico scrittore autorizzato dei campi lifecycle di `published_answers` per il path di withdrawal. `SELECT ... FOR UPDATE OF pa`, INSERT idempotenti dei due eventi `withdrawal_requested`/`withdrawn`, UPDATE status-guarded `WHERE status='published'`, audit `published_answer.withdrawn` emesso solo quando l'UPDATE muta una riga. Outcomes: `withdrawn`, `already_withdrawn`, `already_superseded`, `unsupported_status`, `not_found`.
  - `source_loss_propagator.propagate_source_loss` — risolve l'impact set da `evidence_span_id`, per ogni claim impattato acquisisce `FOR UPDATE` su `logical_claims`, appende v(N+1) `unverifiable / unsupported / source_lost` quando la head è `verified_fact`, inserisce lineage `supersedes` con ON CONFLICT DO NOTHING, registra propagation records idempotenti via partial UNIQUE, emette audit `source_loss.propagated_to_claim` e `source_loss.propagated_to_published_answer` solo quando la propagation row viene effettivamente inserita su questa chiamata. Outcomes: `propagated`, `no_claims_impacted`, `not_found`. Non muta `published_answers.status`.

- **Consumer worker** (`apps/worker/app/consumers/`):
  - `published_answer_withdrawal`, `consumer_name` stabile `"published_answer_withdrawal"`, EPR consumer-level via `event_processing_records`, FK-safe (`WORKER_PUBLISHED_ANSWER_NOT_VISIBLE`), rifiuto pre-transaction per campi obbligatori malformati e per `requested_by` malformato.
  - `source_loss`, `consumer_name` stabile `"source_loss"`, scope risolto da `source_loss_events LEFT JOIN task_masters` con `COALESCE`, `task_id` può legittimamente restare NULL, FK-safe (`WORKER_SOURCE_LOSS_EVENT_NOT_VISIBLE`).
  - **Dispatcher** `dispatch.handle_event(event, *, redis_consumer_name=None)`: instrada per `event_type`. Per `task.created`/alias forwarda `redis_consumer_name`. Per `published_answer.withdrawal_requested` e `source_loss.detected` **non** forwarda `redis_consumer_name`: la UNIQUE EPR `(consumer_name, idempotency_key)` deve restare globale tra worker instances.

- **Worker multi-stream loop** (`apps/worker/app/main.py`):
  - `xreadgroup` su tre stream con consumer group condiviso `worker_default`:
    - `app.events.task_created`
    - `app.events.published_answer_withdrawal_requested`
    - `app.events.source_loss_detected`
  - ACK sullo stream concreto restituito dalla risposta. ACK solo per statuses `{processed, skipped_already_succeeded, skipped_in_flight, skipped_terminal}`; `failed` lascia pending.

- **API producer 8.5** (`apps/api/app/routes/`):
  - `POST /api/v1/published-answers/{published_answer_id}/withdrawal-requests` in `answers.py`. Read-only DB lato API, XADD su `app.events.published_answer_withdrawal_requested`. `event_payload` opzionale serializzato JSON compatto sort_keys sotto la chiave Redis `event_payload_json`. Errori: 404 `RESOURCE_NOT_FOUND` con `details.resource="published_answers"`, 500 `INTERNAL_ERROR` su fallimento XADD.
  - `POST /api/v1/source-loss-events` in `source_loss.py`. Risolve scope canonico dalla catena `evidence_spans → document_chunks → document_versions → uploaded_documents`. `task_id` NULL by design. INSERT in `source_loss_events` + XADD su `app.events.source_loss_detected` nella stessa transazione (rollback su XADD failure). Errori: 404 `RESOURCE_NOT_FOUND` con `details.resource="evidence_spans"`, 409 `RESOURCE_CONFLICT` su collisione UNIQUE con `details.resource="source_loss_events"`, 500 `INTERNAL_ERROR` su fallimento XADD.

- **Test 8.5** (presenti e funzionanti):
  - Unitari: `apps/api/tests/test_published_answer_withdrawal_request.py`, `apps/api/tests/test_source_loss_endpoint.py`, `apps/worker/tests/test_published_answer_withdrawal_consumer.py`, `apps/worker/tests/test_source_loss_consumer.py`, `apps/worker/tests/test_source_loss_propagator_service.py`, `apps/worker/tests/test_dispatch.py`, `apps/worker/tests/test_main_multistream.py`.
  - Realistic flow cross-component: `tests/test_phase_8_5_withdrawal_flow.py`, `tests/test_phase_8_5_source_loss_flow.py`. Entrambi usano FakeRedis e dispatcher diretto: **non** attraversano `XREADGROUP`/`XACK`/PEL/signal handlers né un Redis reale.

- **Schemi shared** (`packages/shared/evidencefirst_shared/schemas.py`):
  - `PublishedAnswerLifecycleEventRead`, `SourceLossEventRead`, `SourceLossPropagationRecordRead` sono già definiti. **Non sono ancora usati da nessun endpoint**: 8.6 li adotterà invariati.

**Disambiguazione dei commit citati nel prompt:**

- Commit **tecnico** di completamento 8.5: `03c418693f4eb8ba7019c9785d149dbad83b87fe` ("Add realistic source loss flow test"). È l'ultimo commit che ha introdotto codice/test 8.5.
- Commit **documentale** corrente: `5f7f4ce7646affed29dfcdcc3943f0669a7f7e5e` ("Update project state after phase 8.5"). È l'aggiornamento di `PROJECT_STATE.md`/`PHASE_8_5_PLAN.md` che riflette lo stato post-8.5.

Non è un conflitto: sono due commit con ruoli distinti. Il codice 8.5 è stabilizzato a `03c418…`; la documentazione di stato è stabilizzata a `5f7f4ce…`. HEAD remoto atteso per l'inizio della Fase 8.6 è `5f7f4ce…`.

**Cosa manca davvero e motiva 8.6:** non esistono endpoint read-only sui tre nuovi domini. Lo smoke test 8.5 documentato in `PHASE_8_5_PLAN.md` §10 oggi richiede `psql` per osservare gli effetti asincroni della pipeline.

---

## 2. Obiettivo 8.6

Aggiungere **endpoint API read-only** che espongano lo stato scritto dalla pipeline 8.5, riusando gli schemi shared già definiti e mantenendo zero side-effect sul DB.

Domini esposti:

- **lifecycle events** di un `published_answer`;
- **source_loss_events** singolo, per id;
- **source_loss_propagation_records** di un source_loss_event;
- **source_loss_events** visibili da un task (unione `task_scope` + `claim_evidence_link`);
- **opzionale (stretch):** impact set di source_loss su un singolo `published_answer`.

**Nessuna mutazione DB da nessuno dei nuovi endpoint.** Nessun INSERT, nessun UPDATE, nessun DELETE su nessuna tabella. Gli endpoint sono pure read API.

Risultato atteso: lo smoke test 8.5 diventa eseguibile interamente via HTTP, senza ricorrere a `psql`. Operatori e test di livello superiore possono polling-are gli effetti del worker direttamente.

---

## 3. Non-obiettivi

Esplicitamente fuori scope per la Fase 8.6:

- **Nessuna nuova migration.** `0007_evaluation_retention.sql` resta rinviato a una fase futura.
- **Nessun nuovo worker consumer.** Nessun nuovo handler di eventi.
- **Nessun nuovo stream Redis.** Gli stream esistenti (`app.events.task_created`, `app.events.published_answer_withdrawal_requested`, `app.events.source_loss_detected`) restano invariati.
- **Nessun provider AI reale.** `PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0` restano in vigore.
- **Nessuna web search**, nessun Verified Web Mode, nessun Hybrid Mode.
- **Nessun frontend completo.** Eventuali modifiche all'app web di MVP-0 sono fuori scope.
- **Nessuna auth/RBAC reale.** Da documentare esplicitamente: gli endpoint 8.6 sono privi di autorizzazione, coerentemente con le altre read API MVP-0.
- **Nessuna retention distruttiva**, nessun cleanup, nessun export job.
- **Nessun DLQ esplicito**, nessun pattern di dead-lettering Redis.
- **Nessuna modifica a `apps/worker/app/services/`, `apps/worker/app/consumers/`, `apps/worker/app/main.py`.** Il worker non viene toccato.
- **Nessuna estensione di `task_masters.status`.** Il CHECK constraint resta invariato.
- **Nessun withdrawal automatico da source_loss.** La perdita di una fonte continua a registrare l'impatto ma a **non** ritirare automaticamente i `published_answers`. La withdrawal resta operazione separata, asincrona, via `POST /api/v1/published-answers/{id}/withdrawal-requests`.
- **Nessuna modifica a `claim_lineage.relation_kind`, `verification_records.check_kind`, o a qualsiasi CHECK constraint esistente.**
- **Nessuna modifica ai test 8.5 esistenti**, né a `PROJECT_STATE.md` o `README.md` in questa fase di piano. L'aggiornamento di `PROJECT_STATE.md` arriverà al termine dell'implementazione 8.6.

---

## 4. Endpoint read-only proposti

Cinque endpoint complessivi, organizzati in cinque blocchi 8.6A–8.6E. Tutti read-only, tutti privi di side-effect, tutti riusano gli schemi shared esistenti.

### 4.1 8.6A — `GET /api/v1/published-answers/{published_answer_id}/lifecycle-events`

- **Perché serve.** Permette di osservare la storia lifecycle di un `published_answer` (eventi `withdrawal_requested`, `withdrawn`, e in futuro `superseded`/`published` se backfill-ati) senza interrogare manualmente il DB. È l'osservabilità minima del flusso di withdrawal asincrono: oggi, dopo il `202` del producer, il client può solo polling-are `GET /api/v1/published-answers/{id}` e vedere `status='withdrawn'` arrivare; non può vedere quando l'evento `withdrawal_requested` viene scritto né con quale `event_reason`.
- **Tabelle lette.** Solo `published_answer_lifecycle_events`, più un check di esistenza su `published_answers` per discriminare la 404.
- **Response shape indicativa.**
  ```json
  {
    "published_answer_id": "<uuid>",
    "items": [
      {
        "id": "<uuid>",
        "published_answer_id": "<uuid>",
        "task_id": "<uuid>",
        "event_type": "withdrawal_requested",
        "event_reason": "...",
        "event_payload": { ... },
        "requested_by": "<uuid|null>",
        "idempotency_key": "...",
        "created_at": "..."
      }
    ]
  }
  ```
  Ogni item rispetta `PublishedAnswerLifecycleEventRead`. Ordinamento ASC per `(created_at, id)` per replay-friendliness.
- **Filtri/paginazione minimi.** Query string:
  - `limit`: int, default 200, min 1, max 2000.
  - `event_type`: opzionale, Literal `{published, withdrawal_requested, withdrawn, superseded}`.
  Nessun cursor: dataset bounded per single published_answer.
- **Errori normalizzati.**
  - 404 `RESOURCE_NOT_FOUND` con `details.resource="published_answers"`, `details.id=<uuid>` se il published_answer non esiste.
  - 400 `VALIDATION_ERROR` se `event_type` non è nel Literal o `limit` fuori range (gestito da Pydantic + handler).
- **Test necessari.** In `apps/api/tests/test_published_answer_lifecycle_events_endpoint.py`:
  - happy path: 200 con due eventi seedati a mano via SQL nel test;
  - filtro `event_type=withdrawn` → singolo item;
  - 404 su published_answer inesistente;
  - lista vuota (200) quando il published_answer esiste ma nessun evento è stato registrato;
  - `limit=1` rispettato;
  - `event_type` invalido → 400 `VALIDATION_ERROR`;
  - read-only invariant: snapshot pre/post di counter su tabelle 8.5 + `published_answers` invariati.
- **Rischi.** Esposizione di `event_payload` JSONB senza RBAC. Coerente con MVP-0; da documentare.
- **Cosa NON deve fare.**
  - Non chiamare `apply_withdrawal`.
  - Non scrivere righe in `published_answer_lifecycle_events`.
  - Non inserire eventi mancanti per backfill (`published` su PA pre-8.5 resta assente).
  - Non interrogare `audit_records`.
  - Non eseguire `verify_task_audit_chain` lato endpoint.

### 4.2 8.6B — `GET /api/v1/source-loss-events/{source_loss_event_id}`

- **Perché serve.** Singola vista di un `source_loss_events` row. Indispensabile per chiudere il loop dopo `POST /api/v1/source-loss-events`: oggi il `202` ritorna solo l'id, e non c'è alcun GET corrispondente. Utile per debug operativo: verificare scope canonico risolto, `loss_kind`, `idempotency_key`, `event_payload`.
- **Tabelle lette.** Solo `source_loss_events`.
- **Response shape indicativa.** Singolo `SourceLossEventRead`, identica struttura allo schema shared:
  ```json
  {
    "id": "<uuid>",
    "tenant_id": "<uuid>",
    "project_id": "<uuid|null>",
    "task_id": "<uuid|null>",
    "evidence_span_id": "<uuid>",
    "document_chunk_id": "<uuid|null>",
    "document_version_id": "<uuid|null>",
    "document_id": "<uuid|null>",
    "loss_kind": "...",
    "loss_reason": "...",
    "detected_by": "...",
    "event_payload": { ... },
    "idempotency_key": "...",
    "created_at": "..."
  }
  ```
- **Filtri/paginazione minimi.** Nessuno: single-row endpoint.
- **Errori normalizzati.**
  - 404 `RESOURCE_NOT_FOUND` con `details.resource="source_loss_events"`, `details.id=<uuid>` se la SLE non esiste.
- **Test necessari.** In `apps/api/tests/test_source_loss_events_read_endpoint.py`:
  - happy path su SLE seedato (tenant, project, `task_id=NULL` by design, document chain completa);
  - 404 su UUID inesistente;
  - `task_id=None` serializzato correttamente come JSON `null`;
  - `event_payload={}` di default correttamente restituito come dict vuoto;
  - read-only invariant: counter pre/post invariati su tutte le tabelle (incluse `source_loss_propagation_records`, `claim_ledger_entries`).
- **Rischi.** Esposizione di `event_payload` JSONB; coerente con MVP-0.
- **Cosa NON deve fare.**
  - Non chiamare `propagate_source_loss`.
  - Non interrogare `source_loss_propagation_records` (endpoint 4.3 fa quello).
  - Non risolvere `task_id` via `claim_evidence_links` se è NULL nel DB: il NULL è informazione di dominio, non da camuffare.

### 4.3 8.6C — `GET /api/v1/source-loss-events/{source_loss_event_id}/propagation`

- **Perché serve.** Dato un source loss event, espone l'intera collezione di `source_loss_propagation_records` generata dal propagator: ogni claim impattato (con vecchio/nuovo ledger entry id), ogni published_answer attivo impattato, e gli stati degeneri `no_claims_impacted` e `no_active_published_answers_impacted`. È il "what happened" del propagator, leggibile via HTTP.
- **Tabelle lette.** `source_loss_propagation_records` filtrate per `source_loss_event_id`, più un check di esistenza su `source_loss_events` per discriminare la 404.
- **Response shape indicativa.**
  ```json
  {
    "source_loss_event_id": "<uuid>",
    "items": [
      {
        "id": "<uuid>",
        "source_loss_event_id": "<uuid>",
        "claim_logical_id": "<uuid|null>",
        "old_claim_ledger_entry_id": "<uuid|null>",
        "new_claim_ledger_entry_id": "<uuid|null>",
        "published_answer_id": "<uuid|null>",
        "propagation_kind": "claim_marked_unverifiable",
        "status": "recorded",
        "details": { ... },
        "created_at": "..."
      }
    ]
  }
  ```
  Ogni item rispetta `SourceLossPropagationRecordRead`. Ordinamento ASC per `(created_at, id)`. Gli Optional UUID sono `null` quando inapplicabili (es. `no_claims_impacted` ha tutti gli Optional a None).
- **Filtri/paginazione minimi.**
  - `limit`: int, default 500, min 1, max 5000.
  - `propagation_kind`: opzionale, Literal `{claim_marked_unverifiable, published_answer_impacted, no_claims_impacted, no_active_published_answers_impacted}`.
  - `status`: opzionale, Literal `{recorded, skipped, failed}`.
- **Errori normalizzati.**
  - 404 `RESOURCE_NOT_FOUND` con `details.resource="source_loss_events"` se la SLE non esiste.
  - Lista vuota (200) se la SLE esiste ma il propagator non ha ancora processato (race tra POST + GET).
  - 400 `VALIDATION_ERROR` su filtri non in Literal o `limit` fuori range.
- **Test necessari.** In `apps/api/tests/test_source_loss_propagation_endpoint.py`:
  - happy path scenario "realistic source loss": 1 `claim_marked_unverifiable/recorded` + 1 `no_active_published_answers_impacted/recorded`;
  - happy path con `published_answer_impacted/recorded` presente;
  - filtro `propagation_kind=claim_marked_unverifiable` isola la riga claim;
  - filtro `status=recorded` isola tutte le righe `recorded`;
  - 404 su SLE inesistente;
  - lista vuota (200) se SLE esiste ma propagation ancora non avvenuta;
  - row `failed` correttamente serializzata (i partial UNIQUE non la coprono ma resta nella tabella append-only);
  - filtri invalidi → 400 `VALIDATION_ERROR`;
  - read-only invariant globale.
- **Rischi.** Il campo `details` JSONB contiene `service_name`, `service_version`, `call_idempotency_key`, riferimenti incrociati a entry ledger. Esposizione coerente con la convenzione 8.4/8.5.
- **Cosa NON deve fare.**
  - Non chiamare `propagate_source_loss`.
  - Non scrivere righe `failed` retroattive.
  - Non collassare `recorded + skipped` in un unico stato.
  - Non escludere righe `failed` di default: sono storia append-only valida.

### 4.4 8.6D — `GET /api/v1/tasks/{task_id}/source-loss-events`

- **Perché serve.** Vista task-centric della source loss. Dato che `source_loss_events.task_id` è spesso NULL (l'API producer 8.5 lo lascia by-design NULL perché una span può servire più task), la sola lookup per `task_id` non basta. Lo smoke test 8.5 chiede di osservare le source loss "che hanno impattato" un task: questo richiede l'unione di due insiemi.
  - **Insieme S1 — `task_scope`:** righe con `source_loss_events.task_id = :tid`. Rare in MVP-0 (`task_id` NULL by-design dall'API producer), ma valide quando un consumer interno o un futuro producer creasse SLE già scoped.
  - **Insieme S2 — `claim_evidence_link`:** righe la cui `evidence_span_id` è collegata via `claim_evidence_links → logical_claims.task_id = :tid`. È esattamente la stessa logica con cui il propagator computa l'impact set sui claim.
  - L'endpoint ritorna `S1 ∪ S2`, distinct per `source_loss_events.id`. Se una stessa SLE soddisfa entrambi, vince `task_scope`.
- **Tabelle lette.** `source_loss_events`, `claim_evidence_links`, `logical_claims`, più un check di esistenza su `task_masters` per discriminare la 404.
- **Response shape indicativa.**
  ```json
  {
    "task_id": "<uuid>",
    "items": [
      {
        "source_loss_event": { /* SourceLossEventRead */ },
        "impacted_via": "claim_evidence_link"
      }
    ]
  }
  ```
  `impacted_via` ∈ `{task_scope, claim_evidence_link}`. Ordinamento ASC per `(source_loss_events.created_at, id)`.
- **Filtri/paginazione minimi.**
  - `limit`: int, default 200, min 1, max 2000.
  - Nessun filtro su `impacted_via` in 8.6 (può essere aggiunto in una fase futura senza rotture).
- **Errori normalizzati.**
  - 404 `RESOURCE_NOT_FOUND` con `details.resource="task_masters"`, `details.id=<uuid>` se il task non esiste.
  - Lista vuota (200) se il task esiste ma nessuna SLE lo impatta.
- **Test necessari.** In `apps/api/tests/test_task_source_loss_events_endpoint.py`:
  - happy path solo-S2: scenario "realistic source loss" (task con claim collegato a evidence_span, POST `source-loss-events` con `task_id=NULL` per il DB, GET ritorna 1 item con `impacted_via="claim_evidence_link"`);
  - happy path con S1: seed manuale di un SLE con `task_id` non-null → `impacted_via="task_scope"`;
  - happy path misto S1+S2 → entrambi presenti, niente duplicati su `source_loss_events.id`, `task_scope` prevale su `claim_evidence_link` per la stessa SLE;
  - 404 su task inesistente;
  - lista vuota su task esistente senza SLE collegate;
  - `limit=1` rispettato;
  - read-only invariant.
- **Rischi.** Query con due join + UNION (o equivalente). Costo accettabile per MVP-0 (closed corpus, volumi piccoli); indici esistenti utili: `sle_task_created_idx`, `cel_logical_idx`, `logical_claims_task_idx`. Non servono nuovi indici per il volume MVP-0.
- **Cosa NON deve fare.**
  - Non riusare la query del propagator (è scritta per i suoi scopi; duplicarla in API risk-ifica drift): riscrittura locale, semplice, ben commentata.
  - Non eseguire propagation o lookup di `claim_ledger_entries` v(N+1): lo scope di questo endpoint è "quali SLE toccano questo task", non "quali claim sono stati superseded".
  - Non camuffare il `task_id` NULL: l'endpoint espone esattamente la SLE come è scritta a DB, con il NULL preservato; la chiave del task è il `task_id` del path e il flag `impacted_via`.

### 4.5 8.6 Stretch (opzionale) — `GET /api/v1/published-answers/{published_answer_id}/source-loss-impact`

- **Perché serve.** Inverso di 4.3: dato un `published_answer`, espone tutte le propagation rows `published_answer_impacted` che lo riferiscono, e tutti i source loss events corrispondenti. Utile per dashboard "questa risposta è ancora affidabile?" o per audit a posteriori.
- **Tabelle lette.** `source_loss_propagation_records` JOIN `source_loss_events`, più un check di esistenza su `published_answers`.
- **Response shape indicativa.**
  ```json
  {
    "published_answer_id": "<uuid>",
    "items": [
      {
        "propagation_record": { /* SourceLossPropagationRecordRead */ },
        "source_loss_event": { /* SourceLossEventRead */ }
      }
    ]
  }
  ```
- **Filtri/paginazione minimi.** `limit` (default 200, max 2000).
- **Errori normalizzati.** 404 `RESOURCE_NOT_FOUND` con `details.resource="published_answers"`.
- **Test necessari.** In `apps/api/tests/test_published_answer_source_loss_impact_endpoint.py`:
  - happy path con PA impattato (almeno una `published_answer_impacted/recorded`);
  - happy path con PA non impattato (lista vuota);
  - 404 su id inesistente;
  - read-only invariant.
- **Rischi.** Aumenta la superficie di 8.6. Se 4.4 + 4.3 coprono già lo smoke test e i casi d'uso operativi, 4.5 può essere rinviato senza perdita.
- **Cosa NON deve fare.**
  - Non eseguire withdraw automatico.
  - Non emettere alcun evento audit.
  - Non chiamare `apply_withdrawal`.
  - Non escludere PA in stato `withdrawn`: l'impatto storico va mostrato anche su PA ormai ritirati.

**Decisione vincolante su 4.5:** opzionale. La raccomandazione minima per "8.6 completa" è 4.1 + 4.2 + 4.3 + 4.4. 4.5 entra solo come blocco extra se il tempo e l'effettivo bisogno lo giustificano.

---

## 5. Schema response proposto

**Schemi shared già esistenti in `packages/shared/evidencefirst_shared/schemas.py`, da riusare invariati:**

- `PublishedAnswerLifecycleEventRead` — usato da 4.1.
- `SourceLossEventRead` — usato da 4.2, 4.4, 4.5.
- `SourceLossPropagationRecordRead` — usato da 4.3, 4.5.

**Wrapper locali preferiti** (definiti nei route module dove servono, non in `schemas.py`):

- 4.1: wrapper inline `{"published_answer_id": <uuid>, "items": [PublishedAnswerLifecycleEventRead, ...]}`. Nessun BaseModel dedicato richiesto, stesso pattern di `claims.py` con `{"items": [...]}`. Se per qualche ragione (es. necessità di un response_model strict) servisse un BaseModel, definirlo localmente nel route module.
- 4.3: wrapper inline `{"source_loss_event_id": <uuid>, "items": [SourceLossPropagationRecordRead, ...]}`.
- 4.4: BaseModel locale `TaskSourceLossEventItem` con due campi (`source_loss_event: SourceLossEventRead`, `impacted_via: Literal["task_scope", "claim_evidence_link"]`), più wrapper `{"task_id": <uuid>, "items": [TaskSourceLossEventItem, ...]}`. La semantica extra (`impacted_via`) giustifica un BaseModel locale rispetto a un dict inline.
- 4.5 (stretch): BaseModel locale `PublishedAnswerSourceLossImpactItem` con `{propagation_record: SourceLossPropagationRecordRead, source_loss_event: SourceLossEventRead}`, più wrapper `{"published_answer_id": <uuid>, "items": [...]}`.

**Principio.** Non aggiungere nulla a `packages/shared/evidencefirst_shared/schemas.py` in 8.6 se non strettamente necessario. Le wrapper class sono response-shape, non domain-shape; appartengono ai route module. Aggiungere a `schemas.py` significa toccare un file shared cross-app (API + worker) ed è non breaking ma da evitare quando non serve.

---

## 6. Error handling

Tutti gli endpoint usano i pattern già consolidati in 8.4/8.5:

- **404 `RESOURCE_NOT_FOUND`** con `details.resource` corretta per il sotto-dominio:
  - `details.resource="published_answers"` — 4.1, 4.5 quando il published_answer non esiste.
  - `details.resource="source_loss_events"` — 4.2, 4.3 quando la SLE non esiste.
  - `details.resource="task_masters"` — 4.4 quando il task non esiste.
  - In ogni caso `details.id=<uuid>`.
- **400 `VALIDATION_ERROR`** per query string malformate:
  - `limit` fuori range (gestito da Pydantic `Query(ge=..., le=...)`).
  - `event_type`/`propagation_kind`/`status` non in Literal (gestito da Pydantic Literal).
  - `RequestValidationError` viene normalizzato da `install_normalized_error_handler` in `evidencefirst_shared/errors.py`.
- **500 `INTERNAL_ERROR`** solo per eccezioni non previste. Nessun caso atteso per endpoint puramente read-only.

**Nessun nuovo `ErrorCode` introdotto** in 8.6. L'enum esistente in `packages/shared/evidencefirst_shared/errors.py` è sufficiente.

**Nessun 409, 501, o codice custom** per nessuno dei nuovi endpoint. Nessun envelope custom: l'handler di `errors.py` produce la stessa shape `{"error": {"code", "message", "details", "request_id", "policy_version", "remediation_hint"}}` già usata da 8.4/8.5.

---

## 7. Test plan

Pattern: per ogni endpoint, file di test dedicato sotto `apps/api/tests/`, stessa struttura dei file 8.5 esistenti. TestClient privato per test (no fixture session-scoped). Nessun Redis, nessun monkeypatch su `get_redis`: gli endpoint sono read-only puri, non chiamano XADD. DB reale tramite il pattern `_skip_if_db_unreachable()` già consolidato.

**File di test API previsti:**

- `apps/api/tests/test_published_answer_lifecycle_events_endpoint.py` (8.6A)
- `apps/api/tests/test_source_loss_events_read_endpoint.py` (8.6B)
- `apps/api/tests/test_source_loss_propagation_endpoint.py` (8.6C)
- `apps/api/tests/test_task_source_loss_events_endpoint.py` (8.6D)
- `apps/api/tests/test_published_answer_source_loss_impact_endpoint.py` (8.6 stretch, opzionale)

**File di realistic read flow previsto:**

- `tests/test_phase_8_6_read_flow.py` (8.6E)

**Casi minimi obbligatori per ogni endpoint:**

1. **happy path** con seed minimo che produce dati osservabili dall'endpoint;
2. **404** sul dominio inesistente, con `details.resource` corretta e `details.id` corretto;
3. **lista vuota (200)** quando il dominio esiste ma nessun sotto-elemento (applicabile a 4.1, 4.3, 4.4, 4.5);
4. **filtri validi** restringono correttamente (4.1: `event_type`; 4.3: `propagation_kind`, `status`);
5. **filtri invalidi** → 400 `VALIDATION_ERROR` (4.1, 4.3);
6. **`limit`** rispettato (es. `limit=1`);
7. **read-only invariant**: snapshot pre/post di counter su `published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records`, `published_answers`, `claim_ledger_entries`, `claim_lineage`, `audit_records`. Tutti invariati. Questo è il test load-bearing di "l'endpoint non muta DB".

**Realistic read flow (`tests/test_phase_8_6_read_flow.py`)** — 8.6E:

1. Esegue lo smoke test 8.5 withdrawal (chiama l'API producer, dispatcher diretto come negli esistenti realistic flow, verifica DB);
2. Chiama via HTTP `GET /api/v1/published-answers/{id}/lifecycle-events` e verifica che ci siano i due eventi attesi (`withdrawal_requested`, `withdrawn`);
3. Esegue lo smoke test 8.5 source-loss (idem);
4. Chiama via HTTP `GET /api/v1/source-loss-events/{id}`, `GET /api/v1/source-loss-events/{id}/propagation`, `GET /api/v1/tasks/{tid}/source-loss-events` e verifica le response;
5. Asserzione finale di audit chain integrity via `verify_task_audit_chain` (proprietà DB-side, non esposta via HTTP).

Questo test sostituisce il bisogno di `psql` nello smoke 8.5 documentato in `PHASE_8_5_PLAN.md` §10.

**Test che NON vengono toccati in 8.6:**

- Nessun nuovo test sul worker.
- Nessun nuovo test sul dispatcher.
- Nessuna modifica ai test 8.5 esistenti.
- Nessuna modifica ai test 8.4 esistenti.

---

## 8. Rollout a blocchi

Cinque blocchi 8.6A–8.6E più uno stretch opzionale. Ogni blocco è autonomo: si può fermare a qualunque punto senza degradare lo stato del repo.

- **8.6A — Lifecycle read endpoint + tests.**
  - File nuovo: `apps/api/app/routes/lifecycle_events.py` (router separato per non gonfiare `answers.py`), oppure estensione di `answers.py` — decisione di stile in fase implementativa.
  - Registrazione router in `apps/api/app/main.py`.
  - File nuovo: `apps/api/tests/test_published_answer_lifecycle_events_endpoint.py`.

- **8.6B — Source-loss event read endpoint + tests.**
  - Estensione di `apps/api/app/routes/source_loss.py` aggiungendo `GET /api/v1/source-loss-events/{id}`.
  - File nuovo: `apps/api/tests/test_source_loss_events_read_endpoint.py`.

- **8.6C — Source-loss propagation read endpoint + tests.**
  - Estensione di `apps/api/app/routes/source_loss.py` con `GET /api/v1/source-loss-events/{id}/propagation`.
  - File nuovo: `apps/api/tests/test_source_loss_propagation_endpoint.py`.

- **8.6D — Task-level source-loss listing + tests.**
  - File nuovo: `apps/api/app/routes/task_source_loss.py`, oppure estensione di `source_loss.py` o `tasks.py` — decisione di stile.
  - Registrazione router in `apps/api/app/main.py` (se file nuovo).
  - File nuovo: `apps/api/tests/test_task_source_loss_events_endpoint.py`.

- **8.6E — Realistic read flow tests + docs update.**
  - File nuovo: `tests/test_phase_8_6_read_flow.py`.
  - Aggiornamento di `PROJECT_STATE.md` (tabella endpoint aggiornata, riferimento ai nuovi endpoint nella sezione "Cosa esiste oggi").
  - Aggiornamento di `README.md` (eventuale nuovo smoke test).

- **8.6 Stretch (opzionale) — Published-answer source-loss impact + tests.**
  - Estensione di `apps/api/app/routes/answers.py` (o file dedicato) con `GET /api/v1/published-answers/{id}/source-loss-impact`.
  - File nuovo: `apps/api/tests/test_published_answer_source_loss_impact_endpoint.py`.
  - Eseguibile solo dopo 8.6C e 8.6E.

**Proprietà di ogni blocco:** aggiunge solo file nuovi o estende route module esistenti con metodi GET. La compatibilità con il codice precedente è garantita: nessun cambio breaking, nessuna firma modificata.

---

## 9. Rischi residui

Rischi reali, verificati o ragionevolmente derivati dai file letti.

- **Esposizione di `event_payload` e `details` JSONB senza RBAC.** I tre endpoint che leggono campi JSONB (4.1, 4.2, 4.3, 4.4 indirettamente, 4.5) espongono payload opachi a chiunque conosca un UUID. Coerente con altre read API MVP-0 (audit, claims, draft) che già espongono `payload` JSONB. Va dichiarato in docstring degli endpoint. Una futura fase con RBAC dovrà introdurre redazione/filtri.
- **`source_loss_events.task_id` può essere NULL by-design.** L'API producer 8.5 lascia `task_id` NULL perché una span può servire più task. L'endpoint 4.4 deve implementare `S1 ∪ S2` (task_scope + claim_evidence_link) per non ritornare lista vuota su task realmente impattati. La logica `S2` replica la stessa risoluzione del propagator ma è riscritta localmente in API per evitare drift.
- **Race POST `source-loss-events` → GET `propagation`.** Tra il `POST /api/v1/source-loss-events` e il `GET /api/v1/source-loss-events/{id}/propagation` esiste una finestra in cui la SLE esiste ma le propagation rows no (il worker non ha ancora elaborato). L'endpoint ritorna 200 con `items=[]`; il client deve polling-are. Documentato in docstring.
- **Nessuna paginazione cursor-based.** I `limit` (200/500/2000/5000) mitigano response esplose ma non eliminano il rischio su task molto vecchi o su published_answer con storia lifecycle estesa. Cursor rinviato a fasi future, coerente con `claims.py` 8.3 che usa lo stesso approccio limit-only.
- **Le tabelle 8.5 crescono senza retention `0007`.** `published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records` crescono linearmente. I nuovi read endpoint non aggravano il problema ma lo rendono più visibile (response più grandi col tempo). `0007_evaluation_retention.sql` resta out-of-scope per 8.6.
- **`published_answer` creati prima di 8.5 non hanno l'evento lifecycle `published`.** `PHASE_8_5_PLAN.md` §7 chiarisce che 8.5 non ha eseguito alcun backfill. Conseguenza pratica: `GET /api/v1/published-answers/{id}/lifecycle-events` su un PA pre-8.5 mostrerà lista vuota (se ancora published) o solo `withdrawal_requested`/`withdrawn` (se withdrawn). 8.6 non corregge questo: sarebbe una mutazione DB ed è esplicitamente fuori scope. Va documentato in docstring di 4.1.
- **Realistic flow usa FakeRedis.** I test in `tests/test_phase_8_5_*_flow.py` e il futuro `tests/test_phase_8_6_read_flow.py` non attraversano un Redis reale: usano FakeRedis e invocano il dispatcher direttamente. Implicazione: la semantica di transport Redis (`XREADGROUP`, `XACK`, PEL, signal handlers) non è coperta dai realistic flow.
- **Worker loop reale non attraversato dai realistic flow.** `apps/worker/app/main.py` è coperto solo da unit test (`apps/worker/tests/test_main_multistream.py`) con FakeRedis interno. Nessun test end-to-end con Redis reale + worker loop reale è presente nel repo né è introdotto da 8.6.
- **`idempotency_key` esposta in chiaro.** Sia consumer-level (in EPR, non esposti) sia service-level (su lifecycle events e source_loss events, esposti dai nuovi endpoint). In MVP-0 sono valori opachi privi di significato di sicurezza; in produzione potrebbero diventare leak-vector se i client li scelgono male. Da segnalare.
- **Disallineamento di nomenclatura URL.** `/propagation` singolare vs `/propagation-records` plurale: scelta singolare per coerenza con altre risorse REST 8.4 (`/history`, `/evidence`), ma è una decisione di gusto, non vincolante.
- **Verifica audit chain non esposta via HTTP.** `verify_task_audit_chain` resta proprietà DB-side. Una futura `GET /api/v1/tasks/{id}/audit?verify=true` sarebbe utile ma è out-of-scope per 8.6.

---

## 10. Criteri di completamento

La Fase 8.6 è completa quando, in ordine:

1. **Endpoint implementati e registrati** in `apps/api/app/main.py`:
   - 4.1, 4.2, 4.3, 4.4 obbligatori per "8.6 completa" minima.
   - 4.5 facoltativo (stretch).
2. **`make test-api` passa** con i nuovi test file inclusi.
3. **`make test-db` passa** con `tests/test_phase_8_6_read_flow.py` incluso.
4. **`make test` (gate finale) passa** senza regressioni su test 8.4/8.5.
5. **Per ogni endpoint, l'invariante read-only è verificata** da almeno un test che fa snapshot pre/post dei counter su tutte le tabelle 8.5 + `published_answers` + `claim_ledger_entries` + `claim_lineage` + `audit_records`.
6. **Lo smoke test in `PROJECT_STATE.md` / `PHASE_8_5_PLAN.md` §10 si può eseguire interamente via HTTP**, senza ricorrere a `make psql`.
7. **`PROJECT_STATE.md` aggiornato a fine fase** con la nuova tabella degli endpoint e un riferimento ai nuovi domini esposti via API.
8. **Nessuna modifica involontaria** a file in `apps/worker/`, `migrations/`, `apps/worker/tests/`, e nessuna modifica ai test 8.5 esistenti (`test_published_answer_withdrawal_request.py`, `test_source_loss_endpoint.py`, `test_phase_8_5_*_flow.py`). Verifica diff git pre-merge.
9. **Endpoint dimostrabilmente read-only:** la suite passa anche con un `monkeypatch` paranoico che intercetta `INSERT`/`UPDATE`/`DELETE` sulle tabelle 8.5 e fallisce se chiamate da un route module (opzionale, ma fortemente raccomandato come gate qualitativo).

---

## 11. Decisione documentale

- **Creare `PHASE_8_6_PLAN.md` ora** (questo file), come record di piano e successivamente di stato della Fase 8.6. Stesso ruolo che `PHASE_8_5_PLAN.md` ha avuto per la Fase 8.5.
- **Non aggiornare `PROJECT_STATE.md` ora.** `PROJECT_STATE.md` riflette lo stato del repo, non lo stato del piano. Aggiornarlo prima dell'implementazione produrrebbe documentazione inconsistente con il codice.
- **`PROJECT_STATE.md` si aggiornerà al termine dell'implementazione parziale o completa di 8.6**, all'interno del blocco 8.6E (o di un suo equivalente). L'aggiornamento conterrà:
  - tabella endpoint aggiornata con i nuovi GET;
  - sezione "Cosa esiste oggi" estesa con i nuovi domini esposti;
  - rimozione (o aggiornamento) della voce "read API per i nuovi domini lifecycle e source loss" da §11 "Prossimo passo" di `PROJECT_STATE.md`.
- **Non aggiornare `README.md` ora.** Eventuale aggiornamento dello smoke test in `README.md` rientra nel blocco 8.6E, dopo che gli endpoint sono implementati e testati.
- **Non creare alcun nuovo file in `migrations/`, `apps/`, `packages/`, `tests/` in questa fase di solo-piano.** Questo file è esclusivamente piano.

---

FILE_COMPLETATI
- PHASE_8_6_PLAN.md

FILE_DA_NON_TOCCARE_ORA
- codice applicativo
- tests
- migrations
- PROJECT_STATE.md
- README.md

RISCHI_RESIDUI
- `event_payload` e `details` JSONB esposti senza RBAC, coerente con MVP-0 ma debito di sicurezza esplicito.
- `source_loss_events.task_id` può essere NULL: l'endpoint 4.4 richiede `S1 ∪ S2` (task_scope + claim_evidence_link) per non perdere SLE impattanti il task.
- Race POST `source-loss-events` → GET `propagation`: la SLE può esistere senza propagation rows finché il worker non ha processato; il GET ritorna lista vuota.
- Niente cursor pagination: `limit` semplice, dataset bounded ma può diventare grande nel tempo.
- Le tabelle 8.5 crescono senza retention (`0007` non esiste); i nuovi endpoint rendono il problema più visibile.
- I `published_answer` creati prima di 8.5 non hanno l'evento lifecycle `published` (nessun backfill); il GET 4.1 può mostrare lista vuota anche su PA legittimamente pubblicati pre-8.5.
- I realistic flow test usano FakeRedis e dispatcher diretto: la semantica di transport Redis non è coperta.
- Il worker loop reale (`XREADGROUP`/`XACK`/PEL/signal handlers) non è attraversato dai realistic flow di 8.5 né da quelli previsti per 8.6.
