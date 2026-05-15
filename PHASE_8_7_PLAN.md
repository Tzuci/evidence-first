# PHASE_8_7_PLAN — Source Quality Evaluator / Evidence Quality Layer

Documento di **piano architetturale** per la Fase 8.7 dell'Evidence-First MVP-0. La fase introduce il primo modulo dedicato alla valutazione della **qualità delle fonti** che supportano i claim del Claim Ledger, **e l'integrazione decisionale di quella valutazione nel Final Answer Gate**, **validata end-to-end da un realistic flow test**.

> **Stato di questo documento.**
>
> Documento nato come piano 8.7A; aggiornato dopo 8.7F al commit `91397ae6f02abd429cff29b6e0248cf9a7c16317`; aggiornato dopo 8.7G al commit `79815764cd8c588556b81c5914b61deb16eb7370`; aggiornato dopo 8.7H al commit `b70ef8fb394e0f28befdfd2b3a699c32a88e9914` ("Add phase 8.7 source quality realistic flow").
>
> **Phase 8.7 complete.** I blocchi **8.7A–8.7H sono tutti implementati**. Le sezioni di questo documento sono allineate dove rilevante. Il prossimo blocco operativo consigliato è 8.8A (Claim Entailment Checker).

**Commit di partenza del piano**: `7cbd45ae416ead0b2f5221ace4925dee374fa0c9`.
**Commit di allineamento documentale post-8.7F**: `91397ae6f02abd429cff29b6e0248cf9a7c16317`.
**Commit di allineamento documentale post-8.7G**: `79815764cd8c588556b81c5914b61deb16eb7370`.
**Commit di chiusura della fase 8.7 (post-8.7H)**: `b70ef8fb394e0f28befdfd2b3a699c32a88e9914`.

**Collegamento logico**: la Fase 8.6 ha reso **osservabili** via HTTP read-only gli eventi lifecycle e la propagazione della source loss. La Fase 8.7 ha cominciato a **valutare la qualità** delle fonti che il sistema usa per supportare claim (8.7B–F), la 8.7G ha reso quella valutazione **consumata** dal Final Answer Gate, e la 8.7H ha **validato end-to-end** l'intera catena (warning + block) con un realistic flow test che attraversa API HTTP → FakeRedis → dispatcher → consumer → servizi worker → DB → read API.

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
| 8.7G-DOC | Documentazione post-8.7G | **done** |
| 8.7H-PRE | Realistic flow analysis (`PHASE_8_7H_PRE.md`) | **done** |
| 8.7H-CODE | Realistic flow test (`tests/test_phase_8_7_source_quality_flow.py`) | **done** |
| 8.7H-DOC | Documentazione di chiusura (`PROJECT_STATE.md`, `PHASE_8_7_PLAN.md`, `README.md`) | **done** |

**Phase 8.7 complete.**

---

## 1. Stato di partenza (storico)

[invariata rispetto alla revisione precedente]

Al commit `7cbd45a` il repo offriva gli elementi rilevanti per la 8.7. Tutto ciò che è elencato in questa sezione è verificabile leggendo i file indicati; nessuno di questi elementi è stato modificato dalla 8.7B/C/D/E/F/G/H.

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

La 8.7 ha introdotto questi giudizi come **dimensioni separate** in `source_quality_assessments` (8.7B–F), la 8.7G li ha resi consumabili dal Gate **senza confonderli** con la verifica testuale o con la source loss, e la 8.7H ha **validato end-to-end** che il consumo dal Gate produce realmente gli outcome attesi nei due path attivabili (warning con mock, block con stub dell'orchestrator).

---

## 2. Definizione realistica di source quality (invariata)

[Le tassonomie 2.1 e l'append-only/versionato 2.2 restano valide. Il codominio enum a livello DB è quello definito in `0007_source_quality.sql`; le tuple Python sono in `packages/shared/evidencefirst_shared/schemas.py`.]

---

## 3. Cosa la 8.7 NON è (rigorosamente)

Invarianti semantiche fondative, **ancora valide dopo 8.7H**:

1. **Source quality ≠ claim correctness.** 8.7G/H non hanno cambiato questo. Il Gate non valuta la verità del claim. **Una fonte citata non implica un claim vero.**
2. **Source quality ≠ evidence support.** 8.7G/H non hanno cambiato questo. Il Gate richiede prima la verifica 8.4 (`unverified_spans_present` ha priorità), poi consulta la source quality. Il realistic flow 8.7H non rilassa questo ordine.
3. **Source quality ≠ verification outcome.** 8.7G/H non hanno cambiato questo. CVE-lite e Source Quality vivono su tabelle separate, alimentano due branch decisionali separati.
4. **Source quality ≠ source loss.** 8.7G/H non hanno cambiato questo. Le tabelle e i propagator restano separati.
5. **Source quality ≠ final publication eligibility.** 8.7G ha aggiunto il terzo asse al Gate, ma non ha collassato i quattro: correctness rimane fuori scope, evidence support resta CVE-lite, source quality è 8.7G, policy gate è la composizione.

Conseguenza diretta confermata da 8.7G/H:

- 8.7G/H **non** introducono uno stato `claim_ledger_entries.state` del tipo `source_quality_downgraded`. La policy M1 (`§5.2`, solo metadata) resta attiva.
- 8.7G/H **non** estendono `claim_lineage.relation_kind`.
- 8.7G/H **non** estendono `verification_records.check_kind`.
- 8.7G/H **non** estendono `task_masters.status` (un task source-quality-bloccato segue lo stesso path `analyzed_partial + task.publication_held` dei rejected esistenti).

---

## 4. Modello dati — Opzione A IMPLEMENTATA (invariata)

[Sezione invariata. L'Opzione A è stata implementata in `migrations/0007_source_quality.sql`. La discussione storica delle alternative B/C resta come memoria progettuale.]

---

## 5. Interazione con Claim Ledger (invariata, comportamento M1 attivo)

[Sezione invariata. M1 (solo metadata) confermato anche post-8.7G/H: il Gate consulta `source_quality_assessments` in lettura, non modifica `claim_ledger_entries`. Il realistic flow 8.7H verifica esplicitamente che `claim_ledger_entries` non venga mutato dal Gate (asserzione invariant nel test).]

---

## 6. Interazione con Final Answer Gate — IMPLEMENTATA in 8.7G, VALIDATA in 8.7H

**Stato corrente: il Gate è esteso. La policy è P1+P3+P4 secondo la decisione di `PHASE_8_7G_PRE.md §12`. Il realistic flow 8.7H ha confermato il comportamento end-to-end per i due path attivabili.**

### 6.1 Regola 8.4 di verifica (invariata)

Uno span è verified-backed se e solo se:
```
link.claim_ledger_entry_id == latest_entry_id_for(claim_logical_id)
AND latest_entry_state_for(claim_logical_id) == 'verified_fact'
```

### 6.2 Policy implementata in 8.7G: P1 + P3 + P4

Dopo che tutti gli span sono verified-backed (passa Branch A e Branch C invariati 8.4), il Gate consulta la **latest assoluta** di `source_quality_assessments` per ciascun `evidence_span_id` che supporta uno span verified-backed.

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

**Aggregazione tra più evidence_span dello stesso span**: worst-on-block, any-on-warn.

**Priorità invariante**: CVE-lite > Source Quality. Uno span non verified-backed produce `unverified_spans_present` (Branch C 8.4), e il Gate NON consulta la source quality in quel caso.

### 6.3 Policy scartate per MVP-0

- **P0** (observation-only): scartata, avrebbe lasciato la promessa "evidence-gated sull'asse qualità" non onorata.
- **P2** (block su `weak`): scartata in MVP-0 senza un evaluator reale; potrebbe diventare difendibile con un evaluator reale + P5.
- **P5** (disclosure esplicita): non implementata come meccanismo separato; il warning branch (P4 implementato) svolge la stessa funzione informativa via `coverage_gap_statements`.

### 6.4 Nuovi reason_code e nuovi kind

`final_gate_reports.reason_code` ammette:

- (preesistenti 8.4): `no_verified_claims`, `unverified_spans_present`, `all_spans_verified`;
- (aggiunti in 8.7G): `all_spans_verified_with_warnings`, `source_quality_block`.

`coverage_gap_statements.kind` ammette (via migration `0008_coverage_gap_source_quality.sql`):

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

### 6.6 Validazione end-to-end (8.7H)

**Il realistic flow test `tests/test_phase_8_7_source_quality_flow.py` valida l'intera catena**:

```
API HTTP (POST /projects, /documents, /tasks)
  → FakeRedis (cattura xadd su app.events.task_created)
  → dispatcher (_dispatch.handle_event)
  → task.created consumer
  → extractor + CVE-lite + 8.7E source quality + compiler + Final Answer Gate
  → DB (audit_records, source_quality_assessments, final_gate_reports,
        coverage_gap_statements, published_answers)
  → read API (GET /final-gate-report, /published-answer, /source-quality,
              /evidence-spans/{es}/source-quality)
```

Due path coperti:

- **Warning flow** (`test_phase_8_7_source_quality_warning_flow_end_to_end`): mock source quality evaluator produce `overall_quality='unknown'` + `contradiction_status='unchecked'` per ogni evidence_span linkato → Final Answer Gate approved con `reason_code='all_spans_verified_with_warnings'` → emissione `coverage_gap_statements` di kind `source_quality_warning` (severity='warn') con `details.reasons` includente `source_quality_unknown` e/o `source_quality_contradiction_unchecked` → `published_answers` v1 inserito con `status='published'`. La pipeline esegue il mock evaluator REALE, senza patch.

- **Block flow** (`test_phase_8_7_source_quality_block_flow_end_to_end`): monkeypatch del simbolo `_wapp.consumers.task_created.run_source_quality_assessment` con uno stub orchestrator che inserisce v1 di `source_quality_assessments` con `overall_quality='unsuitable'` per ogni evidence_span e ritorna il dict counts canonico atteso dal consumer → Final Answer Gate rejected con `reason_code='source_quality_block'` → emissione `coverage_gap_statements` di kind `source_quality_block` (severity='block') con `details.reasons` includente `source_quality_unsuitable` → `task.publication_held` come evento terminale, task in `analyzed_partial`, nessun `published_answer`.

**Chiarimento Branch C' (8.7H).** Il Branch C' è implementato in `final_answer_gate.py` e testato a livello unit (13 scenari di `apps/worker/tests/test_final_answer_gate_source_quality.py`) con seed diretto. Nel realistic test 8.7H, **Branch C' viene attivato esclusivamente tramite stub dell'orchestrator** perché **il mock evaluator reale non produce `unsuitable`** spontaneamente — produce sempre e solo `unknown` + `unchecked`. Lo stub non altera il Gate né il Compiler né l'extractor né il CVE-lite né la migration: sostituisce esclusivamente la funzione `run_source_quality_assessment` come bindata sul consumer, mantenendo la sua signature e la sua semantica di counts. In produzione, con il mock attuale, **Branch C' resta dormiente**: si attiverà naturalmente quando l'8.8C (Contradiction Detector reale) o un evaluator reale produrranno `unsuitable` / `contradicted_by_stronger_source` / `conflicting_sources`.

---

## 7. Interazione con Source Loss (invariata)

I due concetti restano distinti. 8.7G/H non alterano la propagazione source-loss né le tabelle 0006.

---

## 8. API read-only — invariata in 8.7G/H

Le read API 8.7F NON sono cambiate in 8.7G/H. Il consumatore può tuttavia osservare gli effetti della policy 8.7G via l'endpoint preesistente `GET /api/v1/tasks/{task_id}/final-gate-report`, che ora restituisce `coverage_gap_statements` con `kind ∈ {source_quality_block, source_quality_warning}`. Il realistic flow 8.7H interroga questo endpoint e i tre endpoint dedicati (8.7F + `/published-answer`) per validare la coerenza tra stato DB e response HTTP.

---

## 9. Worker / pipeline — invariata in 8.7G/H

L'integrazione W-A (8.7E) resta in piedi. Lo step Source Quality continua a essere eseguito in `task_created` tra `task.analyzed_partial` e `task.compiling`, SAVEPOINT-protected, con audit aggregato `task.source_quality_assessed`.

La 8.7G interviene esclusivamente nella fase Final Answer Gate (`apps/worker/app/services/final_answer_gate.py`). La 8.7H **non modifica** né il consumer né il Gate né i servizi né le migration: aggiunge solo il file di test `tests/test_phase_8_7_source_quality_flow.py`.

---

## 10. Test plan — COMPLETO

Test implementati nei blocchi 8.7D/E/F/G/H:

- `apps/worker/tests/test_source_quality_evaluator_service.py` — 14 scenari (8.7D).
- `apps/worker/tests/test_source_quality_orchestrator.py` — 7 scenari (8.7E).
- `apps/worker/tests/test_task_created_source_quality_step.py` — 4 scenari (8.7E, incluso savepoint rollback + audit `failed`).
- `apps/worker/tests/test_consumer_with_documents.py` — 14 eventi nella sequenza approved post-8.7E (incluso `task.source_quality_assessed`).
- `apps/api/tests/test_source_quality_read_endpoint.py` — read API 8.7F.
- `apps/worker/tests/test_final_answer_gate_source_quality.py` — 13 scenari (8.7G): warning path (`unknown`, `weak`, `unchecked`, missing), block path (`unsuitable`, `contradicted_by_stronger_source`, `conflicting_sources`), latest-wins versioning, multi-evidence worst-on-block, **priorità CVE-lite > Source Quality**, idempotenza redelivery.
- Test 8.4 esistenti allineati al nuovo reason_code di default approved (`all_spans_verified_with_warnings`):
  - `apps/worker/tests/test_compiler_and_gate.py`;
  - `apps/worker/tests/test_extractor_and_cve_lite.py`.
- **`tests/test_phase_8_7_source_quality_flow.py` (8.7H, root-level)** — due test realistic flow end-to-end:
  - `test_phase_8_7_source_quality_warning_flow_end_to_end`: warning path attivato dal mock evaluator reale.
  - `test_phase_8_7_source_quality_block_flow_end_to_end`: block path attivato via stub dell'orchestrator (`monkeypatch.setattr(_wapp.consumers.task_created, "run_source_quality_assessment", _stub)`).

**Risultati**: tutti i test passano al commit `b70ef8f`. La fase 8.7 è formalmente completa.

---

## 11. Non-obiettivi (esplicitamente fuori scope per 8.7)

Restano fuori scope per tutta la 8.7 (inclusa 8.7H):

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
- Worker main loop reale negli end-to-end test (8.7H usa FakeRedis + dispatch.handle_event diretta).

---

## 12. Roadmap a blocchi — stato finale

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
| 8.7H-PRE | Realistic flow analysis (`PHASE_8_7H_PRE.md`) | done |
| 8.7H-CODE | `tests/test_phase_8_7_source_quality_flow.py` | done |
| 8.7H-DOC | Documentazione di chiusura | done |

**Phase 8.7 complete.** Prossimo blocco: 8.8A-PRE / Claim Entailment Checker.

---

## 13. Anti-Hallucination roadmap (aggiornata)

> **Disclaimer (invariato e ribadito post-8.7H).** Il progetto **non elimina le allucinazioni in senso assoluto**. La piattaforma impedisce o rende visibili claim non supportati, contraddetti o basati su fonti inadeguate prima della pubblicazione affidabile. **Una fonte citata non implica un claim vero**: la qualità della fonte e la correttezza del claim restano assi separati. Non promette di impedire a un LLM di generare internamente output errati; promette che claim fattuali insufficientemente supportati non vengano pubblicati come affidabili.

Componenti **ancora mancanti** dopo la chiusura della 8.7:

- **8.8A — Claim Entailment Checker.** Verifica che la quote effettivamente implichi (o sia compatibile con) il claim, non solo che sia testualmente presente. **Mancante.**
- **8.8B — Citation-to-Claim Validator.** Verifica che il claim citi le evidenze corrette. **Mancante.**
- **8.8C — Contradiction Detector reale.** Detector reale di contraddizioni tra claim o tra fonti. Quando attivato, sostituirà le `contradiction_status='unchecked'` del mock con valori reali e attiverà naturalmente il Branch C' del Gate sui `contradicted_by_stronger_source` / `conflicting_sources`. **Mancante.**
- **8.8D — Final Answer Sentence Gate.** Gate a livello frase del published_answer. **Mancante.**
- **8.8E — Anti-Hallucination Report API.** Endpoint aggregato che espone, per un published_answer, lo stato di tutti gli assi (entailment, citation, contradiction, source quality, source loss). **Mancante.**
- **8.9 — External Verification / Web-RAG controllato.** **Mancante.**
- **9.0 — Multi-agent consensus + adversarial review reale.** **Mancante.**

---

## 14. Rischi residui (aggiornati post-8.7H)

Rischi specifici allo stato post-8.7H:

- **Source Quality mock deterministic.** L'evaluator scrive sempre `overall_quality='unknown'` e `contradiction_status='unchecked'`. Con 8.7G in atto, ogni task verified-backed produce un `source_quality_warning` per span. Rumore strutturale fino all'arrivo di un evaluator reale. Il realistic flow 8.7H lo verifica esplicitamente nel warning test.
- **Branch C' (source_quality_block) non si attiva mai oggi in produzione con il mock attuale.** Il mock non emette `unsuitable`, né `contradicted_by_stronger_source`, né `conflicting_sources`. La policy è implementata e testata: a livello unit (`test_final_answer_gate_source_quality.py` con seed diretto), e a livello realistic flow end-to-end (`tests/test_phase_8_7_source_quality_flow.py` con stub dell'orchestrator). Ma di fatto non blocca nulla in produzione finché un evaluator reale non emetterà quei codomini (8.8C).
- **Reason code default cambiato.** Il reason_code di default per task approved con mock è `all_spans_verified_with_warnings`, non `all_spans_verified`. Consumatori esterni (UI, eval, report) che testavano su stringa esatta `all_spans_verified` potrebbero rompersi. I test interni sono allineati.
- **Falso senso di sicurezza dei warning.** Un task `published` con `source_quality_warning` può essere percepito come "approvato senza riserve". La read API `/final-gate-report` espone i gap; la UI futura deve renderli visibili.
- **Coerenza tra "latest" del Gate e "latest" della read API 8.7F.** Il Gate usa la latest assoluta DB-level; l'API 8.7F restituisce la "latest nello slice" dato un `limit`. Le due possono coincidere o no. Documentato come avvertenza.
- **`conflicting_sources` come block è opinabile.** Sarebbe semanticamente più corretto un "hold for human review", ma `task_masters.status` non si estende.
- **`unsuitable` block è permanente per quel draft.** Un draft rejected per source_quality_block resta rejected (compiler v1-only, nessun path per draft v2 in MVP-0).
- **Payload JSONB esposto senza RBAC.** Gli endpoint 8.7F e `/final-gate-report` espongono `payload` e `details` verbatim. Debito noto.
- **Task pre-8.7E senza assessment.** Comportamento 8.7G: warning `source_quality_missing_assessment` per span. Non blocca, ma rumoroso. Nessun backfill.
- **No Claim Entailment Checker, no Citation-to-Claim Validator, no Contradiction Detector reale, no Final Answer Sentence Gate, no Anti-Hallucination Report API, no External Verification/Web-RAG controllato.** Tutti rinviati a 8.8x e 8.9.
- **"unknown" continua a non significare "approvato".** Il Gate oggi lo tratta correttamente come warning. Va martellato nei docs e nella futura UI. **Una fonte citata non implica un claim vero.**
- **Costo computazionale futuro** quando si introdurrà un evaluator reale.
- **N+1 query** nel task endpoint 8.7F: invariato.
- **Coesistenza con retention.** Il numero `0008` è occupato da `coverage_gap_source_quality`; la retention futura distruttiva slitta a `0009_*` o successivo.
- **`coverage_gap_statements` senza trigger append-only.** 8.7G rispetta operativamente l'insert-only ma non c'è enforcement a DB. Future fasi potranno introdurre il trigger.
- **Calibrazione futura della policy.** P2 (block su `weak`) potrebbe diventare ragionevole con evaluator reale + P5. La policy è versionata (`mvp0_source_quality_gate_policy` v0.1.0) per consentire un bump tracciabile.
- **Priorità CVE-lite > Source Quality come invariante "fragile".** Un refactor incauto del Gate potrebbe invertirla. Mitigazione: testato esplicitamente in scenario 12 di `test_final_answer_gate_source_quality.py`.
- **Worker main loop reale negli end-to-end test (incluso 8.7H).** Il realistic flow 8.7H usa FakeRedis e invoca `dispatch.handle_event` direttamente; non testa il loop XREADGROUP del worker.

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
- **8.7G-DOC**: aggiornamento `PROJECT_STATE.md` + `PHASE_8_7_PLAN.md` + `README.md`.
- **8.7H-PRE**: `PHASE_8_7H_PRE.md` scritto (analisi del realistic flow e scelte di attivazione del Branch C').
- **8.7H-CODE**: `tests/test_phase_8_7_source_quality_flow.py` scritto (due test end-to-end: warning + block).
- **8.7H-DOC**: questo aggiornamento + `PROJECT_STATE.md` + `README.md`.
- **`PHASE_8_6_PLAN.md`** non modificato.

---

FILE_COMPLETATI (8.7A–H, cumulativo)
- `PHASE_8_7_PLAN.md` (8.7A; aggiornato post-8.7F, post-8.7G, post-8.7H)
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
- `apps/worker/tests/test_compiler_and_gate.py` allineato (8.7G-CODE)
- `apps/worker/tests/test_extractor_and_cve_lite.py` allineato (8.7G-CODE)
- `PROJECT_STATE.md` aggiornato (8.7G-DOC, poi 8.7H-DOC)
- `README.md` aggiornato (8.7G-DOC, poi 8.7H-DOC)
- `PHASE_8_7H_PRE.md` (8.7H-PRE)
- **`tests/test_phase_8_7_source_quality_flow.py` (8.7H-CODE)**

FILE_DA_FARE_PROSSIMO_BLOCCO
- **8.8A-PRE** — apertura della fase 8.8 con il Claim Entailment Checker (vedi §13).
- Direzioni complementari da decidere con prompt operativo dedicato: `0009_*` retention; RBAC/redaction; cursor pagination; trigger append-only su `coverage_gap_statements`; smoke test end-to-end con Redis reale.

RISCHI_RESIDUI (sintesi, vedi §14 per il dettaglio)
- Source Quality mock deterministic (`overall_quality='unknown'`, `contradiction_status='unchecked'`): ogni task approved emette warning oggi.
- Branch C' (`source_quality_block`) **non si attiva spontaneamente con il mock attuale**; implementato e testato unit + realistic flow (via stub dell'orchestrator).
- Reason code default approved cambiato in `all_spans_verified_with_warnings`.
- `unknown` ≠ approvato: martellare nei docs e nella futura UI.
- **Una fonte citata non implica un claim vero**: source quality ≠ claim correctness.
- Payload/details JSONB esposti senza RBAC.
- Task pre-8.7E senza assessment, no backfill.
- No Claim Entailment Checker, no Citation-to-Claim Validator, no Contradiction Detector reale, no Final Answer Sentence Gate, no Anti-Hallucination Report API, no External Verification/Web-RAG controllato (rinviati a 8.8x/8.9).
- `coverage_gap_statements` senza trigger append-only.
- Priorità CVE-lite > Source Quality: invariante critica, testata, ma fragile a refactor.
- Coesistenza retention: `0009_*` da assegnare.
- Worker main loop reale non testato (8.7H usa FakeRedis + dispatch.handle_event diretta).
