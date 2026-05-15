# PROJECT_STATE — Evidence-First MVP-0

Documento di onboarding tecnico, una pagina, leggibile dal collaboratore al primo accesso senza dover leggere il codice. Riflette lo stato del repo al commit **Fase 8.7G**: `79815764cd8c588556b81c5914b61deb16eb7370` ("Use source quality in final answer gate").

---

## Cosa è il progetto

Piattaforma multi-AI **evidence-first** ed **evidence-gated**.

Il sistema è progettato per impedire che claim fattuali non supportati, contraddetti o basati su fonti inadeguate vengano pubblicati come affidabili. Non promette di eliminare le allucinazioni: promette evidenze tracciabili, registrate nel Claim Ledger, verificate dal CVE-lite, valutate sul piano della qualità delle fonti, propagate via lifecycle e source-loss, **e ora consumate dal Final Answer Gate** prima di qualunque pubblicazione.

In MVP-0 il nucleo evidence-gated è costruito **prima** della visione multi-AI. Provider AI reali, Verified Web Mode, Hybrid Mode, consensus engine, contradiction detector avanzato e critical reviewer sono fasi future. Il claim "evidence-gated" qui significa: esiste una base append-only verificabile end-to-end per draft/gate/published, una propagazione lifecycle e source-loss minimale per MVP-0, una superficie di osservabilità HTTP read-only sopra di essa, un primo Source Quality Evaluator deterministico mock (8.7) che scrive assessment append-only sulle fonti che supportano i claim, **e una policy decisionale (8.7G) che fa consultare quegli assessment al Final Answer Gate per bloccare o segnalare warning su fonti inadeguate**. Non è una soluzione completa al problema delle allucinazioni.

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

## Cosa esiste oggi (Fasi 8.4 + 8.5 + 8.6 minima + 8.7A–G)

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

### Fase 8.7 — Source Quality (A–G implementate)

Stato post-8.7G: lo strato di valutazione qualità delle fonti esiste come capability append-only e osservabile via HTTP, **ed è ora consumato dal Final Answer Gate** secondo la policy P1+P3+P4 (vedi §8.7G più sotto).

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
- Politica mock fissa: tutti gli evidence_span ricevono `overall_quality='unknown'`, `contradiction_status='unchecked'`, `confidence=0.5`, e le altre dimensioni fissate (vedi `PHASE_8_7_PLAN.md`).
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

L'estensione è realizzata via DROP + ADD del CHECK con identificazione robusta del constraint via `pg_constraint.conkey` JOIN `pg_attribute`, per essere indipendente dalla rappresentazione testuale interna di Postgres (`kind IN (...)` ↔ `kind = ANY (ARRAY[...])`).

Il Final Answer Gate (`apps/worker/app/services/final_answer_gate.py`) ora consuma `source_quality_assessments` come terzo asse decisionale, dopo:

1. **Branch A — zero spans**: invariato. `decision='rejected'`, `reason_code='no_verified_claims'`, gap `kind='missing_evidence'`, `gap_key='no_verified_claims'`.
2. **Branch C — unverified_spans_present**: invariato. Se almeno uno span non è verified-backed (link non punta alla latest entry verified_fact), `decision='rejected'`, `reason_code='unverified_spans_present'`, un gap `kind='unverified_claim'` per ciascuno span scoperto, `gap_key=f'span:{final_answer_span_id}'`. **Source Quality NON è consultata in questo branch.** Questa è l'invariante di priorità *CVE-lite > Source Quality* documentata in `PHASE_8_7G_PRE.md §8.4`.
3. **Phase 8.7G — applicata solo quando tutti gli span sono verified-backed.** Per ogni span verified-backed, il Gate consulta la `source_quality_assessments` **latest assoluta** (`ORDER BY version_no DESC, created_at DESC, id DESC LIMIT 1`) per ciascun `evidence_span_id` di supporto (risolto via `claim_evidence_links` filtrato sulla latest entry verified_fact). Aggregazione per span: **worst-on-block, any-on-warn**.

Policy MVP-0 implementata = **P1 + P3 + P4**:

- **Block** (per evidence_span supportante uno span verified-backed):
  - `overall_quality = 'unsuitable'` → reason `source_quality_unsuitable`;
  - `contradiction_status = 'contradicted_by_stronger_source'` → reason `source_quality_contradicted_by_stronger_source`;
  - `contradiction_status = 'conflicting_sources'` → reason `source_quality_conflicting_sources`.
- **Warning** (solo se nessuna condizione di block ha colpito la stessa evidence_span):
  - `overall_quality = 'weak'` → reason `source_quality_weak`;
  - `overall_quality = 'unknown'` → reason `source_quality_unknown`;
  - `contradiction_status = 'unchecked'` → reason `source_quality_contradiction_unchecked`;
  - latest assessment mancante → reason `source_quality_missing_assessment`.
- **Clean**: `overall_quality ∈ {strong, adequate}` AND `contradiction_status = 'no_known_contradiction'`.

Branch decisionali aggiunti dal Gate post-8.7G:

- **Branch C' — source_quality_block**: almeno uno span verified-backed ha una condizione di block. `decision='rejected'`, `reason_code='source_quality_block'`. Per ogni span bloccato viene emesso un gap `kind='source_quality_block'`, `severity='block'`, `gap_key=f'span:{span_id}:source_quality_block'`. Eventuali warning su altri span vengono comunque emessi (`kind='source_quality_warning'`, `severity='warn'`). Nessun `published_answers` v1.
- **Branch B' — all_spans_verified_with_warnings**: nessuno span è bloccato, almeno uno presenta warning. `decision='approved'`, `reason_code='all_spans_verified_with_warnings'`. Per ogni span con warning viene emesso un gap `kind='source_quality_warning'`, `severity='warn'`, `gap_key=f'span:{span_id}:source_quality_warning'`. Il decision NON è cambiato dai warning: `published_answers` v1 viene inserito.
- **Branch B — all_spans_verified**: invariato 8.4. Nessuno span bloccato, nessun warning, `decision='approved'`, `reason_code='all_spans_verified'`. Nessun gap source_quality. `published_answers` v1 inserito.

**Comportamento con il mock evaluator attuale.** Il mock 8.7D scrive sempre `overall_quality='unknown'` e `contradiction_status='unchecked'` per ogni evidence_span. Conseguenza: ogni task verified-backed entra nel **Branch B'** e raggiunge `published` come terminale, ma con un `coverage_gap_statements` di tipo `source_quality_warning` per span. Il `reason_code` di default nel `final_gate_reports` di un task approved oggi è **`all_spans_verified_with_warnings`**, non `all_spans_verified`. Quest'ultimo si attiverà solo con un evaluator reale che produca codomini clean (`strong`/`adequate` + `no_known_contradiction`). **Il `Branch C'` non si attiva mai con il mock attuale**, perché il mock non emette `unsuitable`, `contradicted_by_stronger_source`, né `conflicting_sources`.

**Reason code ufficiali (final_gate_reports.reason_code).**

| reason_code | Branch | decision | Quando si attiva |
|---|---|---|---|
| `no_verified_claims` | A | rejected | Zero span al compiler (nessun verified_fact). |
| `unverified_spans_present` | C | rejected | Almeno uno span non verified-backed. Priorità CVE-lite. |
| `source_quality_block` | C' | rejected | Tutti verified-backed ma almeno uno span ha una source quality block. |
| `all_spans_verified_with_warnings` | B' | approved | Tutti verified-backed, almeno uno con warning (default oggi con mock). |
| `all_spans_verified` | B | approved | Tutti verified-backed e nessun warning (richiede evaluator reale clean). |

**Idempotenza 8.7G.** Una doppia invocazione di `run_final_answer_gate` sullo stesso draft NON duplica:

- il `final_gate_reports` (UNIQUE su `draft_final_answer_id`);
- i `coverage_gap_statements` di tipo `source_quality_*` (UNIQUE `(draft_final_answer_id, kind, gap_key)`, dove `gap_key` è deterministico per span);
- il `published_answers` v1 (UNIQUE `(task_id, version_no)`).

**Invarianti 8.7G rispettate dal Gate** (verificate dai test `apps/worker/tests/test_final_answer_gate_source_quality.py`):

- il Gate NON muta `source_quality_assessments` (consumo read-only via SELECT);
- il Gate NON muta `claim_ledger_entries` né `claim_lineage`;
- la priorità *CVE-lite > Source Quality* è preservata: uno span unverified produce `unverified_spans_present` anche se l'evidence_span supportante è `unsuitable` (scenario 12 della test suite);
- la "latest" usata dal Gate è la latest assoluta a livello DB (massimo `version_no` per `evidence_span_id`), non la "latest in slice" delle read API 8.7F: i due `latest` possono coincidere o no a seconda del parametro `limit` dell'API;
- aggregazione tra più evidence_span dello stesso span: worst-on-block, any-on-warn (scenari 11 della test suite).

**Test coverage 8.7G.** Il file `apps/worker/tests/test_final_answer_gate_source_quality.py` contiene 13 scenari che coprono: il warning path per `unknown`, `weak`, `unchecked`, missing; il block path per `unsuitable`, `contradicted_by_stronger_source`, `conflicting_sources`; la regola latest-wins su versioning; l'aggregazione multi-evidence; la priorità CVE-lite; l'idempotenza su redelivery. I test 8.4 esistenti (`test_compiler_and_gate.py`, `test_extractor_and_cve_lite.py`) sono stati allineati al nuovo reason_code di default approved (`all_spans_verified_with_warnings`). **Worker suite: 105 passed. Root suite: 109 passed.**

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
| `GET /api/v1/tasks/{id}/final-gate-report` | Gate report + coverage gaps (ora include `source_quality_*`) | 8.4 (esteso 8.7G) |
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

L'endpoint `/final-gate-report` non è stato riscritto in 8.7G: continua a esporre i `coverage_gap_statements` collegati al draft, e quei `coverage_gap_statements` ora possono avere `kind ∈ {source_quality_block, source_quality_warning}` in aggiunta ai kind preesistenti.

---

## Pipeline 8.4 / 8.7 (sintesi aggiornata post-8.7G)

### Task con documenti, approved scenario (mock attuale)

`task.created` → `task.docs_attached` (API) → `task.analyzing` → `task.docs_loaded` → `task.claims_extracted` → `task.claims_classified` → `task.claims_ledger_initialized` → `task.cve_lite_started` → `task.cve_lite_completed` → `task.analyzed_partial` → `task.source_quality_assessed` → `task.compiling` → `task.draft_compiled` → `task.final_gate_started` → `task.final_gate_completed` → `task.published`.

Il `final_gate_reports` del task termina con `reason_code='all_spans_verified_with_warnings'` (mock evaluator → ogni span emette `source_quality_warning` con reasons `source_quality_unknown` + `source_quality_contradiction_unchecked`). Il `published_answers` v1 con `status='published'` viene comunque inserito: i warning NON cambiano la decision.

### Task con documenti, rejected zero-verified (invariato)

Sequenza identica fino a `task.final_gate_completed`, poi `task.publication_held` (evento audit-only, lo `status` resta `analyzed_partial`). `reason_code='no_verified_claims'`. Gap `kind='missing_evidence'`. Source Quality NON consultata (il gate entra in Branch A prima).

### Task con documenti, rejected unverified spans (invariato)

Sequenza identica al rejected zero-verified, ma con `reason_code='unverified_spans_present'` e un gap `kind='unverified_claim'` per ogni span scoperto. Source Quality NON consultata (priorità CVE-lite, Branch C).

### Task con documenti, rejected source_quality_block (nuovo branch 8.7G; oggi mai attivato con mock)

Sequenza identica al rejected, ma con `reason_code='source_quality_block'` e almeno un gap `kind='source_quality_block'`. Si attiverà solo con un evaluator reale che produca `overall_quality='unsuitable'` o `contradiction_status ∈ {contradicted_by_stronger_source, conflicting_sources}` su almeno un evidence_span che supporta uno span verified-backed.

### Task senza documenti (invariato)

`task.created` (API) → `task.analyzing` → `task.blocked`. Lo step source quality NON viene eseguito (non c'è il path `_run_8_3_extract_and_verify`). Il Gate non viene mai chiamato.

---

## Semantica lifecycle e source loss (Fase 8.5, invariata)

**Withdrawal è asincrona.** L'API pubblica un evento `published_answer.withdrawal_requested` su Redis e ritorna 202. Il consumer `published_answer_withdrawal` delega ad `apply_withdrawal`, unica entità che muta `published_answers.status` da `published` a `withdrawn`.

**Source loss è asincrona ma con INSERT immediato lato API.** `POST /api/v1/source-loss-events` inserisce la riga `source_loss_events` e pubblica `source_loss.detected` nella stessa transazione DB.

**Source loss NON ritira automaticamente published_answers.** Cascade soft via `source_loss_propagation_records`; la withdrawal resta operazione separata.

**`task_masters.status` non viene esteso** per withdrawal/source loss/source quality. Il lifecycle vive su `published_answers.status`, la propagazione su `claim_ledger_entries`/`source_loss_propagation_records`, gli assessment su `source_quality_assessments`. **Anche 8.7G non aggiunge stati a `task_masters.status`**: un task bloccato da `source_quality_block` segue lo stesso path dei rejected esistenti (terminale in `analyzed_partial` con audit `task.publication_held`).

**Audit chain resta verificabile.** `verify_task_audit_chain` ritorna `ok=True` dopo le transizioni 8.5, dopo lo step 8.7E (success o failed), e dopo le decisioni 8.7G (approved/rejected/warning).

---

## Semantica Source Quality (Fase 8.7, aggiornata post-8.7G)

Distinzioni fondative, valide per chiunque legga, consumi o veda emergere `source_quality_assessments` come gap:

- **source quality ≠ claim correctness.** Un claim può essere falso anche se la fonte è autorevole; un claim può essere corretto anche se la fonte è debole.
- **source quality ≠ evidence support.** Un legame `claim_evidence_links` ben formato non implica qualità della fonte.
- **source quality ≠ verification outcome.** `verification_records.outcome='pass'` significa "CVE-lite passato", non "fonte affidabile".
- **source quality ≠ source loss.** La perdita di fonte (8.5) è un evento; la qualità (8.7) è un giudizio strutturale sulla fonte presente.
- **source quality ≠ publication eligibility.** L'eligibility è composta da correctness, evidence support, source quality e policy gate: quattro assi separati. La 8.7G aggiunge il terzo asse alla decisione del Gate; non collassa i quattro.

Stato corrente dell'evaluator:

- L'evaluator è un **mock deterministico** (`mock_source_quality_evaluator` v0.1.0, policy `mvp0_mock_source_quality` v0.1.0).
- Tutte le righe scritte oggi hanno `overall_quality='unknown'` e `confidence=0.5`. Le altre dimensioni sono fissate dalla policy mock.
- Gli endpoint read 8.7F espongono il payload JSONB **verbatim**, senza RBAC e senza redaction.
- Il valore `unknown` NON deve essere interpretato come approvazione forte: significa letteralmente "il sistema oggi non sa, e non finge di sapere". In 8.7G il Gate tratta `unknown` come **warning**, non come clean: un task verified-backed con mock attuale viene pubblicato con `reason_code='all_spans_verified_with_warnings'` e un `coverage_gap_statements` di tipo `source_quality_warning` per span.

Stato corrente del Final Answer Gate (post-8.7G):

- **Source Quality è consumata dal Final Answer Gate** secondo la policy P1+P3+P4 (block su `unsuitable`/`contradicted_by_stronger_source`/`conflicting_sources`; warning su `weak`/`unknown`/`unchecked`/missing; clean altrimenti).
- La priorità *CVE-lite > Source Quality* è invariante: uno span non verified-backed produce `unverified_spans_present` indipendentemente dalla qualità delle fonti di supporto.
- L'identità della policy è stampata nel `details` di ogni gap source_quality come `{"policy": {"name": "mvp0_source_quality_gate_policy", "version": "0.1.0"}}`, per consentire un audit deterministico del classificatore quando la policy verrà bumpata.

---

## Final Answer Gate — regole di verifica (post-8.7G)

Uno span è **verified-backed** se e solo se esiste almeno un `final_answer_span_claim_links` tale che:

```
link.claim_ledger_entry_id == latest_entry_id_for(claim_logical_id)
AND latest_entry_state_for(claim_logical_id) == 'verified_fact'
```

Branch decisionali (post-8.7G):

| Condizione del draft | `decision` | `reason_code` | Coverage gap | `published_answers` |
|---|---|---|---|---|
| Zero spans | `rejected` | `no_verified_claims` | `kind='missing_evidence'`, `gap_key='no_verified_claims'` | assente |
| Almeno uno span non verified-backed (priorità CVE-lite) | `rejected` | `unverified_spans_present` | un gap `kind='unverified_claim'` per ogni span scoperto | assente |
| Tutti verified-backed + almeno uno span ha source_quality block | `rejected` | `source_quality_block` | un gap `kind='source_quality_block'` per span bloccato + eventuali `source_quality_warning` per gli altri | assente |
| Tutti verified-backed + almeno uno span ha source_quality warning (no block) | `approved` | `all_spans_verified_with_warnings` | un gap `kind='source_quality_warning'` per span con warning | v1 con `status='published'` |
| Tutti verified-backed + nessun warning | `approved` | `all_spans_verified` | nessuno | v1 con `status='published'` |

### Convenzione errori

`ErrorCode.NOT_PUBLISHED` non esiste in MVP-0. Per le GET su task esistente non ancora pubblicato si restituisce `RESOURCE_NOT_FOUND` con `details.resource='published_answers'`. Per task inesistente: `details.resource='task_masters'`. In 8.5/8.6/8.7 si usa la stessa convenzione (`details.resource='evidence_spans'`, `'source_loss_events'`, `'task_masters'`, `'published_answers'` a seconda dell'endpoint).

---

## Cosa è ancora rinviato (non implementato) — debiti tecnici e roadmap

### Anti-Hallucination roadmap (proposta aggiornata)

- **8.7H — Realistic flow + documentazione finale.** Test realistico end-to-end task → published che includa scenari source_quality_block/warning con seed manuale di assessment non-mock; aggiornamento finale di PROJECT_STATE e README.
- **8.8A — Claim Entailment Checker.** Verifica che l'evidence quote effettivamente implichi (o sia compatibile con) il claim, non solo che sia testualmente presente.
- **8.8B — Citation-to-Claim Validator.** Verifica che il claim citi le evidenze giuste, non evidenze "vicine" che non lo supportano.
- **8.8C — Contradiction Detector.** Detector reale di contraddizioni tra claim o tra fonti (oggi `contradiction_records` placeholder, `contradiction_status='unchecked'` per costruzione del mock). Quando attivato, sostituirà le `unchecked` con valori reali e attiverà naturalmente il `Branch C'` del Gate.
- **8.8D — Final Answer Sentence Gate.** Gate a livello frase nel published_answer, non solo a livello span verified-backed.
- **8.8E — Anti-Hallucination Report API.** Endpoint aggregato che riporta su un singolo published_answer tutti gli assi (entailment, citation, contradiction, source quality).
- **8.9 — External Verification / Web-RAG controllato.** Verifica esterna su fonti web in modalità controllata (Verified Web Mode), non più solo closed corpus.
- **9.0 — Multi-agent consensus + adversarial review reale.** Provider AI reali, consensus engine, critical reviewer adversariale.

Il prossimo blocco operativo può essere **8.7H** (chiusura ordinata della fase 8.7 con realistic flow tests) oppure **8.8A** (apertura della fase successiva con Claim Entailment Checker). Decisione da prendere con prompt operativo dedicato.

### Altri debiti tecnici

- **Backfill source quality per task pre-8.7E.** I task processati prima dell'integrazione 8.7E non hanno righe in `source_quality_assessments`. In 8.7G il Gate per tali task emetterebbe warning `source_quality_missing_assessment` su ogni span (senza bloccare). Non esiste backfill script.
- **Recompile/v2 dopo source_quality_block.** Un task bloccato da `source_quality_block` non ha oggi un path applicativo per ritentare con un draft v2 (il compiler emette solo v1). Conseguenza: rejected è terminale per quel task. Coerente con il design MVP-0; da rivedere quando si introdurranno draft v2.
- **`coverage_gap_statements` senza trigger append-only.** La tabella non ha un trigger `reject_modify_append_only` (vedi `0005_answers_gate.sql`). 8.7G rispetta operativamente l'invariante insert-only, ma non c'è enforcement a DB. Future fasi potranno aggiungere il trigger.
- **`conflicting_sources` come block è un compromesso.** Sarebbe semanticamente più corretto un "hold for human review", ma `task_masters.status` non si estende. Bloccare e mettere il task in `analyzed_partial` con `task.publication_held` è il compromesso meno cattivo.
- **Retention reale distruttiva** (`0009_*` da scrivere). Le tabelle 8.5/8.7 crescono senza pruning.
- **RBAC / redaction** sui payload JSONB esposti dagli endpoint read 8.6/8.7F e sulle `details` dei `coverage_gap_statements` (che ora contengono motivazioni source quality strutturate, non sensibili oggi ma esposti verbatim via `/final-gate-report`).
- **Provider AI reali, Verified Web Mode, Hybrid Mode.** MVP-0 gira con `PROVIDERS_ENABLED=mock` e `MAX_COST_PER_TASK=0`.
- **Renderer ed export** Markdown/HTML/PDF/DOCX/JSON-LD.
- **Auth reale.** Gli endpoint espongono JSONB verbatim senza autorizzazione.
- **DLQ esplicita per il worker.** Le entry il cui handler ritorna `failed` restano pending nel PEL.
- **UI completa.** Esiste solo un'app web minimale. In particolare nessuna UI ancora espone i nuovi gap source_quality.
- **OCR / parsing PDF, vector store cloud, storage S3 / GCS / Azure operativo.**
- **Cursor pagination** sugli endpoint read 8.6/8.7F (solo `limit` con tetto).
- **Stretch 8.6** `GET /api/v1/published-answers/{id}/source-loss-impact` — opzionale, non implementato.
- **Backfill `published` lifecycle events** per pubblicazioni create in 8.4: nessuno script.
- **Worker main loop reale negli end-to-end test.** I realistic flow 8.5/8.6 usano FakeRedis e invocano `dispatch.handle_event` direttamente.
- **N+1 nel task endpoint 8.7F** (loop su evidence_span_id): accettato per MVP-0.
- **Calibrazione futura della policy 8.7G con evaluator reale.** P2 (block su `weak`) è oggi scartata; potrebbe diventare difendibile con un evaluator reale + P5 (disclosure). La policy versionata (`mvp0_source_quality_gate_policy` v0.1.0) abilita un bump futuro tracciabile.
- **"unknown" non significa "approvato".** Va martellato nei docs ogni volta che si parla di source_quality. Oggi il Gate lo tratta correttamente come warning; un futuro consumatore esterno (UI, report) deve continuare a presentarlo come incertezza, non come approvazione.

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

Decisione tra due blocchi operativi, da prendere con prompt operativo separato:

- **8.7H — Realistic flow + docs finalization.** Test end-to-end realistico che attivi sia il Branch C' (`source_quality_block`) sia il Branch B' (`all_spans_verified_with_warnings`) con seed di assessment non-mock, chiusura formale della fase 8.7, eventuale aggiornamento del realistic flow file in `tests/`.
- **8.8A — Claim Entailment Checker.** Apertura della fase successiva con il primo entailment checker reale (oltre la CVE-lite di sola presenza testuale). Vedi `PHASE_8_7_PLAN.md §13` per la roadmap completa anti-allucinazione.

Direzioni complementari (sempre da decidere con prompt dedicato):

- **0009_* retention** una volta deciso il perimetro distruttivo.
- **RBAC e redaction** dei JSONB esposti dagli endpoint read 8.6/8.7F e dei `details` dei `coverage_gap_statements`.
- **Cursor pagination** sugli endpoint read.
- **Stretch 8.6**: `GET /api/v1/published-answers/{id}/source-loss-impact`.
- **Smoke test end-to-end con Redis reale** e worker main loop reale.
- **Trigger append-only** su `coverage_gap_statements` (oggi insert-only operativo, non enforced a DB).
