# PHASE_8_8A_PRE — Claim Entailment Checker (analisi architetturale pre-codice)

Documento **decisionale e di piano** per l'apertura della Fase 8.8 dell'Evidence-First MVP-0. Questo blocco è **solo analisi e progettazione**: non scrive codice applicativo, non scrive migration, non scrive test implementativi. Il solo deliverable è questo documento.

**Commit di partenza implicito**: stato post-8.7H al main attuale `b3231a51290777c53e73c38c2e835e5149efc78e` ("Close phase 8.7 source quality"). Lo stato tecnico di riferimento è quello descritto in `PROJECT_STATE.md` e `PHASE_8_7_PLAN.md` al commit `b70ef8fb394e0f28befdfd2b3a699c32a88e9914` ("Add phase 8.7 source quality realistic flow").

**Promessa anti-allucinazione (invariata e ribadita).** La piattaforma non promette di eliminare le allucinazioni in senso assoluto. La promessa corretta è: **il sistema è progettato per impedire che claim fattuali non supportati, contraddetti o basati su fonti inadeguate vengano pubblicati come affidabili.** Source quality citata non implica claim vero; quote presente non implica claim implicato dalla quote. La Fase 8.8A si occupa di quest'ultima distinzione.

---

## 1. Stato di partenza post-8.7H

Tutto ciò che segue è riassunto direttamente dai file letti (`PROJECT_STATE.md`, `PHASE_8_7_PLAN.md`, `PHASE_8_7H_PRE.md`, le migration 0004/0005/0007/0008, i servizi worker 8.7E/G, le read API 8.7F, il consumer `task_created.py`, gli schemi shared). Nessuna affermazione è inventata.

### 1.1 Schema DB applicato e immutabile

| Migration | Stato | Contenuto rilevante per 8.8A |
|---|---|---|
| `0001_foundation.sql` | applicata, immutabile | `tenants`, `users`, `projects`, `task_masters`, `audit_records` append-only via trigger comune `reject_modify_append_only`, `event_processing_records`. |
| `0002_storage.sql` | applicata, immutabile | `storage_blobs`, `storage_objects`, `evidence_spans` append-only. |
| `0003_documents.sql` | applicata, immutabile | `uploaded_documents`, `document_versions`, `document_chunks`. |
| `0004_claim_ledger.sql` | applicata, immutabile | **Tabelle chiave per 8.8A**: `logical_claims`, `claim_ledger_entries` (append-only stretto), `claim_evidence_links` (con CHECK `cel_origin_xor` e UNIQUE `(claim_ledger_entry_id, evidence_span_id)` e FK composita `cel_entry_logical_consistency` su `claim_ledger_entries(id, claim_logical_id)`), `verification_records` (UNIQUE su `(claim_ledger_entry_id, check_kind, check_name)`, `check_kind ∈ {csv, cve_lite, nli, judge}`). |
| `0005_answers_gate.sql` | applicata, immutabile | `draft_final_answers`, `final_answer_spans` (append-only), `final_answer_span_claim_links` (FK composita `fasc_entry_logical_consistency`), `final_gate_reports` (append-only, UNIQUE per draft), `published_answers`, `coverage_gap_statements` (UNIQUE `(draft_final_answer_id, kind, gap_key)`). |
| `0006_lifecycle.sql` | applicata (8.5), immutabile | lifecycle / source loss, fuori scope diretto 8.8A. |
| `0007_source_quality.sql` | applicata (8.7B), immutabile | `source_quality_assessments` append-only, codomini codificati. |
| `0008_coverage_gap_source_quality.sql` | applicata (8.7G), immutabile | estende `coverage_gap_statements.kind` da quattro a sei valori (`unverified_claim`, `missing_evidence`, `out_of_scope`, `source_loss`, `source_quality_block`, `source_quality_warning`). |

**Conseguenza per la numerazione**: la prossima migration disponibile è **0009**. La retention futura distruttiva, già rinviata in 8.7G/H, slitterà a 0010 (o successivo) se 0009 viene assegnato a 8.8A.

### 1.2 Pipeline worker corrente (`apps/worker/app/consumers/task_created.py`)

Sequenza eventi audit per task con documenti, approved scenario (mock attuale → warning flow):

```
task.created (event)
  -> task.analyzing
  -> task.docs_loaded
  -> task.claims_extracted
  -> task.claims_classified
  -> task.claims_ledger_initialized
  -> task.cve_lite_started
  -> task.cve_lite_completed
  -> task.analyzed_partial
  -> task.source_quality_assessed        (8.7E — SAVEPOINT-wrapped, status='completed'|'failed')
  -> task.compiling
  -> task.draft_compiled
  -> task.final_gate_started
  -> task.final_gate_completed
  -> task.published                       (oppure task.publication_held se rejected)
```

Lo step 8.7E è incapsulato in `conn.begin_nested()` (vedi `_run_8_7_source_quality` in `task_created.py`); audit aggregato `task.source_quality_assessed` emesso una sola volta per task, success o failure. Resume da `analyzed_partial`/`compiling` NON re-esegue 8.7E.

### 1.3 Final Answer Gate corrente (`apps/worker/app/services/final_answer_gate.py`)

Branch decisionali post-8.7G (validati end-to-end da 8.7H):

| Condizione | `decision` | `reason_code` | `coverage_gap_statements` |
|---|---|---|---|
| Zero spans | `rejected` | `no_verified_claims` | `missing_evidence` |
| ≥1 span non verified-backed (priorità CVE-lite) | `rejected` | `unverified_spans_present` | un `unverified_claim` per span scoperto |
| Tutti verified-backed + ≥1 span con source quality block | `rejected` | `source_quality_block` | `source_quality_block` + eventuali warning |
| Tutti verified-backed + ≥1 span con source quality warning | `approved` | `all_spans_verified_with_warnings` | `source_quality_warning` per span |
| Tutti verified-backed + nessun warning | `approved` | `all_spans_verified` | nessuno |

**Regola "verified-backed"** (8.4, invariata in 8.7G/H):
```
link.claim_ledger_entry_id == latest_entry_id_for(claim_logical_id)
AND latest_entry_state_for(claim_logical_id) == 'verified_fact'
```

**Priorità invariante**: CVE-lite > Source Quality. Il Gate consulta `source_quality_assessments` solo quando ogni span è verified-backed.

### 1.4 Read API 8.7F

- `GET /api/v1/evidence-spans/{evidence_span_id}/source-quality`
- `GET /api/v1/tasks/{task_id}/source-quality`

Read-only end-to-end, JSONB `payload` verbatim, nessuna RBAC redaction.

### 1.5 Cosa manca esplicitamente nel sistema

Dal `PHASE_8_7_PLAN.md §13` (anti-allucinazione roadmap), componenti **dichiaratamente mancanti**:

- **8.8A — Claim Entailment Checker.** Mancante.
- 8.8B — Citation-to-Claim Validator. Mancante.
- 8.8C — Contradiction Detector reale. Mancante.
- 8.8D — Final Answer Sentence Gate. Mancante.
- 8.8E — Anti-Hallucination Report API. Mancante.
- 8.9 — External Verification / Web-RAG controllato. Mancante.
- 9.0 — Multi-agent consensus + adversarial review reale. Mancante.

Questo blocco apre la prima voce della lista.

---

## 2. Problema da risolvere in 8.8A: perché CVE-lite + Source Quality non bastano

### 2.1 Cosa verifica oggi il sistema

Per ogni span del draft:
1. **Evidence support** (esiste un `claim_evidence_links` claim → `evidence_span_id`). Inserito dall'extractor 8.3 e da CVE-lite 8.3.
2. **CVE-lite verification** (`apps/worker/app/services/cve_lite.py`): la `quote` dello `evidence_spans` è substring del `document_chunks.inline_text` AND `sha256(quote utf-8) == evidence_spans.quote_hash`. Outcome scritto in `verification_records` con `check_kind='cve_lite'`, `check_name='quote_hash_and_substring_v1'`. Su PASS, ledger v2 con `state='verified_fact'`.
3. **Source Quality** (`source_quality_evaluator.py` + `source_quality_orchestrator.py`): valuta la fonte che ospita la quote (autorità, freschezza, indipendenza, ecc.). Mock attuale produce sempre `overall_quality='unknown'` + `contradiction_status='unchecked'`.
4. **Final Answer Gate** (`final_answer_gate.py`): compone i tre assi precedenti in una decision.

### 2.2 Cosa NON verifica oggi il sistema

**La relazione semantica claim ↔ evidence_span**: il fatto che la quote, anche se testualmente presente e proveniente da una fonte adeguata, **implichi effettivamente il claim** che si pretende di supportare.

Esempi concreti (riprendendo il prompt operativo):

- Evidence span: `"Revenue grew by 37% in Q3."`
- Claim A (forma debole, corretta): `"Revenue grew by 37% in Q3."` — entailed.
- Claim B (claim troppo forte): `"The company is financially healthy."` — non implicato dalla quote (Q3 ≠ salute finanziaria).
- Claim C (claim non implicato): `"The company became market leader."` — irrilevante rispetto alla quote.
- Claim D (claim contraddetto): `"Revenue declined in Q3."` — contraddetto dalla quote.

Oggi:
- CVE-lite passa per tutti i quattro casi (la quote è presente e ha l'hash giusto).
- Source Quality valuta la fonte, non la coerenza tra claim e quote.
- Il Final Answer Gate approva tutti i quattro casi nei branch B / B' (clean o warning), perché la regola verified-backed è soddisfatta.

Conseguenza: **un claim non implicato dalla quote o addirittura contraddetto dalla quote viene pubblicato come `verified_fact`**, purché esistano:
1. un evidence_span con quote testualmente presente,
2. un quote_hash corretto,
3. un link `claim_evidence_links` ben formato verso la v2 `verified_fact`.

Questo è il primo buco anti-allucinazione lasciato aperto dalla pipeline 8.7. Il Claim Entailment Checker (8.8A) chiude esattamente questo buco.

### 2.3 Cosa il Claim Entailment Checker NON è

Per evitare confusione con i blocchi successivi (8.8B/C/D):
- **NON è** un Citation-to-Claim Validator (8.8B): quello verifica che il claim citi le evidenze giuste, non vicine. È una verifica di selezione, non di implicazione.
- **NON è** un Contradiction Detector reale (8.8C): quello rileva contraddizioni tra claim o tra fonti diverse. Un singolo entailment checker che restituisca `contradicted` su una coppia (claim, quote) è una segnalazione locale, non una contraddizione cross-source globale.
- **NON è** un Final Answer Sentence Gate (8.8D): quello opera a livello di frase del published_answer, non a livello di (claim, evidence_span).
- **NON è** un giudice di verità del claim. L'entailment dice solo: "questa quote implica/contraddice/non supporta questo claim". Non dice se il claim è vero nel mondo.

---

## 3. Definizione operativa di entailment

Per evitare slittamenti semantici futuri, fissiamo qui le definizioni operative e le distinzioni rispetto ai concetti già esistenti.

### 3.1 Codomino dei verdict proposto

Cinque valori:

- **`entailed`**: la quote implica o è equivalente al claim. Caso "claim debole = quote", o "claim ⊂ quote" semanticamente.
- **`partially_supported`**: la quote supporta parte del claim ma non tutto (es. la quote dà solo Q3, il claim parla di "tutto l'anno"). Decision policy lo tratta come stato intermedio, vedi §7.
- **`not_supported`**: la quote non implica il claim e non lo contraddice. Tipico caso "claim irrilevante" rispetto al contenuto della quote.
- **`contradicted`**: la quote contraddice direttamente il claim (claim afferma X, quote afferma ¬X, o un numero/segno opposto).
- **`uncertain`**: il checker non sa decidere. Tipico per la versione mock MVP-0 quando un'euristica non si applica.

Questo codomino è esplicitamente **diverso** da:
- `verification_records.outcome ∈ {pass, fail, inconclusive}` (CVE-lite parla di hash/substring),
- `claim_ledger_entries.state ∈ {candidate, verified_fact, ..., unverifiable, rejected}` (parla dello stato del claim nel Ledger),
- `source_quality_assessments.contradiction_status ∈ {no_known_contradiction, contradicted_by_stronger_source, conflicting_sources, unchecked}` (parla del rapporto della fonte con altre fonti, non claim ↔ quote).

### 3.2 Distinzioni invariate

| Concetto | Tabella | Dimensione misurata |
|---|---|---|
| Evidence support | `claim_evidence_links` | esiste il link claim → evidence_span. |
| CVE-lite verification | `verification_records` (`check_kind='cve_lite'`) | la quote è testualmente presente nel chunk e ha l'hash atteso. |
| Source quality | `source_quality_assessments` | autorità / freschezza / indipendenza / qualità estratto della fonte. |
| **Claim entailment (8.8A, nuovo)** | **`claim_entailment_checks`** (proposto) | **la quote semanticamente implica/non-implica/contraddice il claim.** |
| Final answer gate | `final_gate_reports` | composizione delle dimensioni precedenti in una decisione di pubblicazione. |

Ognuno di questi assi è separato. **Una fonte citata non implica un claim vero**; **una quote presente non implica che la quote sostenga il claim**.

---

## 4. Invarianti semantiche

Queste invarianti devono essere preservate dal blocco 8.8A-CODE e da ogni blocco futuro che tocchi entailment, salvo motivazione esplicita e migration dedicata.

1. **claim correctness ≠ evidence support**: invariata.
2. **evidence support ≠ entailment**: nuova. Il link claim → span non implica nulla sull'implicazione semantica.
3. **entailment ≠ source quality**: nuova. Una fonte autorevole può ospitare una quote che non implica il claim. Una fonte debole può ospitare una quote che lo implica esattamente.
4. **source quality ≠ verification outcome**: invariata.
5. **contradiction detection ≠ entailment singolo**: nuova. `verdict='contradicted'` su una sola coppia (claim, quote) è una segnalazione locale; il Contradiction Detector reale (8.8C) opera su molteplicità di fonti.
6. **final answer gate ≠ verità assoluta**: invariata.
7. **append-only**: ogni riga di `claim_entailment_checks` (se introdotta) è append-only via trigger comune `reject_modify_append_only`, coerente con `claim_ledger_entries`, `final_answer_spans`, `final_gate_reports`, `source_quality_assessments`, `audit_records`, `evidence_spans`, `published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records`.
8. **invarianza del Claim Ledger**: l'entailment checker NON muta `claim_ledger_entries`, NON aggiunge una transizione di stato `entailment_failed`, NON crea v(N+1) `unverifiable` su `not_supported`. Eventuali ricadute sul ledger sono compito di blocchi futuri (8.8D / human review), non del checker.
9. **invarianza CVE-lite**: l'entailment checker NON sostituisce CVE-lite. La presenza testuale e l'hash della quote restano competenza di `cve_lite.py`. L'entailment riceve come input due cose già verificate dal CVE-lite (quote presente, hash corretto) e aggiunge il giudizio semantico.
10. **invarianza Source Quality**: l'entailment checker NON sostituisce Source Quality. Le due dimensioni sono ortogonali. Un span può essere `entailed` con `overall_quality='unsuitable'`, o `not_supported` con `overall_quality='strong'`; sono casi reali e devono restare distinguibili nel Final Answer Gate.

### 4.1 Priorità da preservare nel Final Answer Gate

L'invariante critica di 8.7G ("CVE-lite > Source Quality") va estesa, motivando la nuova posizione di entailment nello stack. La proposta è:

```
CVE-lite (verified-backed)  >  Claim Entailment  >  Source Quality
```

Motivazione:
- Se CVE-lite fallisce, la quote non è nemmeno presente: non ha senso valutare se implica il claim. Branch `unverified_spans_present` resta prioritario.
- Se entailment fallisce con `not_supported` o `contradicted`, la pubblicazione è semanticamente scorretta indipendentemente da quanto sia autorevole la fonte. Va trattato come block (o warning in MVP-0 con mock, vedi §7).
- Source Quality interviene quando i due assi precedenti sono passati: la fonte è strutturalmente adeguata?

Questa è una proposta operativa. La motivazione è che claim-quote semantic mismatch è un errore più grave (anti-allucinazione diretta) di una source weak/unknown (warning informativo). 8.8A-CODE non implementa il Gate; lo motiva qui per fissare il design.

---

## 5. Opzioni architetturali valutate

Quattro opzioni, ognuna valutata contro lo schema DB reale post-0008.

### 5.1 Opzione A — Nuova tabella `claim_entailment_checks`

Tabella append-only dedicata, granularità `(claim_ledger_entry_id, evidence_span_id)`, versionata e idempotente.

**Pro**:
- Separazione semantica pulita: entailment è una nuova dimensione, non un sotto-tipo di CVE-lite né di Source Quality.
- Storicizzabile: ogni rerun (anche con checker futuro reale) appende una nuova versione, mantenendo la storia.
- Auditabile: si può cercare "tutti i claim del task X che sono `not_supported` dall'evidence linkata" via JOIN diretta.
- Non muta `claim_ledger_entries`: rispetta l'invariante append-only stretto di 0004.
- Allinea il pattern con `source_quality_assessments` (0007), `verification_records` (0004), `published_answer_lifecycle_events` (0006), `source_loss_events`/`source_loss_propagation_records` (0006). Coerenza architetturale forte.
- Idempotenza naturale via partial UNIQUE indexes.

**Contro**:
- Richiede nuova migration (0009).
- Il Final Answer Gate dovrà consultare un nuovo strato, con SELECT aggiuntiva e nuova logica di policy. Il file `final_answer_gate.py` cresce di nuovo.

### 5.2 Opzione B — Estendere `verification_records` con `check_kind='entailment'`

Riusare la tabella `verification_records` (0004) aggiungendo un nuovo valore al CHECK di `check_kind`.

**Pro**:
- Nessuna nuova tabella, nessuna migration di schema strutturalmente nuova (solo estensione di un CHECK, analoga a quanto fatto per `coverage_gap_statements.kind` in 0008).
- Riusa l'UNIQUE `(claim_ledger_entry_id, check_kind, check_name)` per idempotenza.
- Il Final Answer Gate ha già una query verso `verification_records`.

**Contro decisivi**:
- **Confusione semantica**: `verification_records` oggi rappresenta CVE-lite e (futuro) NLI/judge a livello di "verifica testuale". Mescolare entailment lì spinge il significato del campo `outcome ∈ {pass, fail, inconclusive}` a coprire un codominio diverso. Un futuro lettore della tabella non saprebbe se `outcome='fail'` è "quote non presente nel chunk" o "quote non implica il claim".
- **Codomino di outcome inadeguato**: l'entailment ha cinque verdict (entailed, partially_supported, not_supported, contradicted, uncertain). Mappare cinque verdict su tre outcome perde informazione. Aggiungere altri valori al CHECK di `outcome` estende un campo storicamente diverso e impatta blocchi futuri (8.8C/D potrebbero a loro volta volere il proprio codominio).
- **FK non coerente**: `verification_records` ha FK su `claim_ledger_entries(id)` ma NON sull'`evidence_span_id`. L'entailment è per natura `(claim, evidence_span)`, e perderemmo questo legame strutturale.
- **Migration potenzialmente delicata**: il CHECK di `check_kind` è inline e va riprodotto in modo difensivo come in 0008.
- **Versionamento**: `verification_records` non ha `version_no`. L'idempotency-key dovrebbe vivere dentro `check_name` (es. `entailment_v1`, `entailment_v2`), che è uno spazio testuale arbitrario.

Scartata.

### 5.3 Opzione C — Estendere `claim_ledger_entries` con nuovi stati o metadata

Aggiungere `state='not_supported_by_quote'` (o simili) o un campo `entailment_metadata JSONB`.

**Pro**:
- Il Gate è più semplice: legge solo la latest entry.

**Contro decisivi**:
- **Viola l'invariante append-only del Claim Ledger** (0004 §1.4 di `PHASE_8_7_PLAN.md` e §3.5 dei vincoli MVP-0): un'entailment failure rilevata in un blocco successivo dovrebbe appendere una v(N+1) con stato negativo, che è un'azione semanticamente diversa da "supersede di un fact con uno nuovo". Confonde "stato del claim" con "relazione claim-evidence".
- **Cross-cutting concern**: lo stato del Claim Ledger riguarda il claim nel suo complesso, non la relazione di un singolo evidence_span. Un claim può essere entailed da uno span e not_supported da un altro span: con C non si può rappresentare entrambi i fatti.
- **Frizione con 8.8B/C/D**: ognuno di questi blocchi avrebbe a sua volta interesse a estendere il codominio di `state`, portando a inflazione di stati e a un Claim Ledger illeggibile.

Sconsigliata salvo motivazione fortissima. Scartata.

### 5.4 Opzione D — Solo `coverage_gap_statements`

Il Gate produce direttamente gap di kind `entailment_block` / `entailment_warning` senza tabella intermedia.

**Pro**:
- Minimo schema. Solo estensione di un CHECK in 0008 (per il `kind`).

**Contro decisivi**:
- **Niente audit storico del check**: il checker scrive nei `coverage_gap_statements` legati al draft v1. Se il draft viene rifatto (path futuro v2), il check va rieseguito da zero, perdendo la storia delle valutazioni precedenti.
- **Non osservabile prima del Gate**: un consumatore esterno (UI, eval, ricerca diagnostica) non può chiedere "qual è il verdict di entailment per la coppia (claim X, evidence_span Y)?" perché il dato esiste solo come effetto-laterale del Gate.
- **Non riusabile in API/Report futuri**: 8.8E (Anti-Hallucination Report API) sarebbe costretto a inferire l'entailment dai gap, perdendo dimensioni come `confidence` e `rationale`.
- **Confonde stato e segnalazione**: `coverage_gap_statements` è per natura il *risultato* di una decisione del Gate. La valutazione di entailment è un dato di input al Gate, non un suo output.

Scartata.

---

## 6. Raccomandazione

**Opzione A — nuova tabella `claim_entailment_checks` come migration 0009.**

Motivazione sintetica:
1. Allinea il pattern architetturale di Evidence-First: ogni asse anti-allucinazione vive in tabella propria, append-only, versionata, idempotente, indipendente dagli altri assi (CVE-lite in `verification_records`, source quality in `source_quality_assessments`, ora entailment in `claim_entailment_checks`).
2. Rispetta tutte le invarianti dichiarate (§4): non muta Claim Ledger, non sostituisce CVE-lite, non sostituisce Source Quality, è append-only.
3. Storicizzabile: quando arriverà un checker reale (LLM/NLI vero), la storia mock vs reale è interamente preservata.
4. Costi accettabili: una migration in più, una nuova SELECT nel Final Answer Gate, una nuova route read API futura. Tutti gli altri blocchi 8.8x potranno consumare la tabella senza ulteriori cambi schema.

La scelta è coerente con la motivazione tecnica del 8.7B (vedi `PHASE_8_7_PLAN.md §4`, Opzione A implementata): "lo strato in tabella propria offre la massima separazione semantica e auditabilità".

---

## 7. Schema DB candidato (NON una migration definitiva)

Questo è un **disegno preliminare** per discussione in 8.8A-CODE. La forma finale verrà ratificata in 8.8A-SCHEMA / 8.8A-CODE.

```sql
-- migrations/0009_claim_entailment_checks.sql  (CANDIDATO, non finale)

CREATE TABLE claim_entailment_checks (
  id                       UUID        PRIMARY KEY DEFAULT app_new_uuid(),
  tenant_id                UUID        NOT NULL REFERENCES tenants(id)            ON DELETE RESTRICT,
  project_id               UUID                 REFERENCES projects(id)           ON DELETE RESTRICT,
  task_id                  UUID        NOT NULL REFERENCES task_masters(id)       ON DELETE RESTRICT,

  -- Granularità: ogni check è per coppia (claim_ledger_entry, evidence_span).
  -- claim_logical_id ridondato per query ergonomiche e per consentire FK
  -- composita verso (id, claim_logical_id) di claim_ledger_entries (UNIQUE
  -- cle_id_logical_uq dichiarato in 0004).
  claim_logical_id         UUID        NOT NULL REFERENCES logical_claims(id)     ON DELETE RESTRICT,
  claim_ledger_entry_id    UUID        NOT NULL,
  evidence_span_id         UUID        NOT NULL REFERENCES evidence_spans(id)     ON DELETE RESTRICT,

  -- Versioning per coppia (claim_ledger_entry_id, evidence_span_id).
  version_no               INTEGER     NOT NULL,

  -- Verdict semantico (vedi §3.1).
  verdict                  TEXT        NOT NULL,

  -- Confidenza interna in [0.0, 1.0] o NULL se il checker non sa.
  confidence               DOUBLE PRECISION,

  -- Provenienza del checker.
  checker_name             TEXT        NOT NULL,
  checker_version          TEXT        NOT NULL,

  -- Policy che ha governato il giudizio.
  policy_name              TEXT        NOT NULL,
  policy_version           TEXT        NOT NULL,

  -- Idempotency-key per redelivery (vedi §7.2).
  idempotency_key          TEXT        NOT NULL,

  -- Rationale leggibile dall'operatore (breve, troncato se necessario).
  rationale                TEXT,

  -- Payload opaco per dati interni del checker (es. spans intermedi, scoring).
  payload                  JSONB       NOT NULL DEFAULT '{}'::jsonb,

  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- CHECK codominio verdict
  CONSTRAINT cec_verdict_chk CHECK (verdict IN (
    'entailed',
    'partially_supported',
    'not_supported',
    'contradicted',
    'uncertain'
  )),

  CONSTRAINT cec_version_no_chk CHECK (version_no >= 1),

  CONSTRAINT cec_confidence_range CHECK (
    confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)
  ),

  -- FK composita verso claim_ledger_entries (id, claim_logical_id) — UNIQUE
  -- cle_id_logical_uq esiste già in 0004. Garantisce DB-level che la
  -- coppia (claim_ledger_entry_id, claim_logical_id) sia coerente.
  CONSTRAINT cec_entry_logical_consistency
    FOREIGN KEY (claim_ledger_entry_id, claim_logical_id)
    REFERENCES claim_ledger_entries(id, claim_logical_id)
);

-- UNIQUE per versioning: una sola riga per (entry, span, version_no).
CREATE UNIQUE INDEX cec_entry_span_version_uq
  ON claim_entailment_checks (claim_ledger_entry_id, evidence_span_id, version_no);

-- UNIQUE per idempotency: redelivery con stessa key e stessa coppia non
-- duplica.
CREATE UNIQUE INDEX cec_entry_span_idem_uq
  ON claim_entailment_checks (claim_ledger_entry_id, evidence_span_id, idempotency_key);

-- Indici di lookup.
CREATE INDEX cec_task_idx              ON claim_entailment_checks (task_id);
CREATE INDEX cec_claim_logical_idx     ON claim_entailment_checks (claim_logical_id);
CREATE INDEX cec_evidence_span_idx     ON claim_entailment_checks (evidence_span_id);
CREATE INDEX cec_verdict_idx           ON claim_entailment_checks (verdict);

-- Append-only enforcement via trigger comune.
CREATE TRIGGER claim_entailment_checks_append_only
BEFORE UPDATE OR DELETE ON claim_entailment_checks
FOR EACH ROW EXECUTE FUNCTION reject_modify_append_only();
```

### 7.1 Note di disegno

- **`tenant_id` NOT NULL, `project_id` NULLABLE**: stesso pattern di `source_quality_assessments`. Permette check su artefatti senza progetto (improbabile in MVP-0 ma coerente).
- **`task_id` NOT NULL**: l'entailment è sempre nel contesto di un task. Coerente con la pipeline `task.created`.
- **`claim_logical_id` ridondato**: come in `final_answer_span_claim_links` (0005), per FK composita su `claim_ledger_entries(id, claim_logical_id)`. Senza questo, una riga potrebbe puntare a un entry inesistente o a un entry di un altro logical_claim.
- **`evidence_span_id` come FK diretta**: a differenza di Source Quality (che ha XOR su tre target), l'entailment ha sempre granularità claim-evidence, mai claim-chunk o claim-document. Codifica esplicitamente questa semantica.
- **`verdict` con CHECK enum**: parallelo a `source_quality_assessments` (vedi 0007). Cinque valori del §3.1.
- **`confidence DOUBLE PRECISION` con CHECK [0,1]**: parallelo a `source_quality_assessments.confidence`. NULL per checker che non sanno.
- **`checker_name`, `checker_version`**: provenance. Parallelo a `evaluator_name`/`evaluator_version` di 0007.
- **`policy_name`, `policy_version`**: identità della policy. Parallelo a 0007. Il mock MVP-0 userà `mvp0_mock_entailment` v0.1.0.
- **`idempotency_key`**: testo opaco. Pattern proposto in §7.2.
- **`rationale TEXT`** opzionale: breve spiegazione human-readable. Non serve a logiche di gate (che leggono `verdict`), ma utile in UI/eval. Va troncato lato servizio se troppo lungo (decisione lasciata a 8.8A-CODE).
- **`payload JSONB`** opaco per dati interni del checker: scoring intermedi, alignment tokens, hash del prompt, ecc.
- **FK composita `cec_entry_logical_consistency`**: enforced a DB. Stesso pattern di `cel_entry_logical_consistency` (0004) e `fasc_entry_logical_consistency` (0005). Senza questo, una riga potrebbe scattare per (entry_id di un claim, claim_logical_id di un altro claim).

### 7.2 Versioning, idempotency, partial UNIQUE — decisione

Diversamente da `source_quality_assessments` (che usa partial UNIQUE indexes per via dello XOR a tre target), `claim_entailment_checks` ha una sola dimensione di target (`(claim_ledger_entry_id, evidence_span_id)`): possiamo usare UNIQUE indexes "pieni" (non partial) come mostrato in `cec_entry_span_version_uq` e `cec_entry_span_idem_uq`. Pattern più semplice di 0007, applicabile perché non c'è XOR.

**Format di idempotency_key proposto** per il mock MVP-0:
```
task:{task_id}:entry:{claim_ledger_entry_id}:span:{evidence_span_id}:v1
```
Il `:v1` è un version-tag globale del formato. Bumparlo (a `:v2`) forzerebbe nuova append su redelivery, utile in caso di rilascio di un checker reale che vuole "rivalutare" il corpus storico.

### 7.3 FK opzionali da NON aggiungere ora

Per minimizzare la superficie e mantenere il disegno simmetrico a 0007:

- **FK verso `final_answer_spans`**: NO. L'entailment è una verifica su (claim, evidence_span). I `final_answer_spans` sono un'entità lato draft, derivata. Il Gate è il punto giusto per fare il join.
- **`session_id`**: NO. Non rilevante in MVP-0.
- **`source_quality_assessment_id` come correlazione**: NO. Cross-asse non va modellato a DB; il Gate compone gli assi.

Vedi §11.

### 7.4 Numerazione migration

**Proposta: 0009** per `claim_entailment_checks`. La retention futura distruttiva slitta a 0010 o successivo. Coerente con la nota già presente in `PROJECT_STATE.md`.

---

## 8. Pipeline integration point candidato

Il prompt operativo elenca quattro possibilità. Le analizziamo:

### 8.1 Possibilità 1 — Dopo CVE-lite e prima di `task.source_quality_assessed`

Lo step entailment girerebbe dentro `_run_8_3_extract_and_verify`, AFTER `task.cve_lite_completed` e BEFORE `task.analyzed_partial`.

**Pro**:
- Logicamente sensato: entailment opera sulle latest verified_fact, e CVE-lite ha appena prodotto la v2 verified_fact. La pipeline ha già tutti gli ingredienti.
- Composizione con 8.7E è simmetrica.

**Contro**:
- Inserire un audit (es. `task.entailment_checked`) fra `task.cve_lite_completed` e `task.analyzed_partial` mescola due fasi logicamente diverse (8.3 verification text-level vs 8.8A semantic-level).
- `task.analyzed_partial` ha sempre rappresentato "ho finito la fase di analisi base e ora ho un payload pronto per la composizione". Spostare entailment prima rompe questa lettura.

### 8.2 Possibilità 2 — Dopo `task.source_quality_assessed` e prima di `task.compiling`

Lo step entailment è un nuovo step della pipeline, parallelo a 8.7E ma successivo.

**Pro**:
- Simmetria forte con 8.7E: SAVEPOINT-wrapped, audit aggregato `task.entailment_checked`, idempotente, non blocca 8.4 se fallisce.
- L'integration code in `task_created.py` riusa il pattern già rodato di `_run_8_7_source_quality`.
- Naming-friendly: la fase 8.8A è un nuovo step indipendente, e va a un posto suo nel chain.
- Audit chain leggibile: `analyzed_partial → source_quality_assessed → entailment_checked → compiling`.

**Contro**:
- Sequenza tra 8.7E e 8.8A è arbitraria: nessuno dei due dipende dall'altro. Va però fissata in modo deterministico.

### 8.3 Possibilità 3 — Dopo `task.analyzed_partial` e prima di `task.compiling`, prima di Source Quality

Equivalente alla 2 ma con ordine invertito (entailment prima di source quality).

**Pro/Contro**:
- Equivalente alla 2 in termini di pattern. La scelta dell'ordine è cosmetica e va motivata.

### 8.4 Possibilità 4 — Dentro Final Answer Gate, lazy/read-time

Il Gate, quando consulta un span verified-backed, esegue entailment al volo.

**Pro**:
- Niente nuovo audit, niente nuovo step.

**Contro decisivi**:
- Il Gate diventa lento e non più read-only sul DB applicativo: lazy compute al volo significa INSERT su `claim_entailment_checks` durante la fase Gate. Frizione con la disciplina "Gate read-only" di 8.7G.
- Non risolve il problema di redelivery: ogni rerun del Gate ricalcola.
- Audit povero: nessun audit task-level dedicato.

Scartata.

### 8.5 Raccomandazione

**Possibilità 2** — Inserire un nuovo step `_run_8_8_entailment` in `task_created.py`, AFTER l'esistente `_run_8_7_source_quality`, BEFORE `_advance_to_compiling`.

Sequenza audit risultante (approved scenario, con futuro checker mock 8.8A):
```
... task.cve_lite_completed
  -> task.analyzed_partial
  -> task.source_quality_assessed         (8.7E)
  -> task.entailment_checked              (8.8A — NUOVO)
  -> task.compiling
  -> task.draft_compiled
  -> task.final_gate_started
  -> task.final_gate_completed
  -> task.published / task.publication_held
```

Vincoli operativi:
- **SAVEPOINT obbligatorio** (`conn.begin_nested()`): un fallimento del checker non aborta la transazione esterna e non blocca 8.4. Mirror di 8.7E.
- **Idempotente**: redelivery non riemette `task.entailment_checked`. Resume da `compiling` skippa lo step (mirror di 8.7E con guard `current_status == 'analyzing'` → ramo fresh-run).
- **Audit aggregato unico**: `task.entailment_checked` emesso una sola volta per task fresh-run, con `status='completed'|'failed'` e dict `counts` (numero coppie entry × span valutate, per ognuna `assessed/already_assessed/error/...`).
- **MVP-0 non-blocking sul mock**: il checker mock NON deve causare hold sistemico del pipeline. Vedi §10.
- **No mutazione di stati esistenti**: lo step NON tocca `task_masters.status`, NON tocca `claim_ledger_entries`, NON tocca `final_gate_reports`, NON tocca `source_quality_assessments`.

L'ordine 8.7E → 8.8A è motivato da:
- "valuta prima la fonte, poi la coerenza claim↔quote": lettura naturale per un operatore.
- Coerenza con la priorità del Gate proposta in §4.1, che è CVE-lite > Entailment > Source Quality: la priorità del Gate non deve coincidere con l'ordine di scrittura nella pipeline, quindi la scelta è puramente cosmetica e si privilegia la simmetria con 8.7E.

---

## 9. Policy Gate candidata

**8.8A-PRE non implementa il Gate.** Definisce solo come il Final Answer Gate userà l'entailment in futuro (8.8A-CODE o 8.8A-GATE, da decidere se in due sotto-blocchi).

### 9.1 Politica proposta MVP-0

| Verdict latest sul (claim, evidence_span) | Comportamento Gate |
|---|---|
| `entailed` | clean: non emette gap entailment per quel span. |
| `partially_supported` | **warning** in MVP-0. Emette `coverage_gap_statements` kind `entailment_warning`, severity `warn`. Decision NON cambia. |
| `not_supported` | **block** se policy "P-block-strict" attiva, **warning** se policy "P-block-lazy" (MVP-0 default — vedi §9.2). |
| `contradicted` | **block** sempre. Emette `entailment_block`, severity `block`. |
| `uncertain` | **warning** in MVP-0. |
| latest mancante | **warning** in MVP-0 (`entailment_missing_check`). Mirror del comportamento Source Quality per assessment mancante (8.7G). |

Aggregazione tra più evidence_span dello stesso span: **worst-on-block, any-on-warn**. Pattern già rodato in 8.7G.

### 9.2 Calibrazione MVP-0: P-block-strict vs P-block-lazy

Decisione operativa importante. Il prompt avverte: "non bloccare tutto il sistema per missing/uncertain se il checker mock non è maturo, salvo motivazione forte".

**P-block-strict**: `not_supported` → block. Realistico per checker reale (NLI / LLM judge), ma con un checker mock euristico (vedi §10) rischia overblocking sistemico, perché ogni claim che ha tipi di numeri leggermente diversi dalla quote finirebbe `not_supported` per pura euristica.

**P-block-lazy**: `not_supported` → warning. Conservativo. Solo `contradicted` blocca. Riduce overblocking; lascia visibili tutti i `not_supported` come `entailment_warning` nei coverage_gap_statements.

**Raccomandazione**: **P-block-lazy in MVP-0** con il mock. La policy è versionata (`mvp0_entailment_gate_policy` v0.1.0); un futuro bump a v0.2.0 attiverà P-block-strict quando un checker reale (LLM/NLI) sarà disponibile (8.9 o successive). Motivazione esplicita: il mock euristico non ha la maturità semantica per giustificare un block.

`contradicted` resta block anche in MVP-0 perché un checker mock che riesce a dire "contradicted" lo fa solo con segnali forti (es. numeri opposti esplicitamente), e quei casi sono effettivamente da bloccare.

### 9.3 Nuovi `final_gate_reports.reason_code` candidati

- `entailment_block` (per il caso `contradicted` o `not_supported` in P-block-strict),
- `all_spans_verified_with_warnings` riusato per il caso entailment-warning (no block-source-quality, no block-entailment): il `reason_code` esistente già copre semanticamente "approvato con warning", quindi la calibrazione dei nuovi reason si fa solo sui block paths,
- in alternativa, codici più specifici come `all_spans_verified_with_entailment_warnings` se si vuole esporre via API la differenza tra source-quality-warning e entailment-warning. **Raccomandato**: NON introdurli ora. Il `reason_code` resta `all_spans_verified_with_warnings`; il dettaglio per asse vive nei `details.reasons` del singolo gap.

### 9.4 Nuovi `coverage_gap_statements.kind`

Servirà una **nuova migration** sul CHECK di `kind` (estensione robusta come in 0008). Candidati:

- `entailment_block` (severity='block'),
- `entailment_warning` (severity='warn').

Pattern identico al 0008.

`gap_key` deterministico, idempotente:
- `f'span:{final_answer_span_id}:entailment_block'`
- `f'span:{final_answer_span_id}:entailment_warning'`

Compatibile con l'UNIQUE composito `(draft_final_answer_id, kind, gap_key)` di 0005.

### 9.5 Priorità di emissione nel Gate

Quando il Gate vede sia un block di source quality sia un block di entailment sullo stesso span, **entrambi i gap devono essere emessi** (per audit completo), ma il `final_gate_reports.reason_code` deve seguire una priorità deterministica. Proposta:

```
unverified_spans_present  >  entailment_block  >  source_quality_block
```

Motivazione:
- `unverified_spans_present` è il fallback più antico e indica un buco strutturale.
- `entailment_block` è "il claim non è semanticamente sostenuto": più diretto di un block di qualità della fonte.
- `source_quality_block` è "la fonte è inadeguata": può coesistere con entailment.

Quando più block sono attivi sullo stesso draft, il Gate sceglie il `reason_code` "più alto in priorità" ma emette tutti i gap, in modo che `details.reasons` rifletta l'intera situazione.

### 9.6 Numerazione migration per il CHECK extension

Se 0009 viene usato per `claim_entailment_checks`, **0010** servirà per estendere `coverage_gap_statements.kind` con `entailment_block` / `entailment_warning`. La retention futura distruttiva slitta a 0011 o successivo. È un costo accettabile.

Alternativa: una sola migration 0009 che crea la tabella E estende il CHECK. Sconsigliato perché mescola due cambi concettualmente distinti. Meglio due migration sequenziali (0009 schema, 0010 gap kind extension), 8.8A-CODE le farà a coppia.

---

## 10. Mock checker MVP-0

### 10.1 Vincoli

- **Niente LLM reale.**
- **Niente web search.**
- **Niente embedding o NLI model.**
- **Niente provider AI esterno.** `PROVIDERS_ENABLED=mock` resta invariato.
- **Deterministico** per stessa coppia (claim, quote).
- **Testabile** unit + realistic flow.
- **Esplicito** sulla propria natura mock: ogni riga scritta porta `payload.mock = true` e `payload.semantic_warning = "entailment_mock_is_not_real_semantic_judgement"`. Mirror del 8.7D.

### 10.2 Euristica proposta per il mock

Tre regole deterministiche, applicate in ordine. Su match, il verdict è fissato; nessuna regola viene applicata dopo il primo match.

#### Regola 1 — Containment normalizzato → `entailed`

Normalizza claim e quote: lowercase, whitespace collapsed, punteggiatura periferica rimossa. Se la versione normalizzata del claim è una substring della versione normalizzata della quote, oppure se i due testi sono uguali post-normalizzazione, **verdict = `entailed`**, `confidence = 0.7`.

#### Regola 2 — Numbers mismatch → `not_supported`

Estrai con regex i token numerici dal claim e dalla quote (intero o decimale, con eventuale `%`). Se il claim contiene ALMENO un numero, e quel numero NON compare nella quote, **verdict = `not_supported`**, `confidence = 0.6`, `rationale = "claim_numbers_not_present_in_quote"`. Esempio: claim `"Revenue grew by 41%"` con quote `"Revenue grew by 37 percent"` → 41 non è in quote → `not_supported`.

#### Regola 3 — Numbers opposite-direction → `uncertain` (non `contradicted`)

Se sia claim sia quote contengono numeri e contengono parole-chiave di direzione opposta (es. claim ha "grew/increased/rose" mentre quote ha "declined/dropped/fell", o viceversa), il mock **non** ha la maturità per dire `contradicted`. **verdict = `uncertain`**, `confidence = 0.4`, `rationale = "opposite_direction_keywords_detected_but_mock_cannot_assert_contradiction"`.

#### Default

Se nessuna delle tre regole si applica, **verdict = `uncertain`**, `confidence = 0.5`. Rationale: `"mock_heuristic_no_signal"`.

### 10.3 Cosa il mock NON è

- **Non riconosce paraphrasing reale**: due frasi equivalenti scritte con parole diverse → uncertain.
- **Non rileva implicazioni logiche complesse**: claim "tutte le X" supportato da quote "solo alcune X" non è rilevabile.
- **Non rileva contraddizioni semantiche**: solo segnali sintattici molto espliciti (direction keywords su numeri).
- **Non assegna verdict `contradicted`**: il mock è troppo debole per asserire contraddizione. Solo `not_supported` o `uncertain`. Questa è una scelta esplicita per non produrre falsi positivi di contraddizione che bloccherebbero pubblicazioni legittime.

### 10.4 Identità del mock

- `CHECKER_NAME = "mvp0_mock_entailment_checker"`
- `CHECKER_VERSION = "0.1.0"`
- `DEFAULT_POLICY_NAME = "mvp0_mock_entailment"`
- `DEFAULT_POLICY_VERSION = "0.1.0"`

### 10.5 Comportamento atteso a regime pipeline

Con il mock attuale:
- Claim che CVE-lite ha verificato pass (`verified_fact`) → la quote è già substring del chunk per costruzione → Regola 1 (containment normalizzato) molto spesso scatta → `verdict='entailed'`.
- Quando l'extractor estrae sentence con numeri diversi dalla quote (caso teorico, perché in 8.3 l'extractor crea raw_claim DALLA stessa quote, quindi i numeri coincidono di default) → `not_supported`.

**Conseguenza pratica**: con la pipeline 8.3 attuale, il mock entailment produrrà quasi sempre `entailed`. Il Branch entailment-block del Gate sarà raramente attivato in produzione (analogo a Branch C' di Source Quality oggi). Sarà attivabile nel realistic flow 8.8A-CODE solo via stub dell'orchestrator (pattern 8.7H), oppure quando un Citation-to-Claim Validator (8.8B) o un human review introdurranno claim disaccoppiati dalla quote.

---

## 11. API / observability futura (read-only)

Endpoint **da progettare per blocchi successivi**, non da implementare ora.

### 11.1 Necessari subito (in 8.8A-CODE o blocco read-API immediatamente successivo)

- `GET /api/v1/claims/{logical_claim_id}/entailment-checks`
  Lista cronologica delle valutazioni di entailment per il logical claim, con tutti i (entry, span, version_no, verdict) collegati. Permette diagnostica claim-centric.
- `GET /api/v1/tasks/{task_id}/entailment`
  Aggregato task-level: per ogni span del task, l'ultimo verdict per ogni evidence_span supportante. Summary con counts per verdict. Mirror di `/api/v1/tasks/{task_id}/source-quality`.

### 11.2 Non necessari subito (rinviabili)

- `GET /api/v1/evidence-spans/{evidence_span_id}/entailment-checks`
  Utile per indagini evidence-centric ma non bloccante in MVP-0.
- Aggregato esplicito nel `/final-gate-report`: già coperto via i `coverage_gap_statements` di kind `entailment_*`. Non serve un campo dedicato.

### 11.3 Vincoli sulle nuove read API

- Read-only end-to-end (mirror 8.6, 8.7F).
- Nessuna RBAC redaction in MVP-0 (debito noto, ereditato da 8.6 / 8.7F).
- Pagination via `limit` (no cursor).
- JSONB `payload` verbatim.

---

## 12. Test plan futuro (per 8.8A-CODE)

Test che 8.8A-CODE dovrà scrivere. Lista di intenti, non di file specifici.

### 12.1 Migration tests (`tests/test_migration_0009_*`)

- CHECK enum verdict accetta i cinque valori validi, rifiuta valori arbitrari.
- CHECK `confidence` rifiuta `< 0` e `> 1`.
- CHECK `version_no >= 1`.
- Trigger append-only su `claim_entailment_checks`: UPDATE e DELETE rifiutati.
- FK composita `cec_entry_logical_consistency` rifiuta coppie (entry_id, claim_logical_id) inconsistenti.
- UNIQUE `cec_entry_span_version_uq` rifiuta v1 duplicate.
- UNIQUE `cec_entry_span_idem_uq` rifiuta stessa idempotency-key su stessa coppia.
- FK `ON DELETE RESTRICT` su evidence_spans, claim_logical_id, tenant_id, project_id, task_id.

Se 0010 estende `coverage_gap_statements.kind`:
- `kind ∈ {entailment_block, entailment_warning}` accettato.
- vecchi kind ancora accettati (regression).

### 12.2 Service tests (`apps/worker/tests/test_entailment_checker_service.py`)

- Path `entailed` su containment normalizzato.
- Path `not_supported` su numbers mismatch.
- Path `uncertain` su default + su opposite-direction.
- Idempotenza per `(claim_ledger_entry_id, evidence_span_id, idempotency_key)`: short-circuit `STATUS_ALREADY_CHECKED`.
- Race resolution via SAVEPOINT + recovery SELECT su IntegrityError (pattern di 8.7D).
- Canonical scope: `tenant_id`/`project_id` letti dal target, non dal caller.
- Nessuna mutazione su `claim_ledger_entries`, `final_gate_reports`, `source_quality_assessments`, `verification_records`, `audit_records`.

### 12.3 Orchestrator tests (`apps/worker/tests/test_entailment_orchestrator.py`)

- Computa DISTINCT `(claim_ledger_entry_id, evidence_span_id)` derivati da `claim_evidence_links` dei verified_fact del task.
- Idempotenza redelivery.
- Counts dict completo.
- Task non esistente → status='not_found' con counts azzerati.

### 12.4 Worker integration tests (`apps/worker/tests/test_task_created_entailment_step.py`)

Mirror dei 4 scenari di `test_task_created_source_quality_step.py`:
- emissione audit `task.entailment_checked` dopo `task.source_quality_assessed` e prima di `task.compiling`;
- popolamento `claim_entailment_checks` per le coppie del task;
- resume da `compiling` non re-emette l'audit;
- failure stub → savepoint rollback → audit `failed` → 8.4 prosegue.

### 12.5 Gate tests (`apps/worker/tests/test_final_answer_gate_entailment.py`)

Mirror dei 13 scenari di `test_final_answer_gate_source_quality.py`:
- `entailed` → no gap, decision invariata,
- `contradicted` → block path,
- `not_supported` con P-block-lazy → warning,
- `not_supported` con P-block-strict → block (test con policy bumped),
- `uncertain` → warning,
- missing latest → warning,
- multi-evidence worst-on-block,
- latest version wins (v1 entailed, v2 contradicted → block),
- **priorità CVE-lite > Entailment**: se uno span non è verified-backed, Gate ignora entailment e produce `unverified_spans_present`,
- **priorità Entailment > Source Quality**: se uno span ha entailment-block e source-quality-warning, il reason_code è `entailment_block` (vedi §9.5),
- idempotenza redelivery.

### 12.6 Realistic flow tests (`tests/test_phase_8_8_entailment_flow.py`)

Mirror del 8.7H realistic flow:
- **Warning flow**: pipeline normale con mock → `verdict='entailed'` predominante (vedi §10.5) → published.
- **Block flow**: stub dell'orchestrator inserisce `verdict='contradicted'` su uno span → Gate rejected con `reason_code='entailment_block'` → `task.publication_held`.

---

## 13. Rischi residui

Rischi specifici dell'introduzione di 8.8A, da considerare prima di 8.8A-CODE.

### 13.1 Rischi semantici

- **Entailment mock non è semantica reale.** Tre regole regex-based non si avvicinano a NLI vero. Il mock può sbagliare in entrambe le direzioni (false `entailed` su containment apparente, false `not_supported` su numeri non rilevati). Documentazione esplicita necessaria.
- **`payload.mock = true` deve essere onorato** dai consumatori (UI, eval) come segnale che il verdict NON è da prendere come autorevole.
- **"entailed quote-presente" può comunque essere falsa-affermazione**: la quote può dire "X" e la quote essere essa stessa una citazione fuori contesto della fonte. L'entailment dice (claim, quote); non interferisce con la verità di quote vs mondo.

### 13.2 Rischi architetturali

- **Crescita del Final Answer Gate**. Il file `final_answer_gate.py` ha già 800+ righe post-8.7G, con cinque branch decisionali. Aggiungere entailment porta a sei branch e a una doppia LATERAL JOIN. Va valutato se splittare in `final_answer_gate.py` + `final_answer_gate_policy.py` (raccomandato in 8.8A-CODE per leggibilità).
- **Audit chain inflation**: ogni nuovo step aggiunge un evento. Da 14 eventi worker-side (post-8.7E) si passa a 15. Accettabile.
- **Two-migration cost (0009 + 0010)**: numeratura conseguente per retention. Costo amministrativo, non tecnico.
- **Priorità Gate**: stabilire CVE-lite > Entailment > Source Quality è una scelta motivata in §4.1, ma resta da testare esplicitamente (vedi §12.5). Va presa come invariante da preservare in tutti i refactor futuri.

### 13.3 Rischi operativi

- **Overblocking**: P-block-strict prematuro renderebbe MVP-0 inutilizzabile (ogni task con qualche frase non perfettamente sovrapponibile alla quote finirebbe bloccato). Mitigazione: P-block-lazy in MVP-0, documentato come scelta esplicita e versionato (`mvp0_entailment_gate_policy` v0.1.0).
- **Underblocking**: P-block-lazy con mock means `not_supported` non blocca. Documentato: questa è una scelta consapevole MVP-0; il rinforzo richiede checker reale (8.9 o successive).
- **Confusione consumatori esterni**: il nuovo `reason_code='entailment_block'` può rompere consumer che oggi gestiscono solo `all_spans_verified`, `no_verified_claims`, `unverified_spans_present`, `source_quality_block`, `all_spans_verified_with_warnings`. Mitigazione: documentare in `PROJECT_STATE.md` post-8.8A.
- **Backfill per task pre-8.8A**: nessun backfill è previsto. Task processati prima di 8.8A non avranno righe in `claim_entailment_checks`; il Gate dovrà gestirlo come "latest assessment mancante" → warning, mirror del comportamento Source Quality post-8.7E.

### 13.4 Rischi documentali

- **"entailed mock" continua a non significare "claim vero".** Va martellato in `PROJECT_STATE.md` post-8.8A e nei docs futuri.
- **Una fonte citata non implica un claim vero, e una quote presente non implica che la quote sostenga il claim.** Disclaimer da estendere nel README post-8.8A.

### 13.5 Rischi ereditati

- Branch C' (`source_quality_block`) resta dormiente con mock attuale (8.7G/H).
- `coverage_gap_statements` senza trigger append-only (debito 8.7G).
- RBAC/redaction non implementata su payload JSONB esposti (debito 8.6/8.7F).
- Retention distruttiva non implementata (debito storico).

Tutti questi rischi sono ereditati invariati. 8.8A non li risolve né li peggiora.

---

## 14. Decisione finale

### 14.1 Architettura raccomandata

- **Opzione A** (vedi §6): nuova tabella `claim_entailment_checks` append-only, granularità `(claim_ledger_entry_id, evidence_span_id)`, versionata e idempotente. Migration `0009_claim_entailment_checks.sql`.
- **Pipeline integration point**: nuovo step `_run_8_8_entailment` in `apps/worker/app/consumers/task_created.py`, dopo `_run_8_7_source_quality` e prima di `_advance_to_compiling`. SAVEPOINT-wrapped, audit aggregato `task.entailment_checked`, idempotente, non-blocking sul mock.
- **Policy Gate**: estendere `apps/worker/app/services/final_answer_gate.py` per consumare `claim_entailment_checks`. Policy MVP-0 = **P-block-lazy** (solo `contradicted` blocca; `not_supported`/`uncertain`/`partially_supported`/missing → warning). Priorità `unverified_spans_present > entailment_block > source_quality_block`.
- **Coverage gap kinds**: nuova migration `0010_coverage_gap_entailment.sql` per estendere il CHECK di `coverage_gap_statements.kind` con `entailment_block` e `entailment_warning` (severity rispettive `block` e `warn`).
- **Mock checker**: deterministico, tre regole sintattiche (vedi §10.2). Identità: `mvp0_mock_entailment_checker` v0.1.0, policy `mvp0_mock_entailment` v0.1.0.
- **Read API**: due endpoint nuovi (`/claims/{logical_id}/entailment-checks`, `/tasks/{task_id}/entailment`), read-only, da implementare nel sotto-blocco read API di 8.8A.
- **Realistic flow test**: due test end-to-end (warning + block), il block flow attivato via stub dell'orchestrator (mirror 8.7H).

### 14.2 File da NON modificare

In 8.8A-CODE:
- Migrations 0001–0008.
- `apps/worker/app/services/cve_lite.py` (CVE-lite invariata).
- `apps/worker/app/services/extractor.py` (extractor invariato).
- `apps/worker/app/services/compiler.py` (compiler invariato).
- `apps/worker/app/services/source_quality_evaluator.py` (Source Quality invariata).
- `apps/worker/app/services/source_quality_orchestrator.py` (invariata).
- `apps/api/app/routes/source_quality.py` (invariate).
- Schema shared per Source Quality (invariati). Nuovi shared schemas saranno aggiunti SOLO per entailment.

### 14.3 File da creare nei blocchi successivi della fase 8.8A (anticipazione)

Questi file non vanno necessariamente creati nello stesso blocco. La raccomandazione operativa è procedere per sotto-blocchi: 8.8A-SCHEMA, poi 8.8A-CODE, poi 8.8A-READ, poi 8.8A-FLOW, poi 8.8A-DOC.

- `migrations/0009_claim_entailment_checks.sql`
- `migrations/0010_coverage_gap_entailment.sql`
- `packages/shared/evidencefirst_shared/schemas.py` — esteso con `CLAIM_ENTAILMENT_VERDICT_VALUES`, `ClaimEntailmentVerdict` Literal alias, `ClaimEntailmentCheckRead`.
- `apps/worker/app/services/claim_entailment_checker.py` (mock checker).
- `apps/worker/app/services/claim_entailment_orchestrator.py` (orchestrator).
- `apps/worker/app/consumers/task_created.py` — esteso con `_run_8_8_entailment`.
- `apps/worker/app/services/final_answer_gate.py` — esteso con il branch entailment.
- `apps/api/app/routes/entailment.py` — read API.
- `apps/api/app/main.py` — registrazione router.
- Tutti i test (vedi §12).
- Aggiornamento `PROJECT_STATE.md`, `README.md`, `PHASE_8_8_PLAN.md` (nuovo).

### 14.4 Vincoli sempre validi

- Nessun provider AI reale. `PROVIDERS_ENABLED=mock`. `MAX_COST_PER_TASK=0`.
- Closed Corpus only.
- SQLAlchemy 2.0 Core. Query SQL con bound params.
- Append-only enforced a DB.
- Idempotenza redelivery via UNIQUE indexes.
- Test rerun-safe con UUID/hash unici per invocazione.

---

## 15. Riepilogo deliverable

| Item | Stato |
|---|---|
| `PHASE_8_8A_PRE.md` | questo documento |
| nuove migration | non scritte (8.8A-CODE) |
| nuovo service mock | non scritto (8.8A-CODE) |
| nuovo orchestrator | non scritto (8.8A-CODE) |
| integration in `task_created.py` | non scritta (8.8A-CODE) |
| extension del Gate | non scritta (8.8A-CODE) |
| nuove read API | non scritte (8.8A-CODE) |
| test (migration / service / orchestrator / worker / Gate / realistic) | non scritti (8.8A-CODE) |
| documentazione `PROJECT_STATE.md` / `README.md` / `PHASE_8_8_PLAN.md` | non aggiornata (8.8A-DOC) |

---

FILE_COMPLETATI (8.8A-PRE)
- `PHASE_8_8A_PRE.md`

FILE_NON_MODIFICATI
- `migrations/*`
- `apps/*`
- `packages/*`
- `tests/*`
- `README.md`
- `PROJECT_STATE.md`
- `PHASE_8_7_PLAN.md`

FILE_DA_FARE_PROSSIMO_BLOCCO
- **8.8A-SCHEMA** (raccomandato come sotto-blocco separato di 8.8A-CODE): `migrations/0009_claim_entailment_checks.sql` + test migration corrispondenti, prima del codice applicativo. La separazione schema-vs-codice è coerente con l'approccio 8.7B → 8.7D → 8.7E → 8.7F → 8.7G adottato per Source Quality e riduce il rischio di mismatch shared schemas / DB.
- **8.8A-CODE** (servizi worker + integrazione consumer + estensione Gate + migration 0010): da affrontare DOPO 8.8A-SCHEMA.
- **8.8A-READ** (read API + nuovo file `PHASE_8_8_PLAN.md`): da affrontare DOPO 8.8A-CODE.
- **8.8A-FLOW** (realistic flow test end-to-end mirror 8.7H): da affrontare DOPO 8.8A-READ.
- **8.8A-DOC** (aggiornamento `PROJECT_STATE.md` / `README.md` / `PHASE_8_8_PLAN.md` per chiudere 8.8A): finalizzazione della sotto-fase.

RISCHI_RESIDUI
- L'entailment mock NON è semantica reale: tre regole sintattiche su containment + numeri non si avvicinano a NLI vero. `payload.mock = true` da onorare in tutti i consumatori.
- Senza LLM/embedding/NLI reale, il checker resta euristico; un futuro checker reale (8.9 o successive) richiederà bump di policy_version e probabile re-run del corpus storico.
- La policy Gate dovrà evitare overblocking sistemico: P-block-lazy in MVP-0 è la scelta esplicita (solo `contradicted` blocca, gli altri verdict producono warning).
- Va preservata la priorità **CVE-lite > Entailment > Source Quality** nel Gate. Invariante esplicitamente da testare in 8.8A-CODE (vedi §12.5).
- Branch entailment-block con mock sarà raramente attivato in produzione (mirror del Branch C' di 8.7G): attivabile via stub orchestrator nel realistic flow (pattern 8.7H).
- Numerazione migration: 0009 (`claim_entailment_checks`) + 0010 (`coverage_gap_entailment`) → retention futura slitta a 0011 o successivo. Coerente con quanto già rinviato in 8.7G/H.
- Una quote presente non implica che la quote sostenga il claim, e una fonte citata non implica un claim vero. Disclaimer da preservare in tutti i docs.
- Tutti i rischi ereditati da 8.7G/H restano invariati: no Contradiction Detector reale, no Final Answer Sentence Gate, no Anti-Hallucination Report API, no External Verification, RBAC/redaction non implementata, retention distruttiva non implementata, append-only trigger su `coverage_gap_statements` mancante, payload JSONB esposti verbatim.
