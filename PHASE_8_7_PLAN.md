# PHASE_8_7_PLAN — Source Quality Evaluator / Evidence Quality Layer

Documento di **piano architetturale** per la Fase 8.7 dell'Evidence-First MVP-0. La fase introduce il primo modulo dedicato alla valutazione della **qualità delle fonti** che supportano i claim del Claim Ledger, **e l'integrazione decisionale di quella valutazione nel Final Answer Gate**.

> **Stato di questo documento.**
>
> Documento nato come piano 8.7A; aggiornato dopo 8.7F al commit `91397ae6f02abd429cff29b6e0248cf9a7c16317`; aggiornato di nuovo dopo 8.7G al commit `79815764cd8c588556b81c5914b61deb16eb7370`.
>
> I blocchi **8.7A–8.7G sono implementati**; **8.7H resta da fare**. Le sezioni di questo documento sono state allineate dove rilevante.

**Commit di partenza del piano**: `7cbd45ae416ead0b2f5221ace4925dee374fa0c9`.
**Commit di allineamento documentale post-8.7F**: `91397ae6f02abd429cff29b6e0248cf9a7c16317`.
**Commit di allineamento documentale post-8.7G**: `79815764cd8c588556b81c5914b61deb16eb7370`.

**Collegamento logico**: la Fase 8.6 ha reso **osservabili** via HTTP read-only gli eventi lifecycle e la propagazione della source loss. La Fase 8.7 ha cominciato a **valutare la qualità** delle fonti che il sistema usa per supportare claim (8.7B–F), e l'8.7G ha reso quella valutazione **consumata** dal Final Answer Gate.

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
| 8.7G-PRE | Policy analysis (`PHASE_8_7G_PRE.md`) | **done** |
| 8.7G-CODE | Migration `0008_coverage_gap_source_quality.sql` + Gate integration | **done** |
| 8.7G-DOC | Documentazione post-8.7G (`PROJECT_STATE.md`, `PHASE_8_7_PLAN.md`, `README.md`) | **done** |
| 8.7H | Realistic flow + docs finalization | **next** |

---

## 1. Stato di partenza (storico)

[invariata rispetto alla revisione precedente]

Al commit `7cbd45a` il repo offriva gli elementi rilevanti per la 8.7. Tutto ciò che è elencato in questa sezione è verificabile leggendo i file indicati; nessuno di questi elementi è stato modificato dalla 8.7B/C/D/E/F/G.

### 1.1 Schema DB già applicato (migrations 0001–0006)

- **Storage e documenti** (`0002_storage.sql`, `0003_documents.sql`).
- **Claim Ledger** (`0004_claim_ledger.sql`): `logical_claims`, `raw_claims`, `classified_claims`, `claim_ledger_entries` append-only, supersede via `claim_lineage.relation_kind='supersedes'`, `claim_evidence_links` (CHECK `cel_origin_xor`), `verification_records` con `check_kind ∈ {csv, cve_lite, nli, judge}`.
- **Answers / Gate / Published** (`0005_answers_gate.sql`): `agent_runs`, `draft_final_answers`, `final_answer_spans`, `final_answer_span_claim_links`, `final_gate_reports` append-only, `published_answers`, `coverage_gap_statements`.
- **Lifecycle e source loss** (`0006_lifecycle.sql`).

### 1.2 Endpoint API attivi al commit di partenza

[invariata: la lista completa attualmente attiva è in `PROJECT_STATE.md`]

### 1.3 Servizi worker attivi al commit di partenza

[invariata]

### 1.4 Chiarimento critico sullo stato originario

Tre affermazioni che hanno vincolato la progettazione della 8.7:

1. Una `evidence_span` collegata a un claim NON significa automaticamente fonte affidabile.
2. `verified_fact` (state del Claim Ledger) significa esclusivamente "supporto verificato secondo il CVE-lite mock-driven".
3. Source loss gestisce la perdita o invalidazione di una fonte, non la qualità iniziale della fonte.

La 8.7 ha introdotto questi giudizi come **dimensioni separate** in `source_quality_assessments` (8.7B–F) e la 8.7G li ha resi consumabili dal Gate **senza confonderli** con la verifica testuale o con la source loss.

---

## 2. Definizione realistica di source quality (invariata)

[Le tassonomie 2.1 e l'append-only/versionato 2.2 restano valide. Il codominio enum a livello DB è quello definito in `0007_source_quality.sql`; le tuple Python sono in `packages/shared/evidencefirst_shared/schemas.py`.]

---

## 3. Cosa la 8.7 NON è (rigorosamente)

Invarianti semantiche fondative, **ancora valide dopo 8.7G**:

1. **Source quality ≠ claim correctness.** 8.7G non ha cambiato questo. Il Gate non valuta la verità del claim.
2. **Source quality ≠ evidence support.** 8.7G non ha cambiato questo. Il Gate richiede prima la verifica 8.4 (`unverified_spans_present` ha priorità), poi consulta la source quality.
3. **Source quality ≠ verification outcome.** 8.7G non ha cambiato questo. CVE-lite e Source Quality vivono su tabelle separate, alimentano due branch decisionali separati.
4. **Source quality ≠ source loss.** 8.7G non ha cambiato questo. Le tabelle e i propagator restano separati.
5. **Source quality ≠ final publication eligibility.** 8.7G ha aggiunto il terzo asse al Gate, ma non ha collassato i quattro: correctness rimane fuori scope, evidence support resta CVE-lite, source quality è 8.7G, policy gate è la composizione.

Conseguenza diretta confermata da 8.7G:

- 8.7G **non** introduce uno stato `claim_ledger_entries.state` del tipo `source_quality_downgraded`. La policy M1 (`§5.2`, solo metadata) resta attiva.
- 8.7G **non** estende `claim_lineage.relation_kind`.
- 8.7G **non** estende `verification_records.check_kind`.
- 8.7G **non** estende `task_masters.status` (un task source-quality-bloccato segue lo stesso path `analyzed_partial + task.publication_held` dei rejected esistenti).

---

## 4. Modello dati — Opzione A IMPLEMENTATA (invariata)

[Sezione invariata. L'Opzione A è stata implementata in `migrations/0007_source_quality.sql`. La discussione storica delle alternative B/C resta come memoria progettuale.]

---

## 5. Interazione con Claim Ledger (invariata, comportamento M1 attivo)

[Sezione invariata. M1 (solo metadata) confermato anche post-8.7G: il Gate consulta `source_quality_assessments` in lettura, non modifica `claim_ledger_entries`.]

---

## 6. Interazione con Final Answer Gate — IMPLEMENTATA in 8.7G

**Stato corrente: il Gate è esteso. La policy è P1+P3+P4 secondo la decisione di `PHASE_8_7G_PRE.md §12`.**

### 6.1 Regola 8.4 di verifica (invariata)

Uno span è verified-backed se e solo se:
```
link.claim_ledger_entry_id == latest_entry_id_for(claim_logical_id)
AND latest_entry_state_for(claim_logical_id) == 'verified_fact'
```

### 6.2 Policy implementata in 8.7G: P1 + P3 + P4

Dopo che tutti gli span sono verified-backed (passa Branch A e Branch C invariati 8.4), il Gate consulta la **latest assoluta** di `source_quality_assessments` per ciascun `evidence_span_id` che supporta uno span verified-backed (risolto via `claim_evidence_links` filtrato sulla latest entry verified_fact).

**Block conditions** (la decision diventa `rejected`, `reason_code='source_quality_block'`):

- `overall_quality = 'unsuitable'` → reason `source_quality_unsuitable` (P1);
- `contradiction_status = 'contradicted_by_stronger_source'` → reason `source_quality_contradicted_by_stronger_source` (P3);
- `contradiction_status = 'conflicting_sources'` → reason `source_quality_conflicting_sources` (P3).

**Warning conditions** (la decision non cambia; il task può raggiungere `published`):

- `overall_quality = 'weak'` → reason `source_quality_weak` (P4);
- `overall_quality = 'unknown'` → reason `source_quality_unknown` (P4);
- `contradiction_status = 'unchecked'` → reason `source_quality_contradiction_unchecked` (P4);
- latest assessment mancante → reason `source_quality_missing_assessment` (P4).

**Clean conditions**: `overall_quality ∈ {strong, adequate}` AND `contradiction_status = 'no_known_contradiction'`. Nessun gap.

**Aggregazione tra più evidence_span dello stesso span**: worst-on-block, any-on-warn. Una sola evidenza `unsuitable` basta per bloccare; una sola evidenza `unknown` (senza block) basta per emettere warning.

**Priorità invariante**: CVE-lite > Source Quality. Uno span non verified-backed produce `unverified_spans_present` (Branch C 8.4), e il Gate NON consulta la source quality in quel caso.

### 6.3 Policy scartate per MVP-0

- **P0** (observation-only): scartata, avrebbe lasciato la promessa "evidence-gated sull'asse qualità" non onorata.
- **P2** (block su `weak`): scartata in MVP-0 senza un evaluator reale; potrebbe diventare difendibile con un evaluator reale + P5.
- **P5** (disclosure esplicita): non implementata come meccanismo separato; il warning branch (P4 implementato) svolge la stessa funzione informativa via `coverage_gap_statements`.

### 6.4 Nuovi reason_code e nuovi kind

`final_gate_reports.reason_code` ora ammette:

- (preesistenti 8.4): `no_verified_claims`, `unverified_spans_present`, `all_spans_verified`;
- (aggiunti in 8.7G): `all_spans_verified_with_warnings`, `source_quality_block`.

`coverage_gap_statements.kind` ora ammette (via migration `0008_coverage_gap_source_quality.sql`):

- (preesistenti da 0005): `unverified_claim`, `missing_evidence`, `out_of_scope`, `source_loss`;
- (aggiunti in 0008): **`source_quality_block`**, **`source_quality_warning`**.

I `gap_key` dei nuovi kind sono deterministici per span e idempotenti su redelivery:

- `f'span:{final_answer_span_id}:source_quality_block'`
- `f'span:{final_answer_span_id}:source_quality_warning'`

L'UNIQUE composito `(draft_final_answer_id, kind, gap_key)` garantisce idempotenza.

### 6.5 Migration 0008 (formalizzata)

`migrations/0008_coverage_gap_source_quality.sql` (applicata):

- DROP del CHECK preesistente su `coverage_gap_statements.kind` (identificato in modo robusto via `pg_constraint.conkey` JOIN `pg_attribute`, indipendentemente dalla rappresentazione testuale interna);
- ADD del nuovo CHECK con codominio `{unverified_claim, missing_evidence, out_of_scope, source_loss, source_quality_block, source_quality_warning}`;
- Nessun trigger nuovo, nessuna modifica all'UNIQUE composito, nessuna modifica alla `severity`.

**Numero migration**: 0008. Conseguenza: la retention futura distruttiva slitta a `0009_*` o successivo.

---

## 7. Interazione con Source Loss (invariata)

I due concetti restano distinti. 8.7G non altera la propagazione source-loss né le tabelle 0006.

---

## 8. API read-only — invariata in 8.7G

Le read API 8.7F NON sono cambiate in 8.7G. Il consumatore può tuttavia osservare gli effetti della policy 8.7G via l'endpoint preesistente `GET /api/v1/tasks/{task_id}/final-gate-report`, che ora può restituire `coverage_gap_statements` con `kind ∈ {source_quality_block, source_quality_warning}`.

Endpoint futuri 8.2/8.3/8.5 (document-level, claim-level, post-published) restano opzionali; nessuno è stato aggiunto in 8.7G.

---

## 9. Worker / pipeline — invariata in 8.7G

L'integrazione W-A (8.7E) resta in piedi. Lo step Source Quality continua a essere eseguito in `task_created` tra `task.analyzed_partial` e `task.compiling`, SAVEPOINT-protected, con audit aggregato `task.source_quality_assessed`.

La 8.7G interviene esclusivamente nella fase Final Answer Gate (`apps/worker/app/services/final_answer_gate.py`), che ora consulta `source_quality_assessments` come read-only. Nessun nuovo stream Redis, nessun nuovo consumer, nessuna modifica al dispatcher.

---

## 10. Test plan — STATO

Test implementati nei blocchi 8.7D/E/F/G:

- `apps/worker/tests/test_source_quality_evaluator_service.py` — 14 scenari (8.7D).
- `apps/worker/tests/test_source_quality_orchestrator.py` — 7 scenari (8.7E).
- `apps/worker/tests/test_task_created_source_quality_step.py` — 4 scenari (8.7E, incluso savepoint rollback + audit `failed`).
- `apps/worker/tests/test_consumer_with_documents.py` — 14 eventi nella sequenza approved post-8.7E (incluso `task.source_quality_assessed`).
- `apps/api/tests/test_source_quality_read_endpoint.py` — read API 8.7F.
- **`apps/worker/tests/test_final_answer_gate_source_quality.py` — 13 scenari (8.7G)**:
  1. `unknown` → warning, approved;
  2. missing assessment → warning, approved;
  3. `weak` → warning, approved;
  4. `unchecked` contradiction → warning, approved;
  5. `unsuitable` → block, rejected, no published;
  6. `contradicted_by_stronger_source` → block;
  7. `conflicting_sources` → block;
  8. latest version wins (v1 weak, v2 unsuitable) → block;
  9. latest version wins (v1 unsuitable, v2 strong) → clean approved;
  10. latest version wins (v1 unknown, v2 unsuitable) → block;
  11. multi-evidence worst-on-block (strong + unsuitable) → block;
  12. **priorità CVE-lite > Source Quality** (unverified span + unsuitable evidence) → `unverified_spans_present`;
  13. idempotenza su redelivery (no duplicate gap/report/published).

I test 8.4 esistenti sono stati aggiornati per riflettere il nuovo reason_code di default approved con mock attuale (`all_spans_verified_with_warnings`):

- `apps/worker/tests/test_compiler_and_gate.py`;
- `apps/worker/tests/test_extractor_and_cve_lite.py`.

**Risultati**: worker suite 105 passed; root suite 109 passed.

Test plan ancora da implementare in 8.7H:

- Realistic flow test `tests/test_phase_8_7_source_quality_flow.py` che attivi sia il Branch C' (`source_quality_block`) sia il Branch B' (`all_spans_verified_with_warnings`) con seed di assessment non-mock.

---

## 11. Non-obiettivi (esplicitamente fuori scope per 8.7)

Restano fuori scope per tutta la 8.7 (compreso 8.7H):

- Web search reale, provider AI reali, crawling/scraping.
- UI dedicata per source quality (anche per la visualizzazione dei nuovi gap `source_quality_*`).
- RBAC reale o redaction di JSONB.
- Retention policy distruttiva.
- Scoring "perfetto", algoritmi di reputazione cross-tenant.
- Verità assoluta sulla fonte.
- Ranking commerciale o monetario.
- Withdrawal automatico da source quality block.
- Modifica della propagazione source loss.
- Estensione di `claim_lineage.relation_kind`.
- Estensione di `verification_records.check_kind`.
- Modifica del CVE-lite o dell'extractor.
- Estensione di `task_masters.status`.
- Recompile/draft v2 dopo source_quality_block.

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
| 8.7G-PRE | Policy analysis (`PHASE_8_7G_PRE.md`) | done |
| 8.7G-CODE | Migration 0008 + Gate integration | done |
| 8.7G-DOC | Documentazione post-8.7G | done |
| 8.7H | Realistic flow tests + docs finalization | next |

---

## 13. Anti-Hallucination roadmap (aggiornata)

> **Il progetto non promette di impedire a un LLM di generare internamente output errati. Promette di impedire che claim fattuali non supportati, contraddetti o basati su fonti inadeguate vengano pubblicati come affidabili.**

Roadmap successiva alla 8.7G, da affrontare in blocchi separati:

- **8.7H — Realistic flow + docs finalization.** Test end-to-end realistico con scenari source_quality_block e source_quality_warning attivati da seed non-mock, chiusura formale della fase 8.7.
- **8.8A — Claim Entailment Checker.** Verifica che la quote effettivamente implichi (o sia compatibile con) il claim, non solo che sia testualmente presente.
- **8.8B — Citation-to-Claim Validator.** Verifica che il claim citi le evidenze corrette.
- **8.8C — Contradiction Detector.** Detector reale di contraddizioni tra claim o tra fonti. Quando attivato, sostituirà le `contradiction_status='unchecked'` del mock con valori reali e attiverà naturalmente il Branch C' del Gate sui `contradicted_by_stronger_source` / `conflicting_sources`.
- **8.8D — Final Answer Sentence Gate.** Gate a livello frase del published_answer.
- **8.8E — Anti-Hallucination Report API.** Endpoint aggregato che espone, per un published_answer, lo stato di tutti gli assi (entailment, citation, contradiction, source quality, source loss).
- **8.9 — External Verification / Web-RAG controllato.**
- **9.0 — Multi-agent consensus + adversarial review reale.**

---

## 14. Rischi residui (aggiornati post-8.7G)

Rischi specifici allo stato post-8.7G:

- **Source Quality mock deterministic.** L'evaluator scrive sempre `overall_quality='unknown'` e `contradiction_status='unchecked'`. Con 8.7G in atto, ogni task verified-backed produrrà oggi un `source_quality_warning` per span. Rumore strutturale fino all'arrivo di un evaluator reale.
- **`Branch C' (source_quality_block) non si attiva mai oggi.** Il mock non emette `unsuitable`, né `contradicted_by_stronger_source`, né `conflicting_sources`. La policy è implementata e testata (via seed manuale in `test_final_answer_gate_source_quality.py`), ma di fatto non blocca nulla in produzione finché un evaluator reale non emetterà quei codomini.
- **Reason code default cambiato.** Il reason_code di default per task approved con mock è ora `all_spans_verified_with_warnings`, non `all_spans_verified`. Consumatori esterni (UI, eval, report) che testavano su stringa esatta `all_spans_verified` potrebbero rompersi. I test interni sono allineati.
- **Falso senso di sicurezza dei warning.** Un task `published` con `source_quality_warning` può essere percepito come "approvato senza riserve". La read API `/final-gate-report` espone i gap; la UI futura deve renderli visibili.
- **Coerenza tra "latest" del Gate e "latest" della read API 8.7F.** Il Gate usa la latest assoluta DB-level (`ORDER BY version_no DESC, created_at DESC, id DESC LIMIT 1`); l'API 8.7F restituisce la "latest nello slice" dato un `limit`. Le due possono coincidere o no. Documentato come avvertenza per consumatori che confrontano i due output.
- **`conflicting_sources` come block è opinabile.** Sarebbe semanticamente più corretto un "hold for human review", ma `task_masters.status` non si estende.
- **`unsuitable` block è permanente per quel draft.** Un draft rejected per source_quality_block resta rejected (compiler v1-only, nessun path per draft v2 in MVP-0). Coerente con il design 8.4.
- **Payload JSONB esposto senza RBAC.** Gli endpoint 8.7F e `/final-gate-report` espongono `payload` e `details` verbatim. Debito noto.
- **Task pre-8.7E senza assessment.** Comportamento 8.7G: warning `source_quality_missing_assessment` per span. Non blocca, ma rumoroso. Nessun backfill.
- **No Claim Entailment Checker, no Citation-to-Claim Validator, no Contradiction Detector reale, no Final Answer Sentence Gate, no External Verification.** Tutti rinviati a 8.8x e 8.9.
- **"unknown" continua a non significare "approvato".** Il Gate oggi lo tratta correttamente come warning. Va martellato nei docs e nella futura UI.
- **Costo computazionale futuro** quando si introdurrà un evaluator reale.
- **N+1 query** nel task endpoint 8.7F: invariato.
- **Coesistenza con retention.** Il numero `0008` è ora occupato da `coverage_gap_source_quality`; la retention futura distruttiva slitta a `0009_*` o successivo.
- **`coverage_gap_statements` senza trigger append-only.** 8.7G rispetta operativamente l'insert-only ma non c'è enforcement a DB. Future fasi potranno introdurre il trigger.
- **Calibrazione futura della policy.** P2 (block su `weak`) potrebbe diventare ragionevole con evaluator reale + P5. La policy è versionata (`mvp0_source_quality_gate_policy` v0.1.0) per consentire un bump tracciabile.
- **Priorità CVE-lite > Source Quality come invariante "fragile".** Un refactor incauto del Gate potrebbe invertirla. Mitigazione: testato esplicitamente in scenario 12 di `test_final_answer_gate_source_quality.py`.

---

## 15. Decisione documentale

- **8.7A**: `PHASE_8_7_PLAN.md` creato.
- **8.7B**: `migrations/0007_source_quality.sql` scritta e applicata.
- **8.7C**: `packages/shared/evidencefirst_shared/schemas.py` esteso.
- **8.7D**: `apps/worker/app/services/source_quality_evaluator.py` scritto.
- **8.7E**: `apps/worker/app/services/source_quality_orchestrator.py` scritto; `apps/worker/app/consumers/task_created.py` integrato.
- **8.7F**: `apps/api/app/routes/source_quality.py` scritto; registrato in `apps/api/app/main.py`.
- **8.7G-PRE**: `PHASE_8_7G_PRE.md` scritto, policy P1+P3+P4 + migration 0008 raccomandate.
- **8.7G-CODE**: `migrations/0008_coverage_gap_source_quality.sql` scritta e applicata; `apps/worker/app/services/final_answer_gate.py` esteso; test 8.4 esistenti allineati; nuovo file test `apps/worker/tests/test_final_answer_gate_source_quality.py` (13 scenari).
- **8.7G-DOC**: questo aggiornamento + `PROJECT_STATE.md` + `README.md`.
- **8.7H**: pending.
- **`PHASE_8_6_PLAN.md`** non modificato.

---

FILE_COMPLETATI (8.7A–G, cumulativo)
- `PHASE_8_7_PLAN.md` (8.7A; aggiornato post-8.7F; aggiornato post-8.7G)
- `migrations/0007_source_quality.sql` (8.7B)
- `packages/shared/evidencefirst_shared/schemas.py` (8.7C)
- `apps/worker/app/services/source_quality_evaluator.py` (8.7D)
- `apps/worker/app/services/source_quality_orchestrator.py` (8.7E)
- `apps/worker/app/consumers/task_created.py` (integrazione 8.7E)
- `apps/api/app/routes/source_quality.py` (8.7F)
- `apps/api/app/main.py` (registrazione router 8.7F)
- Test 8.7D/E/F worker + API
- `PHASE_8_7G_PRE.md` (8.7G-PRE)
- `migrations/0008_coverage_gap_source_quality.sql` (8.7G-CODE)
- `apps/worker/app/services/final_answer_gate.py` esteso (8.7G-CODE)
- `apps/worker/tests/test_final_answer_gate_source_quality.py` (8.7G-CODE)
- `apps/worker/tests/test_compiler_and_gate.py` allineato a `all_spans_verified_with_warnings` (8.7G-CODE)
- `apps/worker/tests/test_extractor_and_cve_lite.py` allineato a `all_spans_verified_with_warnings` (8.7G-CODE)
- `PROJECT_STATE.md` aggiornato (8.7G-DOC)
- `README.md` aggiornato (8.7G-DOC)

FILE_DA_FARE_PROSSIMO_BLOCCO
- Decisione tra:
  - **8.7H** — Realistic flow test `tests/test_phase_8_7_source_quality_flow.py` (Branch C' + Branch B' end-to-end) + chiusura formale della fase 8.7.
  - **8.8A** — Claim Entailment Checker (apre la fase 8.8 anti-hallucination).

RISCHI_RESIDUI (sintesi, vedi §14 per il dettaglio)
- Source Quality mock deterministic (`overall_quality='unknown'`, `contradiction_status='unchecked'`): ogni task approved emette warning oggi.
- Branch C' (`source_quality_block`) non si attiva mai con il mock attuale; coperto da test via seed manuale.
- Reason code default approved cambiato in `all_spans_verified_with_warnings`.
- `unknown` ≠ approvato: martellare nei docs e nella futura UI.
- Payload/details JSONB esposti senza RBAC.
- Task pre-8.7E senza assessment, no backfill.
- No Claim Entailment Checker, no Citation-to-Claim Validator, no Contradiction Detector reale, no Final Answer Sentence Gate, no External Verification (rinviati a 8.8x/8.9).
- `coverage_gap_statements` senza trigger append-only.
- Priorità CVE-lite > Source Quality: invariante critica, testata, ma fragile a refactor.
- Coesistenza retention: `0009_*` da assegnare quando si scriverà.
