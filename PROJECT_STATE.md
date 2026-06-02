# PROJECT_STATE — Evidence-First MVP-0

Documento di onboarding tecnico, una pagina, leggibile dal collaboratore al primo accesso senza dover leggere il codice. Riflette lo stato del repo al commit più recente su `main`: `28cecbe` ("Add multi-agent mock orchestration runner").

**Punto più recente del repository: sotto-fase ORCH-MULTI-A** (bounded multi-agent **mock** orchestration runner). La sotto-fase **8.8B-REPORT** (Anti-Hallucination Report API aggregata, commit `af74187`, "Fix report CVE lineage and add realistic flow") **resta tecnicamente chiusa e invariata**, ma **non è più il punto più recente del repository**.

---

## Cosa è il progetto

Piattaforma multi-AI **evidence-first** ed **evidence-gated**.

Il sistema è progettato per impedire che claim fattuali non supportati, contraddetti o basati su fonti inadeguate vengano pubblicati come affidabili. **Il progetto non promette di eliminare le allucinazioni in senso assoluto**: promette evidenze tracciabili, registrate nel Claim Ledger, verificate dal CVE-lite, valutate sul piano della qualità delle fonti, **verificate anche sull'asse della relazione semantica claim ↔ evidence_span via Claim Entailment**, propagate via lifecycle e source-loss, **e consumate dal Final Answer Gate** prima di qualunque pubblicazione. La piattaforma rende visibili o blocca i claim non supportati, contraddetti o basati su fonti inadeguate prima della pubblicazione affidabile; non garantisce che un LLM non generi internamente output errati.

Nel MVP-0 il nucleo evidence-gated è costruito **prima** della visione multi-AI. Provider AI reali, Verified Web Mode, Hybrid Mode, consensus engine, detector avanzato di contraddizioni e critical reviewer sono fasi future. Il claim "evidence-gated" qui significa: esiste una base append-only verificabile end-to-end per draft/gate/published, una propagazione lifecycle e source-loss minimale per MVP-0, una superficie di osservabilità HTTP read-only sopra di essa, un Source Quality Evaluator deterministico mock (8.7) che scrive assessment append-only sulle fonti che supportano i claim, una policy decisionale (8.7G) che fa consultare quegli assessment al Final Answer Gate per bloccare o segnalare warning su fonti inadeguate, validata end-to-end da un realistic flow test (8.7H), un Claim Entailment Checker (8.8A) deterministico mock che scrive append-only sul piano della relazione semantica claim ↔ quote, consumato dal Final Answer Gate con policy P1 (block solo su `contradicted`), validato end-to-end da un realistic flow test (8.8A-GATE-FLOW), **e ora un'Anti-Hallucination Report API aggregata task-level (8.8B-REPORT) che espone in sola lettura una vista derivata di publication, gate, claims, evidence, CVE-lite, Source Quality, Claim Entailment, coverage gaps, mock indicators e limitations, validata end-to-end da un realistic flow test (8.8B-REPORT-FLOW) che esercita warning path con i mock reali e block path tramite stub dell'orchestrator entailment**.

**Distinzioni semantiche da preservare in tutta la documentazione:**

- **claim correctness ≠ evidence support ≠ CVE-lite verification ≠ source quality ≠ claim entailment ≠ final gate truth.**
- Un link `claim_evidence_links` ben formato non implica supporto semantico.
- CVE-lite (`verification_records`) verifica la presenza testuale della quote nel chunk e l'hash della quote; non valuta se la quote implichi il claim.
- Source Quality (`source_quality_assessments`) valuta la fonte che ospita la quote, non la relazione claim ↔ quote.
- Claim Entailment (`claim_entailment_checks`) verifica se la quote implichi semanticamente (o sia compatibile con) il claim collegato; non giudica la verità del claim nel mondo.
- Il Final Answer Gate compone questi assi nella decisione di pubblicazione; non garantisce verità assoluta.
- **L'Anti-Hallucination Report API (8.8B-REPORT) è una vista read-only derivata.** Aggrega e rende leggibili gli assi sopra. Non introduce nuove decisioni, non ricalcola il Gate, non rivaluta claim né fonti, non muta DB, non sostituisce le tabelle append-only né gli endpoint read specialistici.

Una fonte citata non implica un claim vero. Una quote presente non implica che la quote sostenga il claim. Un verdict `entailed` del checker mock non implica che il claim sia vero nel mondo.

---

## Stato fase

| Fase | Descrizione | Stato |
|---|---|---|
| 8.7 | Source Quality (A–H) | **chiusa** |
| 8.8A | Claim Entailment Checker | **tecnicamente chiusa** (mancano read API claim-entailment dedicata e UI) |
| 8.8B-REPORT | Anti-Hallucination Report API aggregata | **tecnicamente chiusa** (mancano UI, RBAC, lifecycle/source-loss details) |

### Fasi orchestration (mock-only) — più recenti di 8.8B-REPORT

Fasi product/orchestration concluse, tutte **mock-only** (nessun provider AI reale, nessun network I/O):

| Fase | Stato |
|---|---|
| PRODUCT-ORCHESTRATION-PRE | done |
| PRODUCT-ORCHESTRATION-PRE-FIX-A | done |
| ORCH-SCHEMA-PRE | done |
| ORCH-SCHEMA-A | done |
| ORCH-PROVIDER-PRE | done |
| ORCH-PROVIDER-A | done |
| ORCH-RUNNER-PRE | done |
| ORCH-RUNNER-A | done |
| ORCH-MULTI-A-PRE | done |
| ORCH-MULTI-A | **done, commit `28cecbe`** |

ORCH-MULTI-A aggiunge un bounded multi-agent **mock** orchestration runner. È **mock-only**, **non integrata nella pipeline `task.created`**, **non integrata nella UI**, **non pubblica risposte**, e **non esegue il Final Answer Gate**. Vedi sezione "Orchestration foundation (mock-only)" e "ORCH-MULTI-A status" sotto. Dettaglio in `ORCH_MULTI_A_IMPLEMENTATION_REPORT.md`.

### Blocchi 8.8A

| Blocco | Descrizione | Stato |
|---|---|---|
| 8.8A-PRE | Analisi architetturale (`PHASE_8_8A_PRE.md`) | done |
| 8.8A-SCHEMA | `migrations/0009_claim_entailment_checks.sql` + test migration | done |
| 8.8A-SHARED | Estensione `packages/shared/evidencefirst_shared/schemas.py` con `ClaimEntailmentCheckRead` e `SOURCE_ENTAILMENT_VERDICT_VALUES` | done |
| 8.8A-SERVICE | `apps/worker/app/services/claim_entailment_checker.py` + test (mock deterministic checker) | done |
| 8.8A-ORCHESTRATOR | `apps/worker/app/services/claim_entailment_orchestrator.py` + test (task-level fan-out) | done |
| 8.8A-WORKER | Integrazione in `apps/worker/app/consumers/task_created.py` (step SAVEPOINT + audit aggregato) + test | done |
| 8.8A-GATE-PRE | Analisi policy gate (`PHASE_8_8A_GATE_PRE.md`) | done |
| 8.8A-GATE-SCHEMA | `migrations/0010_coverage_gap_entailment.sql` + test migration | done |
| 8.8A-GATE-CODE | Estensione `apps/worker/app/services/final_answer_gate.py` con branch entailment + test (13 scenari) | done |
| 8.8A-GATE-FLOW | `tests/test_phase_8_8a_entailment_gate_flow.py` (warning + block path end-to-end) | done |
| 8.8A-READ-A | `GET /api/v1/tasks/{task_id}/claim-entailment` task-level read endpoint | done (commit `13533ac`) |

### Blocchi 8.8B-REPORT

| Blocco | Descrizione | Stato |
|---|---|---|
| 8.8B-REPORT-PRE | Analisi decisionale (`PHASE_8_8B_REPORT_PRE.md`) | done (commit `13533ac`) |
| 8.8B-REPORT-CODE-A | `apps/api/app/routes/anti_hallucination_report.py` (top-level shape: task/publication/gate/coverage_gaps + mock_indicators + limitations) + 6 test API | done (commit `a49f923` plan, `ce31488` skeleton) |
| 8.8B-REPORT-CODE-B | Aggregazione claims/evidence/CVE-lite/Source Quality/Claim Entailment + axis_summary completo + 6 test API aggiuntivi | done (commit `eaab497`) |
| 8.8B-REPORT-CODE-C-FIX | CVE-lite lineage fix: il report aggrega i `verification_records` di kind `cve_lite` letti sia dalla latest ledger entry sia dal parent superseded via `claim_lineage(relation_kind='supersedes')`. Test regression `test_get_anti_hallucination_report_maps_parent_cve_record_to_latest_entry`. | done (commit `af74187`) |
| 8.8B-REPORT-FLOW | `tests/test_phase_8_8b_report_flow.py` (warning path con mock reali + publication_held entailment_block path via stub orchestrator) | done (commit `af74187`) |

**Cosa resta fuori dalla chiusura tecnica 8.8B-REPORT:** UI dedicata che consumi il report aggregato; RBAC/redaction sui payload JSONB esposti dal report e dagli endpoint read specialistici; sezione lifecycle/source-loss event details aggregata nel report (rinviata, dichiarata in `limitations`); endpoint published-answer-level `GET /api/v1/published-answers/{id}/anti-hallucination-report` (rinviato a v2); shared schema Pydantic `AntiHallucinationReportRead` (oggi wrapper inline nel route module, promozione rinviata se la shape resta stabile dopo le prime integrazioni UI).

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
| `0007_source_quality.sql` | applicata (Fase 8.7B), immutabile |
| `0008_coverage_gap_source_quality.sql` | applicata (Fase 8.7G), immutabile |
| `0009_claim_entailment_checks.sql` | applicata (Fase 8.8A-SCHEMA), immutabile |
| `0010_coverage_gap_entailment.sql` | applicata (Fase 8.8A-GATE-SCHEMA), immutabile |
| `0011_orchestration_schema.sql` | applicata (ORCH-SCHEMA-A), immutabile |
| `0012_*` retention futura | numero da assegnare; retention reale distruttiva ancora non scritta |

**Nota di rinumerazione:** la retention futura distruttiva slitta a `0012_*` o successiva perché `0011_orchestration_schema.sql` è già occupata dalla foundation orchestration. 8.8B-REPORT non ha introdotto migration; ORCH-SCHEMA-A ha introdotto `0011_orchestration_schema.sql`.

---

## Cosa esiste oggi (Fasi 8.4 + 8.5 + 8.6 minima + 8.7A–H + 8.8A + 8.8B-REPORT)

### Base 8.4 (invariata nel comportamento; reason_code esteso in 8.7G e 8.8A-GATE)

- **DB foundation multi-tenant**: `tenants`, `users`, `projects`, `sessions`, `task_masters`, `event_processing_records`, `policy_versions`.
- **Audit chain hash-linked, append-only, verificabile end-to-end** via `verify_audit_chain` / `verify_task_audit_chain`. Append-only enforced a DB tramite trigger comune `reject_modify_append_only`.
- **Storage layer content-addressed, deduplicato, refcount-based**: `storage_blobs`, `storage_objects`. Dedup global concorrenza-safe via `INSERT ... ON CONFLICT DO NOTHING` sull'indice parziale `sb_global_uq`.
- **Document store** con upload reale `.txt`/`.md`, chunking deterministico, `evidence_spans` minimali, `task_documents`. `evidence_spans` append-only.
- **Claim Ledger append-only stretto**: `logical_claims`, `raw_claims`, `classified_claims`, `claim_ledger_entries`, `claim_lineage`, `claim_evidence_links`, `verification_records`. Supersede esclusivamente via `claim_lineage.relation_kind='supersedes'`.
- **Extractor mock-driven**, **CVE-lite mock-driven**, **Compiler mock-driven**, **Final Answer Gate mock-driven (esteso in 8.7G e 8.8A-GATE)**.
- **Worker single-consumer 8.4** per `task.created`, FK-safe, resume-safe, idempotente.
- **Coerenza referenziale stretta a DB** tra `task_masters` ↔ `draft_final_answers` ↔ `final_gate_reports` ↔ `published_answers` via UNIQUE composite e FK composite.

### Fase 8.5 (invariata)

`published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records`, servizi `published_answer_lifecycle` e `source_loss_propagator`, consumer dedicati, due API producer. Vedi commit precedenti per il dettaglio.

### Fase 8.6 minima (invariata)

Quattro endpoint GET read-only di osservabilità su lifecycle e source-loss.

### Fase 8.7 — Source Quality (chiusa)

Tabella `source_quality_assessments` append-only, mock evaluator deterministic che produce sempre `overall_quality='unknown'` + `contradiction_status='unchecked'`, orchestrator chiamato in `task.created` dopo `analyzed_partial` (SAVEPOINT-protected, audit aggregato `task.source_quality_assessed`), due endpoint read 8.7F, Final Answer Gate consuma `source_quality_assessments` con policy P1+P3+P4, validato end-to-end da `tests/test_phase_8_7_source_quality_flow.py`. Branch C' (`source_quality_block`) implementato e testato ma in produzione con mock attuale non si attiva spontaneamente.

### Fase 8.8A — Claim Entailment (tecnicamente chiusa)

Schema (migration `0009_claim_entailment_checks.sql`): tabella `claim_entailment_checks` append-only, granularità per pair `(claim_ledger_entry_id, evidence_span_id)`, codominio `verdict` ∈ {`entailed`, `partially_supported`, `not_supported`, `contradicted`, `uncertain`}, FK composita `cec_entry_logical_consistency`, UNIQUE versioning + idempotency. Mock checker (`claim_entailment_checker.py`, identità `mvp0_mock_entailment_checker` v0.1.0, policy `mvp0_mock_entailment` v0.1.0) deterministic a tre regole: containment match → `entailed` confidence 0.8; numeric mismatch → `not_supported` confidence 0.6; default → `uncertain` confidence 0.5. Il mock NON emette mai `contradicted` né `partially_supported`. Ogni riga porta `payload.mock=true` + `payload.semantic_warning`. Orchestrator task-level (`claim_entailment_orchestrator.py`) fan-out su DISTINCT pair derivate da `claim_evidence_links`. Integrazione in `task.created` consumer come step `_run_8_8_claim_entailment` SAVEPOINT-protected, audit aggregato `task.entailment_checked` tra `task.source_quality_assessed` e `task.compiling` (15 eventi worker-side).

Final Answer Gate (esteso in 8.8A-GATE-CODE) consulta `claim_entailment_checks` read-only con policy `mvp0_entailment_gate_policy` v0.1.0 = P1: `contradicted` blocca; `not_supported`/`partially_supported`/`uncertain`/missing → warning; `entailed` clean. Migration `0010_coverage_gap_entailment.sql` estende `coverage_gap_statements.kind` con `entailment_block` (severity `block`) e `entailment_warning` (severity `warn`). Priorità decisionale Gate post-8.8A-GATE:

1. `no_verified_claims` (zero spans)
2. `unverified_spans_present` (CVE-lite priority)
3. `entailment_block` (8.8A-GATE)
4. `source_quality_block` (8.7G, abbassato di un livello)
5. `approved_with_warnings` (reason_code `all_spans_verified_with_warnings`, semantica estesa)
6. `approved_clean` (reason_code `all_spans_verified`)

Quando entailment_block e source_quality_block fioccano sullo stesso draft, il reason_code è `entailment_block` ma entrambi i kind di gap sono emessi per audit completo. Realistic flow validato da `tests/test_phase_8_8a_entailment_gate_flow.py` (warning + block via stub orchestrator).

Endpoint read claim-entailment task-level: `GET /api/v1/tasks/{task_id}/claim-entailment` (8.8A-READ-A, commit `13533ac`): ordering `(created_at DESC, id DESC)`, limite, 404 `details.resource='task_masters'`, `items=[]` per task esistente senza checks. Endpoint claim-level `GET /api/v1/claims/{logical_id}/entailment-checks` ancora **non esposto** (rinviato a 8.8A-READ-B, valutabile post-UI).

### 8.8B Anti-Hallucination Report API (tecnicamente chiusa)

**Endpoint**:

```
GET /api/v1/tasks/{task_id}/anti-hallucination-report
```

**Scopo.** Esporre una vista task-level read-only aggregata, pensata per UI e audit umano, che renda leggibili in un unico response gli assi anti-allucinazione già persistiti dalle fasi precedenti, normalizzando nomenclatura, decorazioni (`axis` per i coverage gaps), counters di asse e mock indicators. La UI dovrebbe consumare prevalentemente questo endpoint anziché orchestrare letture su molte superfici specialistiche.

**Cosa aggrega.** Per ogni `task_id` esistente:

- **task metadata** (status, objective, mode, timestamps);
- **publication status** derivato deterministicamente (`published`, `withdrawn`, `superseded`, `publication_held`, `not_ready`, `failed`, `unknown` come fallback difensivo);
- **gate decision/reason_code/payload** dal latest `final_gate_reports` per il draft del task, **verbatim**;
- **coverage gaps** del draft con `axis` derivato (`cve_lite`, `source_quality`, `claim_entailment`, `coverage`, `source_loss`, `other`), ordinati severity-first (`block` > `warn` > `info`) poi `created_at ASC`, poi `id ASC`;
- **claims**: un elemento per ogni `logical_claims` del task, con `latest_entry_id` / `latest_state` / `support_scope` dalla latest `claim_ledger_entries`, `evidence_links` strutturali scoped alla latest entry, `cve_lite` records, `source_quality` slot (uno per `evidence_span_id` linkato, con la latest assessment o slot missing), `entailment` slot (uno per pair `(latest_entry, evidence_span)`, con la latest check o slot missing);
- **evidence**: un elemento per ogni `evidence_spans` raggiunto via `task_documents`, in ordering deterministico `(document_id, chunk_index, char_start, evidence id)`;
- **axis_summary**: counters per CVE-lite (`verified_claims_count` / `unverified_claims_count` / `inconclusive_count`), Source Quality (counter per ognuno dei cinque `overall_quality` + `missing_count`), Claim Entailment (counter per ognuno dei cinque verdict + `missing_count`), Final Gate (`has_blocking_gaps`, `has_warnings`, `blocking_gap_count`, `warning_gap_count`);
- **mock_indicators**: quattro flag booleani (`uses_mock_source_quality`, `uses_mock_claim_entailment`, `uses_mock_compiler`, `uses_mock_cve_lite`) derivati da identità servizio + `payload.mock`, con `notes` testuali; in MVP-0 con `PROVIDERS_ENABLED=mock` tutti True;
- **limitations**: lista di disclaimer testuali sempre presente, anche su task "puliti".

**Cosa NON fa.** Il report è **strettamente read-only**:

- non esegue INSERT / UPDATE / DELETE su nessuna tabella;
- non chiama worker, non importa codice worker, non usa Redis;
- non ricalcola la decisione del Final Answer Gate (legge `final_gate_reports.decision` / `reason_code` / `payload` verbatim);
- non rivaluta claim, fonti, entailment, CVE-lite;
- non sostituisce le tabelle append-only né gli endpoint read specialistici (8.4 answers, 8.6, 8.7F, 8.8A-READ-A);
- non implementa RBAC né redaction sui payload JSONB (debito noto, dichiarato in `limitations`);
- non include lifecycle / source-loss event details (rinviati), ma eventuali `coverage_gap_statements` con `kind='source_loss'` possono comunque apparire in `gate.coverage_gaps`;
- non esporrà necessariamente un endpoint published-answer-level in v1 (rinviato a v2).

**Latest semantics.** Il report usa il **latest assoluto DB-level** per target/pair (`ORDER BY version_no DESC, created_at DESC, id DESC`), coerente con la semantica già adottata dal Final Answer Gate. Si distingue dall'endpoint 8.7F (latest in slice) e dall'endpoint 8.8A-READ-A (ordering cronologico globale): l'API-level test del report copre esplicitamente le scelte di latest-per-target e latest-per-pair.

**Comportamento sui casi edge.**

- Task inesistente → **404** `RESOURCE_NOT_FOUND` con `details.resource='task_masters'`.
- Task esistente senza documenti / draft / gate → **200** con campi parziali (`publication.status='not_ready'`, `claims=[]`, `evidence=[]`, counters a 0, mock_indicators in fallback MVP-0, limitations sempre presente).
- Pre-8.7 / pre-8.8A → slot SQ / entailment `null` con `missing_count` incrementato.
- `published_answers.status` ∈ {`withdrawn`, `superseded`} → **non flatten** a `published`: esposti AS-IS in `publication.status` e in `publication.published_answer_status`.

**CVE-lite lineage note.** CVE-lite scrive `verification_records` (`check_kind='cve_lite'`, `check_name='quote_hash_and_substring_v1'`) sulla v1 candidate `claim_ledger_entries`, e su PASS appende una v2 `verified_fact` (o, su FAIL, una v2 `unverifiable`) tracciando la transizione tramite `claim_lineage(relation_kind='supersedes')`. Il report è keyed per la **latest entry** (tipicamente v2), mentre il `verification_records` resta legato a v1: il fix 8.8B-REPORT-CODE-C garantisce che il report aggreghi correttamente i record CVE-lite letti **sia dalla latest entry stessa sia dal parent v1 superseded dalla latest entry**, via JOIN su `claim_lineage` con `relation_kind='supersedes'`. La regression è testata da `test_get_anti_hallucination_report_maps_parent_cve_record_to_latest_entry` in `apps/api/tests/test_anti_hallucination_report_endpoint.py`.

**Test coverage.**

- **API-level** (`apps/api/tests/test_anti_hallucination_report_endpoint.py`, 13 scenari):
  1. 404 per task inesistente con `details.resource='task_masters'`.
  2. Task esistente senza documenti → 200, sezioni vuote, `publication.status='not_ready'`.
  3. Gate rejected + coverage gaps → `publication.status='publication_held'`, gaps con `axis` decorato.
  4. `withdrawn` / `superseded` non flatten.
  5. Read-only snapshot invariant (count pre/post invariato su tutte le tabelle append-only rilevanti).
  6. Severity-first ordering dei coverage gaps (block prima, poi warn in `created_at ASC`).
  7. Full happy path: claim + evidence + CVE-lite + SQ + CE popolati, `axis_summary` coerente.
  8. Latest source_quality version wins.
  9. Latest entailment version wins.
  10. Missing SQ e CE producono null slots e `missing_count` incrementato.
  11. Spans attaccati al task ma non linkati a claim non contano in `missing_count`.
  12. Ordering deterministico di `claims` (created_at ASC) ed `evidence` (document_id ASC, chunk_index ASC, char_start ASC, id ASC).
  13. **CVE-lite lineage regression**: record CVE-lite scritto sulla v1 parent entry, latest entry è una v2 `verified_fact` superseded via `claim_lineage(relation_kind='supersedes')`, il report deve mappare correttamente il record sotto la v2 e contare `verified_claims_count=1`.
- **Realistic flow** (`tests/test_phase_8_8b_report_flow.py`, 2 scenari):
  - **Warning path** con mock reali end-to-end: API → FakeRedis → dispatcher → consumer → servizi worker → DB → GET report. `publication.status='published'`, gate approved con `reason_code` ∈ {`all_spans_verified_with_warnings`, `all_spans_verified`}, claims/evidence non vuoti, axis_summary coerente, `mock_indicators` tutti True.
  - **Publication-held entailment_block path** via monkeypatch del simbolo `_wapp.consumers.task_created.run_claim_entailment_checks` con uno stub che inserisce v1 di `claim_entailment_checks` con `verdict='contradicted'` per ogni pair (necessario perché il mock checker reale non emette `contradicted`). `publication.status='publication_held'`, `published_answer_id=None`, gate rejected con `reason_code='entailment_block'`, coverage gaps include `kind='entailment_block'` severity='block', almeno un `claim.entailment[].verdict=='contradicted'`, `axis_summary.claim_entailment.contradicted_count >= 1`, `axis_summary.final_gate.has_blocking_gaps=True`. `GET /published-answer` → 404 con `details.resource='published_answers'`.

**Risultati riportati prima del commit di chiusura tecnica 8.8B-REPORT (`af74187`):**

- `apps/api/tests/test_anti_hallucination_report_endpoint.py` → 13 passed
- `tests/test_phase_8_8b_report_flow.py` → 2 passed
- `tests/test_phase_8_8a_entailment_gate_flow.py` → 2 passed
- `apps/api` tests → 112 passed, 8 skipped
- root tests → 164 passed

**Prossimo blocco consigliato sul ramo UI:** **UI-PRE** (il ramo orchestration immediato è invece ORCH-MULTI-B-PRE; vedi "Prossimo passo"). Il report aggregato è il primo contratto stabile su cui aprire la fase UI; la UI dovrebbe usarlo come superficie primaria di rendering anti-allucinazione, e gli endpoint read specialistici (8.6, 8.7F, 8.8A-READ-A) come superficie secondaria per il drill-down.

### Orchestration foundation (mock-only) — ORCH-SCHEMA / ORCH-PROVIDER / ORCH-RUNNER / ORCH-MULTI-A

Sopra il nucleo evidence-gated esiste oggi una **orchestration foundation mock-only**, separata dalla pipeline `task.created` e non integrata in essa. Comprende:

- **orchestration schema foundation** (tabelle elencate sotto);
- **mock provider abstraction** (nessun provider AI reale, nessun network I/O);
- **single-agent mock orchestration runner**;
- **bounded multi-agent mock orchestration runner** (ORCH-MULTI-A).

Tabelle dell'orchestration foundation:

- `orchestration_runs`
- `agent_config_snapshots`
- `orchestration_events`
- `orchestration_agent_runs`
- `orchestration_agent_messages`
- `provider_invocations`
- `token_usage_records`
- `orchestration_agent_outputs`
- `source_candidates`
- `token_budgets`

**Semantica `source_candidates`.** I `source_candidates` sono **proposte non verificate** prodotte dall'adapter del provider mock:

- **NON** sono `evidence_spans`;
- **NON** sono `claim_evidence_links`;
- una provider citation / source **non** è evidenza;
- incidono su qualunque cosa downstream **solo dopo** future fasi di source resolution, retrieval, evidence extraction, source verification, claim binding e gate evaluation — nessuna delle quali esiste oggi.

**Semantica provider output.** L'output del provider è **output auditabile dell'agent**:

- **non** è una final answer;
- **non** è una published answer;
- **non** bypassa il Final Answer Gate.

#### ORCH-MULTI-A status

- ORCH-MULTI-A è **mock-only**.
- Esiste un **bounded multi-agent mock runner**; funzione pubblica: `run_multi_agent_mock_orchestration`.
- Test finale documentato nel report: **17 passed**.
- Il **Final Answer Gate NON è eseguito** in questa fase.
- I `final_gate_reports` **non** sono creati in questa fase.
- I `published_answers` **non** sono creati in questa fase.
- Ogni risultato multi-agent della fase mantiene `publication_status = "not_evaluated"` e `gate_report_id = None`.
- ORCH-MULTI-A **non** è integrata nella pipeline `task.created`, **non** è integrata nella UI, **non** pubblica risposte.

---

## Endpoint API attivi

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
| `GET /api/v1/tasks/{id}/final-gate-report` | Gate report + coverage gaps (include `source_quality_*` e `entailment_*`) | 8.4 (esteso 8.7G + 8.8A-GATE) |
| `GET /api/v1/tasks/{id}/published-answer` | Published answer per task | 8.4 |
| `GET /api/v1/published-answers/{id}` | Published answer per id | 8.4 |
| `POST /api/v1/published-answers/{published_answer_id}/withdrawal-requests` | Producer asincrono di withdrawal | 8.5 |
| `POST /api/v1/source-loss-events` | Producer asincrono di source loss | 8.5 |
| `GET /api/v1/published-answers/{published_answer_id}/lifecycle-events` | Read lifecycle events di un published_answer | 8.6A |
| `GET /api/v1/source-loss-events/{source_loss_event_id}` | Read single source_loss_event | 8.6B |
| `GET /api/v1/source-loss-events/{source_loss_event_id}/propagation` | Read propagation records di un source_loss_event | 8.6C |
| `GET /api/v1/tasks/{task_id}/source-loss-events` | Read task-level source-loss listing | 8.6D |
| `GET /api/v1/evidence-spans/{evidence_span_id}/source-quality` | Read source quality assessments per evidence_span | 8.7F |
| `GET /api/v1/tasks/{task_id}/source-quality` | Read task-level source quality summary | 8.7F |
| `GET /api/v1/tasks/{task_id}/claim-entailment` | Read task-level claim_entailment_checks listing | 8.8A-READ-A |
| `GET /api/v1/tasks/{task_id}/anti-hallucination-report` | **Read-only Anti-Hallucination Report API aggregata task-level (task / publication / gate / claims / evidence / CVE-lite / Source Quality / Claim Entailment / axis_summary / mock_indicators / limitations)** | **8.8B-REPORT** |
| `GET /health/live` / `/health/db` / `/health/queue` / `/health/storage` / `/health/ready` | Health checks | 8.1+ |

**Importante — cosa NON esiste ancora:**

- **NON esiste** `GET /api/v1/claims/{logical_id}/entailment-checks` (read API claim-level entailment, rinviata a 8.8A-READ-B).
- **NON esiste** `GET /api/v1/published-answers/{id}/anti-hallucination-report` (variante published-answer-level del report, rinviata a 8.8B-REPORT-CODE v2 se la UI lo richiederà).
- **NON esiste** un validatore citazione-claim (8.8B "storico", distinto da 8.8B-REPORT) né un detector di contraddizioni reale (8.8C) né un Final Answer Sentence Gate (8.8D) né External Verification / Web-RAG (8.9) né multi-agent consensus reale (9.0).

L'endpoint `/final-gate-report` non è stato modificato in 8.8B-REPORT: continua a esporre i `coverage_gap_statements` collegati al draft. Quei gap possono ora avere `kind ∈ {entailment_block, entailment_warning, source_quality_block, source_quality_warning, unverified_claim, missing_evidence, out_of_scope, source_loss}`. Il `payload` del gate report include una sezione `entailment` con counts e identità della policy.

---

## Pipeline 8.4 / 8.7 / 8.8A (validata end-to-end dai realistic flow test)

### Task con documenti, approved scenario (mock attuale → warning flow, 15 eventi worker-side)

1. `task.analyzing`
2. `task.docs_loaded`
3. `task.claims_extracted`
4. `task.claims_classified`
5. `task.claims_ledger_initialized`
6. `task.cve_lite_started`
7. `task.cve_lite_completed`
8. `task.analyzed_partial`
9. `task.source_quality_assessed`
10. `task.entailment_checked` (8.8A)
11. `task.compiling`
12. `task.draft_compiled`
13. `task.final_gate_started`
14. `task.final_gate_completed`
15. `task.published` (oppure `task.publication_held` se rejected)

Con il mock attuale: `reason_code='all_spans_verified_with_warnings'`, gap `source_quality_warning` per span, eventuali gap `entailment_warning` se il mock entailment ha emesso `uncertain`/`not_supported`. `published_answers` v1 inserito.

**8.8B-REPORT non altera la pipeline.** L'endpoint legge le stesse tabelle che la pipeline ha già popolato. Nessun nuovo step, nessun nuovo audit event.

### Task con documenti, rejected zero-verified

Identica fino a `task.final_gate_completed`, poi `task.publication_held`. `reason_code='no_verified_claims'`. Gap `kind='missing_evidence'`. Source Quality NON consultata. Entailment NON consultato (Branch A). Il report espone `publication.status='publication_held'`, `gate.decision='rejected'`, `gate.reason_code='no_verified_claims'`, `claims=[]`/`evidence=[]` se non c'erano documenti o claim estratti.

### Task con documenti, rejected unverified spans

Identica al rejected zero-verified, ma con `reason_code='unverified_spans_present'` e un gap `kind='unverified_claim'` per ogni span scoperto. Source Quality NON consultata. Entailment NON consultato (priorità CVE-lite, Branch C). Il report espone `axis='cve_lite'` sui gap `unverified_claim`.

### Task con documenti, rejected entailment_block (Branch entailment 8.8A-GATE; oggi attivabile solo via stub dell'orchestrator)

Identica al rejected, con `reason_code='entailment_block'` e almeno un gap `kind='entailment_block'`. Eventuali gap `source_quality_*` emessi in parallelo per audit completeness. Implementato e testato end-to-end nel realistic flow 8.8A-GATE-FLOW e nel realistic flow 8.8B-REPORT-FLOW (block path) tramite stub dell'orchestrator entailment. In produzione con il mock attuale non si attiva spontaneamente.

### Task con documenti, rejected source_quality_block (Branch C' 8.7G; oggi attivabile solo via stub dell'orchestrator SQ)

Identica al rejected, con `reason_code='source_quality_block'` e almeno un gap `kind='source_quality_block'`. Reached solo quando nessun entailment block fire (priorità: entailment > source quality). Implementato e testato end-to-end nel realistic flow 8.7H (block path) tramite stub dell'orchestrator SQ.

### Task senza documenti (invariato)

`task.created` (API) → `task.analyzing` → `task.blocked`. Step source quality e step entailment NON vengono eseguiti. Il report espone `publication.status='not_ready'` se non c'è draft, o lo stato derivato corrispondente.

---

## Final Answer Gate — branch e priorità (post-8.8A-GATE)

Uno span è **verified-backed** se e solo se esiste almeno un `final_answer_span_claim_links` tale che:

```
link.claim_ledger_entry_id == latest_entry_id_for(claim_logical_id)
AND latest_entry_state_for(claim_logical_id) == 'verified_fact'
```

Branch decisionali in ordine di priorità:

| Condizione del draft | `decision` | `reason_code` | Coverage gap (kind) | `published_answers` |
|---|---|---|---|---|
| Zero spans | `rejected` | `no_verified_claims` | `missing_evidence`, `gap_key='no_verified_claims'` | assente |
| Almeno uno span non verified-backed (priorità CVE-lite) | `rejected` | `unverified_spans_present` | un `unverified_claim` per ogni span scoperto | assente |
| Tutti verified-backed + almeno uno span con entailment block | `rejected` | `entailment_block` | `entailment_block` per span bloccato + eventuali `entailment_warning` + eventuali `source_quality_*` (audit) | assente |
| Tutti verified-backed + nessun entailment block + almeno uno span con source_quality block | `rejected` | `source_quality_block` | `source_quality_block` per span bloccato + eventuali `source_quality_warning` + eventuali `entailment_warning` (audit) | assente |
| Tutti verified-backed + nessun block + almeno uno warning (entailment OR source quality) | `approved` | `all_spans_verified_with_warnings` | `entailment_warning` e/o `source_quality_warning` per span con warning | v1 con `status='published'` |
| Tutti verified-backed + nessun warning su entrambi gli assi | `approved` | `all_spans_verified` | nessuno | v1 con `status='published'` |

### Convenzione errori

`ErrorCode.NOT_PUBLISHED` non esiste in MVP-0. Per le GET su task esistente non ancora pubblicato si restituisce `RESOURCE_NOT_FOUND` con `details.resource='published_answers'`. Per task inesistente: `details.resource='task_masters'`. Il report 8.8B-REPORT segue la stessa convenzione: 404 con `details.resource='task_masters'`.

---

## Test (post-8.8B-REPORT)

Test plan implementato (in aggiunta a quelli già documentati per 8.4 / 8.5 / 8.6 / 8.7 / 8.8A):

- **Anti-Hallucination Report API**: `apps/api/tests/test_anti_hallucination_report_endpoint.py` (13 scenari, vedi sezione 8.8B sopra).
- **Anti-Hallucination Report realistic flow**: `tests/test_phase_8_8b_report_flow.py` (2 scenari: warning con mock reali, publication_held entailment_block via stub orchestrator).

**Risultati riportati prima del commit di chiusura tecnica 8.8B-REPORT (`af74187`):**

- `apps/api/tests/test_anti_hallucination_report_endpoint.py`: 13 passed.
- `tests/test_phase_8_8b_report_flow.py`: 2 passed.
- `tests/test_phase_8_8a_entailment_gate_flow.py`: 2 passed (regression).
- Root tests: 164 passed.
- `apps/api` tests: 112 passed, 8 skipped.

---

## Cosa è ancora rinviato (non implementato) — debiti tecnici e roadmap

### Anti-Hallucination roadmap (post-chiusura tecnica 8.8B-REPORT)

**Disclaimer.** Il sistema è progettato per impedire che claim fattuali non supportati, contraddetti o basati su fonti inadeguate vengano pubblicati come affidabili. Non elimina le allucinazioni in senso assoluto.

Componenti **ancora mancanti** dopo la chiusura tecnica di 8.8B-REPORT:

- **UI-PRE / UI** — Nessuna interfaccia utente espone ancora il report aggregato né i gap entailment / source_quality. Prossimo blocco consigliato sul ramo UI (il ramo orchestration immediato è ORCH-MULTI-B-PRE).
- **8.8A-READ-B** — Read API claim-level per `claim_entailment_checks` (`GET /api/v1/claims/{logical_id}/entailment-checks`). Backlog; valutabile se la UI di dettaglio claim lo richiede.
- **8.8B-REPORT v2 (published-answer-level)** — Variante `GET /api/v1/published-answers/{id}/anti-hallucination-report`. Rinviata; ricostruibile lato UI da `published_answers.task_id`.
- **8.8B-REPORT-SHARED** — Promozione di `AntiHallucinationReportRead` a Pydantic shared model. Rinviata; valutare se la shape resta stabile dopo le prime due-tre integrazioni UI.
- **8.8B — validatore citazione-claim** (8.8B storico, distinto da 8.8B-REPORT). Verifica che il claim citi le evidenze corrette, non evidenze "vicine" che non lo supportano. **Mancante.**
- **8.8C — detector di contraddizioni reale.** Detector reale di contraddizioni tra claim o tra fonti (oggi il mock entailment non emette `contradicted` spontaneamente). Quando attivato, il Branch entailment_block del Gate si attiverà naturalmente. **Mancante.**
- **8.8D — Final Answer Sentence Gate.** Gate a livello frase del published_answer. **Mancante.**
- **8.9 — External Verification / Web-RAG controllato.** Verifica esterna su fonti web in modalità controllata. **Mancante.**
- **9.0 — Multi-agent consensus + adversarial review reale.** Provider AI reali, consensus engine, critical reviewer adversariale. **Mancante.**

### Orchestration — cosa NON esiste ancora (post ORCH-MULTI-A)

Esiste un **bounded multi-agent mock runner** (ORCH-MULTI-A). **Non** esistono ancora:

- multi-agent reale con provider reali;
- source resolution pass;
- source retrieval reale;
- source verification downstream per l'orchestration;
- evidence extraction da `source_candidates`;
- claim binding da `source_candidates`;
- synthesis / candidate synthesis;
- Final Answer Gate integration per l'orchestration runner;
- publication dall'orchestration runner;
- API / UI orchestration.

### Altri debiti tecnici

- **NLI reale per Claim Entailment.** Il mock entailment è un'euristica deterministica a 3 regole sintattiche e NON è un modello NLI reale. Un futuro checker reale richiederà bump di `policy_version` e probabile re-run del corpus storico.
- **Mock entailment non emette `contradicted` né `partially_supported`.** Branch `entailment_block` dormiente in produzione mock; attivato solo via stub nei test.
- **Backfill claim_entailment_checks per task pre-8.8A-WORKER.** I task processati prima dell'integrazione 8.8A nel consumer non hanno righe in `claim_entailment_checks`. Il report espone slot `entailment` con `latest_check_id=null` e incrementa `missing_count`. Nessun backfill script.
- **Backfill source quality per task pre-8.7E.** Analogo per Source Quality.
- **Recompile/v2 dopo entailment_block o source_quality_block.** Un task bloccato non ha oggi un path applicativo per ritentare con un draft v2 (il compiler emette solo v1). Rejected è terminale per quel task.
- **`coverage_gap_statements` senza trigger append-only.** La tabella non ha un trigger `reject_modify_append_only`. 8.7G/8.8A/8.8B-REPORT rispettano operativamente l'invariante insert-only, ma non c'è enforcement a DB.
- **Retention reale distruttiva** (`0012_*` da scrivere; `0011_orchestration_schema.sql` è già occupata dalla foundation orchestration). Le tabelle 8.5/8.7/8.8A crescono senza pruning.
- **RBAC / redaction** sui payload JSONB esposti dagli endpoint read 8.6/8.7F/8.8A-READ-A e **dal report 8.8B-REPORT** (che compone payload da molti assi in un singolo response body, aggravando il rischio di leak). Dichiarata in `limitations`.
- **Provider AI reali, Verified Web Mode, Hybrid Mode.** MVP-0 gira con `PROVIDERS_ENABLED=mock` e `MAX_COST_PER_TASK=0`.
- **Renderer ed export** Markdown/HTML/PDF/DOCX/JSON-LD.
- **Auth reale.**
- **DLQ esplicita per il worker.**
- **UI completa.** Nessuna UI espone ancora il report aggregato né i nuovi gap `entailment_*` o i gap `source_quality_*`. Prossimo blocco consigliato sul ramo UI (UI-PRE); sul ramo orchestration il prossimo blocco è ORCH-MULTI-B-PRE.
- **OCR / parsing PDF, vector store cloud, storage S3 / GCS / Azure operativo.**
- **Cursor pagination** sugli endpoint read 8.6/8.7F/8.8A-READ-A. Il report 8.8B-REPORT non è paginato in v1 (task-level naturalmente bounded).
- **Worker main loop reale negli end-to-end test.** I realistic flow 8.5/8.6/8.7H/8.8A-GATE-FLOW/8.8B-REPORT-FLOW usano FakeRedis e invocano `dispatch.handle_event` direttamente.
- **Lifecycle / source-loss event details aggregati nel report.** Rinviati; eventuali `coverage_gap_statements` di kind `source_loss` possono comunque apparire in `gate.coverage_gaps`.
- **Calibrazione futura della policy 8.8A-GATE con checker reale.** P2 (`not_supported → block`) è oggi scartata; potrebbe diventare difendibile con un checker reale. La policy è versionata (`mvp0_entailment_gate_policy` v0.1.0) per abilitare un bump futuro tracciabile.
- **"entailed" mock con verità del claim**: confondere i due resta scorretto. Il Gate e il report trattano correttamente gli assi come ortogonali. Una fonte citata non implica un claim vero. Una quote presente non implica che la quote sostenga il claim. Un verdict `entailed` non implica verità del claim nel mondo.
- **Branch `entailment_block`** è implementato e testato end-to-end (via stub orchestrator), ma con il mock checker attuale **non si attiva spontaneamente in produzione**: serve un checker reale che emetta `verdict='contradicted'` (8.8C o successive).
- **Crescita del Final Answer Gate.** Post-8.8A-GATE il file ha sei branch + due LATERAL JOIN. Refactor in modulo policy separato rinviato a blocco dedicato. 8.8B-REPORT non ha modificato il Gate.
- **Crescita del route module del report.** Il file `apps/api/app/routes/anti_hallucination_report.py` ha cresciuto in CODE-B e CODE-C-FIX (lineage CVE-lite). Refactor in helper modules separati valutabile post-UI.
- **`evaluator` NLI reale non esiste**; il mock heuristic non sostituisce un NLI semantico.

---

## Vincoli sempre validi (MVP-0)

- Nessun provider AI reale, nessun riferimento operativo a OpenAI, Anthropic, Google o altri provider esterni nel codice di MVP-0.
- `PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0`.
- Closed Corpus only.
- SQLAlchemy 2.0 Core: `Connection`, non `Engine.execute`. Query SQL con bound params.
- Migration applicate (0001–0010) sono immutabili. Modifiche schema solo via nuove migration.
- Test rerun-safe con UUID/hash/marker unici per invocazione.
- Append-only enforced a DB su `audit_records`, `evidence_spans`, `claim_ledger_entries`, `final_answer_spans`, `final_gate_reports`, `published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records`, `source_quality_assessments`, `claim_entailment_checks`.
- Endpoint API 8.6/8.7F/8.8A-READ-A/8.8B-REPORT read-only.
- Final Answer Gate (8.7G + 8.8A-GATE): consultazione read-only di `source_quality_assessments` e `claim_entailment_checks`; nessuna mutazione su quelle tabelle né su `claim_ledger_entries`/`claim_lineage`.
- Anti-Hallucination Report API (8.8B-REPORT): strettamente read-only su tutte le tabelle; nessuna mutazione, nessun worker call, nessun Redis, nessun ricalcolo del Gate.

---

## Prossimo passo

**Sotto-fase 8.8B-REPORT tecnicamente chiusa al commit `af74187`; punto più recente del repository: ORCH-MULTI-A (commit `28cecbe`).** Esistono due rami complementari, da decidere con prompt dedicato:

- **Ramo orchestration (immediato consigliato) — ORCH-MULTI-B-PRE:** design di un **source resolution pass** per i `source_candidates` proposti dagli agent. Sarà **design-only** e dovrà preservare le invarianti: un `source_candidate` **non** è evidence; una provider citation / source **non** è evidence; il source resolution **non** deve automaticamente rendere publishable alcunché; **nessun Gate** e **nessuna publication** in quel pass.
- **Ramo UI (complementare) — UI-PRE:** resta valido se si decide di tornare alla UI anti-hallucination report. Il report aggregato 8.8B-REPORT è il primo contratto stabile su cui aprire la fase UI: la UI dovrebbe usarlo come superficie primaria di rendering anti-allucinazione (publication, gate, coverage gaps, claims con CVE-lite/SQ/CE, evidence, axis_summary, mock_indicators, limitations), e gli endpoint read specialistici (8.6, 8.7F, 8.8A-READ-A) come superficie secondaria per il drill-down.

Direzioni complementari (sempre da decidere con prompt dedicato):

- **8.8A-READ-B** — Read API claim-level per `claim_entailment_checks` (`GET /api/v1/claims/{logical_id}/entailment-checks`). Backlog; valutabile se la UI di dettaglio claim lo richiede.
- **8.8B-REPORT v2 (published-answer-level)** — Endpoint `GET /api/v1/published-answers/{id}/anti-hallucination-report`. Rinviato.
- **8.8B-REPORT-SHARED** — Promozione di `AntiHallucinationReportRead` a Pydantic shared model. Rinviata.
- **0012_* retention** una volta deciso il perimetro distruttivo.
- **RBAC e redaction** dei JSONB esposti dagli endpoint read 8.6/8.7F/8.8A-READ-A/8.8B-REPORT e dei `details` dei `coverage_gap_statements`.
- **Cursor pagination** sugli endpoint read.
- **Smoke test end-to-end con Redis reale** e worker main loop reale.
- **Trigger append-only** su `coverage_gap_statements`.
- **8.8C — detector di contraddizioni reale** per attivare spontaneamente il Branch `entailment_block` (e popolare `source_quality_assessments.contradiction_status` con valori reali).
