# PROJECT_STATE — Evidence-First MVP-0

Documento di onboarding tecnico, una pagina, leggibile dal collaboratore al primo accesso senza dover leggere il codice. Riflette lo stato del repo al commit **Fase 8.8A** (Claim Entailment Checker tecnicamente chiusa): `394257b141a2109c1aca0ad937ae775bf51bb143` ("Add claim entailment gate realistic flow").

---

## Cosa è il progetto

Piattaforma multi-AI **evidence-first** ed **evidence-gated**.

Il sistema è progettato per impedire che claim fattuali non supportati, contraddetti o basati su fonti inadeguate vengano pubblicati come affidabili. **Il progetto non promette di eliminare le allucinazioni in senso assoluto**: promette evidenze tracciabili, registrate nel Claim Ledger, verificate dal CVE-lite, valutate sul piano della qualità delle fonti, **verificate anche sull'asse della relazione semantica claim ↔ evidence_span via Claim Entailment**, propagate via lifecycle e source-loss, **e consumate dal Final Answer Gate** prima di qualunque pubblicazione. La piattaforma rende visibili o blocca i claim non supportati, contraddetti o basati su fonti inadeguate prima della pubblicazione affidabile; non garantisce che un LLM non generi internamente output errati.

Nel MVP-0 il nucleo evidence-gated è costruito **prima** della visione multi-AI. Provider AI reali, Verified Web Mode, Hybrid Mode, consensus engine, contradiction detector avanzato e critical reviewer sono fasi future. Il claim "evidence-gated" qui significa: esiste una base append-only verificabile end-to-end per draft/gate/published, una propagazione lifecycle e source-loss minimale per MVP-0, una superficie di osservabilità HTTP read-only sopra di essa, un Source Quality Evaluator deterministico mock (8.7) che scrive assessment append-only sulle fonti che supportano i claim, una policy decisionale (8.7G) che fa consultare quegli assessment al Final Answer Gate per bloccare o segnalare warning su fonti inadeguate, validata end-to-end da un realistic flow test (8.7H), **e ora un Claim Entailment Checker (8.8A) deterministico mock che scrive append-only sul piano della relazione semantica claim ↔ quote, consumato dal Final Answer Gate con policy P1 (block solo su `contradicted`), e validato end-to-end da un realistic flow test (8.8A-GATE-FLOW) che esercita sia il warning path con il mock reale sia il block path tramite stub dell'orchestrator**.

**Distinzioni semantiche da preservare in tutta la documentazione:**

- **claim correctness ≠ evidence support ≠ CVE-lite verification ≠ source quality ≠ claim entailment ≠ final gate truth.**
- Un link `claim_evidence_links` ben formato non implica supporto semantico.
- CVE-lite (`verification_records`) verifica la presenza testuale della quote nel chunk e l'hash della quote; non valuta se la quote implichi il claim.
- Source Quality (`source_quality_assessments`) valuta la fonte che ospita la quote, non la relazione claim ↔ quote.
- **Claim Entailment (`claim_entailment_checks`) verifica se la quote implichi semanticamente (o sia compatibile con) il claim collegato**; non giudica la verità del claim nel mondo.
- Il Final Answer Gate compone questi assi nella decisione di pubblicazione; non garantisce verità assoluta.

Una fonte citata non implica un claim vero. Una quote presente non implica che la quote sostenga il claim.

---

## Stato fase

| Fase | Descrizione | Stato |
|---|---|---|
| 8.7 | Source Quality (A–H) | **chiusa** |
| 8.8A | Claim Entailment Checker | **tecnicamente chiusa** (mancano read API, report API, UI) |

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

**Cosa resta fuori dalla chiusura tecnica 8.8A:** read API claim-entailment, Anti-Hallucination Report API aggregata, UI dedicata.

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
| `0009_claim_entailment_checks.sql` | **applicata (Fase 8.8A-SCHEMA), immutabile** |
| `0010_coverage_gap_entailment.sql` | **applicata (Fase 8.8A-GATE-SCHEMA), immutabile** |
| `0011_*` retention futura | numero da assegnare; retention reale distruttiva ancora non scritta |

**Nota di rinumerazione:** la retention futura distruttiva, già rinviata in 8.7G/H e 8.8A-SCHEMA, slitta ora a `0011_*` o successivo. NON è 0009: 0009 è occupato da `claim_entailment_checks` e 0010 da `coverage_gap_entailment`.

---

## Cosa esiste oggi (Fasi 8.4 + 8.5 + 8.6 minima + 8.7A–H + 8.8A)

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

[Sezione invariata: `published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records`, servizi `published_answer_lifecycle` e `source_loss_propagator`, consumer dedicati, due API producer. Vedi commit precedenti per il dettaglio.]

### Fase 8.6 minima (invariata)

[Sezione invariata: quattro endpoint GET read-only di osservabilità su lifecycle e source-loss.]

### Fase 8.7 — Source Quality (chiusa)

[Sezione invariata rispetto al post-8.7H. Tabella `source_quality_assessments` append-only, mock evaluator deterministic che produce sempre `overall_quality='unknown'` + `contradiction_status='unchecked'`, orchestrator chiamato in `task.created` dopo `analyzed_partial` (SAVEPOINT-protected, audit aggregato `task.source_quality_assessed`), due endpoint read 8.7F, Final Answer Gate consuma `source_quality_assessments` con policy P1+P3+P4, validato end-to-end da `tests/test_phase_8_7_source_quality_flow.py`. **Branch C' (`source_quality_block`) implementato e testato ma in produzione con mock attuale non si attiva spontaneamente.**]

### Fase 8.8A — Claim Entailment (tecnicamente chiusa)

**Schema (migration `0009_claim_entailment_checks.sql`).**

- Tabella `claim_entailment_checks` append-only (trigger `claim_entailment_checks_append_only` su `reject_modify_append_only`).
- Granularità: per coppia `(claim_ledger_entry_id, evidence_span_id)`. `claim_logical_id` denormalizzato per FK composita `cec_entry_logical_consistency` verso `claim_ledger_entries(id, claim_logical_id)`.
- CHECK enum su `verdict`: codominio fisso a 5 valori — **`entailed`, `partially_supported`, `not_supported`, `contradicted`, `uncertain`**.
- CHECK su `confidence` in `[0.0, 1.0]` o NULL.
- CHECK `version_no >= 1`.
- UNIQUE `cec_entry_span_version_uq (claim_ledger_entry_id, evidence_span_id, version_no)` per versioning.
- UNIQUE `cec_entry_span_idem_uq (claim_ledger_entry_id, evidence_span_id, idempotency_key)` per idempotenza applicativa.
- Indici di lookup su `task_id`, `claim_logical_id`, `evidence_span_id`, `verdict`.
- FK con `ON DELETE RESTRICT`.

**Shared schema (`packages/shared/evidencefirst_shared/schemas.py`).**

- Tupla `SOURCE_ENTAILMENT_VERDICT_VALUES` che mirror esattamente il CHECK enum di 0009.
- `Literal` alias `ClaimEntailmentVerdict` per consumer con tipizzazione stretta.
- `ClaimEntailmentCheckRead` model usato dai test e (futuro) read API.

**Mock checker (`apps/worker/app/services/claim_entailment_checker.py`).**

- Deterministico, mock-driven. Nessun provider AI, nessuna web search, nessun NLI reale, nessun embedding.
- Identità: `SERVICE_NAME="mvp0_mock_entailment_checker"`, `SERVICE_VERSION="0.1.0"`, `DEFAULT_POLICY_NAME="mvp0_mock_entailment"`, `DEFAULT_POLICY_VERSION="0.1.0"`.
- Tre regole deterministiche, primo match vince:
  1. **Containment normalizzato** (claim ⊆ quote o quote ⊆ claim, lowercase + whitespace collapsed) → `entailed`, `confidence=0.8`.
  2. **Numeric mismatch** (entrambi i testi hanno numeri AND i set differiscono) → `not_supported`, `confidence=0.6`.
  3. **Default** → `uncertain`, `confidence=0.5`.
- **Il mock NON produce mai `contradicted` né `partially_supported`**: questi verdict sono riservati a checker reali futuri o a seed di test fixtures (stub).
- Ogni riga scritta porta `payload.mock=true` e `payload.semantic_warning="mvp0 heuristic; not a real NLI/LLM entailment model"`.
- `version_no` fissato a 1 in MVP-0. Una collisione su `cec_entry_span_version_uq` con idempotency_key diversa → `status='error'`, `error_code='entailment_version_conflict'`. Mai mascherata.
- Idempotenza via SAVEPOINT + recovery SELECT su `IntegrityError`.
- Canonical scope: `tenant_id`/`project_id`/`task_id`/`claim_logical_id` letti dal target row, non dal caller.
- Non emette mai `audit_records`. Non muta `claim_ledger_entries`, `claim_lineage`, `claim_evidence_links`, `verification_records`, `source_quality_assessments`, `final_gate_reports`, `published_answers`, `source_loss_*`, `published_answer_lifecycle_events`.

**Orchestrator (`apps/worker/app/services/claim_entailment_orchestrator.py`).**

- Task-level fan-out: per ogni `(claim_ledger_entry_id, evidence_span_id)` DISTINCT derivato da `claim_evidence_links JOIN logical_claims` sul task, chiama il checker una volta.
- Idempotency key deterministica: **`task:{task_id}:entry:{claim_ledger_entry_id}:span:{evidence_span_id}:v1`**.
- Conta esiti: `pairs_total`, `assessed_count`, `already_assessed_count`, `not_found_count`, `invalid_target_count`, `error_count`.
- Status retornati: `completed` (anche con zero pair) o `not_found` (task inesistente).
- Non emette audit. Non muta `task_masters`.

**Worker integration (`apps/worker/app/consumers/task_created.py`).**

- Nuovo step `_run_8_8_claim_entailment` SAVEPOINT-protected, dopo `_run_8_7_source_quality` e prima del `_advance_to_compiling`.
- Emette un singolo audit aggregato **`task.entailment_checked`** con `status='completed'` o `status='failed'`, payload contiene counts e identità del checker.
- Audit chain con documenti (approved scenario) ora a **15 eventi** (vedi sotto).
- Failure 8.8A NON blocca 8.4: SAVEPOINT rollback + audit `failed`, pipeline prosegue.
- Resume da `compiling` o `analyzed_partial` non re-esegue lo step.

**Final Answer Gate integration (`apps/worker/app/services/final_answer_gate.py`).**

Il Gate consulta `claim_entailment_checks` come asse decisionale, applicato dopo CVE-lite e prima di Source Quality.

Mapping verdict → comportamento (policy MVP-0 P1, identità `mvp0_entailment_gate_policy` v0.1.0):

- `contradicted` → **block**. Reason code `entailment_block`. Gap `coverage_gap_statements.kind='entailment_block'`, severity `block`, `gap_key=f'span:{final_answer_span_id}:entailment_block'`.
- `not_supported` / `partially_supported` / `uncertain` / latest mancante → **warning**. Gap `kind='entailment_warning'`, severity `warn`, `gap_key=f'span:{final_answer_span_id}:entailment_warning'`. Decision invariata.
- `entailed` → **clean** lato entailment (può comunque essere bloccato da Source Quality o downstream).

Aggregazione tra più pair `(entry, span)` di supporto allo stesso `final_answer_span`: **worst-on-block, any-on-warn**.

**Priorità decisionale Gate (post-8.8A-GATE):**

1. `no_verified_claims` (zero spans)
2. `unverified_spans_present` (CVE-lite priority)
3. **`entailment_block`** (8.8A-GATE, nuovo)
4. `source_quality_block` (8.7G, abbassato di un livello)
5. `approved_with_warnings` (reason_code `all_spans_verified_with_warnings`, semantica estesa: warning entailment e/o warning source quality)
6. `approved_clean` (reason_code `all_spans_verified`)

Quando un draft attiva sia `entailment_block` sia `source_quality_block` sugli stessi span, il `reason_code` è `entailment_block` ma **entrambi i kind di gap sono emessi** per completezza audit. Il Gate è read-only su `claim_entailment_checks` (zero mutazioni).

**Realistic flow validato (`tests/test_phase_8_8a_entailment_gate_flow.py`).**

Due test end-to-end indipendenti che esercitano l'intera catena `API HTTP → FakeRedis → dispatcher → task.created consumer → servizi worker → DB → read API`:

- **Warning flow** (`test_phase_8_8a_entailment_warning_flow_end_to_end`): pipeline normale con mock checker reale. Verdict possibili: `entailed` (containment) o `uncertain`/`not_supported` (altre regole). Final Answer Gate approved con `reason_code='all_spans_verified_with_warnings'`. Gap di tipo `entailment_warning` e/o `source_quality_warning`. `published_answers` v1 inserito.

- **Block flow** (`test_phase_8_8a_entailment_block_flow_end_to_end`): monkeypatch del simbolo `_wapp.consumers.task_created.run_claim_entailment_checks` con uno stub che inserisce v1 di `claim_entailment_checks` con `verdict='contradicted'` per ogni pair, e ritorna il dict counts canonico atteso dal consumer. Lo stub è necessario perché **il mock checker reale non emette mai `contradicted`**. Final Answer Gate rejected con `reason_code='entailment_block'`. Gap `entailment_block` severity='block'. `task.publication_held` come evento terminale. Nessun `published_answer`. GET `/published-answer` ritorna 404 RESOURCE_NOT_FOUND con `details.resource='published_answers'`.

**Branch entailment_block in produzione mock-driven.** Il branch è implementato, testato a livello unit (13 scenari di `test_final_answer_gate_entailment.py` con seed diretto) e a livello realistic flow end-to-end (via stub orchestrator). Ma **in produzione con il mock attuale il branch resta dormiente**: il mock non produce `contradicted` spontaneamente. Si attiverà naturalmente con un checker reale (NLI/LLM) o con il Contradiction Detector reale (8.8C).

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
| `GET /api/v1/tasks/{id}/final-gate-report` | Gate report + coverage gaps (include `source_quality_*` e ora `entailment_*`) | 8.4 (esteso 8.7G + 8.8A-GATE) |
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
| `GET /health/live` / `/health/db` / `/health/queue` / `/health/storage` / `/health/ready` | Health checks | 8.1+ |

**Importante — cosa NON esiste ancora:**

- **NON esiste** `GET /api/v1/tasks/{id}/claim-entailment` (read API per claim_entailment_checks). Da implementare in un blocco read API dedicato (provvisoriamente 8.8A-READ).
- **NON esiste** `GET /api/v1/claims/{logical_id}/entailment-checks`.
- **NON esiste** un endpoint Anti-Hallucination Report API aggregato (8.8E, rinviato).
- **NON esiste** un endpoint dedicato per il payload entailment del Final Answer Gate: i dati sono accessibili via `GET /api/v1/tasks/{task_id}/final-gate-report`, che ora può restituire `coverage_gap_statements` con `kind ∈ {entailment_block, entailment_warning}` e un `payload.entailment` summary nel gate report.

L'endpoint `/final-gate-report` non è stato riscritto in 8.8A-GATE: continua a esporre i `coverage_gap_statements` collegati al draft. Quei gap possono ora avere `kind ∈ {entailment_block, entailment_warning, source_quality_block, source_quality_warning, unverified_claim, missing_evidence, out_of_scope, source_loss}`. Il `payload` del gate report include una sezione `entailment` con counts e identità della policy.

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
10. **`task.entailment_checked`** (8.8A, nuovo)
11. `task.compiling`
12. `task.draft_compiled`
13. `task.final_gate_started`
14. `task.final_gate_completed`
15. `task.published` (oppure `task.publication_held` se rejected)

Con il mock attuale: `reason_code='all_spans_verified_with_warnings'`, gap `source_quality_warning` per span, eventuali gap `entailment_warning` se il mock entailment ha emesso `uncertain`/`not_supported`. `published_answers` v1 inserito.

### Task con documenti, rejected zero-verified

Identica fino a `task.final_gate_completed`, poi `task.publication_held`. `reason_code='no_verified_claims'`. Gap `kind='missing_evidence'`. Source Quality NON consultata. Entailment NON consultato (Branch A).

### Task con documenti, rejected unverified spans

Identica al rejected zero-verified, ma con `reason_code='unverified_spans_present'` e un gap `kind='unverified_claim'` per ogni span scoperto. Source Quality NON consultata. Entailment NON consultato (priorità CVE-lite, Branch C).

### Task con documenti, rejected entailment_block (Branch entailment 8.8A-GATE; oggi attivabile solo via stub dell'orchestrator)

Identica al rejected, con `reason_code='entailment_block'` e almeno un gap `kind='entailment_block'`. Eventuali gap `source_quality_*` emessi in parallelo per audit completeness (ma il reason_code resta `entailment_block`). **Implementato e testato end-to-end nel realistic flow 8.8A-GATE-FLOW tramite stub dell'orchestrator**; in produzione con il mock attuale non si attiva spontaneamente. Si attiverà naturalmente con un checker reale che produca `verdict='contradicted'`.

### Task con documenti, rejected source_quality_block (Branch C' 8.7G; oggi attivabile solo via stub dell'orchestrator SQ)

Identica al rejected, con `reason_code='source_quality_block'` e almeno un gap `kind='source_quality_block'`. Reached solo quando nessun entailment block fire (priorità: entailment > source quality). Eventuali gap `entailment_warning` emessi in parallelo.

### Task senza documenti (invariato)

`task.created` (API) → `task.analyzing` → `task.blocked`. Step source quality e step entailment NON vengono eseguiti.

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

`ErrorCode.NOT_PUBLISHED` non esiste in MVP-0. Per le GET su task esistente non ancora pubblicato si restituisce `RESOURCE_NOT_FOUND` con `details.resource='published_answers'`. Per task inesistente: `details.resource='task_masters'`.

---

## Test (post-8.8A-GATE-FLOW)

Test plan implementato:

- **Migration**: `tests/test_migration_0009_claim_entailment_checks.py` e `tests/test_migration_0010_coverage_gap_entailment.py`.
- **Service**: `apps/worker/tests/test_claim_entailment_checker_service.py` (8 scenari principali + defensive coverage).
- **Orchestrator**: `apps/worker/tests/test_claim_entailment_orchestrator.py` (9 scenari).
- **Worker integration**: `apps/worker/tests/test_task_created_entailment_step.py` (4 scenari: audit position, popolamento checks, resume non re-emette, failure SAVEPOINT).
- **Worker pipeline aggregata**: `apps/worker/tests/test_consumer_with_documents.py` aggiornato per la sequenza a **15 eventi** (approved + rejected scenarios).
- **Gate**: `apps/worker/tests/test_final_answer_gate_entailment.py` (13 scenari: contradicted/not_supported/partially_supported/uncertain/missing → policy, priorità CVE-lite > entailment, priorità entailment > source_quality, coesistenza warning, latest version wins, read-only contract, payload entailment).
- **Realistic flow end-to-end** (root): `tests/test_phase_8_8a_entailment_gate_flow.py` (2 test: warning flow con mock reale, block flow via stub orchestrator).

**Risultati riportati:**

- `tests/test_phase_8_8a_entailment_gate_flow.py`: 2 passed.
- Root tests: 162 passed.
- Worker tests: 143 passed.
- Commit: `394257b Add claim entailment gate realistic flow`.

---

## Cosa è ancora rinviato (non implementato) — debiti tecnici e roadmap

### Anti-Hallucination roadmap (post-chiusura tecnica 8.8A)

**Disclaimer.** Il sistema è progettato per impedire che claim fattuali non supportati, contraddetti o basati su fonti inadeguate vengano pubblicati come affidabili. Non elimina le allucinazioni in senso assoluto.

Componenti **ancora mancanti** dopo la chiusura tecnica di 8.8A:

- **8.8A-READ** — Read API per `claim_entailment_checks` (`GET /api/v1/tasks/{id}/claim-entailment` e/o `GET /api/v1/claims/{logical_id}/entailment-checks`). **Mancante.**
- **8.8B — Citation-to-Claim Validator.** Verifica che il claim citi le evidenze corrette, non evidenze "vicine" che non lo supportano. **Mancante.**
- **8.8C — Contradiction Detector reale.** Detector reale di contraddizioni tra claim o tra fonti (oggi il mock entailment non emette `contradicted` spontaneamente). Quando attivato, il Branch entailment_block del Gate si attiverà naturalmente. **Mancante.**
- **8.8D — Final Answer Sentence Gate.** Gate a livello frase del published_answer. **Mancante.**
- **8.8E — Anti-Hallucination Report API.** Endpoint aggregato che espone, per un published_answer, lo stato di tutti gli assi (entailment, citation, contradiction, source quality, source loss). **Mancante.**
- **8.9 — External Verification / Web-RAG controllato.** Verifica esterna su fonti web in modalità controllata. **Mancante.**
- **9.0 — Multi-agent consensus + adversarial review reale.** Provider AI reali, consensus engine, critical reviewer adversariale. **Mancante.**

### Altri debiti tecnici

- **NLI reale per Claim Entailment.** Il mock entailment è un'euristica deterministica a 3 regole sintattiche e NON è un modello NLI reale. Un futuro checker reale richiederà bump di `policy_version` (`mvp0_entailment_gate_policy` → futuro) e probabile re-run del corpus storico.
- **Mock entailment non emette `contradicted` né `partially_supported`.** Branch `entailment_block` dormiente in produzione mock; attivato solo via stub nei test.
- **Backfill claim_entailment_checks per task pre-8.8A-WORKER.** I task processati prima dell'integrazione 8.8A nel consumer non hanno righe in `claim_entailment_checks`. Il Gate per tali task emette warning `entailment_missing_check` su ogni span (senza bloccare). Nessun backfill script.
- **Backfill source quality per task pre-8.7E.** Analogo per Source Quality.
- **Recompile/v2 dopo entailment_block o source_quality_block.** Un task bloccato non ha oggi un path applicativo per ritentare con un draft v2 (il compiler emette solo v1). Rejected è terminale per quel task.
- **`coverage_gap_statements` senza trigger append-only.** La tabella non ha un trigger `reject_modify_append_only`. 8.7G/8.8A rispettano operativamente l'invariante insert-only, ma non c'è enforcement a DB.
- **Retention reale distruttiva** (`0011_*` da scrivere). Le tabelle 8.5/8.7/8.8A crescono senza pruning.
- **RBAC / redaction** sui payload JSONB esposti dagli endpoint read 8.6/8.7F e sulle `details` dei `coverage_gap_statements` (incluse le nuove `entailment_*`).
- **Provider AI reali, Verified Web Mode, Hybrid Mode.** MVP-0 gira con `PROVIDERS_ENABLED=mock` e `MAX_COST_PER_TASK=0`.
- **Renderer ed export** Markdown/HTML/PDF/DOCX/JSON-LD.
- **Auth reale.**
- **DLQ esplicita per il worker.**
- **UI completa.** Nessuna UI espone ancora i nuovi gap `entailment_*` né i gap `source_quality_*`.
- **OCR / parsing PDF, vector store cloud, storage S3 / GCS / Azure operativo.**
- **Cursor pagination** sugli endpoint read 8.6/8.7F (e futuri 8.8A-READ).
- **Worker main loop reale negli end-to-end test.** I realistic flow 8.5/8.6/8.7H/8.8A-GATE-FLOW usano FakeRedis e invocano `dispatch.handle_event` direttamente.
- **Calibrazione futura della policy 8.8A-GATE con checker reale.** P2 (`not_supported → block`) è oggi scartata; potrebbe diventare difendibile con un checker reale + P5 (disclosure). La policy è versionata (`mvp0_entailment_gate_policy` v0.1.0) per abilitare un bump futuro tracciabile.
- **Confondere "entailed" mock con verità del claim resta scorretto.** Il Gate oggi tratta correttamente le dimensioni come ortogonali; un futuro consumatore esterno (UI, report) deve continuare a presentare entailment come "supporto semantico claim ↔ quote", NON come verità del claim. **Una fonte citata non implica un claim vero. Una quote presente non implica che la quote sostenga il claim.**
- **Branch `entailment_block`** è implementato e testato end-to-end (via stub orchestrator), ma con il mock checker attuale **non si attiva spontaneamente in produzione**: serve un checker reale che emetta `verdict='contradicted'` (8.8C o successive).
- **Crescita del Final Answer Gate.** Post-8.8A-GATE il file ha sei branch + due LATERAL JOIN. Refactor in modulo policy separato rinviato a blocco dedicato.
- **`evaluator` NLI reale non esiste**; il mock heuristic non sostituisce un NLI semantico.

---

## Vincoli sempre validi (MVP-0)

- Nessun provider AI reale, nessun riferimento operativo a OpenAI, Anthropic, Google o altri provider esterni nel codice di MVP-0.
- `PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0`.
- Closed Corpus only.
- SQLAlchemy 2.0 Core: `Connection`, non `Engine.execute`. Query SQL con bound params.
- Migration applicate (0001–0010) sono immutabili. Modifiche schema solo via nuove migration.
- Test rerun-safe con UUID/hash/marker unici per invocazione.
- Append-only enforced a DB su `audit_records`, `evidence_spans`, `claim_ledger_entries`, `final_answer_spans`, `final_gate_reports`, `published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records`, `source_quality_assessments`, **`claim_entailment_checks`**.
- Endpoint API 8.6/8.7F read-only. Read API claim-entailment NON ancora esposta.
- Final Answer Gate (8.7G + 8.8A-GATE): consultazione read-only di `source_quality_assessments` e `claim_entailment_checks`; nessuna mutazione su quelle tabelle né su `claim_ledger_entries`/`claim_lineage`.

---

## Prossimo passo

**Fase 8.8A tecnicamente chiusa al commit `394257b`.** Il prossimo blocco operativo consigliato è uno dei seguenti, in ordine di valore architetturale:

- **8.8A-READ** — Read API per `claim_entailment_checks` (`GET /api/v1/tasks/{id}/claim-entailment` task-level summary + per-span breakdown; opzionale `GET /api/v1/claims/{logical_id}/entailment-checks`). Read-only, JSONB verbatim, no RBAC in MVP-0.
- **8.8B-REPORT** — Anti-Hallucination Report API aggregato (`GET /api/v1/tasks/{id}/anti-hallucination-report` o `GET /api/v1/published-answers/{id}/anti-hallucination-report`) che compone i quattro assi (entailment, source quality, source loss, CVE-lite verification) in un singolo payload.
- **UI-PRE** — Apertura della fase UI con un primo design dedicato per esporre coverage gaps (`entailment_*`, `source_quality_*`, `unverified_claim`, `missing_evidence`) e per distinguere visivamente i quattro assi anti-allucinazione.

Direzioni complementari (sempre da decidere con prompt dedicato):

- **0011_* retention** una volta deciso il perimetro distruttivo.
- **RBAC e redaction** dei JSONB esposti dagli endpoint read 8.6/8.7F e dei `details` dei `coverage_gap_statements`.
- **Cursor pagination** sugli endpoint read.
- **Smoke test end-to-end con Redis reale** e worker main loop reale.
- **Trigger append-only** su `coverage_gap_statements`.
- **8.8C — Contradiction Detector reale** per attivare spontaneamente il Branch `entailment_block` (e popolare `source_quality_assessments.contradiction_status` con valori reali).
