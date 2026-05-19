# Evidence-First Multi-AI Platform — MVP-0

Piattaforma multi-AI **evidence-first** ed **evidence-gated**. Il sistema è progettato per impedire che claim fattuali non supportati, contraddetti o basati su fonti inadeguate vengano pubblicati come affidabili. **Non promette di eliminare le allucinazioni in senso assoluto**: impedisce o rende visibili claim non supportati, contraddetti o basati su fonti inadeguate prima della pubblicazione affidabile. Promette evidenze tracciabili, registrate nel Claim Ledger, verificate dal CVE-lite, valutate sulla qualità delle fonti, valutate anche sul piano della **relazione semantica claim ↔ quote** tramite il Claim Entailment Checker (8.8A), propagate via lifecycle/source-loss, **consultate dal Final Answer Gate** prima di qualunque pubblicazione, validate end-to-end da realistic flow test che esercitano warning e block path attraverso l'intera catena API → FakeRedis → dispatcher → consumer → servizi worker → DB → read API, **e ora esposte come vista task-level read-only aggregata dall'Anti-Hallucination Report API (8.8B-REPORT)** che consolida in un singolo payload publication, gate, claims, evidence, CVE-lite, Source Quality, Claim Entailment, coverage gaps con axis derivato, axis_summary, mock indicators e limitations.

Una fonte citata **non implica** un claim vero. Una quote testualmente presente **non implica** che la quote sostenga il claim. Un verdict `entailed` del checker mock **non implica** che il claim sia vero nel mondo: significa solo che la quote contiene testualmente il claim o gli è equivalente sotto la normalizzazione del mock heuristic. Source quality, evidence support, CVE-lite verification e claim entailment restano quattro assi separati anche dopo 8.8A. **L'Anti-Hallucination Report API è una vista read-only derivata: non introduce nuove decisioni, non ricalcola il Final Answer Gate, non rivaluta claim né fonti, non muta DB, non sostituisce le tabelle append-only né gli endpoint read specialistici.**

> **Stato corrente.** Repository al commit **`af74187`** ("Fix report CVE lineage and add realistic flow"), **sotto-fase 8.8B-REPORT tecnicamente conclusa**.
> Fasi implementate: 8.1–8.4 (foundation, storage, claim ledger, compiler + Final Answer Gate + first `published_answers`), 8.5 (lifecycle + source loss + propagator), 8.6 minima (read API lifecycle e source-loss), 8.7A–F (Source Quality Evaluator append-only + worker integration in `task.created` + read API), 8.7G (Source Quality consumata dal Final Answer Gate, policy P1+P3+P4, migration `0008_coverage_gap_source_quality.sql`), 8.7H (realistic flow test end-to-end `tests/test_phase_8_7_source_quality_flow.py`, chiusura fase 8.7), 8.8A (Claim Entailment Checker mock heuristic deterministic + worker integration + Gate consumption + realistic flow test, chiusura tecnica della sotto-fase), 8.8A-READ-A (`GET /api/v1/tasks/{task_id}/claim-entailment` read endpoint), **8.8B-REPORT (Anti-Hallucination Report API aggregata task-level read-only, CODE-A + CODE-B + CVE-lineage fix + realistic flow test, chiusura tecnica della sotto-fase)**.
> Per lo stato di dettaglio fase-per-fase, l'elenco completo degli endpoint, la pipeline aggiornata e i debiti tecnici, vedi [`PROJECT_STATE.md`](PROJECT_STATE.md). Per il piano architetturale 8.8A vedi [`PHASE_8_8A_PRE.md`](PHASE_8_8A_PRE.md) e [`PHASE_8_8A_GATE_PRE.md`](PHASE_8_8A_GATE_PRE.md). Per il piano architetturale 8.8B-REPORT vedi [`PHASE_8_8B_REPORT_PRE.md`](PHASE_8_8B_REPORT_PRE.md) (con appendice "Implementation status").
>
> Le sezioni storiche più sotto (Fase 8.4 in particolare) descrivono lo stato del nucleo evidence-gated minimo al momento della chiusura della relativa fase e restano valide come documentazione architetturale, ma NON riflettono lo stato corrente del repository. Le fasi successive (8.5, 8.6, 8.7, 8.8A, 8.8B-REPORT) hanno aggiunto step alla pipeline e/o superfici di osservabilità; vedi `PROJECT_STATE.md` per la pipeline e le decisioni correnti.

Disponibile localmente:
- Postgres 16, Redis 7
- API HTTP (FastAPI) con `health`, `projects`, `tasks`, `audit`, `documents`, `claims`, `answers`, lifecycle/source-loss read API (8.6), source quality read API (8.7F), claim-entailment task-level read API (8.8A-READ-A), **Anti-Hallucination Report API aggregata task-level (8.8B-REPORT)**
- Worker Redis Streams single-consumer, FK-safe, idempotente, resume-safe, con pipeline 8.4 + step Source Quality (8.7E) + step Claim Entailment (8.8A) prima del compiler, e con il Final Answer Gate che consuma sia `source_quality_assessments` (8.7G) sia `claim_entailment_checks` (8.8A-GATE)
- Claim Entailment Checker mock heuristic deterministic — tre regole sintattiche (containment / numeric mismatch / default uncertain); non è un NLI/LLM reale
- `task.entailment_checked` nella pipeline audit chain (SAVEPOINT-protetto, idempotente)
- Final Answer Gate consuma `claim_entailment_checks` con policy `mvp0_entailment_gate_policy` v0.1.0 (solo `contradicted` blocca)
- Coverage gaps `entailment_block` / `entailment_warning` emessi dal Gate quando rilevanti (migration 0010)
- **Anti-Hallucination Report API aggregata** che espone, per ogni task esistente, una vista task-level read-only contenente task metadata, publication status derivato, gate decision/reason_code/payload verbatim, coverage gaps con `axis` decorato severity-first, claims con CVE-lite / Source Quality (latest-per-target) / Claim Entailment (latest-per-pair), evidence dei `evidence_spans` task-attached, axis_summary completo per i quattro assi, mock indicators derivati da identità servizio + `payload.mock`, e disclaimer testuali sempre presenti
- Web Next.js minimale (home + `/diagnostic`)
- Storage filesystem deduplicato content-addressed con upsert concorrenza-safe
- Audit chain hash-linked, append-only, verificabile end-to-end

In 8.4 i task con documenti e claim verificati raggiungevano lo stato terminale `published`, con un `published_answers` v1 e un `final_gate_reports` `approved` (`reason_code='all_spans_verified'`). I task con documenti ma senza claim verificati raggiungevano `analyzed_partial` con un `final_gate_reports` `rejected` e l'evento audit `task.publication_held`. La sequenza audit è stata estesa in 8.7E con `task.source_quality_assessed` tra `task.analyzed_partial` e `task.compiling`, e ulteriormente in 8.8A con `task.entailment_checked` tra `task.source_quality_assessed` e `task.compiling`. In 8.7G il Final Answer Gate consulta `source_quality_assessments` come terzo asse decisionale; in 8.8A-GATE consulta anche `claim_entailment_checks` come quarto asse. Con il mock evaluator + mock checker attuali, il `reason_code` di default per task approved è **`all_spans_verified_with_warnings`** (le righe SQ mock-driven producono sempre un `source_quality_warning` per span; le righe entailment mock-driven producono tipicamente verdict `entailed` via containment rule e quindi non aggiungono warning entailment di default). In 8.8A-GATE-FLOW l'intera catena è stata validata end-to-end dal realistic flow test `tests/test_phase_8_8a_entailment_gate_flow.py`. **In 8.8B-REPORT-FLOW l'intera catena è stata ri-validata end-to-end con osservazione finale via report aggregato**: il realistic flow test `tests/test_phase_8_8b_report_flow.py` copre entrambi i path (warning con mock reali, publication-held entailment_block via stub orchestrator) attraverso API HTTP → FakeRedis → dispatcher → `task.created` consumer → source quality → claim entailment → final gate → `GET /api/v1/tasks/{task_id}/anti-hallucination-report`. Vedi `PROJECT_STATE.md` per la sequenza aggiornata e per i branch decisionali del Gate.

I quattro outcome MVP-0 oggi osservabili sono:

- **`all_spans_verified` → published clean.** Branch B del Gate: tutti gli span verified-backed, nessun warning di alcun asse. `decision='approved'`, `reason_code='all_spans_verified'`, nessun coverage gap, `published_answers` v1 inserito. Nel report 8.8B-REPORT: `publication.status='published'`, `axis_summary.final_gate.has_warnings=False`. **Non è il path di default oggi**.
- **`all_spans_verified_with_warnings` → published con warning.** Branch W del Gate: tutti verified-backed, nessun block, almeno un warning. Nel report: `publication.status='published'`, `axis_summary.final_gate.has_warnings=True`, `coverage_gaps` include `source_quality_warning` e/o `entailment_warning` con `axis` decorato. **Path attivo di default oggi**.
- **`source_quality_block` → publication_held.** Branch C' del Gate. Nel report: `publication.status='publication_held'`, `axis_summary.final_gate.has_blocking_gaps=True`, `coverage_gaps` include `source_quality_block` severity='block'. **Path implementato e testato (unit + realistic flow 8.7H via stub dell'orchestrator SQ); in produzione con il mock attuale non si attiva spontaneamente.**
- **`entailment_block` → publication_held.** Branch E del Gate (8.8A-GATE-CODE): tutti verified-backed, almeno uno span con latest entailment verdict `contradicted` su almeno una pair (entry, evidence_span) supportante. Priorità Entailment > Source Quality: se entrambi i block fioccano, reason_code è `entailment_block` ma il Gate emette tutti i gap rilevanti. Nel report: `publication.status='publication_held'`, `axis_summary.claim_entailment.contradicted_count >= 1`, `axis_summary.final_gate.has_blocking_gaps=True`, `coverage_gaps` include `entailment_block` severity='block' con `axis='claim_entailment'`. **Path implementato e testato (unit 13 scenari + realistic flow end-to-end via stub orchestrator in 8.8A-GATE-FLOW e in 8.8B-REPORT-FLOW); in produzione con il mock checker attuale non si attiva spontaneamente.**

**Renderer esterni Markdown/HTML/PDF/DOCX/JSON-LD restano fuori scope per MVP-0.** Il nucleo evidence-gated produce `draft_final_answers`, `final_answer_spans` e `published_answers` con `summary_text` testuale e `content_hash`, ma non emette artefatti esportabili in formati di rendering. Gli endpoint answers sono read-only e restituiscono JSON normalizzato. Anche l'Anti-Hallucination Report API restituisce JSON normalizzato e non emette artefatti renderizzabili.

---

## Scope MVP-0 (sintesi)

Incluso (oggi, post-8.8B-REPORT):
- Closed Corpus, Postgres + Redis + filesystem locale.
- Audit chain hash-linked (append-only, verificabile).
- `event_processing_records` con idempotenza per consumer.
- API: `health/projects/tasks/audit/documents/claims/answers`, lifecycle/source-loss read (8.6), source quality read (8.7F), claim-entailment task-level read (8.8A-READ-A), **Anti-Hallucination Report API aggregata task-level (8.8B-REPORT)**.
- Storage content-addressed deduplicato, refcount-based.
- Document upload reale `.txt`/`.md` con chunking deterministico ed `evidence_spans` minimali.
- Claim Ledger append-only con extractor mock-driven e CVE-lite mock-driven.
- Compiler mock-driven che produce `draft_final_answers` v1 con `final_answer_spans` 1:1 sui claim `verified_fact`.
- Final Answer Gate mock-driven: decide `approved`/`rejected`, scrive `final_gate_reports` (append-only) e, su approved, inserisce `published_answers` v1 con `status='published'`. In 8.7G consulta `source_quality_assessments`. In 8.8A-GATE consulta anche `claim_entailment_checks`.
- Lifecycle e source loss (8.5): withdrawal asincrona, source loss event/propagation append-only, due API producer.
- Read API 8.6 su lifecycle events e source-loss events/propagation/task-listing (read-only end-to-end).
- Source Quality 8.7: tabella `source_quality_assessments` append-only, mock evaluator deterministic (continua a produrre solo `overall_quality='unknown'` + `contradiction_status='unchecked'`), orchestrator chiamato in `task.created` dopo `analyzed_partial` (SAVEPOINT-protected, audit aggregato `task.source_quality_assessed`), due endpoint read 8.7F.
- Source Quality consumata dal Gate (8.7G): migration `0008_coverage_gap_source_quality.sql` estende `coverage_gap_statements.kind` con `source_quality_block` e `source_quality_warning`; il Gate emette i nuovi gap e i nuovi reason_code `source_quality_block` (rejected) e `all_spans_verified_with_warnings` (approved).
- Claim Entailment 8.8A: tabella `claim_entailment_checks` append-only (migration `0009_claim_entailment_checks.sql`), mock checker heuristic deterministic (tre regole sintattiche: containment match → `entailed`; numeric mismatch → `not_supported`; default → `uncertain`; il mock NON emette mai `contradicted` né `partially_supported`), orchestrator chiamato in `task.created` dopo `task.source_quality_assessed` (SAVEPOINT-protected, audit aggregato `task.entailment_checked`), shared schemas `SOURCE_ENTAILMENT_VERDICT_VALUES` + `ClaimEntailmentVerdict` Literal alias + `ClaimEntailmentCheckRead` in `packages/shared/evidencefirst_shared/schemas.py`. Mock heuristic — **non è un NLI/LLM reale**. Ogni riga emessa porta `payload.mock=true` e `payload.semantic_warning="mvp0 heuristic; not a real NLI/LLM entailment model"`.
- Claim Entailment consumata dal Gate (8.8A-GATE-CODE): migration `0010_coverage_gap_entailment.sql` estende `coverage_gap_statements.kind` con `entailment_block` e `entailment_warning`; il Gate emette i nuovi gap, il nuovo reason_code `entailment_block` (rejected, priorità Entailment > Source Quality) e riusa `all_spans_verified_with_warnings` (approved) per warning misti. Identità della policy del Gate: `mvp0_entailment_gate_policy` v0.1.0. Priorità invariante: **CVE-lite > Claim Entailment > Source Quality**.
- Realistic flow test end-to-end (8.8A-GATE-FLOW): `tests/test_phase_8_8a_entailment_gate_flow.py` valida l'intera catena con due test indipendenti — warning flow (mock checker reale) e block flow (Branch E attivato via stub dell'orchestrator).
- **Anti-Hallucination Report API 8.8B-REPORT** (NUOVO): `apps/api/app/routes/anti_hallucination_report.py` espone `GET /api/v1/tasks/{task_id}/anti-hallucination-report`. **Strettamente read-only**: nessun INSERT/UPDATE/DELETE, nessun worker call, nessun Redis, nessun ricalcolo del Gate. Aggrega in un singolo response: task metadata, publication status derivato (`published` / `withdrawn` / `superseded` / `publication_held` / `not_ready` / `failed` / `unknown` fallback), gate decision / reason_code / payload **verbatim**, coverage gaps con `axis` derivato e ordering severity-first (`block` > `warn` > `info`), claims (uno per logical_claim del task) con `latest_entry_id` / `latest_state` / `support_scope` dalla latest `claim_ledger_entries`, `evidence_links` strutturali scoped alla latest entry, `cve_lite` records (con lineage via `claim_lineage(relation_kind='supersedes')` per i record scritti sulla v1 parent prima del supersede a v2), `source_quality` slot per ogni `evidence_span_id` linkato con la latest assessment (o slot missing), `entailment` slot per ogni pair `(latest_entry, evidence_span)` con la latest check (o slot missing), evidence dei `evidence_spans` task-attached in ordering deterministico, `axis_summary` counters per i quattro assi, `mock_indicators` derivati da identità servizio + `payload.mock`, e `limitations` testuali sempre presenti. Latest semantics: il report usa il **latest assoluto DB-level** per target/pair (`ORDER BY version_no DESC, created_at DESC, id DESC`), coerente con la semantica del Gate, distinto dalla semantica latest-in-slice di 8.7F e dalla cronologia globale di 8.8A-READ-A.
- **Realistic flow test 8.8B-REPORT-FLOW**: `tests/test_phase_8_8b_report_flow.py` valida l'intera catena `API HTTP → FakeRedis → dispatcher → task.created consumer → source quality → claim entailment → final gate → GET report` con due test indipendenti — warning flow (mock reali end-to-end, `publication.status='published'`, axis_summary coerente, mock_indicators tutti True) e publication-held entailment_block flow (Branch E attivato via stub dell'orchestrator entailment perché il mock checker reale non produce `contradicted`; `publication.status='publication_held'`, `published_answer_id=null`, `axis_summary.claim_entailment.contradicted_count >= 1`, `axis_summary.final_gate.has_blocking_gaps=True`, `coverage_gaps` con `kind='entailment_block'` severity='block').
- Coerenza referenziale stretta a livello DB tra task ↔ draft ↔ gate ↔ published via UNIQUE composite e FK composite.

Escluso (rinviato a fasi successive — vedi `PHASE_8_7_PLAN.md §13`, `PHASE_8_8A_PRE.md`, `PHASE_8_8B_REPORT_PRE.md` per la roadmap anti-allucinazione):
- Provider AI reali, Verified Web Mode, Hybrid Mode, consensus engine.
- **Evaluator NLI reale**: il checker MVP-0 è un mock heuristic deterministico a tre regole. Un checker reale NLI/LLM (8.9 o successive) è ancora mancante; senza di esso il branch `entailment_block` del Gate è dormiente in produzione mock-driven.
- **API read claim-level entailment**: `GET /api/v1/claims/{logical_id}/entailment-checks` rinviata a **8.8A-READ-B**.
- **Anti-Hallucination Report API v2 published-answer-level**: `GET /api/v1/published-answers/{id}/anti-hallucination-report` rinviato; in v1 la UI può ricostruirlo dal task-level via `published_answers.task_id`.
- **Lifecycle / source-loss event details aggregati nel report**: rinviati; eventuali `coverage_gap_statements` di kind `source_loss` possono comunque apparire in `gate.coverage_gaps`.
- **Shared schema Pydantic per il report**: `AntiHallucinationReportRead` oggi wrapper inline nel route module, promozione a shared rinviata se la shape resta stabile dopo le prime due-tre integrazioni UI.
- **Citation-to-Claim Validator** (8.8B storico, distinto da 8.8B-REPORT): verifica che il claim citi le evidenze giuste, non solo "vicine". Mancante.
- **Contradiction detector reale** (8.8C): cross-source contradiction su `contradiction_records`. Mancante.
- **Final Answer Sentence Gate** (8.8D): gate a livello frase del published_answer. Mancante.
- **External Verification / Web-RAG** (8.9): Verified Web Mode controllato. Mancante.
- **Multi-agent consensus + adversarial review reale** (9.0). Mancante.
- **UI completa**: nessuna interfaccia utente espone ancora il report aggregato né i gap entailment / source_quality. Mancante. **Prossimo blocco consigliato (UI-PRE).**
- Renderer e/o export Markdown/HTML/PDF/DOCX/JSON-LD.
- PDF/OCR, vector store cloud, S3/GCS/Azure.
- Human Review UI completa, retention/eval/export jobs.
- **RBAC reale e redaction** dei JSONB esposti dagli endpoint read (8.6, 8.7F, 8.8A-READ-A) e **dal report 8.8B-REPORT** (che compone payload da molti assi in un singolo response, aggravando il rischio di leak). Dichiarata in `limitations` del report stesso.
- Retention reale distruttiva (`0011_*` o successiva; i numeri `0009` e `0010` sono occupati da 8.8A).
- Backfill source quality e entailment per task pre-8.7E / pre-8.8A.
- Recompile/draft v2 dopo `source_quality_block` o `entailment_block`.
- Smoke/realistic test end-to-end con Redis loop reale e worker main loop reale (i realistic flow 8.5/8.6/8.7H/8.8A/8.8B-REPORT usano FakeRedis + `dispatch.handle_event` diretta).

---

## Fasi precedenti (sintesi)

- **8.1b**: API + Worker + Web stub.
- **8.1c**: audit/idempotency centralizzati in `packages/shared`, `verify_audit_chain`, `/health/ready` con 503 quando non ready.
- **8.1d**: Dockerfile web robusto, payload audit normalizzato, commit-then-publish lato API, worker difensivo.
- **8.1d-patch1**: Makefile default goal corretto, worker FK-safe sul caso task non visibile.
- **8.2**: storage reale, document upload reale, `task_documents`, worker `analyzed_partial` con documenti.
- **8.2a-patch**: stabilizzazione pre-8.3. Test rerun-safe (hash unici per invocazione), dedup storage concorrenza-safe via `INSERT ... ON CONFLICT DO NOTHING`, validazione `document_ids` con `bindparam(expanding=True, type_=Uuid())`.
- **8.3**: `0004_claim_ledger.sql`. Extractor mock-driven, CVE-lite mock-driven, ledger append-only stretto, supersede via `claim_lineage`.

Per 8.4 vedi la sezione storica più sotto. Per 8.5, 8.6, 8.7, 8.8A, 8.8B-REPORT vedi [`PROJECT_STATE.md`](PROJECT_STATE.md).

---

## Fase 8.4 — Compiler, Final Answer Gate, primo `published_answers` (sezione storica)

> Le righe sotto descrivono lo stato al commit di chiusura 8.4 e restano valide per il nucleo 8.4. Le fasi successive (8.5/8.6/8.7/8.8A/8.8B-REPORT) hanno aggiunto step alla pipeline (in particolare `task.source_quality_assessed` in 8.7E e `task.entailment_checked` in 8.8A) e branch decisionali al Gate (in 8.7G e 8.8A-GATE-CODE), hanno validato l'intera pipeline end-to-end (8.7H + 8.8A-GATE-FLOW + 8.8B-REPORT-FLOW), e hanno aggiunto una vista aggregata read-only (8.8B-REPORT). Vedi `PROJECT_STATE.md` per la pipeline e le decisioni correnti.

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

  **Nota post-8.7G/H + 8.8A-GATE-CODE + 8.8B-REPORT.** Il Final Answer Gate ora consulta `source_quality_assessments` (8.7G) E `claim_entailment_checks` (8.8A-GATE) come terzo e quarto asse decisionale. La regola di verifica 8.4 è invariata, ma vengono aggiunti branch (approved with warnings, rejected per source quality, rejected per entailment). Priorità: **CVE-lite > Claim Entailment > Source Quality**. **8.8B-REPORT non modifica il Gate**: aggiunge solo una vista derivata read-only.
- **Worker single-consumer 8.4 (`apps/worker/app/consumers/task_created.py`).** La pipeline 8.3 (extractor + CVE-lite → `analyzed_partial`) è preservata. Dopo `task.analyzed_partial`, il consumer prosegue nello stesso evento verso compiler + gate. FK-safe sul caso task non visibile (preserva il comportamento 8.1d-patch1). Resume-safe. Guardia finale `WORKER_PIPELINE_INCOMPLETE`. **Nota post-8.7E + 8.8A.** Tra `task.analyzed_partial` e `task.compiling` sono inseriti, nell'ordine, lo step Source Quality (8.7E, audit `task.source_quality_assessed`, SAVEPOINT-protetto) e lo step Claim Entailment (8.8A, audit `task.entailment_checked`, SAVEPOINT-protetto). **Nota post-8.7G/H + 8.8A-GATE-CODE + 8.8B-REPORT.** Il consumer non è modificato in 8.7G, 8.7H, 8.8A-GATE-CODE né in 8.8B-REPORT: solo il Gate è esteso (8.7G + 8.8A-GATE) e una nuova route HTTP read-only è aggiunta (8.8B-REPORT).
- **Sequenza audit con documenti (approved scenario, post-8.8A).** Sulla chain del task, **15 eventi worker-side** dopo `task.created`/`task.docs_attached`. **8.8B-REPORT non altera la sequenza audit**: legge solo `audit_records` come una qualunque GET, senza scriverne. 8.8B-REPORT-FLOW verifica esplicitamente l'ordinamento end-to-end (asserzione su `analyzed_partial < source_quality_assessed < entailment_checked < compiling < final_gate_completed < published` nel warning flow; `... < publication_held` nel block flow).
- **Sequenza audit con documenti (rejected scenario, originale 8.4).** Identica fino a `task.final_gate_completed`, poi `task.publication_held`. **`task.publication_held` è esclusivamente un evento audit, non uno stato di `task_masters.status`.**
- **Task senza documenti: comportamento invariato.** Sequenza worker: `task.analyzing`, `task.blocked`.
- **Idempotenza completa.** Doppio delivery dello stesso `task.created` non duplica righe in nessuna tabella, comprese le `coverage_gap_statements` di kind `source_quality_*` (8.7G) e `entailment_*` (8.8A-GATE-CODE).
- **Endpoint API read-only.**
  - `GET /api/v1/tasks/{task_id}/draft`
  - `GET /api/v1/tasks/{task_id}/final-gate-report` — invariato in 8.7G/H + 8.8A-GATE-CODE + 8.8B-REPORT come signature; i `coverage_gap_statements` collegati possono ora avere `kind ∈ {source_quality_block, source_quality_warning, entailment_block, entailment_warning}` in aggiunta ai kind preesistenti. Il `final_gate_reports.payload` espone una sezione `entailment` con `policy_name`, `policy_version`, `status` ∈ {clean, warnings, blocked}, `spans_with_block`, `spans_with_warnings`, `block_reason_counts`, `warning_reason_counts`.
  - `GET /api/v1/tasks/{task_id}/published-answer`
  - `GET /api/v1/published-answers/{published_answer_id}`
  - **`GET /api/v1/tasks/{task_id}/anti-hallucination-report` (8.8B-REPORT, NUOVO)**: vista task-level read-only aggregata. Vedi sezione "Smoke test 8.8B-REPORT" sotto e `PROJECT_STATE.md` per la shape di dettaglio.

  Errori normalizzati con envelope `{"error": {"code": "...", "message": "...", "details": {...}, ...}}`. `ErrorCode.NOT_PUBLISHED` **non esiste** in MVP-0: per "task esiste ma non è ancora pubblicato" si restituisce `RESOURCE_NOT_FOUND` con `details.resource='published_answers'`. **Per task inesistente, sia su endpoint specialistici sia su 8.8B-REPORT**, si restituisce `RESOURCE_NOT_FOUND` con `details.resource='task_masters'`. 8.7H + 8.8A-GATE-FLOW + 8.8B-REPORT-FLOW verificano esplicitamente questa convenzione nei rispettivi block flow.

  Gli endpoint aggiuntivi 8.5/8.6/8.7F/8.8A-READ-A/8.8B-REPORT sono elencati in `PROJECT_STATE.md`.
- **Schemi shared aggiornati.** `packages/shared/evidencefirst_shared/schemas.py` espone i Read model 8.4 + 8.5 + 8.7C + 8.8A-SHARED. **8.8B-REPORT non ha aggiunto Read model shared in v1**: la shape del report è un wrapper inline nel route module (pattern già usato da `apps/api/app/routes/answers.py`). La promozione a shared model è rinviata se la shape resta stabile dopo le prime integrazioni UI.

### Stati terminali del consumer in 8.4 (invariati in 8.7G/H + 8.8A-GATE-CODE + 8.8B-REPORT)

Il consumer considera la task terminale (e chiama `mark_succeeded` sulla `event_processing_records`) nei seguenti casi:
- `blocked` — task senza documenti, branch invariato.
- `published` — approved scenario completato (oggi con mock: `reason_code='all_spans_verified_with_warnings'` per via dei warning Source Quality).
- `analyzed_partial` **e** esiste già un `final_gate_reports` per il task — rejected scenario completato (zero verified oppure unverified spans oppure source_quality_block oppure entailment_block).

Lo stato `analyzed_partial` **non è** terminale di per sé. Lo stato `compiling` **non è mai** terminale di propria iniziativa.

### Cosa NON cambia in 8.4 (storico) — invariato anche in 8.7G/H + 8.8A-GATE-CODE + 8.8B-REPORT

- Nessun **renderer** Markdown/HTML/PDF/DOCX/JSON-LD.
- Nessun **export** verso filesystem o storage cloud.
- Nessun **provider AI esterno**. **Costo API = 0.** `PROVIDERS_ENABLED=mock`.
- Nessun **Verified Web Mode**, **Hybrid Mode**, **consensus engine**, **contradiction detector** avanzato, **critical reviewer**.
- Nessuno **stato `publication_held`** a livello DB. Solo evento audit `task.publication_held`. **Il report 8.8B-REPORT espone `publication.status='publication_held'` come stato DERIVATO**, non come valore di `task_masters.status`.
- Nessun **trigger di propagazione** su `published_answers` (la withdrawal/supersede di un published answer non è automatizzata in 8.4: i campi `withdrawn_at`, `superseded_at`, `superseded_by_id` esistono ma non sono guidati da pipeline). In 8.5 è arrivato il path withdrawal asincrono via API + consumer dedicato. **8.7G/H + 8.8A-GATE-CODE + 8.8B-REPORT non hanno aggiunto nulla a questo path**.
- Nessun **stato `claim_ledger_entries.state`** del tipo `source_quality_downgraded` né `entailment_failed`. Gate read-only sulle relative tabelle. **8.8B-REPORT è anch'esso read-only** su tutte le tabelle.

### Cosa è arrivato dopo

`0006_lifecycle.sql` (8.5) ha introdotto lifecycle/source-loss. La 8.6 minima ha aggiunto endpoint read-only su lifecycle e source-loss. La 8.7B ha introdotto `0007_source_quality.sql`, la 8.7D il mock Source Quality Evaluator, la 8.7E l'orchestrator + integrazione in `task.created`, la 8.7F i due endpoint read. La 8.7G ha introdotto `0008_coverage_gap_source_quality.sql` con i due nuovi kind `source_quality_block` e `source_quality_warning`, ed ha esteso `apps/worker/app/services/final_answer_gate.py` per consumare `source_quality_assessments`. La 8.7H ha aggiunto il realistic flow test root-level che valida end-to-end entrambi i path source-quality. La 8.8A ha introdotto `0009_claim_entailment_checks.sql`, il mock claim entailment checker, l'orchestrator + integrazione in `task.created` con audit `task.entailment_checked`, e l'estensione del Final Answer Gate per consumare `claim_entailment_checks`. Migration `0010_coverage_gap_entailment.sql` ha esteso `coverage_gap_statements.kind` con `entailment_block` e `entailment_warning`. Realistic flow test `tests/test_phase_8_8a_entailment_gate_flow.py`. La 8.8A-READ-A ha aggiunto `GET /api/v1/tasks/{task_id}/claim-entailment`. **La 8.8B-REPORT ha aggiunto `GET /api/v1/tasks/{task_id}/anti-hallucination-report` (CODE-A + CODE-B + CVE-lineage fix), un endpoint strettamente read-only che aggrega task / publication / gate / claims / evidence / CVE-lite / Source Quality / Claim Entailment / axis_summary / mock_indicators / limitations in un singolo response JSON, validato end-to-end dal realistic flow test `tests/test_phase_8_8b_report_flow.py`. Nessuna nuova migration in 8.8B-REPORT.** Vedi `PROJECT_STATE.md` e `PHASE_8_8B_REPORT_PRE.md` (con appendice "Implementation status") per i dettagli. Il prossimo blocco candidato è **UI-PRE**.

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
| `make test` | tutti i test (include i realistic flow 8.5/8.6/8.7H/8.8A/8.8B-REPORT) |
| `make test-db` / `test-shared` / `test-api` / `test-worker` / `test-web` | per modulo |
| `make lock-web` | genera `apps/web/package-lock.json` |
| `make psql` / `make redis-cli` | shell |
| `make clean` | distrugge i volumi |

`make migrate` applica `0001`, `0002`, `0003`, `0004`, `0005`, `0006`, `0007`, `0008`, `0009`, `0010` in ordine. Idempotente: rieseguirla è no-op. Le migration `0011` e successive sono ancora da assegnare (retention distruttiva candidata). **8.8B-REPORT non ha introdotto nuove migration.**

### Realistic flow tests (root-level)

I realistic flow test girano nello stesso pytest session della suite standard e richiedono solo `DATABASE_URL` raggiungibile (Postgres via `make up` + `make migrate`); usano una FakeRedis interna e invocano `dispatch.handle_event` direttamente, quindi non richiedono il worker main loop reale.

````bash
# Solo il realistic flow 8.8B-REPORT (warning + publication_held entailment_block end-to-end osservati via report aggregato)
pytest tests/test_phase_8_8b_report_flow.py -v

# Solo il realistic flow 8.8A-GATE (warning + block path entailment end-to-end)
pytest tests/test_phase_8_8a_entailment_gate_flow.py -v

# Solo il realistic flow 8.7H (warning + block path source quality end-to-end)
pytest tests/test_phase_8_7_source_quality_flow.py -v

# Tutti i realistic flow root-level (8.5 withdrawal/source-loss, 8.6 read API,
# 8.7H source quality, 8.8A claim entailment, 8.8B-REPORT report aggregato)
pytest tests/ -v
````

Il file `tests/test_phase_8_8b_report_flow.py` contiene due test indipendenti:
- `test_anti_hallucination_report_flow_published_warning_path` — warning flow: mock services reali end-to-end → Final Answer Gate approved con `reason_code` ∈ {`all_spans_verified_with_warnings`, `all_spans_verified`} → `published_answers` v1 status='published' → GET report → `publication.status='published'`, claims/evidence popolati, axis_summary coerente, `mock_indicators` tutti True, `limitations` non vuoto. Audit chain valida.
- `test_anti_hallucination_report_flow_publication_held_entailment_block` — block flow: monkeypatch del simbolo `run_claim_entailment_checks` sul consumer module (`_wapp.consumers.task_created`) con uno stub orchestrator che inserisce v1 di `claim_entailment_checks` con `verdict='contradicted'` per ogni pair (necessario perché il mock checker reale non produce `contradicted`) → Final Answer Gate rejected con `reason_code='entailment_block'` → GET report → `publication.status='publication_held'`, `published_answer_id=null`, `gate.decision='rejected'`, `gate.reason_code='entailment_block'`, `coverage_gaps` include `entailment_block` severity='block', almeno un `claim.entailment[].verdict=='contradicted'`, `axis_summary.claim_entailment.contradicted_count >= 1`, `axis_summary.final_gate.has_blocking_gaps=True`. GET `/published-answer` ritorna 404 RESOURCE_NOT_FOUND con `details.resource='published_answers'`.

---

## Smoke test 8.4 (approved scenario end-to-end, aggiornato post-8.8B-REPORT)

````bash
# 1) Crea progetto
PID=$(curl -s -X POST localhost:8000/api/v1/projects \
  -H 'content-type: application/json' \
  -d '{"name":"smoke-88b-demo"}' | jq -r .id)

# 2) Carica un documento .txt con frasi factual (cifre)
DID=$(curl -s -X POST "localhost:8000/api/v1/projects/$PID/documents" \
  -F "file=@evaluation/fixtures/closed_corpus_basic/doc_en.txt;type=text/plain" \
  | jq -r .id)

# 3) Crea il task con documento
TID=$(curl -s -X POST localhost:8000/api/v1/tasks \
  -H 'content-type: application/json' \
  -d "{\"project_id\":\"$PID\",\"objective\":\"smoke 8.8B-REPORT\",\"mode\":\"closed_corpus\",\"document_ids\":[\"$DID\"]}" \
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

# 5) Audit chain (post-8.8A approved: 15 eventi worker-side)
curl -s "localhost:8000/api/v1/tasks/$TID/audit?limit=500" | jq '.items[].event_type'

# 6) Latest del ledger (claim verified visibili)
curl -s "localhost:8000/api/v1/tasks/$TID/claims" | jq

# 7) Endpoint answers 8.4 (con reason_code post-8.7G + 8.8A-GATE)
curl -s "localhost:8000/api/v1/tasks/$TID/draft" | jq
curl -s "localhost:8000/api/v1/tasks/$TID/final-gate-report" | jq
curl -s "localhost:8000/api/v1/tasks/$TID/published-answer" | jq

# 8) Single-row view del published answer
PAID=$(curl -s "localhost:8000/api/v1/tasks/$TID/published-answer" | jq -r .id)
curl -s "localhost:8000/api/v1/published-answers/$PAID" | jq

# 9) Source quality 8.7F (per task)
curl -s "localhost:8000/api/v1/tasks/$TID/source-quality" | jq

# 10) Claim entailment 8.8A-READ-A (per task)
curl -s "localhost:8000/api/v1/tasks/$TID/claim-entailment" | jq
````

### Smoke test 8.8B-REPORT (Anti-Hallucination Report API aggregata)

A task creato (`$TID` dalla sequenza precedente), il report aggregato può essere recuperato in un solo round-trip:

````bash
# 11) Anti-Hallucination Report aggregato (8.8B-REPORT)
curl -s "http://localhost:8000/api/v1/tasks/$TID/anti-hallucination-report" | jq
````

Mini-shape del response (campi top-level; vedi `PROJECT_STATE.md` per il dettaglio per-sezione):

````jsonc
{
  "task_id": "uuid",
  "project_id": "uuid",
  "tenant_id": "uuid",
  "task":          { "status": "...", "objective": "...", "mode": "closed_corpus", ... },
  "publication":   { "status": "published|withdrawn|superseded|publication_held|not_ready|failed|unknown", ... },
  "gate":          { "decision": "approved|rejected|null",
                     "reason_code": "all_spans_verified|all_spans_verified_with_warnings|...|null",
                     "payload": { /* JSONB verbatim */ },
                     "coverage_gaps": [ { "kind": "...", "severity": "...", "axis": "...", "details": {...} }, ... ]
                   },
  "claims":        [ { "logical_claim_id": "uuid", "latest_entry_id": "uuid|null",
                       "evidence_links": [...], "cve_lite": [...],
                       "source_quality": [...], "entailment": [...] }, ... ],
  "evidence":      [ { "evidence_span_id": "uuid", "document_id": "uuid", "quote": "...", "quote_hash": "..." }, ... ],
  "axis_summary":  { "cve_lite": {...}, "source_quality": {...}, "claim_entailment": {...},
                     "final_gate": { "has_blocking_gaps": false, "has_warnings": true, ... } },
  "mock_indicators": { "uses_mock_source_quality": true, "uses_mock_claim_entailment": true,
                       "uses_mock_compiler": true, "uses_mock_cve_lite": true, "notes": [...] },
  "limitations":   [ "Una fonte citata non implica che il claim sia vero.", ... ]
}
````

**Convenzione errori e casi edge:**

- Task inesistente → **404** `RESOURCE_NOT_FOUND` con `details.resource='task_masters'`. Mirror della convenzione 8.4/8.6/8.7F/8.8A-READ-A.
- Task esistente ma senza draft/gate/published → **200** con campi parziali (`publication.status='not_ready'`, `claims=[]`, `evidence=[]`, counters a 0, `mock_indicators` in fallback MVP-0, `limitations` sempre presente).
- Task pre-8.7E / pre-8.8A-WORKER → slot Source Quality / Claim Entailment con `latest_assessment_id`/`latest_check_id` `null` e `missing_count` incrementato in `axis_summary` (nessun dato inventato).
- `published_answers.status` ∈ {`withdrawn`, `superseded`} → **non flatten** a `published`: esposto AS-IS in `publication.status` e in `publication.published_answer_status`.

**Nota importante (disclaimer anti-allucinazione).** **Il report 8.8B-REPORT non è una garanzia di verità e non ricalcola il Final Answer Gate.** È una **vista derivata read-only**: aggrega evidenze e decisioni già persistite, normalizza nomenclatura, espone semantica anti-allucinazione, dichiara mock indicators e limitazioni. **Non introduce nuove decisioni**, **non rivaluta claim né fonti**, **non muta DB**. Le fonti di verità primarie restano le tabelle append-only (`final_gate_reports`, `published_answers`, `claim_ledger_entries`, `verification_records`, `source_quality_assessments`, `claim_entailment_checks`, `coverage_gap_statements`) e gli endpoint read specialistici (8.4 answers, 8.6, 8.7F, 8.8A-READ-A).

**CVE-lite lineage (8.8B-REPORT-CODE-C-FIX).** CVE-lite scrive `verification_records` (`check_kind='cve_lite'`, `check_name='quote_hash_and_substring_v1'`) sulla v1 candidate `claim_ledger_entries`, e poi appende una v2 `verified_fact` (su PASS) o una v2 `unverifiable` (su FAIL) tracciando la transizione tramite `claim_lineage(relation_kind='supersedes')`. Il report è keyed per la **latest entry** (tipicamente v2), mentre il `verification_records` resta legato a v1: il report aggrega correttamente i record CVE-lite letti **sia dalla latest entry sia dal parent v1 superseded dalla latest entry**, via JOIN su `claim_lineage` con `relation_kind='supersedes'`. Regression coperta da `test_get_anti_hallucination_report_maps_parent_cve_record_to_latest_entry`.

Il reason code atteso di default in approved con il mock attuale è **`all_spans_verified_with_warnings`** (warning Source Quality sempre presenti; warning entailment occasionali). Il report 8.8B-REPORT espone questo come `gate.reason_code` verbatim e popola `coverage_gaps` di conseguenza, con `axis` decorato.

Smoke test rejected zero-verified: come sopra ma con un documento privo di frasi che superino CVE-lite (oppure forzando `quote_hash` non corrispondente in test). Il task termina in `analyzed_partial`; `GET /final-gate-report` restituisce `decision='rejected'`, `reason_code='no_verified_claims'`, una `coverage_gap_statements` con `kind='missing_evidence'`, `gap_key='no_verified_claims'`; `GET /published-answer` restituisce `404 RESOURCE_NOT_FOUND` con `details.resource='published_answers'`. `GET /anti-hallucination-report` espone `publication.status='publication_held'` (gate rejected senza published), `gate.reason_code='no_verified_claims'`, `axis_summary.final_gate.has_blocking_gaps=True`. **Né Source Quality né Entailment vengono consultati in questo branch (priorità CVE-lite > Entailment > Source Quality).**

Smoke test rejected `source_quality_block` o `entailment_block`: non riproducibili da smoke test end-to-end via curl senza un evaluator/checker reale o un seed manuale di `source_quality_assessments` / `claim_entailment_checks`, perché **il mock evaluator deterministico non produce `unsuitable`** e **il mock checker deterministico non produce `contradicted`**. Coperti a livello unit dai test worker `apps/worker/tests/test_final_answer_gate_source_quality.py` (13 scenari) e `apps/worker/tests/test_final_answer_gate_entailment.py` (13 scenari), e a livello end-to-end dai realistic flow `tests/test_phase_8_7_source_quality_flow.py` (8.7H), `tests/test_phase_8_8a_entailment_gate_flow.py` (8.8A-GATE-FLOW) e **`tests/test_phase_8_8b_report_flow.py` (8.8B-REPORT-FLOW)**, che attivano i rispettivi branch via monkeypatch dell'orchestrator nel consumer e osservano l'esito anche tramite il report aggregato.

---

## Architettura runtime (sintesi)

Pipeline 8.4 + 8.7E + 8.7G + 8.8A + 8.8A-GATE-CODE nel worker, con osservabilità HTTP read-only aggiunta da 8.8B-REPORT:

````
Client
  │
  ▼
API FastAPI
  │
  ├──► PostgreSQL
  │       - task, documenti, evidenze, claim ledger
  │       - source quality (8.7), claim entailment (8.8A)
  │       - gate report, published answer, audit chain
  │
  ├──► Redis Stream (task.created, ...)
  │       │
  │       ▼
  │     Worker (pipeline 8.4 + step 8.7E + step 8.8A + Gate 8.7G + 8.8A-GATE)
  │       │
  │       ├── Extractor → CVE-lite → Source Quality → Claim Entailment
  │       └── Compiler → Final Answer Gate
  │             ├── approved / approved-with-warnings → published
  │             └── rejected → publication_held
  │                    ├── source_quality_block (8.7H via stub)
  │                    └── entailment_block      (8.8A-GATE-FLOW via stub,
  │                                                osservato anche da
  │                                                8.8B-REPORT-FLOW)
  │
  └──► GET /api/v1/tasks/{id}/anti-hallucination-report  (8.8B-REPORT, NUOVO)
         vista task-level read-only aggregata su:
           publication, gate, claims, evidence,
           CVE-lite, Source Quality, Claim Entailment,
           axis_summary, mock_indicators, limitations
         strettamente read-only:
           - no INSERT/UPDATE/DELETE
           - no worker call
           - no Redis
           - no ricalcolo del Gate
````

Note (post-8.8B-REPORT):

- L'endpoint 8.8B-REPORT non altera la pipeline. Legge `task_masters`, `draft_final_answers`, `final_gate_reports`, `coverage_gap_statements`, `published_answers`, `logical_claims`, `claim_ledger_entries`, `claim_evidence_links`, `verification_records`, `source_quality_assessments`, `claim_entailment_checks`, `task_documents`, `uploaded_documents`, `document_versions`, `document_chunks`, `evidence_spans`, `claim_lineage` — tutte in SELECT.
- Il report usa **latest assoluto DB-level** per target/pair (coerente col Gate). Si distingue dalla semantica "latest in slice" di 8.7F e dalla cronologia globale di 8.8A-READ-A. Le tre superfici sono complementari: 8.7F / 8.8A-READ-A per drill-down dettagliato, 8.8B-REPORT per la vista consolidata task-level.
- Il report aggrega CVE-lite tramite `claim_lineage(relation_kind='supersedes')`: i `verification_records` scritti sulla v1 parent vengono mappati sotto la v2 latest entry. Vedi sezione "CVE-lite lineage" sopra.
- Eventuali `coverage_gap_statements` di kind `source_loss` possono apparire in `gate.coverage_gaps` con `axis='source_loss'`, ma il report **non** include lifecycle/source-loss event details (rinviati, dichiarati in `limitations`).

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
- [`PHASE_8_8B_REPORT_PRE.md`](PHASE_8_8B_REPORT_PRE.md) — piano architetturale 8.8B-REPORT (con appendice "Implementation status")
