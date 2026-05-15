# PROJECT_STATE — Evidence-First MVP-0

Documento di onboarding tecnico, una pagina, leggibile dal collaboratore al primo accesso senza dover leggere il codice. Riflette lo stato del repo al commit **Fase 8.7F**: `91397ae6f02abd429cff29b6e0248cf9a7c16317` ("Add source quality read endpoints").

---

## Cosa è il progetto

Piattaforma multi-AI **evidence-first** ed **evidence-gated**.

Il sistema è progettato per impedire che claim fattuali non supportati, contraddetti o basati su fonti inadeguate vengano pubblicati come affidabili. Non promette di eliminare le allucinazioni: promette evidenze tracciabili, registrate nel Claim Ledger, verificate dal CVE-lite, valutate sul piano della qualità delle fonti, propagate via lifecycle e source-loss, e approvate dal Final Answer Gate prima di qualunque pubblicazione.

In MVP-0 il nucleo evidence-gated è costruito **prima** della visione multi-AI. Provider AI reali, Verified Web Mode, Hybrid Mode, consensus engine, contradiction detector avanzato e critical reviewer sono fasi future. Il claim "evidence-gated" qui significa: esiste una base append-only verificabile end-to-end per draft/gate/published, una propagazione lifecycle e source-loss minimale per MVP-0, una superficie di osservabilità HTTP read-only sopra di essa, e un primo Source Quality Evaluator deterministico mock (8.7) che scrive assessment append-only sulle fonti che supportano i claim. Non è una soluzione completa al problema delle allucinazioni.

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
| `0008_*` retention futura | numero da assegnare; retention reale distruttiva ancora non scritta |

Nota: la voce `0007_evaluation_retention.sql` indicata nei documenti precedenti è stata superata. Il numero `0007` è ora occupato da `source_quality`; la retention futura prenderà un numero successivo (provvisoriamente `0008_*`).

---

## Cosa esiste oggi (Fasi 8.4 + 8.5 + 8.6 minima + 8.7A–F)

### Base 8.4 (invariata)

- **DB foundation multi-tenant**: `tenants`, `users`, `projects`, `sessions`, `task_masters`, `event_processing_records`, `policy_versions`.
- **Audit chain hash-linked, append-only, verificabile end-to-end** via `verify_audit_chain` / `verify_task_audit_chain`. Append-only enforced a DB tramite trigger comune `reject_modify_append_only`.
- **Storage layer content-addressed, deduplicato, refcount-based**: `storage_blobs`, `storage_objects`. Dedup global concorrenza-safe via `INSERT ... ON CONFLICT DO NOTHING` sull'indice parziale `sb_global_uq`.
- **Document store** con upload reale `.txt`/`.md`, chunking deterministico, `evidence_spans` minimali, `task_documents`. `evidence_spans` append-only.
- **Claim Ledger append-only stretto**: `logical_claims`, `raw_claims`, `classified_claims`, `claim_ledger_entries`, `claim_lineage`, `claim_evidence_links`, `verification_records`. Supersede esclusivamente via `claim_lineage.relation_kind='supersedes'`.
- **Extractor mock-driven**, **CVE-lite mock-driven**, **Compiler mock-driven**, **Final Answer Gate mock-driven**.
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

### Fase 8.7 — Source Quality

Stato post-8.7F: lo strato di valutazione qualità delle fonti esiste come capability append-only e osservabile via HTTP, ma **non è ancora consumato dal Final Answer Gate**.

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
- Politica mock fissa:
  - `source_type='user_document'`, `source_role='unclear'`, `authority_level='unknown'`, `independence_level='unknown'`, `freshness='undated'`, `contradiction_status='unchecked'`, `overall_quality='unknown'`, `confidence=0.5`.
  - per `evidence_span`: `relevance='direct_support'`, `extract_quality='exact_quote_match'`.
  - per `document_chunk` / `document`: `relevance='contextual_support'`, `extract_quality='partial_match'`.
- Validazione codomini al module-load (assert su appartenenza alle tuple shared).
- Target XOR validato a livello applicativo prima di toccare il DB.
- Lock `FOR UPDATE` sul target parent row per serializzare il calcolo del prossimo `version_no`.
- Idempotenza per `(target_kind, target_id, idempotency_key)` con short-circuit `STATUS_ALREADY_ASSESSED`; doppia protezione via SAVEPOINT su `IntegrityError` (race) con recovery SELECT.
- Canonical scope: l'INSERT scrive `tenant_id`/`project_id` letti dal target row, non quelli passati dal caller (un caller scorretto non può inquinare la tabella).
- Payload JSONB include `mock=true`, `semantic_warning="source_quality_does_not_mean_claim_truth"`, e `input_payload` opzionale verbatim.
- Status di ritorno: `assessed`, `already_assessed`, `invalid_target`, `not_found`.
- Non emette mai `audit_records`. Non muta `claim_ledger_entries`, `claim_lineage`, `claim_evidence_links`, `verification_records`, `final_gate_reports`, `published_answers`, `source_loss_*`, `published_answer_lifecycle_events`.

**8.7E — Worker integration (`apps/worker/app/services/source_quality_orchestrator.py` + integrazione in `apps/worker/app/consumers/task_created.py`).**

- Orchestrator `run_source_quality_assessment(conn, task_id)`:
  - risolve `(tenant_id, project_id)` da `task_masters`;
  - calcola DISTINCT `evidence_span_id` linkati ai claim del task via `claim_evidence_links` JOIN `logical_claims` (filtro `evidence_span_id IS NOT NULL`, ordinati ASC per determinismo);
  - per ogni span chiama `assess_source_quality` con idempotency key deterministica `task:{task_id}:span:{evidence_span_id}:v1`;
  - aggrega contatori `{spans_total, assessed_count, already_assessed_count, not_found_count, invalid_target_count, error_count}`;
  - ritorna `status='not_found'` se il task non esiste, `status='completed'` altrimenti (anche con `spans_total=0`).
- Integrazione nel consumer `task_created`:
  - Lo step viene eseguito **solo nel fresh-run path**, dentro `_run_8_3_extract_and_verify`, **dopo `task.analyzed_partial`** e **prima di `task.compiling`**.
  - La chiamata è incapsulata in `conn.begin_nested()` (SAVEPOINT): un fallimento dell'orchestrator NON aborta la transazione esterna e NON blocca la pipeline 8.4.
  - Un singolo audit aggregato `task.source_quality_assessed` viene emesso con `status='completed'` (success) o `status='failed'` (rollback savepoint + audit). Sul ramo failed il payload include `error_type` (nome classe eccezione, **mai stack trace**) e `counts` con `error_count=1`.
  - Sui resume da `compiling` o `analyzed_partial` lo step **non** viene re-eseguito (il path entra direttamente in `_run_8_4_compile_and_gate`). L'audit `task.source_quality_assessed` resta unico per task lifetime.
  - Nessun nuovo stream Redis, nessun nuovo consumer, nessuna modifica al dispatcher.

**8.7F — Read API (`apps/api/app/routes/source_quality.py`).**

Due endpoint GET read-only, registrati in `apps/api/app/main.py`:

- `GET /api/v1/evidence-spans/{evidence_span_id}/source-quality`
  - 404 `RESOURCE_NOT_FOUND` con `details.resource="evidence_spans"` se lo span non esiste;
  - 200 con `items=[]` e `latest_assessment=null` se lo span esiste senza assessment;
  - 200 con `items` ordinati ASC per `(version_no, created_at, id)`, `limit` 1–5000 default 100;
  - wrapper `{evidence_span_id, latest_assessment, items}`; `latest_assessment` è l'ultimo elemento dello slice ritornato.
- `GET /api/v1/tasks/{task_id}/source-quality`
  - 404 `RESOURCE_NOT_FOUND` con `details.resource="task_masters"` se il task non esiste;
  - 200 con un item per evidence_span linkato al task; span senza assessment esposti con `latest_assessment=null` e `items=[]` (no occultamento);
  - `summary` con `evidence_spans_total`, `spans_with_assessment`, `spans_without_assessment`, `latest_overall_quality_counts` su tutto il codominio di `overall_quality` (`{strong, adequate, weak, unsuitable, unknown}`), inizializzato a zero;
  - i counts del summary contano solo l'`latest_assessment` per span;
  - `limit_per_span` 1–5000 default 100.

Invarianti comuni:
- Read-only end-to-end (nessun INSERT/UPDATE/DELETE, nessuna chiamata a `assess_source_quality` o `run_source_quality_assessment`, nessun import di codice worker).
- JSONB `payload` esposto verbatim (nessuna RBAC redaction in MVP-0).
- Pagination via `limit` (no cursor).
- N+1 query per il task endpoint (loop su span_id) accettato per MVP-0; documentato come debito.

Test riportati dall'utente dopo 8.7F: test source quality read endpoint passati, API suite passata, root tests passati, worker suite già passata dopo 8.7E.

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
| `GET /api/v1/evidence-spans/{evidence_span_id}/source-quality` | Read source quality assessments per evidence_span | 8.7F |
| `GET /api/v1/tasks/{task_id}/source-quality` | Read task-level source quality summary | 8.7F |
| `GET /health/live` / `/health/db` / `/health/queue` / `/health/storage` / `/health/ready` | Health checks | 8.1+ |

---

## Pipeline 8.4 / 8.7 (sintesi aggiornata)

### Task con documenti, approved scenario

`task.created` → `task.docs_attached` (API) → `task.analyzing` → `task.docs_loaded` → `task.claims_extracted` → `task.claims_classified` → `task.claims_ledger_initialized` → `task.cve_lite_started` → `task.cve_lite_completed` → `task.analyzed_partial` → **`task.source_quality_assessed`** → `task.compiling` → `task.draft_compiled` → `task.final_gate_started` → `task.final_gate_completed` → `task.published`.

### Task con documenti, rejected zero-verified

Sequenza identica fino a `task.final_gate_completed`, poi `task.publication_held` (evento audit-only, lo `status` resta `analyzed_partial`).

### Task senza documenti

`task.created` (API) → `task.analyzing` → `task.blocked`. Lo step source quality NON viene eseguito (non c'è il path `_run_8_3_extract_and_verify`).

**Nota su `task.source_quality_assessed`.** Evento audit unico per task lifetime, emesso nel fresh-run path tra `task.analyzed_partial` e `task.compiling`. Sui resume non viene re-emesso. Sul fallimento dell'orchestrator viene comunque emesso con `status='failed'` e il SAVEPOINT viene rollback-ato, lasciando 8.4 in grado di proseguire.

---

## Semantica lifecycle e source loss (Fase 8.5, invariata)

**Withdrawal è asincrona.** L'API pubblica un evento `published_answer.withdrawal_requested` su Redis e ritorna 202. Il consumer `published_answer_withdrawal` delega ad `apply_withdrawal`, unica entità che muta `published_answers.status` da `published` a `withdrawn`.

**Source loss è asincrona ma con INSERT immediato lato API.** `POST /api/v1/source-loss-events` inserisce la riga `source_loss_events` e pubblica `source_loss.detected` nella stessa transazione DB.

**Source loss NON ritira automaticamente published_answers.** Cascade soft via `source_loss_propagation_records`; la withdrawal resta operazione separata.

**`task_masters.status` non viene esteso** per withdrawal/source loss/source quality. Il lifecycle vive su `published_answers.status`, la propagazione su `claim_ledger_entries`/`source_loss_propagation_records`, gli assessment su `source_quality_assessments`.

**Audit chain resta verificabile.** `verify_task_audit_chain` ritorna `ok=True` dopo le transizioni 8.5 e dopo lo step 8.7E (success o failed).

---

## Semantica Source Quality (Fase 8.7)

Distinzioni fondative, valide per chiunque legga o consumi `source_quality_assessments`:

- **source quality ≠ claim correctness.** Un claim può essere falso anche se la fonte è autorevole; un claim può essere corretto anche se la fonte è debole.
- **source quality ≠ evidence support.** Un legame `claim_evidence_links` ben formato non implica qualità della fonte.
- **source quality ≠ verification outcome.** `verification_records.outcome='pass'` significa "CVE-lite passato", non "fonte affidabile".
- **source quality ≠ source loss.** La perdita di fonte (8.5) è un evento; la qualità (8.7) è un giudizio strutturale sulla fonte presente.
- **source quality ≠ publication eligibility.** L'eligibility è composta da correctness, evidence support, source quality e policy gate: quattro assi separati.

Stato corrente dell'evaluator:

- L'evaluator è un **mock deterministico** (`mock_source_quality_evaluator` v0.1.0, policy `mvp0_mock_source_quality` v0.1.0).
- Tutte le righe scritte oggi hanno `overall_quality='unknown'` e `confidence=0.5`. Le altre dimensioni sono fissate dalla policy mock (vedi §8.7D sopra).
- Gli endpoint read 8.7F espongono il payload JSONB **verbatim**, senza RBAC e senza redaction.
- Il valore `unknown` NON deve essere interpretato come approvazione forte: significa letteralmente "il sistema oggi non sa, e non finge di sapere".

Stato corrente del Final Answer Gate:

- **Source Quality non è ancora consumata dal Final Answer Gate.**
- Il gate continua a usare la regola "verified-backed" definita in 8.4: uno span è verified-backed sse esiste `final_answer_span_claim_links` tale che `link.claim_ledger_entry_id == latest_entry_id_for(claim_logical_id)` AND `latest_entry_state_for(claim_logical_id) == 'verified_fact'`.
- L'integrazione decisionale tra Source Quality e Gate è prevista per il blocco **8.7G**.

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

`ErrorCode.NOT_PUBLISHED` non esiste in MVP-0. Per le GET su task esistente non ancora pubblicato si restituisce `RESOURCE_NOT_FOUND` con `details.resource='published_answers'`. Per task inesistente: `details.resource='task_masters'`. In 8.5/8.6/8.7 si usa la stessa convenzione (`details.resource='evidence_spans'`, `'source_loss_events'`, `'task_masters'`, `'published_answers'` a seconda dell'endpoint).

---

## Cosa è ancora rinviato (non implementato) — debiti tecnici e roadmap

### Anti-Hallucination roadmap (proposta)

- **8.7G — Source Quality Gate.** Integrazione decisionale di `source_quality_assessments` nel Final Answer Gate (es. policy P1 blocking + P5 disclosure, da decidere nel blocco 8.7G-PRE).
- **8.7H — Realistic flow + documentazione finale.** Test realistico end-to-end task → published con metadati di qualità, aggiornamento finale di PROJECT_STATE.
- **8.8A — Claim Entailment Checker.** Verifica che l'evidence quote effettivamente implichi (o sia compatibile con) il claim, non solo che sia testualmente presente.
- **8.8B — Citation-to-Claim Validator.** Verifica che il claim citi le evidenze giuste, non evidenze "vicine" che non lo supportano.
- **8.8C — Contradiction Detector.** Detector reale di contraddizioni tra claim o tra fonti (oggi `contradiction_records` placeholder, `contradiction_status='unchecked'` per costruzione).
- **8.8D — Final Answer Sentence Gate.** Gate a livello frase nel published_answer, non solo a livello span verified-backed.
- **8.8E — Anti-Hallucination Report API.** Endpoint aggregato che riporta su un singolo published_answer tutti gli assi (entailment, citation, contradiction, source quality).
- **8.9 — External Verification / Web-RAG controllato.** Verifica esterna su fonti web in modalità controllata (Verified Web Mode), non più solo closed corpus.
- **9.0 — Multi-agent consensus + adversarial review reale.** Provider AI reali, consensus engine, critical reviewer adversariale.

### Altri debiti tecnici

- **Backfill source quality per task pre-8.7E.** I task processati prima dell'integrazione 8.7E non hanno righe in `source_quality_assessments`. Nessuno script di backfill è in atto: gli endpoint 8.7F ritornano `items=[]` per quei task, coerentemente con lo stato DB.
- **Retention reale distruttiva** (`0008_*` da scrivere). Le tabelle 8.5/8.7 crescono senza pruning.
- **RBAC / redaction** sui payload JSONB esposti dagli endpoint read 8.6/8.7F.
- **Provider AI reali, Verified Web Mode, Hybrid Mode.** MVP-0 gira con `PROVIDERS_ENABLED=mock` e `MAX_COST_PER_TASK=0`.
- **Renderer ed export** Markdown/HTML/PDF/DOCX/JSON-LD.
- **Auth reale.** Gli endpoint espongono JSONB verbatim senza autorizzazione.
- **DLQ esplicita per il worker.** Le entry il cui handler ritorna `failed` restano pending nel PEL.
- **UI completa.** Esiste solo un'app web minimale.
- **OCR / parsing PDF, vector store cloud, storage S3 / GCS / Azure operativo.**
- **Cursor pagination** sugli endpoint read 8.6/8.7F (solo `limit` con tetto).
- **Stretch 8.6** `GET /api/v1/published-answers/{id}/source-loss-impact` — opzionale, non implementato.
- **Backfill `published` lifecycle events** per pubblicazioni create in 8.4: nessuno script, 8.6A ritorna `items=[]` per quei published_answer.
- **Worker main loop reale negli end-to-end test.** I realistic flow 8.5/8.6 usano FakeRedis e invocano `dispatch.handle_event` direttamente.
- **N+1 nel task endpoint 8.7F** (loop su evidence_span_id): accettato per MVP-0, batchabile in futuro.

---

## Vincoli sempre validi (MVP-0)

- Nessun provider AI reale, nessun riferimento operativo a OpenAI, Anthropic, Google o altri provider esterni nel codice di MVP-0.
- `PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0`.
- Closed Corpus only.
- SQLAlchemy 2.0 Core: `Connection`, non `Engine.execute`.
- Migration applicate (0001–0007) sono immutabili. Modifiche schema solo via nuove migration.
- Test rerun-safe con UUID/hash/marker unici per invocazione.
- Append-only enforced a DB su `audit_records`, `evidence_spans`, `claim_ledger_entries`, `final_answer_spans`, `final_gate_reports`, `published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records`, `source_quality_assessments`.
- Endpoint API 8.6/8.7F read-only verificato (per 8.6) da snapshot pre/post sui count delle tabelle 8.4/8.5/audit.

---

## Prossimo passo

Il blocco operativo immediatamente successivo è **8.7G-PRE**: analisi rigorosa del Final Answer Gate e decisione sulla policy Source Quality Gate (selezione tra P1/P2/P3/P4/P5 come definite in PHASE_8_7_PLAN.md §6.2, eventuale introduzione di `coverage_gap_statements.kind='source_quality_block'` in una migration separata, definizione del contratto di consultazione degli assessment da parte del gate). Nessun codice 8.7G viene scritto prima di 8.7G-PRE.

Direzioni complementari, da decidere con prompt operativo separato:

- **8.7H** — realistic flow tests source quality + documentazione finale.
- **0008_* retention** una volta deciso il perimetro.
- **RBAC e redaction** dei JSONB esposti dagli endpoint read 8.6/8.7F.
- **Cursor pagination** sugli endpoint read.
- **Stretch 8.6**: `GET /api/v1/published-answers/{id}/source-loss-impact`.
- **Smoke test end-to-end con Redis reale** e worker main loop reale.
