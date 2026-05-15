# PHASE_8_7_PLAN — Source Quality Evaluator / Evidence Quality Layer

Documento di **piano architetturale** per la Fase 8.7 dell'Evidence-First MVP-0. La fase introduce il primo modulo dedicato alla valutazione della **qualità delle fonti** che supportano i claim del Claim Ledger.

> **Stato di questo documento.**
>
> Documento nato come piano 8.7A; aggiornato dopo 8.7F al commit `91397ae6f02abd429cff29b6e0248cf9a7c16317`.
>
> I blocchi **8.7A–8.7F sono implementati**; **8.7G e 8.7H restano da fare**. Le sezioni di questo documento sono state allineate dove rilevante; le formulazioni "non implementato" originarie sono state superate per 8.7B/C/D/E/F e mantenute per 8.7G/H.

**Commit di partenza del piano**: `7cbd45ae416ead0b2f5221ace4925dee374fa0c9`.
**Commit di allineamento documentale**: `91397ae6f02abd429cff29b6e0248cf9a7c16317`.

**Collegamento logico**: la Fase 8.6 ha reso **osservabili** via HTTP read-only gli eventi lifecycle e la propagazione della source loss. La Fase 8.7 ha cominciato a **valutare la qualità** delle fonti che il sistema usa per supportare claim. La 8.6 osserva; la 8.7 valuta. La 8.7G consumerà.

---

## Stato dei blocchi 8.7

| Blocco | Descrizione | Stato |
|---|---|---|
| 8.7A | Plan (`PHASE_8_7_PLAN.md`) | **done** |
| 8.7B | DB schema (`migrations/0007_source_quality.sql`) | **done** |
| 8.7C | Shared schemas (`SOURCE_QUALITY_*_VALUES`, Literal aliases, `SourceQualityAssessmentRead`) | **done** |
| 8.7D | Mock Source Quality Evaluator service | **done** |
| 8.7E | Worker integration (W-A) — step in `task_created` con SAVEPOINT + audit aggregato | **done** |
| 8.7F | Read API (due endpoint GET su evidence_span e su task) | **done** |
| 8.7G | Gate integration (Source Quality Gate policy) | **next** |
| 8.7H | Realistic flow + docs finalization | **pending** |

---

## 1. Stato di partenza

Al commit `7cbd45a` il repo offriva gli elementi rilevanti per la 8.7. Tutto ciò che segue è verificabile leggendo i file indicati; nessuno di questi elementi è stato modificato dalla 8.7B/C/D/E/F.

### 1.1 Schema DB già applicato (migrations 0001–0006)

- **Storage e documenti** (`0002_storage.sql`, `0003_documents.sql`):
  - `storage_blobs`, `storage_objects` (content-addressed, deduplicato, refcount-based).
  - `uploaded_documents` con colonne `tier`, `language`, `mime_type`, `content_hash`, `size_bytes`, `created_by`.
  - `document_versions`, `document_chunks`, `evidence_spans` append-only, `prompt_injection_flags` placeholder.

- **Claim Ledger** (`0004_claim_ledger.sql`):
  - `logical_claims`, `raw_claims`, `classified_claims`.
  - `claim_ledger_entries` append-only, supersede via `claim_lineage.relation_kind='supersedes'`.
  - `claim_evidence_links` (CHECK `cel_origin_xor`).
  - `verification_records` con `check_kind ∈ {csv, cve_lite, nli, judge}`.
  - placeholder per `contradiction_records`, `claim_support_links`, `human_review_requests`.

- **Answers / Gate / Published** (`0005_answers_gate.sql`):
  - `agent_runs`, `draft_final_answers`, `final_answer_spans`, `final_answer_span_claim_links`.
  - `final_gate_reports` append-only, UNIQUE per draft, FK composita.
  - `published_answers` con `status ∈ {published, withdrawn, superseded}`.
  - `coverage_gap_statements`.

- **Lifecycle e source loss** (`0006_lifecycle.sql`):
  - `published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records`.

### 1.2 Endpoint API attivi al commit di partenza

Read-only rilevanti per la 8.7 (ora coesistono con i due endpoint 8.7F implementati):

- `POST /api/v1/projects/{id}/documents`, `GET /api/v1/documents/{id}`, `GET /api/v1/documents/{id}/chunks`.
- `GET /api/v1/claims/{logical_id}/evidence`.
- `GET /api/v1/source-loss-events/{id}` (8.6B), `/propagation` (8.6C).
- `GET /api/v1/tasks/{task_id}/source-loss-events` (8.6D).

### 1.3 Servizi worker attivi al commit di partenza

- `services/extractor.py`, `services/cve_lite.py`, `services/final_answer_gate.py`, `services/source_loss_propagator.py`, `services/published_answer_lifecycle.py`.

### 1.4 Chiarimento critico sullo stato attuale

Tre affermazioni che hanno vincolato (e continuano a vincolare) la progettazione della 8.7:

1. **Una `evidence_span` collegata a un claim NON significa automaticamente fonte affidabile.** L'autorevolezza, la freschezza, l'indipendenza e la rilevanza del documento sottostante NON sono mai state valutate prima della 8.7.

2. **`verified_fact` (state del Claim Ledger) significa esclusivamente "supporto verificato secondo il CVE-lite mock-driven"**: la quote esiste nel chunk con il suo hash atteso. Non significa che la fonte sia di qualità.

3. **Source loss gestisce la perdita o invalidazione di una fonte**, non la qualità iniziale della fonte. Un documento perfettamente accessibile può comunque essere debole, secondario, datato o non indipendente.

La 8.7 ha introdotto questi giudizi come **dimensioni separate** in `source_quality_assessments`, senza confonderle con la verifica testuale o con la source loss.

---

## 2. Definizione realistica di source quality

Per MVP-0 "source quality" è un giudizio **strutturale, multi-dimensionale, dichiarato e append-only/versionato** su una fonte. Le dimensioni sotto sono progettate per essere **ortogonali**.

### 2.1 Tassonomia (codomini implementati in 8.7B + 8.7C)

I codomini sono fissati come stringhe-enum a livello DB (CHECK constraint in `0007_source_quality.sql`) e come tuple Python in `packages/shared/evidencefirst_shared/schemas.py` (`SOURCE_QUALITY_*_VALUES`).

#### `source_type`
- `user_document` | `web_page` | `academic_paper` | `official_document` | `database_record` | `news_article` | `blog` | `forum` | `unknown`

In MVP-0 closed-corpus, la quasi totalità delle fonti è `user_document`. Il codominio prevede già i valori web/news/etc. per riusabilità futura.

#### `source_role`
- `primary` | `secondary` | `tertiary` | `unclear`

#### `authority_level`
- `high` | `medium` | `low` | `unknown`

#### `independence_level`
- `independent` | `affiliated` | `self_reported` | `unknown`

#### `freshness`
- `current` | `recent` | `stale` | `undated` | `not_time_sensitive`

#### `relevance`
- `direct_support` | `contextual_support` | `weak_support` | `irrelevant`

#### `extract_quality`
- `exact_quote_match` | `paraphrase_match` | `partial_match` | `quote_mismatch`

`quote_mismatch` qui è un fatto di qualità dell'estratto; lo stesso fatto in `source_loss_events` è un fatto di perdita di fonte. Le due tabelle restano separate.

#### `contradiction_status`
- `no_known_contradiction` | `contradicted_by_stronger_source` | `conflicting_sources` | `unchecked`

In 8.7 il valore di default è `unchecked` (mock). Detector reale rinviato a 8.8C.

#### `overall_quality`
- `strong` | `adequate` | `weak` | `unsuitable` | `unknown`

NON derivato automaticamente da formule opache. Prodotto da una funzione di policy esplicita.

#### `confidence`
`DOUBLE PRECISION` in `[0.0, 1.0]` o NULL.

**Score interno, non verità assoluta.** Non deve essere usato dal Final Answer Gate come unica chiave decisionale.

### 2.2 Append-only / versionato (implementato in 8.7B)

Tutte le dimensioni sono registrate in modo **append-only** (trigger `source_quality_assessments_append_only` su `reject_modify_append_only`) e **versionato** (`version_no` monotonicamente crescente per `(target_kind, target_id)`, partial unique indexes `sqa_evidence_version_uq` / `sqa_chunk_version_uq` / `sqa_document_version_uq`).

Motivazioni:
- coerenza con le altre tabelle append-only;
- auditabilità nel tempo;
- abilità di rieseguire l'assessment con una policy diversa senza riscrivere la storia;
- compatibilità con un futuro detector di drift.

---

## 3. Cosa la 8.7 NON è (rigorosamente)

Invarianti semantiche fondative:

1. **Source quality ≠ claim correctness.**
2. **Source quality ≠ evidence support.**
3. **Source quality ≠ verification outcome.**
4. **Source quality ≠ source loss.**
5. **Source quality ≠ final publication eligibility.**

Vedi `PROJECT_STATE.md` "Semantica Source Quality" per la spiegazione operativa.

---

## 4. Modello dati — Opzione A IMPLEMENTATA

Confronto storico tra opzioni mantenuto per memoria progettuale. **L'Opzione A è stata implementata in `migrations/0007_source_quality.sql`** (blocco 8.7B).

### 4.1 Opzione A — Nuova tabella dedicata `source_quality_assessments` (IMPLEMENTATA)

Schema effettivamente applicato (vedi `migrations/0007_source_quality.sql` per la fonte autorevole):

- Tabella `source_quality_assessments` con `id` UUID, `tenant_id` NOT NULL FK, `project_id` FK NULL-able, tre colonne target (`evidence_span_id`, `document_chunk_id`, `document_id`) con CHECK `sqa_target_xor`.
- `version_no` INTEGER NOT NULL `CHECK (version_no >= 1)`.
- Nove dimensioni di qualità con CHECK enum.
- `confidence` DOUBLE PRECISION CHECK range `[0.0, 1.0]` o NULL.
- `evaluator_name`, `evaluator_version`, `policy_name`, `policy_version` come stringhe opache (nessun FK a `policy_versions`).
- `idempotency_key`, `payload` JSONB DEFAULT `'{}'`, `created_at`.
- Sei partial unique indexes: tre per versioning + tre per idempotency, uno per target kind.
- Trigger append-only standard.
- Indici di lookup su `tenant_id`, `project_id`, target per granularità, `overall_quality`, `source_role`, `freshness`.

**Pro effettivi (osservati dopo l'implementazione):**
- Separazione semantica netta dal Claim Ledger.
- Append-only standard via trigger condiviso.
- Test isolation eccellente (il modulo si testa senza interferire con CVE-lite o gate).
- Permette assessment a granularità diverse senza join sintetici.

**Contro effettivi:**
- Una tabella in più.
- I tre partial unique indexes richiedono attenzione (non un singolo UNIQUE sulla coppia (target, version)).

### 4.2 Opzione B — Riuso di `verification_records` con nuovi `check_kind` (SCARTATA)

Confusione semantica tra "verifica testuale" e "qualità della fonte", modifica vincolante del CHECK in 0004, difficoltà di versioning aggregato. **Non implementata.**

### 4.3 Opzione C — Ibrida (NON SCELTA)

Combinazione di A + sotto-check eventuali in `verification_records`. **Non implementata**: la complessità non era giustificata in MVP-0.

### 4.4 Decisione finale

Opzione A implementata in 8.7B. Storicamente raccomandata; confermata in implementazione.

---

## 5. Interazione con Claim Ledger (invariante, comportamento M1 attivo)

La 8.7 NON cancella, NON modifica e NON sostituisce le `claim_ledger_entries` esistenti. Le invarianti enunciate originariamente restano in piedi:

1. **Append-only stretto preservato.**
2. **Nessuna estensione di `claim_lineage.relation_kind`.**
3. **Nessuna estensione di `claim_ledger_entries.state`.**
4. **Distinzione semantica rigorosa.**

### 5.2 Comportamento attivo: M1 (solo metadata)

Lo stato corrente del sistema implementa **M1**:
- L'assessment vive nella tabella `source_quality_assessments`.
- `claim_ledger_entries` resta invariata.
- Il Final Answer Gate NON consulta gli assessment in 8.4/8.7F. La consultazione è 8.7G.

**M2** (superseding del claim per source_quality_downgrade) resta una opzione di policy futura, esplicitamente rinviata e non scelta in 8.7G come default.

### 5.3 Distinzione dei quattro assi

| Asse                               | Domanda                                                | Dove vive                                                                 |
| ---------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------- |
| Claim correctness                  | Il claim è vero?                                       | (non valutato in MVP-0)                                                   |
| Evidence support                   | C'è almeno un'evidenza ben formata per questo claim?   | `claim_evidence_links`                                                    |
| Source quality                     | Le fonti di quell'evidenza sono buone?                 | `source_quality_assessments` (implementata 8.7B)                          |
| Final publication eligibility      | Il sistema deve pubblicare questo claim?               | `final_gate_reports` + policy 8.7G (pendente)                             |

---

## 6. Interazione con Final Answer Gate

**Stato corrente: il Gate NON è modificato.** La 8.7G introdurrà la policy. Le policy candidate restano in piedi.

### 6.1 Regola attuale (8.4, invariata)

Uno span è verified-backed se e solo se:
```
link.claim_ledger_entry_id == latest_entry_id_for(claim_logical_id)
AND latest_entry_state_for(claim_logical_id) == 'verified_fact'
```

### 6.2 Policy candidate per 8.7G

- **P1 — block on uniformly weak support.** Block se tutte le evidence_spans di uno span hanno `overall_quality ∈ {weak, unsuitable}`.
- **P2 — require strong support for sensitive claims.** Per claim "rilevanti", richiesta `overall_quality='strong'` + `source_role='primary'`.
- **P3 — flag secondary-only support.** Non blocca ma marca lo span come secondary-only.
- **P4 — downgrade confidence on stale or non-independent sources.** Non blocca, abbassa una confidence aggregata.
- **P5 — publish with disclosure on weak-but-declared sources.** Pubblicazione con nota.

Proposta di default per 8.7G-PRE: **P1 + P5** (block stretto + escape esplicito).

### 6.3 Coverage gap kinds

Per evitare di mischiare "claim non verificato" con "fonte debole", si raccomanda di introdurre in una migration separata (probabilmente in concomitanza con 8.7G) un nuovo `kind='source_quality_block'` su `coverage_gap_statements`, NON in 8.7B. Decisione formale rinviata a 8.7G-PRE.

### 6.4 Nessun cambiamento contrattuale prima di 8.7G

Il gate corrente continua a usare la regola "verified-backed". 8.7B/C/D/E/F non lo alterano. 8.7G discute e implementa la policy.

---

## 7. Interazione con Source Loss (invariata)

I due concetti restano distinti:

| Concetto                | Significato                                                                    | Dove vive                                                  |
| ----------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------- |
| Source loss             | Fonte persa, inaccessibile, modificata, quote non più riconducibile            | `source_loss_events`, `source_loss_propagation_records`    |
| Source quality          | Fonte presente ma debole / obsoleta / non indipendente / non primaria          | `source_quality_assessments` (implementata 8.7B)           |

Le invarianti incrociate restano in piedi: la 8.7 NON modifica le tabelle source_loss; un source_loss event può ESSERE seguito da un assessment di qualità, mai sostituito da esso; un assessment non emette `source_loss_events`.

---

## 8. API read-only — STATO IMPLEMENTATO (8.7F)

### 8.1 `GET /api/v1/evidence-spans/{evidence_span_id}/source-quality` — IMPLEMENTATO

Vedi `apps/api/app/routes/source_quality.py` per la fonte autorevole.

- 404 `RESOURCE_NOT_FOUND` `details.resource="evidence_spans"` se lo span non esiste.
- 200 con `items=[]` e `latest_assessment=null` se non esistono assessment.
- 200 con wrapper `{evidence_span_id, latest_assessment, items}`, items ordinati ASC per `(version_no, created_at, id)`, `limit` 1–5000 default 100.
- `latest_assessment` = ultimo elemento dello slice (massimo `version_no` tra gli items restituiti).

### 8.4 `GET /api/v1/tasks/{task_id}/source-quality` — IMPLEMENTATO

- 404 `RESOURCE_NOT_FOUND` `details.resource="task_masters"` se il task non esiste.
- 200 con un item per evidence_span linkato al task via `claim_evidence_links` JOIN `logical_claims`; span senza assessment esposti con `latest_assessment=null` e `items=[]`.
- `summary` con `evidence_spans_total`, `spans_with_assessment`, `spans_without_assessment`, `latest_overall_quality_counts` sul codominio `overall_quality` (`{strong, adequate, weak, unsuitable, unknown}`, sempre tutti presenti con default 0).
- I counts del summary considerano solo l'`latest_assessment` per span.

### 8.2 / 8.3 / 8.5 — FUTURI / OPZIONALI

I seguenti endpoint restano future/opzionali, NON implementati in 8.7F:

- `GET /api/v1/documents/{id}/source-quality` (document-level).
- `GET /api/v1/claims/{logical_id}/source-quality` (claim-level con rollup).
- `GET /api/v1/published-answers/{id}/source-quality-report` (post-fatto sul published).

Verranno valutati quando emergerà un consumatore concreto (UI, reporting, ecc.).

### 8.6 Invarianti comuni (osservate in 8.7F)

- Read-only end-to-end (no INSERT/UPDATE/DELETE; nessun import di codice worker).
- Schemi shared dedicati (`SourceQualityAssessmentRead` in `packages/shared/evidencefirst_shared/schemas.py`).
- 404 normalizzati secondo la convenzione `details.resource`.
- JSONB esposti verbatim (RBAC redaction = debito futuro).
- Nessun nuovo `ErrorCode`.

---

## 9. Worker / pipeline — W-A IMPLEMENTATA (8.7E)

### 9.1 Opzione W-A — Step sincrono dentro `task.created` (IMPLEMENTATA)

Stato:
- Source quality eseguita nel `task_created` fresh path **dopo `task.analyzed_partial`** e **prima di `task.compiling`**, dentro `_run_8_3_extract_and_verify`.
- Implementata via `apps/worker/app/services/source_quality_orchestrator.py` che chiama `assess_source_quality` per ogni `evidence_span_id` linkato ai claim del task via `claim_evidence_links` JOIN `logical_claims`.
- Idempotency key deterministica: `task:{task_id}:span:{evidence_span_id}:v1`.
- **Nessun nuovo stream Redis. Nessun nuovo consumer. Nessuna modifica al dispatcher.**
- Singolo audit aggregato `task.source_quality_assessed` sulla chain del task, con `status='completed'|'failed'` e payload con counts.
- **Fallimento source quality non blocca 8.4**: la chiamata all'orchestrator è wrappata in `conn.begin_nested()` (SAVEPOINT). Su eccezione: rollback del savepoint + audit `failed` con `error_type` (no stack trace) + pipeline continua.
- Sui resume da `compiling` o `analyzed_partial` lo step **non** viene re-eseguito.

### 9.2 Opzione W-B — Consumer asincrono dedicato

Non implementata. Resta candidato per quando arriverà un evaluator reale (con eventuale web search).

### 9.3 / 9.4 — W-C / W-D

Non implementate. Rinviate.

### 9.5 Trade-off osservati

- **Semplicità MVP**: W-A ha vinto e funziona; integrazione contenuta.
- **Idempotenza**: garantita via key deterministica + UNIQUE `(target_id, idempotency_key)`.
- **Audit trail**: singolo aggregato; copre success e failed senza affollare la chain.
- **Costo computazionale**: trascurabile (mock).
- **Compatibilità futura**: per provider reali / web search si dovrà valutare il passaggio a W-B.

---

## 10. Test plan — STATO

Test implementati nei blocchi 8.7D/E/F (riferimenti ai file presenti nel repo):

- `apps/worker/tests/test_source_quality_evaluator_service.py` — 14 scenari.
- `apps/worker/tests/test_source_quality_orchestrator.py` — 7 scenari.
- `apps/worker/tests/test_task_created_source_quality_step.py` — 4 scenari (incluso savepoint rollback + audit `failed`).
- `apps/worker/tests/test_consumer_with_documents.py` — 14 eventi nella sequenza approved (incluso `task.source_quality_assessed` al posto 9 / 14 e nella sequenza rejected).
- `apps/api/tests/test_source_quality_read_endpoint.py` — read API 8.7F (test passati riportati dall'utente).

Test plan ancora da implementare (in 8.7G/H):

- Final Answer Gate policy test (8.7G).
- Realistic flow test `tests/test_phase_8_7_source_quality_flow.py` (8.7H).

---

## 11. Non-obiettivi (esplicitamente fuori scope per 8.7)

Restano fuori scope per tutta la 8.7 (compresi 8.7G/H):

- Web search reale, provider AI reali, crawling/scraping.
- UI dedicata per source quality.
- RBAC reale o redaction di JSONB.
- Retention policy distruttiva.
- Scoring "perfetto", algoritmi di reputazione cross-tenant.
- Verità assoluta sulla fonte.
- Ranking commerciale o monetario.
- Withdrawal automatico da source quality.
- Modifica della propagazione source loss.
- Estensione di `claim_lineage.relation_kind`.
- Estensione di `verification_records.check_kind`.
- Modifica del CVE-lite o dell'extractor.

Per 8.7G in particolare resta ammessa solo: una migration aggiuntiva separata che eventualmente estenda `coverage_gap_statements.kind` per introdurre `source_quality_block`, da decidere in 8.7G-PRE.

---

## 12. Roadmap a blocchi — stato

| Blocco | Descrizione | Stato |
|---|---|---|
| 8.7A | `PHASE_8_7_PLAN.md` | done |
| 8.7B | `migrations/0007_source_quality.sql` | done |
| 8.7C | Shared schemas | done |
| 8.7D | Mock Source Quality Evaluator service | done |
| 8.7E | Worker integration (W-A) | done |
| 8.7F | Read API (due endpoint) | done |
| 8.7G | Gate policy integration + eventuale `coverage_gap_statements.kind='source_quality_block'` | next |
| 8.7H | Realistic flow tests + docs finalization | pending |

Variante accettabile (rispettata): chiusura intermedia a 8.7F. Gli assessment sono scritti e osservabili via HTTP; il gate resta invariato. La distinzione tra "qualità misurata" e "qualità decisionale" è la conquista intermedia.

---

## 13. Anti-Hallucination roadmap

> **Il progetto non promette di impedire a un LLM di generare internamente output errati. Promette di impedire che claim fattuali non supportati, contraddetti o basati su fonti inadeguate vengano pubblicati come affidabili.**

Roadmap successiva alla 8.7F, da affrontare in blocchi separati:

- **8.7G — Source Quality nel Final Answer Gate.** Integrazione decisionale: il gate consulta `source_quality_assessments` e applica una policy (proposta: P1 block + P5 disclosure). Eventuale introduzione di `coverage_gap_statements.kind='source_quality_block'` in una migration separata (da decidere in 8.7G-PRE).
- **8.8A — Claim Entailment Checker.** Verifica che la quote effettivamente implichi (o sia compatibile con) il claim, non solo che sia testualmente presente. Oggi `verification_records.check_kind='cve_lite'` verifica solo presenza testuale.
- **8.8B — Citation-to-Claim Validator.** Verifica che il claim citi le evidenze corrette, non evidenze "vicine" che non lo supportano davvero.
- **8.8C — Contradiction Detector.** Detector reale di contraddizioni tra claim o tra fonti. Oggi `contradiction_records` placeholder, `source_quality.contradiction_status='unchecked'` per costruzione.
- **8.8D — Final Answer Sentence Gate.** Gate a livello frase del published_answer, non solo a livello span verified-backed (uno span può contenere prosa generata dal compiler che eccede ciò che la quote effettivamente supporta).
- **8.8E — Anti-Hallucination Report API.** Endpoint aggregato che espone, per un published_answer, lo stato di tutti gli assi (entailment, citation, contradiction, source quality, source loss).
- **8.9 — External Verification / Web-RAG controllato.** Verified Web Mode con cattura immutabile delle fonti recuperate (`retrieved_sources`/`retrieved_chunks`/`retrieved_source_spans` previste in schema futuro).
- **9.0 — Multi-agent consensus + adversarial review reale.** Provider AI reali, consensus engine, critical reviewer adversariale.

---

## 14. Rischi residui

Rischi specifici allo stato post-8.7F:

- **Source Quality mock deterministic.** L'evaluator scrive sempre `overall_quality='unknown'` e `confidence=0.5`. Le altre dimensioni sono fissate dalla policy mock. Nessuna valutazione semantica è realmente effettuata.
- **Gate non ancora integrato.** Gli assessment sono scritti ma non consumati dal Final Answer Gate. Un consumatore disattento potrebbe trattare l'esistenza dell'assessment come "approvazione di qualità": NON lo è.
- **Payload JSONB esposto senza RBAC.** Gli endpoint 8.7F restituiscono `payload` verbatim. Debito già noto in 8.6, non risolto in 8.7F.
- **Task pre-8.7E senza assessment.** I task processati prima dell'integrazione 8.7E non hanno righe in `source_quality_assessments`. Nessun backfill: gli endpoint 8.7F restituiscono `items=[]` per quei task. Comportamento coerente con lo stato DB, ma può sorprendere chi cerca uniformità di copertura.
- **No backfill** previsto in 8.7G/H.
- **No Claim Entailment Checker, no Citation-to-Claim Validator, no Contradiction Detector reale, no Final Answer Sentence Gate, no External Verification.** Tutti rinviati a 8.8x e 8.9.
- **"unknown" non deve essere interpretato come approvazione forte.** L'enum `overall_quality='unknown'` significa letteralmente "il sistema oggi non sa". Una policy 8.7G ingenua che trattasse `unknown` come "passa" produrrebbe falsi positivi sistemici. La policy 8.7G dovrà gestire `unknown` esplicitamente.
- **Falso senso di sicurezza da score numerici.** `confidence=0.5` costante oggi: nessuna informazione utile. Quando un evaluator reale produrrà valori variabili, sarà tentante leggerli come verità.
- **Bias verso fonti istituzionali.** Una futura policy di `authority_level='high'` ingenua marcherebbe ogni documento "ufficiale" come autorevole. Mitigazione: la policy resta esplicita e versionata via `(policy_name, policy_version)`.
- **Domini eterogenei** (scientifico/legale/news) richiedono criteri diversi non scrivibili in un unico CHECK.
- **Assenza di web search reale**: indipendenza, corroborazione e freschezza esterne restano largamente `unknown`.
- **Fonti user-provided autorevoli ma non verificabili esternamente.**
- **Rischio di overblocking** in 8.7G se P1 viene calibrata troppo severamente.
- **Rischio di underblocking** finché 8.7G non viene scritta.
- **Costo computazionale futuro** quando si introdurrà un evaluator reale.
- **N+1 query** nel task endpoint 8.7F (loop su evidence_span_id): accettato per MVP-0, batchabile in futuro.
- **Coesistenza con retention.** Il numero `0007` è ora occupato da source_quality; la retention futura (storica `0007_evaluation_retention.sql`) deve essere rinominata a `0008_*` o successiva.

---

## 15. Decisione documentale

- **8.7A**: `PHASE_8_7_PLAN.md` creato (questo file nella sua versione originale).
- **8.7B**: `migrations/0007_source_quality.sql` scritta e applicata.
- **8.7C**: `packages/shared/evidencefirst_shared/schemas.py` esteso con codomini e `SourceQualityAssessmentRead`.
- **8.7D**: `apps/worker/app/services/source_quality_evaluator.py` scritto.
- **8.7E**: `apps/worker/app/services/source_quality_orchestrator.py` scritto; `apps/worker/app/consumers/task_created.py` integrato con step SAVEPOINT-protetto e audit aggregato.
- **8.7F**: `apps/api/app/routes/source_quality.py` scritto; registrato in `apps/api/app/main.py`.
- **8.7G**: pending (next).
- **8.7H**: pending.
- **`PHASE_8_6_PLAN.md`** non modificato.
- **`PROJECT_STATE.md`** aggiornato (post-8.7F) a riflettere stato implementato + roadmap.
- **`README.md`**: aggiornamento minimo di nota di stato post-8.7F.

---

FILE_COMPLETATI (8.7A–F, cumulativo)
- `PHASE_8_7_PLAN.md` (8.7A; aggiornato post-8.7F)
- `migrations/0007_source_quality.sql` (8.7B)
- `packages/shared/evidencefirst_shared/schemas.py` (8.7C)
- `apps/worker/app/services/source_quality_evaluator.py` (8.7D)
- `apps/worker/app/services/source_quality_orchestrator.py` (8.7E)
- `apps/worker/app/consumers/task_created.py` (integrazione 8.7E)
- `apps/api/app/routes/source_quality.py` (8.7F)
- `apps/api/app/main.py` (registrazione router 8.7F)
- Test 8.7D/E/F worker + API

FILE_DA_FARE_PROSSIMO_BLOCCO
- `PHASE_8_7G_PRE.md` — analisi rigorosa del Final Answer Gate e decisione policy Source Quality Gate (P1/P2/P3/P4/P5, gestione `unknown`, eventuale `coverage_gap_statements.kind='source_quality_block'`).

RISCHI_RESIDUI (sintesi, vedi §14 per il dettaglio)
- Source Quality mock deterministic (`overall_quality='unknown'`, `confidence=0.5`).
- Gate non ancora integrato; assessment scritti ma non consumati.
- Payload JSONB esposto senza RBAC.
- Task pre-8.7E senza assessment, no backfill.
- No Claim Entailment Checker, no Citation-to-Claim Validator, no Contradiction Detector reale, no Final Answer Sentence Gate, no External Verification (rinviati a 8.8x/8.9).
- "unknown" non equivale ad approvazione forte: deve essere gestito esplicitamente dalla policy 8.7G.
- Rischio overblocking / underblocking nel futuro Gate policy.
- Coesistenza retention: `0008_*` da assegnare quando si scriverà.
