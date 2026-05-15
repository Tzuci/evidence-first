# PROJECT_STATE — Evidence-First MVP-0

Documento di onboarding tecnico, una pagina, leggibile dal collaboratore al primo accesso senza dover leggere il codice. Riflette lo stato del repo al commit **Fase 8.7H** (chiusura della fase 8.7): `b70ef8fb394e0f28befdfd2b3a699c32a88e9914` ("Add phase 8.7 source quality realistic flow").

---

## Cosa è il progetto

Piattaforma multi-AI **evidence-first** ed **evidence-gated**.

Il sistema è progettato per impedire che claim fattuali non supportati, contraddetti o basati su fonti inadeguate vengano pubblicati come affidabili. **Il progetto non promette di eliminare le allucinazioni in senso assoluto**: promette evidenze tracciabili, registrate nel Claim Ledger, verificate dal CVE-lite, valutate sul piano della qualità delle fonti, propagate via lifecycle e source-loss, **e consumate dal Final Answer Gate** prima di qualunque pubblicazione. La piattaforma rende visibili o blocca i claim non supportati, contraddetti o basati su fonti inadeguate prima della pubblicazione affidabile; non garantisce che un LLM non generi internamente output errati.

In MVP-0 il nucleo evidence-gated è costruito **prima** della visione multi-AI. Provider AI reali, Verified Web Mode, Hybrid Mode, consensus engine, contradiction detector avanzato e critical reviewer sono fasi future. Il claim "evidence-gated" qui significa: esiste una base append-only verificabile end-to-end per draft/gate/published, una propagazione lifecycle e source-loss minimale per MVP-0, una superficie di osservabilità HTTP read-only sopra di essa, un primo Source Quality Evaluator deterministico mock (8.7) che scrive assessment append-only sulle fonti che supportano i claim, e una policy decisionale (8.7G) che fa consultare quegli assessment al Final Answer Gate per bloccare o segnalare warning su fonti inadeguate, **validata end-to-end da un realistic flow test (8.7H) che esercita sia il warning path sia il block path attraverso l'intera catena API → FakeRedis → dispatcher → consumer → servizi worker → DB → read API**.

---

## Stato fase 8.7

**Fase 8.7 chiusa.** Tutti i blocchi 8.7A–8.7H sono completati. Il prossimo blocco operativo è 8.8A (Claim Entailment Checker), o in alternativa un blocco infrastrutturale (retention 0009, RBAC/redaction, cursor pagination).

| Blocco | Descrizione | Stato |
|---|---|---|
| 8.7A | `PHASE_8_7_PLAN.md` | done |
| 8.7B | `migrations/0007_source_quality.sql` | done |
| 8.7C | Shared schemas (codomini + Literal alias + `SourceQualityAssessmentRead`) | done |
| 8.7D | Mock Source Quality Evaluator service | done |
| 8.7E | Worker integration (W-A) — step in `task_created` con SAVEPOINT + audit aggregato | done |
| 8.7F | Read API (due endpoint GET) | done |
| 8.7G | Migration 0008 + Gate integration (policy P1+P3+P4) | done |
| 8.7H | Realistic flow tests + docs finalization | **done** |

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
| `0009_*` retention futura | numero da assegnare; retention reale distruttiva ancora non scritta |

Nota: il numero `0008` è ora occupato da `coverage_gap_source_quality` (estensione del CHECK su `coverage_gap_statements.kind`). La retention futura distruttiva prenderà un numero successivo (provvisoriamente `0009_*`).

---

## Cosa esiste oggi (Fasi 8.4 + 8.5 + 8.6 minima + 8.7A–H)

### Base 8.4 (invariata nel comportamento; reason_code esteso in 8.7G)

- **DB foundation multi-tenant**: `tenants`, `users`, `projects`, `sessions`, `task_masters`, `event_processing_records`, `policy_versions`.
- **Audit chain hash-linked, append-only, verificabile end-to-end** via `verify_audit_chain` / `verify_task_audit_chain`. Append-only enforced a DB tramite trigger comune `reject_modify_append_only`.
- **Storage layer content-addressed, deduplicato, refcount-based**: `storage_blobs`, `storage_objects`. Dedup global concorrenza-safe via `INSERT ... ON CONFLICT DO NOTHING` sull'indice parziale `sb_global_uq`.
- **Document store** con upload reale `.txt`/`.md`, chunking deterministico, `evidence_spans` minimali, `task_documents`. `evidence_spans` append-only.
- **Claim Ledger append-only stretto**: `logical_claims`, `raw_claims`, `classified_claims`, `claim_ledger_entries`, `claim_lineage`, `claim_evidence_links`, `verification_records`. Supersede esclusivamente via `claim_lineage.relation_kind='supersedes'`.
- **Extractor mock-driven**, **CVE-lite mock-driven**, **Compiler mock-driven**, **Final Answer Gate mock-driven (esteso in 8.7G)**.
- **Worker single-consumer 8.4** per `task.created`, FK-safe, resume-safe, idempotente.
- **Coerenza referenziale stretta a DB** tra `task_masters` ↔ `draft_final_answers` ↔ `final_gate_reports` ↔ `published_answers` via UNIQUE composite e FK composite.

### Fase 8.5 (invariata)

**Schema (migration `0006_lifecycle.sql`).** Tre tabelle append-only:

- `published_answer_lifecycle_events`: FK composita `(published_answer_id, task_id) → published_answers(id, task_id)`, `event_type ∈ {published, withdrawal_requested, withdrawn, superseded}`, UNIQUE `(published_answer_id, event_type, idempotency_key)`.
- `source_loss_events`: FK `evidence_span_id → evidence_spans(id)` come granularità canonica, `loss_kind ∈ {source_deleted, source_access_lost, quote_mismatch, document_replaced, policy_retraction}`, UNIQUE `(evidence_span_id, loss_kind, idempotency_key)`.
- `source_loss_propagation_records`: `propagation_kind ∈ {claim_marked_unverifiable, published_answer_impacted, no_claims_impacted, no_active_published_answers_impacted}`, `status ∈ {recorded, skipped, failed}`, idempotenza via partial unique indexes ristretti a `status IN ('recorded','skipped')`.

**Servizi worker.**

- `published_answer_lifecycle.apply_withdrawal`: unico scrittore autorizzato dei campi lifecycle di `published_answers` per il path di withdrawal.
- `source_loss_propagator.propagate_source_loss`: risolve l'impact set da `evidence_span_id`, append `v(N+1)` `unverifiable / unsupported / source_lost` con lineage `supersedes`, registra `source_loss_propagation_records`.

**Consumer e dispatcher.** `consumers/published_answer_withdrawal.py`, `consumers/source_loss.py`, `consumers/dispatch.py`. Worker multi-stream con gruppo `worker_default`.

**API producer 8.5.** `POST /api/v1/published-answers/{published_answer_id}/withdrawal-requests`, `POST /api/v1/source-loss-events`.

### Fase 8.6 minima (invariata)

Quattro endpoint GET read-only di osservabilità su lifecycle e source-loss:

- `GET /api/v1/published-answers/{published_answer_id}/lifecycle-events` (8.6A)
- `GET /api/v1/source-loss-events/{source_loss_event_id}` (8.6B)
- `GET /api/v1/source-loss-events/{source_loss_event_id}/propagation` (8.6C)
- `GET /api/v1/tasks/{task_id}/source-loss-events` (8.6D)

Tutti read-only end-to-end, verificati da snapshot pre/post sui count.

### Fase 8.7 — Source Quality (A–H implementate, fase chiusa)

Stato post-8.7H: lo strato di valutazione qualità delle fonti esiste come capability append-only e osservabile via HTTP, **è consumato dal Final Answer Gate** secondo la policy P1+P3+P4 (vedi §8.7G più sotto), **ed è validato end-to-end da un realistic flow test (8.7H) che esercita warning e block path attraverso l'intera catena API → FakeRedis → dispatcher → task.created consumer → source quality → final gate → read API**.

**8.7B — Schema (`migrations/0007_source_quality.sql`).**

- Tabella `source_quality_assessments` append-only (trigger `source_quality_assessments_append_only` su `reject_modify_append_only`).
- CHECK `sqa_target_xor`: esattamente UNO tra `evidence_span_id`, `document_chunk_id`, `document_id` non-null per riga.
- Nove CHECK enum sui codomini: `source_type`, `source_role`, `authority_level`, `independence_level`, `freshness`, `relevance`, `extract_quality`, `contradiction_status`, `overall_quality`.
- `confidence` `DOUBLE PRECISION` in `[0.0, 1.0]` o NULL.
- Sei partial unique indexes (tre versioning + tre idempotency, uno per target kind): `sqa_evidence_version_uq`, `sqa_chunk_version_uq`, `sqa_document_version_uq`, `sqa_evidence_idem_uq`, `sqa_chunk_idem_uq`, `sqa_document_idem_uq`.
- FK con `ON DELETE RESTRICT`.
- `policy_name` e `policy_version` come stringhe opache (nessun FK a `policy_versions`).
- Indici di lookup su `tenant_id`, `project_id`, target per granularità, `overall_quality`, `source_role`, `freshness`.

**8.7C — Codomini shared (`packages/shared/evidencefirst_shared/schemas.py`).**

- Nove tuple `SOURCE_QUALITY_*_VALUES` che rispecchiano esattamente i CHECK enum di 0007.
- Nove `Literal` alias (`SourceQualitySourceType`, `SourceQualitySourceRole`, …) per consumer che vogliono tipizzazione stretta.
- `SourceQualityAssessmentRead` con campi quality come `str` per coerenza con gli altri Read model (l'enforcement resta a DB level).

**8.7D — Mock Source Quality Evaluator (`apps/worker/app/services/source_quality_evaluator.py`).**

- Deterministico, mock-driven. Nessun provider AI, nessuna web search, nessuna euristica reale.
- Identità: `SERVICE_NAME="mock_source_quality_evaluator"`, `SERVICE_VERSION="0.1.0"`, `DEFAULT_POLICY_NAME="mvp0_mock_source_quality"`, `DEFAULT_POLICY_VERSION="0.1.0"`.
- Politica mock fissa: tutti gli evidence_span ricevono `overall_quality='unknown'`, `contradiction_status='unchecked'`, `confidence=0.5`, e le altre dimensioni fissate (vedi `PHASE_8_7_PLAN.md`). **Il mock evaluator è ancora deterministico e produce sempre `unknown` + `unchecked`**; non emette mai `unsuitable`, `weak`, `strong`, `adequate`, né valori reali di `contradiction_status`.
- Idempotenza per `(target_kind, target_id, idempotency_key)` con short-circuit `STATUS_ALREADY_ASSESSED`; doppia protezione via SAVEPOINT su `IntegrityError` (race) con recovery SELECT.
- Canonical scope: l'INSERT scrive `tenant_id`/`project_id` letti dal target row, non quelli passati dal caller.
- Non emette mai `audit_records`. Non muta `claim_ledger_entries`, `claim_lineage`, `claim_evidence_links`, `verification_records`, `final_gate_reports`, `published_answers`, `source_loss_*`, `published_answer_lifecycle_events`.

**8.7E — Worker integration (`apps/worker/app/services/source_quality_orchestrator.py` + integrazione in `apps/worker/app/consumers/task_created.py`).**

- Orchestrator `run_source_quality_assessment(conn, task_id)` chiama `assess_source_quality` per ogni `evidence_span_id` linkato ai claim del task con idempotency key deterministica `task:{task_id}:span:{evidence_span_id}:v1`.
- Lo step viene eseguito **solo nel fresh-run path**, dentro `_run_8_3_extract_and_verify`, **dopo `task.analyzed_partial`** e **prima di `task.compiling`**.
- La chiamata è incapsulata in `conn.begin_nested()` (SAVEPOINT): un fallimento NON aborta la transazione esterna e NON blocca la pipeline 8.4.
- Un singolo audit aggregato `task.source_quality_assessed` viene emesso con `status='completed'` (success) o `status='failed'` (rollback savepoint + audit).

**8.7F — Read API (`apps/api/app/routes/source_quality.py`).**

Due endpoint GET read-only, registrati in `apps/api/app/main.py`:

- `GET /api/v1/evidence-spans/{evidence_span_id}/source-quality`
- `GET /api/v1/tasks/{task_id}/source-quality`

Invarianti: read-only end-to-end, JSONB `payload` esposto verbatim (nessuna RBAC redaction in MVP-0), pagination via `limit` (no cursor), N+1 query per il task endpoint accettato per MVP-0.

**8.7G — Source Quality consumata dal Final Answer Gate.**

Migration `0008_coverage_gap_source_quality.sql` (applicata) estende il CHECK su `coverage_gap_statements.kind` da quattro a sei valori:

- valori preesistenti (da 0005): `unverified_claim`, `missing_evidence`, `out_of_scope`, `source_loss`;
- valori aggiunti in 0008: **`source_quality_block`**, **`source_quality_warning`**.

Il Final Answer Gate (`apps/worker/app/services/final_answer_gate.py`) consuma `source_quality_assessments` come terzo asse decisionale, dopo:

1. **Branch A — zero spans**: invariato. `decision='rejected'`, `reason_code='no_verified_claims'`, gap `kind='missing_evidence'`, `gap_key='no_verified_claims'`.
2. **Branch C — unverified_spans_present**: invariato. Se almeno uno span non è verified-backed, `decision='rejected'`, `reason_code='unverified_spans_present'`, un gap `kind='unverified_claim'` per ciascuno span scoperto. **Source Quality NON è consultata in questo branch** (priorità *CVE-lite > Source Quality*).
3. **Phase 8.7G — applicata solo quando tutti gli span sono verified-backed.** Per ogni span verified-backed, il Gate consulta la `source_quality_assessments` **latest assoluta** per ciascun `evidence_span_id` di supporto. Aggregazione per span: **worst-on-block, any-on-warn**.

Policy MVP-0 implementata = **P1 + P3 + P4**:

- **Block**: `overall_quality='unsuitable'`; `contradiction_status ∈ {contradicted_by_stronger_source, conflicting_sources}`.
- **Warning**: `overall_quality ∈ {weak, unknown}`; `contradiction_status='unchecked'`; latest mancante.
- **Clean**: `overall_quality ∈ {strong, adequate}` AND `contradiction_status='no_known_contradiction'`.

Branch decisionali post-8.7G:

- **Branch C' — source_quality_block**: `decision='rejected'`, `reason_code='source_quality_block'`, gap `kind='source_quality_block'` severity='block', nessun published_answer.
- **Branch B' — all_spans_verified_with_warnings**: `decision='approved'`, `reason_code='all_spans_verified_with_warnings'`, gap `kind='source_quality_warning'` severity='warn', published_answer v1 inserito.
- **Branch B — all_spans_verified**: invariato 8.4.

**Comportamento con il mock evaluator attuale.** Il mock 8.7D scrive sempre `overall_quality='unknown'` e `contradiction_status='unchecked'`. Conseguenza: ogni task verified-backed entra nel **Branch B'** e raggiunge `published` come terminale, con un `coverage_gap_statements` di tipo `source_quality_warning` per span. **Il `Branch C'` (source_quality_block) è implementato e testato, ma in produzione con il mock attuale non si attiva spontaneamente**: il mock non emette `unsuitable`, `contradicted_by_stronger_source`, né `conflicting_sources`. Il branch è attivato dal realistic flow test 8.7H tramite uno stub dell'orchestrator (vedi sotto).

**8.7H — Realistic flow test + chiusura formale fase 8.7.**

Nuovo file di test root-level: **`tests/test_phase_8_7_source_quality_flow.py`**. Due funzioni di test end-to-end indipendenti che esercitano l'intera catena API → FakeRedis → dispatcher → `task.created` consumer → servizi worker (extractor + CVE-lite + 8.7E source quality + compiler + Final Answer Gate) → DB → read API 8.7F + endpoint 8.4 di lettura. Il worker viene caricato sotto un alias namespace `_wapp` per evitare la collisione con il package `app` dell'API (entrambi sono package top-level letteralmente chiamati `app`); Redis è una `FakeRedis` minima che cattura solo `xadd`.

**Scenario warning flow** (`test_phase_8_7_source_quality_warning_flow_end_to_end`):

- Setup: tenant `dev` + user `dev@local` seedati direttamente in DB; project + document (`.txt` con frasi contenenti cifre, perché l'extractor mock-driven richiede digit per emettere raw_claim) + task creati via API HTTP (`POST /api/v1/projects`, `POST /api/v1/projects/{id}/documents`, `POST /api/v1/tasks`).
- Pipeline drive: `_dispatch.handle_event(event, redis_consumer_name='realistic_8_7h_warning')`.
- Mock source quality evaluator → ogni evidence_span linkato ai claim del task riceve una v1 con `overall_quality='unknown'` + `contradiction_status='unchecked'`.
- Final Answer Gate → `decision='approved'`, `reason_code='all_spans_verified_with_warnings'`.
- Coverage gaps → almeno una riga `kind='source_quality_warning'` severity='warn', `gap_key=f'span:{span_id}:source_quality_warning'`, `details.reasons` include `source_quality_unknown` e/o `source_quality_contradiction_unchecked`. Nessun `source_quality_block`, nessun `unverified_claim`.
- `published_answers` v1 con `status='published'`.
- Verifica read API: `GET /api/v1/tasks/{id}/final-gate-report` 200 con `coverage_gap_statements` di kind `source_quality_warning`; `GET /api/v1/tasks/{id}/published-answer` 200; `GET /api/v1/tasks/{id}/source-quality` 200 con `summary.latest_overall_quality_counts.unknown >= 1`; `GET /api/v1/evidence-spans/{es_id}/source-quality` 200 con `latest_assessment.overall_quality='unknown'`.
- Verifica audit chain end-to-end: `task.source_quality_assessed` strettamente tra `task.analyzed_partial` e `task.compiling`; `task.published` come evento terminale; `verify_task_audit_chain` ok.

**Scenario block flow** (`test_phase_8_7_source_quality_block_flow_end_to_end`):

- Setup analogo + monkeypatch del simbolo `_wapp.consumers.task_created.run_source_quality_assessment` con uno **stub orchestrator** che, per ogni evidence_span linkato al task, inserisce una riga v1 in `source_quality_assessments` con `overall_quality='unsuitable'`, `evaluator_name='test_source_quality_evaluator'`, e ritorna il dict counts canonico atteso dal consumer (`status='completed'`, `spans_total=N`, ecc.). Lo stub è necessario perché **il mock evaluator reale non produce `unsuitable` spontaneamente**.
- Pipeline drive: `_dispatch.handle_event(event, redis_consumer_name='realistic_8_7h_block')`.
- Final Answer Gate → `decision='rejected'`, `reason_code='source_quality_block'`.
- Coverage gaps → almeno una riga `kind='source_quality_block'` severity='block', `gap_key=f'span:{span_id}:source_quality_block'`, `details.reasons` include `source_quality_unsuitable`. Nessun `source_quality_warning`, nessun `unverified_claim`.
- Task terminale: `status='analyzed_partial'`, audit terminale = `task.publication_held`. **Nessun `task.published`**. Nessun `published_answers` v1.
- Verifica read API: `GET /api/v1/tasks/{id}/final-gate-report` 200 con `decision='rejected'` e `coverage_gap_statements` di kind `source_quality_block`; `GET /api/v1/tasks/{id}/published-answer` **404 RESOURCE_NOT_FOUND** con `details.resource='published_answers'`; `GET /api/v1/tasks/{id}/source-quality` 200 con `summary.latest_overall_quality_counts.unsuitable >= 1`.

Il test valida l'intera catena: API HTTP → FakeRedis → dispatcher → `task.created` consumer → source quality (mock o stub) → final gate → read API. **Branch C' viene attivato nel realistic test esclusivamente tramite stub dell'orchestrator** perché il mock evaluator reale produce solo `unknown`+`unchecked`; questa proprietà del mock è preservata e non viene alterata dal test.

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
| `GET /api/v1/tasks/{id}/final-gate-report` | Gate report + coverage gaps (include `source_quality_*`) | 8.4 (esteso 8.7G) |
| `GET /api/v1/tasks/{id}/published-answer` | Published answer per task | 8.4 |
| `GET /api/v1/published-answers/{id}` | Published answer per id | 8.4 |
| `POST /api/v1/published-answers/{published_answer_id}/withdrawal-requests` | Producer asincrono di withdrawal | 8.5 |
| `POST /api/v1/source-loss-events` | Producer asincrono di source loss | 8.5 |
| `GET /api/v1/published-answers/{published_answer_id}/lifecycle-events` | Read lifecycle events di un published_answer | 8.6A |
| `GET /api/v1/source-loss-events/{source_loss_event_id}` | Read single source_loss_event | 8.6B |
| `GET /api/v1/source-loss-events/{source_loss_event_id}/propagation` | Read propagation records di un source_loss_event | 8.6C |
| `GET /api/v1/tasks/{task_id}/source-loss-events` | Read task-level source-loss listing (S1 ∪ S2) | 8.6D |
| `GET /api/v1/evidence-spans/{evidence_span_id}/source-quality` | Read source quality assessments per evidence_span | 8.7F |
| `GET /api/v1/tasks/{task_id}/source-quality` | Read task-level source quality summary | 8.7F |
| `GET /health/live` / `/health/db` / `/health/queue` / `/health/storage` / `/health/ready` | Health checks | 8.1+ |

L'endpoint `/final-gate-report` non è stato riscritto in 8.7G/H: continua a esporre i `coverage_gap_statements` collegati al draft, e quei `coverage_gap_statements` possono avere `kind ∈ {source_quality_block, source_quality_warning}` in aggiunta ai kind preesistenti.

---

## Pipeline 8.4 / 8.7 (validata end-to-end dal test 8.7H)

### Task con documenti, approved scenario (mock attuale = warning flow)

`task.created` → `task.docs_attached` (API) → `task.analyzing` → `task.docs_loaded` → `task.claims_extracted` → `task.claims_classified` → `task.claims_ledger_initialized` → `task.cve_lite_started` → `task.cve_lite_completed` → `task.analyzed_partial` → `task.source_quality_assessed` → `task.compiling` → `task.draft_compiled` → `task.final_gate_started` → `task.final_gate_completed` → `task.published`.

`reason_code='all_spans_verified_with_warnings'` (mock evaluator → ogni span emette `source_quality_warning` con reasons `source_quality_unknown` + `source_quality_contradiction_unchecked`). `published_answers` v1 inserito.

### Task con documenti, rejected zero-verified (invariato)

Identica fino a `task.final_gate_completed`, poi `task.publication_held`. `reason_code='no_verified_claims'`. Gap `kind='missing_evidence'`. Source Quality NON consultata (Branch A).

### Task con documenti, rejected unverified spans (invariato)

Identica al rejected zero-verified, ma con `reason_code='unverified_spans_present'` e un gap `kind='unverified_claim'` per ogni span scoperto. Source Quality NON consultata (priorità CVE-lite, Branch C).

### Task con documenti, rejected source_quality_block (Branch C' 8.7G; oggi attivabile solo via stub dell'orchestrator)

Identica al rejected, ma con `reason_code='source_quality_block'` e almeno un gap `kind='source_quality_block'`. **Implementato e testato end-to-end nel realistic flow 8.7H tramite stub dell'orchestrator**; in produzione con il mock evaluator attuale non si attiva spontaneamente. Si attiverà naturalmente con un evaluator reale che produca `overall_quality='unsuitable'` o `contradiction_status ∈ {contradicted_by_stronger_source, conflicting_sources}`.

### Task senza documenti (invariato)

`task.created` (API) → `task.analyzing` → `task.blocked`. Lo step source quality NON viene eseguito.

---

## Semantica lifecycle e source loss (Fase 8.5, invariata)

[Sezione invariata rispetto al post-8.7G. Withdrawal asincrona via API + consumer + `apply_withdrawal`; source loss INSERT immediato + pubblicazione `source_loss.detected`; propagazione soft via `source_loss_propagation_records`; nessuna estensione di `task_masters.status`; audit chain verificabile end-to-end.]

---

## Semantica Source Quality (Fase 8.7, finale post-8.7H)

Distinzioni fondative, valide per chiunque legga, consumi o veda emergere `source_quality_assessments` come gap:

- **source quality ≠ claim correctness.** Un claim può essere falso anche se la fonte è autorevole; un claim può essere corretto anche se la fonte è debole. Una fonte citata NON implica claim vero.
- **source quality ≠ evidence support.** Un legame `claim_evidence_links` ben formato non implica qualità della fonte.
- **source quality ≠ verification outcome.** `verification_records.outcome='pass'` significa "CVE-lite passato", non "fonte affidabile".
- **source quality ≠ source loss.** La perdita di fonte (8.5) è un evento; la qualità (8.7) è un giudizio strutturale sulla fonte presente.
- **source quality ≠ publication eligibility.** L'eligibility è composta da correctness, evidence support, source quality e policy gate: quattro assi separati. La 8.7G aggiunge il terzo asse alla decisione del Gate; non collassa i quattro.

Stato corrente dell'evaluator:

- L'evaluator è un **mock deterministico** (`mock_source_quality_evaluator` v0.1.0, policy `mvp0_mock_source_quality` v0.1.0).
- Tutte le righe scritte oggi hanno `overall_quality='unknown'` e `contradiction_status='unchecked'` con `confidence=0.5`.
- Gli endpoint read 8.7F espongono il payload JSONB **verbatim**, senza RBAC e senza redaction.
- Il valore `unknown` NON deve essere interpretato come approvazione forte: significa letteralmente "il sistema oggi non sa, e non finge di sapere". Il Gate lo tratta come **warning**, non come clean: un task verified-backed con mock attuale viene pubblicato con `reason_code='all_spans_verified_with_warnings'` e un `coverage_gap_statements` di tipo `source_quality_warning` per span.

Stato corrente del Final Answer Gate (post-8.7G/H):

- **Source Quality è consumata dal Final Answer Gate** secondo la policy P1+P3+P4 (block su `unsuitable`/`contradicted_by_stronger_source`/`conflicting_sources`; warning su `weak`/`unknown`/`unchecked`/missing; clean altrimenti).
- La priorità *CVE-lite > Source Quality* è invariante: uno span non verified-backed produce `unverified_spans_present` indipendentemente dalla qualità delle fonti di supporto.
- L'identità della policy è stampata nel `details` di ogni gap source_quality come `{"policy": {"name": "mvp0_source_quality_gate_policy", "version": "0.1.0"}}`.
- **Validato end-to-end** dal realistic flow test 8.7H sia per il warning path (Branch B') sia per il block path (Branch C', tramite stub dell'orchestrator).

---

## Final Answer Gate — regole di verifica (post-8.7G, validate da 8.7H)

Uno span è **verified-backed** se e solo se esiste almeno un `final_answer_span_claim_links` tale che:

```
link.claim_ledger_entry_id == latest_entry_id_for(claim_logical_id)
AND latest_entry_state_for(claim_logical_id) == 'verified_fact'
```

Branch decisionali:

| Condizione del draft | `decision` | `reason_code` | Coverage gap | `published_answers` |
|---|---|---|---|---|
| Zero spans | `rejected` | `no_verified_claims` | `kind='missing_evidence'`, `gap_key='no_verified_claims'` | assente |
| Almeno uno span non verified-backed (priorità CVE-lite) | `rejected` | `unverified_spans_present` | un gap `kind='unverified_claim'` per ogni span scoperto | assente |
| Tutti verified-backed + almeno uno span ha source_quality block | `rejected` | `source_quality_block` | un gap `kind='source_quality_block'` per span bloccato + eventuali `source_quality_warning` per gli altri | assente |
| Tutti verified-backed + almeno uno span ha source_quality warning (no block) | `approved` | `all_spans_verified_with_warnings` | un gap `kind='source_quality_warning'` per span con warning | v1 con `status='published'` |
| Tutti verified-backed + nessun warning | `approved` | `all_spans_verified` | nessuno | v1 con `status='published'` |

### Convenzione errori

`ErrorCode.NOT_PUBLISHED` non esiste in MVP-0. Per le GET su task esistente non ancora pubblicato si restituisce `RESOURCE_NOT_FOUND` con `details.resource='published_answers'`. Per task inesistente: `details.resource='task_masters'`. Convenzione confermata dal block flow 8.7H, che riceve 404 RESOURCE_NOT_FOUND con `details.resource='published_answers'` dopo il `source_quality_block`.

---

## Test (post-8.7H)

Test plan implementato per la fase 8.7:

- **Unit / integration**:
  - `apps/worker/tests/test_source_quality_evaluator_service.py` (14 scenari, 8.7D)
  - `apps/worker/tests/test_source_quality_orchestrator.py` (7 scenari, 8.7E)
  - `apps/worker/tests/test_task_created_source_quality_step.py` (4 scenari, 8.7E)
  - `apps/worker/tests/test_consumer_with_documents.py` (sequenza 14 eventi post-8.7E)
  - `apps/api/tests/test_source_quality_read_endpoint.py` (read API 8.7F)
  - `apps/worker/tests/test_final_answer_gate_source_quality.py` (13 scenari, 8.7G)
  - `apps/worker/tests/test_compiler_and_gate.py` e `test_extractor_and_cve_lite.py` allineati al nuovo reason_code di default (`all_spans_verified_with_warnings`)
- **Realistic flow (8.7H, root-level)**:
  - **`tests/test_phase_8_7_source_quality_flow.py`** — due test end-to-end:
    - `test_phase_8_7_source_quality_warning_flow_end_to_end` — copre il warning flow: mock source quality → `overall_quality='unknown'` + `contradiction_status='unchecked'` → Final Answer Gate approved con `reason_code='all_spans_verified_with_warnings'` → `source_quality_warning` gap → `published_answers` v1.
    - `test_phase_8_7_source_quality_block_flow_end_to_end` — copre il block flow: orchestrator stub nel consumer → `source_quality_assessments` con `overall_quality='unsuitable'` → Final Answer Gate rejected con `reason_code='source_quality_block'` → `source_quality_block` gap → `task.publication_held` → nessun `published_answer`.

Tutti i test sono passing al commit `b70ef8f` post-8.7H.

---

## Cosa è ancora rinviato (non implementato) — debiti tecnici e roadmap

### Anti-Hallucination roadmap (post-chiusura 8.7)

**Disclaimer.** La piattaforma non elimina le allucinazioni in senso assoluto. Impedisce o rende visibili claim non supportati, contraddetti o basati su fonti inadeguate prima della pubblicazione affidabile. I componenti elencati di seguito sono **ancora mancanti** in MVP-0 post-8.7H:

- **8.8A — Claim Entailment Checker.** Verifica che l'evidence quote effettivamente implichi (o sia compatibile con) il claim, non solo che sia testualmente presente. **Mancante.**
- **8.8B — Citation-to-Claim Validator.** Verifica che il claim citi le evidenze giuste, non evidenze "vicine" che non lo supportano. **Mancante.**
- **8.8C — Contradiction Detector reale.** Detector reale di contraddizioni tra claim o tra fonti (oggi `contradiction_records` placeholder, `contradiction_status='unchecked'` per costruzione del mock). Quando attivato, sostituirà le `unchecked` con valori reali e attiverà naturalmente il `Branch C'` del Gate. **Mancante.**
- **8.8D — Final Answer Sentence Gate.** Gate a livello frase nel published_answer, non solo a livello span verified-backed. **Mancante.**
- **8.8E — Anti-Hallucination Report API.** Endpoint aggregato che riporta su un singolo published_answer tutti gli assi (entailment, citation, contradiction, source quality). **Mancante.**
- **8.9 — External Verification / Web-RAG controllato.** Verifica esterna su fonti web in modalità controllata (Verified Web Mode), non più solo closed corpus. **Mancante.**
- **9.0 — Multi-agent consensus + adversarial review reale.** Provider AI reali, consensus engine, critical reviewer adversariale.

Il prossimo blocco operativo consigliato è **8.8A-PRE / Claim Entailment Checker**.

### Altri debiti tecnici

- **Backfill source quality per task pre-8.7E.** I task processati prima dell'integrazione 8.7E non hanno righe in `source_quality_assessments`. Il Gate per tali task emette warning `source_quality_missing_assessment` su ogni span (senza bloccare). Non esiste backfill script.
- **Recompile/v2 dopo source_quality_block.** Un task bloccato da `source_quality_block` non ha oggi un path applicativo per ritentare con un draft v2 (il compiler emette solo v1). Rejected è terminale per quel task.
- **`coverage_gap_statements` senza trigger append-only.** La tabella non ha un trigger `reject_modify_append_only`. 8.7G rispetta operativamente l'invariante insert-only, ma non c'è enforcement a DB.
- **`conflicting_sources` come block è un compromesso.** Sarebbe semanticamente più corretto un "hold for human review", ma `task_masters.status` non si estende.
- **Retention reale distruttiva** (`0009_*` da scrivere). Le tabelle 8.5/8.7 crescono senza pruning.
- **RBAC / redaction** sui payload JSONB esposti dagli endpoint read 8.6/8.7F e sulle `details` dei `coverage_gap_statements`.
- **Provider AI reali, Verified Web Mode, Hybrid Mode.** MVP-0 gira con `PROVIDERS_ENABLED=mock` e `MAX_COST_PER_TASK=0`.
- **Renderer ed export** Markdown/HTML/PDF/DOCX/JSON-LD.
- **Auth reale.**
- **DLQ esplicita per il worker.**
- **UI completa.** In particolare nessuna UI ancora espone i nuovi gap source_quality.
- **OCR / parsing PDF, vector store cloud, storage S3 / GCS / Azure operativo.**
- **Cursor pagination** sugli endpoint read 8.6/8.7F.
- **Stretch 8.6** `GET /api/v1/published-answers/{id}/source-loss-impact` — opzionale, non implementato.
- **Worker main loop reale negli end-to-end test.** I realistic flow 8.5/8.6/8.7H usano FakeRedis e invocano `dispatch.handle_event` direttamente.
- **N+1 nel task endpoint 8.7F**: accettato per MVP-0.
- **Calibrazione futura della policy 8.7G con evaluator reale.** P2 (block su `weak`) è oggi scartata; potrebbe diventare difendibile con un evaluator reale + P5 (disclosure). La policy versionata (`mvp0_source_quality_gate_policy` v0.1.0) abilita un bump futuro tracciabile.
- **"unknown" non significa "approvato".** Il Gate oggi lo tratta correttamente come warning; un futuro consumatore esterno (UI, report) deve continuare a presentarlo come incertezza, non come approvazione. **Una fonte citata non implica un claim vero**: la qualità della fonte e la correttezza del claim restano assi separati.
- **Branch C' (source_quality_block)** è implementato e testato end-to-end (via stub orchestrator in 8.7H), ma con il mock evaluator attuale **non si attiva spontaneamente in produzione**: serve un evaluator reale che emetta `unsuitable` / `contradicted_by_stronger_source` / `conflicting_sources` (8.8C).

---

## Vincoli sempre validi (MVP-0)

- Nessun provider AI reale, nessun riferimento operativo a OpenAI, Anthropic, Google o altri provider esterni nel codice di MVP-0.
- `PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0`.
- Closed Corpus only.
- SQLAlchemy 2.0 Core: `Connection`, non `Engine.execute`.
- Migration applicate (0001–0008) sono immutabili. Modifiche schema solo via nuove migration.
- Test rerun-safe con UUID/hash/marker unici per invocazione.
- Append-only enforced a DB su `audit_records`, `evidence_spans`, `claim_ledger_entries`, `final_answer_spans`, `final_gate_reports`, `published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records`, `source_quality_assessments`.
- Endpoint API 8.6/8.7F read-only.
- Final Answer Gate (8.7G): consultazione read-only di `source_quality_assessments`; nessuna mutazione su quella tabella né su `claim_ledger_entries`/`claim_lineage`.

---

## Prossimo passo

**Fase 8.7 chiusa al commit `b70ef8f`.** Il prossimo blocco operativo consigliato è:

- **8.8A-PRE / Claim Entailment Checker** — apertura della fase 8.8 anti-hallucination con il primo entailment checker reale (oltre la CVE-lite di sola presenza testuale). Vedi `PHASE_8_7_PLAN.md §13` per la roadmap completa.

Direzioni complementari (sempre da decidere con prompt dedicato):

- **0009_* retention** una volta deciso il perimetro distruttivo.
- **RBAC e redaction** dei JSONB esposti dagli endpoint read 8.6/8.7F e dei `details` dei `coverage_gap_statements`.
- **Cursor pagination** sugli endpoint read.
- **Stretch 8.6**: `GET /api/v1/published-answers/{id}/source-loss-impact`.
- **Smoke test end-to-end con Redis reale** e worker main loop reale.
- **Trigger append-only** su `coverage_gap_statements` (oggi insert-only operativo, non enforced a DB).
