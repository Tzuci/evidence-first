# PHASE_8_6_PLAN — Evidence-First MVP-0

Documento di piano/stato per la **Fase 8.6** dell'Evidence-First MVP-0. La fase introduce endpoint API read-only di osservabilità sui domini lifecycle e source-loss introdotti in 8.5.

Questo file non implica automaticamente scrittura di codice, test o migration: ogni blocco 8.6 viene aperto, implementato, testato e committato separatamente.

---

## 0. Stato corrente della Fase 8.6

La **Fase 8.6 minima è completata**.

Blocchi completati:

- **8.6A — Lifecycle read endpoint + tests**
  - `GET /api/v1/published-answers/{published_answer_id}/lifecycle-events`
  - `apps/api/app/routes/lifecycle_events.py`
  - registrazione router in `apps/api/app/main.py`
  - `apps/api/tests/test_published_answer_lifecycle_events_endpoint.py`
  - commit: `e2b5472` — `Add published answer lifecycle events endpoint`
- **8.6B — Source-loss event read endpoint + tests**
  - `GET /api/v1/source-loss-events/{source_loss_event_id}`
  - estensione di `apps/api/app/routes/source_loss.py`
  - `apps/api/tests/test_source_loss_events_read_endpoint.py`
  - commit: `dedf0ac` — `Add source loss event read endpoint`
- **8.6C — Source-loss propagation read endpoint + tests**
  - `GET /api/v1/source-loss-events/{source_loss_event_id}/propagation`
  - estensione di `apps/api/app/routes/source_loss.py`
  - `apps/api/tests/test_source_loss_propagation_endpoint.py`
  - commit: `2da610c` — `Add source loss propagation read endpoint`
- **8.6D — Task-level source-loss listing + tests**
  - `GET /api/v1/tasks/{task_id}/source-loss-events`
  - `apps/api/app/routes/task_source_loss.py`
  - registrazione router in `apps/api/app/main.py`
  - `apps/api/tests/test_task_source_loss_events_endpoint.py`
  - commit: `cd26cb4` — `Add task source loss events endpoint`
- **8.6E-1 — Realistic read flow test**
  - `tests/test_phase_8_6_read_flow.py`
  - due scenari cross-component: withdrawal read flow + source-loss read flow
  - commit: `7ee687b` — `Add realistic phase 8.6 read flow test`

Stretch opzionale non implementato:

- **8.6 Stretch — Published-answer source-loss impact**
  - `GET /api/v1/published-answers/{published_answer_id}/source-loss-impact`
  - resta opzionale, non implementato.

Risultati di test riportati per la 8.6E-1:

- `tests/test_phase_8_6_read_flow.py` → 2 passed.
- `tests/` root → 70 passed.

Risultati di `make test`, `make test-api`, `make test-worker`, `make test-shared`, `make test-db` come gate complessivo non sono dichiarati come passati in questo documento per la 8.6: non sono stati riportati in modo esplicito. La 8.6 minima resta completata sulla base dei test sopra e degli endpoint registrati.

---

## Nota strategica importante — Source Quality Evaluator futuro

La Fase 8.6 si limita a esporre via read API ciò che la pipeline 8.5 scrive sui domini lifecycle e source-loss. È un livello di osservabilità: rende leggibili via HTTP eventi e propagazioni che prima richiedevano accesso diretto al database.

Questa osservabilità è necessaria, ma non basta per completare la visione anti-allucinazione del progetto. Il progetto è evidence-first, ma **“fonte presente” non significa automaticamente “fonte attendibile”**. Una risposta può essere collegata a una fonte reale e tuttavia la fonte può essere debole, obsoleta, commerciale, non indipendente, secondaria, contraddetta da fonti migliori o non sufficientemente rilevante.

La Fase 8.6 **NON** implementa un modulo di valutazione della qualità delle fonti. Non assegna score di credibilità a documenti, chunk, evidence spans o claim. Non distingue tra fonte primaria e secondaria. Non valuta autorevolezza, indipendenza, freschezza, conflitti di interesse o coerenza con fonti esterne. Non introduce alcuna logica di Source Quality Evaluation.

La necessità di integrare un modulo futuro di analisi dell’attendibilità delle fonti è quindi un punto strategico importante del progetto. Questo modulo dovrebbe diventare una fase dedicata successiva, ad esempio **8.7** o **9.0**, e dovrebbe permettere al sistema di passare da:

- claim collegato a evidenza;
- a claim collegato a evidenza di qualità valutata.

Un futuro **Source Quality Evaluator** dovrebbe valutare almeno:

- autorevolezza della fonte;
- fonte primaria vs fonte secondaria;
- autore, editore o istituzione responsabile;
- data di pubblicazione e freschezza;
- identificatori verificabili come DOI, PMID, ISBN, standard number o URL ufficiali;
- indipendenza della fonte;
- possibili conflitti di interesse;
- coerenza con altre fonti indipendenti;
- eventuali contraddizioni tra fonti;
- stabilità e accessibilità della fonte.

Il modulo futuro potrebbe produrre artefatti dedicati come:

- `source_quality_assessments`;
- `source_reliability_score`;
- `source_freshness_score`;
- `source_independence_score`;
- `source_primaryness_score`;
- `source_conflicts`.

Il Final Answer Gate futuro dovrebbe poter distinguere tra:

- claim supportato da fonte forte;
- claim supportato solo da fonte debole;
- claim supportato da fonti contraddittorie;
- claim non sufficientemente supportato.

Questa fase futura non deve essere confusa con 8.6: **8.6 rende osservabili lifecycle, source-loss e propagation; il Source Quality Evaluator renderà il sistema evidence-quality-aware**. È un prerequisito importante per avvicinare il progetto alla promessa anti-allucinazione completa, ed è oggi il debito più rilevante sul piano evidence-quality del progetto.

---

## 1. Stato di partenza

La **Fase 8.5 è completata tecnicamente e documentata**.

Elementi già presenti nel progetto al momento dell'avvio della 8.6:

- schema lifecycle e source-loss in `migrations/0006_lifecycle.sql`;
- `published_answer_lifecycle_events`;
- `source_loss_events`;
- `source_loss_propagation_records`;
- servizio worker `published_answer_lifecycle.apply_withdrawal`;
- servizio worker `source_loss_propagator.propagate_source_loss`;
- consumer `published_answer_withdrawal`;
- consumer `source_loss`;
- dispatcher worker con routing per `task.created`, `published_answer.withdrawal_requested`, `source_loss.detected`;
- worker multi-stream loop su tre stream Redis;
- endpoint API producer `POST /api/v1/published-answers/{published_answer_id}/withdrawal-requests`;
- endpoint API producer `POST /api/v1/source-loss-events`;
- realistic flow test withdrawal;
- realistic flow test source-loss.

Commit rilevanti:

- commit tecnico di completamento 8.5: `03c4186` (`Add realistic source loss flow test`);
- commit documentale post-8.5: `5f7f4ce` (`Update project state after phase 8.5`);
- commit piano 8.6: `4875698` (`Add phase 8.6 plan`);
- commit implementazione 8.6A: `e2b5472`;
- commit implementazione 8.6B: `dedf0ac`;
- commit implementazione 8.6C: `2da610c`;
- commit implementazione 8.6D: `cd26cb4`;
- commit realistic read flow 8.6E-1: `7ee687b`.

Cosa la 8.6 ha aggiunto rispetto a 8.5:

- read API per una singola riga `source_loss_events`;
- read API per `source_loss_propagation_records` di un dato source-loss event;
- listing task-level delle source loss collegate a un task tramite l'unione S1 ∪ S2 (task scope ∪ claim_evidence_link);
- realistic read flow test che verifica via HTTP gli effetti DB della pipeline 8.5.

---

## 2. Obiettivo 8.6

Aggiungere endpoint API read-only che espongono lo stato scritto dalla pipeline 8.5, riusando gli schemi shared già definiti e mantenendo zero side-effect sul DB.

Domini esposti:

- lifecycle events di un `published_answer`;
- source-loss event singolo, per id;
- propagation records di un source-loss event;
- source-loss events visibili da un task;
- opzionale (non implementato): impact set di source-loss su un singolo `published_answer`.

Risultato raggiunto: lo smoke test 8.5 è ora eseguibile interamente via HTTP, senza ricorrere a `psql`. Operatori e test di livello superiore possono osservare gli effetti del worker direttamente.

---

## 3. Non-obiettivi

Esplicitamente fuori scope per la Fase 8.6:

- nessuna nuova migration;
- nessun nuovo worker consumer;
- nessun nuovo stream Redis;
- nessun provider AI reale;
- nessuna web search;
- nessun frontend completo;
- nessuna auth/RBAC reale;
- nessuna retention distruttiva;
- nessun DLQ esplicito;
- nessuna modifica a servizi, consumer o worker main loop;
- nessuna estensione di `task_masters.status`;
- nessun withdrawal automatico da source-loss;
- nessuna modifica a CHECK constraint esistenti;
- nessuna modifica ai test 8.5 esistenti;
- nessun Source Quality Evaluator in 8.6.

La valutazione dell’attendibilità intrinseca delle fonti è una priorità strategica futura, ma non viene implementata in questa fase read-only.

---

## 4. Endpoint read-only

### 4.1 8.6A — `GET /api/v1/published-answers/{published_answer_id}/lifecycle-events`

**Stato:** implementato al commit `e2b5472`.

Perché serve: permette di osservare la storia lifecycle di un `published_answer` senza interrogare manualmente il DB.

Tabelle lette:

- `published_answers` per check di esistenza;
- `published_answer_lifecycle_events` per gli eventi.

Response shape indicativa:

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
      "event_payload": {},
      "requested_by": null,
      "idempotency_key": "...",
      "created_at": "..."
    }
  ]
}
```

Query params:

- `limit`: default 200, min 1, max 2000;
- `event_type`: opzionale, uno tra `published`, `withdrawal_requested`, `withdrawn`, `superseded`.

Errori:

- 404 `RESOURCE_NOT_FOUND` con `details.resource="published_answers"`;
- 400 / 422 `VALIDATION_ERROR` su query param non validi.

Cosa non fa:

- non chiama `apply_withdrawal`;
- non scrive lifecycle events;
- non fa backfill di `published`;
- non modifica `published_answers`;
- non usa Redis;
- non usa worker.

Test:

- `apps/api/tests/test_published_answer_lifecycle_events_endpoint.py`.

---

### 4.2 8.6B — `GET /api/v1/source-loss-events/{source_loss_event_id}`

**Stato:** implementato al commit `dedf0ac`.

Perché serve: permette di leggere una singola riga `source_loss_events` dopo il `POST /api/v1/source-loss-events`.

Tabelle lette:

- `source_loss_events`.

Response shape:

- `SourceLossEventRead` (schema shared).

Errori:

- 404 `RESOURCE_NOT_FOUND` con `details.resource="source_loss_events"`.

Cosa non fa:

- non chiama `propagate_source_loss`;
- non legge propagation records;
- non risolve `task_id` via claim graph se nel DB è `NULL` (per design: il campo è esposto verbatim);
- non modifica DB;
- non usa Redis.

Test:

- `apps/api/tests/test_source_loss_events_read_endpoint.py`.

Casi coperti dai test:

- happy path completo;
- 404 con `details.resource="source_loss_events"`;
- `event_payload={}` default surface come dict vuoto;
- read-only invariant con snapshot pre/post sui count delle tabelle 8.4/8.5/audit;
- nullable fields (`project_id`, `task_id`, `document_chunk_id`, `document_version_id`, `document_id`) serializzati come JSON `null`.

---

### 4.3 8.6C — `GET /api/v1/source-loss-events/{source_loss_event_id}/propagation`

**Stato:** implementato al commit `2da610c`.

Perché serve: dato un source-loss event, espone ciò che il propagator ha registrato in `source_loss_propagation_records`.

Tabelle lette:

- `source_loss_events` per check di esistenza;
- `source_loss_propagation_records`.

Query params:

- `limit`: default 500, min 1, max 5000;
- `propagation_kind`: opzionale, Literal sul codominio CHECK;
- `status`: opzionale, Literal sul codominio CHECK.

Errori:

- 404 `RESOURCE_NOT_FOUND` con `details.resource="source_loss_events"`;
- 400 / 422 `VALIDATION_ERROR` su query param non validi.

Cosa non fa:

- non chiama `propagate_source_loss`;
- non scrive propagation records;
- non collassa recorded/skipped/failed;
- non esclude righe `failed` (sono parte della storia osservabile).

Test:

- `apps/api/tests/test_source_loss_propagation_endpoint.py`.

Casi coperti dai test:

- happy path `claim_marked_unverifiable`;
- happy path `published_answer_impacted`;
- evento esistente con zero propagation rows → 200 `items=[]` (race window producer → propagator);
- 404 con `details.resource="source_loss_events"`;
- filtro `propagation_kind`;
- filtro `status=failed`;
- truncation con `limit`;
- query param invalidi rifiutati (400/422);
- read-only invariant con snapshot pre/post.

---

### 4.4 8.6D — `GET /api/v1/tasks/{task_id}/source-loss-events`

**Stato:** implementato al commit `cd26cb4`.

Perché serve: fornisce una vista task-centric delle source loss. Poiché `source_loss_events.task_id` può essere `NULL` per design, la query considera due insiemi:

- S1: source-loss events già task-scoped (`source_loss_events.task_id = :task_id`);
- S2: source-loss events collegate al task tramite `claim_evidence_links → logical_claims.task_id`.

L’endpoint ritorna `S1 ∪ S2`, distinct per `source_loss_events.id`. Quando una stessa riga soddisfa entrambi gli insiemi, `impacted_via='task_scope'` ha precedenza su `claim_evidence_link`.

Tabelle lette:

- `task_masters` per check di esistenza;
- `source_loss_events`;
- `claim_evidence_links`;
- `logical_claims`.

Query params:

- `limit`: default 200, min 1, max 2000.

Errori:

- 404 `RESOURCE_NOT_FOUND` con `details.resource="task_masters"`.

Cosa non fa:

- non chiama il propagator;
- non muta claim ledger;
- non camuffa `task_id=NULL` sulla SLE (il campo `source_loss_event.task_id` resta `null` anche quando il match è S2);
- non cerca lo stato v(N+1) del claim.

Test:

- `apps/api/tests/test_task_source_loss_events_endpoint.py`.

Casi coperti dai test:

- happy path solo S2 (`claim_evidence_link`);
- happy path solo S1 (`task_scope`);
- dedup precedence: stessa SLE in S1 e S2 → `impacted_via=task_scope`;
- mix S1 + S2 con due SLE distinte;
- task esistente senza eventi → 200 `items=[]`;
- 404 con `details.resource="task_masters"`;
- truncation con `limit`;
- read-only invariant con snapshot pre/post.

---

### 4.5 8.6 Stretch — `GET /api/v1/published-answers/{published_answer_id}/source-loss-impact`

**Stato:** opzionale, **non implementato**.

Perché servirebbe: dato un `published_answer`, mostrerebbe i source-loss events che lo hanno impattato via propagation records `published_answer_impacted`.

Tabelle che leggerebbe:

- `published_answers`;
- `source_loss_propagation_records`;
- `source_loss_events`.

Cosa non dovrebbe fare:

- non withdraw automatico;
- non audit;
- non chiamare `apply_withdrawal`;
- non nascondere impatti storici su PA già withdrawn.

Resta una direzione naturale ma non parte dei criteri di completamento della 8.6 minima.

---

## 5. Schema response

Schemi shared esistenti riusati invariati:

- `PublishedAnswerLifecycleEventRead`;
- `SourceLossEventRead`;
- `SourceLossPropagationRecordRead`.

Wrapper locali ai route module:

- lifecycle list wrapper (`{"published_answer_id", "items"}`);
- propagation list wrapper (`{"source_loss_event_id", "items"}`);
- task-level source-loss item con `impacted_via` (`task_scope` | `claim_evidence_link`).

Principio: niente aggiunte a `packages/shared/evidencefirst_shared/schemas.py` durante la 8.6.

---

## 6. Error handling

Pattern esistenti riusati:

- 404 `RESOURCE_NOT_FOUND`;
- 400 / 422 `VALIDATION_ERROR`;
- 500 `INTERNAL_ERROR` solo per eccezioni impreviste.

Resource details:

- `published_answers`;
- `source_loss_events`;
- `task_masters`.

Nessun nuovo `ErrorCode` introdotto in 8.6.

---

## 7. Test plan

File di test API presenti:

- `apps/api/tests/test_published_answer_lifecycle_events_endpoint.py` — 8.6A;
- `apps/api/tests/test_source_loss_events_read_endpoint.py` — 8.6B;
- `apps/api/tests/test_source_loss_propagation_endpoint.py` — 8.6C;
- `apps/api/tests/test_task_source_loss_events_endpoint.py` — 8.6D.

Realistic read flow:

- `tests/test_phase_8_6_read_flow.py` — due scenari cross-component:
  - withdrawal: API producer → FakeRedis → dispatcher → consumer → service → DB; poi GET 8.6A + GET single published_answer + verify_task_audit_chain;
  - source-loss: API producer → FakeRedis → dispatcher → consumer → propagator → DB; poi GET 8.6B + GET 8.6C + GET 8.6D + verify_task_audit_chain + verifica della head del claim a `unverifiable / unsupported / source_lost`.

Casi minimi per ogni endpoint, ribaditi: happy path, 404, lista vuota dove applicabile, filtri validi, filtri invalidi, limit, read-only invariant con snapshot pre/post.

Risultati noti del realistic flow (riportati al commit `7ee687b`):

- `tests/test_phase_8_6_read_flow.py` → 2 passed;
- `tests/` root → 70 passed.

Risultati di `make test`, `make test-api`, `make test-worker`, `make test-shared`, `make test-db` come gate complessivo dopo 8.6E non sono dichiarati come passati in questo documento perché non sono stati riportati in modo esplicito.

---

## 8. Rollout a blocchi — riepilogo

- **8.6A — Lifecycle read endpoint + tests** — completato (`e2b5472`).
- **8.6B — Source-loss event read endpoint + tests** — completato (`dedf0ac`).
- **8.6C — Source-loss propagation read endpoint + tests** — completato (`2da610c`).
- **8.6D — Task-level source-loss listing + tests** — completato (`cd26cb4`).
- **8.6E-1 — Realistic read flow test** — completato (`7ee687b`).
- **8.6E-2 — Docs update finale** — blocco corrente.
- **8.6 Stretch — Published-answer source-loss impact** — non implementato, opzionale.

Ogni blocco è stato autonomo, testato e committato separatamente.

---

## 9. Rischi residui

Rischi reali al termine della 8.6 minima:

- **RBAC mancante.** Gli endpoint read 8.6 espongono `event_payload`, `details` (JSONB), e ogni colonna delle tabelle senza alcuna autorizzazione applicativa o redaction.
- **JSONB esposti verbatim.** `event_payload` di `published_answer_lifecycle_events` e `source_loss_events`, `details` di `source_loss_propagation_records` sono restituiti nella loro forma persistita. Eventuali campi sensibili presenti nei payload arrivano al client.
- **FakeRedis nei realistic flow.** `tests/test_phase_8_6_read_flow.py` installa una `FakeRedis` sui route module 8.5 e ricostruisce l'evento direttamente per il dispatcher. Non c'è interazione con un Redis reale.
- **Worker main loop non attraversato nei realistic flow.** `XREADGROUP`, `XACK`, gestione PEL e signal handlers non vengono attraversati dai realistic flow 8.5 e 8.6. La copertura dello XREADGROUP/XACK resta limitata agli unit test in `apps/worker/tests/test_main_multistream.py` con FakeRedis interno.
- **Nessuna retention `0007`.** Le tabelle 8.5 (`published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records`) crescono senza politiche di pruning.
- **Nessuna cursor pagination.** Solo `limit` su lifecycle (max 2000), propagation (max 5000), task source-loss (max 2000). Nessun cursore stabile per scorrere liste lunghe.
- **Race producer → propagation read.** `POST /api/v1/source-loss-events` ritorna l'`id` immediatamente; le righe in `source_loss_propagation_records` arrivano solo dopo il consumer worker. L'endpoint 8.6C ritorna `200 items=[]` durante questa finestra: comportamento corretto ma da ricordare lato client.
- **`source_loss_events.task_id` può essere NULL.** Il producer API non lo deriva (uno span può supportare claim di task diversi). 8.6D copre la cosa via `S1 ∪ S2` con `impacted_via`, ma 8.6B continua a esporre `task_id=null` verbatim.
- **`published_answers` pre-8.5 senza lifecycle event `published`.** Nessun backfill è stato eseguito; 8.6A ritorna `items=[]` per quei published_answers, e questo è corretto rispetto allo stato DB.
- **Stretch endpoint non implementato.** `GET /api/v1/published-answers/{id}/source-loss-impact` resta opzionale.
- **Source Quality Evaluator futuro.** Il punto strategico più importante: 8.6 espone evidenze e propagazioni, ma non valuta ancora la qualità delle fonti.

---

## 10. Criteri di completamento — verifica

La Fase 8.6 minima è completa quando:

1. 8.6A, 8.6B, 8.6C e 8.6D sono implementati e registrati in `apps/api/app/main.py` — **soddisfatto**.
2. I test API dedicati passano — **soddisfatto sui file di test esistenti**; non si dichiara il pass dell'intero `make test-api` perché non si ha evidenza esplicita.
3. Il realistic read flow `tests/test_phase_8_6_read_flow.py` passa — **soddisfatto**, 2 passed riportati al commit `7ee687b`; root `tests/` riportato a 70 passed.
4. `make test-api`, `make test-db` e `make test` passano — **non dichiarato come passato in questo documento**: non vi è evidenza esplicita riportata per la 8.6E.
5. Ogni endpoint è coperto da read-only invariant — **soddisfatto** (snapshot pre/post in ciascun file di test API e nelle assertion del realistic flow).
6. `PROJECT_STATE.md` viene aggiornato a fine fase — **soddisfatto** dal blocco 8.6E-2 (questo aggiornamento documentale).
7. Nessuna migration, nessun worker e nessun test 8.5 esistente è stato modificato — **soddisfatto** dalla revisione dei file 8.5 nel repo.
8. Gli endpoint restano read-only — **soddisfatto**, verificato dai read-only invariant test.

Per i punti 2 e 4 l'evidenza disponibile copre i singoli file di test eseguiti per il blocco 8.6E-1; un eventuale gate `make test` completo va eseguito separatamente come passo finale.

---

## 11. Decisione documentale

- `PHASE_8_6_PLAN.md` resta il documento di piano/stato della fase 8.6 e dichiara ora la **8.6 minima come completata**.
- `PROJECT_STATE.md` viene aggiornato in 8.6E-2 al commit `7ee687b`, con la nuova lista di endpoint API e la sezione "cosa esiste oggi" estesa con i quattro GET 8.6 e il realistic read flow.
- `README.md` non viene modificato in 8.6E-2: non contiene una sezione endpoint/stato 8.5/8.6 da correggere, `PROJECT_STATE.md` copre già lo stato corrente in dettaglio, e gonfiare README con dettagli interni andrebbe contro la decisione documentale di tenere i due file con responsabilità separate.
- La nota sul Source Quality Evaluator resta strategica e centrale: 8.6 NON la implementa; resta priorità futura.

---

FILE_COMPLETATI
- PHASE_8_6_PLAN.md
- PROJECT_STATE.md

FILE_DA_NON_TOCCARE_ORA
- codice applicativo
- tests
- migrations
- README.md (non contiene una sezione 8.5/8.6 evidentemente obsoleta)

RISCHI_RESIDUI
- RBAC mancante sugli endpoint read 8.6.
- `event_payload` e `details` JSONB esposti verbatim.
- FakeRedis nei realistic flow; dispatcher invocato direttamente.
- Worker main loop reale (XREADGROUP/XACK, PEL, signal handlers) non attraversato dai realistic flow.
- Nessuna retention `0007`.
- Niente cursor pagination.
- Race producer → propagation read gestita come 200 items=[].
- `source_loss_events.task_id` può essere NULL by design; 8.6D usa S1 ∪ S2.
- Published answers pre-8.5 senza lifecycle event `published` (nessun backfill).
- Stretch `/published-answers/{id}/source-loss-impact` non implementato.
- Source Quality Evaluator: debito strategico futuro, non implementato.
- `make test` / `make test-api` complessivi non dichiarati come passati dopo la 8.6E in questo documento: pass riportati limitatamente a `tests/test_phase_8_6_read_flow.py` (2 passed) e `tests/` root (70 passed).
