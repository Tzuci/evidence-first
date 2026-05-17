# PHASE_8_8B_REPORT_PRE — Anti-Hallucination Report API aggregata

Documento **decisionale e di piano** per la sotto-fase **8.8B-REPORT**. Questo blocco è **solo analisi e progettazione**: non scrive codice applicativo, non scrive migration, non scrive test implementativi, non modifica API, non modifica worker, non modifica shared schemas, non modifica `README.md` o `PROJECT_STATE.md`. Il solo deliverable è questo documento.

Stile: italiano tecnico da System Architect; non enciclopedico. Per dettagli implementativi già consolidati questo documento rimanda ai file letti (`PROJECT_STATE.md`, `PHASE_8_8A_PRE.md`, `PHASE_8_8A_GATE_PRE.md`, `PHASE_8_7_PLAN.md`) anziché replicarne il contenuto.

**Promessa anti-allucinazione (ribadita).** Il sistema è progettato per impedire che claim fattuali non supportati, contraddetti o basati su fonti inadeguate vengano pubblicati come affidabili. Non promette di eliminare le allucinazioni in senso assoluto. Una fonte citata non implica un claim vero. Una quote testualmente presente non implica che la quote sostenga il claim. Un verdict `entailed` non significa che il claim sia vero nel mondo. Il futuro Anti-Hallucination Report aggregato è una **vista read-only** che rende leggibili assi separati (CVE-lite, Source Quality, Claim Entailment, Final Answer Gate, coverage gaps, publication status); non introduce nuove decisioni, non ricalcola il Gate, non muta DB, non sostituisce le fonti di verità primarie (tabelle append-only e read API specialistiche).

---

## 0. Stato corrente e commit di partenza

- **Commit di partenza:** `13533ac1f70884db52749c24457bb42153a7c4d9` ("Add claim entailment read endpoint").
- **8.8A-READ-A:** chiusa. Endpoint task-level read-only `GET /api/v1/tasks/{task_id}/claim-entailment` attivo con ordering `(created_at DESC, id DESC)`, limite, 404 normalizzato `details.resource='task_masters'` per task inesistente, `items=[]` per task esistente senza checks. Vedi `apps/api/app/routes/claim_entailment.py`.
- **8.8A core:** tecnicamente chiusa. Resta in backlog la possibilità di un endpoint **claim-level** futuro (`GET /api/v1/claims/{logical_id}/entailment-checks`), eventualmente 8.8A-READ-B; va valutato se ha senso farlo prima della UI o se la vista claim-level può essere coperta dal report aggregato in alcuni casi d'uso.
- **Nomenclatura.** In alcune roadmap pre-8.8A-READ-A il prefisso 8.8B era usato per il **Citation-to-Claim Validator**. Dopo 8.8A-READ-A questo blocco usa il prefisso `8.8B-REPORT` per progettare il **report aggregato** necessario alla futura UI. Il Citation-to-Claim Validator (8.8B "storico") **resta futuro e non viene implementato in questo blocco**, né progettato qui.
- **Nessuna migration richiesta per questo blocco PRE.** La numerazione disponibile resta da 0011 in poi (la retention distruttiva è candidata naturale a 0011 e già rinviata in 8.7G/H/8.8A).
- **Nessun codice scritto in questo blocco.** Nessuna modifica ad API, worker, packages/shared o test.

---

## 1. Problema da risolvere

Lo stato post-8.8A-READ-A offre osservabilità HTTP read-only su molti assi, ma su superfici distinte. Una UI che voglia mostrare, per un task, una sintesi anti-allucinazione completa deve oggi orchestrare letture su molte superfici e conoscerne dettagli interni:

- `task_masters` (status, objective, mode, timestamps);
- `projects`, `tenants`;
- `task_documents`, `uploaded_documents`, `document_versions`, `document_chunks`, `evidence_spans`;
- `raw_claims`, `classified_claims`, `logical_claims`, `claim_ledger_entries`, `claim_evidence_links`;
- `verification_records` con `check_kind='cve_lite'` (asse CVE-lite);
- `source_quality_assessments` (asse qualità della fonte);
- `claim_entailment_checks` (asse entailment claim ↔ quote);
- `draft_final_answers`, `final_answer_spans`, `final_answer_span_claim_links`;
- `final_gate_reports` (decisione + reason_code + payload con summary entailment 8.8A-GATE-CODE);
- `coverage_gap_statements` (gap con `kind ∈ {unverified_claim, missing_evidence, out_of_scope, source_loss, source_quality_block, source_quality_warning, entailment_block, entailment_warning}`);
- `published_answers` (publication status reale);
- `published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records` (lifecycle e source-loss).

La UI non dovrebbe ricostruire da sola questa orchestrazione, e non dovrebbe dipendere dai dettagli interni dei singoli endpoint (in particolare dalla differenza fra "latest assoluto DB-level" e "latest nello slice limit"). Serve un **contratto stabile, task-level, read-only**, progettato per il consumo da UI e da audit umano, che renda esplicito **cosa il sistema garantisce** e **cosa NON garantisce** su ciascun asse.

---

## 2. Non-obiettivi

Il futuro Anti-Hallucination Report API aggregato **non** introduce alcuno dei seguenti elementi:

- nessun provider AI reale;
- nessun NLI / LLM reale;
- nessun Source Quality evaluator reale;
- nessuna web search, nessun web-RAG, nessun crawling;
- nessun contradiction detector cross-source (rinviato a 8.8C);
- nessun Final Answer Sentence Gate (rinviato a 8.8D);
- nessun Citation-to-Claim Validator (storicamente 8.8B; non in questo blocco);
- nessuna UI in questo blocco;
- nessuna migration in questo blocco;
- nessuna mutation DB;
- nessun nuovo worker, nessun nuovo step di pipeline;
- nessun backfill per task storici;
- nessuna RBAC / redaction reale (debito noto);
- nessuna pretesa di verità assoluta;
- nessuna modifica al Final Answer Gate (la priorità `CVE-lite > Claim Entailment > Source Quality` resta invariata);
- nessuna modifica al Claim Ledger né al supersede via `claim_lineage.relation_kind='supersedes'`;
- nessuna modifica agli endpoint read specialistici esistenti (8.7F, 8.6, 8.8A-READ-A, 8.4 answers);
- nessuna rimozione né rinomina di `reason_code`, `kind`, `severity`.

---

## 3. Endpoint futuro proposto

### 3.1 Endpoint primario raccomandato

```
GET /api/v1/tasks/{task_id}/anti-hallucination-report
```

**Comportamento atteso (da implementare in 8.8B-REPORT-CODE, non in questo blocco):**

- **404** `RESOURCE_NOT_FOUND` con `details.resource='task_masters'` se il task non esiste (mirror della convenzione 8.4/8.6/8.7F/8.8A-READ-A).
- **200** se il task esiste, anche se incompleto (pre-8.7, pre-8.8A, draft mancante, gate mancante, published mancante, source-loss successivo a publish, eccetera). Il report deve **riflettere lo stato reale**, non fabbricare dati mancanti: ogni asse sa esporre la propria mancanza in modo strutturato (`null`, `missing`, `unknown` a seconda della semantica dell'asse).
- **Strettamente read-only.** Nessun INSERT/UPDATE/DELETE su nessuna tabella. Nessun audit. Nessun Redis. Nessun worker import (nessun import da `apps/worker/*` né dalle funzioni `assess_*` / `run_*`). Solo `SELECT` su tabelle reali.
- **Snapshot consistency point:** il report fotografa lo stato al momento della chiamata. Nessuna garanzia transazionale di consistenza con eventi concorrenti del worker: l'API è read-only e non blocca la pipeline. Una snapshot lievemente inconsistente è preferibile a una RETRY o a un lock applicativo.

### 3.2 Posizionamento architetturale

Il report aggregato **non sostituisce** gli endpoint read specialistici esistenti (8.7F, 8.6, 8.8A-READ-A, 8.4 answers). Le fonti di verità primarie restano le **tabelle append-only** e gli **endpoint specialistici**. Il report è una **vista derivata** task-level orientata a UI e audit umano: aggrega, normalizza nomenclatura, espone semantica anti-allucinazione, dichiara mock-indicators e limitazioni. Non introduce decisioni nuove e non muta lo stato del Gate.

Conseguenza per la documentazione (non per il codice di questo blocco): tutti i futuri consumer di entailment / source quality task-level dovrebbero poter continuare a usare gli endpoint specialistici, ma **la UI dovrebbe consumare il report aggregato**; questo riduce la superficie API che la UI deve conoscere e isola la UI dai dettagli di ordering interni delle read API.

### 3.3 Endpoint secondario (futuro, da valutare in 8.8B-REPORT-CODE)

```
GET /api/v1/published-answers/{published_answer_id}/anti-hallucination-report
```

**Raccomandazione operativa:** **non implementarlo in v1**. Partire da task-level. Il published-answer-level può essere costruito successivamente (eventuale 8.8B-REPORT-CODE-v2) o ricostruito lato UI a partire dal task-level via `published_answers.task_id`. Motivazione: il published-answer-level introduce ambiguità sul versioning (storia del task vs snapshot al momento della pubblicazione) che è prematuro decidere senza esperienza d'uso.

---

## 4. Shape JSON proposta

Si propone una shape indicativa per discussione in 8.8B-REPORT-CODE. **Non è uno schema Pydantic obbligatorio**; serve a stabilizzare la conversazione e a guidare la query layer. Il blocco CODE potrà raffinarla.

```jsonc
{
  "task_id": "uuid",
  "project_id": "uuid",
  "tenant_id": "uuid",

  "task": {
    "status": "created|analyzing|analyzed_partial|compiling|published|blocked|failed|cancelled|archived",
    "objective": "string",
    "mode": "closed_corpus",
    "created_at": "iso8601",
    "updated_at": "iso8601"
  },

  "publication": {
    "status": "published|withdrawn|superseded|publication_held|not_ready|failed|unknown",
    "published_answer_id": "uuid|null",
    "published_answer_status": "published|withdrawn|superseded|null",
    "summary_text": "string|null",
    "content_hash": "sha256-hex|null",
    "final_gate_report_id": "uuid|null"
  },

  "gate": {
    "decision": "approved|rejected|null",
    "reason_code": "no_verified_claims|unverified_spans_present|entailment_block|source_quality_block|all_spans_verified_with_warnings|all_spans_verified|null",
    "policy_name": "string|null",
    "policy_version": "string|null",
    "payload": { /* JSONB verbatim del final_gate_reports.payload */ },
    "coverage_gaps": [
      {
        "id": "uuid",
        "kind": "...",
        "severity": "info|warn|block",
        "gap_key": "string",
        "details": { /* JSONB verbatim */ },
        "axis": "cve_lite|source_quality|claim_entailment|source_loss|coverage|other"
      }
    ]
  },

  "claims": [
    {
      "logical_claim_id": "uuid",
      "latest_entry_id": "uuid",
      "latest_state": "candidate|verified_fact|unverifiable|...",
      "canonical_claim_text": "string",
      "claim_type": "factual|causal|opinion|recommendation|hypothesis|scenario|null",
      "support_scope": "string",
      "evidence_links": [
        {
          "claim_evidence_link_id": "uuid",
          "evidence_span_id": "uuid",
          "link_role": "primary_support|supporting_context|counter_evidence"
        }
      ],
      "cve_lite": [
        {
          "verification_record_id": "uuid",
          "claim_ledger_entry_id": "uuid",
          "outcome": "pass|fail|inconclusive",
          "check_name": "quote_hash_and_substring_v1"
        }
      ],
      "source_quality": [
        {
          "evidence_span_id": "uuid",
          "latest_assessment_id": "uuid|null",
          "overall_quality": "strong|adequate|weak|unsuitable|unknown|null",
          "contradiction_status": "no_known_contradiction|contradicted_by_stronger_source|conflicting_sources|unchecked|null",
          "evaluator_name": "string|null",
          "policy_name": "string|null",
          "policy_version": "string|null",
          "mock": true
        }
      ],
      "entailment": [
        {
          "claim_ledger_entry_id": "uuid",
          "evidence_span_id": "uuid",
          "latest_check_id": "uuid|null",
          "verdict": "entailed|partially_supported|not_supported|contradicted|uncertain|null",
          "confidence": 0.8,
          "checker_name": "string|null",
          "policy_name": "string|null",
          "policy_version": "string|null",
          "mock": true
        }
      ]
    }
  ],

  "evidence": [
    {
      "evidence_span_id": "uuid",
      "document_chunk_id": "uuid",
      "quote": "string",
      "quote_hash": "sha256-hex",
      "document_id": "uuid",
      "document_filename": "string"
    }
  ],

  "axis_summary": {
    "cve_lite": {
      "verified_claims_count": 0,
      "unverified_claims_count": 0,
      "inconclusive_count": 0
    },
    "source_quality": {
      "strong_count": 0,
      "adequate_count": 0,
      "weak_count": 0,
      "unsuitable_count": 0,
      "unknown_count": 0,
      "missing_count": 0
    },
    "claim_entailment": {
      "entailed_count": 0,
      "partially_supported_count": 0,
      "not_supported_count": 0,
      "contradicted_count": 0,
      "uncertain_count": 0,
      "missing_count": 0
    },
    "final_gate": {
      "has_blocking_gaps": false,
      "has_warnings": false,
      "blocking_gap_count": 0,
      "warning_gap_count": 0
    }
  },

  "mock_indicators": {
    "uses_mock_source_quality": true,
    "uses_mock_claim_entailment": true,
    "uses_mock_compiler": true,
    "uses_mock_cve_lite": true,
    "notes": ["..."]
  },

  "limitations": [
    "Una fonte citata non implica un claim vero.",
    "Una quote testualmente presente non implica che la quote sostenga il claim.",
    "Verdict 'entailed' del mock non implica che il claim sia vero nel mondo.",
    "Il branch 'entailment_block' è attivabile in MVP-0 solo via stub: il mock checker non emette 'contradicted'.",
    "Il branch 'source_quality_block' è attivabile in MVP-0 solo via stub: il mock evaluator non emette 'unsuitable'.",
    "Il payload JSONB è esposto verbatim; RBAC/redaction non implementata."
  ]
}
```

La shape è indicativa. Il blocco CODE può ridurre/espandere alcune sezioni, ma deve mantenere:
- separazione netta degli assi (`cve_lite`, `source_quality`, `claim_entailment` per ogni claim, e contatori per asse in `axis_summary`);
- `mock_indicators` esplicito e non rimovibile;
- `limitations` testuali, presenti anche su task "puliti".

---

## 5. Sorgenti DB per sezione

Mappatura ogni sezione → tabella reale. Solo `SELECT`; nessuna mutation.

**Task / scope:**
- `task_masters` (status, objective, mode, timestamps, tenant_id, project_id).
- `projects` (eventuale nome/metadata di progetto se utile alla UI).
- `tenants` (solo se necessario; in MVP-0 probabilmente non utile alla UI).

**Documents / evidence:**
- `task_documents` (associazione task ↔ documento, role, position).
- `uploaded_documents` (filename, content_hash, mime_type, tier).
- `document_versions` (versione 'parsed', text_hash).
- `document_chunks` (chunk_index, char range).
- `evidence_spans` (quote, quote_hash).

**Claims:**
- `logical_claims` (canonical_claim_text, canonical_claim_hash).
- `raw_claims`, `classified_claims` (utili per troubleshooting; **da valutare se includere nella v1**, vedi §15).
- `claim_ledger_entries` (latest per logical_claim, append-only, supersede via `claim_lineage`).
- `claim_evidence_links` (link claim ↔ evidence_span; CHECK `cel_origin_xor`).

**CVE-lite:**
- `verification_records` con `check_kind='cve_lite'`.
- `check_name='quote_hash_and_substring_v1'` (mock attuale).

**Source Quality:**
- `source_quality_assessments` (target XOR; in pratica `evidence_span_id` non NULL in MVP-0).
- **Latest per target evidence_span:** ordering deterministico `version_no DESC, created_at DESC, id DESC` (vedi §11).

**Claim Entailment:**
- `claim_entailment_checks` (granularità `(claim_ledger_entry_id, evidence_span_id)`).
- **Latest per pair:** ordering deterministico `version_no DESC, created_at DESC, id DESC` (vedi §11).
- Attenzione: l'endpoint 8.8A-READ-A ordina **globalmente** per `(created_at DESC, id DESC)` con limit. Il report deve sintetizzare correttamente la **latest-per-pair**, che è una semantica diversa.

**Final answer / gate:**
- `draft_final_answers` (latest v1 per task).
- `final_answer_spans` (append-only; span_index ASC).
- `final_answer_span_claim_links` (FK composita `fasc_entry_logical_consistency`).
- `final_gate_reports` (append-only, UNIQUE per draft; latest tramite ORDER BY created_at DESC, id DESC).
- `coverage_gap_statements` (UNIQUE composito `(draft_final_answer_id, kind, gap_key)`).
- `published_answers` (latest v1 per task; UNIQUE `(task_id, version_no)`).

**Lifecycle / source-loss:**
- `published_answer_lifecycle_events` (lifecycle del published).
- `source_loss_events`, `source_loss_propagation_records` (source-loss).
- **Decisione:** in v1 **dichiarare come estensione futura**. Vedi §15. Motivazione: l'asse source-loss è semanticamente ortogonale al "report di pubblicabilità" e merita un payload dedicato; includerlo subito rende la shape più rumorosa e meno leggibile. Riservare lo spazio nominale (`limitations`, eventuale futuro campo `source_loss`) senza popolarlo.

---

## 6. Semantica degli assi

Il report **rende leggibili assi separati**. Non compone un punteggio unico, non "decide". I disclaimer di seguito devono comparire (anche solo testualmente in `limitations`) ad ogni invocazione.

**CVE-lite (`verification_records`, `check_kind='cve_lite'`):**
- Verifica testuale: la `quote` è substring del `document_chunks.inline_text` AND `sha256(quote utf-8) == evidence_spans.quote_hash`.
- **Non valuta supporto semantico.** Una quote presente con hash valido non implica che la quote sostenga il claim.

**Source Quality (`source_quality_assessments`):**
- Valuta la **qualità della fonte** (authority, freshness, independence, contradiction_status come visto, extract_quality).
- **Non valuta se la fonte sostiene il claim.** Una fonte autorevole può ospitare una quote che non implica il claim.
- **Non valuta verità del claim.** Una fonte debole può ospitare una quote vera; una fonte forte può ospitare una quote falsa.

**Claim Entailment (`claim_entailment_checks`):**
- Valuta la **relazione semantica** claim ↔ `evidence_span` per la pair.
- `entailed` **non significa claim vero.** Significa: la quote (sotto la normalizzazione del checker) contiene/equivale al claim.
- `contradicted` **è locale alla pair, non un cross-source contradiction detector.** Quest'ultimo è 8.8C, mancante.

**Final Gate (`final_gate_reports`):**
- Compone gli assi per **pubblicabilità**, secondo la policy versionata. Priorità invariante: `no_verified_claims > unverified_spans_present > entailment_block > source_quality_block > approved_with_warnings > approved_clean`.
- **Non garantisce verità assoluta.** Blocca o segnala secondo policy MVP-0.

**Report aggregato:**
- **Aggrega e rende leggibile.**
- **Non introduce nuove decisioni.**
- **Non ricalcola il Gate.**
- **Non rivaluta claim né fonti.**
- **Non muta DB.**
- **Non sostituisce le tabelle append-only né gli endpoint specialistici.**

---

## 7. Stato publication/published/held

Il campo `publication.status` deve essere derivato in modo deterministico dalla combinazione di `published_answers`, `final_gate_reports` e `task_masters.status`. Proposta semantica (da ratificare in 8.8B-REPORT-CODE):

| Condizione DB | `publication.status` |
|---|---|
| `published_answers` v1 esiste con `status='published'` | `published` |
| `published_answers` v1 esiste con `status='withdrawn'` | `withdrawn` |
| `published_answers` v1 esiste con `status='superseded'` | `superseded` |
| Nessun `published_answers` AND `final_gate_reports.decision='rejected'` | `publication_held` |
| Nessun `published_answers` AND nessun `final_gate_reports` AND `task_masters.status` in `{created, analyzing, analyzed_partial, compiling}` | `not_ready` |
| `task_masters.status='failed'` | `failed` |
| Qualsiasi altro stato non riconducibile | `unknown` (fallback difensivo) |

**Nota critica:** `publication.status='published'` deve indicare una pubblicazione attualmente attiva, non la semplice esistenza storica di una riga in `published_answers`. Gli stati `withdrawn` e `superseded` sono stati reali di `published_answers.status` e vanno esposti come tali. `publication_held` invece è uno **stato derivato dal report**, non uno status DB di `task_masters`. La frase "task in `publication_held`" che compare in altri docs si riferisce all'evento audit `task.publication_held`, non a uno status. Il report deve essere chiaro su questa derivazione in `limitations`.

---

## 8. Coverage gaps

Il report deve esporre tutti i `coverage_gap_statements` collegati al draft del task. Per ogni gap esporre: `id`, `kind`, `severity`, `gap_key`, `details` (verbatim), `created_at`, e una colonna derivata `axis` per facilitare il rendering UI.

Mapping `kind` → `axis`:

| `kind` | `axis` derivato |
|---|---|
| `missing_evidence` | `coverage` (struttura del draft; tipicamente Branch A) |
| `unverified_claim` | `cve_lite` |
| `out_of_scope` | `coverage` (riservato; non emesso in MVP-0) |
| `source_loss` | `source_loss` (riservato; non emesso dal Gate in MVP-0) |
| `source_quality_block` | `source_quality` |
| `source_quality_warning` | `source_quality` |
| `entailment_block` | `claim_entailment` |
| `entailment_warning` | `claim_entailment` |

Il mapping è **una decorazione lato report**, non un cambiamento dello schema DB. Il `kind` resta autoritativo a livello DB; `axis` esiste solo per la UI.

`details.payload` può contenere collegamenti utili (`span_id`, `span_index`, `reasons[]` con `claim_ledger_entry_id`/`evidence_span_id`/`assessment_id`/`entailment_check_id`, `policy` con `name`+`version`); il report li espone verbatim. **Non riscrivere il payload**: la UI o un futuro RBAC layer si occupano di redaction.

---

## 9. Mock indicators

Il report deve rendere **esplicito** che gli assi sono mock-driven in MVP-0. Decisione operativa raccomandata (in 8.8B-REPORT-CODE):

- **Strategia ibrida (raccomandata):** derivare i mock indicator parte da identità del servizio (`evaluator_name`, `checker_name`, `policy_name`) e parte da inspection del `payload`. Specificamente:
  - `uses_mock_source_quality = true` se ALMENO una `source_quality_assessments` rilevante per il task ha `evaluator_name='mock_source_quality_evaluator'` (mock attuale) o `payload.mock=true`.
  - `uses_mock_claim_entailment = true` se ALMENO una `claim_entailment_checks` rilevante per il task ha `checker_name='mvp0_mock_entailment_checker'` o `payload.mock=true`.
  - `uses_mock_compiler = true` se `draft_final_answers.compiler_name='mvp0_compiler_v1'`.
  - `uses_mock_cve_lite = true` se `verification_records.check_kind='cve_lite'` con `check_name='quote_hash_and_substring_v1'`.
  - `notes`: stringhe brevi con avvisi semantici (es. "mock entailment NON emette 'contradicted'", "mock SQ NON emette 'unsuitable'", "una quote presente non implica supporto semantico"). Le frasi sono **disclaimer testuali**, non flag programmabili.
- **Default sicuro:** in MVP-0 con `PROVIDERS_ENABLED=mock` tutti gli indicator restano `true`. La policy `PROVIDERS_ENABLED` non è oggi consumata dal codice di routing, ma il report può **dichiarare** in `notes` che il deployment è in modalità mock.
- **Non hardcodare la lista in un costante immutable**: deve restare derivabile, così l'arrivo di un checker reale produce automaticamente `uses_mock_*=false` senza intervento del report.

---

## 10. Redaction / RBAC debt

- Gli endpoint read 8.6, 8.7F, 8.8A-READ-A oggi espongono `payload` e `details` JSONB **verbatim**, senza RBAC né redaction.
- Il report aggregato **aggrava** il rischio di esposizione perché compone payload provenienti da molti assi in un singolo response body.
- **Decisione operativa MVP-0:** non implementare RBAC/redaction in 8.8B-REPORT-CODE. Dichiarare esplicitamente questo debito in `limitations` e nel `PHASE_8_8B_REPORT_CODE-DOC` futuro.
- **Vincoli sulla shape:** la shape deve essere progettata in modo da **rendere agevole una futura redaction**:
  - `payload` e `details` JSONB sono oggetti con confini chiari (un futuro layer può redarli per chiave);
  - dati sensibili (testo claim, testo quote, filename documento) sono in campi nominati distinti, non innestati profondamente in JSONB;
  - non includere stack traces né error messages interni del Gate/orchestrator.

---

## 11. Ordering e latest semantics

Il report deve essere **deterministico** sull'output per lo stesso stato DB. Regole proposte:

- **Latest claim_ledger_entry per `logical_claim`:**
  `ORDER BY version_no DESC, created_at DESC, id DESC LIMIT 1`.
  Mirror della query usata dal Gate (`_select_spans_with_links` / `_select_source_quality_per_span` / `_select_entailment_per_span` in `final_answer_gate.py`).

- **Latest source_quality_assessments per `evidence_span_id`:**
  `ORDER BY version_no DESC, created_at DESC, id DESC LIMIT 1`.
  Mirror del Gate. **Non usare** "latest in slice" come fa l'endpoint task-level 8.7F (che ordina ASC e prende l'ultimo nello slice): la semantica è diversa, e il report deve mostrare la latest **assoluta DB-level** (la stessa consultata dal Gate). Questa differenza va dichiarata in `limitations`.

- **Latest claim_entailment_checks per pair `(claim_ledger_entry_id, evidence_span_id)`:**
  `ORDER BY version_no DESC, created_at DESC, id DESC LIMIT 1`.
  Mirror del Gate. **Non usare** l'ordering globale `created_at DESC, id DESC` dell'endpoint 8.8A-READ-A: in 8.8A-READ-A il task-level espone una lista cronologica, non un latest-per-pair.

- **Coverage gaps:**
  **Raccomandazione: severity-first, poi created_at ASC.** Motivazione UI: i block prima, i warning poi, i puramente informativi alla fine. In assenza di prio UI esplicita, una semplice `created_at ASC` è ammessa come fallback. La scelta va ratificata in CODE.

- **Claims:**
  Deterministic. Raccomandazione: `(logical_claims.created_at ASC, logical_claims.id ASC)`. Coerente con l'ordering già usato dal compiler in `compiler._select_verified_latest_for_task`.

- **Evidence:**
  `(document_id ASC, document_chunks.chunk_index ASC, evidence_spans.char_start ASC, evidence_spans.id ASC)`. Determinismo + ricostruzione logica del documento.

**Raccomandazione forte:** per Source Quality e Claim Entailment il report **deve** usare il latest assoluto DB-level per target/pair (ORDER BY `version_no DESC, created_at DESC, id DESC`). **Non** usare "latest in slice" dipendente da `limit`. Questa è la semantica già adottata dal Final Answer Gate e l'unica che produce un report coerente con la decisione del Gate.

---

## 12. Errori e casi edge

Il report deve gestire i seguenti casi senza inventare dati:

- **Task inesistente:** 404 `RESOURCE_NOT_FOUND` con `details.resource='task_masters'`, `details.id=str(task_id)`.
- **Task esistente senza documenti:** 200 con `evidence=[]`, `claims=[]`, `gate.decision=null`, `publication.status='not_ready'`. Sezioni vuote, non assenti.
- **Task con documenti ma senza claim estratti:** 200 con `evidence` popolato e `claims=[]`. `axis_summary` con contatori a 0.
- **Task pre-8.7 senza `source_quality_assessments`:** ogni claim espone `source_quality=[{...latest_assessment_id: null, overall_quality: null, ...}]` per ogni `evidence_span` linkato; `axis_summary.source_quality.missing_count` incrementato.
- **Task pre-8.8A senza `claim_entailment_checks`:** ogni claim espone `entailment=[{...latest_check_id: null, verdict: null, ...}]` per ogni pair `(entry, span)`; `axis_summary.claim_entailment.missing_count` incrementato.
- **Source quality step fallito (audit `task.source_quality_assessed` con `status='failed'`):** il report mostra `source_quality` parziale o vuoto per le pair coinvolte; eventuale `notes` esplicita.
- **Entailment step fallito:** stesso pattern.
- **Final gate report mancante (task in `analyzing`/`compiling` senza essere arrivato al Gate):** `gate.decision=null`, `gate.reason_code=null`, `coverage_gaps=[]`, `publication.status='not_ready'`.
- **Published answer mancante con gate rejected:** `publication.status='publication_held'`.
- **Coverage gaps presenti ma published assente:** caso normale di rejected; va mostrato.
- **Source-loss successiva alla pubblicazione:** in v1 **non incluso** nel report aggregato; documentare in `limitations` che la sezione lifecycle/source-loss è estensione futura. La UI può continuare a consumare `/api/v1/source-loss-events/...` separatamente.
- **Dati inconsistenti (es. claim_evidence_links che punta a entry non latest):** il report mostra il dato reale come letto dal DB; non corregge. Eventuale segnalazione su `notes`.

Il report **non deve mai** inventare valori non presenti a DB. La regola guida è: "se è `null` a DB, è `null` nel report".

---

## 13. Test futuri per 8.8B-REPORT-CODE

Test da proporre, NON da scrivere in questo blocco.

**Unit / API tests:**

1. 404 per task inesistente, `details.resource='task_masters'`.
2. 200 per task esistente senza documenti, sezioni vuote coerenti.
3. 200 per task con published answer e warning di source quality (`reason_code='all_spans_verified_with_warnings'`), `publication.status='published'`, `gate.coverage_gaps` non vuoto.
4. 200 per task con warning di entailment (mock heuristic uncertain/not_supported).
5. 200 per task con `entailment_block` (via seed/stub): `publication.status='publication_held'`, `axis_summary.claim_entailment.contradicted_count >= 1`.
6. 200 per task con `source_quality_block` (via seed/stub): `publication.status='publication_held'`, `axis_summary.source_quality.unsuitable_count >= 1`.
7. 200 per task pre-8.7 / pre-8.8A senza assessment/checks: campi `null` e `missing_count` incrementato senza errori.
8. **Read-only snapshot:** count pre/post invariato su `claim_entailment_checks`, `source_quality_assessments`, `claim_ledger_entries`, `final_gate_reports`, `coverage_gap_statements`, `published_answers`, `audit_records`. Mirror del pattern già usato in `test_claim_entailment_read_endpoint.py` e `test_source_quality_read_endpoint.py`.
9. **Deterministic ordering:** stesso stato DB → stesso payload (con possibile eccezione di campi timestamp).
10. **No worker imports / no Redis:** il modulo route non importa `apps/worker/*` né `redis`; assertion testabile lato CI tramite ispezione `sys.modules` post-import, o tramite un test che blocca l'engine Redis.

**Realistic flow tests (root-level):**

- Warning path mock-driven (riusare la macchina già rodata in `tests/test_phase_8_8a_entailment_gate_flow.py::test_phase_8_8a_entailment_warning_flow_end_to_end` per arrivare allo stato approved-with-warnings, poi GET sul report e verifica della shape).
- Block path entailment via stub dell'orchestrator (mirror del block path 8.8A-GATE-FLOW).
- Block path source quality via stub dell'orchestrator SQ (mirror del block path 8.7H).

**Regression tests:**

- Esistenti `test_answers_endpoints.py`, `test_source_quality_read_endpoint.py`, `test_claim_entailment_read_endpoint.py` invariati: nessun cambio di signature o di body sui rispettivi endpoint.

---

## 14. Possibile implementazione futura 8.8B-REPORT-CODE

File toccati nel futuro blocco CODE:

**Probabile:**

- `apps/api/app/routes/anti_hallucination_report.py` (nuovo).
- `apps/api/app/main.py` (registrazione router).
- `apps/api/tests/test_anti_hallucination_report_endpoint.py` (nuovo).

**Possibile ma da valutare separatamente (raccomandazione: NON in 8.8B-REPORT-CODE):**

- `packages/shared/evidencefirst_shared/schemas.py` per `AntiHallucinationReportRead` Pydantic. **Raccomandazione:** valutare in un blocco dedicato **8.8B-REPORT-SHARED**, prima del CODE, **solo se** la shape converge stabilmente in §4. Altrimenti procedere con wrapper inline nel route module (pattern già usato da `apps/api/app/routes/answers.py` con `WithdrawalRequestCreate` / `WithdrawalRequestQueued`).

**File da NON toccare in CODE (invariante):**

- Migrations: nessuna nuova migration richiesta da 8.8B-REPORT-CODE.
- Worker: `apps/worker/*` non modificato.
- `apps/worker/app/services/final_answer_gate.py` invariato.
- `apps/worker/app/services/source_quality_evaluator.py` invariato.
- `apps/worker/app/services/source_quality_orchestrator.py` invariato.
- `apps/worker/app/services/claim_entailment_checker.py` invariato.
- `apps/worker/app/services/claim_entailment_orchestrator.py` invariato.
- `apps/worker/app/services/compiler.py` invariato.
- `apps/worker/app/consumers/*` invariati.
- Endpoint specialistici esistenti invariati.

---

## 15. Decisioni aperte

| Decisione | Opzioni | Raccomandazione |
|---|---|---|
| Schema Pydantic shared o wrapper inline | (a) introdurre `AntiHallucinationReportRead` shared in 8.8B-REPORT-SHARED; (b) wrapper inline nel route module | **(b) wrapper inline** nel CODE; promuovere a shared solo se la shape resta stabile dopo le prime due-tre integrazioni UI. Pattern coerente con `apps/api/app/routes/answers.py`. |
| Report task-level vs published-answer-level | (a) solo task-level in v1; (b) entrambi in v1 | **(a) solo task-level** in v1. Vedi §3.3. |
| Lifecycle / source-loss in v1 | (a) includere subito; (b) estensione futura | **(b) estensione futura.** Vedi §5. |
| Raw e classified claims | (a) includere tutti; (b) solo logical + latest ledger entry | **(b) solo logical + latest entry** in v1. raw/classified accessibili via endpoint specialistici. Inclusione opzionale via query param `?include=raw_claims,classified_claims` da valutare in CODE. |
| Dettaglio del payload JSONB | (a) verbatim integrale; (b) selezione di campi | **(a) verbatim integrale** in MVP-0. La selezione è compito di un futuro layer RBAC/redaction. |
| Summary counts: SQL o Python? | (a) calcolare in SQL (un'unica query con aggregazione); (b) calcolare in Python iterando i row results | **(b) Python** in v1 per leggibilità e per accomodarsi più facilmente alla logica di latest-per-target. SQL ottimizzato solo se profiling lo richiede. |
| Ordering coverage gaps | (a) severity-first poi `created_at ASC`; (b) `created_at ASC` puro | **(a) severity-first**, fallback `created_at ASC`. Vedi §11. |
| Report per task non terminali | (a) restituire 200 con campi parziali e `publication.status='not_ready'`; (b) restituire 422 o 409 fino al gate | **(a) 200 con campi parziali.** Il report deve essere sempre disponibile come strumento di diagnostica anche prima del Gate. |
| Pagination | (a) limit-only; (b) cursor | **(a) limit-only** in v1 (mirror del pattern 8.6/8.7F/8.8A-READ-A). Cursor pagination è già nota come debito globale. |

Tutte le decisioni "aperte" hanno una raccomandazione operativa per evitare di lasciare il prossimo blocco indefinito.

---

## 16. Raccomandazione finale

Sequenza operativa raccomandata:

1. **8.8B-REPORT-PRE** (questo documento) — done con questo blocco.
2. **8.8B-REPORT-CODE** — implementare `GET /api/v1/tasks/{task_id}/anti-hallucination-report` task-level con wrapper inline (no shared schema in v1), latest-per-target via ORDER BY `version_no DESC, created_at DESC, id DESC`, ordering coverage gaps severity-first, esclusione lifecycle/source-loss, mock indicators derivati da nome servizio + `payload.mock`, payload JSONB esposto verbatim, RBAC non implementata e dichiarata in `limitations`.
3. **8.8B-REPORT-FLOW** — realistic flow test root-level con warning path e due block path (entailment via stub, source quality via stub), mirror 8.7H / 8.8A-GATE-FLOW.
4. **UI-PRE** — apertura della fase UI usando il report aggregato come primo contratto stabile.
5. **8.8A-READ-B** (claim-level per `claim_entailment_checks`) — backlog. Da realizzare se la UI di dettaglio claim lo richiede; altrimenti può restare differito a 8.8A-READ-B post-UI.

**Vincoli sempre validi (MVP-0):**

- Closed Corpus only.
- `PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0`.
- SQLAlchemy 2.0 Core, bound parameters.
- Append-only enforced a DB (le tabelle restano insert-only; il report è read-only).
- Idempotenza redelivery via UNIQUE composito sui dati sorgente (non rilevante per il report che non scrive).
- Test rerun-safe con UUID/hash unici per invocazione.
- Disclaimer anti-allucinazione preservato in ogni report e in ogni documento futuro: una fonte citata non implica un claim vero; una quote presente non implica supporto semantico; un verdict `entailed` non implica verità del claim nel mondo.

---

## FILE_COMPLETATI

- `PHASE_8_8B_REPORT_PRE.md`

## FILE_NON_MODIFICATI

- `migrations/*`
- `apps/api/*`
- `apps/worker/*`
- `packages/shared/*`
- `tests/*`
- `README.md`
- `PROJECT_STATE.md`
- `PHASE_8_8A_PRE.md`
- `PHASE_8_8A_GATE_PRE.md`
- `PHASE_8_7_PLAN.md`

## FILE_DA_FARE_PROSSIMO_BLOCCO

- **8.8B-REPORT-CODE**: nuovo `apps/api/app/routes/anti_hallucination_report.py`, registrazione in `apps/api/app/main.py`, nuovo `apps/api/tests/test_anti_hallucination_report_endpoint.py` con i test enumerati in §13. Implementazione strettamente read-only secondo la shape §4, latest semantics §11, gestione casi edge §12, mock indicators §9, ordering coverage gaps severity-first.
- (Opzionale, da valutare prima di CODE): **8.8B-REPORT-SHARED** per `AntiHallucinationReportRead` Pydantic in `packages/shared/evidencefirst_shared/schemas.py` se la shape è stabile.

## RISCHI_RESIDUI

- **Esposizione payload JSONB senza redaction:** il report compone payload da molti assi; il rischio di leak è maggiore del singolo endpoint specialistico. RBAC/redaction rinviata, dichiarata in `limitations`.
- **Latest semantics differente dall'endpoint 8.7F:** il report usa latest assoluto DB-level; l'endpoint task-level 8.7F espone "latest in slice". I due possono divergere; va dichiarato in `limitations` per non confondere consumer che mescolano le due superfici.
- **Latest semantics differente dall'endpoint 8.8A-READ-A:** stesso problema; 8.8A-READ-A ordina globalmente, il report sintetizza latest-per-pair.
- **Branch `entailment_block` dormiente in produzione mock:** il mock checker non produce `contradicted`. Il report deve mostrare lo stato reale; `axis_summary.claim_entailment.contradicted_count` sarà tipicamente 0. Il `notes` lo deve esplicitare.
- **Branch `source_quality_block` dormiente in produzione mock:** analogo. Mock evaluator scrive solo `overall_quality='unknown'` + `contradiction_status='unchecked'`.
- **N+1 latente:** task → claims → evidence_spans → assessments → checks. Implementabile con join + LATERAL come fa il Gate, oppure con N+1 in Python. La scelta è di CODE; in MVP-0 numero di claim/span per task è bounded e l'N+1 è accettabile.
- **Inflazione semantica del report:** il rischio di trattarlo come "fonte di verità" anziché vista derivata. Mitigazione: `limitations` testuali, separazione netta degli assi, mock_indicators sempre presenti, e disclaimer ripetuto in tutta la documentazione futura.
- **Dipendenza implicita dalla policy del Gate:** se la policy `mvp0_entailment_gate_policy` o la policy SQ vengono bumpate, il report deve continuare a essere coerente con la decisione del Gate. Mitigazione: il report **non ricalcola** la decisione; legge `final_gate_reports.decision/reason_code/payload` e li espone verbatim.
- **No claim-level entailment read API in v1:** il dettaglio claim-level resta non esposto via HTTP. La UI può ricavarlo dal report aggregato in molti casi, ma non è sostitutivo dell'endpoint dedicato. Rinviato a 8.8A-READ-B.
- **Rischi ereditati invariati:** no Citation-to-Claim Validator (8.8B storico), no Contradiction Detector reale (8.8C), no Final Answer Sentence Gate (8.8D), no External Verification / Web-RAG (8.9), no Multi-agent consensus (9.0), no UI, no retention distruttiva, no trigger append-only su `coverage_gap_statements`, no worker main loop reale negli end-to-end test, no backfill per task pre-8.7E / pre-8.8A-WORKER.
