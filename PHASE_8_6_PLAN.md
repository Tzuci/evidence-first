# PHASE_8_6_PLAN — Evidence-First MVP-0

Documento di piano/stato per la **Fase 8.6** dell'Evidence-First MVP-0. La fase introduce endpoint API read-only di osservabilità sui domini lifecycle e source-loss introdotti in 8.5.

Questo file non implica automaticamente scrittura di codice, test o migration: ogni blocco 8.6 viene aperto, implementato, testato e committato separatamente.

---

## 0. Stato corrente della Fase 8.6

La Fase 8.6 è stata pianificata e avviata.

- Piano 8.6 creato al commit `4875698ad9c6129653d2f55de61313cefdcc2a0e` (`Add phase 8.6 plan`).
- Primo blocco implementativo completato al commit `e2b54727fa8868360161fde797eed5685e90be27` (`Add published answer lifecycle events endpoint`).

Blocco completato:

- **8.6A — Lifecycle read endpoint + tests**
  - `GET /api/v1/published-answers/{published_answer_id}/lifecycle-events`
  - `apps/api/app/routes/lifecycle_events.py`
  - registrazione router in `apps/api/app/main.py`
  - `apps/api/tests/test_published_answer_lifecycle_events_endpoint.py`

Blocchi ancora da completare per la 8.6 minima:

- **8.6B — Source-loss event read endpoint**
  - `GET /api/v1/source-loss-events/{source_loss_event_id}`
- **8.6C — Source-loss propagation read endpoint**
  - `GET /api/v1/source-loss-events/{source_loss_event_id}/propagation`
- **8.6D — Task-level source-loss listing**
  - `GET /api/v1/tasks/{task_id}/source-loss-events`
- **8.6E — Realistic read flow tests + docs update**
  - `tests/test_phase_8_6_read_flow.py`
  - aggiornamento finale di `PROJECT_STATE.md`

Stretch opzionale:

- **8.6 Stretch — Published-answer source-loss impact**
  - `GET /api/v1/published-answers/{published_answer_id}/source-loss-impact`

---

## Nota strategica importante — Source Quality Evaluator futuro

La Fase 8.6 si limita a esporre via read API ciò che la pipeline 8.5 scrive sui domini lifecycle e source-loss. È un livello di osservabilità: rende leggibili via HTTP eventi e propagazioni che prima richiedevano accesso diretto al database.

Questa osservabilità è necessaria, ma non basta per completare la visione anti-allucinazione del progetto. Il progetto è evidence-first, ma **“fonte presente” non significa automaticamente “fonte attendibile”**. Una risposta può essere collegata a una fonte reale e tuttavia la fonte può essere debole, obsoleta, commerciale, non indipendente, secondaria, contraddetta da fonti migliori o non sufficientemente rilevante.

La Fase 8.6 **NON** implementa ancora un modulo di valutazione della qualità delle fonti. Non assegna score di credibilità a documenti, chunk, evidence spans o claim. Non distingue tra fonte primaria e secondaria. Non valuta autorevolezza, indipendenza, freschezza, conflitti di interesse o coerenza con fonti esterne. Non introduce alcuna logica di Source Quality Evaluation.

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

Questa fase futura non deve essere confusa con 8.6: **8.6 rende osservabili lifecycle, source-loss e propagation; il Source Quality Evaluator renderà il sistema evidence-quality-aware**. È un prerequisito importante per avvicinare il progetto alla promessa anti-allucinazione completa.

---

## 1. Stato di partenza

La **Fase 8.5 è completata tecnicamente e documentata**.

Elementi già presenti nel progetto:

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

- commit tecnico di completamento 8.5: `03c418693f4eb8ba7019c9785d149dbad83b87fe` (`Add realistic source loss flow test`);
- commit documentale post-8.5: `5f7f4ce7646affed29dfcdcc3943f0669a7f7e5e` (`Update project state after phase 8.5`);
- commit piano 8.6: `4875698ad9c6129653d2f55de61313cefdcc2a0e` (`Add phase 8.6 plan`);
- commit implementazione 8.6A: `e2b54727fa8868360161fde797eed5685e90be27` (`Add published answer lifecycle events endpoint`).

Cosa manca e motiva il resto della 8.6:

- read API per una singola riga `source_loss_events`;
- read API per `source_loss_propagation_records`;
- listing task-level delle source loss collegate a un task;
- realistic read flow test che verifichi via HTTP ciò che oggi i realistic flow osservano soprattutto via DB;
- aggiornamento finale di `PROJECT_STATE.md`.

---

## 2. Obiettivo 8.6

Aggiungere endpoint API read-only che espongano lo stato scritto dalla pipeline 8.5, riusando gli schemi shared già definiti e mantenendo zero side-effect sul DB.

Domini esposti:

- lifecycle events di un `published_answer`;
- source-loss event singolo, per id;
- propagation records di un source-loss event;
- source-loss events visibili da un task;
- opzionale: impact set di source-loss su un singolo `published_answer`.

Risultato atteso: lo smoke test 8.5 diventa eseguibile interamente via HTTP, senza ricorrere a `psql`. Operatori e test di livello superiore possono osservare gli effetti del worker direttamente.

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

## 4. Endpoint read-only proposti

### 4.1 8.6A — `GET /api/v1/published-answers/{published_answer_id}/lifecycle-events`

**Stato:** implementato al commit `e2b54727fa8868360161fde797eed5685e90be27`.

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
- 400 `VALIDATION_ERROR` su query param non validi.

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

**Stato:** da implementare.

Perché serve: permette di leggere una singola riga `source_loss_events` dopo il `POST /api/v1/source-loss-events`.

Tabelle lette:

- `source_loss_events`.

Response shape:

- `SourceLossEventRead`.

Errori:

- 404 `RESOURCE_NOT_FOUND` con `details.resource="source_loss_events"`.

Cosa non deve fare:

- non chiamare `propagate_source_loss`;
- non leggere propagation records;
- non risolvere `task_id` via claim graph se nel DB è `NULL`;
- non modificare DB;
- non usare Redis.

Test previsti:

- happy path;
- 404;
- `task_id=null`;
- `event_payload={}`;
- read-only invariant.

---

### 4.3 8.6C — `GET /api/v1/source-loss-events/{source_loss_event_id}/propagation`

**Stato:** da implementare.

Perché serve: dato un source-loss event, espone ciò che il propagator ha registrato in `source_loss_propagation_records`.

Tabelle lette:

- `source_loss_events` per check di esistenza;
- `source_loss_propagation_records`.

Query params:

- `limit`: default 500, min 1, max 5000;
- `propagation_kind`: opzionale;
- `status`: opzionale.

Errori:

- 404 `RESOURCE_NOT_FOUND` con `details.resource="source_loss_events"`;
- 400 `VALIDATION_ERROR` su query param non validi.

Cosa non deve fare:

- non chiamare `propagate_source_loss`;
- non scrivere propagation records;
- non collassare recorded/skipped/failed;
- non escludere righe `failed`.

---

### 4.4 8.6D — `GET /api/v1/tasks/{task_id}/source-loss-events`

**Stato:** da implementare.

Perché serve: fornisce una vista task-centric delle source loss. Poiché `source_loss_events.task_id` può essere `NULL` by design, la query deve considerare due insiemi:

- S1: source-loss events già task-scoped (`source_loss_events.task_id = :task_id`);
- S2: source-loss events collegate al task tramite `claim_evidence_links → logical_claims.task_id`.

L’endpoint ritorna `S1 ∪ S2`, distinct per `source_loss_events.id`.

Tabelle lette:

- `task_masters`;
- `source_loss_events`;
- `claim_evidence_links`;
- `logical_claims`.

Query params:

- `limit`: default 200, min 1, max 2000.

Errori:

- 404 `RESOURCE_NOT_FOUND` con `details.resource="task_masters"`.

Cosa non deve fare:

- non chiamare il propagator;
- non mutare claim ledger;
- non camuffare `task_id=NULL` sulla SLE;
- non cercare lo stato v(N+1) del claim.

---

### 4.5 8.6 Stretch — `GET /api/v1/published-answers/{published_answer_id}/source-loss-impact`

**Stato:** opzionale.

Perché serve: dato un `published_answer`, mostra i source-loss events che lo hanno impattato via propagation records `published_answer_impacted`.

Tabelle lette:

- `published_answers`;
- `source_loss_propagation_records`;
- `source_loss_events`.

Cosa non deve fare:

- non withdraw automatico;
- non audit;
- non chiamare `apply_withdrawal`;
- non nascondere impatti storici su PA già withdrawn.

---

## 5. Schema response proposto

Schemi shared esistenti da riusare invariati:

- `PublishedAnswerLifecycleEventRead`;
- `SourceLossEventRead`;
- `SourceLossPropagationRecordRead`.

Wrapper locali preferiti:

- lifecycle list wrapper;
- propagation list wrapper;
- task-level source-loss item con `impacted_via`;
- stretch impact wrapper.

Principio: non aggiungere nulla a `packages/shared/evidencefirst_shared/schemas.py` se non strettamente necessario.

---

## 6. Error handling

Pattern esistenti da riusare:

- 404 `RESOURCE_NOT_FOUND`;
- 400 `VALIDATION_ERROR`;
- 500 `INTERNAL_ERROR` solo per eccezioni impreviste.

Resource details:

- `published_answers`;
- `source_loss_events`;
- `task_masters`.

Nessun nuovo `ErrorCode` previsto in 8.6.

---

## 7. Test plan

File di test API previsti o già presenti:

- `apps/api/tests/test_published_answer_lifecycle_events_endpoint.py` — implementato con 8.6A;
- `apps/api/tests/test_source_loss_events_read_endpoint.py`;
- `apps/api/tests/test_source_loss_propagation_endpoint.py`;
- `apps/api/tests/test_task_source_loss_events_endpoint.py`;
- opzionale: `apps/api/tests/test_published_answer_source_loss_impact_endpoint.py`.

Realistic read flow previsto:

- `tests/test_phase_8_6_read_flow.py`.

Casi minimi per ogni endpoint:

- happy path;
- 404;
- lista vuota dove applicabile;
- filtri validi;
- filtri invalidi;
- limit;
- read-only invariant con snapshot pre/post.

---

## 8. Rollout a blocchi

- **8.6A — Lifecycle read endpoint + tests** — completato.
- **8.6B — Source-loss event read endpoint + tests** — prossimo blocco consigliato.
- **8.6C — Source-loss propagation read endpoint + tests**.
- **8.6D — Task-level source-loss listing + tests**.
- **8.6E — Realistic read flow tests + docs update**.
- **8.6 Stretch — Published-answer source-loss impact** — opzionale.

Ogni blocco deve essere autonomo, testato e committato separatamente.

---

## 9. Rischi residui

- `event_payload` e `details` JSONB esposti senza RBAC;
- `source_loss_events.task_id` può essere `NULL`, quindi l’endpoint task-level richiede `S1 ∪ S2`;
- race `POST /api/v1/source-loss-events` → `GET /propagation`: SLE presente ma propagation rows non ancora scritte;
- niente cursor pagination;
- tabelle 8.5 crescono senza retention `0007`;
- `published_answer` creati prima di 8.5 possono non avere lifecycle event `published`;
- realistic flow usa FakeRedis e dispatcher diretto;
- worker loop reale non attraversato dai realistic flow;
- 8.6 espone evidenze e propagazioni, ma non valuta ancora l’attendibilità delle fonti.

Il rischio più importante sul piano anti-allucinazione è l’ultimo: un claim può essere tracciato a una fonte reale, ma 8.6 non stabilisce se quella fonte sia primaria, autorevole, indipendente, aggiornata o coerente con altre fonti. Questo resta un debito centrale da affrontare con un futuro Source Quality Evaluator.

---

## 10. Criteri di completamento

La Fase 8.6 minima è completa quando:

1. 8.6A, 8.6B, 8.6C e 8.6D sono implementati e registrati in `apps/api/app/main.py`.
2. I test API dedicati passano.
3. Il realistic read flow `tests/test_phase_8_6_read_flow.py` passa.
4. `make test-api`, `make test-db` e `make test` passano.
5. Ogni endpoint è coperto da read-only invariant.
6. `PROJECT_STATE.md` viene aggiornato a fine fase.
7. Nessuna migration, nessun worker e nessun test 8.5 esistente viene modificato involontariamente.
8. Gli endpoint restano read-only.

---

## 11. Decisione documentale

- `PHASE_8_6_PLAN.md` resta il documento di piano/stato della fase 8.6.
- `PROJECT_STATE.md` non va aggiornato per ogni blocco intermedio, salvo decisione esplicita.
- `PROJECT_STATE.md` si aggiornerà in 8.6E, quando gli endpoint minimi saranno implementati e testati.
- `README.md` si aggiornerà solo se si decide di includere uno smoke test HTTP leggibile dall’utente.
- La nota sul Source Quality Evaluator resta strategica: non rappresenta una feature implementata.

---

FILE_COMPLETATI
- PHASE_8_6_PLAN.md

FILE_DA_NON_TOCCARE_ORA
- codice applicativo non coinvolto dal blocco corrente
- tests non coinvolti dal blocco corrente
- migrations
- PROJECT_STATE.md
- README.md

RISCHI_RESIDUI
- `event_payload` e `details` JSONB esposti senza RBAC.
- `source_loss_events.task_id` può essere NULL.
- Race tra source-loss producer e propagation read.
- Niente cursor pagination.
- Nessuna retention 0007.
- Realistic flow con FakeRedis.
- Worker loop reale non attraversato dai realistic flow.
- Nessun Source Quality Evaluator implementato in 8.6.
