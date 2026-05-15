# PHASE_8_7H_PRE — Realistic flow + chiusura formale fase 8.7

Documento **decisionale e di piano** per il blocco 8.7H. Questo blocco non
scrive codice applicativo, non modifica `final_answer_gate.py`, non modifica
`source_quality_orchestrator.py`, non modifica `source_quality_evaluator.py`,
non modifica le migration `0007_source_quality.sql` / `0008_coverage_gap_source_quality.sql`,
non modifica le read API 8.7F, non tocca `task_created.py`. L'unica modifica
documentale ammessa in 8.7H-CODE (blocco successivo) sarà
`PROJECT_STATE.md` / `PHASE_8_7_PLAN.md` / `README.md` per registrare la
chiusura della fase 8.7.

L'obiettivo del documento è fissare, **prima** di scrivere il test, la
struttura esatta del realistic flow 8.7H, i seed manuali necessari per
attivare i branch oggi non raggiungibili con il mock, gli endpoint
chiamati, le asserzioni minime, e i rischi residui specifici al test
end-to-end.

**Commit di partenza implicito**: stato post-8.7G-DOC al main attuale
`1c0aaaf653c2cf0d5b1ece9a146bc3a57e1137bc` ("Update project state after
phase 8.7G"), con stato tecnico 8.7G al commit
`79815764cd8c588556b81c5914b61deb16eb7370` ("Use source quality in final
answer gate").

---

## 1. Stato di partenza post-8.7G

Tutto ciò che segue è leggibile direttamente dal repo (vedi i file
elencati nel prompt operativo) e non aggiunge nulla di nuovo. È
riassunto qui solo per fissare il contesto del realistic flow 8.7H.

### 1.1 Schema DB

- Migration `0001..0008` applicate, immutabili. In particolare:
  - `0007_source_quality.sql`: tabella `source_quality_assessments`
    append-only, nove CHECK enum sui codomini di qualità, CHECK
    `sqa_target_xor`, sei partial unique index (tre versioning + tre
    idempotency, uno per target kind), trigger
    `source_quality_assessments_append_only` su
    `reject_modify_append_only`;
  - `0008_coverage_gap_source_quality.sql`: estensione robusta del
    CHECK su `coverage_gap_statements.kind` da quattro a sei valori
    (`unverified_claim`, `missing_evidence`, `out_of_scope`,
    `source_loss`, `source_quality_block`, `source_quality_warning`).

### 1.2 Pipeline 8.7E (integrazione worker)

Lo step Source Quality gira dentro `_run_8_3_extract_and_verify` in
`apps/worker/app/consumers/task_created.py`, tra `task.analyzed_partial`
e `task.compiling`, SAVEPOINT-protected, ed emette un singolo audit
aggregato `task.source_quality_assessed` con `status='completed'` o
`status='failed'`. Sui resume da `compiling` o `analyzed_partial` lo
step non viene re-eseguito.

### 1.3 Mock evaluator (stato corrente)

Il mock `source_quality_evaluator.py` scrive sempre, per ogni evidence_span:
- `overall_quality='unknown'`;
- `contradiction_status='unchecked'`;
- `confidence=0.5`;
- altre dimensioni fissate dalla policy mock `mvp0_mock_source_quality` v0.1.0.

Conseguenza: con il mock attuale, un task verified-backed entra
**sempre** nel Branch B' del Gate (`reason_code='all_spans_verified_with_warnings'`),
non nel Branch C' (`source_quality_block`).

### 1.4 Final Answer Gate 8.7G

Il Gate consulta `source_quality_assessments` come terzo asse decisionale
(read-only), con policy P1+P3+P4:
- Block: `overall_quality='unsuitable'`, OR
  `contradiction_status ∈ {contradicted_by_stronger_source, conflicting_sources}`;
- Warning: `overall_quality ∈ {weak, unknown}`, OR
  `contradiction_status='unchecked'`, OR latest mancante;
- Clean: `overall_quality ∈ {strong, adequate}` AND
  `contradiction_status='no_known_contradiction'`.

Aggregazione tra evidence_span dello stesso span: worst-on-block,
any-on-warn. Priorità CVE-lite > Source Quality: uno span non
verified-backed produce sempre `unverified_spans_present`,
indipendentemente dalla qualità delle fonti.

### 1.5 Read API 8.7F

- `GET /api/v1/evidence-spans/{evidence_span_id}/source-quality` → lista
  assessment per evidence_span con `latest_assessment` (nello slice
  restituito);
- `GET /api/v1/tasks/{task_id}/source-quality` → lista per evidence_span
  collegati al task + summary con `latest_overall_quality_counts`.

Entrambi read-only end-to-end, JSONB `payload` verbatim.

### 1.6 Test esistenti rilevanti

- `apps/worker/tests/test_task_created_source_quality_step.py` (4
  scenari): emissione audit, popolamento `source_quality_assessments`,
  resume da `compiling`, failure SAVEPOINT.
- `apps/worker/tests/test_final_answer_gate_source_quality.py` (13
  scenari): warning/block path, latest-wins, multi-evidence, priorità
  CVE-lite, idempotenza.
- `apps/api/tests/test_source_quality_read_endpoint.py` (10 scenari):
  read API 8.7F.
- `tests/test_phase_8_5_source_loss_flow.py` e
  `tests/test_phase_8_6_read_flow.py`: realistic flow per 8.5 e 8.6 —
  **template di riferimento** per il flow 8.7H.

---

## 2. Problema da risolvere in 8.7H

I 27 test 8.7E/F/G coprono **unitariamente** ogni branch della policy
Source Quality, ma nessun test end-to-end fa:

1. Un task creato dall'API HTTP con un documento reale uploadato;
2. La pipeline `task.created` completa (extractor → CVE-lite →
   analyzed_partial → 8.7E source quality → compiling → compiler → Gate
   con consultazione Source Quality → published o publication_held);
3. Le read API 8.7F interrogate dopo che la pipeline ha scritto
   `source_quality_assessments`;
4. L'endpoint `/final-gate-report` interrogato dopo che il Gate ha
   emesso `coverage_gap_statements` di kind `source_quality_warning`
   (warning path) o `source_quality_block` (block path).

Inoltre, due branch architetturali della 8.7G **non sono mai attivati
da un test end-to-end realistico**:

- **Branch B'** (`all_spans_verified_with_warnings`): è raggiungibile
  con il mock attuale, ma nessun realistic flow oggi lo verifica
  end-to-end attraverso l'API HTTP.
- **Branch C'** (`source_quality_block`): non è raggiungibile con il
  mock. Il test 8.7G `test_unsuitable_quality_produces_block_rejects_task`
  lo verifica solo a livello di unit del Gate, con seed diretto di
  `source_quality_assessments`.

Il realistic flow 8.7H deve coprire entrambi i branch via API HTTP +
worker reali (con seed manuale per il block).

---

## 3. Scelta del tipo di test 8.7H

Tre opzioni considerate:

### Opzione A — Realistic flow file singolo, due test in un file

`tests/test_phase_8_7_source_quality_flow.py` con **due funzioni di
test** indipendenti:

- `test_phase_8_7_source_quality_warning_flow`: attiva Branch B' via
  pipeline completa con mock standard;
- `test_phase_8_7_source_quality_block_flow`: attiva Branch C' via
  pipeline completa + seed manuale di una riga
  `source_quality_assessments` v2 con `overall_quality='unsuitable'`
  iniettata DOPO che la pipeline ha scritto la v1 del mock (rispetta
  l'invariante append-only e il partial unique
  `sqa_evidence_version_uq`).

**Pro**:
- Stesso pattern dei file 8.5/8.6 realistic flow (`tests/test_phase_8_5_*`,
  `tests/test_phase_8_6_read_flow.py`).
- Cross-component: API HTTP + dispatcher worker + servizi worker + DB
  + read API HTTP, in un singolo processo Python.
- Bootstrap worker già consolidato (alias `_wapp`, helper
  `_load_pkg` / `_load_mod` / `_bootstrap_worker`).
- I due test sono indipendenti, ognuno con proprio task e proprio
  scope; nessun coupling cross-test.

**Contro**:
- Il bootstrap del worker via importlib + alias `_wapp` è verboso. Va
  duplicato qui (vincolo "no imports from other test files").
- Il test del block path richiede un seed manuale di
  `source_quality_assessments` dopo la pipeline → due fasi in un
  singolo test (pipeline completa → seed → re-invocazione del Gate).
  Va deciso come re-invocare il Gate (vedi §6).

### Opzione B — Due file separati

`tests/test_phase_8_7_source_quality_warning_flow.py` +
`tests/test_phase_8_7_source_quality_block_flow.py`.

**Pro**:
- Separazione netta dei due scenari.

**Contro**:
- Duplicazione del bootstrap worker e degli helper di seed in due
  file. Aumenta la superficie di manutenzione senza valore.
- Non c'è precedente: i flow 8.5 hanno withdrawal+source-loss in due
  file separati, ma sono pipeline diverse; warning e block 8.7H sono
  due branch della stessa pipeline.

### Opzione C — Estensione di un file esistente

Aggiungere test al 8.5 realistic flow o al 8.6 realistic flow.

**Pro**:
- Riuso del bootstrap worker.

**Contro**:
- Confonde la fase: 8.5 è withdrawal/source-loss, 8.6 è read API
  lifecycle/source-loss. 8.7H è Source Quality. Mescolare riduce la
  navigabilità della test suite.
- Vincolo del prompt: "non importare helper da altri test file".

**Raccomandazione: Opzione A.** Un singolo file
`tests/test_phase_8_7_source_quality_flow.py` con due funzioni di test.
Mirror del pattern usato in 8.5/8.6, vincolo sul "no imports" rispettato.

---

## 4. Architettura del realistic flow 8.7H

### 4.1 Bootstrap (identico a 8.5/8.6 realistic flow)

`sys.path` setup all'import: aggiungere `apps/api`, `packages/shared`,
e ROOT. NON aggiungere `apps/worker` (collisione `app` namespace).

Bootstrap worker via importlib sotto l'alias `_wapp`:
- `_wapp` (package skeleton da `apps/worker/app/__init__.py`);
- `_wapp.consumers`, `_wapp.services` (package skeleton);
- `_wapp.config`, `_wapp.db`;
- Tutti i servizi che i consumer importano: `compiler`, `cve_lite`,
  `extractor`, `final_answer_gate`, `published_answer_lifecycle`,
  `source_loss_propagator`, **`source_quality_evaluator`**,
  **`source_quality_orchestrator`** (questi due ultimi sono **specifici
  a 8.7E** e vanno aggiunti al bootstrap; non sono presenti nei flow
  8.5/8.6);
- Consumer: `task_created`, `published_answer_withdrawal`,
  `source_loss`;
- Modulo finale: `_wapp.consumers.dispatch`.

Co-residenza con i flow 8.5/8.6 nello stesso pytest session: idempotente
via short-circuit su `sys.modules` (pattern già provato).

### 4.2 FakeRedis

Solo `xadd` implementato. Installato via `monkeypatch.setattr` su
`app.routes.tasks` (il routes module che pubblica `task.created` su
Redis dopo il commit DB). NON va patchato su `app.redis`: il routes
module ha catturato `get_redis` al `import` time via
`from ..redis import get_redis`.

Cattura del payload del `xadd` per stream `app.events.task_created` per
poi passarlo a `_dispatch.handle_event(event, redis_consumer_name=...)`.

### 4.3 Seed iniziale

Identico in entrambi i test:
- tenant + user + project FRESHI per ogni invocazione (rerun-safe);
- task NON viene creato direttamente nel DB (a differenza dei flow
  8.5/8.6): vogliamo testare anche il path **API HTTP `POST
  /api/v1/tasks`** + upload documento via
  **`POST /api/v1/projects/{id}/documents`** se ragionevole; oppure
  seed diretto del documento (chain
  blob/object/document/version/chunk/span) e poi creazione del task
  via API HTTP con `document_ids`.

Decisione operativa: il prompt operativo dice "creazione task con
documento". L'upload via HTTP testa anche l'extractor che si attiva
sui contenuti reali. **Si raccomanda** di usare l'API
`POST /api/v1/projects/{id}/documents` (upload reale `.txt`) per
massimizzare la realisticità, seguito da
`POST /api/v1/tasks` con `document_ids=[doc_id]`.

Il documento deve contenere almeno una frase factual con cifre
(es. "Sales grew by 37 percent in Q3.") perché l'extractor mock-driven
seleziona solo frasi con digit; senza digit, niente raw_claim, niente
verified_fact, e il Gate finirebbe in Branch A
(`no_verified_claims`) — non utile per testare 8.7G.

Alternativa più controllata: seed diretto del documento via SQL come
fatto in `test_task_created_source_quality_step.py`
(`_create_doc_with_chunk_and_span`). Più rapido, meno realistico ma
deterministico al 100%.

**Raccomandazione**: API HTTP per l'upload e per la creazione del task,
per testare la coerenza end-to-end. Vedi §10 sui rischi (extractor mock
deve produrre almeno una verified_fact perché il test sia significativo).

### 4.4 Pipeline drive

Dopo la POST del task, il routes module pubblica `task.created` su
FakeRedis. Il test:
- legge `fake.xadd_calls[0]` (un solo xadd atteso);
- ricostruisce l'event dict;
- chiama `_dispatch.handle_event(event, redis_consumer_name='...')`;
- asserisce `rc == "processed"`.

### 4.5 Asserzioni post-pipeline (warning flow)

Dopo `handle_event` con il mock standard:

1. **task_masters.status** = `'published'`.
2. **audit chain**: la sequenza include `task.analyzing`,
   `task.docs_loaded`, `task.claims_extracted`, `task.claims_classified`,
   `task.claims_ledger_initialized`, `task.cve_lite_started`,
   `task.cve_lite_completed`, `task.analyzed_partial`,
   **`task.source_quality_assessed`** (status='completed'), `task.compiling`,
   `task.draft_compiled`, `task.final_gate_started`,
   `task.final_gate_completed`, `task.published`. Verifica
   `verify_task_audit_chain` → ok=True.
3. **source_quality_assessments**: per ogni evidence_span linkato a un
   claim del task, esiste una riga v1 con
   `overall_quality='unknown'`, `contradiction_status='unchecked'`,
   `confidence=0.5`, `evaluator_name='mock_source_quality_evaluator'`.
4. **final_gate_reports**: 1 riga con `decision='approved'`,
   `reason_code='all_spans_verified_with_warnings'`,
   `payload.source_quality_summary` presente con
   `policy_name='mvp0_source_quality_gate_policy'`,
   `policy_version='0.1.0'`.
5. **coverage_gap_statements**: per ogni span verified-backed, una
   riga `kind='source_quality_warning'`, `severity='warn'`,
   `gap_key=f'span:{span_id}:source_quality_warning'`. Nessuna riga
   con `kind='source_quality_block'`, `kind='unverified_claim'`,
   `kind='missing_evidence'`.
6. **published_answers**: 1 riga v1 con `status='published'`,
   `content_hash = sha256(summary_text utf-8)`.

### 4.6 Asserzioni post-pipeline (warning flow) — endpoint HTTP

Dopo (1)-(6) di §4.5, il test interroga via `TestClient(api_app)`:

7. **GET `/api/v1/tasks/{task_id}/final-gate-report`** →
   HTTP 200, `decision='approved'`,
   `reason_code='all_spans_verified_with_warnings'`,
   `coverage_gap_statements` include almeno un elemento di
   `kind='source_quality_warning'`.
8. **GET `/api/v1/tasks/{task_id}/published-answer`** →
   HTTP 200, `status='published'`, `version_no=1`.
9. **GET `/api/v1/tasks/{task_id}/source-quality`** →
   HTTP 200, `items` non vuoto, `summary.evidence_spans_total >= 1`,
   `summary.spans_with_assessment >= 1`,
   `summary.latest_overall_quality_counts.unknown >= 1`.
10. **GET `/api/v1/evidence-spans/{evidence_span_id}/source-quality`**
    per uno degli evidence_span del task: HTTP 200, `items` length ≥ 1,
    `latest_assessment.overall_quality='unknown'`.

### 4.7 Asserzioni invariant (warning flow)

11. **claim_ledger_entries**: non mutato dal Gate (il Gate è read-only
    su questa tabella). Conteggio righe per i logical_claims del task
    invariato pre/post Gate (le v1 verified_fact create dalla pipeline
    CVE-lite restano latest).
12. **task_masters.status** del task non finisce mai in stati
    inesistenti come `source_quality_held` o simili: 8.7G non estende il
    codominio.

---

## 5. Block flow (Branch C') — strategia di seed

Il branch C' non è raggiungibile con il mock. Tre opzioni per
attivarlo:

### Opzione S1 — Monkeypatch dell'evaluator

`monkeypatch.setattr(source_quality_evaluator_module, "_MOCK_OVERALL_QUALITY", "unsuitable")` o
analogo.

**Pro**: nessun seed manuale.
**Contro**: rompe l'invariante "uniform mock writes". Il mock è
usato anche da altri test che girano in parallelo o consecutivamente
nello stesso pytest session — con i flow 8.5/8.6 già caricati. Il
monkeypatch va via tear-down ma è fragile. Scartata.

### Opzione S2 — Seed di una v2 dopo la pipeline

La pipeline `task.created` scrive v1 (`overall_quality='unknown'`).
Subito dopo `handle_event`, il test inserisce una v2 manuale con
`overall_quality='unsuitable'` per UNO degli evidence_span linkati al
task. Poi RI-invoca il Gate sullo stesso task.

**Pro**:
- Rispetta append-only e versionato.
- Usa esattamente il mock che la pipeline scrive di default.
- Il Gate consulta la `latest` (massimo `version_no`), quindi la v2
  prevale (test 8 del file 8.7G unit `test_latest_version_wins_block_after_weak`
  prova esattamente questa proprietà).

**Contro**:
- Re-invocare il Gate richiede di chiamare `run_final_answer_gate(...)`
  direttamente OPPURE re-deliverare un `task.created` event sul
  consumer. Vedi §6 per la decisione.

### Opzione S3 — Skip della pipeline 8.7E e seed manuale di v1 unsuitable PRIMA del Gate

Setup alternativo: il test crea il task ma NON invia mai
`task.created`. Esegue manualmente extract/cve_lite/analyzed_partial
via le funzioni helper interne, salta lo step 8.7E (non chiama
l'orchestrator), inserisce manualmente una riga
`source_quality_assessments` v1 con `overall_quality='unsuitable'`,
poi triggers compiler + Gate manualmente.

**Pro**: setup deterministico.
**Contro**: smonta interamente il valore "realistic flow". Scartata.

**Raccomandazione: S2.**

### 5.1 Come ri-eseguire il Gate dopo il seed di v2

Due strade per ri-eseguire il Gate:

**Strada A — Re-delivery dello stesso `task.created`**:
Il consumer `task_created.py` ha un meccanismo di terminalità che
ritorna `skipped_terminal` per task già `published`. Un task in
`published` con un `final_gate_report` esistente non rifa il Gate. Per
forzare la riesecuzione del Gate sulla nuova v2 di
`source_quality_assessments`, andrebbe **scartato** il
`final_gate_reports` precedente, cosa che viola l'append-only. Strada
NON percorribile.

**Strada B — Chiamata diretta a `run_final_answer_gate` su un draft
nuovo**:
Inserire un nuovo `draft_final_answers` v2 manualmente, poi chiamare
`run_final_answer_gate(conn, ...)`. Il `final_gate_reports` ha UNIQUE su
`draft_final_answer_id`, quindi una nuova invocazione su un draft v2
nuovo emette un report distinto.

**Strada C — Test in DUE task indipendenti**:
- Task 1: warning flow (mock standard). Gate emette Branch B'.
- Task 2: pre-seed di `source_quality_assessments` PRIMA che la
  pipeline arrivi al Gate. Ma il Gate viene invocato dalla pipeline
  DOPO 8.7E, e 8.7E inserisce v1 mock. Quindi anche qui serve una v2.

**Strada D — Seed di v1 = 'unsuitable' iniettata DURANTE la pipeline**:
Patch dell'orchestrator (`run_source_quality_assessment`) con uno
stub che invece di chiamare il mock evaluator inserisce direttamente
una riga `source_quality_assessments` con `overall_quality='unsuitable'`.
Il Gate, alla sua prima invocazione naturale dalla pipeline, leggerà la
v1 con quel valore e produrrà Branch C'.

**Pro**:
- Una sola invocazione del Gate.
- Mantiene la pipeline `task.created` come trigger.
- Audit chain identica a un task post-8.7E reale.
- Nessuna manipolazione post-Gate.

**Contro**:
- Patch dell'orchestrator vs patch dell'evaluator: stessa categoria di
  intrusione, ma su un'unità più piccola (solo l'orchestrator del task
  in test).
- Il monkeypatch va su `_wapp.consumers.task_created.run_source_quality_assessment`
  (il consumer importa il simbolo a module load time), analogamente
  a quanto fa il test 4 di
  `test_task_created_source_quality_step.py` per simulare il failure
  path.

**Raccomandazione finale**: combinazione di **Strada D** (patch
dell'orchestrator per inserire `overall_quality='unsuitable'` come v1)
nel block flow. Il warning flow non richiede alcun patch.

### 5.2 Stub orchestrator per il block flow

Lo stub deve:
1. Risolvere `tenant_id`, `project_id` da `task_masters` (come fa
   l'orchestrator reale).
2. Risolvere gli evidence_span del task come fa
   `_select_distinct_evidence_span_ids`.
3. Per ognuno, INSERT diretto in `source_quality_assessments` con
   `overall_quality='unsuitable'`, `contradiction_status='no_known_contradiction'`,
   `version_no=1`, `idempotency_key` deterministica
   `task:{task_id}:span:{span_id}:test-block-v1`, e tutte le altre
   dimensioni con valori codomain-validi.
4. Ritornare il dict counts atteso dal consumer (`status='completed'`,
   `spans_total=N`, `assessed_count=N`, `already_assessed_count=0`,
   `not_found_count=0`, `invalid_target_count=0`, `error_count=0`).

Lo stub NON deve emettere audit (il consumer lo emette già esterno).

### 5.3 Asserzioni post-pipeline (block flow)

Differenze rispetto al warning flow:

1. **task_masters.status** = `'analyzed_partial'` (NON `'published'`).
2. **audit chain**: come warning flow fino a
   `task.final_gate_completed`, poi `task.publication_held` (non
   `task.published`). `verify_task_audit_chain` → ok=True.
3. **source_quality_assessments**: 1 riga per evidence_span con
   `overall_quality='unsuitable'`.
4. **final_gate_reports**: 1 riga con `decision='rejected'`,
   `reason_code='source_quality_block'`.
5. **coverage_gap_statements**: per ogni span bloccato, una riga
   `kind='source_quality_block'`, `severity='block'`,
   `gap_key=f'span:{span_id}:source_quality_block'`,
   `details.reasons` include `source_quality_unsuitable`. Nessuna riga
   `kind='source_quality_warning'` (clean su contradiction status,
   nessun warning residuo). Nessuna riga `kind='unverified_claim'`.
6. **published_answers**: 0 righe (nessuna pubblicazione).

### 5.4 Asserzioni endpoint HTTP (block flow)

7. **GET `/api/v1/tasks/{task_id}/final-gate-report`** →
   HTTP 200, `decision='rejected'`,
   `reason_code='source_quality_block'`,
   `coverage_gap_statements` include almeno un elemento di
   `kind='source_quality_block'`.
8. **GET `/api/v1/tasks/{task_id}/published-answer`** →
   HTTP 404 con `details.resource='published_answers'`,
   `details.id=task_id`.
9. **GET `/api/v1/tasks/{task_id}/source-quality`** →
   HTTP 200, `summary.latest_overall_quality_counts.unsuitable >= 1`.

### 5.5 Asserzioni invariant (block flow)

10. **claim_ledger_entries** non mutato dal Gate.
11. **task_masters.status** terminale è `'analyzed_partial'`, NON un
    nuovo stato.

---

## 6. Decisione operativa sul re-invocare il Gate

§5.1 conclude: per il block flow, **patch dell'orchestrator** all'INSERT
di `source_quality_assessments` v1 con `overall_quality='unsuitable'`,
poi pipeline normale che invoca il Gate **una sola volta**. Il Gate
trova la latest = unsuitable, entra in Branch C', emette
`final_gate_reports` rejected, `coverage_gap_statements`
source_quality_block, NON inserisce `published_answers`.

Nessuna seconda invocazione del Gate, nessun draft v2, nessuna
violazione dell'append-only.

---

## 7. Co-residenza con altri flow nello stesso pytest session

Il bootstrap worker via alias `_wapp` è idempotente: la funzione
`_bootstrap_worker()` controlla `sys.modules` per
`_wapp.consumers.dispatch` e short-circuita se già presente. Tre
realistic flow possono girare nello stesso pytest session:

- `tests/test_phase_8_5_withdrawal_flow.py` (esistente);
- `tests/test_phase_8_5_source_loss_flow.py` (esistente);
- `tests/test_phase_8_6_read_flow.py` (esistente);
- **`tests/test_phase_8_7_source_quality_flow.py` (nuovo, 8.7H)**.

Il bootstrap 8.7H aggiunge due servizi al pacchetto `_wapp.services`:
`source_quality_evaluator` e `source_quality_orchestrator`. Vanno
caricati come parte della sequenza `_load_mod` del bootstrap nel file
8.7H. Se uno dei flow 8.5/8.6 ha già caricato la lista parziale dei
servizi senza questi due, il loro `_load_mod` rimane no-op? No:
`_load_mod` esegue lo `spec.loader.exec_module(mod)` solo se l'alias non
è in `sys.modules`. Se la lista 8.5 NON include i due servizi 8.7E, e
8.7H gira DOPO 8.5, gli alias
`_wapp.services.source_quality_evaluator` e
`_wapp.services.source_quality_orchestrator` non esistono ancora →
`_load_mod` li carica correttamente.

**Rischio**: il consumer `task_created` ha
`from ..services.source_quality_orchestrator import run_source_quality_assessment`
come import a module load time. Se 8.5/8.6 ha caricato
`_wapp.consumers.task_created` SENZA aver prima caricato
`_wapp.services.source_quality_orchestrator`, l'import dentro
`task_created` fallisce e la funzione `_bootstrap_worker` solleva.

**Verifica**: il bootstrap in 8.5/8.6 (vedi `tests/test_phase_8_5_source_loss_flow.py`
e `tests/test_phase_8_6_read_flow.py`) **NON** carica
`source_quality_evaluator` né `source_quality_orchestrator`. Questo
significa che, oggi, eseguendo `test_phase_8_5_source_loss_flow.py`
dopo 8.7G, il bootstrap deve essere già rotto.

Apertura: bisogna **verificare** in 8.7H-CODE se i flow 8.5/8.6
attualmente sono passing nel CI post-8.7G. Se sì, una di queste
possibilità è vera:

1. Il bootstrap 8.5/8.6 carica `task_created` dopo aver caricato i due
   servizi 8.7E in modo implicito (ma `_load_mod` è esplicito, quindi
   non possibile). **No.**
2. Il caricamento di `task_created` triggera l'import normale di
   `..services.source_quality_orchestrator` che, NON essendo già in
   sys.modules sotto l'alias `_wapp.services.source_quality_orchestrator`,
   viene cercato come modulo Python normale. Sotto sys.path che NON
   contiene `apps/worker`, l'import fallisce.
3. Ma il bootstrap 8.5 NON aggiunge `apps/worker` a sys.path: aggiunge
   solo `apps/api`, `packages/shared`, ROOT. Quindi
   `_wapp.consumers.task_created` quando viene caricato via importlib
   sotto l'alias `_wapp.consumers.task_created`, le sue relative
   import (`from ..services.source_quality_orchestrator import ...`)
   risolvono come `_wapp.services.source_quality_orchestrator`. Se
   quel modulo NON è in sys.modules sotto quell'alias, viene importato
   tramite il package `_wapp.services` (che ha
   `submodule_search_locations=[apps/worker/app/services]`). L'import
   ricorsivo apre `apps/worker/app/services/source_quality_orchestrator.py`
   come un nuovo modulo, e Python lo registra sotto
   `_wapp.services.source_quality_orchestrator`.

Conclusione: il caricamento del consumer `task_created` triggera in
cascata l'import del servizio mancante via il meccanismo standard di
risoluzione delle sub-package import. **Non c'è errore di bootstrap
in 8.5/8.6 post-8.7G.**

**Implicazione per 8.7H**: nel suo bootstrap, 8.7H può scegliere di:
- (a) aggiungere `source_quality_evaluator` e `source_quality_orchestrator`
  esplicitamente al loop dei servizi (mirror del pattern esplicito);
- (b) lasciare che vengano caricati implicitamente come fanno 8.5/8.6.

**Raccomandazione**: (a). Esplicito è meglio di implicito; il file
documenta meglio le sue dipendenze. E se in 8.5/8.6 oggi il
caricamento implicito funziona, in 8.7H l'esplicito è sicuro
comunque.

---

## 8. Test plan dettagliato

### 8.1 File da creare

- `tests/test_phase_8_7_source_quality_flow.py`

### 8.2 File da modificare

Nessuno in 8.7H. La documentazione (`PROJECT_STATE.md`,
`PHASE_8_7_PLAN.md`, `README.md`) andrebbe aggiornata in
8.7H-DOC (sotto-blocco di 8.7H-CODE) per registrare la chiusura della
fase 8.7. Vedi §11.

### 8.3 Struttura del file

```text
tests/test_phase_8_7_source_quality_flow.py

  Header docstring (scope, vincoli, non-obiettivi, riferimento ai
  pre/code blocks 8.7G e a PHASE_8_7_PLAN.md).

  sys.path bootstrap (identico ai flow 8.5/8.6).
  Import API (app.db, app.main, app.routes.tasks, app.routes.documents,
  app.routes.projects).
  Import shared (verify_task_audit_chain).

  Alias `_wapp` bootstrap:
    - _load_pkg _wapp, _wapp.consumers, _wapp.services
    - _load_mod _wapp.config, _wapp.db
    - _load_mod TUTTI i servizi worker incluso
      source_quality_evaluator e source_quality_orchestrator
    - _load_mod TUTTI i consumer (task_created, ...)
    - _load_mod _wapp.consumers.dispatch
    - return dispatch module

  _dispatch = _bootstrap_worker()

  Constants:
    TASK_CREATED_STREAM = "app.events.task_created"
    TASK_CREATED_EVENT_TYPE = "task.created"
    WORKER_CONSUMER_NAME = "task_created" (per inspection EPR)

  Environment guard:
    _skip_if_db_unreachable()

  Helpers locali (NO imports da altri test file):
    - _unique_hex
    - _normalize_jsonb
    - _seeded_dev (tenant + user, ritorna ids)
    - _create_project_via_api(client) -> project_id
    - _upload_document_via_api(client, project_id, content) -> doc_id
    - _create_task_via_api(client, project_id, document_ids) -> task_id
    - _fetch_task_status(conn, task_id)
    - _count_audit_event(conn, task_id, event_type)
    - _fetch_audit_event_types(conn, task_id) -> list[(seq, etype)]
    - _fetch_final_gate_report(conn, task_id) -> dict | None
    - _fetch_coverage_gaps(conn, draft_id) -> list[dict]
    - _fetch_published(conn, task_id) -> dict | None
    - _fetch_sqa_for_task(conn, task_id) -> list[dict]
    - _distinct_evidence_span_ids_for_task(conn, task_id) -> set[UUID]
    - _gaps_by_kind(gaps, kind) -> list[dict]
    - _reason_codes_in_gap(gap) -> list[str]

  FakeRedis class (xadd only).

  ============================================================
  Test 1: test_phase_8_7_source_quality_warning_flow_end_to_end
  ============================================================
  Skip if DB unreachable.

  Seed: tenant + user via DB direct INSERT.

  Install FakeRedis on the tasks route module.

  Create project + upload doc + create task via TestClient HTTP calls.

  Assert FakeRedis observed exactly one xadd on TASK_CREATED_STREAM.

  Reconstruct event from xadd fields.

  _dispatch.handle_event(event, redis_consumer_name='realistic_8_7h_warning')
  -> assert "processed"

  DB assertions (warning flow):
    - task status = 'published'
    - audit chain includes task.source_quality_assessed BETWEEN
      task.analyzed_partial and task.compiling
    - audit chain includes task.published as terminal
    - verify_task_audit_chain ok=True
    - source_quality_assessments has at least 1 row per evidence_span
      linked to the task, with overall_quality='unknown'
    - final_gate_reports row with reason_code='all_spans_verified_with_warnings'
    - coverage_gap_statements with kind='source_quality_warning' for
      each verified-backed span; details.reasons includes
      'source_quality_unknown' and 'source_quality_contradiction_unchecked'
    - NO kind='source_quality_block'
    - NO kind='unverified_claim'
    - published_answers v1 exists with status='published'

  HTTP assertions:
    - GET /tasks/{id}/final-gate-report -> 200, reason_code match
    - GET /tasks/{id}/published-answer -> 200, status='published'
    - GET /tasks/{id}/source-quality -> 200,
      summary.latest_overall_quality_counts.unknown >= 1
    - GET /evidence-spans/{es_id}/source-quality (for one es of the
      task) -> 200, latest_assessment.overall_quality='unknown'

  Invariant assertions:
    - claim_ledger_entries count unchanged before/after Gate
      (read the latest entry per logical_claim and assert state stays
       'verified_fact')
    - task_masters.status terminale ∈ {'published'}

  ============================================================
  Test 2: test_phase_8_7_source_quality_block_flow_end_to_end
  ============================================================
  Skip if DB unreachable.

  Same seed pattern.

  Install FakeRedis.

  Monkeypatch _wapp.consumers.task_created.run_source_quality_assessment
  with a stub that:
    - resolves task scope from task_masters
    - resolves distinct evidence_span_ids for the task
    - for each, INSERTs source_quality_assessments v1 with
      overall_quality='unsuitable', contradiction_status='no_known_contradiction',
      idempotency_key=f'task:{task_id}:span:{span_id}:test-block-v1'
    - returns counts dict with status='completed', spans_total=N,
      assessed_count=N

  Create project + upload doc + create task via TestClient HTTP.

  Drive dispatcher.

  DB assertions (block flow):
    - task status = 'analyzed_partial' (terminal for rejected)
    - audit chain includes task.source_quality_assessed (status='completed')
    - audit chain includes task.publication_held as last event
    - NOT task.published
    - verify_task_audit_chain ok=True
    - source_quality_assessments has rows with
      overall_quality='unsuitable'
    - final_gate_reports row with reason_code='source_quality_block',
      decision='rejected'
    - coverage_gap_statements with kind='source_quality_block' for at
      least one span; details.reasons includes
      'source_quality_unsuitable'
    - NO source_quality_warning gap (clean on contradiction)
    - published_answers count for task = 0

  HTTP assertions:
    - GET /tasks/{id}/final-gate-report -> 200,
      reason_code='source_quality_block', decision='rejected'
    - GET /tasks/{id}/published-answer -> 404 RESOURCE_NOT_FOUND with
      details.resource='published_answers'
    - GET /tasks/{id}/source-quality -> 200,
      summary.latest_overall_quality_counts.unsuitable >= 1

  Invariant assertions:
    - claim_ledger_entries count unchanged before/after Gate
    - task_masters.status terminale = 'analyzed_partial'
    - source_quality_assessments rows have evaluator_name from the
      stub (NOT 'mock_source_quality_evaluator') — sanity check the
      stub was actually invoked instead of the real mock
```

### 8.4 Numero di scenari

Due test, secondo il prompt operativo:
- Warning flow end-to-end (Branch B', `all_spans_verified_with_warnings`).
- Block flow end-to-end (Branch C', `source_quality_block`).

Nessun terzo test per "unverified_spans_present priorità CVE-lite" via
realistic flow: questa invariante è già coperta dal test 12 di
`test_final_answer_gate_source_quality.py` (scenario unit). Aggiungerla
qui sarebbe ridondante e non testerebbe pipeline aggiuntiva.

### 8.5 Test ESCLUSI esplicitamente dal blocco 8.7H

- Test che richiedono provider AI reali.
- Test che richiedono web search/RAG.
- Test che richiedono RBAC o redaction dei JSONB.
- Test che richiedono Retention policy (0009_*).
- Test che richiedono backfill di task pre-8.7E.
- Test su trigger append-only su `coverage_gap_statements` (non
  introdotto in 8.7H).
- Test che richiedono Redis reale (FakeRedis basta).
- Test che richiedono il worker main loop reale (chiamata diretta a
  `dispatch.handle_event` basta).

---

## 9. Endpoint chiamati

| Endpoint | Metodo | Quando | Atteso |
|---|---|---|---|
| `/api/v1/projects` | POST | warning + block, setup | 201, project_id |
| `/api/v1/projects/{id}/documents` | POST (multipart) | warning + block, setup | 201, document_id |
| `/api/v1/tasks` | POST | warning + block, trigger | 201, task_id; xadd osservato |
| `/api/v1/tasks/{task_id}/final-gate-report` | GET | warning + block, post-pipeline | 200 con shape attesa |
| `/api/v1/tasks/{task_id}/published-answer` | GET | warning | 200; block: 404 |
| `/api/v1/tasks/{task_id}/source-quality` | GET | warning + block | 200 |
| `/api/v1/evidence-spans/{evidence_span_id}/source-quality` | GET | warning | 200 |

Le route esatte vanno verificate in 8.7H-CODE leggendo
`apps/api/app/routes/*.py`. In particolare, il path per l'upload
multipart e la signature di `POST /api/v1/tasks` (campo `document_ids`)
sono già documentati in PROJECT_STATE.md ed esistenti dalla 8.2.

---

## 10. Rischi residui

### 10.1 Rischi specifici a 8.7H

- **Extractor mock non produce verified_fact**. Se il documento
  uploadato non contiene almeno una frase con cifre, l'extractor
  mock-driven non emette raw_claim, il CVE-lite non passa nulla, il
  Gate entra in Branch A (`no_verified_claims`) — entrambi i test 8.7H
  fallirebbero. **Mitigazione**: il documento di test contiene frasi
  factual con cifre esplicite (es. "Sales grew by 37 percent in Q3.").
  Pattern già usato in `test_task_created_source_quality_step.py`.
- **CVE-lite mock-driven potrebbe non emettere verified_fact se il
  quote_hash non corrisponde**. **Mitigazione**: usare un file uploadato
  via API (no manipolazione del contenuto, no hash mismatch).
- **API HTTP per upload documento**. Se 8.2 cambia la sua signature
  (es. campo `file` rinominato, `multipart/form-data` vs JSON), il test
  va aggiornato. **Mitigazione**: leggere `apps/api/app/routes/documents.py`
  in 8.7H-CODE prima di scrivere il body della POST.
- **Order audit chain dipende dall'implementazione**. Il test 1 di
  `test_task_created_source_quality_step.py` già verifica che
  `task.source_quality_assessed` sia strettamente tra `task.analyzed_partial`
  e `task.compiling`. Il realistic flow lo verifica anche end-to-end.
- **Monkeypatch dell'orchestrator nel block flow**. Il monkeypatch va
  su `_wapp.consumers.task_created.run_source_quality_assessment` (il
  consumer importa la funzione a module load time). **Mitigazione**:
  pattern già provato in `test_task_created_source_quality_step.py`
  test 4 (failure path).
- **Stub orchestrator deve replicare la signature reale**. Il consumer
  chiama `run_source_quality_assessment(conn, task_id=task_id)` e usa
  il return value per il payload dell'audit `task.source_quality_assessed`.
  Lo stub deve restituire un dict con almeno
  `{'status': 'completed', 'spans_total': N, 'assessed_count': N,
  'already_assessed_count': 0, 'not_found_count': 0,
  'invalid_target_count': 0, 'error_count': 0}`. **Mitigazione**:
  copiare la shape da `_empty_counts` in `source_quality_orchestrator.py`.
- **Stub deve usare INSERT bound-params**. Vincolo del prompt operativo
  "Query SQL con bound params". Il stub deve usare
  `text(...) , {params}` come ogni altra parte del codice.
- **Idempotency key dello stub**. Il stub deve usare una key
  deterministica diversa da quella dell'orchestrator reale
  (`task:{task_id}:span:{span_id}:v1`) per non collidere se i due
  scenari vengono incrociati per errore. **Raccomandazione**:
  `task:{task_id}:span:{span_id}:test-block-v1`.
- **N spans potrebbe essere zero in alcuni casi**. Se l'extractor mock
  estrae 0 verified_fact, il consumer non scrive evidence_span linkati
  ai claim e il Gate entra in Branch A. **Mitigazione**: il documento
  uploadato contiene almeno UNA frase con cifre + almeno UNA quote che
  l'extractor sa estrarre.
- **Co-residenza con altri pytest sessions**. Il file `_wapp` namespace
  è singleton-cache: se 8.5/8.6 girano prima di 8.7H, lo short-circuit
  garantisce che il bootstrap 8.7H ottenga la stessa istanza di
  `dispatch`. **Mitigazione**: testato in pratica dal pattern 8.5
  withdrawal+source_loss che già condividono il bootstrap.
- **Costo computazionale**. Due test end-to-end DB-real con upload reale
  + pipeline completa: indicativamente 2-4 secondi per test, dipende
  dalla velocità DB locale. Trascurabile.

### 10.2 Rischi documentali (per 8.7H-DOC, post-CODE)

- **"unknown" continua a non significare "approvato"**. Vincolo da
  martellare in tutti gli aggiornamenti di
  PROJECT_STATE.md / PHASE_8_7_PLAN.md / README.md.
- **Branch C' "non si attiva mai con il mock"**. Il realistic flow lo
  attiva via stub: questo non cambia il fatto che in produzione, con il
  mock attuale, il branch è dormiente. La documentazione 8.7H-DOC
  deve essere esplicita su questo.
- **`conflicting_sources` come block**: non testato dal realistic flow
  (basta `unsuitable` per il block branch). Già coperto dai test unit
  6 e 7 di `test_final_answer_gate_source_quality.py`. Accettato.

### 10.3 Rischi architetturali ereditati (non risolvibili in 8.7H)

- Mock evaluator emette warning universali: rumore strutturale.
- Branch C' non si attiva con mock: necessita evaluator reale o stub.
- Reason code default approved cambiato (`all_spans_verified_with_warnings`):
  consumatori esterni potrebbero rompersi.
- `coverage_gap_statements` senza trigger append-only.
- `conflicting_sources` come block è compromesso vs hold.
- `unsuitable` block permanente per quel draft (no compiler v2 in MVP-0).
- Priorità CVE-lite > Source Quality come invariante fragile a
  refactor (testata, ma da preservare).
- Coesistenza retention: `0009_*` da assegnare.

---

## 11. Cosa NON si fa in 8.7H

- Nessuna modifica a `final_answer_gate.py`.
- Nessuna modifica a `source_quality_orchestrator.py`.
- Nessuna modifica a `source_quality_evaluator.py`.
- Nessuna modifica al consumer `task_created.py`.
- Nessuna modifica alle migration `0007`/`0008`.
- Nessuna nuova migration (no `0009_*`).
- Nessuna modifica alle read API 8.7F.
- Nessuna nuova read API.
- Nessun nuovo stato in `task_masters`, `claim_ledger_entries`.
- Nessun nuovo CHECK su `coverage_gap_statements.kind`.
- Nessun trigger append-only su `coverage_gap_statements`.
- Nessuna RBAC, redaction, backfill, retention.
- Nessun provider AI reale, nessun web search.
- Nessun renderer/export.
- Nessun helper importato da altri test file.

L'unico file di codice/test creato in 8.7H-CODE è
`tests/test_phase_8_7_source_quality_flow.py`. L'unica modifica
documentale (in 8.7H-DOC, sotto-blocco di 8.7H-CODE) è:
- `PROJECT_STATE.md`: marker "Fase 8.7 chiusa", elenco aggiornato test,
  riferimento al nuovo file flow.
- `PHASE_8_7_PLAN.md`: tabella stato blocchi → 8.7H done.
- `README.md`: riga "Fase 8.7H conclusa" nell'header.

---

## 12. Decisione finale

### 12.1 Tipo di test scelto: Opzione A

Un singolo file `tests/test_phase_8_7_source_quality_flow.py` con due
test functions:
- `test_phase_8_7_source_quality_warning_flow_end_to_end`;
- `test_phase_8_7_source_quality_block_flow_end_to_end`.

### 12.2 Strategia di attivazione del Branch C': monkeypatch dell'orchestrator (Strada D)

Patch di `_wapp.consumers.task_created.run_source_quality_assessment`
con uno stub che inserisce `source_quality_assessments` v1 con
`overall_quality='unsuitable'`, lasciando la pipeline `task.created`
normale come trigger del Gate.

### 12.3 Setup task: API HTTP end-to-end

`POST /api/v1/projects`, `POST /api/v1/projects/{id}/documents` (upload
reale `.txt`), `POST /api/v1/tasks`. Il documento contiene frasi
factual con cifre per garantire che l'extractor mock-driven emetta
almeno un verified_fact.

### 12.4 Drive worker via dispatch diretta

`_dispatch.handle_event(event, redis_consumer_name=...)` come nei flow
8.5/8.6, no Redis loop reale.

### 12.5 Endpoint chiamati post-pipeline

Per warning flow: `/final-gate-report`, `/published-answer`,
`/source-quality` (task), `/source-quality` (evidence_span).
Per block flow: `/final-gate-report`, `/published-answer` (404 atteso),
`/source-quality` (task).

### 12.6 File da creare in 8.7H-CODE

- `tests/test_phase_8_7_source_quality_flow.py`.

### 12.7 File da modificare in 8.7H-DOC

- `PROJECT_STATE.md` (chiusura fase 8.7, riga test 8.7H aggiunta).
- `PHASE_8_7_PLAN.md` (tabella stato blocchi).
- `README.md` (riga header "Fase 8.7H conclusa").

---

FILE_COMPLETATI (8.7H-PRE)
- `PHASE_8_7H_PRE.md`

FILE_DA_FARE_PROSSIMO_BLOCCO (8.7H-CODE)
- `tests/test_phase_8_7_source_quality_flow.py` (due test
  end-to-end secondo §4-§5 e §8 di questo documento).
- `PROJECT_STATE.md` aggiornato (8.7H-DOC).
- `PHASE_8_7_PLAN.md` aggiornato (8.7H-DOC).
- `README.md` aggiornato (8.7H-DOC).

RISCHI_RESIDUI
- Extractor mock-driven: il documento di test deve contenere frasi con
  cifre per produrre raw_claim.
- API HTTP per upload `.txt`: la signature di
  `POST /api/v1/projects/{id}/documents` va riletta in 8.7H-CODE.
- Monkeypatch dell'orchestrator nel block flow: pattern già provato in
  `test_task_created_source_quality_step.py` test 4.
- Stub deve restituire la shape esatta del dict counts attesa dal
  consumer per l'audit `task.source_quality_assessed`.
- Branch C' attivato solo via stub: in produzione, con mock attuale,
  Branch C' resta dormiente. Documentazione da preservare in
  8.7H-DOC.
- Co-residenza con flow 8.5/8.6: bootstrap singleton via `_wapp` alias
  garantisce idempotenza.
- "unknown" ≠ approvato: vincolo invariato.
- Costo: 2 test DB-real ~ 2-4s ciascuno.
- Rischi architetturali ereditati da 8.7G (vedi §10.3): tutti
  invariati, nessuno risolto da 8.7H.

---
