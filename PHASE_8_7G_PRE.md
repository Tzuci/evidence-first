# PHASE_8_7G_PRE — Source Quality Gate policy analysis

Documento **decisionale e di piano** per il blocco 8.7G-CODE. Questo
blocco non scrive codice applicativo, non scrive migration, non scrive
test, non modifica `final_answer_gate.py`, non modifica
`source_quality_assessments`, non modifica `coverage_gap_statements`,
non modifica `task_masters`, non tocca `PROJECT_STATE.md`, `README.md`
o `PHASE_8_7_PLAN.md`.

L'obiettivo del documento è fissare, **prima** di toccare il Gate, la
policy esatta con cui il Final Answer Gate consumerà
`source_quality_assessments` in 8.7G-CODE, e decidere se serve una
migration aggiuntiva (provvisoriamente `0008_coverage_gap_source_quality.sql`)
per estendere `coverage_gap_statements.kind`.

**Commit di partenza implicito**: stato post-8.7F al commit
`1a48ec39e772ef7abb610682173621d802e656b7`.

---

## 1. Stato di partenza post-8.7F

Tutto ciò che segue è leggibile direttamente dal repo e non aggiunge
nulla di nuovo. È riassunto qui solo per fissare il contesto della
decisione 8.7G.

### 1.1 Schema esistente

- `source_quality_assessments` (migration `0007_source_quality.sql`)
  esiste, è append-only, e ammette esattamente uno tra
  `evidence_span_id`, `document_chunk_id`, `document_id` per riga
  (CHECK `sqa_target_xor`). I nove codomini di qualità sono fissati a
  livello DB (CHECK enum) e mirrorati a livello Python come tuple
  `SOURCE_QUALITY_*_VALUES` in `packages/shared/evidencefirst_shared/schemas.py`.
- `final_gate_reports` è append-only via trigger
  `final_gate_reports_append_only` su `reject_modify_append_only`;
  UNIQUE su `draft_final_answer_id`; FK composita
  `(draft_final_answer_id, task_id) → draft_final_answers(id, task_id)`.
- `coverage_gap_statements` ha CHECK
  `kind IN ('unverified_claim','missing_evidence','out_of_scope','source_loss')`
  e UNIQUE composito `(draft_final_answer_id, kind, gap_key)`.
  **`coverage_gap_statements` NON ha un trigger append-only**
  (verificato in `0005_answers_gate.sql`): ammette tecnicamente
  UPDATE/DELETE. Il Gate 8.4 attualmente la usa solo come tabella
  insert-only di fatto.
- `claim_evidence_links` ha CHECK `cel_origin_xor` (exactly one of
  `evidence_span_id`, `retrieved_source_span_id` NOT NULL), e FK
  composita su `(claim_ledger_entry_id, claim_logical_id)`.

### 1.2 Pipeline 8.7E

Lo step Source Quality gira dentro `_run_8_3_extract_and_verify` tra
`task.analyzed_partial` e `task.compiling`, SAVEPOINT-protected; emette
un singolo audit aggregato `task.source_quality_assessed`. Sui resume
da `compiling` o `analyzed_partial` lo step **non** viene re-eseguito.

### 1.3 Mock evaluator (stato corrente)

Il mock (`source_quality_evaluator.py`) scrive sempre, per ogni span:

- `source_type='user_document'`
- `source_role='unclear'`
- `authority_level='unknown'`
- `independence_level='unknown'`
- `freshness='undated'`
- `contradiction_status='unchecked'`
- `overall_quality='unknown'`
- `confidence=0.5`

Per `evidence_span` target:
- `relevance='direct_support'`
- `extract_quality='exact_quote_match'`

Per `document_chunk` e `document` target:
- `relevance='contextual_support'`
- `extract_quality='partial_match'`

L'orchestrator 8.7E scrive **solo** target `evidence_span` (mai
`document_chunk` o `document`).

### 1.4 Final Answer Gate (stato corrente, 8.4 invariato)

Regola di verifica (file `final_answer_gate.py`, branch in
`run_final_answer_gate`):

- Zero spans → `rejected`, `reason_code='no_verified_claims'`, gap
  `kind='missing_evidence'`, `gap_key='no_verified_claims'`.
- Tutti gli spans verified-backed → `approved`,
  `reason_code='all_spans_verified'`, `published_answers` v1.
- Almeno uno span non verified-backed → `rejected`,
  `reason_code='unverified_spans_present'`, un gap per span con
  `kind='unverified_claim'`, `gap_key=f'span:{final_answer_span_id}'`.

Source Quality **non viene consultata**.

### 1.5 Read API 8.7F

Espongono assessment per evidence_span e summary per task, leggendo
solo il `latest` per span. Non valutano nulla, non decidono nulla.

---

## 2. Problema da risolvere

In 8.7G-CODE il Final Answer Gate dovrà **consumare** la Source Quality
nelle sue decisioni di pubblicazione. Il problema è duplice:

1. **Calibrazione della policy.** Con il mock attuale, ogni span ha
   `overall_quality='unknown'` e `contradiction_status='unchecked'`. Una
   policy che blocchi su `unknown` o `unchecked` farebbe fallire la
   sequenza approved testata in `test_consumer_with_documents.py`
   (`task.published` come stato terminale) e di fatto azzererebbe la
   coverage 8.4 esistente. Una policy che ignori `unknown` rischia di
   non bloccare mai nulla, rendendo la 8.7G priva di effetto pratico
   in MVP-0 ma soprattutto creando un futuro problema il giorno in cui
   un evaluator reale produrrà `unknown` per casi davvero incerti.

2. **Forma del rifiuto.** Oggi `coverage_gap_statements.kind` ammette
   solo quattro valori. Riusare `kind='unverified_claim'` per
   esprimere "fonte inadeguata" mescola due assi semantici distinti
   (vedi §3 e §7). Aggiungere un kind richiede una migration.

Il documento qui sotto risolve entrambi i problemi senza scrivere
codice.

### Vincoli architetturali (richiamati dal prompt operativo)

- `final_gate_reports` e `coverage_gap_statements` non si aggiornano,
  si inseriscono. Anche se `coverage_gap_statements` non ha un trigger
  append-only oggi, l'8.7G **non** introdurrà UPDATE/DELETE su di
  essa: l'invariante operativa è già "insert-only".
- Non modificare `claim_ledger_entries`. Source quality NON è un nuovo
  state del ledger e NON deve mai diventarlo.
- Non modificare `source_quality_assessments`. Il Gate è puro
  consumer.
- Non introdurre nuovi stati in `task_masters`. Il branch "block per
  fonte inadeguata" deve riusare i due stati terminali già esistenti:
  `analyzed_partial` + audit `task.publication_held` (rejected
  scenario), oppure `published` (approved scenario).
- Non confondere source quality con claim correctness, con source
  loss, con verification outcome.
- Non bloccare l'intera pipeline solo perché il mock scrive `unknown`.
- Nessun provider AI reale, nessuna web search, nessun RBAC, nessun
  backfill.
- Niente "elimina allucinazioni": il framing operativo resta "riduce
  il rischio di pubblicare claim non supportati o basati su fonti
  inadeguate".

---

## 3. Invarianti semantiche

Le invarianti fondative di `PHASE_8_7_PLAN.md §3` restano vincolanti
per 8.7G:

1. **source quality ≠ claim correctness.** Un claim può essere falso
   anche con fonte forte. Un claim può essere vero anche con fonte
   debole. La policy 8.7G non deve mai trattare `overall_quality`
   come verità del claim.
2. **source quality ≠ evidence support.** Avere un
   `claim_evidence_links` ben formato non implica qualità della fonte.
   Continuiamo a richiedere prima la verifica 8.4 (verified-backed) e
   solo dopo a controllare la qualità.
3. **source quality ≠ verification outcome.** `verification_records`
   con `outcome='pass'` significa CVE-lite passato (la quote esiste
   nel chunk con l'hash atteso). NON significa "fonte affidabile".
4. **source quality ≠ source loss.** Lifecycle source-loss e Source
   Quality vivono in tabelle separate, vengono propagate da servizi
   separati, hanno semantiche separate.
5. **source quality ≠ final publication eligibility.** La eligibility
   è composta da quattro assi (correctness — non implementato in
   MVP-0; evidence support; source quality; policy gate). 8.7G aggiunge
   il terzo asse al Gate; non collassa i quattro.

Conseguenza diretta:

- 8.7G **non** introduce uno stato `claim_ledger_entries.state` del
  tipo `source_quality_downgraded`. La policy M1 di
  `PHASE_8_7_PLAN.md §5.2` (solo metadata) resta attiva.
- 8.7G **non** estende `claim_lineage.relation_kind`.
- 8.7G **non** estende `verification_records.check_kind`.

---

## 4. Analisi dei campi Source Quality

Per ognuno dei nove enum (più `confidence`) decidiamo se 8.7G-CODE lo
deve consultare, e con quale forza. Il principio guida è: **una
dimensione partecipa alla decisione solo se ha un significato
operativo chiaro in MVP-0 con il mock attuale**.

### 4.1 `overall_quality` ∈ {strong, adequate, weak, unsuitable, unknown}

Asse principale. È il giudizio aggregato dell'evaluator. Comportamento
raccomandato per 8.7G-CODE:

- `strong` / `adequate`: nessun effetto sul Gate, lo span è
  "qualitatively-adequate".
- `weak`: **non blocca in MVP-0**, ma produce una `coverage_gap_statements`
  con `severity='warn'` se la migration 0008 viene approvata
  (vedi §7). Senza migration, debito documentato.
- `unsuitable`: **blocca**. Non c'è caso d'uso legittimo in MVP-0 in
  cui una fonte classificata `unsuitable` debba comparire come supporto
  di un claim pubblicato.
- `unknown`: **non blocca in MVP-0**. Significa letteralmente "il
  sistema oggi non sa". Trattarlo come block farebbe esplodere il
  100% delle pipeline correnti (il mock scrive `unknown` per tutti).
  Trattarlo come "passa silenziosamente" sarebbe equivalente a non
  averlo: si raccomanda di emetterlo come `severity='warn'` se la
  migration 0008 è in atto. Vedi §6.

### 4.2 `contradiction_status` ∈ {no_known_contradiction, contradicted_by_stronger_source, conflicting_sources, unchecked}

Asse secondario, ortogonale a `overall_quality`.

- `contradicted_by_stronger_source`: **blocca**. Il sistema ha rilevato
  una contraddizione *risolta a sfavore* della fonte attuale. Pubblicare
  significherebbe pubblicare un claim sostenuto da una fonte sconfitta
  da un'altra fonte. Va sempre rifiutato.
- `conflicting_sources`: **blocca** in MVP-0. Esprime un conflitto non
  ancora risolto. L'alternativa sarebbe un nuovo `severity='hold'` con
  branch decisionale separato, ma `task_masters.status` non si estende
  in 8.7G e il rejected scenario esistente (`analyzed_partial` +
  `task.publication_held`) è già il branch corretto per "non pubblicare
  in attesa di chiarimenti".
- `no_known_contradiction`: nessun effetto.
- `unchecked`: **non blocca**, ma non deve mai essere presentato come
  "no contradiction" nelle read API o nei log. È il valore di default
  del mock. Vedi §11 sui rischi.

### 4.3 `freshness` ∈ {current, recent, stale, undated, not_time_sensitive}

In MVP-0 non c'è informazione attendibile su scadenze e tempi: il
mock scrive `undated`. Una policy `block on stale` o `block on undated`
sarebbe pura cargo-cult.

- Nessuna dimensione `freshness` blocca in MVP-0.
- Opzionalmente: `stale` può contribuire a un warning aggregato,
  ma non è un asse decisionale per 8.7G-CODE.

### 4.4 `source_role` ∈ {primary, secondary, tertiary, unclear}

In MVP-0 closed-corpus tutti gli upload sono di fatto fonti utente
(`source_role='unclear'` nel mock). Distinguere tra `primary` e
`tertiary` richiederebbe un evaluator reale.

- Nessuna dimensione `source_role` blocca in MVP-0.
- Future-proof: una policy `require primary for sensitive claims` (P2
  in `PHASE_8_7_PLAN.md §6.2`) è esplicitamente rinviata a una
  classificazione di sensibilità che non esiste in MVP-0.

### 4.5 `authority_level` ∈ {high, medium, low, unknown}

Mock fisso a `unknown`. Stessa logica di `source_role`: senza un
evaluator reale non c'è segnale.

- Nessuna dimensione `authority_level` blocca in MVP-0.

### 4.6 `independence_level` ∈ {independent, affiliated, self_reported, unknown}

Mock fisso a `unknown`. In MVP-0 le fonti sono uploadate dall'utente e
non c'è metrica di indipendenza esterna disponibile.

- Nessuna dimensione `independence_level` blocca in MVP-0.

### 4.7 `relevance` ∈ {direct_support, contextual_support, weak_support, irrelevant}

Mock fissa a `direct_support` per evidence_span (l'unico target usato
dall'orchestrator 8.7E).

- `irrelevant` dovrebbe in teoria bloccare. Ma il mock non lo emette
  mai e nessuna pipeline reale lo produrrebbe oggi.
- 8.7G-CODE può prepararsi a bloccare su `irrelevant` come safety net,
  ma in pratica non si attiverà mai con il mock.

### 4.8 `extract_quality` ∈ {exact_quote_match, paraphrase_match, partial_match, quote_mismatch}

`quote_mismatch` come fatto di qualità è semanticamente vicino a
`source_loss_events.loss_kind='quote_mismatch'`. La differenza è
deliberata e va preservata.

- `quote_mismatch` come dimensione di qualità non blocca da 8.7G.
  La perdita di evidenza è gestita dal source-loss propagator
  esistente, che marca il claim come `unverifiable`. Il Gate 8.4
  riprende lo span dal Claim Ledger e già emette `unverified_spans_present`.
- Non duplichiamo la logica.

### 4.9 `source_type` ∈ {user_document, web_page, …, unknown}

Solo classificazione. Nessun effetto sulla policy 8.7G-CODE.

### 4.10 `confidence` (DOUBLE PRECISION in [0, 1] o NULL)

Mock costante a 0.5. Il piano (`PHASE_8_7_PLAN.md §13`) avverte
esplicitamente di NON usare `confidence` come chiave decisionale
unica.

- 8.7G-CODE non legge `confidence` per decidere. Può eventualmente
  surfacciarlo nel payload del gate report per debugging.

---

## 5. Policy candidate

Le opzioni elencate nel prompt operativo, valutate nel contesto sopra:

### P0 — Observation-only

8.7G-CODE NON modifica le decisioni del Gate. Source Quality resta
scritta in `source_quality_assessments` ma non viene consultata. Il
Gate continua come 8.4.

- Pro: zero rischio di regressione su 8.4, zero rischio di overblock,
  zero rischio di rompere i test esistenti.
- Contro: 8.7G diventa un no-op. Non c'è effetto sulla pubblicabilità.
  La promessa "evidence-gated" rimane non onorata sull'asse qualità.

### P1 — Block solo su `overall_quality='unsuitable'`

Il Gate consulta il latest per ogni evidence_span che supporta uno
span verified-backed. Se almeno uno di quegli evidence_span ha
`latest.overall_quality='unsuitable'`, il Gate rigetta il task.

- Pro: chiaro, semanticamente difendibile (`unsuitable` significa
  letteralmente "non utilizzabile"), non si attiva mai con il mock
  attuale (mock scrive `unknown`, mai `unsuitable`), quindi non rompe
  test esistenti.
- Contro: in MVP-0 questa policy non blocca mai nulla in pratica,
  perché il mock non produce `unsuitable`. Diventa effettiva solo con
  un evaluator reale.

### P2 — Block su `weak` o `unsuitable`

Più aggressiva. Bloccherebbe anche fonti deboli.

- Pro: alza il livello di garanzia.
- Contro: il mock non produce `weak`, quindi anche P2 oggi non si
  attiva. Quando un evaluator reale entrasse in produzione, P2
  rischierebbe di bloccare task per i quali "fonte debole" è
  l'unica fonte disponibile. Senza un meccanismo di disclosure
  (P5), P2 produce overblocking.

### P3 — Block solo su `contradiction_status='contradicted_by_stronger_source'`

Indipendente da `overall_quality`. Esprime: "se sappiamo che la fonte
attuale è stata contraddetta da una più forte, non pubblichiamo".

- Pro: semanticamente molto difendibile. Se sappiamo che esiste una
  fonte più forte che contraddice, pubblicare comunque è
  evidence-gating fallito.
- Contro: dipende dall'esistenza di un detector reale. Il mock
  scrive sempre `contradiction_status='unchecked'`, quindi anche P3
  oggi non si attiva mai.

### P4 — Soft disclosure per `unknown` / `weak` / `unchecked`

Non blocca, ma emette `coverage_gap_statements` con `severity='warn'`
per ogni span supportato da fonti `unknown`/`weak` o non verificate
per contraddizione.

- Pro: alza la trasparenza senza overblocking. È compatibile col
  mock: ogni task emetterà warning, ma la pipeline approved
  continuerà a produrre `task.published`.
- Contro: richiede una nuova `coverage_gap_statements.kind` per non
  inquinare l'asse `unverified_claim`. Vedi §7.

### P5 — Hard block per missing assessment (per ogni evidence_span senza assessment, block)

- Pro: chiude la gap dei task pre-8.7E o di quelli dove l'8.7E è
  fallito.
- Contro: i task processati prima della 8.7E non hanno assessment per
  costruzione (vedi `PROJECT_STATE.md` §"debito"). Bloccarli
  retroattivamente romperebbe la garanzia di non-regressione 8.4.
  Bloccarli prospettivamente è invece sensato: se 8.7E è disponibile
  e ha fallito, l'audit `task.source_quality_assessed` con
  `status='failed'` documenta il fatto, ma non sappiamo nulla sulla
  qualità. Tuttavia, oggi il mock non fallisce mai e 8.7E è
  affidabile per ogni nuovo task. La decisione operativa è di NON
  bloccare in MVP-0 sui missing assessment, e di emettere warning.

### Combinazioni rilevanti

- **P1 + P3**: blocco su `unsuitable` OR `contradicted_by_stronger_source`.
  Né l'una né l'altra si attiva col mock. Safe per MVP-0.
- **P1 + P3 + P4**: blocco su segnali forti + warning su `weak`/`unknown`/`unchecked`/`conflicting_sources`/missing.
  Richiede 0008 per il nuovo `kind`.

---

## 6. Raccomandazione MVP-0

Si raccomanda l'adozione di **P1 + P3 + P4** con i seguenti
parametri concreti.

### 6.1 Branch decisionali del Gate post-8.7G

Per ogni span verified-backed, il Gate aggrega lo stato Source
Quality dei suoi evidence_span di supporto. Branch:

**Block branch (decision = `rejected`, `reason_code = 'source_quality_block'`)**

Almeno uno degli evidence_span di supporto presenta una delle
seguenti condizioni *sul latest_assessment*:

- `overall_quality = 'unsuitable'`;
- `contradiction_status = 'contradicted_by_stronger_source'`;
- `contradiction_status = 'conflicting_sources'`.

Per ciascuno span sospetto, emissione di una
`coverage_gap_statements` con `kind = 'source_quality_block'` (vedi
§7), `severity = 'block'`,
`gap_key = f'span:{final_answer_span_id}:source_quality_block'`,
`details` arricchiti con la dimensione che ha fatto scattare il block.

**Warning branch (decision invariata, ma warning emessi)**

Almeno uno degli evidence_span di supporto presenta una delle
seguenti condizioni *sul latest_assessment* (senza alcuna condizione
da block branch sullo stesso span):

- `overall_quality = 'weak'`;
- `overall_quality = 'unknown'`;
- `contradiction_status = 'unchecked'`;
- nessun assessment esiste (latest mancante per uno o più
  evidence_span di supporto).

Emissione di `coverage_gap_statements` con
`kind = 'source_quality_warning'`, `severity = 'warn'`,
`gap_key = f'span:{final_answer_span_id}:source_quality_warning'`,
`details` con motivazione esplicita. **La decision del Gate NON
cambia** per il solo warning. Lo span resta approved-eligible se
verified-backed (regola 8.4), e il task può raggiungere `published`.

**Approved branch (invariato rispetto a 8.4)**

Se nessuno span verified-backed presenta condizioni di block, e
tutti gli span sono verified-backed, il Gate emette `approved` come
in 8.4. Eventuali warning sono coverage_gap_statements emessi a
fianco, NON cambiano la decision.

### 6.2 Aggregazione tra più evidence_span che supportano uno stesso span

Uno span può essere supportato da più claim, e ciascun claim può
avere più evidence_span. La regola di aggregazione raccomandata è
**worst-on-block, any-on-warn**:

- Se ALMENO UNO degli evidence_span ha una condizione di block →
  block branch per quello span.
- Altrimenti, se ALMENO UNO degli evidence_span ha una condizione di
  warning → warning branch per quello span.
- Altrimenti → clean.

Questa regola è conservativa sull'asse block (un'evidenza
`unsuitable` basta per bloccare anche se ne esistono altre `strong`)
ed è coerente con la regola 8.4 "approved richiede TUTTI gli span
verified-backed".

### 6.3 Comportamento con il mock attuale

Con il mock 8.7D che scrive sempre `overall_quality='unknown'` e
`contradiction_status='unchecked'`:

- Block branch: **mai** attivato (mock non produce `unsuitable`,
  `contradicted_by_stronger_source`, `conflicting_sources`).
- Warning branch: **sempre** attivato per ogni span (mock produce
  `unknown` + `unchecked` per ogni evidence_span linkato).
- Approved scenario 8.4 esistente: **preservato**. Il task arriva a
  `published` come oggi.
- Rejected scenario 8.4 zero-verified e unverified_spans: **preservato**.
  Le `coverage_gap_statements` di tipo `unverified_claim` e
  `missing_evidence` continuano a essere emesse e prevalgono.

Questa è la proprietà fondamentale che P1+P3+P4 deve garantire: con il
mock attuale, NESSUN task che era approved in 8.4 viene rejected in
8.7G. Solo le `coverage_gap_statements` di tipo `source_quality_warning`
si aggiungono.

### 6.4 Perché non P0

P0 è scartata perché lascia la promessa "evidence-gated sull'asse
qualità" inadempiuta anche solo come *segnale*. P4 (warning) costa
poco e fornisce immediatamente uno strato di osservabilità sulla
qualità che è coerente con la roadmap anti-hallucination
(`PHASE_8_7_PLAN.md §13`).

### 6.5 Perché non P2 da sola

P2 (block su `weak`) è troppo aggressiva senza un evaluator reale e
senza disclosure. Con un evaluator reale e P5 (disclosure), P2
diventa eventualmente difendibile; in MVP-0 no.

---

## 7. Decisione su `coverage_gap_statements.kind`

Quattro opzioni dal prompt:

### Opzione A — Riusare `kind='unverified_claim'` con `reason_code` source_quality_*

Mantenere `kind='unverified_claim'` e differenziare via
`reason_code` (sul `final_gate_reports`) o via `details.reason_code`
(sul `coverage_gap_statements`).

- Pro: zero migration.
- Contro: confonde semanticamente "claim non verificato dal CVE-lite"
  con "claim verificato ma con fonte inadeguata". Sono due assi
  diversi (§3). I consumatori (UI futura, eval) non possono
  distinguere senza ispezionare `details`. **Scartata.**

### Opzione B — Aggiungere solo `kind='source_quality_block'`

Una sola estensione del CHECK. Per i warning, riuso di kind esistente
o nessuna emissione.

- Pro: una migration sola.
- Contro: non lascia spazio al warning branch raccomandato in §6, che
  richiede comunque un kind dedicato per non inquinare
  `unverified_claim`. Se P4 viene incluso (e si raccomanda di
  includerlo), serve anche `source_quality_warning`. **Scartata.**

### Opzione C — Aggiungere solo `kind='source_quality_warning'`

Inverso di B. Solo warning, nessun block kind dedicato. Il block
riutilizza `unverified_claim`.

- Pro: una migration sola.
- Contro: stessa confusione di A sul block path. **Scartata.**

### Opzione D — Aggiungere entrambe `kind ∈ {source_quality_block, source_quality_warning}`

- Pro: separa nettamente i due assi. Il consumer può sempre sapere se
  un gap è dovuto a "claim non verificato" (8.4 CVE-lite) o a "fonte
  inadeguata" (8.7G Source Quality). Le `severity` (`block` vs `warn`)
  rinforzano la distinzione.
- Contro: due valori nuovi nel CHECK invece di uno. Costo della
  migration trascurabile.

**Raccomandazione: opzione D.**

### 7.1 Migration aggiuntiva

Una migration **separata**, non un'estensione di 0007.

- Numero raccomandato: `0008_coverage_gap_source_quality.sql`.
- Contenuto:
  - DROP CONSTRAINT `coverage_gap_statements_kind_check` (CHECK
    inline di 0005); ADD CONSTRAINT con il codominio esteso
    `{unverified_claim, missing_evidence, out_of_scope, source_loss,
    source_quality_block, source_quality_warning}`.
  - Nessuna modifica al CHECK su `severity`.
  - Nessun nuovo trigger.
  - Nessuna modifica all'UNIQUE composito
    `(draft_final_answer_id, kind, gap_key)` (rimane idempotente).
- Coordinamento con retention futura: la retention storica
  `0007_evaluation_retention.sql` è già stata bumpata a `0008_*` in
  `PROJECT_STATE.md` per evitare collisione con 0007 source_quality.
  Se la migration source_quality_kind viene assegnata 0008,
  **la retention futura prenderà 0009**. Questa rinumerazione va
  registrata in `PROJECT_STATE.md` quando 0008 verrà committata
  (nel blocco 8.7G-CODE, non in 8.7G-PRE).
- Provvisorietà del nome: il prompt suggerisce
  `0008_coverage_gap_source_quality.sql`; nome equivalente accettabile
  se più chiaro al momento della scrittura del codice.

### 7.2 Comportamento se la migration non viene scritta

Se per qualunque ragione la 0008 viene rinviata, 8.7G-CODE deve
ripiegare su una delle seguenti, in ordine di preferenza:

1. **Debt esplicito.** 8.7G-CODE NON emette nuove
   `coverage_gap_statements` di tipo source_quality. Il block branch
   resta documentato nel payload del `final_gate_reports`
   (`payload.source_quality_block` con dettagli per span). Il warning
   branch viene rinviato. Questa è la fallback raccomandata, da
   documentare esplicitamente come debito tecnico in `PROJECT_STATE.md`
   al momento del rinvio.
2. **Riuso di `kind='unverified_claim'`** con `details.reason_kind='source_quality_block'`.
   Scartata in §7 ma teoricamente possibile. Sconsigliata.

---

## 8. Punto di integrazione nel Final Answer Gate

8.7G-CODE modificherà esclusivamente `apps/worker/app/services/final_answer_gate.py`.
Nessuna modifica al consumer `task_created.py`. Nessuna modifica
all'orchestrator 8.7E. Nessuna modifica al mock evaluator.

### 8.1 File interessato

`apps/worker/app/services/final_answer_gate.py`, funzione
`run_final_answer_gate(conn, *, tenant_id, project_id, task_id)`.

### 8.2 Punti di lettura

Tra il calcolo delle `spans` e l'emissione del verdict (block C o B di
`run_final_answer_gate`), va inserita una nuova fase:

1. Per ogni span verified-backed, identificare il set di
   `evidence_span_id` che lo supportano via:

   ```text
   final_answer_spans (fas)
     -> final_answer_span_claim_links (fascl)         [join su fas.id]
     -> claim_ledger_entries (cle)                    [join su fascl.claim_ledger_entry_id]
     -> claim_evidence_links (cel)                    [join su cle.id == cel.claim_ledger_entry_id]
     -> filtro: cel.evidence_span_id IS NOT NULL
   ```

   Vincolo: lo span è verified-backed (per 8.4) sse il link punta
   alla *latest* entry verified_fact. La policy 8.7G consulta gli
   evidence_span del link che effettivamente sostiene lo span: cioè
   gli `evidence_span_id` collegati alla *entry latest verified_fact*,
   non a entry storiche.

2. Per ogni `evidence_span_id` così identificato, leggere il *latest*
   `source_quality_assessments` (massimo `version_no` per quel
   `evidence_span_id`). Il latest può non esistere → gestire come
   "missing assessment" → warning branch.

3. Aggregare per span secondo §6.2.

4. Emettere coverage_gap_statements (block e/o warning) ed eventualmente
   ribaltare la decision da `approved` a `rejected`.

### 8.3 Query SQL raccomandata (forma logica, non implementativa)

Una singola query per draft che restituisce, per ogni span, un'aggregato
delle dimensioni rilevanti:

```text
SELECT
  fas.id              AS span_id,
  fas.span_index      AS span_index,
  cel.evidence_span_id AS evidence_span_id,
  sqa_latest.overall_quality,
  sqa_latest.contradiction_status
FROM final_answer_spans fas
JOIN final_answer_span_claim_links fascl ON fascl.final_answer_span_id = fas.id
JOIN claim_ledger_entries cle ON cle.id = fascl.claim_ledger_entry_id
JOIN claim_evidence_links cel ON cel.claim_ledger_entry_id = cle.id
LEFT JOIN LATERAL (
  SELECT overall_quality, contradiction_status
  FROM source_quality_assessments
  WHERE evidence_span_id = cel.evidence_span_id
  ORDER BY version_no DESC
  LIMIT 1
) sqa_latest ON TRUE
WHERE fas.draft_final_answer_id = :did
  AND cel.evidence_span_id IS NOT NULL
ORDER BY fas.span_index ASC, cel.evidence_span_id ASC
```

Note:

- La condizione "il link punta alla latest verified_fact" è già
  vincolata dalla regola 8.4 esistente per qualificare uno span come
  verified-backed; in pratica la query è ristretta agli span
  verified-backed prima di applicare il source_quality filter.
  L'implementazione 8.7G-CODE può scegliere di iterare in Python sui
  risultati di `_select_spans_with_links` esistente, oppure di
  riscrivere la query: la decisione è di codifica, non di policy.
- `sqa_latest` può essere NULL → missing assessment.
- Non si filtra su `tenant_id`: lo `evidence_span_id` è di per sé
  univoco e source_quality_assessments scrive con scope canonico
  (vedi `source_quality_evaluator.py` correzione "canonical scope").

### 8.4 Ordine delle decisioni nel Gate

Per non rompere 8.4, l'ordine raccomandato è:

1. Calcolo span e verified-backed (8.4, immutato).
2. Se zero spans → branch A `no_verified_claims` (8.4, immutato).
3. Se almeno uno span non verified-backed → branch C
   `unverified_spans_present` (8.4, immutato).
   **Nessuna consultazione source quality**: il problema è già a livello
   verifica testuale e ha priorità.
4. Se tutti gli span sono verified-backed → applicare la fase
   Source Quality:
   - Se almeno uno span ha condizione di block (§6.1) → branch C'
     `source_quality_block` (nuovo), `rejected`, emissione gap kind
     `source_quality_block`. Eventuali warning su altri span vengono
     comunque emessi.
   - Altrimenti, se almeno uno span ha condizione di warning (§6.1) →
     branch B' `all_spans_verified_with_warnings`, `approved`,
     emissione gap kind `source_quality_warning` e
     `published_answers` v1.
   - Altrimenti → branch B `all_spans_verified` (8.4, immutato),
     `approved`, no gap.

Questa priorità (CVE-lite > Source Quality) è coerente con §3
("source quality ≠ verification outcome").

### 8.5 Reason code raccomandati

- `reason_code` su `final_gate_reports`:
  - `'no_verified_claims'` (esistente)
  - `'unverified_spans_present'` (esistente)
  - `'all_spans_verified'` (esistente)
  - `'all_spans_verified_with_warnings'` (nuovo, in 8.7G-CODE)
  - `'source_quality_block'` (nuovo, in 8.7G-CODE)

- Nuovi reason code per `coverage_gap_statements.details.reason_code`:
  - `'source_quality_unsuitable'`
  - `'source_quality_contradicted_by_stronger_source'`
  - `'source_quality_conflicting_sources'`
  - `'source_quality_unknown'`
  - `'source_quality_weak'`
  - `'source_quality_unchecked'`
  - `'source_quality_missing_assessment'`

I reason_code testuali su `details` non richiedono migration (è
JSONB).

### 8.6 Cosa NON cambia

- `_upsert_gate_run`: invariato.
- `_upsert_gate_report`: invariato come signature, ma il payload può
  arricchirsi con un campo `source_quality_summary` (counts).
- `_upsert_published_answer`: invariato.
- `_upsert_coverage_gap`: invariato (accetta già qualunque `kind`
  che superi il CHECK).
- `_select_latest_draft_for_task`, `_select_spans_with_links`:
  invariati o estesi opzionalmente.

---

## 9. Algoritmo proposto per 8.7G-CODE

Pseudocodice non implementativo. La codifica reale è 8.7G-CODE.

```text
function run_final_answer_gate_v2(conn, tenant_id, project_id, task_id):
    draft = _select_latest_draft_for_task(conn, task_id)
    if draft is None:
        return {decision: 'no_draft', ...}

    agent_run_id = _upsert_gate_run(conn, ...)
    rows = _select_spans_with_links(conn, draft.id)
    spans = aggregate_by_span(rows)              # invariato 8.4

    # ----- 8.4 branches (preserved) -----
    if len(spans) == 0:
        emit_gap(kind='missing_evidence', gap_key='no_verified_claims', severity='block')
        return upsert_report_and_return('rejected', 'no_verified_claims', ...)

    if any(span.verified == False for span in spans):
        for span in spans where span.verified == False:
            emit_gap(kind='unverified_claim', gap_key=f'span:{span.id}', severity='block')
        return upsert_report_and_return('rejected', 'unverified_spans_present', ...)

    # ----- 8.7G new branch -----
    # All spans are verified-backed (8.4 ALL-verified branch). Now
    # consult Source Quality on the evidence_spans supporting each
    # span.
    sq_per_span = compute_source_quality_aggregate_per_span(
        conn, draft.id, spans
    )
    # sq_per_span[span_id] = {
    #     'block_reasons':   [reason_code, ...],
    #     'warning_reasons': [reason_code, ...],
    #     'details':         {evidence_span_id: {oq, cs}, ...}
    # }

    blocked_spans  = [s for s in spans if sq_per_span[s.id].block_reasons]
    warning_spans  = [s for s in spans
                      if not sq_per_span[s.id].block_reasons
                      and sq_per_span[s.id].warning_reasons]

    if blocked_spans:
        for s in blocked_spans:
            emit_gap(kind='source_quality_block',
                     gap_key=f'span:{s.id}:source_quality_block',
                     severity='block',
                     details={'reasons': sq_per_span[s.id].block_reasons,
                              'per_evidence': sq_per_span[s.id].details})
        # Optionally emit warning gaps for other spans
        for s in warning_spans:
            emit_gap(kind='source_quality_warning',
                     gap_key=f'span:{s.id}:source_quality_warning',
                     severity='warn',
                     details={'reasons': sq_per_span[s.id].warning_reasons,
                              'per_evidence': sq_per_span[s.id].details})
        return upsert_report_and_return('rejected', 'source_quality_block', ...)

    if warning_spans:
        for s in warning_spans:
            emit_gap(kind='source_quality_warning',
                     gap_key=f'span:{s.id}:source_quality_warning',
                     severity='warn',
                     details={...})
        report_id = upsert_report('approved', 'all_spans_verified_with_warnings', ...)
        publish_id = _upsert_published_answer(conn, ..., report_id, draft.summary_text)
        return {...}

    # Clean path: identical to 8.4 ALL-verified.
    report_id = upsert_report('approved', 'all_spans_verified', ...)
    publish_id = _upsert_published_answer(conn, ..., report_id, draft.summary_text)
    return {...}
```

### 9.1 Helper proposti (logici, non implementativi)

- `compute_source_quality_aggregate_per_span(conn, draft_id, spans) -> dict`
  - una sola query (vedi §8.3) seguita da aggregazione in Python;
  - per ciascuno span verified-backed, raccoglie i suoi
    evidence_span_id e i rispettivi `latest_assessment` (None se
    mancante);
  - applica la matrice della §6.1 e separa block_reasons da
    warning_reasons.
- `emit_gap(...)`: wrapping di `_upsert_coverage_gap` esistente. Non
  introduce nuova logica.

### 9.2 Idempotenza

- `_upsert_coverage_gap` è già idempotente via UNIQUE
  `(draft_final_answer_id, kind, gap_key)`. Le nuove kind partecipano
  al constraint in modo nativo grazie alla migration 0008. Una doppia
  invocazione del gate non duplica gap.
- `_upsert_gate_report` ha UNIQUE su `draft_final_answer_id`, quindi
  una redelivery del consumer non duplica il report. La policy 8.7G
  NON dipende dall'ora corrente: dato lo stesso latest source quality
  e lo stesso draft, la decision è deterministica e idempotente.

### 9.3 Comportamento con `task.publication_held` resume

In 8.4, un task in `compiling` con `final_gate_reports` esistente
viene finalizzato da `_finalize_from_existing_gate_report` senza
rieseguire il gate. Questo comportamento si preserva: se 8.7G ha
emesso un `final_gate_reports` con `reason_code='source_quality_block'`,
la riconsegna del consumer NON rivaluta source quality, e
`_revert_to_analyzed_partial_with_held` produce
`task.publication_held` come per gli altri rejected. Nessuna
modifica al consumer è richiesta.

---

## 10. Test plan per 8.7G-CODE

I test sotto sono **proposte**, da scrivere effettivamente in
8.7G-CODE. Nessun test viene scritto in 8.7G-PRE.

### 10.1 Test unit del gate (worker-level, DB-real)

Suffisso file proposto: `apps/worker/tests/test_final_answer_gate_source_quality.py`.

| # | Scenario | Setup | Atteso |
|---|---|---|---|
| 1 | Span senza alcun assessment (latest mancante) | un task con verified_fact su uno span; NON eseguire 8.7E | gate emette warning kind `source_quality_warning` con `details.reasons=['source_quality_missing_assessment']`; decision `approved`; `reason_code='all_spans_verified_with_warnings'`; `published_answers` v1 esiste |
| 2 | Latest `overall_quality='unknown'` | mock 8.7D normale | warning kind `source_quality_warning`; `details.reasons` include `source_quality_unknown` e probabilmente `source_quality_unchecked`; decision `approved`; `published` |
| 3 | Latest `overall_quality='weak'` | seed manuale di una sqa con `overall_quality='weak'` | warning kind `source_quality_warning`; `details.reasons` include `source_quality_weak`; decision `approved`; `published` |
| 4 | Latest `overall_quality='unsuitable'` | seed manuale `overall_quality='unsuitable'` | block kind `source_quality_block`; `details.reasons` include `source_quality_unsuitable`; decision `rejected`; `reason_code='source_quality_block'`; nessun `published_answers`; `task` arriva a `analyzed_partial` con audit `task.publication_held` |
| 5 | `contradiction_status='contradicted_by_stronger_source'` | seed manuale | block analogo a #4; `details.reasons` include `source_quality_contradicted_by_stronger_source` |
| 6 | `contradiction_status='conflicting_sources'` | seed manuale | block analogo a #4; reason `source_quality_conflicting_sources` |
| 7 | Multiple assessments versionati per stesso evidence_span | seed v1 `weak`, v2 `unsuitable` | block (usa v2 latest) |
| 8 | Multiple assessments versionati con miglioramento | seed v1 `unsuitable`, v2 `strong` | clean approved (usa v2 latest); nessun gap source_quality |
| 9 | Multiple evidence_span supportano stesso span: tutti `unknown` | mock 8.7D, claim con 2 evidence_span | un singolo warning per span (worst-on-warn) |
| 10 | Multiple evidence_span: uno `strong`, uno `weak` | seed misto | warning (any-on-warn) |
| 11 | Multiple evidence_span: uno `adequate`, uno `unsuitable` | seed misto | block (worst-on-block) |
| 12 | Source quality 8.7E failure non impedisce pipeline | monkeypatch orchestrator a raise | 8.7E audit `status='failed'`; 8.7G branch missing_assessment per ogni span; warning emessi; `published` (perché il mock non emette block reasons) |
| 13 | Audit chain valida post-8.7G in tutti gli scenari sopra | run `verify_task_audit_chain` | `ok=True` |
| 14 | No regressione 8.4 approved | task normale con mock | sequenza audit `task.published`, plus eventuali gap warning; nessun rejected |
| 15 | No regressione 8.4 rejected zero-verified | task con quote_hash sbagliato | `unverified_spans_present` o `no_verified_claims` come 8.4, source_quality NON consultata (priorità CVE-lite > SQ) |
| 16 | No regressione idempotenza | doppia esecuzione gate sullo stesso draft | nessuna duplicazione gap/report/published |

### 10.2 Test consumer-level (DB-real)

File proposto: estendere `apps/worker/tests/test_consumer_with_documents.py`
oppure file nuovo `apps/worker/tests/test_consumer_with_source_quality_block.py`.

Scenari minimi:

- task con seed che produce un evidence_span con `overall_quality='unsuitable'`
  → sequenza audit finisce con `task.publication_held`,
  `task_masters.status='analyzed_partial'`,
  `final_gate_reports.reason_code='source_quality_block'`,
  un `coverage_gap_statements` di kind `source_quality_block`.
- task con seed che produce warning (mock standard) → sequenza audit
  finisce con `task.published`, ma `coverage_gap_statements` di kind
  `source_quality_warning` esistono.

### 10.3 Test API (read endpoints)

Le read API 8.7F NON cambiano in 8.7G. Resta tuttavia raccomandato
aggiungere a `apps/api/tests/test_final_answer_gate_read.py` (file
ipotetico) un test che verifichi che:

- `GET /api/v1/tasks/{task_id}/final-gate-report` espone i nuovi
  `coverage_gap_statements` di kind `source_quality_*`;
- `GET /api/v1/tasks/{task_id}/source-quality` resta read-only e
  invariato (no regressione 8.7F).

### 10.4 Test migration 0008

Se la 0008 viene scritta, test minimi suggeriti:

- migration applicata: CHECK su `coverage_gap_statements.kind` accetta
  i due nuovi valori e respinge ancora gli sconosciuti;
- UNIQUE composito `(draft_final_answer_id, kind, gap_key)` continua a
  funzionare con i nuovi kind.

### 10.5 Test escluso esplicitamente

- Nessun test su provider AI reali.
- Nessun test su retention.
- Nessun test su RBAC.
- Nessun test che richieda di backfillare task pre-8.7E.

---

## 11. Rischi residui

### 11.1 Rischi specifici a 8.7G

- **Falsa sicurezza del warning.** Un task `published` con
  `source_quality_warning` può essere percepito da consumer ingenui
  come "approvato" senza distinguo. Mitigazione: la read API
  `final-gate-report` espone i gap; la UI futura deve renderli
  visibili. Per ora il rischio resta documentato.
- **Mock evaluator ⇒ warning universali.** Ogni task post-8.7G
  produrrà un set di warning costante. Questo è il prezzo di non
  modificare il mock in 8.7G. Quando un evaluator reale entrerà, i
  warning diventeranno informativi. Fino ad allora, sono rumore
  strutturale.
- **Coerenza tra latest globale e latest restituito da 8.7F.** Le read
  API 8.7F espongono `latest_assessment` come "ultimo nello slice
  restituito" (semantica documentata in
  `apps/api/app/routes/source_quality.py`). Il Gate, invece, deve
  leggere il latest **assoluto** (massimo `version_no` per
  evidence_span). I due "latest" possono coincidere o no a seconda del
  parametro `limit` dell'API. Mitigazione: nel Gate non si usa l'API,
  si fa una query SQL diretta che usa `ORDER BY version_no DESC LIMIT 1`
  (vedi §8.3). Da segnalare nei commenti del codice 8.7G-CODE.
- **N+1 query.** L'algoritmo §9 può degenerare in O(n_spans * n_evidence)
  se implementato ingenuamente. Mitigazione: una singola query con
  `LATERAL` come in §8.3.
- **Race con redelivery / SAVEPOINT 8.7E failure.** Se 8.7E è fallito
  per un task (audit `status='failed'`), gli evidence_span di quel
  task non hanno assessment. La policy 8.7G interpreta missing
  assessment come warning (non block). Conseguenza: un task il cui
  8.7E fallisce può ancora arrivare a `published` con un warning.
  Coerente con il principio "8.7E failure non blocca 8.4". Accettato.
- **Ordering tra branch.** Se in 8.7G-CODE si invertisse l'ordine
  (Source Quality block prima di unverified_spans_present),
  un task con uno span non verified-backed E un evidence_span
  `unsuitable` produrrebbe `source_quality_block` invece di
  `unverified_spans_present`. La priorità raccomandata in §8.4 (CVE-lite
  > Source Quality) va rispettata. Mitigazione: documentata in §8.4 e
  testata in #15 del test plan.

### 11.2 Rischi dalla migration 0008

- **Numerazione collisione retention.** Se 0008 viene assegnato a
  source_quality_kind, la retention futura prende 0009 o successivo.
  Va aggiornato `PROJECT_STATE.md` in 8.7G-CODE, NON in 8.7G-PRE.
- **Modifica del CHECK.** Drop e ricreazione di
  `coverage_gap_statements_kind_check` deve essere fatta in una
  transazione singola, come già fatto per `task_masters_status_check`
  in 0005. Pattern collaudato.
- **Trigger append-only mancante su coverage_gap_statements.** Oggi
  `coverage_gap_statements` non ha trigger append-only. 8.7G NON
  introduce trigger append-only (sarebbe un cambio di scope rispetto a
  questo blocco) ma rispetta operativamente la convenzione
  insert-only. Se in futuro si volesse rendere append-only "vero",
  servirà un blocco dedicato. Documentato come debito.

### 11.3 Rischi dalla policy P1+P3+P4 stessa

- **`conflicting_sources` come block è opinabile.** Sarebbe
  semanticamente più corretto un branch "hold for human review", ma
  `task_masters.status` non si estende. Bloccare e mettere il task in
  `analyzed_partial` con `task.publication_held` è il compromesso
  meno cattivo: il task è osservabile, non pubblica, e può essere
  ripreso (oggi solo manualmente) quando il conflict si risolve.
- **`unsuitable` block è permanente per quel draft.** Il gate è
  append-only sul `final_gate_reports`. Un draft rejected per
  source_quality_block resta rejected. Un nuovo draft (vN+1) sarebbe
  necessario per ritentare. In MVP-0 il compiler produce solo v1, e
  non c'è path applicativo per emettere v2. Conseguenza: un task
  bloccato da source_quality non ha oggi un "ripristino". Coerente
  con il design 8.4 ("v1 only") e accettabile in MVP-0. Documentato
  in `PHASE_8_7_PLAN.md §11` (no recompile in MVP-0).
- **Calibrazione con evaluator reale.** Quando un evaluator reale
  entrerà, la policy P1+P3+P4 dovrà essere rivalutata. In particolare
  P2 (block su weak) potrebbe diventare difendibile con P5
  (disclosure). 8.7G-CODE deve restare facile da rivedere: la
  raccomandazione operativa è di centralizzare la matrice di
  classificazione (block/warning/clean) in un'unica funzione helper
  in `final_answer_gate.py` o in un modulo separato
  `source_quality_policy.py`.

### 11.4 Rischi documentali

- **"unknown" non significa "approvato".** Va martellato nei docs
  ogni volta che si parla di source_quality, in 8.7G-CODE e dopo.
  Già richiamato in `PROJECT_STATE.md` e in `PHASE_8_7_PLAN.md §11`.
- **Tag anti-hallucination.** Continuare a usare la formula
  "riduce il rischio di pubblicare claim non supportati o basati su
  fonti inadeguate". Mai "elimina allucinazioni". Vincolo di prompt
  rispettato in tutto questo documento.

---

## 12. Decisione finale

### 12.1 Policy MVP-0 per 8.7G-CODE: P1 + P3 + P4

- **Block** (decision = `rejected`, `reason_code = 'source_quality_block'`):
  - `overall_quality = 'unsuitable'`, OR
  - `contradiction_status ∈ {contradicted_by_stronger_source, conflicting_sources}`.

- **Warning** (decision invariata, gap `severity='warn'`):
  - `overall_quality ∈ {weak, unknown}`, OR
  - `contradiction_status = 'unchecked'`, OR
  - latest assessment mancante.

- **Clean** (decision come 8.4):
  - `overall_quality ∈ {strong, adequate}` AND
    `contradiction_status ∈ {no_known_contradiction}`.

- **Aggregazione tra evidence_span dello stesso span**:
  worst-on-block, any-on-warn.

- **Priorità rispetto a 8.4**:
  CVE-lite (unverified) ha priorità sulla Source Quality. La Source
  Quality si applica solo dopo che tutti gli span sono
  verified-backed.

### 12.2 Migration 0008

Si raccomanda la migration aggiuntiva
`0008_coverage_gap_source_quality.sql` con:

- Estensione del CHECK su `coverage_gap_statements.kind` per
  ammettere `source_quality_block` e `source_quality_warning`;
- Nessuna altra modifica strutturale.

Il numero `0008` è preso al posto della futura retention, che si
sposta a `0009` o successivo. Questo va registrato in
`PROJECT_STATE.md` **in 8.7G-CODE**, non qui.

### 12.3 Cosa NON si fa in 8.7G

- Nessun nuovo state in `claim_ledger_entries`.
- Nessuna estensione di `claim_lineage.relation_kind`.
- Nessuna modifica al mock evaluator.
- Nessun cambio alla source-loss propagation.
- Nessun renderer, nessun export, nessuna UI.
- Nessun provider AI reale.
- Nessun backfill.
- Nessuna read API nuova (8.7F basta per osservare i gap; la
  `GET /tasks/{id}/final-gate-report` già esistente espone gli
  `coverage_gap_statements` aggiornati).

### 12.4 Cosa abilita questa decisione

- Pipeline approved 8.4 esistente: preservata (warning silenziosi sul
  payload, no rejected con il mock corrente).
- Pipeline rejected 8.4 esistente: preservata, perché la priorità
  CVE-lite > Source Quality blocca prima.
- Quando un evaluator reale produrrà `unsuitable` o
  `contradicted_by_stronger_source`, il sistema bloccherà
  spontaneamente, **senza nuove modifiche al Gate**, perché il branch
  è già scritto.
- Il framing "evidence-gated sull'asse qualità" diventa effettivo:
  oggi come warning, domani come block.

---

FILE_COMPLETATI
- `PHASE_8_7G_PRE.md`

FILE_DA_FARE_PROSSIMO_BLOCCO (8.7G-CODE)
- `migrations/0008_coverage_gap_source_quality.sql` (estensione CHECK
  su `coverage_gap_statements.kind`).
- `apps/worker/app/services/final_answer_gate.py` (estensione policy
  P1+P3+P4 secondo §8 e §9 di questo documento).
- Eventuale `apps/worker/app/services/source_quality_policy.py`
  (helper di classificazione block/warning/clean, opzionale).
- `apps/worker/tests/test_final_answer_gate_source_quality.py` (test
  unit per §10.1).
- Estensione `apps/worker/tests/test_consumer_with_documents.py` o
  nuovo file per §10.2.
- Test migration 0008 (§10.4).
- Aggiornamento `PROJECT_STATE.md` (rinumerazione retention a 0009,
  documentazione policy 8.7G effettiva).
- Aggiornamento `PHASE_8_7_PLAN.md` (stato 8.7G → done).
- Aggiornamento `README.md` (sezione "Source Quality Gate ora
  consumata").

RISCHI_RESIDUI
- Mock evaluator emette `unknown` per ogni span: ogni task post-8.7G
  produrrà warning costante. Rumore strutturale fino a evaluator
  reale.
- `coverage_gap_statements` non ha trigger append-only oggi. 8.7G
  rispetta insert-only operativamente, ma non c'è enforcement a DB.
- Un task bloccato da `source_quality_block` non ha oggi un path di
  ripristino (compiler v1-only). Coerente con MVP-0, ma andrà
  affrontato quando 8.4 verrà esteso a draft v2.
- `conflicting_sources` come block è un compromesso, non un hold
  vero. Un futuro stato `held_for_review` su `task_masters` o un
  flag su `final_gate_reports.decision='held_for_review'` (già nel
  CHECK di 0005, ma non usato) potrebbero rifinire la decisione in
  fasi successive.
- Priorità CVE-lite > Source Quality: documentata ma fragile. Un
  refactor incauto del Gate potrebbe invertirla. Mitigazione: test
  #15 del test plan.
- Numerazione migration 0008 vs retention futura: 0008 va a
  source_quality_kind, retention slitta a 0009. Documentazione da
  fare in 8.7G-CODE.
- Nessun provider AI reale, nessuna web search, nessun RBAC, nessun
  backfill: tutti i rischi residui di `PHASE_8_7_PLAN.md §14` restano
  in piedi.
- "unknown" continua a non significare "approvato". Rischio di
  errata interpretazione in UI o consumer esterni: martellare nei
  docs.
