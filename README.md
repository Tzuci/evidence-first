# Evidence-First Multi-AI Platform — MVP-0

Piattaforma multi-AI **evidence-first** ed **evidence-gated**. Il sistema è progettato per impedire che claim fattuali non supportati, contraddetti o basati su fonti inadeguate vengano pubblicati come affidabili. Non promette di eliminare le allucinazioni: promette evidenze tracciabili, registrate nel Claim Ledger, verificate, valutate sulla qualità delle fonti, propagate via lifecycle/source-loss, e approvate dal Final Answer Gate.

> **Stato corrente.** Repository al commit **`91397ae`** ("Add source quality read endpoints"), Fase **8.7F** conclusa.
> Fasi implementate: 8.1–8.4 (foundation, storage, claim ledger, compiler + Final Answer Gate + first `published_answers`), 8.5 (lifecycle + source loss + propagator), 8.6 minima (read API lifecycle e source-loss), 8.7A–F (Source Quality Evaluator append-only + worker integration in `task.created` + read API).
> Per lo stato di dettaglio fase-per-fase, l'elenco completo degli endpoint, la pipeline aggiornata e i debiti tecnici, vedi [`PROJECT_STATE.md`](PROJECT_STATE.md).
>
> Le sezioni sotto descrivono lo stato della Fase 8.4 al momento della sua chiusura e sono **storiche**: hanno valore di documentazione architetturale del nucleo evidence-gated minimo, ma NON riflettono lo stato corrente del repository. Le fasi successive (8.5, 8.6, 8.7) NON sono qui documentate; vedi `PROJECT_STATE.md`.

Disponibile localmente:
- Postgres 16, Redis 7
- API HTTP (FastAPI) con `health`, `projects`, `tasks`, `audit`, `documents`, `claims`, `answers`, lifecycle/source-loss read API (8.6), source quality read API (8.7F)
- Worker Redis Streams single-consumer, FK-safe, idempotente, resume-safe, con pipeline 8.4 + step Source Quality (8.7E) prima del compiler
- Web Next.js minimale (home + `/diagnostic`)
- Storage filesystem deduplicato content-addressed con upsert concorrenza-safe
- Audit chain hash-linked, append-only, verificabile end-to-end

In 8.4 i task con documenti e claim verificati raggiungono lo stato terminale `published`, con un `published_answers` v1 e un `final_gate_reports` `approved`. I task con documenti ma senza claim verificati raggiungono lo stato terminale `analyzed_partial` con un `final_gate_reports` `rejected` e l'evento audit `task.publication_held`. La sequenza audit con documenti è stata estesa in 8.7E con `task.source_quality_assessed` tra `task.analyzed_partial` e `task.compiling`: vedi `PROJECT_STATE.md` per la sequenza aggiornata.

**Renderer esterni Markdown/HTML/PDF/DOCX/JSON-LD restano fuori scope per MVP-0.** Il nucleo evidence-gated produce `draft_final_answers`, `final_answer_spans` e `published_answers` con `summary_text` testuale e `content_hash`, ma non emette artefatti esportabili in formati di rendering. Gli endpoint answers sono read-only e restituiscono JSON normalizzato.

---

## Scope MVP-0 (sintesi)

Incluso (oggi, post-8.7F):
- Closed Corpus, Postgres + Redis + filesystem locale.
- Audit chain hash-linked (append-only, verificabile).
- `event_processing_records` con idempotenza per consumer.
- API: `health/projects/tasks/audit/documents/claims/answers`, lifecycle/source-loss read (8.6), source quality read (8.7F).
- Storage content-addressed deduplicato, refcount-based.
- Document upload reale `.txt`/`.md` con chunking deterministico ed `evidence_spans` minimali.
- **Claim Ledger append-only** con extractor mock-driven e CVE-lite mock-driven.
- **Compiler mock-driven** che produce `draft_final_answers` v1 con `final_answer_spans` 1:1 sui claim `verified_fact`.
- **Final Answer Gate mock-driven** che decide `approved`/`rejected`, scrive `final_gate_reports` (append-only) e, su approved, inserisce `published_answers` v1 con `status='published'`.
- **Lifecycle e source loss** (8.5): withdrawal asincrona, source loss event/propagation append-only, due API producer.
- **Read API 8.6** su lifecycle events e source-loss events/propagation/task-listing (read-only end-to-end).
- **Source Quality 8.7**: tabella `source_quality_assessments` append-only, mock evaluator deterministic, orchestrator chiamato in `task.created` dopo `analyzed_partial` (SAVEPOINT-protected, audit aggregato `task.source_quality_assessed`), due endpoint read 8.7F.
- Coerenza referenziale stretta a livello DB tra task ↔ draft ↔ gate ↔ published via UNIQUE composite e FK composite.

Escluso (rinviato a fasi successive — vedi `PHASE_8_7_PLAN.md` §13 per la roadmap anti-allucinazione):
- Provider AI reali, Verified Web Mode, Hybrid Mode, consensus engine.
- Source Quality Gate (8.7G): gli assessment esistono ma il Final Answer Gate NON li consuma ancora.
- Contradiction detector reale (8.8C), Claim Entailment Checker (8.8A), Citation-to-Claim Validator (8.8B), Final Answer Sentence Gate (8.8D), Anti-Hallucination Report API (8.8E), External Verification / Web-RAG (8.9), Multi-agent consensus + adversarial review reale (9.0).
- Renderer e/o export Markdown/HTML/PDF/DOCX/JSON-LD.
- PDF/OCR, vector store cloud, S3/GCS/Azure.
- Human Review UI completa, retention/eval/export jobs.
- RBAC reale e redaction dei JSONB esposti dagli endpoint read.
- Retention reale distruttiva (`0008_*` futura).

---

## Fasi precedenti (sintesi)

- **8.1b**: API + Worker + Web stub.
- **8.1c**: audit/idempotency centralizzati in `packages/shared`, `verify_audit_chain`, `/health/ready` con 503 quando non ready.
- **8.1d**: Dockerfile web robusto, payload audit normalizzato, commit-then-publish lato API, worker difensivo.
- **8.1d-patch1**: Makefile default goal corretto, worker FK-safe sul caso task non visibile.
- **8.2**: storage reale, document upload reale, `task_documents`, worker `analyzed_partial` con documenti.
- **8.2a-patch**: stabilizzazione pre-8.3. Test rerun-safe (hash unici per invocazione), dedup storage concorrenza-safe via `INSERT ... ON CONFLICT DO NOTHING`, validazione `document_ids` con `bindparam(expanding=True, type_=Uuid())`.
- **8.3**: `0004_claim_ledger.sql`. Extractor mock-driven, CVE-lite mock-driven, ledger append-only stretto, supersede via `claim_lineage`.

Per 8.4 vedi la sezione storica più sotto. Per 8.5, 8.6, 8.7 vedi [`PROJECT_STATE.md`](PROJECT_STATE.md).

---

## Fase 8.4 — Compiler, Final Answer Gate, primo `published_answers` (sezione storica)

> Le righe sotto descrivono lo stato al commit di chiusura 8.4 e restano valide per il nucleo 8.4. Le fasi successive (8.5/8.6/8.7) hanno aggiunto step alla pipeline (in particolare `task.source_quality_assessed` in 8.7E tra `task.analyzed_partial` e `task.compiling`) e non sono qui documentate. Vedi `PROJECT_STATE.md` per la pipeline corrente.

### Cosa cambia in 8.4

- **Migration `0005_answers_gate.sql` applicata.** Introduce `agent_runs`, `agent_outputs` (placeholder, vuota in 8.4), `truncation_events` (placeholder, vuota in 8.4), `continuation_attempts` (placeholder, vuota in 8.4), `coverage_gap_statements`, `draft_final_answers`, `final_answer_spans`, `final_answer_span_claim_links`, `final_gate_reports`, `published_answers`. Installa il trigger `lc_block_delete_if_published` (rinviato da 0004). Nessuna modifica a 0001, 0002, 0003, 0004.
- **`task_masters.status` ricreato in modo difensivo.** 0005 esegue un `ALTER TABLE ... DROP CONSTRAINT task_masters_status_check` seguito da un `ADD CONSTRAINT` con lo stesso codominio già accettato in 0003: `created`, `ingesting`, `analyzing`, `verifying`, `compiling`, `published`, `blocked`, `failed`, `cancelled`, `archived`, `analyzed_partial`. **Non viene introdotto alcuno status `publication_held`**: gli stati `compiling` e `published` esistevano già nel CHECK precedente e restano invariati.
- **Append-only stretto sui due livelli answers/gate.** Trigger `final_answer_spans_append_only` su `final_answer_spans` e trigger `final_gate_reports_append_only` su `final_gate_reports`, entrambi basati sul comune `reject_modify_append_only`. Nessuna riga di queste due tabelle viene mai mutata dopo l'INSERT.
- **Coerenza referenziale stretta a livello DB.** `draft_final_answers` ha UNIQUE composito `(id, task_id)`. `final_gate_reports` ha UNIQUE composito `(id, task_id, draft_final_answer_id)` e FK composita `(draft_final_answer_id, task_id) → draft_final_answers(id, task_id)`. `published_answers` ha UNIQUE composito `(id, task_id)`, FK composita `(draft_final_answer_id, task_id) → draft_final_answers(id, task_id)` e FK composita `(final_gate_report_id, task_id, draft_final_answer_id) → final_gate_reports(id, task_id, draft_final_answer_id)`. Conseguenza: è impossibile a DB avere un gate report o un published answer il cui `task_id` non corrisponde al `task_id` del draft sottostante.
- **Compiler mock-driven (`apps/worker/app/services/compiler.py`).** Deterministico, nessuna AI. `COMPILER_NAME="mvp0_compiler_v1"`, `COMPILER_VERSION="0.1.0"`. Per ogni task in `analyzed_partial` (o resuming da `compiling`) seleziona la **latest** `claim_ledger_entries` per ogni `claim_logical_id` filtrata a `state='verified_fact'`, ordinata per `(logical_claims.created_at, logical_claims.id)`. Costruisce `summary_text` deterministico concatenando `canonical_claim_text` con terminatore `\n`. Inserisce `draft_final_answers` v1, `final_answer_spans` 1:1 con i verified (offset coerenti), `final_answer_span_claim_links` con `link_role='primary_support'`. Tutti gli INSERT idempotenti via `ON CONFLICT DO NOTHING` sui vincoli UNIQUE dichiarati in 0005. `agent_outputs` non viene popolato in 8.4.
- **Final Answer Gate mock-driven (`apps/worker/app/services/final_answer_gate.py`).** Deterministico, nessuna AI. `GATE_NAME="mvp0_gate_v1"`, `GATE_VERSION="0.1.0"`. Regola di verifica corretta:

  > Uno span è verified-backed se e solo se esiste almeno un `final_answer_span_claim_links` tale che `link.claim_ledger_entry_id == latest_entry_id_for(claim_logical_id)` **e** `latest_entry_state_for(claim_logical_id) == 'verified_fact'`.

  Non basta che l'ultima entry del claim sia `verified_fact`: il link deve puntare esattamente a quella entry. Un link a v1 candidate quando esiste v2 verified non è sufficiente. Tre branch decisionali:
  - **Zero spans** (compiler non ha trovato `verified_fact`): `decision='rejected'`, `reason_code='no_verified_claims'`, una `coverage_gap_statements` con `kind='missing_evidence'`, `severity='block'`, `gap_key='no_verified_claims'`. Nessun `published_answers`.
  - **Tutti gli spans verified-backed**: `decision='approved'`, `reason_code='all_spans_verified'`, `published_answers` v1 con `status='published'`, `content_hash = sha256(summary_text utf-8)`.
  - **Spans non verified-backed presenti**: `decision='rejected'`, `reason_code='unverified_spans_present'`, una `coverage_gap_statements` per ciascuno span scoperto con `kind='unverified_claim'`, `severity='block'`, `gap_key='span:<final_answer_span_id>'`. Nessun `published_answers`.

  **Nota post-8.7F.** Il Final Answer Gate non è cambiato. Source Quality (8.7) NON è ancora consumata dal gate. L'integrazione decisionale è prevista in 8.7G.
- **Worker single-consumer 8.4 (`apps/worker/app/consumers/task_created.py`).** La pipeline 8.3 (extractor + CVE-lite → `analyzed_partial`) è preservata. Dopo `task.analyzed_partial`, il consumer prosegue nello stesso evento verso compiler + gate. FK-safe sul caso task non visibile (preserva il comportamento 8.1d-patch1). Resume-safe: se entra con task in `compiling`, finalizza usando l'eventuale `final_gate_reports` preesistente; se non esiste, riesegue compiler + gate idempotentemente. Guardia finale `WORKER_PIPELINE_INCOMPLETE`: il consumer non chiama mai `mark_succeeded` lasciando un task in `compiling` senza `final_gate_reports`. **Nota post-8.7E.** Tra `task.analyzed_partial` e `task.compiling` è ora inserito lo step Source Quality (mock evaluator + audit aggregato `task.source_quality_assessed`, SAVEPOINT-protetto). Vedi `PROJECT_STATE.md` per la sequenza audit completa post-8.7E.
- **Sequenza audit con documenti (approved scenario, originale 8.4).** Sulla chain del task, 13 eventi worker-side dopo `task.created`/`task.docs_attached` emessi dall'API:
  1. `task.analyzing`
  2. `task.docs_loaded`
  3. `task.claims_extracted`
  4. `task.claims_classified`
  5. `task.claims_ledger_initialized`
  6. `task.cve_lite_started`
  7. `task.cve_lite_completed`
  8. `task.analyzed_partial`
  9. `task.compiling`
  10. `task.draft_compiled`
  11. `task.final_gate_started`
  12. `task.final_gate_completed`
  13. `task.published`

  In 8.7E la sequenza diventa di 14 eventi con `task.source_quality_assessed` inserito tra `task.analyzed_partial` e `task.compiling`. Vedi `PROJECT_STATE.md`.
- **Sequenza audit con documenti (rejected zero-verified scenario, originale 8.4).** Identica fino a `task.final_gate_completed`, poi:
  - 13. `task.publication_held`

  **`task.publication_held` è esclusivamente un evento audit, non uno stato di `task_masters.status`.** La task in DB resta `analyzed_partial`.
- **Task senza documenti: comportamento invariato.** Sequenza worker: `task.analyzing`, `task.blocked`. Lo step Source Quality 8.7E NON viene eseguito (non c'è il path `_run_8_3_extract_and_verify`).
- **Idempotenza completa.** Doppio delivery dello stesso `task.created` non duplica righe in nessuna delle nuove tabelle 8.4 (`agent_runs`, `draft_final_answers`, `final_answer_spans`, `final_answer_span_claim_links`, `final_gate_reports`, `coverage_gap_statements`, `published_answers`) né eventi audit. Tutti gli INSERT del worker usano `ON CONFLICT DO NOTHING` su vincoli UNIQUE espliciti dichiarati in 0005. In 8.7E l'idempotenza dello step source quality è garantita via key deterministica `task:{task_id}:span:{evidence_span_id}:v1` + partial UNIQUE indexes `sqa_*_idem_uq` in `0007_source_quality.sql`.
- **Endpoint API read-only (nessun side effect).**
  - `GET /api/v1/tasks/{task_id}/draft` — ultima `draft_final_answers` per task con `final_answer_spans` ordinati per `span_index`.
  - `GET /api/v1/tasks/{task_id}/final-gate-report` — ultimo `final_gate_reports` per task con `coverage_gap_statements` collegati al draft.
  - `GET /api/v1/tasks/{task_id}/published-answer` — ultimo `published_answers` per task.
  - `GET /api/v1/published-answers/{published_answer_id}` — single-row view per id.

  Errori normalizzati con envelope `{"error": {"code": "...", "message": "...", "details": {...}, ...}}`. `ErrorCode.NOT_PUBLISHED` **non esiste** in MVP-0: per il caso "task esiste ma non è ancora pubblicato" si restituisce `RESOURCE_NOT_FOUND` con `details.resource='published_answers'`. Per task inesistente si restituisce `RESOURCE_NOT_FOUND` con `details.resource='task_masters'`. Per draft/gate non ancora prodotti su task esistente si usa `details.resource='draft_final_answers'` o `'final_gate_reports'`.

  Gli endpoint aggiuntivi 8.5/8.6/8.7F sono elencati in `PROJECT_STATE.md`.
- **Schemi shared aggiornati.** `packages/shared/evidencefirst_shared/schemas.py` espone `AgentRunRead`, `FinalAnswerSpanRead`, `FinalAnswerSpanClaimLinkRead`, `CoverageGapStatementRead`, `DraftFinalAnswerRead`, `DraftFinalAnswerWithSpansRead`, `FinalGateReportRead`, `PublishedAnswerRead`. In 8.5 sono stati aggiunti `PublishedAnswerLifecycleEventRead`, `SourceLossEventRead`, `SourceLossPropagationRecordRead`. In 8.7C sono stati aggiunti `SourceQualityAssessmentRead` e i codomini `SOURCE_QUALITY_*_VALUES`.

### Stati terminali del consumer in 8.4

Il consumer considera la task terminale (e chiama `mark_succeeded` sulla `event_processing_records`) nei seguenti casi:
- `blocked` — task senza documenti, branch invariato.
- `published` — approved scenario completato.
- `analyzed_partial` **e** esiste già un `final_gate_reports` per il task — rejected scenario completato (zero verified oppure unverified spans). In questo caso una redelivery viene loggata come `skipped_terminal`.

Lo stato `analyzed_partial` **non è** terminale di per sé. Una task in `analyzed_partial` senza `final_gate_reports` è in attesa: il consumer la fa proseguire verso `compiling` su una redelivery o sulla prima delivery valida. Lo stato `compiling` **non è mai** terminale di propria iniziativa: la guardia `WORKER_PIPELINE_INCOMPLETE` blocca `mark_succeeded` se il pipeline lascia la task in `compiling` senza un `final_gate_reports`.

### Cosa NON cambia in 8.4 (storico)

- Nessun **renderer** Markdown/HTML/PDF/DOCX/JSON-LD.
- Nessun **export** verso filesystem o storage cloud.
- Nessun **provider AI esterno**. **Costo API = 0.** `PROVIDERS_ENABLED=mock`.
- Nessun **Verified Web Mode**, **Hybrid Mode**, **consensus engine**, **contradiction detector** avanzato, **critical reviewer**.
- Nessun **`published_answer_lifecycle_events`**, nessun **`source_loss_events`** con propagator. (Introdotti in 8.5.)
- Nessun **stato `publication_held`** a livello DB. Solo evento audit `task.publication_held`.
- Nessun **trigger di propagazione** su `published_answers` (la withdrawal/supersede di un published answer non è automatizzata in 8.4: i campi `withdrawn_at`, `superseded_at`, `superseded_by_id` esistono ma non sono guidati da pipeline). In 8.5 è arrivato il path withdrawal asincrono via API + consumer dedicato.

### Cosa è arrivato dopo

`0006_lifecycle.sql` (8.5) ha introdotto `published_answer_lifecycle_events` (append-only) e `source_loss_events`/`source_loss_propagation_records` con il primo propagator che marca i claim impattati e le pubblicazioni dipendenti. La 8.6 minima ha aggiunto endpoint read-only su lifecycle e source-loss. La 8.7 ha introdotto `0007_source_quality.sql`, il mock Source Quality Evaluator (8.7D), l'orchestrator + integrazione in `task.created` con audit aggregato e SAVEPOINT (8.7E), e i due endpoint read 8.7F. La 8.7G consumerà gli assessment nel Final Answer Gate. Vedi `PROJECT_STATE.md` e `PHASE_8_7_PLAN.md` per i dettagli.

---

## Setup e comandi

````bash
cp .env.example .env
make up
make migrate
make seed
make test
````

| Comando | Effetto |
|---|---|
| `make up` | db, redis, api, worker, web |
| `make down` | ferma (mantiene i volumi) |
| `make logs` | tail dei log |
| `make migrate` / `make migrate ARGS="--status"` | migration runner |
| `make seed` | seed di sviluppo |
| `make test` | tutti i test |
| `make test-db` / `test-shared` / `test-api` / `test-worker` / `test-web` | per modulo |
| `make lock-web` | genera `apps/web/package-lock.json` |
| `make psql` / `make redis-cli` | shell |
| `make clean` | distrugge i volumi |

`make migrate` applica `0001`, `0002`, `0003`, `0004`, `0005`, `0006`, `0007` in ordine. Idempotente: rieseguirla è no-op.

---

## Smoke test 8.4 (approved scenario end-to-end)

````bash
# 1) Crea progetto
PID=$(curl -s -X POST localhost:8000/api/v1/projects \
  -H 'content-type: application/json' \
  -d '{"name":"smoke-84-demo"}' | jq -r .id)

# 2) Carica un documento .txt con frasi factual (cifre)
DID=$(curl -s -X POST "localhost:8000/api/v1/projects/$PID/documents" \
  -F "file=@evaluation/fixtures/closed_corpus_basic/doc_en.txt;type=text/plain" \
  | jq -r .id)

# 3) Crea il task con documento
TID=$(curl -s -X POST localhost:8000/api/v1/tasks \
  -H 'content-type: application/json' \
  -d "{\"project_id\":\"$PID\",\"objective\":\"smoke 8.4\",\"mode\":\"closed_corpus\",\"document_ids\":[\"$DID\"]}" \
  | jq -r .id)

# 4) Polling fino a stato terminale
while true; do
  S=$(curl -s "localhost:8000/api/v1/tasks/$TID" | jq -r .status)
  echo "status=$S"
  case "$S" in
    published|blocked) break ;;
    analyzed_partial)
      # Terminale solo se esiste un final_gate_reports per questo task.
      HAS_REPORT=$(curl -s -o /dev/null -w "%{http_code}" "localhost:8000/api/v1/tasks/$TID/final-gate-report")
      [ "$HAS_REPORT" = "200" ] && break
      ;;
  esac
  sleep 1
done

# 5) Audit chain (post-8.7E approved: 14 eventi worker-side dopo task.created/task.docs_attached)
curl -s "localhost:8000/api/v1/tasks/$TID/audit?limit=500" | jq '.items[].event_type'

# 6) Latest del ledger (claim verified visibili)
curl -s "localhost:8000/api/v1/tasks/$TID/claims" | jq

# 7) Endpoint answers 8.4
curl -s "localhost:8000/api/v1/tasks/$TID/draft" | jq
curl -s "localhost:8000/api/v1/tasks/$TID/final-gate-report" | jq
curl -s "localhost:8000/api/v1/tasks/$TID/published-answer" | jq

# 8) Single-row view del published answer
PAID=$(curl -s "localhost:8000/api/v1/tasks/$TID/published-answer" | jq -r .id)
curl -s "localhost:8000/api/v1/published-answers/$PAID" | jq

# 9) Source quality 8.7F (per task)
curl -s "localhost:8000/api/v1/tasks/$TID/source-quality" | jq
````

Smoke test rejected zero-verified: come sopra ma con un documento privo di frasi che superino CVE-lite (oppure forzando `quote_hash` non corrispondente in test). Il task termina in `analyzed_partial`; `GET /final-gate-report` restituisce `decision='rejected'`, `reason_code='no_verified_claims'`, una `coverage_gap_statements` con `kind='missing_evidence'`, `gap_key='no_verified_claims'`; `GET /published-answer` restituisce `404 RESOURCE_NOT_FOUND` con `details.resource='published_answers'`.

---

## Architettura runtime (sintesi)

┌────────────┐  POST /tasks (commit-then-publish)
│    API     │ ──────────────────────────────► Postgres
│  (FastAPI) │                                  ▲
│            │ ──xadd──► Redis Stream events:   │
└────────────┘            task.created          │
                                │               │
                                ▼               │
                         ┌──────────────┐       │
                         │   Worker     |──────►┘
                         │ single-csmr  │
                         │ FK-safe      │
                         │ resume-safe  │
                         │ pipeline 8.4 │
                         │ + 8.7E SQ    │
                         └──────────────┘

Pipeline 8.4 nel worker con documenti, approved scenario (estesa con 8.7E source quality):

task.created event
│
▼
begin_processing(idempotent, FK-safe)
│
▼
created → analyzing                  audit task.analyzing
│
▼
load chunks/spans                    audit task.docs_loaded
│
▼
extractor mock-driven                audit task.claims_extracted
audit task.claims_classified
audit task.claims_ledger_initialized
│
▼
CVE-lite mock-driven                 audit task.cve_lite_started
audit task.cve_lite_completed
│
▼
analyzing → analyzed_partial         audit task.analyzed_partial
│
▼
[8.7E] source quality (SAVEPOINT)    audit task.source_quality_assessed
│                                         (status=completed|failed)
▼
analyzed_partial → compiling         audit task.compiling
│
▼
compiler mock-driven                 audit task.draft_compiled
│
▼
Final Answer Gate                    audit task.final_gate_started
audit task.final_gate_completed
│
├── approved
│     └─► compiling → published  audit task.published
│         + published_answers v1
│
└── rejected
└─► compiling → analyzed_partial  audit task.publication_held
+ final_gate_reports rejected
(NO published_answers)
│
▼
mark_succeeded   (solo se stato terminale coerente:
blocked | published | analyzed_partial+gate_report)

Note 8.7E:
- Lo step source quality è SAVEPOINT-protected: un fallimento NON aborta la transazione del consumer e NON blocca 8.4. L'audit `task.source_quality_assessed` viene comunque emesso con `status='failed'`.
- Sui resume da `compiling` lo step source quality NON viene re-eseguito; il consumer entra direttamente in `_run_8_4_compile_and_gate`.

Resume scenario (consumer entra con task già in `compiling`):
- se esiste `final_gate_reports`: finalizza usandolo (drive a `published` se approved, a `analyzed_partial` con `task.publication_held` se rejected).
- se non esiste: riesegue compiler + gate idempotentemente; gli audit di stato già emessi (`task.compiling`, `task.source_quality_assessed`) non vengono ri-emessi grazie alle guardie status-based degli `UPDATE` su `task_masters` e al fatto che lo step 8.7E vive nel fresh-run path.

---

## Costi

`MAX_COST_PER_TASK=0`, `PROVIDERS_ENABLED=mock`. Nessun provider AI, nessun costo cloud, nessuna chiamata di rete in uscita verso terze parti.

---

## Documenti

- [`docs/migration_plan.md`](docs/migration_plan.md)
- [`PROJECT_STATE.md`](PROJECT_STATE.md)
- [`PHASE_8_7_PLAN.md`](PHASE_8_7_PLAN.md)
