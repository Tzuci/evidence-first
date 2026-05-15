# Evidence-First Multi-AI Platform — MVP-0

Piattaforma multi-AI **evidence-first** ed **evidence-gated**. Il sistema è progettato per impedire che claim fattuali non supportati, contraddetti o basati su fonti inadeguate vengano pubblicati come affidabili. **Non promette di eliminare le allucinazioni in senso assoluto**: impedisce o rende visibili claim non supportati, contraddetti o basati su fonti inadeguate prima della pubblicazione affidabile. Promette evidenze tracciabili, registrate nel Claim Ledger, verificate, valutate sulla qualità delle fonti, propagate via lifecycle/source-loss, **consultate dal Final Answer Gate** prima di qualunque pubblicazione, **e ora validate end-to-end da un realistic flow test che esercita warning path e block path attraverso l'intera catena API → FakeRedis → dispatcher → consumer → servizi worker → DB → read API**.

Una fonte citata **non implica** un claim vero: source quality e claim correctness restano assi separati anche dopo 8.7H.

> **Stato corrente.** Repository al commit **`b70ef8f`** ("Add phase 8.7 source quality realistic flow"), **Fase 8.7H conclusa, fase 8.7 chiusa**.
> Fasi implementate: 8.1–8.4 (foundation, storage, claim ledger, compiler + Final Answer Gate + first `published_answers`), 8.5 (lifecycle + source loss + propagator), 8.6 minima (read API lifecycle e source-loss), 8.7A–F (Source Quality Evaluator append-only + worker integration in `task.created` + read API), 8.7G (Source Quality consumata dal Final Answer Gate, policy P1+P3+P4, migration `0008_coverage_gap_source_quality.sql`), **8.7H (realistic flow test end-to-end `tests/test_phase_8_7_source_quality_flow.py` che valida warning + block path, chiusura formale della fase 8.7)**.
> Per lo stato di dettaglio fase-per-fase, l'elenco completo degli endpoint, la pipeline aggiornata e i debiti tecnici, vedi [`PROJECT_STATE.md`](PROJECT_STATE.md).
>
> Le sezioni sotto descrivono lo stato della Fase 8.4 al momento della sua chiusura e sono **storiche**: hanno valore di documentazione architetturale del nucleo evidence-gated minimo, ma NON riflettono lo stato corrente del repository. Le fasi successive (8.5, 8.6, 8.7) NON sono qui documentate; vedi `PROJECT_STATE.md`.

Disponibile localmente:
- Postgres 16, Redis 7
- API HTTP (FastAPI) con `health`, `projects`, `tasks`, `audit`, `documents`, `claims`, `answers`, lifecycle/source-loss read API (8.6), source quality read API (8.7F)
- Worker Redis Streams single-consumer, FK-safe, idempotente, resume-safe, con pipeline 8.4 + step Source Quality (8.7E) prima del compiler e Source Quality consumata dal Final Answer Gate (8.7G)
- Web Next.js minimale (home + `/diagnostic`)
- Storage filesystem deduplicato content-addressed con upsert concorrenza-safe
- Audit chain hash-linked, append-only, verificabile end-to-end

In 8.4 i task con documenti e claim verificati raggiungevano lo stato terminale `published`, con un `published_answers` v1 e un `final_gate_reports` `approved` (`reason_code='all_spans_verified'`). I task con documenti ma senza claim verificati raggiungevano `analyzed_partial` con un `final_gate_reports` `rejected` e l'evento audit `task.publication_held`. La sequenza audit è stata estesa in 8.7E con `task.source_quality_assessed` tra `task.analyzed_partial` e `task.compiling`. **In 8.7G il Final Answer Gate consulta `source_quality_assessments` come terzo asse decisionale**: con il mock evaluator attuale (che scrive `overall_quality='unknown'` e `contradiction_status='unchecked'`), il `reason_code` di default per task approved è ora **`all_spans_verified_with_warnings`**, e ogni task verified-backed accumula un `coverage_gap_statements` di kind `source_quality_warning` per span. **In 8.7H l'intera catena è stata validata end-to-end** dal realistic flow test `tests/test_phase_8_7_source_quality_flow.py`, che copre entrambi i path (warning con mock evaluator reale, block via stub dell'orchestrator) attraverso API HTTP → FakeRedis → dispatcher → `task.created` consumer → source quality → final gate → read API. Vedi `PROJECT_STATE.md` per la sequenza aggiornata e per i nuovi branch decisionali del Gate.

I due outcome source-quality oggi osservabili sono:

- **`source_quality_warning` → published con warning.** Branch B' del Gate: tutti gli span verified-backed, almeno uno con warning (latest `overall_quality ∈ {weak, unknown}`, `contradiction_status='unchecked'`, o latest mancante). `decision='approved'`, `reason_code='all_spans_verified_with_warnings'`, gap `kind='source_quality_warning'` severity='warn', `published_answers` v1 inserito. **Path attivo di default oggi** con il mock evaluator deterministico.
- **`source_quality_block` → publication_held.** Branch C' del Gate: tutti gli span verified-backed, almeno uno con block (latest `overall_quality='unsuitable'` o `contradiction_status ∈ {contradicted_by_stronger_source, conflicting_sources}`). `decision='rejected'`, `reason_code='source_quality_block'`, gap `kind='source_quality_block'` severity='block', audit `task.publication_held`, **nessun `published_answer`**. **Path implementato e testato (unit + realistic flow end-to-end via stub dell'orchestrator), ma in produzione con il mock attuale non si attiva spontaneamente**: il mock evaluator reale non emette `unsuitable` né i valori di `contradiction_status` che producono block. Si attiverà naturalmente con un evaluator reale o con il Contradiction Detector reale (8.8C).

**Renderer esterni Markdown/HTML/PDF/DOCX/JSON-LD restano fuori scope per MVP-0.** Il nucleo evidence-gated produce `draft_final_answers`, `final_answer_spans` e `published_answers` con `summary_text` testuale e `content_hash`, ma non emette artefatti esportabili in formati di rendering. Gli endpoint answers sono read-only e restituiscono JSON normalizzato.

---

## Scope MVP-0 (sintesi)

Incluso (oggi, post-8.7H):
- Closed Corpus, Postgres + Redis + filesystem locale.
- Audit chain hash-linked (append-only, verificabile).
- `event_processing_records` con idempotenza per consumer.
- API: `health/projects/tasks/audit/documents/claims/answers`, lifecycle/source-loss read (8.6), source quality read (8.7F).
- Storage content-addressed deduplicato, refcount-based.
- Document upload reale `.txt`/`.md` con chunking deterministico ed `evidence_spans` minimali.
- **Claim Ledger append-only** con extractor mock-driven e CVE-lite mock-driven.
- **Compiler mock-driven** che produce `draft_final_answers` v1 con `final_answer_spans` 1:1 sui claim `verified_fact`.
- **Final Answer Gate mock-driven**: decide `approved`/`rejected`, scrive `final_gate_reports` (append-only) e, su approved, inserisce `published_answers` v1 con `status='published'`. In 8.7G consulta `source_quality_assessments` per applicare la policy P1+P3+P4 (block su `unsuitable`/`contradicted_by_stronger_source`/`conflicting_sources`; warning su `weak`/`unknown`/`unchecked`/missing).
- **Lifecycle e source loss** (8.5): withdrawal asincrona, source loss event/propagation append-only, due API producer.
- **Read API 8.6** su lifecycle events e source-loss events/propagation/task-listing (read-only end-to-end).
- **Source Quality 8.7**: tabella `source_quality_assessments` append-only, mock evaluator deterministic (continua a produrre solo `overall_quality='unknown'` + `contradiction_status='unchecked'`), orchestrator chiamato in `task.created` dopo `analyzed_partial` (SAVEPOINT-protected, audit aggregato `task.source_quality_assessed`), due endpoint read 8.7F.
- **Source Quality consumata dal Gate (8.7G)**: migration `0008_coverage_gap_source_quality.sql` estende `coverage_gap_statements.kind` con `source_quality_block` e `source_quality_warning`; il Gate emette i nuovi gap e i nuovi reason_code `source_quality_block` (rejected) e `all_spans_verified_with_warnings` (approved).
- **Realistic flow test end-to-end (8.7H)**: `tests/test_phase_8_7_source_quality_flow.py` valida l'intera catena `API HTTP → FakeRedis → dispatcher → task.created consumer → source quality → final gate → read API` con due test indipendenti — warning flow (mock evaluator reale, Branch B') e block flow (Branch C' attivato via stub dell'orchestrator, perché il mock evaluator reale non produce `unsuitable`).
- Coerenza referenziale stretta a livello DB tra task ↔ draft ↔ gate ↔ published via UNIQUE composite e FK composite.

Escluso (rinviato a fasi successive — vedi `PHASE_8_7_PLAN.md` §13 per la roadmap anti-allucinazione):
- Provider AI reali, Verified Web Mode, Hybrid Mode, consensus engine.
- Contradiction detector reale (8.8C), Claim Entailment Checker (8.8A), Citation-to-Claim Validator (8.8B), Final Answer Sentence Gate (8.8D), Anti-Hallucination Report API (8.8E), External Verification / Web-RAG (8.9), Multi-agent consensus + adversarial review reale (9.0).
- Renderer e/o export Markdown/HTML/PDF/DOCX/JSON-LD.
- PDF/OCR, vector store cloud, S3/GCS/Azure.
- Human Review UI completa, retention/eval/export jobs.
- RBAC reale e redaction dei JSONB esposti dagli endpoint read e dei `details` dei `coverage_gap_statements` source_quality.
- Retention reale distruttiva (`0009_*` futura; il numero `0008` è ora occupato da `coverage_gap_source_quality`).
- Backfill source quality per task pre-8.7E.
- Recompile/draft v2 dopo `source_quality_block`.
- Smoke/realistic test end-to-end con Redis loop reale e worker main loop reale (i realistic flow 8.5/8.6/8.7H usano FakeRedis + `dispatch.handle_event` diretta).

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

> Le righe sotto descrivono lo stato al commit di chiusura 8.4 e restano valide per il nucleo 8.4. Le fasi successive (8.5/8.6/8.7) hanno aggiunto step alla pipeline (in particolare `task.source_quality_assessed` in 8.7E tra `task.analyzed_partial` e `task.compiling`) e branch decisionali al Gate (in 8.7G), **e hanno validato l'intera pipeline end-to-end** (8.7H, vedi sotto). Vedi `PROJECT_STATE.md` per la pipeline e le decisioni correnti.

### Cosa cambia in 8.4

- **Migration `0005_answers_gate.sql` applicata.** Introduce `agent_runs`, `agent_outputs` (placeholder, vuota in 8.4), `truncation_events` (placeholder, vuota in 8.4), `continuation_attempts` (placeholder, vuota in 8.4), `coverage_gap_statements`, `draft_final_answers`, `final_answer_spans`, `final_answer_span_claim_links`, `final_gate_reports`, `published_answers`. Installa il trigger `lc_block_delete_if_published` (rinviato da 0004). Nessuna modifica a 0001, 0002, 0003, 0004.
- **`task_masters.status` ricreato in modo difensivo.** 0005 esegue un `ALTER TABLE ... DROP CONSTRAINT task_masters_status_check` seguito da un `ADD CONSTRAINT` con lo stesso codominio già accettato in 0003. **Non viene introdotto alcuno status `publication_held`**.
- **Append-only stretto sui due livelli answers/gate.** Trigger `final_answer_spans_append_only` su `final_answer_spans` e trigger `final_gate_reports_append_only` su `final_gate_reports`, entrambi basati sul comune `reject_modify_append_only`.
- **Coerenza referenziale stretta a livello DB.** UNIQUE composite e FK composite tra `draft_final_answers`, `final_gate_reports`, `published_answers`.
- **Compiler mock-driven (`apps/worker/app/services/compiler.py`).** Deterministico, nessuna AI. `COMPILER_NAME="mvp0_compiler_v1"`, `COMPILER_VERSION="0.1.0"`.
- **Final Answer Gate mock-driven (`apps/worker/app/services/final_answer_gate.py`).** Deterministico, nessuna AI. `GATE_NAME="mvp0_gate_v1"`, `GATE_VERSION="0.1.0"`. Regola di verifica corretta:

  > Uno span è verified-backed se e solo se esiste almeno un `final_answer_span_claim_links` tale che `link.claim_ledger_entry_id == latest_entry_id_for(claim_logical_id)` **e** `latest_entry_state_for(claim_logical_id) == 'verified_fact'`.

  Tre branch decisionali originari di 8.4:
  - **Zero spans**: `decision='rejected'`, `reason_code='no_verified_claims'`, una `coverage_gap_statements` con `kind='missing_evidence'`, `severity='block'`, `gap_key='no_verified_claims'`. Nessun `published_answers`.
  - **Tutti gli spans verified-backed**: `decision='approved'`, `reason_code='all_spans_verified'`, `published_answers` v1 con `status='published'`, `content_hash = sha256(summary_text utf-8)`.
  - **Spans non verified-backed presenti**: `decision='rejected'`, `reason_code='unverified_spans_present'`, una `coverage_gap_statements` per ciascuno span scoperto con `kind='unverified_claim'`, `severity='block'`, `gap_key='span:<final_answer_span_id>'`. Nessun `published_answers`.

  **Nota post-8.7G/H.** Il Final Answer Gate ora consulta `source_quality_assessments` come terzo asse decisionale. La regola di verifica 8.4 è invariata, ma vengono aggiunti due branch:
  - **Approved con warning** (`reason_code='all_spans_verified_with_warnings'`): tutti gli span verified-backed, ma almeno uno presenta una source quality warning (latest `overall_quality ∈ {weak, unknown}`, `contradiction_status='unchecked'`, o latest mancante). Il `published_answers` v1 viene comunque inserito; il warning è solo un gap `kind='source_quality_warning'`, `severity='warn'`. **Validato end-to-end da 8.7H** (warning flow del realistic test).
  - **Rejected per source quality** (`reason_code='source_quality_block'`): tutti gli span verified-backed, ma almeno uno presenta una source quality block (latest `overall_quality='unsuitable'` o `contradiction_status ∈ {contradicted_by_stronger_source, conflicting_sources}`). Un gap `kind='source_quality_block'`, `severity='block'` per ogni span bloccato. Nessun `published_answers`. **Validato end-to-end da 8.7H** (block flow del realistic test, attivato tramite stub dell'orchestrator perché il mock evaluator reale non produce `unsuitable`).

  Priorità: CVE-lite > Source Quality. Uno span non verified-backed produce sempre `unverified_spans_present`, indipendentemente dalla qualità delle fonti. Vedi `PROJECT_STATE.md` per la tabella completa dei branch e dei reason_code.
- **Worker single-consumer 8.4 (`apps/worker/app/consumers/task_created.py`).** La pipeline 8.3 (extractor + CVE-lite → `analyzed_partial`) è preservata. Dopo `task.analyzed_partial`, il consumer prosegue nello stesso evento verso compiler + gate. FK-safe sul caso task non visibile (preserva il comportamento 8.1d-patch1). Resume-safe. Guardia finale `WORKER_PIPELINE_INCOMPLETE`. **Nota post-8.7E.** Tra `task.analyzed_partial` e `task.compiling` è inserito lo step Source Quality (mock evaluator + audit aggregato `task.source_quality_assessed`, SAVEPOINT-protetto). **Nota post-8.7G/H.** Il consumer non è modificato in 8.7G né in 8.7H: solo il Gate è esteso in 8.7G, e 8.7H aggiunge solo un nuovo file di test root-level.
- **Sequenza audit con documenti (approved scenario, originale 8.4).** Sulla chain del task, 13 eventi worker-side dopo `task.created`/`task.docs_attached`. In 8.7E la sequenza diventa di 14 eventi con `task.source_quality_assessed` inserito tra `task.analyzed_partial` e `task.compiling`. In 8.7G la sequenza è invariata rispetto a 8.7E; cambia solo il `reason_code` nel `final_gate_reports`. 8.7H verifica esplicitamente questa sequenza end-to-end (asserzione sulla posizione di `task.source_quality_assessed` strettamente tra `task.analyzed_partial` e `task.compiling`, e `task.published` come evento terminale nel warning flow; `task.publication_held` come evento terminale nel block flow).
- **Sequenza audit con documenti (rejected scenario, originale 8.4).** Identica fino a `task.final_gate_completed`, poi `task.publication_held`. **`task.publication_held` è esclusivamente un evento audit, non uno stato di `task_masters.status`.** In 8.7G il rejected può ora avere anche `reason_code='source_quality_block'` (oltre a `no_verified_claims` e `unverified_spans_present`); il block flow di 8.7H verifica esattamente questo scenario.
- **Task senza documenti: comportamento invariato.** Sequenza worker: `task.analyzing`, `task.blocked`.
- **Idempotenza completa.** Doppio delivery dello stesso `task.created` non duplica righe in nessuna tabella, comprese le nuove `coverage_gap_statements` di kind `source_quality_*` introdotte da 8.7G.
- **Endpoint API read-only.**
  - `GET /api/v1/tasks/{task_id}/draft`
  - `GET /api/v1/tasks/{task_id}/final-gate-report` — invariato in 8.7G/H come signature; i `coverage_gap_statements` collegati possono ora avere `kind ∈ {source_quality_block, source_quality_warning}`.
  - `GET /api/v1/tasks/{task_id}/published-answer`
  - `GET /api/v1/published-answers/{published_answer_id}`

  Errori normalizzati con envelope `{"error": {"code": "...", "message": "...", "details": {...}, ...}}`. `ErrorCode.NOT_PUBLISHED` **non esiste** in MVP-0: per "task esiste ma non è ancora pubblicato" si restituisce `RESOURCE_NOT_FOUND` con `details.resource='published_answers'`. **8.7H verifica esplicitamente questa convenzione** nel block flow: dopo `source_quality_block`, `GET /published-answer` ritorna 404 RESOURCE_NOT_FOUND con `details.resource='published_answers'`.

  Gli endpoint aggiuntivi 8.5/8.6/8.7F sono elencati in `PROJECT_STATE.md`.
- **Schemi shared aggiornati.** `packages/shared/evidencefirst_shared/schemas.py` espone i Read model 8.4 + 8.5 + 8.7C. Non sono stati aggiunti Read model nuovi in 8.7G né in 8.7H.

### Stati terminali del consumer in 8.4 (invariati in 8.7G/H)

Il consumer considera la task terminale (e chiama `mark_succeeded` sulla `event_processing_records`) nei seguenti casi:
- `blocked` — task senza documenti, branch invariato.
- `published` — approved scenario completato (oggi con mock: `reason_code='all_spans_verified_with_warnings'`; in futuro con evaluator reale clean: `reason_code='all_spans_verified'`).
- `analyzed_partial` **e** esiste già un `final_gate_reports` per il task — rejected scenario completato (zero verified oppure unverified spans oppure source_quality_block).

Lo stato `analyzed_partial` **non è** terminale di per sé. Lo stato `compiling` **non è mai** terminale di propria iniziativa.

### Cosa NON cambia in 8.4 (storico) — invariato anche in 8.7G/H

- Nessun **renderer** Markdown/HTML/PDF/DOCX/JSON-LD.
- Nessun **export** verso filesystem o storage cloud.
- Nessun **provider AI esterno**. **Costo API = 0.** `PROVIDERS_ENABLED=mock`.
- Nessun **Verified Web Mode**, **Hybrid Mode**, **consensus engine**, **contradiction detector** avanzato, **critical reviewer**.
- Nessuno **stato `publication_held`** a livello DB. Solo evento audit `task.publication_held`.
- Nessun **trigger di propagazione** su `published_answers` (la withdrawal/supersede di un published answer non è automatizzata in 8.4: i campi `withdrawn_at`, `superseded_at`, `superseded_by_id` esistono ma non sono guidati da pipeline). In 8.5 è arrivato il path withdrawal asincrono via API + consumer dedicato. **8.7G/H non hanno aggiunto nulla a questo path**: un task bloccato da `source_quality_block` non triggera automaticamente withdrawal su altri published answer.
- Nessun **stato `claim_ledger_entries.state`** del tipo `source_quality_downgraded`. La policy M1 (`PHASE_8_7_PLAN.md §5.2`, solo metadata) resta attiva anche dopo 8.7G/H. Il realistic flow 8.7H verifica esplicitamente che `claim_ledger_entries` non venga mutato dal Gate.

### Cosa è arrivato dopo

`0006_lifecycle.sql` (8.5) ha introdotto `published_answer_lifecycle_events` e `source_loss_events`/`source_loss_propagation_records`. La 8.6 minima ha aggiunto endpoint read-only su lifecycle e source-loss. La 8.7B ha introdotto `0007_source_quality.sql`, la 8.7D il mock Source Quality Evaluator, la 8.7E l'orchestrator + integrazione in `task.created` con audit aggregato e SAVEPOINT, la 8.7F i due endpoint read. La 8.7G ha introdotto `0008_coverage_gap_source_quality.sql` con i due nuovi kind `source_quality_block` e `source_quality_warning`, ed ha esteso `apps/worker/app/services/final_answer_gate.py` per consumare `source_quality_assessments` secondo la policy P1+P3+P4. **La 8.7H ha aggiunto il realistic flow test root-level `tests/test_phase_8_7_source_quality_flow.py` che valida end-to-end entrambi i path source-quality (warning e block) attraverso API HTTP → FakeRedis → dispatcher → consumer → servizi worker → DB → read API, chiudendo formalmente la fase 8.7.** Vedi `PROJECT_STATE.md` e `PHASE_8_7_PLAN.md` per i dettagli. Il prossimo blocco operativo consigliato è 8.8A-PRE / Claim Entailment Checker.

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
| `make test` | tutti i test (include i realistic flow 8.5/8.6/8.7H) |
| `make test-db` / `test-shared` / `test-api` / `test-worker` / `test-web` | per modulo |
| `make lock-web` | genera `apps/web/package-lock.json` |
| `make psql` / `make redis-cli` | shell |
| `make clean` | distrugge i volumi |

`make migrate` applica `0001`, `0002`, `0003`, `0004`, `0005`, `0006`, `0007`, `0008` in ordine. Idempotente: rieseguirla è no-op.

### Realistic flow tests (root-level)

I realistic flow test girano nello stesso pytest session della suite standard e richiedono solo `DATABASE_URL` raggiungibile (Postgres via `make up` + `make migrate`); usano una FakeRedis interna e invocano `dispatch.handle_event` direttamente, quindi non richiedono il worker main loop reale.

````bash
# Solo il realistic flow 8.7H (warning + block path end-to-end)
pytest tests/test_phase_8_7_source_quality_flow.py -v

# Tutti i realistic flow root-level (8.5 withdrawal/source-loss, 8.6 read API, 8.7H source quality)
pytest tests/ -v
````

Il file `tests/test_phase_8_7_source_quality_flow.py` contiene due test indipendenti:
- `test_phase_8_7_source_quality_warning_flow_end_to_end` — warning flow: mock source quality evaluator reale → `overall_quality='unknown'` + `contradiction_status='unchecked'` → Final Answer Gate approved con `reason_code='all_spans_verified_with_warnings'` → `coverage_gap_statements` kind `source_quality_warning` → `published_answers` v1.
- `test_phase_8_7_source_quality_block_flow_end_to_end` — block flow: monkeypatch del simbolo `run_source_quality_assessment` sul consumer con uno stub orchestrator che inserisce `source_quality_assessments` con `overall_quality='unsuitable'` (necessario perché il mock evaluator reale non produce `unsuitable`) → Final Answer Gate rejected con `reason_code='source_quality_block'` → `coverage_gap_statements` kind `source_quality_block` → `task.publication_held` → nessun `published_answer`.

---

## Smoke test 8.4 (approved scenario end-to-end, aggiornato post-8.7G/H)

````bash
# 1) Crea progetto
PID=$(curl -s -X POST localhost:8000/api/v1/projects \
  -H 'content-type: application/json' \
  -d '{"name":"smoke-87h-demo"}' | jq -r .id)

# 2) Carica un documento .txt con frasi factual (cifre)
DID=$(curl -s -X POST "localhost:8000/api/v1/projects/$PID/documents" \
  -F "file=@evaluation/fixtures/closed_corpus_basic/doc_en.txt;type=text/plain" \
  | jq -r .id)

# 3) Crea il task con documento
TID=$(curl -s -X POST localhost:8000/api/v1/tasks \
  -H 'content-type: application/json' \
  -d "{\"project_id\":\"$PID\",\"objective\":\"smoke 8.7H\",\"mode\":\"closed_corpus\",\"document_ids\":[\"$DID\"]}" \
  | jq -r .id)

# 4) Polling fino a stato terminale
while true; do
  S=$(curl -s "localhost:8000/api/v1/tasks/$TID" | jq -r .status)
  echo "status=$S"
  case "$S" in
    published|blocked) break ;;
    analyzed_partial)
      HAS_REPORT=$(curl -s -o /dev/null -w "%{http_code}" "localhost:8000/api/v1/tasks/$TID/final-gate-report")
      [ "$HAS_REPORT" = "200" ] && break
      ;;
  esac
  sleep 1
done

# 5) Audit chain (post-8.7E approved: 14 eventi worker-side; 8.7G/H non aggiungono eventi)
curl -s "localhost:8000/api/v1/tasks/$TID/audit?limit=500" | jq '.items[].event_type'

# 6) Latest del ledger (claim verified visibili)
curl -s "localhost:8000/api/v1/tasks/$TID/claims" | jq

# 7) Endpoint answers 8.4 (post-8.7G: reason_code default ora 'all_spans_verified_with_warnings'
#    quando con mock attuale; coverage_gap_statements include source_quality_warning)
curl -s "localhost:8000/api/v1/tasks/$TID/draft" | jq
curl -s "localhost:8000/api/v1/tasks/$TID/final-gate-report" | jq
curl -s "localhost:8000/api/v1/tasks/$TID/published-answer" | jq

# 8) Single-row view del published answer
PAID=$(curl -s "localhost:8000/api/v1/tasks/$TID/published-answer" | jq -r .id)
curl -s "localhost:8000/api/v1/published-answers/$PAID" | jq

# 9) Source quality 8.7F (per task)
curl -s "localhost:8000/api/v1/tasks/$TID/source-quality" | jq
````

Smoke test rejected zero-verified: come sopra ma con un documento privo di frasi che superino CVE-lite (oppure forzando `quote_hash` non corrispondente in test). Il task termina in `analyzed_partial`; `GET /final-gate-report` restituisce `decision='rejected'`, `reason_code='no_verified_claims'`, una `coverage_gap_statements` con `kind='missing_evidence'`, `gap_key='no_verified_claims'`; `GET /published-answer` restituisce `404 RESOURCE_NOT_FOUND` con `details.resource='published_answers'`. **Source Quality non viene consultata in questo branch (priorità CVE-lite).**

Smoke test rejected source_quality_block: non riproducibile da smoke test end-to-end via curl senza un evaluator reale o un seed manuale di `source_quality_assessments`, perché **il mock evaluator deterministico non produce `unsuitable`**. Coperto a livello unit dai test worker `apps/worker/tests/test_final_answer_gate_source_quality.py` (scenari 5/6/7/8/10/11/12) e a livello end-to-end dal realistic flow `tests/test_phase_8_7_source_quality_flow.py::test_phase_8_7_source_quality_block_flow_end_to_end` (8.7H), che attiva il branch via monkeypatch dell'orchestrator nel consumer.

---

## Architettura runtime (sintesi)

ARCHITETTURA RUNTIME — Evidence-First MVP-0

┌──────────────────────────────────────────────────────────────┐
│                         UTENTE / CLIENT                      │
└───────────────────────────────┬──────────────────────────────┘
                                │
                                │ HTTP
                                ▼

┌──────────────────────────────────────────────────────────────┐
│                         API FastAPI                          │
│                                                              │
│  Responsabilità:                                             │
│  - crea progetti                                             │
│  - carica documenti                                          │
│  - crea task                                                 │
│  - espone endpoint read-only                                 │
│  - pubblica eventi Redis dopo commit DB                      │
│                                                              │
│  Pattern: commit-then-publish                                │
└───────────────┬───────────────────────────────┬──────────────┘
                │                               │
                │ write/read                    │ XADD dopo commit
                ▼                               ▼

┌──────────────────────────────┐      ┌──────────────────────────────┐
│          PostgreSQL          │      │          Redis Stream         │
│                              │      │                              │
│  Fonte di verità:            │      │  Eventi asincroni:           │
│  - tenants / users           │      │  - task.created              │
│  - projects / tasks          │      │  - source_loss.detected      │
│  - documents / chunks        │      │  - withdrawal_requested      │
│  - evidence_spans            │      │                              │
│  - claim ledger              │      └──────────────┬───────────────┘
│  - verification records                            │
│  - source_quality_assessments                      │
│  - draft / gate / published                        │
│  - lifecycle / source loss                         │
│  - audit_records hash-linked                       │
└──────────────────────────────┘                     │
                ▲                                     │
                │                                     │ consume
                │                                     ▼

┌──────────────────────────────────────────────────────────────┐
│                         WORKER                               │
│                                                              │
│  Caratteristiche:                                            │
│  - single-consumer per stream                                │
│  - FK-safe                                                   │
│  - resume-safe                                               │
│  - idempotente                                               │
│  - usa transazioni DB                                        │
│  - usa SAVEPOINT per step non bloccanti                      │
│                                                              │
│  Pipeline task.created:                                      │
│                                                              │
│  1. task.analyzing                                           │
│  2. task.docs_loaded                                         │
│  3. task.claims_extracted                                    │
│  4. task.claims_classified                                   │
│  5. task.claims_ledger_initialized                           │
│  6. task.cve_lite_started                                    │
│  7. task.cve_lite_completed                                  │
│  8. task.analyzed_partial                                    │
│                                                              │
│  9. Source Quality step — 8.7E                               │
│     - legge evidence_span collegati ai claim                 │
│     - scrive source_quality_assessments                      │
│     - emette audit task.source_quality_assessed              │
│     - protetto da SAVEPOINT                                  │
│                                                              │
│  10. task.compiling                                          │
│  11. task.draft_compiled                                     │
│  12. task.final_gate_started                                 │
│                                                              │
│  13. Final Answer Gate — 8.4 + 8.7G                          │
│      - verifica che gli span siano verified-backed           │
│      - consulta source_quality_assessments                   │
│      - applica policy P1 + P3 + P4                           │
│      - può approvare, approvare con warning, o bloccare      │
│                                                              │
│  14. task.final_gate_completed                               │
│  15. task.published                                          │
│      oppure task.publication_held                            │
│                                                              │
│  Validato end-to-end da 8.7H (warning + block path) tramite  │
│  tests/test_phase_8_7_source_quality_flow.py.                │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               │ write/read
                               ▼

┌──────────────────────────────────────────────────────────────┐
│                         PostgreSQL                           │
│                                                              │
│  Il worker aggiorna solo tramite servizi controllati:        │
│                                                              │
│  - Claim Ledger append-only                                  │
│  - Verification records                                      │
│  - Source Quality append-only                                │
│  - Draft final answer                                        │
│  - Final gate report                                         │
│  - Coverage gap statements                                   │
│  - Published answer                                          │
│  - Audit chain                                               │
└──────────────────────────────────────────────────────────────┘

VERSIONE SINTETICA RUNTIME

Client
  │
  ▼
API FastAPI
  │
  ├──► PostgreSQL
  │       - task
  │       - documenti
  │       - evidenze
  │       - claim ledger
  │       - source quality
  │       - gate report
  │       - published answer
  │       - audit chain
  │
  └──► Redis Stream
          task.created
              │
              ▼
        Worker
          │
          ├── Extractor
          ├── CVE-lite verifier
          ├── Claim Ledger
          ├── Source Quality Evaluator     ← 8.7E
          ├── Compiler
          └── Final Answer Gate
                ├── verified-backed check  ← 8.4
                └── source quality policy  ← 8.7G
                      │
                      ├── approved
                      ├── approved with warnings   ← validato 8.7H
                      └── rejected / publication held   ← validato 8.7H (via stub)



Pipeline 8.4 + 8.7E + 8.7G nel worker con documenti, approved scenario (mock attuale → warning path):

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
[8.7G] Final Answer Gate             audit task.final_gate_started
       + Source Quality consultation audit task.final_gate_completed
│
├── approved (clean, evaluator reale)
│     └─► compiling → published          audit task.published
│         + published_answers v1
│         + reason_code='all_spans_verified'
│
├── approved with warnings (default oggi con mock) — VALIDATO da 8.7H warning flow
│     └─► compiling → published          audit task.published
│         + published_answers v1
│         + reason_code='all_spans_verified_with_warnings'
│         + coverage_gap_statements kind='source_quality_warning' per span
│
├── rejected unverified
│     └─► compiling → analyzed_partial   audit task.publication_held
│         + final_gate_reports rejected
│         + reason_code='unverified_spans_present' (priorità CVE-lite)
│         + coverage_gap_statements kind='unverified_claim'
│         (NO published_answers)
│
├── rejected source_quality_block — VALIDATO da 8.7H block flow (via stub orchestrator)
│     (in produzione con mock attuale NON si attiva spontaneamente)
│     └─► compiling → analyzed_partial   audit task.publication_held
│         + final_gate_reports rejected
│         + reason_code='source_quality_block'
│         + coverage_gap_statements kind='source_quality_block'
│         (NO published_answers)
│
└── rejected zero verified
      └─► compiling → analyzed_partial   audit task.publication_held
          + final_gate_reports rejected
          + reason_code='no_verified_claims'
          + coverage_gap_statements kind='missing_evidence'
          (NO published_answers)
│
▼
mark_succeeded   (solo se stato terminale coerente:
                  blocked | published | analyzed_partial+gate_report)

Note 8.7E:
- Lo step source quality è SAVEPOINT-protected: un fallimento NON aborta la transazione del consumer e NON blocca 8.4.
- Sui resume da `compiling` lo step source quality NON viene re-eseguito.

Note 8.7G:
- Il Gate consulta `source_quality_assessments` come read-only (zero mutazioni su quella tabella).
- Priorità: CVE-lite > Source Quality. Uno span non verified-backed produce `unverified_spans_present` indipendentemente dalla qualità delle fonti.
- Con il mock attuale (`overall_quality='unknown'` + `contradiction_status='unchecked'`), il branch attivo per task verified-backed è "approved with warnings".
- Il branch "rejected source_quality_block" si attiverà spontaneamente solo con un evaluator reale che produca `unsuitable`, `contradicted_by_stronger_source` o `conflicting_sources`.

Note 8.7H:
- L'intera pipeline è validata end-to-end dal realistic flow test `tests/test_phase_8_7_source_quality_flow.py`.
- Warning flow: mock evaluator reale → `unknown`+`unchecked` → Branch B' → published con warning.
- Block flow: stub dell'orchestrator (`monkeypatch.setattr(_wapp.consumers.task_created, "run_source_quality_assessment", _stub)`) → `unsuitable` → Branch C' → publication_held. Lo stub è necessario perché il mock evaluator reale non produce `unsuitable`; non altera il Gate né il compiler né le migration né i servizi.
- Il test valida API HTTP (POST projects/documents/tasks), FakeRedis (cattura `xadd`), dispatcher (`_dispatch.handle_event`), consumer, servizi worker, DB (audit chain, source_quality_assessments, final_gate_reports, coverage_gap_statements, published_answers), e read API (`/final-gate-report`, `/published-answer`, `/source-quality`).

Resume scenario (consumer entra con task già in `compiling`):
- se esiste `final_gate_reports`: finalizza usandolo (drive a `published` se approved, a `analyzed_partial` con `task.publication_held` se rejected).
- se non esiste: riesegue compiler + gate idempotentemente (compreso lo step 8.7G di consultazione source quality).

---

## Costi

`MAX_COST_PER_TASK=0`, `PROVIDERS_ENABLED=mock`. Nessun provider AI, nessun costo cloud, nessuna chiamata di rete in uscita verso terze parti.

---

## Documenti

- [`docs/migration_plan.md`](docs/migration_plan.md)
- [`PROJECT_STATE.md`](PROJECT_STATE.md)
- [`PHASE_8_7_PLAN.md`](PHASE_8_7_PLAN.md)
- [`PHASE_8_7G_PRE.md`](PHASE_8_7G_PRE.md)
- [`PHASE_8_7H_PRE.md`](PHASE_8_7H_PRE.md)
