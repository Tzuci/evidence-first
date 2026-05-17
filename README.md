# Evidence-First Multi-AI Platform — MVP-0

Piattaforma multi-AI **evidence-first** ed **evidence-gated**. Il sistema è progettato per impedire che claim fattuali non supportati, contraddetti o basati su fonti inadeguate vengano pubblicati come affidabili. **Non promette di eliminare le allucinazioni in senso assoluto**: impedisce o rende visibili claim non supportati, contraddetti o basati su fonti inadeguate prima della pubblicazione affidabile. Promette evidenze tracciabili, registrate nel Claim Ledger, verificate dal CVE-lite, valutate sulla qualità delle fonti, valutate anche sul piano della **relazione semantica claim ↔ quote** tramite il Claim Entailment Checker (8.8A), propagate via lifecycle/source-loss, **consultate dal Final Answer Gate** prima di qualunque pubblicazione, e validate end-to-end da realistic flow test che esercitano warning e block path attraverso l'intera catena API → FakeRedis → dispatcher → consumer → servizi worker → DB → read API.

Una fonte citata **non implica** un claim vero. Una quote testualmente presente **non implica** che la quote sostenga il claim. Un verdict `entailed` del checker mock **non implica** che il claim sia vero nel mondo: significa solo che la quote contiene testualmente il claim o gli è equivalente sotto la normalizzazione del mock heuristic. Source quality, evidence support, CVE-lite verification e claim entailment restano quattro assi separati anche dopo 8.8A.

> **Stato corrente.** Repository al commit **`394257b`** ("Add claim entailment gate realistic flow"), **Fase 8.8A tecnicamente conclusa**.
> Fasi implementate: 8.1–8.4 (foundation, storage, claim ledger, compiler + Final Answer Gate + first `published_answers`), 8.5 (lifecycle + source loss + propagator), 8.6 minima (read API lifecycle e source-loss), 8.7A–F (Source Quality Evaluator append-only + worker integration in `task.created` + read API), 8.7G (Source Quality consumata dal Final Answer Gate, policy P1+P3+P4, migration `0008_coverage_gap_source_quality.sql`), 8.7H (realistic flow test end-to-end `tests/test_phase_8_7_source_quality_flow.py`, chiusura fase 8.7), **8.8A (Claim Entailment Checker mock heuristic deterministic + worker integration + Gate consumption + realistic flow test, chiusura tecnica della sotto-fase)**.
> Per lo stato di dettaglio fase-per-fase, l'elenco completo degli endpoint, la pipeline aggiornata e i debiti tecnici, vedi [`PROJECT_STATE.md`](PROJECT_STATE.md). Per il piano architetturale 8.8A vedi [`PHASE_8_8A_PRE.md`](PHASE_8_8A_PRE.md) (con box "Stato post-implementazione") e [`PHASE_8_8A_GATE_PRE.md`](PHASE_8_8A_GATE_PRE.md).
>
> Le sezioni storiche più sotto (Fase 8.4 in particolare) descrivono lo stato del nucleo evidence-gated minimo al momento della chiusura della relativa fase e restano valide come documentazione architetturale, ma NON riflettono lo stato corrente del repository. Le fasi successive (8.5, 8.6, 8.7, 8.8A) hanno aggiunto step alla pipeline e branch decisionali al Gate; vedi `PROJECT_STATE.md` per la pipeline e le decisioni correnti.

Disponibile localmente:
- Postgres 16, Redis 7
- API HTTP (FastAPI) con `health`, `projects`, `tasks`, `audit`, `documents`, `claims`, `answers`, lifecycle/source-loss read API (8.6), source quality read API (8.7F)
- Worker Redis Streams single-consumer, FK-safe, idempotente, resume-safe, con pipeline 8.4 + step Source Quality (8.7E) + **step Claim Entailment (8.8A)** prima del compiler, e con il Final Answer Gate che consuma sia `source_quality_assessments` (8.7G) sia `claim_entailment_checks` (8.8A-GATE)
- **Claim Entailment Checker mock heuristic deterministic** — tre regole sintattiche (containment / numeric mismatch / default uncertain); non è un NLI/LLM reale
- **task.entailment_checked** nella pipeline audit chain (SAVEPOINT-protetto, idempotente)
- **Final Answer Gate consuma claim_entailment_checks** con policy `mvp0_entailment_gate_policy` v0.1.0 (solo `contradicted` blocca)
- **Coverage gaps entailment_block / entailment_warning** emessi dal Gate quando rilevanti (migration 0010)
- Web Next.js minimale (home + `/diagnostic`)
- Storage filesystem deduplicato content-addressed con upsert concorrenza-safe
- Audit chain hash-linked, append-only, verificabile end-to-end

In 8.4 i task con documenti e claim verificati raggiungevano lo stato terminale `published`, con un `published_answers` v1 e un `final_gate_reports` `approved` (`reason_code='all_spans_verified'`). I task con documenti ma senza claim verificati raggiungevano `analyzed_partial` con un `final_gate_reports` `rejected` e l'evento audit `task.publication_held`. La sequenza audit è stata estesa in 8.7E con `task.source_quality_assessed` tra `task.analyzed_partial` e `task.compiling`, e ulteriormente in 8.8A con `task.entailment_checked` tra `task.source_quality_assessed` e `task.compiling`. **In 8.7G il Final Answer Gate consulta `source_quality_assessments` come terzo asse decisionale; in 8.8A-GATE consulta anche `claim_entailment_checks` come quarto asse**. Con il mock evaluator + mock checker attuali, il `reason_code` di default per task approved è **`all_spans_verified_with_warnings`** (le righe SQ mock-driven producono sempre un `source_quality_warning` per span; le righe entailment mock-driven producono tipicamente verdict `entailed` via containment rule e quindi non aggiungono warning entailment di default). **In 8.8A-GATE-FLOW l'intera catena è stata validata end-to-end** dal realistic flow test `tests/test_phase_8_8a_entailment_gate_flow.py`, che copre entrambi i path (warning con mock checker reale, block via stub dell'orchestrator) attraverso API HTTP → FakeRedis → dispatcher → `task.created` consumer → source quality → claim entailment → final gate → read API. Vedi `PROJECT_STATE.md` per la sequenza aggiornata e per i branch decisionali del Gate.

I quattro outcome MVP-0 oggi osservabili sono:

- **`all_spans_verified` → published clean.** Branch B del Gate: tutti gli span verified-backed, nessun warning di alcun asse (entailment `entailed` clean su ogni pair AND Source Quality clean su ogni span). `decision='approved'`, `reason_code='all_spans_verified'`, nessun coverage gap, `published_answers` v1 inserito. **Non è il path di default oggi**: richiede una Source Quality clean (es. `overall_quality ∈ {strong, adequate}` con `contradiction_status='no_known_contradiction'`), che il mock evaluator non produce mai (emette sempre `unknown` + `unchecked`).
- **`all_spans_verified_with_warnings` → published con warning.** Branch W del Gate: tutti verified-backed, nessun block (né entailment né SQ), almeno un warning (entailment_warning OR source_quality_warning OR entrambi). `decision='approved'`, `reason_code='all_spans_verified_with_warnings'`, coverage gaps `kind='entailment_warning'` e/o `kind='source_quality_warning'` severity='warn', `published_answers` v1 inserito. **Path attivo di default oggi**: il mock evaluator produce `source_quality_warning` per ogni span, e il mock checker tipicamente produce `entailed` (clean) tramite containment rule — quindi i warning vengono prevalentemente dall'asse Source Quality.
- **`source_quality_block` → publication_held.** Branch C' del Gate: tutti verified-backed, nessun entailment_block, almeno uno span con SQ block (latest `overall_quality='unsuitable'` o `contradiction_status ∈ {contradicted_by_stronger_source, conflicting_sources}`). `decision='rejected'`, `reason_code='source_quality_block'`, gap `kind='source_quality_block'` severity='block', audit `task.publication_held`, **nessun `published_answer`**. **Path implementato e testato (unit + realistic flow end-to-end via stub dell'orchestrator), ma in produzione con il mock attuale non si attiva spontaneamente**: il mock evaluator reale non emette `unsuitable` né i valori di `contradiction_status` che producono block. Si attiverà naturalmente con un evaluator reale o con il Contradiction Detector reale (8.8C).
- **`entailment_block` → publication_held.** Branch E del Gate (8.8A-GATE-CODE): tutti verified-backed, almeno uno span con latest entailment verdict `contradicted` su almeno una pair (entry, evidence_span) supportante. `decision='rejected'`, `reason_code='entailment_block'`, gap `kind='entailment_block'` severity='block', audit `task.publication_held`, **nessun `published_answer`**. Il Gate emette comunque, per audit completo, anche eventuali gap source_quality (block o warning) presenti sullo stesso draft, ma il reason_code finale resta `entailment_block` (priorità Entailment > Source Quality). **Path implementato e testato (unit 13 scenari + realistic flow end-to-end via stub dell'orchestrator), ma in produzione con il mock checker attuale non si attiva spontaneamente**: il mock heuristic NON emette mai `contradicted` (verificato direttamente nelle costanti di output del file `apps/worker/app/services/claim_entailment_checker.py`). Si attiverà naturalmente con un checker reale (NLI/LLM, fase 8.9 o successive) o con un bump di policy a P2 (`not_supported → block`) versionato come `mvp0_entailment_gate_policy` v0.2.0.

**Renderer esterni Markdown/HTML/PDF/DOCX/JSON-LD restano fuori scope per MVP-0.** Il nucleo evidence-gated produce `draft_final_answers`, `final_answer_spans` e `published_answers` con `summary_text` testuale e `content_hash`, ma non emette artefatti esportabili in formati di rendering. Gli endpoint answers sono read-only e restituiscono JSON normalizzato.

---

## Scope MVP-0 (sintesi)

Incluso (oggi, post-8.8A):
- Closed Corpus, Postgres + Redis + filesystem locale.
- Audit chain hash-linked (append-only, verificabile).
- `event_processing_records` con idempotenza per consumer.
- API: `health/projects/tasks/audit/documents/claims/answers`, lifecycle/source-loss read (8.6), source quality read (8.7F).
- Storage content-addressed deduplicato, refcount-based.
- Document upload reale `.txt`/`.md` con chunking deterministico ed `evidence_spans` minimali.
- **Claim Ledger append-only** con extractor mock-driven e CVE-lite mock-driven.
- **Compiler mock-driven** che produce `draft_final_answers` v1 con `final_answer_spans` 1:1 sui claim `verified_fact`.
- **Final Answer Gate mock-driven**: decide `approved`/`rejected`, scrive `final_gate_reports` (append-only) e, su approved, inserisce `published_answers` v1 con `status='published'`. In 8.7G consulta `source_quality_assessments`. In 8.8A-GATE consulta anche `claim_entailment_checks`.
- **Lifecycle e source loss** (8.5): withdrawal asincrona, source loss event/propagation append-only, due API producer.
- **Read API 8.6** su lifecycle events e source-loss events/propagation/task-listing (read-only end-to-end).
- **Source Quality 8.7**: tabella `source_quality_assessments` append-only, mock evaluator deterministic (continua a produrre solo `overall_quality='unknown'` + `contradiction_status='unchecked'`), orchestrator chiamato in `task.created` dopo `analyzed_partial` (SAVEPOINT-protected, audit aggregato `task.source_quality_assessed`), due endpoint read 8.7F.
- **Source Quality consumata dal Gate (8.7G)**: migration `0008_coverage_gap_source_quality.sql` estende `coverage_gap_statements.kind` con `source_quality_block` e `source_quality_warning`; il Gate emette i nuovi gap e i nuovi reason_code `source_quality_block` (rejected) e `all_spans_verified_with_warnings` (approved).
- **Claim Entailment 8.8A** (NUOVO): tabella `claim_entailment_checks` append-only (migration `0009_claim_entailment_checks.sql`), mock checker heuristic deterministic (tre regole sintattiche: containment match → `entailed`; numeric mismatch → `not_supported`; default → `uncertain`; il mock NON emette mai `contradicted` né `partially_supported`), orchestrator chiamato in `task.created` dopo `task.source_quality_assessed` (SAVEPOINT-protected, audit aggregato `task.entailment_checked`), shared schemas `SOURCE_ENTAILMENT_VERDICT_VALUES` + `ClaimEntailmentVerdict` Literal alias + `ClaimEntailmentCheckRead` in `packages/shared/evidencefirst_shared/schemas.py`. Mock heuristic — **non è un NLI/LLM reale**. Ogni riga emessa porta `payload.mock=true` e `payload.semantic_warning="mvp0 heuristic; not a real NLI/LLM entailment model"`.
- **Claim Entailment consumata dal Gate (8.8A-GATE-CODE)**: migration `0010_coverage_gap_entailment.sql` estende `coverage_gap_statements.kind` con `entailment_block` e `entailment_warning`; il Gate emette i nuovi gap, il nuovo reason_code `entailment_block` (rejected, priorità Entailment > Source Quality) e riusa `all_spans_verified_with_warnings` (approved) per warning misti. Identità della policy del Gate: `mvp0_entailment_gate_policy` v0.1.0. Priorità invariante: **CVE-lite > Claim Entailment > Source Quality**.
- **Realistic flow test end-to-end (8.8A-GATE-FLOW)**: `tests/test_phase_8_8a_entailment_gate_flow.py` valida l'intera catena `API HTTP → FakeRedis → dispatcher → task.created consumer → source quality → claim entailment → final gate → read API` con due test indipendenti — warning flow (mock checker reale, asse entailment `clean` o `warnings` a seconda di cosa produce il containment rule; Source Quality contribuisce un warning per span) e block flow (Branch E attivato via stub dell'orchestrator, perché il mock checker reale non produce `contradicted`).
- Coerenza referenziale stretta a livello DB tra task ↔ draft ↔ gate ↔ published via UNIQUE composite e FK composite.

Escluso (rinviato a fasi successive — vedi `PHASE_8_7_PLAN.md §13` e `PHASE_8_8A_PRE.md` per la roadmap anti-allucinazione):
- Provider AI reali, Verified Web Mode, Hybrid Mode, consensus engine.
- **Evaluator NLI reale**: il checker MVP-0 è un mock heuristic deterministico a tre regole. Un checker reale NLI/LLM (8.9 o successive) è ancora mancante; senza di esso il branch `entailment_block` del Gate è dormiente in produzione mock-driven.
- **API read entailment task-level e per logical_claim**: rinviata a **8.8A-READ**. Oggi i dati entailment sono osservabili solo via i `coverage_gap_statements` (kind `entailment_block` / `entailment_warning`) e il `final_gate_reports.payload` (sezione `entailment`), entrambi esposti da `GET /api/v1/tasks/{task_id}/final-gate-report`. Non esiste `/api/v1/tasks/{task_id}/entailment` né `/api/v1/claims/{logical_id}/entailment-checks`.
- **Anti-Hallucination Report API aggregata** (8.8B-REPORT): nessuna API espone un report aggregato di CVE-lite + Source Quality + Entailment per un singolo `published_answer`. Mancante.
- **Citation-to-Claim Validator** (8.8B): verifica che il claim citi le evidenze giuste, non solo "vicine". Mancante.
- **Contradiction detector reale** (8.8C): cross-source contradiction su `contradiction_records`. Mancante.
- **Final Answer Sentence Gate** (8.8D): gate a livello frase del published_answer. Mancante.
- **External Verification / Web-RAG** (8.9): Verified Web Mode controllato. Mancante.
- **Multi-agent consensus + adversarial review reale** (9.0). Mancante.
- **UI completa**: nessuna interfaccia utente espone i gap entailment né i gap source_quality. Mancante.
- Renderer e/o export Markdown/HTML/PDF/DOCX/JSON-LD.
- PDF/OCR, vector store cloud, S3/GCS/Azure.
- Human Review UI completa, retention/eval/export jobs.
- RBAC reale e redaction dei JSONB esposti dagli endpoint read e dei `details` dei `coverage_gap_statements` (source_quality e entailment).
- Retention reale distruttiva (`0011_*` o successiva; il numero `0009` è occupato da `claim_entailment_checks` e `0010` da `coverage_gap_entailment`).
- Backfill source quality e entailment per task pre-8.7E / pre-8.8A.
- Recompile/draft v2 dopo `source_quality_block` o `entailment_block`.
- Smoke/realistic test end-to-end con Redis loop reale e worker main loop reale (i realistic flow 8.5/8.6/8.7H/8.8A usano FakeRedis + `dispatch.handle_event` diretta).

---

## Fasi precedenti (sintesi)

- **8.1b**: API + Worker + Web stub.
- **8.1c**: audit/idempotency centralizzati in `packages/shared`, `verify_audit_chain`, `/health/ready` con 503 quando non ready.
- **8.1d**: Dockerfile web robusto, payload audit normalizzato, commit-then-publish lato API, worker difensivo.
- **8.1d-patch1**: Makefile default goal corretto, worker FK-safe sul caso task non visibile.
- **8.2**: storage reale, document upload reale, `task_documents`, worker `analyzed_partial` con documenti.
- **8.2a-patch**: stabilizzazione pre-8.3. Test rerun-safe (hash unici per invocazione), dedup storage concorrenza-safe via `INSERT ... ON CONFLICT DO NOTHING`, validazione `document_ids` con `bindparam(expanding=True, type_=Uuid())`.
- **8.3**: `0004_claim_ledger.sql`. Extractor mock-driven, CVE-lite mock-driven, ledger append-only stretto, supersede via `claim_lineage`.

Per 8.4 vedi la sezione storica più sotto. Per 8.5, 8.6, 8.7, 8.8A vedi [`PROJECT_STATE.md`](PROJECT_STATE.md).

---

## Fase 8.4 — Compiler, Final Answer Gate, primo `published_answers` (sezione storica)

> Le righe sotto descrivono lo stato al commit di chiusura 8.4 e restano valide per il nucleo 8.4. Le fasi successive (8.5/8.6/8.7/8.8A) hanno aggiunto step alla pipeline (in particolare `task.source_quality_assessed` in 8.7E e `task.entailment_checked` in 8.8A) e branch decisionali al Gate (in 8.7G e 8.8A-GATE-CODE), e hanno validato l'intera pipeline end-to-end (8.7H + 8.8A-GATE-FLOW). Vedi `PROJECT_STATE.md` per la pipeline e le decisioni correnti.

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

  **Nota post-8.7G/H + 8.8A-GATE-CODE.** Il Final Answer Gate ora consulta `source_quality_assessments` (8.7G) E `claim_entailment_checks` (8.8A-GATE) come terzo e quarto asse decisionale. La regola di verifica 8.4 è invariata, ma vengono aggiunti branch:
  - **Approved con warning** (`reason_code='all_spans_verified_with_warnings'`): tutti gli span verified-backed, ma almeno uno presenta un warning (source quality OR entailment OR entrambi). Il `published_answers` v1 viene comunque inserito; i warning sono solo gap `kind='source_quality_warning'` e/o `kind='entailment_warning'`, `severity='warn'`. **Validato end-to-end da 8.7H + 8.8A-GATE-FLOW**.
  - **Rejected per source quality** (`reason_code='source_quality_block'`): tutti verified-backed, nessun entailment_block, ma almeno uno span con SQ block. **Validato end-to-end da 8.7H** (block flow del realistic test, attivato tramite stub dell'orchestrator perché il mock evaluator reale non produce `unsuitable`).
  - **Rejected per entailment** (`reason_code='entailment_block'`): tutti verified-backed, ma almeno uno span con latest entailment verdict `contradicted` su almeno una pair (entry, evidence_span) supportante. Priorità Entailment > Source Quality: se entrambi i block fioccano, reason_code è `entailment_block` ma il Gate emette tutti i gap rilevanti. **Validato end-to-end da 8.8A-GATE-FLOW** (block flow del realistic test, attivato tramite stub dell'orchestrator perché il mock checker reale non produce `contradicted`).

  Priorità: **CVE-lite > Claim Entailment > Source Quality**. Uno span non verified-backed produce sempre `unverified_spans_present`, indipendentemente dalla qualità delle fonti e dall'entailment. Vedi `PROJECT_STATE.md` per la tabella completa dei branch e dei reason_code.
- **Worker single-consumer 8.4 (`apps/worker/app/consumers/task_created.py`).** La pipeline 8.3 (extractor + CVE-lite → `analyzed_partial`) è preservata. Dopo `task.analyzed_partial`, il consumer prosegue nello stesso evento verso compiler + gate. FK-safe sul caso task non visibile (preserva il comportamento 8.1d-patch1). Resume-safe. Guardia finale `WORKER_PIPELINE_INCOMPLETE`. **Nota post-8.7E + 8.8A.** Tra `task.analyzed_partial` e `task.compiling` sono inseriti, nell'ordine, lo step Source Quality (8.7E, audit `task.source_quality_assessed`, SAVEPOINT-protetto) e lo step Claim Entailment (8.8A, audit `task.entailment_checked`, SAVEPOINT-protetto). **Nota post-8.7G/H + 8.8A-GATE-CODE.** Il consumer non è modificato in 8.7G né in 8.7H né in 8.8A-GATE-CODE: solo il Gate è esteso. 8.7H aggiunge un file di test root-level; 8.8A-GATE-FLOW aggiunge un secondo file di test root-level.
- **Sequenza audit con documenti (approved scenario, post-8.8A).** Sulla chain del task, **15 eventi worker-side** dopo `task.created`/`task.docs_attached`. La sequenza era di 13 eventi in 8.4 originale, di 14 in 8.7E (con `task.source_quality_assessed`), e ora di 15 in 8.8A (con `task.entailment_checked` strettamente tra `task.source_quality_assessed` e `task.compiling`). In 8.7G la sequenza era invariata rispetto a 8.7E; cambia solo il `reason_code` nel `final_gate_reports`. In 8.8A-GATE-CODE la sequenza è invariata rispetto a 8.8A-WORKER; cambiano i possibili reason_code e i kind dei gap emessi dal Gate. 8.7H + 8.8A-GATE-FLOW verificano esplicitamente questa sequenza end-to-end (asserzione sull'ordinamento `analyzed_partial < source_quality_assessed < entailment_checked < compiling < final_gate_completed < published` nel warning flow; `analyzed_partial < source_quality_assessed < entailment_checked < compiling < final_gate_completed < publication_held` nel block flow).
- **Sequenza audit con documenti (rejected scenario, originale 8.4).** Identica fino a `task.final_gate_completed`, poi `task.publication_held`. **`task.publication_held` è esclusivamente un evento audit, non uno stato di `task_masters.status`.** In 8.7G il rejected può ora avere anche `reason_code='source_quality_block'`; in 8.8A-GATE-CODE può avere `reason_code='entailment_block'`; le sequenze audit del block flow di 8.7H e 8.8A-GATE-FLOW verificano questi scenari.
- **Task senza documenti: comportamento invariato.** Sequenza worker: `task.analyzing`, `task.blocked`.
- **Idempotenza completa.** Doppio delivery dello stesso `task.created` non duplica righe in nessuna tabella, comprese le `coverage_gap_statements` di kind `source_quality_*` (8.7G) e `entailment_*` (8.8A-GATE-CODE).
- **Endpoint API read-only.**
  - `GET /api/v1/tasks/{task_id}/draft`
  - `GET /api/v1/tasks/{task_id}/final-gate-report` — invariato in 8.7G/H + 8.8A-GATE-CODE come signature; i `coverage_gap_statements` collegati possono ora avere `kind ∈ {source_quality_block, source_quality_warning, entailment_block, entailment_warning}` in aggiunta ai kind preesistenti. Il `final_gate_reports.payload` espone una sezione `entailment` con `policy_name`, `policy_version`, `status` ∈ {clean, warnings, blocked}, `spans_with_block`, `spans_with_warnings`, `block_reason_counts`, `warning_reason_counts`.
  - `GET /api/v1/tasks/{task_id}/published-answer`
  - `GET /api/v1/published-answers/{published_answer_id}`

  Errori normalizzati con envelope `{"error": {"code": "...", "message": "...", "details": {...}, ...}}`. `ErrorCode.NOT_PUBLISHED` **non esiste** in MVP-0: per "task esiste ma non è ancora pubblicato" si restituisce `RESOURCE_NOT_FOUND` con `details.resource='published_answers'`. **8.7H + 8.8A-GATE-FLOW verificano esplicitamente questa convenzione** nei rispettivi block flow.

  Gli endpoint aggiuntivi 8.5/8.6/8.7F sono elencati in `PROJECT_STATE.md`. **Nessuna read API entailment è stata esposta in 8.8A**: la diagnostica claim-entailment passa oggi attraverso `/final-gate-report` (coverage_gap_statements + payload entailment) o via accesso diretto al DB. Le read API entailment sono rinviate a **8.8A-READ**.
- **Schemi shared aggiornati.** `packages/shared/evidencefirst_shared/schemas.py` espone i Read model 8.4 + 8.5 + 8.7C + **8.8A-SHARED** (`SOURCE_ENTAILMENT_VERDICT_VALUES`, `ClaimEntailmentVerdict` Literal alias, `ClaimEntailmentCheckRead`). Non sono stati aggiunti Read model nuovi in 8.7G/H né in 8.8A-GATE-CODE.

### Stati terminali del consumer in 8.4 (invariati in 8.7G/H + 8.8A-GATE-CODE)

Il consumer considera la task terminale (e chiama `mark_succeeded` sulla `event_processing_records`) nei seguenti casi:
- `blocked` — task senza documenti, branch invariato.
- `published` — approved scenario completato (oggi con mock: `reason_code='all_spans_verified_with_warnings'` per via dei warning Source Quality; il `reason_code='all_spans_verified'` clean richiede un evaluator SQ reale che produca `strong`/`adequate`).
- `analyzed_partial` **e** esiste già un `final_gate_reports` per il task — rejected scenario completato (zero verified oppure unverified spans oppure source_quality_block oppure entailment_block).

Lo stato `analyzed_partial` **non è** terminale di per sé. Lo stato `compiling` **non è mai** terminale di propria iniziativa.

### Cosa NON cambia in 8.4 (storico) — invariato anche in 8.7G/H + 8.8A-GATE-CODE

- Nessun **renderer** Markdown/HTML/PDF/DOCX/JSON-LD.
- Nessun **export** verso filesystem o storage cloud.
- Nessun **provider AI esterno**. **Costo API = 0.** `PROVIDERS_ENABLED=mock`.
- Nessun **Verified Web Mode**, **Hybrid Mode**, **consensus engine**, **contradiction detector** avanzato, **critical reviewer**.
- Nessuno **stato `publication_held`** a livello DB. Solo evento audit `task.publication_held`.
- Nessun **trigger di propagazione** su `published_answers` (la withdrawal/supersede di un published answer non è automatizzata in 8.4: i campi `withdrawn_at`, `superseded_at`, `superseded_by_id` esistono ma non sono guidati da pipeline). In 8.5 è arrivato il path withdrawal asincrono via API + consumer dedicato. **8.7G/H + 8.8A-GATE-CODE non hanno aggiunto nulla a questo path**: un task bloccato da `source_quality_block` o `entailment_block` non triggera automaticamente withdrawal su altri published answer.
- Nessun **stato `claim_ledger_entries.state`** del tipo `source_quality_downgraded` né `entailment_failed`. La policy M1 (`PHASE_8_7_PLAN.md §5.2`, solo metadata) resta attiva anche dopo 8.7G/H + 8.8A-GATE-CODE. Il realistic flow 8.7H + 8.8A-GATE-FLOW verifica esplicitamente che `claim_ledger_entries` non venga mutato dal Gate, e il test gate 8.8A-GATE-CODE scenario 12 conferma anche che `claim_entailment_checks` non venga mutato (Gate read-only su quella tabella).

### Cosa è arrivato dopo

`0006_lifecycle.sql` (8.5) ha introdotto `published_answer_lifecycle_events` e `source_loss_events`/`source_loss_propagation_records`. La 8.6 minima ha aggiunto endpoint read-only su lifecycle e source-loss. La 8.7B ha introdotto `0007_source_quality.sql`, la 8.7D il mock Source Quality Evaluator, la 8.7E l'orchestrator + integrazione in `task.created` con audit aggregato e SAVEPOINT, la 8.7F i due endpoint read. La 8.7G ha introdotto `0008_coverage_gap_source_quality.sql` con i due nuovi kind `source_quality_block` e `source_quality_warning`, ed ha esteso `apps/worker/app/services/final_answer_gate.py` per consumare `source_quality_assessments` secondo la policy P1+P3+P4. La 8.7H ha aggiunto il realistic flow test root-level `tests/test_phase_8_7_source_quality_flow.py` che valida end-to-end entrambi i path source-quality (warning e block) attraverso API HTTP → FakeRedis → dispatcher → consumer → servizi worker → DB → read API, chiudendo formalmente la fase 8.7. **La 8.8A ha introdotto `0009_claim_entailment_checks.sql`, il mock claim entailment checker (heuristic deterministic, 3 regole sintattiche), l'orchestrator + integrazione in `task.created` con audit aggregato `task.entailment_checked` e SAVEPOINT, e l'estensione del Final Answer Gate per consumare `claim_entailment_checks` con policy `mvp0_entailment_gate_policy` v0.1.0 (solo `contradicted` blocca). Migration `0010_coverage_gap_entailment.sql` ha esteso `coverage_gap_statements.kind` con `entailment_block` e `entailment_warning`. Realistic flow test root-level `tests/test_phase_8_8a_entailment_gate_flow.py` valida end-to-end entrambi i path entailment (warning e block via stub orchestrator), chiudendo tecnicamente la sotto-fase 8.8A.** Vedi `PROJECT_STATE.md` e `PHASE_8_8A_PRE.md` per i dettagli. I prossimi blocchi candidati sono **8.8A-READ** (read API entailment task-level) e **8.8B-REPORT** (Anti-Hallucination Report API aggregata), poi UI-PRE.

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
| `make test` | tutti i test (include i realistic flow 8.5/8.6/8.7H/8.8A) |
| `make test-db` / `test-shared` / `test-api` / `test-worker` / `test-web` | per modulo |
| `make lock-web` | genera `apps/web/package-lock.json` |
| `make psql` / `make redis-cli` | shell |
| `make clean` | distrugge i volumi |

`make migrate` applica `0001`, `0002`, `0003`, `0004`, `0005`, `0006`, `0007`, `0008`, `0009`, `0010` in ordine. Idempotente: rieseguirla è no-op. Le migration `0011` e successive sono ancora da assegnare (retention distruttiva candidata).

### Realistic flow tests (root-level)

I realistic flow test girano nello stesso pytest session della suite standard e richiedono solo `DATABASE_URL` raggiungibile (Postgres via `make up` + `make migrate`); usano una FakeRedis interna e invocano `dispatch.handle_event` direttamente, quindi non richiedono il worker main loop reale.

````bash
# Solo il realistic flow 8.8A (warning + block path entailment end-to-end)
pytest tests/test_phase_8_8a_entailment_gate_flow.py -v

# Solo il realistic flow 8.7H (warning + block path source quality end-to-end)
pytest tests/test_phase_8_7_source_quality_flow.py -v

# Tutti i realistic flow root-level (8.5 withdrawal/source-loss, 8.6 read API,
# 8.7H source quality, 8.8A claim entailment)
pytest tests/ -v
````

Il file `tests/test_phase_8_8a_entailment_gate_flow.py` contiene due test indipendenti:
- `test_phase_8_8a_entailment_warning_flow_end_to_end` — warning flow: mock claim entailment checker reale (containment / numeric mismatch / default uncertain) → l'asse entailment è `clean` (quando ogni pair finisce nel containment rule e produce `entailed`) o `warnings` (quando almeno una pair finisce nel default uncertain) → Source Quality mock produce `source_quality_warning` per span → Final Answer Gate approved con `reason_code='all_spans_verified_with_warnings'` → `coverage_gap_statements` di kind `source_quality_warning` (sempre presente) e/o `entailment_warning` (presente solo quando l'asse entailment è in stato warnings) → `published_answers` v1 status='published'. Audit chain valida con `task.entailment_checked` strettamente tra `task.source_quality_assessed` e `task.compiling`, `task.published` terminale.
- `test_phase_8_8a_entailment_block_flow_end_to_end` — block flow: monkeypatch del simbolo `run_claim_entailment_checks` sul consumer module (`_wapp.consumers.task_created`) con uno stub orchestrator che inserisce `claim_entailment_checks` con `verdict='contradicted'` per ogni pair (necessario perché il mock checker reale non produce `contradicted`) → Final Answer Gate rejected con `reason_code='entailment_block'` → `coverage_gap_statements` kind `entailment_block` severity='block' → audit terminale `task.publication_held` → nessun `published_answer` → `GET /published-answer` ritorna 404 RESOURCE_NOT_FOUND con `details.resource='published_answers'`.

---

## Smoke test 8.4 (approved scenario end-to-end, aggiornato post-8.7G/H + 8.8A)

````bash
# 1) Crea progetto
PID=$(curl -s -X POST localhost:8000/api/v1/projects \
  -H 'content-type: application/json' \
  -d '{"name":"smoke-88a-demo"}' | jq -r .id)

# 2) Carica un documento .txt con frasi factual (cifre)
DID=$(curl -s -X POST "localhost:8000/api/v1/projects/$PID/documents" \
  -F "file=@evaluation/fixtures/closed_corpus_basic/doc_en.txt;type=text/plain" \
  | jq -r .id)

# 3) Crea il task con documento
TID=$(curl -s -X POST localhost:8000/api/v1/tasks \
  -H 'content-type: application/json' \
  -d "{\"project_id\":\"$PID\",\"objective\":\"smoke 8.8A\",\"mode\":\"closed_corpus\",\"document_ids\":[\"$DID\"]}" \
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

# 5) Audit chain (post-8.8A approved: 15 eventi worker-side, con
#    task.source_quality_assessed e task.entailment_checked tra
#    task.analyzed_partial e task.compiling)
curl -s "localhost:8000/api/v1/tasks/$TID/audit?limit=500" | jq '.items[].event_type'

# 6) Latest del ledger (claim verified visibili)
curl -s "localhost:8000/api/v1/tasks/$TID/claims" | jq

# 7) Endpoint answers 8.4 (post-8.7G + 8.8A-GATE-CODE: reason_code default
#    'all_spans_verified_with_warnings' quando con mock attuale per via dei
#    warning Source Quality; coverage_gap_statements include
#    source_quality_warning sempre, ed entailment_warning solo se il mock
#    checker non ha prodotto 'entailed' per ogni pair; final_gate_reports.payload
#    espone una sezione 'entailment' con policy_name, policy_version, status,
#    spans_with_block, spans_with_warnings)
curl -s "localhost:8000/api/v1/tasks/$TID/draft" | jq
curl -s "localhost:8000/api/v1/tasks/$TID/final-gate-report" | jq
curl -s "localhost:8000/api/v1/tasks/$TID/published-answer" | jq

# 8) Single-row view del published answer
PAID=$(curl -s "localhost:8000/api/v1/tasks/$TID/published-answer" | jq -r .id)
curl -s "localhost:8000/api/v1/published-answers/$PAID" | jq

# 9) Source quality 8.7F (per task)
curl -s "localhost:8000/api/v1/tasks/$TID/source-quality" | jq

# 10) Entailment read API: NON ESISTE in 8.8A. La diagnostica entailment
#     task-level passa oggi attraverso /final-gate-report (campo
#     coverage_gap_statements con kind 'entailment_block' / 'entailment_warning')
#     e payload.entailment (summary policy + counts). Le read API entailment
#     dedicate sono rinviate a 8.8A-READ.
````

Il reason code atteso di default in approved con il mock attuale è **`all_spans_verified_with_warnings`**: i warning provengono sia dall'asse Source Quality (sempre, perché il mock evaluator emette `overall_quality='unknown'` + `contradiction_status='unchecked'`) sia, occasionalmente, dall'asse Entailment (quando una pair (entry, evidence_span) finisce nel default uncertain del mock checker invece che nel containment rule). Tipicamente con extractor mock-driven + containment rule, ogni pair finisce in `entailed` (clean) e l'unico warning è quello Source Quality.

Smoke test rejected zero-verified: come sopra ma con un documento privo di frasi che superino CVE-lite (oppure forzando `quote_hash` non corrispondente in test). Il task termina in `analyzed_partial`; `GET /final-gate-report` restituisce `decision='rejected'`, `reason_code='no_verified_claims'`, una `coverage_gap_statements` con `kind='missing_evidence'`, `gap_key='no_verified_claims'`; `GET /published-answer` restituisce `404 RESOURCE_NOT_FOUND` con `details.resource='published_answers'`. **Né Source Quality né Entailment vengono consultati in questo branch (priorità CVE-lite > Entailment > Source Quality).**

Smoke test rejected source_quality_block o entailment_block: non riproducibili da smoke test end-to-end via curl senza un evaluator/checker reale o un seed manuale di `source_quality_assessments` / `claim_entailment_checks`, perché **il mock evaluator deterministico non produce `unsuitable`** e **il mock checker deterministico non produce `contradicted`**. Coperti a livello unit dai test worker `apps/worker/tests/test_final_answer_gate_source_quality.py` (13 scenari) e `apps/worker/tests/test_final_answer_gate_entailment.py` (13 scenari), e a livello end-to-end dai realistic flow `tests/test_phase_8_7_source_quality_flow.py::test_phase_8_7_source_quality_block_flow_end_to_end` (8.7H) e `tests/test_phase_8_8a_entailment_gate_flow.py::test_phase_8_8a_entailment_block_flow_end_to_end` (8.8A-GATE-FLOW), che attivano i rispettivi branch via monkeypatch dell'orchestrator nel consumer.

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
│  - claim_entailment_checks   (8.8A)                │
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
│  Pipeline task.created (post-8.8A):                          │
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
│  10. Claim Entailment step — 8.8A                            │
│      - legge (claim_ledger_entry, evidence_span) pair        │
│        derivate da claim_evidence_links                      │
│      - mock heuristic: containment / numeric mismatch /      │
│        default uncertain                                     │
│      - scrive claim_entailment_checks                        │
│      - emette audit task.entailment_checked                  │
│      - protetto da SAVEPOINT                                 │
│                                                              │
│  11. task.compiling                                          │
│  12. task.draft_compiled                                     │
│  13. task.final_gate_started                                 │
│                                                              │
│  14. Final Answer Gate — 8.4 + 8.7G + 8.8A-GATE              │
│      - verifica che gli span siano verified-backed           │
│      - consulta claim_entailment_checks (8.8A-GATE)          │
│      - consulta source_quality_assessments (8.7G)            │
│      - applica policy:                                       │
│        CVE-lite > Entailment > Source Quality                │
│      - può approvare, approvare con warning, o bloccare      │
│                                                              │
│  15. task.final_gate_completed                               │
│  16. task.published                                          │
│      oppure task.publication_held                            │
│                                                              │
│  Validato end-to-end da 8.7H (SQ warning + block) e da       │
│  8.8A-GATE-FLOW (entailment warning + block) tramite         │
│  tests/test_phase_8_7_source_quality_flow.py e               │
│  tests/test_phase_8_8a_entailment_gate_flow.py.              │
│                                                              │
│  Nota: il conteggio worker-side è 15 eventi audit             │
│  (analyzing/docs_loaded/claims_*/cve_lite_started/            │
│  cve_lite_completed/analyzed_partial/source_quality_assessed/ │
│  entailment_checked/compiling/draft_compiled/                 │
│  final_gate_started/final_gate_completed/                     │
│  published|publication_held).                                 │
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
│  - Claim Entailment append-only (8.8A)                       │
│  - Draft final answer                                        │
│  - Final gate report                                         │
│  - Coverage gap statements (kind: missing_evidence /         │
│    unverified_claim / out_of_scope / source_loss /           │
│    source_quality_block / source_quality_warning /           │
│    entailment_block / entailment_warning)                    │
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
  │       - claim entailment   (8.8A)
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
          ├── Claim Entailment Checker     ← 8.8A
          ├── Compiler
          └── Final Answer Gate
                ├── verified-backed check  ← 8.4
                ├── claim entailment policy ← 8.8A-GATE
                └── source quality policy  ← 8.7G
                      │
                      ├── approved
                      ├── approved with warnings   ← validato 8.7H + 8.8A-GATE-FLOW
                      └── rejected / publication held
                              ├── source_quality_block  ← validato 8.7H (via stub)
                              └── entailment_block      ← validato 8.8A-GATE-FLOW (via stub)



Pipeline 8.4 + 8.7E + 8.7G + 8.8A + 8.8A-GATE-CODE nel worker con documenti, approved scenario (mock attuale → warning path):

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
[8.8A] claim entailment (SAVEPOINT)  audit task.entailment_checked
│                                         (status=completed|failed)
▼
analyzed_partial → compiling         audit task.compiling
│
▼
compiler mock-driven                 audit task.draft_compiled
│
▼
[8.7G + 8.8A-GATE]                   audit task.final_gate_started
       Final Answer Gate             audit task.final_gate_completed
       + Source Quality consultation
       + Claim Entailment consultation
│
├── approved (clean, evaluator+checker reali)
│     └─► compiling → published          audit task.published
│         + published_answers v1
│         + reason_code='all_spans_verified'
│
├── approved with warnings (default oggi con mock) — VALIDATO da 8.7H + 8.8A-GATE-FLOW
│     └─► compiling → published          audit task.published
│         + published_answers v1
│         + reason_code='all_spans_verified_with_warnings'
│         + coverage_gap_statements kind='source_quality_warning' per span
│         + opzionalmente coverage_gap_statements kind='entailment_warning'
│           se almeno una pair (entry,span) ha verdict in
│           {not_supported, partially_supported, uncertain, missing}
│
├── rejected unverified (priorità CVE-lite)
│     └─► compiling → analyzed_partial   audit task.publication_held
│         + final_gate_reports rejected
│         + reason_code='unverified_spans_present'
│         + coverage_gap_statements kind='unverified_claim'
│         (NO published_answers; Entailment e Source Quality NON consultati)
│
├── rejected entailment_block (Branch E) — VALIDATO da 8.8A-GATE-FLOW (via stub orchestrator)
│     (in produzione con mock attuale NON si attiva spontaneamente)
│     └─► compiling → analyzed_partial   audit task.publication_held
│         + final_gate_reports rejected
│         + reason_code='entailment_block'
│         + coverage_gap_statements kind='entailment_block' severity='block'
│         + audit completeness: anche gap source_quality (se presenti)
│         (NO published_answers)
│
├── rejected source_quality_block (Branch C') — VALIDATO da 8.7H (via stub orchestrator)
│     (in produzione con mock attuale NON si attiva spontaneamente)
│     └─► compiling → analyzed_partial   audit task.publication_held
│         + final_gate_reports rejected
│         + reason_code='source_quality_block'
│         + coverage_gap_statements kind='source_quality_block'
│         + audit completeness: anche gap entailment_warning (se presenti)
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

Note 8.7E + 8.8A:
- Lo step source quality e lo step claim entailment sono entrambi SAVEPOINT-protected: un fallimento di uno dei due NON aborta la transazione del consumer e NON blocca 8.4.
- Sui resume da `compiling` nessuno dei due step viene re-eseguito.
- L'audit task.entailment_checked è strettamente DOPO task.source_quality_assessed e PRIMA di task.compiling.

Note 8.7G + 8.8A-GATE-CODE:
- Il Gate consulta `source_quality_assessments` e `claim_entailment_checks` come read-only (zero mutazioni su entrambe le tabelle).
- Priorità: CVE-lite > Claim Entailment > Source Quality. Uno span non verified-backed produce `unverified_spans_present` indipendentemente da entailment e qualità delle fonti.
- Quando un entailment_block e un source_quality_block fioccano sullo stesso draft, il reason_code è `entailment_block` (priorità Entailment > Source Quality), ma il Gate emette comunque entrambe le tipologie di gap per audit completo.
- Con il mock attuale (`overall_quality='unknown'` + `contradiction_status='unchecked'` da Source Quality, verdict tipicamente `entailed` da claim entailment via containment), il branch attivo per task verified-backed è "approved with warnings" (warning prevalentemente Source Quality).
- I branch "rejected source_quality_block" e "rejected entailment_block" si attiveranno spontaneamente solo con un evaluator/checker reale che produca `unsuitable` (SQ) o `contradicted` (entailment).

Note 8.7H + 8.8A-GATE-FLOW:
- L'intera pipeline è validata end-to-end dai due realistic flow test `tests/test_phase_8_7_source_quality_flow.py` e `tests/test_phase_8_8a_entailment_gate_flow.py`.
- 8.7H warning flow: mock evaluator reale → `unknown`+`unchecked` → Branch W → published con `source_quality_warning`.
- 8.7H block flow: stub dell'orchestrator (`monkeypatch.setattr(_wapp.consumers.task_created, "run_source_quality_assessment", _stub)`) → `unsuitable` → Branch C' → publication_held.
- 8.8A-GATE-FLOW warning flow: mock checker reale → asse entailment `clean` o `warnings` a seconda dell'output del containment rule; Source Quality contribuisce un warning per span → Branch W → published con `source_quality_warning` (sempre) e `entailment_warning` (solo se l'asse entailment è in stato warnings).
- 8.8A-GATE-FLOW block flow: stub dell'orchestrator (`monkeypatch.setattr(_wapp.consumers.task_created, "run_claim_entailment_checks", _stub)`) → `contradicted` per ogni pair → Branch E → publication_held. Lo stub è necessario perché il mock checker reale non produce `contradicted`; non altera il Gate né il compiler né le migration né gli altri servizi.
- Entrambi i flow test validano API HTTP (POST projects/documents/tasks), FakeRedis (cattura `xadd`), dispatcher (`_dispatch.handle_event`), consumer, servizi worker, DB (audit chain, source_quality_assessments, claim_entailment_checks, final_gate_reports, coverage_gap_statements, published_answers), e read API (`/final-gate-report`, `/published-answer`, `/source-quality`).

Resume scenario (consumer entra con task già in `compiling`):
- se esiste `final_gate_reports`: finalizza usandolo (drive a `published` se approved, a `analyzed_partial` con `task.publication_held` se rejected).
- se non esiste: riesegue compiler + gate idempotentemente (compreso il consumo di `source_quality_assessments` 8.7G e di `claim_entailment_checks` 8.8A-GATE).

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
- [`PHASE_8_8A_PRE.md`](PHASE_8_8A_PRE.md) — piano architetturale 8.8A (con box "Stato post-implementazione")
- [`PHASE_8_8A_GATE_PRE.md`](PHASE_8_8A_GATE_PRE.md) — piano architetturale del Gate 8.8A
