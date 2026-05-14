# PHASE_8_7_PLAN — Source Quality Evaluator / Evidence Quality Layer

Documento di **piano architetturale** per la Fase 8.7 dell'Evidence-First MVP-0. La fase introduce il primo modulo dedicato alla valutazione della **qualità delle fonti** che supportano i claim del Claim Ledger.

> **Stato di questo documento: piano, NON implementato.**
>
> Nessun file di codice applicativo, nessuna migration, nessun test, nessun aggiornamento di `PROJECT_STATE.md` o `README.md` viene prodotto in 8.7A. L'unico output di 8.7A è questo file `PHASE_8_7_PLAN.md`. I blocchi successivi (8.7B, 8.7C, …) sono enunciati qui ma non implementati.

**Commit di partenza**: `7cbd45ae416ead0b2f5221ace4925dee374fa0c9`.

**Collegamento logico**: la Fase 8.6 ha reso **osservabili** via HTTP read-only gli eventi lifecycle (`published_answer_lifecycle_events`) e la propagazione della source loss (`source_loss_events`, `source_loss_propagation_records`). La Fase 8.7 deve iniziare a **valutare la qualità** delle fonti che il sistema usa per supportare claim. La 8.6 osserva; la 8.7 valuta.

---

## 1. Stato di partenza

Al commit `7cbd45a` il repo offre i seguenti elementi rilevanti per la 8.7. Tutto ciò che segue è verificabile leggendo i file indicati; nessuno di questi elementi viene modificato dalla 8.7A (questo piano).

### 1.1 Schema DB già applicato (migrations 0001–0006)

- **Storage e documenti** (`0002_storage.sql`, `0003_documents.sql`):
  - `storage_blobs`, `storage_objects` (content-addressed, deduplicato, refcount-based).
  - `uploaded_documents` con colonne `tier` (`user_provided` | `system_generated`), `language`, `mime_type`, `content_hash`, `size_bytes`, `created_by`.
  - `document_versions` (con `version_kind ∈ {original, parsed, normalized}`).
  - `document_chunks` (CHECK `dc_origin_xor` impone `document_version_id IS NOT NULL`).
  - `evidence_spans` (append-only via trigger; `quote`, `quote_hash`).
  - `prompt_injection_flags` (placeholder, vuoto in MVP-0).

- **Claim Ledger** (`0004_claim_ledger.sql`):
  - `logical_claims` con UNIQUE `(task_id, canonical_claim_hash)`.
  - `raw_claims`, `classified_claims`.
  - `claim_ledger_entries` (append-only, supersede via `claim_lineage.relation_kind='supersedes'`).
  - `claim_evidence_links` (CHECK `cel_origin_xor`: `evidence_span_id` NOT NULL, `retrieved_source_span_id` NULL).
  - `verification_records` con `check_kind ∈ {csv, cve_lite, nli, judge}`, UNIQUE `(claim_ledger_entry_id, check_kind, check_name)`.
  - `contradiction_records` (placeholder, vuoto).
  - `claim_support_links` (placeholder per `basis|assumption|precondition|counterposition`).
  - `human_review_requests` (placeholder).

- **Answers / Gate / Published** (`0005_answers_gate.sql`):
  - `agent_runs` (`run_kind ∈ {compile_draft, final_answer_gate}`).
  - `draft_final_answers`, `final_answer_spans` (append-only), `final_answer_span_claim_links`.
  - `final_gate_reports` (append-only, UNIQUE per draft, FK composita verso draft).
  - `published_answers` con `status ∈ {published, withdrawn, superseded}`.
  - `coverage_gap_statements` con `kind ∈ {unverified_claim, missing_evidence, out_of_scope, source_loss}`.

- **Lifecycle e source loss** (`0006_lifecycle.sql`):
  - `published_answer_lifecycle_events` (append-only).
  - `source_loss_events` con `loss_kind ∈ {source_deleted, source_access_lost, quote_mismatch, document_replaced, policy_retraction}`.
  - `source_loss_propagation_records` con `propagation_kind ∈ {claim_marked_unverifiable, published_answer_impacted, no_claims_impacted, no_active_published_answers_impacted}`, `status ∈ {recorded, skipped, failed}`, idempotenza via 4 partial unique indexes ristretti a `status IN ('recorded', 'skipped')`.

### 1.2 Endpoint API attivi

Quelli rilevanti per la 8.7 (read-only, dovranno coesistere con i futuri endpoint 8.7F):

- `POST /api/v1/projects/{id}/documents` — upload `.txt`/`.md`.
- `GET /api/v1/documents/{id}` — single document read.
- `GET /api/v1/documents/{id}/chunks` — lista chunks.
- `GET /api/v1/claims/{logical_id}/evidence` — aggregato latest entry + links + verification_records.
- `GET /api/v1/source-loss-events/{id}` — single source_loss_event (8.6B).
- `GET /api/v1/source-loss-events/{id}/propagation` — propagation rows (8.6C).
- `GET /api/v1/tasks/{task_id}/source-loss-events` — task-level listing S1 ∪ S2 (8.6D).

### 1.3 Servizi worker attivi

- `services/extractor.py` (mock-driven, sentence-split deterministico).
- `services/cve_lite.py` (verifica `quote ∈ chunk_text` AND `sha256(quote) == quote_hash`, scrive `verification_records` con `check_kind='cve_lite'`).
- `services/final_answer_gate.py` (regola "verified-backed" come definita in 8.4).
- `services/source_loss_propagator.py` (append-only su `source_loss_propagation_records`, mai mutazione di `published_answers.status`).
- `services/published_answer_lifecycle.py` (unica entità autorizzata a mutare `published_answers.status` per il path withdrawal).

### 1.4 Chiarimento critico sullo stato attuale

Tre affermazioni che vincolano la progettazione della 8.7:

1. **Una `evidence_span` collegata a un claim NON significa automaticamente fonte affidabile.** Significa solo che esiste un legame referenziale tra un claim del ledger e un quote di un chunk persistito nel corpus chiuso dell'utente. L'autorevolezza, la freschezza, l'indipendenza e la rilevanza del documento sottostante NON sono mai state valutate.

2. **`verified_fact` (state del Claim Ledger) oggi significa esclusivamente "supporto verificato secondo il CVE-lite mock-driven"**: la quote esiste nel chunk con il suo hash atteso. Non significa che la fonte sia di qualità. Significa che il claim ha almeno un supporto testuale presente e checksummed.

3. **Source loss gestisce la perdita o invalidazione di una fonte** (`source_deleted`, `source_access_lost`, `quote_mismatch`, `document_replaced`, `policy_retraction`), non la qualità iniziale della fonte. Un documento perfettamente accessibile e con quote intatte può comunque essere debole, secondario, datato o non indipendente — nessuno di questi giudizi è oggi possibile.

La 8.7 deve introdurre questi giudizi come **dimensioni separate**, senza confonderle con la verifica testuale o con la source loss.

---

## 2. Definizione realistica di source quality

Per MVP-0 "source quality" è un giudizio **strutturale, multi-dimensionale, dichiarato e append-only/versionato** su una fonte. Le dimensioni sotto sono progettate per essere **ortogonali**: ognuna risponde a una domanda diversa e nessuna implica le altre.

### 2.1 Tassonomia (codomini proposti)

I codomini sono fissati come stringhe-enum a livello applicativo e (per il blocco 8.7B) a livello CHECK di DB.

#### `source_type`
Domanda: che tipo di artefatto è la fonte?
Codominio:
- `user_document`
- `web_page`
- `academic_paper`
- `official_document`
- `database_record`
- `news_article`
- `blog`
- `forum`
- `unknown`

Note: in MVP-0 closed-corpus, la quasi totalità delle fonti è `user_document`. Il codominio prevede già i valori `web_page`/`news_article`/etc. perché la stessa tassonomia deve essere riusabile in fasi che introdurranno web retrieval o tier `system_generated` non-corpus. La classificazione resta dichiarata, non dedotta da una rete.

#### `source_role`
Domanda: la fonte è primaria, secondaria, terziaria o non classificabile?
Codominio:
- `primary` (es. articolo originale, sentenza, dataset originale)
- `secondary` (analisi/sintesi/citazione di una primaria)
- `tertiary` (enciclopedia generalista, riassunto di sintesi)
- `unclear`

#### `authority_level`
Domanda: quanto è autorevole l'editore/autore rispetto al dominio del claim?
Codominio: `high` | `medium` | `low` | `unknown`.

Note: la decisione su cosa significa "high" è policy-dipendente e va portata in una tabella `source_quality_policies` (vedi §4) per restare auditabile.

#### `independence_level`
Domanda: la fonte è indipendente dal soggetto del claim, oppure è interna/affiliata/auto-dichiarata?
Codominio: `independent` | `affiliated` | `self_reported` | `unknown`.

#### `freshness`
Domanda: la fonte è recente/aggiornata rispetto al claim?
Codominio: `current` | `recent` | `stale` | `undated` | `not_time_sensitive`.

Note: distinguere `undated` da `stale` evita di penalizzare ingiustamente fonti senza data esplicita ma di natura non time-sensitive.

#### `relevance`
Domanda: la fonte supporta direttamente il claim, ne supporta solo il contesto, o è marginale?
Codominio: `direct_support` | `contextual_support` | `weak_support` | `irrelevant`.

#### `extract_quality`
Domanda: come si comporta l'estratto (`evidence_spans.quote`) rispetto al chunk e al claim?
Codominio:
- `exact_quote_match` (la quote esiste verbatim nel chunk; CVE-lite passa)
- `paraphrase_match` (la quote è una parafrasi corretta)
- `partial_match` (overlap parziale)
- `quote_mismatch` (la quote non si trova nel chunk; può coincidere con `loss_kind='quote_mismatch'` in `source_loss_events`, vedi §7)

Note: `quote_mismatch` qui è un fatto di qualità dell'estratto; lo stesso fatto in `source_loss_events` è un fatto di perdita di fonte. Le due tabelle resteranno separate e ognuna manterrà il proprio significato.

#### `contradiction_status`
Domanda: la fonte è in conflitto con altre fonti note?
Codominio:
- `no_known_contradiction`
- `contradicted_by_stronger_source`
- `conflicting_sources`
- `unchecked`

Note: in 8.7 il valore di default è `unchecked`. Il detector reale di contraddizioni (`contradiction_records` placeholder) è fuori scope.

#### `overall_quality`
Domanda: in una sola etichetta, qual è il giudizio complessivo?
Codominio:
- `strong`
- `adequate`
- `weak`
- `unsuitable`
- `unknown`

Note: NON deve essere derivato automaticamente da una formula opaca. Deve essere prodotto da una funzione di policy esplicita (vedi §4 e §6), e la funzione stessa deve essere parte dell'audit (versione di policy registrata).

#### `confidence`
Domanda: quanto siamo sicuri di questo assessment?
Codominio: `DOUBLE PRECISION` in `[0.0, 1.0]`.

Note: **è uno score interno, non una verità assoluta**. Non deve essere usato dal Final Answer Gate come unica chiave decisionale; va consumato in combinazione con `overall_quality` e con la policy.

### 2.2 Append-only / versionato

Tutte le dimensioni sopra DEVONO essere registrate in modo:

- **Append-only**: nessuna mutazione distruttiva del valore precedente. Un assessment esistente non viene aggiornato; viene supersedeato da uno nuovo.
- **Versionato**: ogni assessment ha un `version_no` monotonicamente crescente per la stessa `target` (es. `evidence_span_id` o `document_id`), oppure è collegato a un `policy_version` esplicito.

Motivazioni:
- coerenza con `claim_ledger_entries`, `evidence_spans`, `audit_records`, `published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records` (tutte append-only via trigger);
- auditabilità completa nel tempo;
- abilità di rieseguire l'assessment con una policy diversa senza riscrivere la storia;
- compatibilità con un futuro detector di drift (un assessment passato da `strong` a `weak` ha valore diagnostico).

---

## 3. Cosa la 8.7 NON è (rigorosamente)

Per evitare confusione di scope, queste sono le distinzioni che il piano impone:

1. **Source quality ≠ claim correctness.** Un claim può essere falso anche se la fonte è autorevole; un claim può essere corretto anche se la fonte è debole.
2. **Source quality ≠ evidence support.** Un legame `claim_evidence_links` ben formato non implica qualità della fonte; un legame mal formato non implica fonte debole.
3. **Source quality ≠ verification outcome.** `verification_records.outcome='pass'` significa "il check CVE-lite è passato", non "la fonte è affidabile".
4. **Source quality ≠ source loss.** La perdita di fonte è un evento (la fonte non è più accessibile / la quote non corrisponde più); la qualità è un giudizio sulla fonte mentre è presente.
5. **Source quality ≠ final publication eligibility.** L'eligibility è composta da: correctness, evidence support, source quality, policy gate. Sono quattro assi separati.

Questa distinzione è la prima invariante semantica della Fase 8.7.

---

## 4. Possibile modello dati (confronto di opzioni)

La 8.7A NON scrive migration. Qui si presentano tre opzioni, con un confronto e una **raccomandazione non implementata**.

In tutti i casi la migration target sarebbe `migrations/0007_source_quality.sql`, che resta da scrivere e applicare nel blocco 8.7B.

### 4.1 Opzione A — Nuova tabella dedicata `source_quality_assessments`

Schema indicativo (NON da scrivere ora):

```sql
-- migrations/0007_source_quality.sql  (BOZZA INDICATIVA, NON DA SCRIVERE IN 8.7A)
CREATE TABLE source_quality_assessments (
    id                      UUID PRIMARY KEY DEFAULT app_new_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project_id              UUID REFERENCES projects(id) ON DELETE RESTRICT,
    -- Target della valutazione: esattamente UNO tra i tre seguenti deve essere non-null.
    evidence_span_id        UUID REFERENCES evidence_spans(id) ON DELETE RESTRICT,
    document_chunk_id       UUID REFERENCES document_chunks(id) ON DELETE RESTRICT,
    document_id             UUID REFERENCES uploaded_documents(id) ON DELETE RESTRICT,
    -- Versione monotona per (target_kind, target_id).
    version_no              INTEGER NOT NULL CHECK (version_no >= 1),
    -- Dimensioni (codomini come da §2.1).
    source_type             TEXT NOT NULL CHECK (source_type IN (...)),
    source_role             TEXT NOT NULL CHECK (source_role IN (...)),
    authority_level         TEXT NOT NULL CHECK (authority_level IN (...)),
    independence_level      TEXT NOT NULL CHECK (independence_level IN (...)),
    freshness               TEXT NOT NULL CHECK (freshness IN (...)),
    relevance               TEXT NOT NULL CHECK (relevance IN (...)),
    extract_quality         TEXT NOT NULL CHECK (extract_quality IN (...)),
    contradiction_status    TEXT NOT NULL CHECK (contradiction_status IN (...)),
    overall_quality         TEXT NOT NULL CHECK (overall_quality IN (...)),
    confidence              DOUBLE PRECISION CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    -- Provenienza dell'assessment.
    evaluator_name          TEXT NOT NULL,
    evaluator_version       TEXT NOT NULL,
    policy_version_id       UUID REFERENCES policy_versions(id) ON DELETE RESTRICT,
    -- Idempotenza e payload.
    idempotency_key         TEXT NOT NULL,
    payload                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT sqa_target_xor CHECK (
        ((evidence_span_id IS NOT NULL)::int +
         (document_chunk_id IS NOT NULL)::int +
         (document_id IS NOT NULL)::int) = 1
    ),
    CONSTRAINT sqa_version_evidence_uq UNIQUE (evidence_span_id, version_no)
        -- equivalenti UNIQUE per i due altri target via partial unique indexes
);
-- Trigger append-only standard via reject_modify_append_only.
```

#### Opzione A — Pro / contro

**Pro:**
- Separazione netta dal Claim Ledger: nessun rischio di confusione semantica.
- Schema pulito, leggibile, evolutivo.
- Append-only banale (trigger esistente `reject_modify_append_only`).
- Append-only versionato compatibile con la convenzione di `claim_ledger_entries`.
- Permette assessment a granularità diverse (span / chunk / document) senza join sintetici.
- Permette di rieseguire assessment con `policy_version_id` diverso senza modificare le vecchie righe.
- Testabilità eccellente: il modulo si testa in isolamento, senza interferire con CVE-lite o gate.

**Contro:**
- Una tabella nuova in più.
- Richiede una nuova migration `0007_source_quality.sql` con tutti i suoi vincoli, partial unique indexes per il target XOR, e trigger.
- Lookup da claim → assessment richiede join via `claim_evidence_links → evidence_spans → source_quality_assessments`, oppure aggregazioni a livello document.
- Le tre forme di target (span/chunk/document) richiedono tre partial unique indexes per la versione monotona.

**Impatto su Claim Ledger:** nessun impatto strutturale. Le entry del ledger restano append-only e non vengono toccate. L'assessment vive in un'altra tabella e viene join-ato dai lettori (gate, API).

**Impatto su Final Answer Gate:** nessuna modifica a `final_gate_reports` o `coverage_gap_statements`. La policy futura del gate (vedi §6) consulterà gli assessment via join e potrà emettere `coverage_gap_statements` con `kind='source_loss'` o un nuovo `kind` da introdurre più avanti — l'introduzione di un nuovo `kind` NON è in 8.7B, è un blocco separato.

**Compatibilità con append-only:** trigger standard.

**Testabilità:** alta (modulo isolato).

**Rischio di migration:** medio. Il CHECK ternario `sqa_target_xor` e i tre partial UNIQUE devono essere scritti correttamente al primo colpo; un secondo `0008` di patch è da evitare.

### 4.2 Opzione B — Riuso di `verification_records` con nuovi `check_kind`

Schema indicativo: NESSUNA migration nuova. Si estendono i valori del CHECK su `verification_records.check_kind` per accogliere check tipo `source_authority`, `source_independence`, `source_freshness`, `source_relevance`, `source_overall_quality`.

**Pro:**
- Zero nuove tabelle.
- Pattern già noto: ogni dimensione diventa un check separato (`check_kind`, `check_name`).
- Audit naturale via `verification_records.payload`.

**Contro:**
- Confonde semanticamente "verifica testuale del claim" con "valutazione della fonte". Va contro l'invariante §3.3.
- `verification_records` è scoped a `claim_ledger_entries`; valutazioni a livello document sono goffe (richiedono claim "proxy" o sono impossibili senza estendere lo schema).
- L'estensione del CHECK `check_kind` è una modifica vincolante di una tabella esistente: rompe la promessa di immutabilità delle migration 0004 (modifica del CHECK richiede DROP/ADD CONSTRAINT).
- Difficile rappresentare versioning monotono di un "assessment complessivo" se ogni dimensione è una riga separata.
- Difficile esprimere un `overall_quality` aggregato senza una semantica artificiale di "riga rappresentativa".

**Impatto su Claim Ledger:** minimo strutturalmente, alto semanticamente (mischia due concetti distinti).

**Impatto su Final Answer Gate:** la regola "verified-backed" si è fondata sulla nozione "esiste un link verso latest entry con state `verified_fact`". L'estensione di `check_kind` rischia di creare ambiguità: il gate dovrebbe filtrare per `check_kind='cve_lite'` esplicitamente, altrimenti varrebbe anche un check di authority. È un cambio di contratto invisibile.

**Compatibilità con append-only:** preservata.

**Testabilità:** ridotta perché i test esistenti su `verification_records` dovrebbero essere aggiornati per non assumere `check_kind ∈ {csv, cve_lite, nli, judge}`.

**Rischio di migration:** alto (modifica CHECK su tabella esistente vincolata in 0004).

### 4.3 Opzione C — Ibrida: tabella dedicata + audit dei check

Combina le due. Si crea `source_quality_assessments` per il giudizio aggregato e versionato (come Opzione A) e, **opzionalmente**, si registrano i singoli check di policy in `verification_records` con `check_kind` esistenti SOLO se semanticamente compatibili (es. un check "evidence_quote_present" potrebbe vivere in `cve_lite`). I check di authority/freshness/indipendence NON entrano in `verification_records`: vivono solo nel payload dell'assessment.

**Pro:**
- Mantiene la separazione semantica netta (Opzione A).
- Permette audit fine-grained di singoli sotto-check tramite `verification_records` quando questi sono semanticamente "verifica testuale".
- Nessun bisogno di estendere il CHECK su `check_kind`.

**Contro:**
- Modello dati più complesso da comprendere e documentare.
- Rischio di duplicazione concettuale tra `payload` dell'assessment e righe `verification_records`.

**Impatto su Claim Ledger / Gate / append-only / testabilità / rischio migration:** come Opzione A, più la complessità descritta.

### 4.4 Raccomandazione (non implementata)

**Raccomandata: Opzione A.**

Motivazioni:
- L'Opzione A mantiene rigida la distinzione delle quattro categorie elencate in §3.
- Non altera in alcun modo le migration 0001–0006.
- È compatibile con qualunque evoluzione futura (web search, contradiction detector, retention).
- Le contro (una tabella in più, partial unique indexes per il target XOR) sono pagamenti contenuti e una-tantum.

L'Opzione C resta un'alternativa accettabile se in fase di scrittura della migration emergono casi reali in cui un sotto-check beneficia di vivere come `verification_records`. L'Opzione B è sconsigliata.

---

## 5. Interazione con Claim Ledger

La 8.7 NON cancella, NON modifica e NON sostituisce le `claim_ledger_entries` esistenti. L'interazione con il ledger è regolata da queste invarianti:

### 5.1 Invarianti

1. **Append-only stretto preservato.** Le entry esistenti restano immutabili.
2. **Nessuna estensione di `claim_lineage.relation_kind`.** La 8.7 non introduce nuove relazioni nel ledger.
3. **Nessuna estensione di `claim_ledger_entries.state`.** Non viene introdotto uno stato `low_quality_evidence` o simile.
4. **Distinzione semantica rigorosa.** Source quality NON è correctness.

### 5.2 Comportamenti ammessi

Quando un assessment produce un giudizio rilevante (es. fonte debole o contraddetta) per un claim, sono ammessi DUE comportamenti, da scegliere a livello di policy (8.7G):

**Comportamento M1 — solo metadata (consigliato come default).**
- L'assessment vive nella nuova tabella `source_quality_assessments`.
- `claim_ledger_entries` resta invariata.
- Il Claim Ledger continua a riflettere la verifica testuale (CVE-lite) e i passaggi a `unverifiable` per source loss.
- Il Final Answer Gate futuro (8.7G) consulta gli assessment e può rifiutare/limitare la pubblicazione anche se il ledger dice `verified_fact`.

**Comportamento M2 — superseding del claim con motivazione esplicita di qualità.**
- Solo in casi gravissimi e dichiarati (es. fonte unica, weakly_supported, contraddetta da una primary autorevole).
- Si appende `claim_ledger_entries v(N+1)` con stato esistente (es. `disputed_fact` o `unverifiable`) e `transition_reason` esplicito che la policy 8.7 deve registrare (es. `transition_reason='source_quality_downgrade'`).
- Lineage via `relation_kind='supersedes'` (nessuna estensione necessaria).
- L'audit è emesso sulla chain task con un nuovo `event_type` (es. `claim.source_quality_downgrade`).

**Nota di prudenza:** M2 è potente ma pericoloso. Se mal calibrato, mischia "claim falso" e "fonte debole", che §3 vieta. Il default deve essere M1.

### 5.3 Distinzione dei quattro assi

Per rendere l'interazione comprensibile a chi legge il sistema:

| Asse                               | Domanda                                                | Dove vive                                                                 |
| ---------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------- |
| Claim correctness                  | Il claim è vero?                                       | (non valutato in MVP-0)                                                   |
| Evidence support                   | C'è almeno un'evidenza ben formata per questo claim?   | `claim_evidence_links`                                                    |
| Source quality                     | Le fonti di quell'evidenza sono buone?                 | `source_quality_assessments` (nuova)                                      |
| Final publication eligibility      | Il sistema deve pubblicare questo claim?               | `final_gate_reports` + policy 8.7G                                        |

Il rispetto di queste distinzioni è l'invariante fondante della Fase 8.7.

---

## 6. Interazione con Final Answer Gate

La 8.7A NON modifica il Final Answer Gate. La 8.7G (futura) introdurrà la policy. Il piano definisce **le policy possibili**, non le scrive.

### 6.1 Regola attuale (8.4, ricapitolata)

Uno span è verified-backed se e solo se esiste `final_answer_span_claim_links` tale che:

```
link.claim_ledger_entry_id == latest_entry_id_for(claim_logical_id)
AND latest_entry_state_for(claim_logical_id) == 'verified_fact'
```

### 6.2 Policy candidate per il gate futuro

Vanno valutate come opzioni, non implementate ora.

- **Policy P1 — block on uniformly weak support.**
  Se per uno span TUTTE le `evidence_spans` collegate al claim hanno `overall_quality ∈ {weak, unsuitable}`, il gate rifiuta con `coverage_gap_statements.kind='unverified_claim'` e una motivazione di qualità (gap_key esteso, o nuovo `kind` dedicato — vedi sotto).

- **Policy P2 — require strong support for sensitive claims.**
  Per claim marcati come "rilevanti" (criterio futuro, non in 8.7), il gate richiede almeno una `evidence_span` con `overall_quality='strong'` e `source_role='primary'`.

- **Policy P3 — flag secondary-only support.**
  Se nessuna `evidence_span` collegata al claim ha `source_role='primary'`, il gate non blocca ma marca il claim "secondary-only-supported" nel payload. La pubblicazione procede con una nota.

- **Policy P4 — downgrade confidence on stale or non-independent sources.**
  Se le sole fonti sono `freshness='stale'` o `independence_level ∈ {affiliated, self_reported}`, il gate abbassa una confidence aggregata, senza necessariamente bloccare.

- **Policy P5 — publish with disclosure on weak-but-declared sources.**
  Mantiene la pubblicazione possibile con un'esplicita nota di "fonte debole dichiarata". Necessario per non penalizzare i casi in cui l'utente carica esplicitamente una fonte che riconosce come weak e cerca comunque elaborazione.

### 6.3 Coverage gap kinds

Per evitare di mischiare "claim non verificato" con "fonte debole", la 8.7 raccomanda di NON riusare `kind='unverified_claim'` per i source-quality block. Una opzione:

- introdurre in una migration separata (NON in 8.7B) un nuovo `kind='source_quality_block'` su `coverage_gap_statements`, con relativa `gap_key`.

Decisione rinviata. In 8.7B questa estensione del CHECK NON viene fatta.

### 6.4 Nessun cambiamento contrattuale in 8.7A

Il gate corrente continua a usare la regola "verified-backed". 8.7A non altera nulla. 8.7G discute la policy e la implementa.

---

## 7. Interazione con Source Loss

La 8.7 distingue rigorosamente i due concetti:

| Concetto                | Significato                                                                    | Dove vive                                                  |
| ----------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------- |
| Source loss             | Fonte persa, inaccessibile, modificata, quote non più riconducibile            | `source_loss_events`, `source_loss_propagation_records`    |
| Source quality          | Fonte presente ma debole / obsoleta / non indipendente / non primaria          | `source_quality_assessments` (futura)                      |

### 7.1 Casi di interazione

- **`loss_kind='quote_mismatch'`** rimane un evento di source loss. Il source quality `extract_quality='quote_mismatch'` riflette lo stesso fatto ma da una prospettiva diversa (qualità dell'estratto). Il modulo 8.7 può decidere se, una volta osservato uno `source_loss_events.loss_kind='quote_mismatch'`, lanciare anche un assessment con `extract_quality='quote_mismatch'`. Questo NON è automaticamente richiesto: l'evento di source loss conserva il suo significato canonico (la propagazione marca i claim `unverifiable/source_lost`), e l'assessment è un complemento informativo, non una conseguenza obbligatoria.

- **`loss_kind='document_replaced'`** può richiedere un **reassessment** della qualità: la nuova versione del documento potrebbe avere un assessment diverso. È legittimo emettere un nuovo `source_quality_assessments v(N+1)` con `policy_version_id` invariata o anche con un payload che spiega "post-replacement reassessment".

- **`loss_kind='source_deleted'`/`source_access_lost'`/`policy_retraction'`** sono eventi che precludono futuri assessment di qualità per quell'evidence_span: la fonte non c'è più. La 8.7 NON deve modificare la riga di source loss per riflettere giudizi di qualità.

- **`freshness='stale'`** è un giudizio di qualità, NON un source loss. Una fonte stale è una fonte presente. Non deve produrre `source_loss_events`.

### 7.2 Invarianti incrociate

1. La 8.7 NON modifica `source_loss_events`, `source_loss_propagation_records`, né il propagator.
2. Un source_loss event può ESSERE seguito da un assessment di qualità, mai sostituito da esso.
3. Un assessment di qualità non emette `source_loss_events`.

---

## 8. API future (read-only, non implementate)

I seguenti endpoint sono **proposte di blocco 8.7F**. Non vengono implementati in 8.7A. Sono read-only end-to-end (nessun INSERT/UPDATE/DELETE, nessun Redis, nessuna invocazione worker).

### 8.1 `GET /api/v1/evidence-spans/{id}/source-quality`

Restituisce l'ultimo assessment per la `evidence_spans` indicata.

**Response shape (indicativa):**
```json
{
  "evidence_span_id": "<uuid>",
  "latest_assessment": {
    "id": "<uuid>",
    "version_no": 2,
    "source_type": "user_document",
    "source_role": "secondary",
    "authority_level": "medium",
    "independence_level": "self_reported",
    "freshness": "undated",
    "relevance": "direct_support",
    "extract_quality": "exact_quote_match",
    "contradiction_status": "unchecked",
    "overall_quality": "adequate",
    "confidence": 0.7,
    "evaluator_name": "mvp0_source_quality_v1",
    "evaluator_version": "0.1.0",
    "policy_version_id": "<uuid>",
    "created_at": "..."
  }
}
```

**Errori:**
- `404 RESOURCE_NOT_FOUND` con `details.resource="evidence_spans"` se lo span non esiste.
- `200` con `latest_assessment: null` se lo span esiste ma non ha mai avuto assessment (lista vuota: non si fabbrica storia).

**Paginazione:** N/A (single-row latest). Una variante futura potrebbe esporre `/history` come fa il claim ledger.

### 8.2 `GET /api/v1/documents/{id}/source-quality`

Latest assessment a granularità documento.

**Response shape:**
```json
{
  "document_id": "<uuid>",
  "latest_assessment": { ... },
  "evaluator_name": "...",
  "policy_version_id": "<uuid>"
}
```

**Errori:** `404 RESOURCE_NOT_FOUND` `details.resource="uploaded_documents"`.

### 8.3 `GET /api/v1/claims/{logical_id}/source-quality`

Aggregato: per ogni `evidence_span` collegata via `claim_evidence_links` alla latest entry del claim, il latest assessment.

**Response shape:**
```json
{
  "claim_logical_id": "<uuid>",
  "items": [
    {
      "evidence_span_id": "<uuid>",
      "latest_assessment": { ... } | null
    }
  ],
  "rollup": {
    "any_strong": false,
    "any_primary": false,
    "all_weak_or_unsuitable": true
  }
}
```

Il blocco `rollup` è un riassunto applicativo deterministico. NON sostituisce la policy del gate, che resta consumatore diretto delle righe.

### 8.4 `GET /api/v1/tasks/{task_id}/source-quality-summary`

Per ogni claim del task, il rollup di qualità delle fonti.

**Response shape:**
```json
{
  "task_id": "<uuid>",
  "items": [
    {
      "claim_logical_id": "<uuid>",
      "rollup": { ... }
    }
  ]
}
```

**Paginazione:** `limit` con tetto coerente con 8.6 (default 200, max 2000).

### 8.5 `GET /api/v1/published-answers/{id}/source-quality-report`

Per ogni span del published_answer, l'aggregato qualità delle fonti che lo supportano. Utile per il post-fatto (capire perché un certo published è stato pubblicato).

**Response shape:**
```json
{
  "published_answer_id": "<uuid>",
  "items": [
    {
      "final_answer_span_id": "<uuid>",
      "claim_logical_id": "<uuid>",
      "evidence_quality_summary": { ... }
    }
  ]
}
```

### 8.6 Invarianti comuni

- **Read-only end-to-end** (verificato da snapshot pre/post sui count delle tabelle, come 8.6).
- **Schemi shared** dedicati in `packages/shared/evidencefirst_shared/schemas.py` (`SourceQualityAssessmentRead`, `SourceQualityRollupRead`, ecc.).
- **404 normalizzati** secondo la convenzione `details.resource` già adottata in 8.6.
- **JSONB esposti verbatim** in MVP-0 (RBAC redaction = debito futuro, come dichiarato in 8.6).
- **Nessun nuovo `ErrorCode`** introdotto.

---

## 9. Worker / pipeline futura (non implementata)

La 8.7 deve scegliere DOVE e QUANDO eseguire l'evaluator. Quattro opzioni, da valutare in fase 8.7E:

### 9.1 Opzione W-A — Step sincrono dentro `task.created`

Il worker, dopo `claim_evidence_links`, invoca il Source Quality Evaluator come step della pipeline (mock-driven in MVP-0).

**Pro:** un solo evento, audit chain compatto, ordine deterministico. **Contro:** allunga la pipeline `task.created`; un fallimento dell'evaluator rischia di bloccare il published.

### 9.2 Opzione W-B — Consumer asincrono su evento dedicato

Un nuovo stream Redis `app.events.source_quality.requested` con un nuovo consumer.

**Pro:** disaccoppia completamente l'evaluator dalla pipeline; permette reassessment indipendenti. **Contro:** nuovo stream, nuovo consumer, nuovo EPR consumer_name, nuove migration test, eventuale DLQ futura.

### 9.3 Opzione W-C — Servizio sincrono chiamato dal worker, ma DOPO il gate

Il gate non consuma source quality in 8.7; ma dopo il gate, l'evaluator gira asincrono per popolare i metadati di qualità che le 8.7F espongono.

**Pro:** non blocca la prima pubblicazione. **Contro:** finestra di "published senza quality assessment" osservabile.

### 9.4 Opzione W-D — Job di reassessment periodico

Un job ricorrente rileva fonti stale o assessment vecchi rispetto alla policy corrente e produce un nuovo assessment.

**Pro:** abilita drift detection futuro. **Contro:** richiede scheduler / cron-like (non esiste in MVP-0).

### 9.5 Raccomandazione (non implementata)

- Per MVP-0 mock-driven: **W-A** è il punto di minor frizione (un nuovo step nel consumer `task_created`, mock-driven, idempotente come gli altri step 8.3/8.4).
- Per un secondo blocco: introdurre **W-B** quando arriva il vero source quality evaluator (con eventuale web search reale).
- **W-C** e **W-D** sono lasciate per fasi successive.

Trade-off:
- **Semplicità MVP**: vince W-A.
- **Idempotenza**: tutte e tre le prime opzioni la garantiscono via UNIQUE su `idempotency_key` come gli altri servizi.
- **Audit trail**: tutte producono audit `chain_scope='task'` con `event_type='source_quality.assessed'` (nuovo event_type, append-only).
- **Costo computazionale**: in MVP-0 trascurabile (mock).
- **Compatibilità futura con web search/API**: meglio W-B.

---

## 10. Test plan futuro (non scritto in 8.7A)

I seguenti test sono **da implementare** nei blocchi successivi (in particolare 8.7D/8.7E/8.7F/8.7H). Qui si elencano e si dichiara cosa devono verificare.

### 10.1 Unit test del Source Quality Evaluator service (mock-driven)

- `test_evaluator_classifies_primary_vs_secondary`
- `test_evaluator_classifies_official_vs_blog`
- `test_evaluator_marks_stale_freshness`
- `test_evaluator_marks_undated_when_no_date_signal`
- `test_evaluator_returns_quote_mismatch_when_quote_not_in_chunk`
- `test_evaluator_records_unchecked_contradiction_status_by_default`
- `test_evaluator_two_conflicting_sources_flow` (richiede contradiction detector, può restare skipped)
- `test_evaluator_is_idempotent_on_redelivery` (stessa target + idempotency_key → no duplicates)
- `test_evaluator_appends_new_assessment_under_different_policy_version`
- `test_evaluator_no_mutation_of_previous_assessments` (snapshot pre/post)

### 10.2 Integration test della pipeline

- `test_pipeline_emits_source_quality_assessed_audit_event`
- `test_pipeline_associates_assessment_with_evidence_span`
- `test_pipeline_realistic_flow_task_to_published_with_quality_metadata`

### 10.3 Final Answer Gate policy test (quando implementato in 8.7G)

- `test_gate_blocks_claim_supported_only_by_weak_sources` (Policy P1)
- `test_gate_approves_claim_with_strong_primary_source`
- `test_gate_flags_secondary_only_claim_without_blocking` (Policy P3)
- `test_gate_publish_with_disclosure_on_weak_declared_source` (Policy P5)

### 10.4 Read-only invariant test (per gli endpoint 8.7F)

Sulla falsariga dei test 8.6 (`test_task_source_loss_events_endpoint_is_read_only`):

- snapshot pre/post sui count di `source_quality_assessments`, `claim_ledger_entries`, `published_answers`, `audit_records`, `source_loss_events`, `source_loss_propagation_records`;
- nessun drift dopo qualunque GET sui 5 endpoint 8.7F.

### 10.5 Append-only / no mutation invariants

- INSERT su `source_quality_assessments` consentito; UPDATE/DELETE rifiutati dal trigger.
- Un assessment v1 + v2 coesistono; v1 ha payload identico prima e dopo l'INSERT di v2.

### 10.6 Realistic flow

Sulla falsariga di `tests/test_phase_8_5_source_loss_flow.py`:

- `tests/test_phase_8_7_source_quality_flow.py` che seed-a task + corpus + claim, esegue mock-evaluator, e verifica via HTTP gli endpoint 8.7F.

---

## 11. Non-obiettivi (esplicitamente fuori scope per 8.7)

La 8.7 planning NON include e NON implementerà:

- Web search reale.
- Provider AI reali.
- Crawling, scraping.
- UI dedicata per source quality.
- RBAC reale o redaction di JSONB.
- Retention policy distruttiva (rinviata a `0007_evaluation_retention.sql` o `0008_*`, comunque diverso e separato dalla migration 8.7B).
- Migration concreta (la 8.7A non scrive `migrations/0007_source_quality.sql`).
- Scoring "perfetto" o algoritmi di reputazione cross-tenant.
- Verità assoluta sulla fonte.
- Ranking commerciale o monetario.
- Withdrawal automatico da source quality.
- Modifica della propagazione source loss.
- Estensione di `claim_lineage.relation_kind`.
- Estensione di `verification_records.check_kind` (in particolare l'Opzione B di §4 è scartata).
- Estensione di `coverage_gap_statements.kind` (rinviata a blocchi successivi).
- Modifica del Final Answer Gate.
- Modifica del CVE-lite o dell'extractor.

Inoltre la 8.7A NON modifica:

- Codice applicativo (API, worker, shared).
- Test esistenti.
- Migrations.
- `PROJECT_STATE.md`.
- `README.md`.

L'unico output della 8.7A è il file `PHASE_8_7_PLAN.md`.

---

## 12. Roadmap a blocchi proposta

I blocchi successivi sono enunciati ma non implementati in 8.7A.

### 12.1 Sequenza proposta

- **8.7A — `PHASE_8_7_PLAN.md`** (questo blocco). Piano e basta.
- **8.7B — `migrations/0007_source_quality.sql`.** Migration per l'Opzione A: `source_quality_assessments` + `source_quality_factors` (opzionale, decisione in 8.7B) + `source_quality_policies` (opzionale). Trigger append-only standard. Partial unique indexes per `(target_kind, target_id, version_no)` o equivalente. Nessun altro CHECK alterato.
- **8.7C — Shared schemas.** `packages/shared/evidencefirst_shared/schemas.py` riceve `SourceQualityAssessmentRead`, `SourceQualityRollupRead`. Nessuna modifica ai modelli 8.4/8.5/8.6 esistenti.
- **8.7D — Mock Source Quality Evaluator service.** `apps/worker/app/services/source_quality_evaluator.py`. Deterministico, mock-driven, niente AI, niente web. Politica di default conservativa: tutti i `user_document` non datati diventano `freshness='undated'`, `source_type='user_document'`, `authority_level='unknown'`, `overall_quality='adequate'` se CVE-lite passa, `overall_quality='weak'` se CVE-lite fallisce. Idempotente.
- **8.7E — Worker integration (W-A).** Il consumer `task_created` chiama l'evaluator dopo `claim_evidence_links`. Audit `source_quality.assessed` su `chain_scope='task'`.
- **8.7F — Read API.** I cinque endpoint di §8. Read-only invariant test inclusi. Nessuna modifica al gate.
- **8.7G — Gate policy integration.** Implementa una sola delle policy P1–P5 (proposta: P1 + P5 con flag di policy). Nuovo `kind='source_quality_block'` su `coverage_gap_statements` SOLO qui, in una migration separata. Aggiornamento del Final Answer Gate per consultare gli assessment.
- **8.7H — Realistic flow tests + docs.** `tests/test_phase_8_7_source_quality_flow.py`. Aggiornamento finale di `PROJECT_STATE.md` (al termine di 8.7H, NON ora).

### 12.2 Ordine

L'ordine proposto rispetta:

1. **Schema first**: 8.7B prima di qualunque codice.
2. **Shared types prima dei consumatori**: 8.7C prima di 8.7D/8.7F.
3. **Service prima del worker integration**: 8.7D prima di 8.7E.
4. **Read API prima del gate policy**: 8.7F prima di 8.7G, così la policy del gate è osservabile via HTTP quando viene scritta.
5. **Test realistici alla fine**: 8.7H come gate complessivo.

### 12.3 Variante accettabile

Se 8.7G appare prematuro (probabile in MVP-0), si può chiudere la fase a 8.7F: gli assessment vengono scritti e osservati, ma il gate resta invariato. La distinzione tra "qualità misurata" e "qualità decisionale" è una conquista intermedia legittima.

---

## 13. Rischi residui

Reali e specifici del piano 8.7:

- **Falso senso di sicurezza da score numerici.** `confidence` ∈ [0, 1] e `overall_quality` ∈ {strong, adequate, weak, unsuitable, unknown} possono essere letti come verità. Mitigazione: documentazione esplicita, response shape che NON mostra mai un singolo numero come "rating della fonte".
- **Bias verso fonti istituzionali.** Una policy ingenua marcherebbe ogni documento ufficiale come `authority_level='high'` ignorando dominio e contesto. Mitigazione: `authority_level` resta `unknown` di default; mai derivato senza policy esplicita.
- **Domini diversi richiedono criteri diversi.** Scientifico vs legale vs news. Mitigazione: codomini ortogonali e policy-versioned. Una `policy_version_id` cambia il significato pratico dei valori senza riscrivere la storia.
- **Assenza di web search reale.** L'indipendenza, la corroborazione e (in parte) la freschezza non sono deducibili da un corpus chiuso. Mitigazione: i campi restano `unknown` finché non c'è una fonte esterna da consultare; la 8.7 NON pretende di valutarli in MVP-0.
- **Fonti user-provided autorevoli ma non verificabili esternamente.** Una sentenza ufficiale caricata dall'utente è autorevole, ma in MVP-0 il sistema non può confermarla esternamente. Mitigazione: distinguere `authority_level='unknown'` (default in MVP-0) da `authority_level='high'` (consentito solo via policy che dichiara la regola usata).
- **Rischio di overblocking.** Una policy troppo severa può bloccare claim legittimi. Mitigazione: in 8.7G partire con una sola policy (es. P1 con soglia conservativa) e P5 come escape.
- **Rischio di underblocking.** Senza policy, gli assessment non producono effetti. Mitigazione: 8.7F espone gli assessment via HTTP; un operatore può consultarli e agire manualmente nei blocchi intermedi 8.7F → 8.7G.
- **Necessità futura di RBAC/redaction.** I payload JSONB degli assessment possono contenere dati sensibili (es. valutazioni interne sull'autore). Stesso debito già dichiarato in 8.6 §9.
- **Costo computazionale.** Mock-driven trascurabile; con un evaluator AI reale il costo diventa significativo. La 8.7 NON introduce provider reali (vincolo MVP-0 `PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0`).
- **Explainability.** Una decisione "fonte debole" deve essere spiegabile. Mitigazione: `payload` JSONB dell'assessment porta i sotto-criteri ridondanti rispetto agli enum, così che un'interfaccia futura possa renderli.
- **Confusione concettuale rispetto al ledger.** Il rischio più grave: mischiare claim-falso e fonte-debole. Mitigazione: §3 e §5.

---

## 14. Decisione documentale (8.7A)

- **`PHASE_8_7_PLAN.md`** è creato (questo file).
- **`PHASE_8_6_PLAN.md`** non viene modificato.
- **`PROJECT_STATE.md`** non viene modificato (l'aggiornamento spetta a 8.7H, non a 8.7A).
- **`README.md`** non viene modificato.
- **Codice applicativo, test, migrations** non vengono toccati.
- La 8.7A è esclusivamente documentale.

---

FILE_COMPLETATI
- PHASE_8_7_PLAN.md

FILE_DA_FARE_PROSSIMO_BLOCCO
- review manuale del piano
- git diff --check
- commit:
  - `git add PHASE_8_7_PLAN.md`
  - `git commit -m "Add phase 8.7 source quality plan"`
  - `git push`

RISCHI_RESIDUI
- Confusione tra correctness, evidence support, source quality e publication eligibility se §3 / §5 non vengono onorati dai blocchi successivi.
- Falso senso di sicurezza da score numerici (`confidence` ∈ [0,1], `overall_quality` enum).
- Bias verso fonti istituzionali se la policy di `authority_level` non resta esplicita e versionata.
- Domini eterogenei (scientifico/legale/news) richiedono criteri diversi non scrivibili in un unico CHECK.
- Assenza di web search reale in MVP-0: indipendenza, corroborazione e freschezza esterne restano largamente `unknown`.
- Fonti user-provided autorevoli ma non verificabili esternamente: rischio di sovra- o sotto-stimare la qualità.
- Rischio di overblocking nel futuro Final Answer Gate (Policy P1 troppo severa).
- Rischio di underblocking se gli assessment vengono scritti ma non consumati dal gate (8.7F senza 8.7G).
- Debito RBAC / redaction su payload JSONB (già noto in 8.6, non risolto in 8.7).
- Costo computazionale futuro quando si introdurrà un evaluator reale (oltre MVP-0).
- Migration `0007_source_quality.sql` con CHECK ternario `sqa_target_xor` e partial unique indexes: rischio medio di errori al primo colpo, da affrontare in 8.7B con cura.
- Coesistenza con `0007_evaluation_retention.sql` (placeholder non scritto): l'allocazione del numero `0007` deve essere coordinata tra source_quality e retention; il piano propone implicitamente di rinominare la retention come `0008_*` o successiva.
