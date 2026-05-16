# PHASE_8_8A_GATE_PRE — Claim Entailment Gate Policy (analisi decisionale pre-codice)

Documento **decisionale e di piano** per la sotto-fase 8.8A-GATE del Claim Entailment Checker. Questo blocco è **solo analisi e progettazione**: non scrive codice applicativo, non scrive migration, non scrive test implementativi, non modifica API, non aggiorna `README.md` o `PROJECT_STATE.md`. Il solo deliverable è questo documento.

**Commit di riferimento implicito**: stato post-8.8A-WORKER al main attuale `357ee654332bff1cc952422c99f6d9123c5a024d` ("Integrate claim entailment into task pipeline"). Lo stato tecnico di riferimento è quello descritto in `PROJECT_STATE.md` (post-8.7H, fase 8.7 chiusa) ESTESO dai blocchi 8.8A-PRE / SCHEMA / SHARED / SERVICE / ORCHESTRATOR / WORKER già completati.

**Promessa anti-allucinazione (invariata, ribadita).** La piattaforma non promette di eliminare le allucinazioni in senso assoluto. La promessa corretta resta: **il sistema è progettato per impedire che claim fattuali non supportati, contraddetti o basati su fonti inadeguate vengano pubblicati come affidabili.** Una fonte citata non implica un claim vero; una quote presente non implica che la quote sostenga il claim. La sotto-fase 8.8A-GATE rende il Final Answer Gate in grado di **consumare** il giudizio di entailment già materializzato in `claim_entailment_checks` e di **bloccare** o **segnalare** in modo osservabile i casi più gravi, senza promettere chiusura completa.

---

## 1. Stato di partenza post-8.8A-WORKER

Tutto ciò che segue è verificato leggendo i file effettivamente forniti. Nessuna affermazione è inventata.

### 1.1 Schema DB applicato e immutabile

| Migration | Stato | Rilevanza per 8.8A-GATE |
|---|---|---|
| `0001_foundation.sql` | applicata, immutabile | `audit_records`, `task_masters`, trigger comuni. |
| `0002_storage.sql` | applicata, immutabile | `storage_blobs`, `storage_objects`. |
| `0003_documents.sql` | applicata, immutabile | `evidence_spans` append-only. |
| `0004_claim_ledger.sql` | applicata, immutabile | **`claim_ledger_entries` append-only**, UNIQUE `cle_id_logical_uq (id, claim_logical_id)`, **`claim_evidence_links`** con UNIQUE `cel_entry_span_uq` e FK composita `cel_entry_logical_consistency`. |
| `0005_answers_gate.sql` | applicata, immutabile | **`final_answer_spans`** append-only, **`final_answer_span_claim_links`** con FK composita `fasc_entry_logical_consistency`, **`final_gate_reports`** append-only UNIQUE per draft, **`coverage_gap_statements`** UNIQUE composito `(draft_final_answer_id, kind, gap_key)`. |
| `0006_lifecycle.sql` | applicata, immutabile | fuori scope diretto. |
| `0007_source_quality.sql` | applicata, immutabile | `source_quality_assessments` append-only. |
| `0008_coverage_gap_source_quality.sql` | applicata, immutabile | estende `coverage_gap_statements.kind` da quattro a **sei** valori: `{unverified_claim, missing_evidence, out_of_scope, source_loss, source_quality_block, source_quality_warning}`. Vincolo nominato `coverage_gap_statements_kind_check`. |
| `0009_claim_entailment_checks.sql` | **applicata, immutabile** (8.8A-SCHEMA) | crea `claim_entailment_checks` append-only, granularità `(claim_ledger_entry_id, evidence_span_id)`, versionata, idempotente. |

**Conseguenza di numerazione**: la prossima migration disponibile è **0010**. È il numero raccomandato per l'estensione del CHECK su `coverage_gap_statements.kind` discusso in §7 di questo documento. La retention futura distruttiva, già rinviata in 8.7G/H/8.8A-SCHEMA, slitterà a 0011 o successivo.

### 1.2 Schema reale `claim_entailment_checks` (da `migrations/0009_claim_entailment_checks.sql`)

Colonne rilevanti per il Gate:

```
id                       UUID PRIMARY KEY
tenant_id                UUID NOT NULL (FK tenants, RESTRICT)
project_id               UUID NULLABLE (FK projects, RESTRICT)
task_id                  UUID NOT NULL (FK task_masters, RESTRICT)
claim_logical_id         UUID NOT NULL (FK logical_claims, RESTRICT)
claim_ledger_entry_id    UUID NOT NULL
evidence_span_id         UUID NOT NULL (FK evidence_spans, RESTRICT)
version_no               INTEGER NOT NULL CHECK (>= 1)
verdict                  TEXT NOT NULL CHECK IN
                           ('entailed','partially_supported',
                            'not_supported','contradicted','uncertain')
confidence               DOUBLE PRECISION NULL CHECK (0.0..1.0 or NULL)
checker_name             TEXT NOT NULL
checker_version          TEXT NOT NULL
policy_name              TEXT NOT NULL
policy_version           TEXT NOT NULL
idempotency_key          TEXT NOT NULL
rationale                TEXT NULL
payload                  JSONB NOT NULL DEFAULT '{}'::jsonb
created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
```

Vincoli rilevanti per la query del Gate:

- FK composita `cec_entry_logical_consistency (claim_ledger_entry_id, claim_logical_id) → claim_ledger_entries(id, claim_logical_id)`: garantisce DB-level che la riga non punti a un ledger entry di un altro logical_claim. Stesso pattern di `cel_entry_logical_consistency` (0004) e `fasc_entry_logical_consistency` (0005).
- UNIQUE `cec_entry_span_version_uq (claim_ledger_entry_id, evidence_span_id, version_no)`: una sola riga per pair-version.
- UNIQUE `cec_entry_span_idem_uq (claim_ledger_entry_id, evidence_span_id, idempotency_key)`: idempotenza per chiave applicativa.
- Indici di lookup: `cec_task_idx (task_id)`, `cec_claim_logical_idx`, `cec_evidence_span_idx`, `cec_verdict_idx`.
- Trigger `claim_entailment_checks_append_only` BEFORE UPDATE OR DELETE → `reject_modify_append_only()`.

### 1.3 Schema reale `coverage_gap_statements.kind` (da 0005 + 0008)

Vincolo CHECK attuale, nominato `coverage_gap_statements_kind_check`:

```
kind IN (
  'unverified_claim',          -- 0005 (CVE-lite branch)
  'missing_evidence',          -- 0005 (no_verified_claims branch)
  'out_of_scope',              -- 0005 (riservato)
  'source_loss',               -- 0005 (riservato, popolato da source-loss propagation futura)
  'source_quality_block',      -- 0008 (8.7G block path)
  'source_quality_warning'     -- 0008 (8.7G warning path)
)
```

UNIQUE composito invariato: `coverage_gap_statements_idem_uq (draft_final_answer_id, kind, gap_key)`. Severity ammessa: `{info, warn, block}`. Nessun trigger append-only (debito noto, ereditato da 8.5/8.7G).

### 1.4 Stato corrente del Final Answer Gate (`apps/worker/app/services/final_answer_gate.py`)

Verificato leggendo il file fornito nel contesto:

| Branch | Condizione | `decision` | `reason_code` | Coverage gap | `published_answers` v1 |
|---|---|---|---|---|---|
| A | zero spans | `rejected` | `no_verified_claims` | `missing_evidence` con `gap_key='no_verified_claims'` | no |
| C | ≥1 span non verified-backed (priorità CVE-lite) | `rejected` | `unverified_spans_present` | un `unverified_claim` per span scoperto, `gap_key=f'span:{span_id}'` | no |
| C' (8.7G) | tutti verified-backed + ≥1 span con SQ block | `rejected` | `source_quality_block` | `source_quality_block` per span bloccato + eventuali `source_quality_warning` | no |
| B' (8.7G) | tutti verified-backed + ≥1 span con SQ warning, nessun SQ block | `approved` | `all_spans_verified_with_warnings` | `source_quality_warning` per span con warning | sì |
| B (8.4) | tutti verified-backed + nessun warning SQ | `approved` | `all_spans_verified` | nessuno | sì |

**Verifica diretta**: il file `final_answer_gate.py` letto NON contiene alcun riferimento a `claim_entailment_checks`, nessuna funzione `_select_entailment_per_span`, nessuna costante `_ENTAILMENT_*`. **Confermato**: il Gate al commit corrente ignora completamente l'entailment.

Priorità invariante 8.7G: **CVE-lite > Source Quality**. Uno span non verified-backed produce `unverified_spans_present` indipendentemente dal source quality e — questa è la novità rilevante per 8.8A-GATE — indipendentemente dall'entailment.

### 1.5 Stato corrente della pipeline `task.created`

Verificato leggendo `apps/worker/app/consumers/task_created.py`:

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
  -> task.source_quality_assessed         (8.7E, SAVEPOINT)
  -> task.entailment_checked              (8.8A-WORKER, SAVEPOINT)   ← già attivo
  -> task.compiling
  -> task.draft_compiled
  -> task.final_gate_started
  -> task.final_gate_completed
  -> task.published | task.publication_held
```

Lo step entailment è già wrapped in `conn.begin_nested()` e produce sempre l'audit aggregato `task.entailment_checked` con `status ∈ {completed, failed}`. Il Gate viene invocato DOPO che `claim_entailment_checks` è già stata popolata per il task.

### 1.6 Stato corrente del mock checker (`claim_entailment_checker.py`)

Verificato leggendo il file:

- Identità: `mvp0_mock_entailment_checker` v0.1.0, policy `mvp0_mock_entailment` v0.1.0.
- Tre regole deterministiche, applicate in ordine, primo match vince:
  1. **Containment normalizzato** (lowercase + whitespace collapsed, claim ⊆ quote o quote ⊆ claim) → `entailed`, `confidence=0.8`.
  2. **Numeric mismatch** (entrambi i testi hanno numeri AND i set differiscono) → `not_supported`, `confidence=0.6`.
  3. **Default** → `uncertain`, `confidence=0.5`.
- **Il mock NON emette MAI `contradicted` né `partially_supported`**: verificato direttamente nel codice del checker e nelle costanti `_CONF_*`. Sono riservati a checker reali futuri o a seed di test fixtures.
- Ogni riga porta `payload.mock=True` e `payload.semantic_warning="mvp0 heuristic; not a real NLI/LLM entailment model"`.
- `version_no` fissato a 1 in MVP-0. Collisione di idempotency_key diversa su stessa pair → `status='error'` con `error_code='entailment_version_conflict'`, NON mascherato.

### 1.7 Stato corrente dell'orchestrator (`claim_entailment_orchestrator.py`)

Verificato leggendo il file:

- Discovery query: `DISTINCT (cel.claim_ledger_entry_id, cel.evidence_span_id) FROM claim_evidence_links cel JOIN logical_claims lc ON lc.id = cel.claim_logical_id WHERE lc.task_id = :task_id AND cel.evidence_span_id IS NOT NULL`.
- Idempotency key deterministica: `task:{task_id}:entry:{entry_id}:span:{span_id}:v1`.
- Conta esiti: `assessed_count`, `already_assessed_count`, `not_found_count`, `invalid_target_count`, `error_count`.
- Non emette audit. Non muta task_masters.

**Conseguenza importante per 8.8A-GATE**: l'orchestrator chiama il checker SOLO per `(claim_ledger_entry_id, evidence_span_id)` derivati da `claim_evidence_links` con `evidence_span_id IS NOT NULL`. Il Gate dovrà ricostruire questa stessa logica (con un filtro aggiuntivo su latest+verified) per leggere "le righe entailment rilevanti per ogni span verified-backed del draft".

### 1.8 Cosa manca esplicitamente: lo step di consumo

Dal `PHASE_8_8A_PRE.md §13` e dallo stato corrente del Gate:

- **Il Gate NON consulta `claim_entailment_checks`**. Le righe vengono scritte ma il Gate non le legge.
- Nessun `coverage_gap_statements.kind ∈ {entailment_block, entailment_warning}` viene mai emesso.
- Nessun `final_gate_reports.reason_code='entailment_block'` viene mai prodotto.
- Nessuna read API entailment è esposta.

**Questo blocco 8.8A-GATE-PRE prepara la chiusura di questo gap**, decidendo policy, priorità, schema delta, query e test plan PRIMA che il codice venga scritto.

---

## 2. Problema da risolvere in 8.8A-GATE

### 2.1 Cosa il sistema verifica oggi

Per ogni span del draft:

1. **Evidence support** (legame strutturale `claim_evidence_links`).
2. **CVE-lite verification** (`verification_records`, `check_kind='cve_lite'`): quote presente nel chunk + `quote_hash` corretto.
3. **Source quality** (`source_quality_assessments`): autorità/freschezza/indipendenza/qualità estratto della fonte.
4. **Claim entailment** (`claim_entailment_checks`): **MATERIALIZZATA ma NON LETTA DAL GATE**.
5. **Final Answer Gate**: compone CVE-lite + Source Quality. Ignora completamente l'asse 4.

### 2.2 Cosa NON viene fatto consumare al Gate

L'entailment è scritto nelle righe ma non agisce. Conseguenza pratica:

- Un mock verdict `not_supported` (es. numeric mismatch nel claim) viene registrato, ma il Gate ignora la riga e pubblica lo span come `verified_fact` se CVE-lite passa e Source Quality non blocca.
- Un (futuro) seed di `verdict='contradicted'` resterebbe inerte rispetto alla decisione di pubblicazione.

8.8A-GATE colma questo gap **rendendo il Gate consumatore read-only** di `claim_entailment_checks`, con:

1. Una **policy** che mappa verdict → block / warning / clean.
2. Una **priorità** ben definita rispetto ai branch esistenti (CVE-lite, Source Quality).
3. **Nuovi coverage gap kinds** (`entailment_block`, `entailment_warning`) idempotenti per draft.
4. **Nuovi reason_code** dove necessario.
5. Una **query DB** che identifica deterministicamente la latest assessment per pair `(claim_ledger_entry_id, evidence_span_id)` rilevante per ogni final_answer_span verified-backed.

### 2.3 Cosa NON è 8.8A-GATE

Per evitare slittamenti di scope:

- **Non è** un Citation-to-Claim Validator (8.8B): non verifica che la quote sia "vicina ma sbagliata" rispetto al claim. Riceve già le pair `(entry, span)` selezionate dall'extractor 8.3.
- **Non è** un Contradiction Detector reale (8.8C): non incrocia più fonti. Il verdict `contradicted` qui resta una segnalazione locale singola-pair.
- **Non è** un Final Answer Sentence Gate (8.8D): non opera a livello frase del published_answer.
- **Non è** un meccanismo di mutazione del Claim Ledger: il Gate resta read-only su `claim_ledger_entries` e su `claim_entailment_checks`.
- **Non è** un upgrade del checker: il mock heuristic resta invariato.

---

## 3. Invarianti semantiche

Invarianti che 8.8A-GATE-CODE deve preservare, salvo motivazione esplicita.

1. **claim correctness ≠ evidence support**: invariata.
2. **evidence support ≠ entailment**: invariata da 8.8A-PRE.
3. **entailment ≠ source quality**: invariata. Una fonte autorevole può ospitare una quote che non implica il claim; una fonte debole può ospitare una quote che lo implica esattamente.
4. **source quality ≠ verification outcome**: invariata.
5. **contradiction detection ≠ entailment singolo**: invariata. `verdict='contradicted'` resta segnalazione locale.
6. **append-only**: invariata. Il Gate NON inserisce, NON aggiorna, NON cancella righe in `claim_entailment_checks`.
7. **invarianza del Claim Ledger**: il Gate NON muta `claim_ledger_entries`, NON crea v(N+1) `unverifiable` su `not_supported`, NON aggiunge stati come `entailment_failed`.
8. **invarianza CVE-lite**: il Gate continua a richiedere verified-backed prima di consultare entailment.
9. **invarianza Source Quality**: il Gate continua a leggere `source_quality_assessments` read-only.
10. **invarianza della pipeline**: il Gate non modifica `task_masters.status`, non emette nuovi `audit_records` oltre quelli già previsti dal consumer, non emette eventi Redis.

### 3.1 Priorità da preservare

Nuova invariante introdotta in 8.8A-GATE, da non rompere in refactor futuri:

```
CVE-lite (verified-backed)  >  Claim Entailment  >  Source Quality
```

Motivazione completa in §6.

---

## 4. Campi `claim_entailment_checks` rilevanti per il Gate

Solo un sottoinsieme delle colonne è effettivamente consultato dal Gate:

| Colonna | Uso nel Gate |
|---|---|
| `id` | identificatore tracciabile in `coverage_gap_statements.details` (audit). |
| `claim_ledger_entry_id` | chiave di JOIN con `claim_evidence_links` e con `final_answer_span_claim_links`. |
| `evidence_span_id` | chiave di aggregazione per span. |
| `claim_logical_id` | usata implicitamente via FK composita; non serve al Gate a runtime. |
| `version_no` | discriminante per "latest assoluta" (ORDER BY `version_no DESC`). |
| `verdict` | input principale della policy. |
| `confidence` | NON consultata in MVP-0 (vedi §5 e §6: P5 scartata). |
| `checker_name`, `checker_version`, `policy_name`, `policy_version` | scritti dentro `coverage_gap_statements.details` per audit/calibrazione futura. |
| `payload.mock` | NON consultato dal Gate per decidere; ma copiato in details come segnalazione downstream. |
| `created_at` | tie-breaker per "latest" quando `version_no` è uguale (caso teorico in MVP-0 con `version_no` fissato a 1, ma tie-breaker utile per evoluzioni future). |

Colonne **deliberatamente non usate** in MVP-0:
- `confidence`: la policy P5 (block solo sopra soglia) è scartata, vedi §5/§6.
- `rationale`: testo human-readable, non semanticamente significativo per il Gate.
- `idempotency_key`: chiave applicativa dell'orchestrator, non del Gate.
- `tenant_id`, `project_id`, `task_id`: il Gate scopre già lo scope dal draft.

---

## 5. Policy candidate

Si analizzano sei policy candidate. Per ognuna: rischio overblocking, rischio underblocking, compatibilità col mock attuale, compatibilità con i test esistenti, valore anti-allucinazione, costo implementativo.

### 5.1 P0 — no-op

Il Gate ignora entailment. Nessun nuovo branch.

- **Overblocking**: nullo.
- **Underblocking**: massimo. Un futuro `verdict='contradicted'` reale non blocca nulla. La promessa "entailment è materializzato e visibile" è disattesa.
- **Compatibilità mock**: totale.
- **Compatibilità test 8.7G**: totale.
- **Valore anti-allucinazione**: zero.
- **Costo**: zero, ma chiude la fase 8.8A-GATE prima di iniziarla.
- **Verdetto**: **scartata**. È equivalente a non fare 8.8A-GATE.

### 5.2 P1 — block only `contradicted`

`contradicted` blocca. `not_supported`, `partially_supported`, `uncertain`, missing → warning. `entailed` → clean.

- **Overblocking**: minimo. Il mock non emette MAI `contradicted` (verificato in §1.6), quindi il branch block è dormiente in produzione mock-driven.
- **Underblocking**: medio. `not_supported` da mock heuristic (numeric mismatch) non blocca: i casi "claim 41% vs quote 37%" passano. Coerente con la promessa MVP-0 di non promettere chiusura.
- **Compatibilità mock**: alta. Il mock produce maggioritariamente `entailed` (Regola 1 contenimento) o `uncertain`/`not_supported`; nessuno di questi blocca.
- **Compatibilità test 8.7G**: i 13 scenari esistenti non vedono entailment; con seed entailment assente in quei test, il Gate emette un warning `entailment_missing_check` per span — i test 8.7G andranno aggiornati (vedi §11).
- **Valore anti-allucinazione**: limitato in produzione mock, ma significativo quando un checker reale comincerà a emettere `contradicted`.
- **Costo**: medio. Una migration di estensione `kind`, un branch nel Gate, due nuovi `kind`, un nuovo `reason_code`.
- **Verdetto**: **candidata principale**. Coerente con il principio "introduci entailment in modo osservabile, blocca solo i casi gravi".

### 5.3 P2 — block `contradicted` + `not_supported`

Entrambi bloccano. `partially_supported`, `uncertain`, missing → warning. `entailed` → clean.

- **Overblocking**: alto in MVP-0. Il mock emette `not_supported` per ogni claim con numeri leggermente diversi dalla quote. In produzione pipeline-driven (extractor 8.3 estrae raw_claim dalla quote, numeri solitamente identici), il rischio è basso ma non zero — un claim che riformula leggermente il numero (`'about 37%'` vs `'37%'`) potrebbe scattare il numeric mismatch. Inoltre rende il `Branch entailment_block` attivo immediatamente, generando rumore se l'heuristic produce falsi positivi.
- **Underblocking**: minore di P1.
- **Compatibilità mock**: bassa. Il mock heuristic non ha la maturità per giustificare `not_supported → block`.
- **Valore anti-allucinazione**: maggiore di P1, ma con costo di affidabilità del mock.
- **Costo**: identico a P1.
- **Verdetto**: **scartata in MVP-0**. Diventa candidata quando arriverà un checker reale (NLI/LLM), con bump di `policy_version`.

### 5.4 P3 — strict (ogni verdict ≠ `entailed` blocca)

Solo `entailed` clean. Tutto il resto → block.

- **Overblocking**: massimo. `uncertain` (verdict di default del mock) blocca → la maggior parte dei task verrebbe rifiutata in produzione mock-driven.
- **Compatibilità mock**: nulla.
- **Valore anti-allucinazione**: irraggiungibile in pratica perché il sistema diventerebbe inutilizzabile.
- **Verdetto**: **scartata**.

### 5.5 P4 — soft MVP

`contradicted` blocca. `not_supported`, `partially_supported`, `uncertain`, missing → warning. `entailed` → clean.

Equivalente a P1. La distinzione lessicale "soft MVP" vs "P1" non porta differenze tecniche. La unifico con P1 nella raccomandazione (§6).

### 5.6 P5 — `not_supported` block sopra soglia

`contradicted` blocca sempre. `not_supported` blocca solo se `confidence >= soglia`. Altrimenti warning.

- **Overblocking**: medio. Il mock emette `not_supported` con `confidence=0.6` fissa. Una soglia `>= 0.7` lascerebbe il mock sempre sotto soglia (warning), una soglia `>= 0.5` lo manda sempre sopra (block, come P2).
- **Compatibilità mock**: il `confidence=0.6` fisso rende la policy una scelta binaria mascherata da soglia. Non c'è discrezione.
- **Compatibilità con `payload.mock`**: nessuna semantica chiara. Una soglia su `confidence` ignora che il `confidence` del mock è un placeholder.
- **Valore**: nullo in MVP-0; significativo se ci fosse un checker reale che modula `confidence` in modo informato.
- **Costo**: medio + soglia da motivare e versionare.
- **Verdetto**: **scartata in MVP-0**. Diventerà sensata quando il `confidence` sarà realmente informativo, allineata con un futuro real checker.

---

## 6. Raccomandazione MVP-0

**Policy raccomandata: P1 / P4 (equivalenti).**

| Verdict latest per pair `(entry, span)` | Decisione Gate | Coverage gap emesso |
|---|---|---|
| `contradicted` | **block** | `kind='entailment_block'`, severity `block` |
| `not_supported` | **warning** in MVP-0 | `kind='entailment_warning'`, severity `warn` |
| `partially_supported` | warning | `entailment_warning`, severity `warn` |
| `uncertain` | warning | `entailment_warning`, severity `warn` |
| missing check (latest assente) | warning | `entailment_warning`, severity `warn` |
| `entailed` | clean lato entailment (può comunque essere bloccato da Source Quality o downstream) | nessuno |

**Motivazione tecnica (verificata contro il codice reale)**:

1. Il mock attuale **non produce `contradicted`** (verificato direttamente nel file `claim_entailment_checker.py` letto: le costanti di output del default heuristic sono solo `VERDICT_ENTAILED`, `VERDICT_NOT_SUPPORTED`, `VERDICT_UNCERTAIN`). Il branch block è quindi attivabile in MVP-0 solo via seed/stub di test (mirror del pattern 8.7H per Branch C' di Source Quality).
2. Il mock produce `not_supported` via euristica `numeric_mismatch`: bloccare su questo verdict in MVP-0 significherebbe far passare al `coverage_gap_statements` falsi positivi da heuristic regex. P-block-lazy.
3. Il progetto ha già due branch block (`no_verified_claims`, `unverified_spans_present`) e uno orientato a fonte (`source_quality_block`). Introdurre entailment in modo **osservabile** (warning visibili come gap, block solo sui casi più gravi) è coerente con la disciplina già adottata in 8.7G.
4. La policy è versionata (`mvp0_entailment_gate_policy` v0.1.0) per permettere un futuro bump tracciabile quando un checker reale renderà difendibile P2 o P5.

**Aggregazione tra più evidence_span che supportano lo stesso final_answer_span**: **worst-on-block, any-on-warn**. Pattern già rodato in 8.7G; vedi §9.

---

## 7. Priorità Gate proposta

### 7.1 Priorità totale raccomandata

```
1. no_verified_claims                     (Branch A invariata)
2. unverified_spans_present / CVE-lite    (Branch C invariata, 'priorità CVE-lite')
3. entailment_block                       (NUOVO, dopo CVE-lite)
4. source_quality_block                   (Branch C' 8.7G, abbassato di un livello)
5. approved_with_warnings                 (B' 8.7G, semantica estesa)
6. approved_clean                         (B 8.4 invariata)
```

### 7.2 Motivazione punto per punto

- **`no_verified_claims` resta in cima**: zero spans significa che non c'è materiale da valutare. Nessun asse downstream ha senso.
- **`unverified_spans_present` resta secondo**: se un link non punta alla latest verified, l'asse evidence-support è già rotto. Discutere entailment di una quote che non sostiene strutturalmente un claim verificato è prematuro. **Questa invariante "CVE-lite > resto" è quella già testata nello scenario 12 di `test_final_answer_gate_source_quality.py`** e deve essere estesa esplicitamente in 8.8A-GATE-CODE con un test "CVE-lite > Entailment".
- **`entailment_block` sopra `source_quality_block`**: una quote che contraddice (`contradicted`) o non sostiene (`not_supported` in P2 futuro) il claim è semanticamente più grave di una fonte autorevole-ma-debole. Il senso comune del "anti-allucinazione" dice: prima blocco il claim non sostenuto dalla sua propria quote, poi discuto la qualità strutturale della fonte. Inoltre: un'allucinazione tipica produce una quote che assomiglia al claim ma non lo implica → entailment lo rileva, source quality no. Questa è una motivazione di valore.
- **`source_quality_block` resta presente, ma posizionato dopo entailment**: scenario realistico in cui entrambi fioccano sullo stesso draft → reason_code = `entailment_block`, ma il Gate emette ENTRAMBE le tipologie di gap (entailment e source_quality), così l'audit è completo. Vedi §10 per la query.
- **Approvazione con warning** assume nuova semantica (vedi §8.2): può ora derivare da warning entailment, warning source quality, o entrambi.

### 7.3 Implicazione di emissione

- Il `reason_code` finale segue la priorità sopra: il primo branch che firma `decision='rejected'` determina il `reason_code`.
- I `coverage_gap_statements` invece sono emessi **per ogni asse che ha qualcosa da dire** sul draft, in modo idempotente (vedi §9). Esempio: un draft con CVE-lite OK ma `contradicted` su uno span e `unsuitable` source quality su un altro → `reason_code='entailment_block'` + 1 gap `entailment_block` + 1 gap `source_quality_block` (e nessun gap `unverified_claim` perché CVE-lite è passata).

### 7.4 Cosa NON cambia

- L'ordine di emissione audit `task.final_gate_started → task.final_gate_completed` invariato.
- Nessuna proliferazione di reason_code (vedi §8): il numero totale resta basso e controllato.

---

## 8. Decisione su `coverage_gap_statements.kind`

### 8.1 Servono nuovi kind?

**Sì, due**: `entailment_block` e `entailment_warning`.

**Motivazione per cui NON riusare kind esistenti**:

- **NON riusare `unverified_claim`**: appartiene a CVE-lite. Mescolarlo con entailment costringe i consumatori (UI, eval, audit) a ispezionare `details` per disambiguare "CVE-lite fallita" da "entailment fallito". Le due cose sono ortogonali (vedi §3, invariante 8).
- **NON riusare `source_quality_block` / `source_quality_warning`**: appartengono a 8.7G. Entailment è asse diverso. La promessa di 0008 era proprio non confondere CVE-lite con Source Quality; rompere questa promessa per Entailment sarebbe regressivo.
- **NON usare un kind generico come `missing_evidence` con info dentro `details`**: il payload JSONB non è enforced né indicizzato; un kind generico nasconde la dimensione al `pg_dump`, agli explain plan, ai dashboard, all'audit `GROUP BY kind`. Il `kind` deve restare una classificazione dimensionale.

### 8.2 Migration futura raccomandata: `0010_coverage_gap_entailment.sql`

**Da implementare in 8.8A-GATE-SCHEMA (NON in questo blocco).** Forma logica:

```sql
-- 0010_coverage_gap_entailment.sql (CANDIDATO per 8.8A-GATE-SCHEMA)
-- Estende il CHECK su coverage_gap_statements.kind con due nuovi valori.

DO $$
DECLARE
  v_conname TEXT;
BEGIN
  -- Stesso pattern di 0008: pg_constraint.conkey JOIN pg_attribute per
  -- localizzare il CHECK su .kind robustamente, indipendentemente da come
  -- Postgres lo abbia internamente rappresentato (IN(...) vs ANY(ARRAY[...])).
  SELECT c.conname
    INTO v_conname
    FROM pg_constraint c
    JOIN pg_attribute a
      ON a.attrelid = c.conrelid
     AND a.attnum   = ANY(c.conkey)
   WHERE c.conrelid = 'coverage_gap_statements'::regclass
     AND c.contype  = 'c'
     AND a.attname  = 'kind'
   ORDER BY c.conname
   LIMIT 1;

  IF v_conname IS NULL THEN
    RAISE EXCEPTION '0010: could not locate CHECK on coverage_gap_statements.kind';
  END IF;

  EXECUTE format(
    'ALTER TABLE coverage_gap_statements DROP CONSTRAINT %I',
    v_conname
  );
END
$$;

ALTER TABLE coverage_gap_statements
  ADD CONSTRAINT coverage_gap_statements_kind_check CHECK (kind IN (
    -- preesistenti da 0005
    'unverified_claim',
    'missing_evidence',
    'out_of_scope',
    'source_loss',
    -- aggiunti da 0008
    'source_quality_block',
    'source_quality_warning',
    -- aggiunti da 0010 (questo blocco)
    'entailment_block',
    'entailment_warning'
  ));
```

**Vincoli da rispettare in 0010**:

- **Modifica SOLO** il CHECK su `kind`. Nessun altro vincolo, nessuna nuova UNIQUE, nessuna nuova severity, nessun trigger nuovo, nessun cambio sui kind esistenti.
- **Preservare** integralmente: `unverified_claim`, `missing_evidence`, `out_of_scope`, `source_loss`, `source_quality_block`, `source_quality_warning`. I dati esistenti devono rimanere validi.
- **Riusare** la severity esistente (`{info, warn, block}`): `entailment_block` → severity `block`, `entailment_warning` → severity `warn`.
- **Mantenere** l'UNIQUE composito `(draft_final_answer_id, kind, gap_key)` invariato.
- **Costo numerazione**: 0010 occupato → retention futura distruttiva slitta a 0011.

### 8.3 Test plan migration (per 8.8A-GATE-SCHEMA)

Test specifici per 0010 sono enumerati in §11.1.

---

## 9. Reason code e semantica dei warning

### 9.1 Nuovi `final_gate_reports.reason_code`

**Aggiungere uno solo: `entailment_block`.**

Per il caso approved-con-warning, **NON introdurre** `all_spans_verified_with_entailment_warnings`. Riusare il preesistente `all_spans_verified_with_warnings` (già introdotto in 8.7G).

**Motivazione (decisione)**:

- Proliferare reason_code (`all_spans_verified_with_entailment_warnings`, `all_spans_verified_with_sq_warnings`, `all_spans_verified_with_entailment_and_sq_warnings`...) genera N+M+NM stringhe per ogni nuova dimensione anti-allucinazione. Non scala.
- Il reason_code finale rappresenta **l'esito decisionale di alto livello**, non il dettaglio del perché. Il dettaglio vive nei `coverage_gap_statements` collegati al draft via `details.reasons`.
- Consumer (UI, eval) che vogliono distinguere il tipo di warning fanno `GROUP BY kind` su `coverage_gap_statements`. Il reason_code resta classificatore di "approved vs rejected vs approved_with_any_warning".

### 9.2 Reason code complessivi post-8.8A-GATE

```
final_gate_reports.reason_code ∈ {
  'no_verified_claims',                       -- Branch A (8.4)
  'unverified_spans_present',                 -- Branch C (8.4)
  'entailment_block',                         -- NUOVO (8.8A-GATE)
  'source_quality_block',                     -- Branch C' (8.7G)
  'all_spans_verified_with_warnings',         -- Branch B' (8.7G, ora con semantica estesa)
  'all_spans_verified'                        -- Branch B (8.4)
}
```

### 9.3 Semantica warning aggregata

Warning entailment possibili (post-8.8A-GATE):

- `entailment_not_supported`
- `entailment_partially_supported`
- `entailment_uncertain`
- `entailment_missing_check`
- (riservato) `entailment_checker_error` — se il Gate vede dati incoerenti, NON usato nella policy MVP-0 ma riservato

Warning Source Quality esistenti (8.7G):

- `source_quality_unknown`, `source_quality_weak`, `source_quality_contradiction_unchecked`, `source_quality_missing_assessment`.

### 9.4 Decisioni operative sulla forma del gap

- **Coesistenza di più warning sullo stesso span**: **consentita**. Un final_answer_span può avere contemporaneamente un `entailment_warning` (uno span supportante con `not_supported`) e un `source_quality_warning` (un altro span supportante con `unknown`). Entrambi i gap vengono emessi, ognuno col proprio kind e gap_key.
- **gap_key deterministico**: `f'span:{final_answer_span_id}:entailment_block'` e `f'span:{final_answer_span_id}:entailment_warning'`. Stesso pattern di 8.7G. **NON includere** il reason nel gap_key (es. NON `span:{id}:entailment_warning:not_supported`): se uno span ha più reasons (es. uno span supportato da due evidence_spans, una con `not_supported` e l'altra con `uncertain`), il Gate emette UN solo gap di kind `entailment_warning` con due entry in `details.reasons`. Vedi §10.4.
- **details JSONB**: contiene `span_id`, `span_index`, `reasons[]` (lista di dict strutturati), `per_evidence[]` (classificazione per ogni evidence_span supportante con il suo verdict), `policy.name`, `policy.version`. **Non** include il testo del claim, **non** include la quote, **non** include stack traces. Usa `ids` (entry_id, evidence_span_id, assessment_id) per consentire al consumatore di re-fetchare i dettagli.
- **Idempotenza redelivery**: garantita dall'UNIQUE `(draft_final_answer_id, kind, gap_key)` di 0005. Una seconda invocazione del Gate sullo stesso draft non duplica gap.

### 9.5 Reasons enumerate (forma data dei dict in `details.reasons`)

Ogni elemento di `details.reasons`:

```jsonc
{
  "reason_code": "entailment_contradicted" | "entailment_not_supported"
               | "entailment_partially_supported" | "entailment_uncertain"
               | "entailment_missing_check",
  "evidence_span_id": "uuid|null",
  "assessment_id": "uuid|null",
  "verdict": "contradicted|not_supported|partially_supported|uncertain|null",
  "confidence": <float|null>
}
```

`evidence_span_id=null` e `assessment_id=null` si presentano nel caso `entailment_missing_check`.

---

## 10. Query logica / punto di integrazione nel Gate

### 10.1 Input al nuovo branch

- `draft_final_answer_id` (già disponibile in `_select_latest_draft_for_task`).
- L'insieme `verified_span_ids` (già calcolato in `final_answer_gate.py` riga `verified_span_ids = {sid for sid, s in spans.items() if s["verified"]}`).

Il branch entailment si esegue **dopo** Branch A e Branch C (CVE-lite invariati), e **prima** del branch source quality. Quindi sostituisce/inserisce a partire dalla riga "Phase 8.7G: all spans are verified-backed. Consult Source Quality." di `final_answer_gate.py`.

### 10.2 Query per la latest entailment per pair `(entry, span)`

Pseudocodice / SQL logico (NON una implementazione finale):

```sql
-- _select_entailment_per_span(draft_id)
SELECT
  fas.id                       AS span_id,
  fas.span_index               AS span_index,
  cel.claim_ledger_entry_id    AS claim_ledger_entry_id,
  cel.evidence_span_id         AS evidence_span_id,
  cec_latest.id                AS cec_id,
  cec_latest.verdict           AS cec_verdict,
  cec_latest.confidence        AS cec_confidence,
  cec_latest.version_no        AS cec_version_no
FROM final_answer_spans fas
JOIN final_answer_span_claim_links fascl
  ON fascl.final_answer_span_id = fas.id
JOIN LATERAL (
  -- latest assoluta sullo stesso logical_claim, per applicare la regola
  -- "verified-backed via latest entry" (invariante 8.4 / 8.7G)
  SELECT cle_latest.id, cle_latest.state, cle_latest.version_no
  FROM claim_ledger_entries cle_latest
  WHERE cle_latest.claim_logical_id = fascl.claim_logical_id
  ORDER BY cle_latest.version_no DESC
  LIMIT 1
) latest_entry ON TRUE
JOIN claim_evidence_links cel
  ON cel.claim_ledger_entry_id = fascl.claim_ledger_entry_id
LEFT JOIN LATERAL (
  -- latest assoluta dell'entailment check sulla pair (entry, evidence_span)
  -- ORDER BY (version_no DESC, created_at DESC, id DESC) per determinismo
  SELECT cec.id, cec.verdict, cec.confidence, cec.version_no
  FROM claim_entailment_checks cec
  WHERE cec.claim_ledger_entry_id = cel.claim_ledger_entry_id
    AND cec.evidence_span_id      = cel.evidence_span_id
  ORDER BY cec.version_no DESC, cec.created_at DESC, cec.id DESC
  LIMIT 1
) cec_latest ON TRUE
WHERE fas.draft_final_answer_id = :draft_id
  AND cel.evidence_span_id IS NOT NULL
  AND fascl.claim_ledger_entry_id = latest_entry.id
  AND latest_entry.state = 'verified_fact'
ORDER BY fas.span_index ASC, cel.evidence_span_id ASC;
```

### 10.3 Considerazioni sulla query

- **Simmetria con `_select_source_quality_per_span`** (8.7G): stessa struttura LATERAL + filtro `latest_entry.state='verified_fact'`. La differenza è la tabella di destra (`claim_entailment_checks` invece di `source_quality_assessments`) e la chiave di JOIN (pair `(entry_id, evidence_span_id)` invece di solo `evidence_span_id`).
- **LEFT JOIN LATERAL** su `cec_latest`: necessario per gestire il caso "missing entailment check" (task pre-8.8A senza righe entailment): il Gate emette un warning `entailment_missing_check` invece di crashare.
- **Filtro `cel.evidence_span_id IS NOT NULL`**: stesso pattern dell'orchestrator entailment. Honor di `cel_origin_xor`.
- **Filtro `latest_entry.state='verified_fact'`**: garantisce che il Gate consulti entailment SOLO per gli span già verified-backed. Quindi se Branch C (CVE-lite) ha già rifiutato, questa query non viene mai eseguita.

### 10.4 Aggregazione: worst-on-block, any-on-warn

Stesso pattern di 8.7G. Per ogni `final_answer_span`:

1. Per ogni pair `(entry, evidence_span)`:
   - Se `cec_latest.id IS NULL` → `missing_check` → contribuisce a warning per quello span.
   - Altrimenti applicare la matrice policy del §6:
     - `verdict='contradicted'` → contribuisce al **block** dello span;
     - `verdict ∈ {not_supported, partially_supported, uncertain}` → contribuisce al **warning** dello span;
     - `verdict='entailed'` → contribuisce al **clean** dello span (no gap).
2. Aggregare per span: worst-on-block (se ANY pair contribuisce a block → span block), altrimenti any-on-warn.
3. Aggregare per draft: se ANY span blocked → reason_code='entailment_block' + emit gap `entailment_block` per ogni span bloccato + emit gap `entailment_warning` per ogni span con warning.
4. Se nessuno span è blocked ma ANY span ha warning → reason_code di approved diventa `all_spans_verified_with_warnings` (riusando quello esistente o aggiunto in 8.7G), emit gap `entailment_warning` per ogni span con warning.
5. Se nessuno span ha warning entailment, il branch entailment non emette gap e cede il controllo al branch source quality 8.7G.

### 10.5 Combinazione con source_quality

Sequenza decisionale completa post-8.8A-GATE:

```
1. Branch A:  spans_total == 0           → rejected, reason='no_verified_claims'
2. Branch C:  ANY span unverified-backed → rejected, reason='unverified_spans_present'
3. Branch E:  ANY span entailment-block  → rejected, reason='entailment_block'
              + emit entailment_block gaps for blocked spans
              + emit entailment_warning gaps for non-blocked spans with warnings
              + emit source_quality_warning/block gaps (audit completo, NON cambia il reason_code)
4. Branch SQ-block: ANY span source-quality-block (senza entailment-block)
              → rejected, reason='source_quality_block'
              + emit source_quality_block gaps
              + emit source_quality_warning gaps
              + emit entailment_warning gaps (audit completo, NON cambia il reason_code)
5. Branch W:  ANY span with warning (entailment or SQ), nessun block
              → approved, reason='all_spans_verified_with_warnings'
              + emit entailment_warning gaps
              + emit source_quality_warning gaps
6. Branch B:  tutti clean                → approved, reason='all_spans_verified'
              + nessun gap
```

**Invariante**: in ogni branch "rejected", il Gate emette TUTTI i gap rilevanti (entailment + source quality), non solo quelli del branch che determina il reason_code. Così l'audit resta completo e i consumatori downstream possono ispezionare lo stato totale del draft.

### 10.6 Read-only contract

Il Gate, durante la valutazione entailment:

- NON inserisce in `claim_entailment_checks`.
- NON aggiorna `claim_entailment_checks`.
- NON cancella da `claim_entailment_checks`.
- NON tocca `claim_ledger_entries` (continua come da 8.7G).
- NON tocca `source_quality_assessments`.
- NON tocca `verification_records`.

L'unica scrittura collegata al nuovo branch è:
- INSERT in `coverage_gap_statements` (idempotente via UNIQUE composito).
- INSERT in `final_gate_reports` (idempotente via UNIQUE su draft_final_answer_id).
- INSERT condizionato in `published_answers` (solo nei branch approved).

### 10.7 No nuovi audit dal Gate

Il Gate continua a emettere SOLO `task.final_gate_started` e `task.final_gate_completed` (gestiti dal consumer). **Nessun nuovo evento audit** è introdotto in 8.8A-GATE: il dettaglio dei warning entailment vive nei `coverage_gap_statements` e nel `final_gate_reports.payload`, non in nuovi tipi di audit event.

---

## 11. Test plan per i blocchi successivi

### 11.1 8.8A-GATE-SCHEMA — `tests/test_migration_0010_coverage_gap_entailment.py`

Test richiesti, mirror di `tests/test_migration_0008_coverage_gap_source_quality.py`:

1. `kind='entailment_block'` accettato (severity `block`).
2. `kind='entailment_warning'` accettato (severity `warn`).
3. Vecchi kind ancora accettati: `unverified_claim`, `missing_evidence`, `out_of_scope`, `source_loss`, `source_quality_block`, `source_quality_warning` — ognuno con un test parametrizzato.
4. Kind sconosciuto (`entailment`, `entailment_blocked`, `entailment_warn`, `''`, `'entailment_block_extra'`) rifiutato con `CheckViolation`.
5. UNIQUE composito `(draft_final_answer_id, kind, gap_key)` ancora funziona:
   - duplicato esatto su `entailment_block` → rifiutato;
   - stesso gap_key con kind diverso (`entailment_block` + `source_quality_block` + `entailment_warning`) → tutti accettati.
6. severity `block` ammessa con `entailment_block`.
7. severity `warn` ammessa con `entailment_warning`.
8. Migration non tocca `claim_entailment_checks`: conteggio CHECK invariato (esattamente 3 da 0009: `cec_verdict_chk`, `cec_version_no_chk`, `cec_confidence_range`).
9. Migration non tocca `source_quality_assessments`: conteggio CHECK invariato (esattamente 12 da 0007).
10. Migration non tocca `final_gate_reports`: conteggio CHECK invariato (esattamente 1 da 0005).
11. Il nuovo CHECK è esplicitamente nominato `coverage_gap_statements_kind_check` (stesso nome del precedente, perché l'estensione lo droppa e ricrea con lo stesso nome).
12. No regressione 0008: il kind `source_quality_block` resta accettato con `details` JSONB conforme alla forma di 8.7G.

### 11.2 8.8A-GATE-CODE — `apps/worker/tests/test_final_answer_gate_entailment.py`

Test richiesti, mirror di `test_final_answer_gate_source_quality.py`. Helpers locali, no import cross-file.

1. **`verdict='contradicted'` → block path**:
   - decision='rejected', reason_code='entailment_block';
   - gap `entailment_block` severity='block', gap_key=`span:{id}:entailment_block`;
   - `details.reasons[0].reason_code='entailment_contradicted'`;
   - no `published_answers` v1.
2. **`verdict='not_supported'` → warning**:
   - decision='approved', reason_code='all_spans_verified_with_warnings';
   - gap `entailment_warning` severity='warn';
   - `details.reasons[0].reason_code='entailment_not_supported'`;
   - `published_answers` v1 inserito.
3. **`verdict='partially_supported'` → warning**: analogo a 2, `reason_code='entailment_partially_supported'`.
4. **`verdict='uncertain'` → warning**: analogo a 2, `reason_code='entailment_uncertain'`.
5. **Missing entailment check (no row in `claim_entailment_checks` per la pair) → warning** `entailment_missing_check`, decision='approved'.
6. **`verdict='entailed'` clean lato entailment, NO SQ warning → clean**:
   - decision='approved', reason_code='all_spans_verified';
   - nessun gap entailment, nessun gap SQ.
7. **`verdict='entailed'` clean lato entailment, MA SQ warning** → decision='approved', reason_code='all_spans_verified_with_warnings', gap `source_quality_warning` ma NESSUN gap `entailment_*` (il Branch entailment cede al branch SQ).
8. **Latest version wins**:
   - v1=`entailed`, v2=`contradicted` → block (v2 vince via `ORDER BY version_no DESC`).
   - v1=`contradicted`, v2=`entailed` → clean.
9. **Multi-evidence worst-on-block**: 1 span con 2 evidence_spans, una con `entailed` e l'altra con `contradicted` → block.
10. **Multi-evidence any-on-warn**: 1 span con 2 evidence_spans, una con `entailed` e l'altra con `uncertain` → warning, NO block.
11. **Priorità CVE-lite > Entailment**: 1 span non-verified-backed con un `entailment_block` seedato → reason_code='unverified_spans_present' (NON `entailment_block`); nessun gap `entailment_*`.
12. **Priorità Entailment > Source Quality**: 1 span con `verdict='contradicted'` (block) AND `overall_quality='unsuitable'` (block) → reason_code='entailment_block' (NON `source_quality_block`); MA il Gate emette ENTRAMBI i gap (audit completo).
13. **Source Quality block invariato in altri scenari**: 1 span con `verdict='entailed'` AND `overall_quality='unsuitable'` → reason_code='source_quality_block'; nessun gap `entailment_*` (solo `source_quality_block`).
14. **Warning SQ + warning Entailment coesistono**: 1 span con `unknown` SQ e `uncertain` entailment → reason_code='all_spans_verified_with_warnings'; UN gap `entailment_warning` + UN gap `source_quality_warning`.
15. **Idempotenza redelivery**:
    - Second-invoke del Gate sullo stesso draft → stessa decision, nessun nuovo gap, stesso `final_gate_report_id`.
16. **No mutazione di `claim_entailment_checks`**: pre/post count di righe per il task uguale.
17. **No nuovo audit aggiuntivo dal Gate**: numero di `audit_records` per il task uguale prima/dopo (escludendo i `final_gate_started`/`final_gate_completed` già emessi dal consumer).
18. **`payload.mock` non altera la decisione**: una riga con `payload.mock=true` ha gli stessi effetti decisionali di una senza il flag (semantica MVP-0).

### 11.3 8.8A-GATE-FLOW — `tests/test_phase_8_8_entailment_flow.py`

Realistic flow test root-level, mirror di `tests/test_phase_8_7_source_quality_flow.py`. Due test indipendenti:

1. **Warning flow** (`test_phase_8_8a_entailment_warning_flow_end_to_end`):
   - Setup: tenant/user seedati, project/document/task via API HTTP.
   - Document con frasi factual (digits) per attivare l'extractor.
   - Pipeline drive: `_dispatch.handle_event` con FakeRedis.
   - Mock checker reale produce `entailed` (containment) o `uncertain` (default) per le pair generate dall'extractor 8.3.
   - Verifica: decision='approved', reason_code='all_spans_verified_with_warnings'; almeno un gap `entailment_warning` o `source_quality_warning` collegato al draft; published_answers v1.
   - Verifica audit chain: `task.entailment_checked` strettamente tra `task.source_quality_assessed` e `task.compiling`; chain valida via `verify_task_audit_chain`.
   - Verifica read API (futura): se 8.8A-READ è chiuso prima di 8.8A-FLOW, `/api/v1/tasks/{id}/entailment` ritorna 200 con summary counts. Altrimenti, validazione DB-only.

2. **Block flow** (`test_phase_8_8a_entailment_block_flow_end_to_end`):
   - Setup analogo + **monkeypatch** del simbolo `_wapp.consumers.task_created.run_claim_entailment_checks` con uno stub orchestrator che inserisce v1 di `claim_entailment_checks` con `verdict='contradicted'` per ogni pair (pattern 8.7H).
   - Lo stub è necessario perché il **mock checker reale non emette `contradicted`** (verificato in §1.6).
   - Lo stub deve ritornare il dict counts canonico atteso dal consumer.
   - Verifica: decision='rejected', reason_code='entailment_block'; almeno un gap `entailment_block`; audit terminale `task.publication_held`; NO `published_answers`.
   - Verifica read API: `/published-answer` ritorna 404 RESOURCE_NOT_FOUND con `details.resource='published_answers'`.

### 11.4 Test 8.7G da aggiornare (regression management)

I 13 scenari esistenti di `test_final_answer_gate_source_quality.py` potrebbero produrre regressioni quando il Gate inizia a leggere `claim_entailment_checks`. Strategia:

- I test 8.7G seedano direttamente `source_quality_assessments`, NON seedano `claim_entailment_checks`.
- Con 8.8A-GATE attivo, ogni span verified-backed senza entailment check produrrà un warning `entailment_missing_check`.
- **Decisione operativa** (per 8.8A-GATE-CODE): nei test 8.7G esistenti dove si vuole testare solo SQ, va aggiunto un helper che pre-seeda `claim_entailment_checks` con `verdict='entailed'` per ogni pair `(entry, span)` rilevante. Helper LOCALE al file di test (no cross-file imports).
- In alternativa: aggiornare i test 8.7G ad aspettare la coesistenza del warning entailment dove pertinente. Questa è la strada più realistica e meno invasiva.
- Lo scenario 6 di 8.7G ("contradicted_by_stronger_source produces block") cambia: la decision resta rejected, ma il reason_code potrebbe diventare `source_quality_block` ancora (perché l'entailment seedato è `entailed`), oppure `entailment_block` se uno scenario combinato è test-driven. Il test va verificato caso per caso.

### 11.5 Test del consumer

Nessuna modifica richiesta a `test_task_created_entailment_step.py` e `test_consumer_with_documents.py`: il consumer non cambia in 8.8A-GATE.

---

## 12. Rischi residui

Rischi specifici di 8.8A-GATE, da documentare in `PHASE_8_8A_*` e (in DOC block) in `PROJECT_STATE.md`.

### 12.1 Rischi semantici

- **Il mock heuristic non è NLI reale.** Le tre regole regex-based del mock non si avvicinano a NLI/entailment vero. Il Gate decide su un input che è esso stesso una euristica.
- **`not_supported` da numeric mismatch può essere falso positivo.** Mitigato dalla scelta P1 (warning, non block) in MVP-0. Diventerà un rischio reale se P2 venisse attivata.
- **`contradicted` non viene prodotto dal mock attuale.** Il branch entailment_block del Gate sarà attivabile in MVP-0 solo via seed/stub di test. Mirror del Branch C' di 8.7G. In produzione mock-driven, il Branch entailment_block è dormiente.
- **Una quote presente non implica che la quote sostenga il claim**, e una fonte citata non implica un claim vero. Da preservare in tutti i docs.

### 12.2 Rischi architetturali

- **Crescita del Gate**. Il file `final_answer_gate.py` ha ~800 righe post-8.7G con cinque branch. 8.8A-GATE-CODE aggiunge un sesto branch + nuova query LATERAL. Va valutato in 8.8A-GATE-CODE se splittare in `final_answer_gate.py` (orchestrazione) + `final_answer_gate_policy.py` (policy matrix entailment + SQ). **Raccomandazione**: rimandare lo split a un blocco di refactor dedicato; non farlo nello stesso commit di 8.8A-GATE-CODE.
- **Priorità Gate fragile**: `unverified_spans_present > entailment_block > source_quality_block` deve restare testato esplicitamente in 8.8A-GATE-CODE. Un refactor incauto del Gate potrebbe invertirla. Mitigazione: scenari 11 e 12 di §11.2.
- **Numerazione migration**: 0010 occupato → retention futura distruttiva slitta a 0011. Costo accettabile.

### 12.3 Rischi operativi

- **Overblocking**: P1 in MVP-0 minimizza. Nessuno span verrà bloccato per entailment finché un seed produce `contradicted`.
- **Underblocking**: P1 lascia `not_supported` come warning. Coerente con la promessa "MVP-0 non chiude il problema".
- **Rumorosità dei warning**. Ogni span verified-backed potenzialmente accumula un `source_quality_warning` (Branch B' 8.7G) E un `entailment_warning` (se mock produce `uncertain` o `not_supported`). Il numero di `coverage_gap_statements` per draft cresce. La UI futura deve sapere come consolidare per dimensione.
- **Task storici senza `claim_entailment_checks`** (processati prima di 8.8A-WORKER): generano warning `entailment_missing_check`. Coerente con il comportamento 8.7G per Source Quality (`source_quality_missing_assessment`). Nessun backfill previsto.
- **Falso senso di sicurezza dei warning**: un task `published` con warning `entailment_uncertain` può essere percepito come "approvato senza riserve". La read API `/final-gate-report` espone i gap, ma la UI deve renderli visibili.
- **`payload.mock` ignorato dal Gate**: scelta consapevole MVP-0. Una futura migration potrebbe renderlo discriminante (es. "block solo se `payload.mock=false`"); per ora no.

### 12.4 Rischi documentali

- **"entailed mock" continua a non significare "claim vero".** Da martellare in `PROJECT_STATE.md` post-8.8A-GATE e nei docs futuri.
- **`entailment_block` con il mock NON si attiva spontaneamente** in produzione. Va detto chiaramente in `README.md` post-8.8A-GATE-DOC.
- **Reason_code di default approved diventa `all_spans_verified_with_warnings`** in molti più scenari (oggi era solo per SQ, ora anche per entailment). Consumer esterni che testano su stringa esatta `all_spans_verified` vanno aggiornati.

### 12.5 Rischi ereditati (invariati)

- Branch C' di 8.7G resta dormiente con mock attuale.
- `coverage_gap_statements` senza trigger append-only (debito 8.7G).
- RBAC/redaction non implementata su payload JSONB esposti.
- Retention distruttiva non implementata.
- No Citation-to-Claim Validator (8.8B).
- No Contradiction Detector reale (8.8C).
- No Final Answer Sentence Gate (8.8D).
- No Anti-Hallucination Report API (8.8E).
- No External Verification / Web-RAG (8.9).
- No worker main loop reale negli end-to-end test (FakeRedis + dispatch.handle_event diretta).
- No API read entailment ancora esposta.

---

## 13. Decisione finale

### 13.1 Architettura raccomandata

1. **Policy MVP-0** = **P1** (= P4): `contradicted` blocca; `not_supported`, `partially_supported`, `uncertain`, missing → warning; `entailed` → clean. Versionata come `mvp0_entailment_gate_policy` v0.1.0.
2. **Priorità Gate**: `no_verified_claims > unverified_spans_present > entailment_block > source_quality_block > approved_with_warnings > approved_clean`.
3. **Nuovi `coverage_gap_statements.kind`**: `entailment_block` (severity `block`), `entailment_warning` (severity `warn`). Migration `0010_coverage_gap_entailment.sql` da scrivere in 8.8A-GATE-SCHEMA, NON in questo blocco.
4. **Nuovo `final_gate_reports.reason_code`**: solo `entailment_block`. Riusare `all_spans_verified_with_warnings` per approved con warning misti.
5. **Aggregazione**: worst-on-block, any-on-warn (pattern 8.7G).
6. **Read-only**: il Gate consulta `claim_entailment_checks` in lettura; nessuna mutazione.
7. **Audit**: nessun nuovo evento audit dal Gate.
8. **Identità policy** stampata in `details` di ogni gap entailment: `{"policy": {"name": "mvp0_entailment_gate_policy", "version": "0.1.0"}}`.

### 13.2 File da NON modificare in 8.8A-GATE-CODE

- `migrations/0001_foundation.sql` … `0009_claim_entailment_checks.sql` (immutabili).
- `apps/worker/app/services/cve_lite.py`.
- `apps/worker/app/services/extractor.py`.
- `apps/worker/app/services/compiler.py`.
- `apps/worker/app/services/source_quality_evaluator.py`.
- `apps/worker/app/services/source_quality_orchestrator.py`.
- `apps/worker/app/services/claim_entailment_checker.py` (8.8A-SERVICE, invariato).
- `apps/worker/app/services/claim_entailment_orchestrator.py` (8.8A-ORCHESTRATOR, invariato).
- `apps/worker/app/consumers/task_created.py` (8.8A-WORKER, invariato).
- `apps/api/app/routes/source_quality.py`.
- `packages/shared/evidencefirst_shared/schemas.py` (lo Shared module entailment è già completo).

### 13.3 File previsti per i sotto-blocchi futuri

| Sotto-blocco | File previsto |
|---|---|
| 8.8A-GATE-SCHEMA | `migrations/0010_coverage_gap_entailment.sql` + `tests/test_migration_0010_coverage_gap_entailment.py` |
| 8.8A-GATE-CODE | `apps/worker/app/services/final_answer_gate.py` esteso + `apps/worker/tests/test_final_answer_gate_entailment.py` + aggiornamenti ai test 8.7G dove il warning entailment_missing_check appare |
| 8.8A-GATE-FLOW | `tests/test_phase_8_8_entailment_flow.py` (warning + block via stub) |
| 8.8A-DOC | aggiornamento `PROJECT_STATE.md`, `README.md`, eventuale nuovo `PHASE_8_8_PLAN.md` |

### 13.4 Vincoli sempre validi

- Nessun provider AI reale.
- `PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0`.
- Closed Corpus only.
- SQLAlchemy 2.0 Core, bound parameters.
- Append-only enforced a DB su tutte le tabelle storiche.
- Idempotenza redelivery via UNIQUE composito.
- Test rerun-safe (UUID/hash unici per invocazione).

---

## FILE_COMPLETATI

- `PHASE_8_8A_GATE_PRE.md`

## FILE_NON_MODIFICATI

- `migrations/*` (incluse 0001–0009)
- `apps/*` (worker, API, web)
- `packages/*` (shared)
- `tests/*`
- `README.md`
- `PROJECT_STATE.md`
- `PHASE_8_8A_PRE.md`
- `PHASE_8_7_PLAN.md`

## FILE_DA_FARE_PROSSIMO_BLOCCO

- **8.8A-GATE-SCHEMA**: `migrations/0010_coverage_gap_entailment.sql` per estendere `coverage_gap_statements.kind` con `entailment_block` e `entailment_warning`; `tests/test_migration_0010_coverage_gap_entailment.py` con i 12 test enumerati in §11.1.

## RISCHI_RESIDUI

- Il mock heuristic non è NLI reale; tre regole sintattiche su containment + numeri non si avvicinano a entailment semantico. `payload.mock=true` da onorare in tutti i consumatori.
- `contradicted` non viene prodotto dal mock attuale: il branch `entailment_block` sarà attivabile in MVP-0 solo via seed/stub di test (mirror 8.7H per Branch C' di Source Quality).
- `not_supported` da numeric mismatch può essere falso positivo: P1 (warning, non block) in MVP-0 mitiga; P2 sarà valutabile solo con checker reale.
- Priorità `unverified_spans_present > entailment_block > source_quality_block` è invariante critica: testata esplicitamente in 8.8A-GATE-CODE scenari 11 e 12, ma fragile a refactor del Gate.
- Numerazione migration: 0010 occupato → retention distruttiva slitta a 0011 o successivo.
- I test 8.7G esistenti (`test_final_answer_gate_source_quality.py`) andranno aggiornati per gestire il nuovo warning `entailment_missing_check` su span verified-backed senza entailment seed. Strategia: helper locale di seed `entailed` o aspettarsi il warning coesistente.
- Crescita del `final_answer_gate.py`: post-8.8A-GATE-CODE avrà sei branch + due query LATERAL. Refactor in modulo policy separato rinviato a blocco dedicato.
- Reason_code di default approved diventa più frequentemente `all_spans_verified_with_warnings`: consumer esterni che testano su `all_spans_verified` esatto andranno aggiornati.
- "entailed mock" continua a non significare "claim vero"; "una fonte citata non implica un claim vero"; "una quote presente non implica che la quote sostenga il claim": disclaimer da preservare in tutti i docs post-8.8A-GATE-DOC.
- Task pre-8.8A-WORKER senza righe `claim_entailment_checks` genereranno warning `entailment_missing_check`: rumoroso ma non bloccante; nessun backfill previsto.
- Nessuna API read entailment ancora esposta (8.8A-READ rinviata).
- Tutti i rischi ereditati da 8.7G/H e 8.8A-PRE restano invariati: no Citation-to-Claim Validator (8.8B), no Contradiction Detector reale (8.8C), no Final Answer Sentence Gate (8.8D), no Anti-Hallucination Report API (8.8E), no External Verification (8.9), RBAC/redaction non implementata, retention distruttiva non implementata, trigger append-only su `coverage_gap_statements` mancante, worker main loop reale non testato negli end-to-end.

---

## Note tecniche su file dichiarati come "letti" dal prompt ma non presenti nel contesto

Per onestà di documentazione:

- `PHASE_8_8A_PRE.md`: presente. Letto integralmente. Coerente con lo stato dello schema 0009.
- `PROJECT_STATE.md`: presente, ma riflette lo stato post-8.7H (commit `b70ef8f`); il prompt indica come main attuale `357ee65` ("Integrate claim entailment into task pipeline"). Le sezioni 8.8A non sono riflesse nel PROJECT_STATE letto. Lo stato 8.8A è stato dedotto dai file di codice e test effettivamente forniti (i quali sono coerenti con lo stato 8.8A-WORKER completato).
- `PHASE_8_7_PLAN.md`: presente, letto.
- `README.md`: presente, riflette lo stato post-8.7H. Non riflette 8.8A.
- `migrations/0005_answers_gate.sql`, `0008_coverage_gap_source_quality.sql`, `0009_claim_entailment_checks.sql`: presenti, letti.
- `packages/shared/evidencefirst_shared/schemas.py`: presente, letto. Contiene già `ClaimEntailmentCheckRead` e `SOURCE_ENTAILMENT_VERDICT_VALUES`.
- `apps/worker/app/services/final_answer_gate.py`: presente, letto. Riflette stato 8.7G (no riferimenti a `claim_entailment_checks`).
- `apps/worker/app/services/claim_entailment_checker.py`: presente, letto.
- `apps/worker/app/services/claim_entailment_orchestrator.py`: presente, letto.
- `apps/worker/app/consumers/task_created.py`: presente, letto. Lo step 8.8A è già integrato.
- Test files: presenti e letti (service, orchestrator, integration, gate SQ, consumer, migrations 0008/0009).

Note di contesto:

PHASE_8_7G_PRE.md e PHASE_8_7H_PRE.md sono documenti storici delle fasi 8.7G/8.7H. Questo documento usa come fonte tecnica primaria lo stato corrente del codice e delle migration post-8.8A-WORKER.

PHASE_8_8A_GATE_PRE.md è il target nuovo di questo blocco.
