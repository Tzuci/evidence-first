# ORCH-MULTI-B Implementation Report — DB-backed Source Resolution Pass

> **Report implementativo della fase `ORCH-MULTI-B`**, che ha realizzato il
> *source resolution pass* mock/local-only progettato in
> `PHASE_ORCH_MULTI_B_PRE.md`. Il pass trasforma un insieme di
> `source_candidates` proposti in un insieme di tentativi di resolution
> persistiti e auditabili. **Non recupera realmente le fonti, non verifica
> alcuna fonte, non collega alcuna fonte a un claim, non decide publishability,
> non sostituisce il Final Answer Gate.** Una `source_candidate` resta una
> proposta non verificata anche dopo essere stata "risolta"; un esito di
> resolution è un record tecnico append-only utile ad audit e debugging.
>
> Lingua: italiano tecnico, registro da System Architect.
>
> **Deliverable unico.** L'unico file prodotto da questa fase di documentazione
> è `ORCH_MULTI_B_IMPLEMENTATION_REPORT.md`. Nessun codice, test, migration,
> `README.md` o `PROJECT_STATE.md` è stato creato o modificato. **Nessun commit
> e nessun push sono stati eseguiti.**
>
> **Nota di provenienza dei dati.** Questo report è redatto a partire dal
> contenuto dei file della fase. I valori `git` (hash commit) e l'esito di
> `pytest`/`py_compile` sono riportati come **dichiarati** dalla verifica
> locale (rig `pgserver` + DDL ricostruita di `0011`); i conteggi dei test
> sono inoltre coerenti con un conteggio statico dei casi presenti nei file di
> test (vedi §8). Gli hash dei commit non sono stati verificati contro un
> checkout vivo in questa sede.

---

## 1. Status

- **Implemented** — il source resolution pass DB-backed è realizzato in
  `orchestration_source_resolution.py`.
- **Validated locally** — suite dedicata verde sul rig locale (§8).
- **Mock/local-only** — policy di resolution deterministica e onesta, senza
  recupero reale (§5, §9).
- **DB-backed** — il pass legge/scrive via una `Connection` SQLAlchemy
  posseduta dal chiamante; non apre connessioni proprie.
- **No migration** — nessun file in `migrations/`; `0001`–`0011` invariate.
- **No API** — nessuna route HTTP aggiunta o modificata.
- **No UI** — nessun componente frontend aggiunto o modificato.
- **Nessun commit eseguito da Claude**, nessun push.

---

## 2. Baseline

- **ORCH-MULTI-B-PRE** ha progettato il pass (`PHASE_ORCH_MULTI_B_PRE.md`):
  input/output contract, candidate selection, resolution policy mock/local-only,
  stato derivato del candidate, event model, idempotenza, failure semantics,
  boundedness, non-goals, test plan.
- **ORCH-MULTI-B1** ha introdotto la **logica pura**: i contratti
  request/result, i value object deterministici e le funzioni pure (classifica
  candidate, stato derivato, sort key stabile, chiave di idempotenza
  candidate-scoped, counters), senza alcun accesso al DB.
- **ORCH-MULTI-B2** ha implementato il pass **DB-backed**, componendo le
  funzioni pure di B1 in `run_source_resolution_pass` (selezione run/candidate
  tenant-scoped, ordinamento stabile, insert append-only in `source_resolutions`,
  emissione eventi `source_resolution_started`/`completed`, idempotenza e
  replay, bounded `max_candidates`).
- **Commit corrente dichiarato:** `5bac834` ("Add DB-backed source resolution
  pass"), preceduto da `9929e41` ("Add source resolution pure logic").

---

## 3. File modificati

Da `ORCH-MULTI-B2`:

- `apps/worker/app/services/orchestration_source_resolution.py`
- `apps/worker/tests/test_orchestration_source_resolution_service.py`

**Nessun altro file è stato modificato.** In particolare non sono toccati:
`orchestration_runner.py`, la provider abstraction, qualunque file in
`migrations/`, `apps/api/*`, `apps/web/*`, `packages/shared/*`, `README.md`,
`PROJECT_STATE.md`, né altri `PHASE_*` o report.

---

## 4. Implementazione

### 4.1 Contratti (value object, frozen dataclass)

- **`SourceResolutionPassRequest`** — richiesta logica di un pass su un run.
  Campi: `tenant_id`, `orchestration_run_id`, `idempotency_key`,
  `max_candidates` (default `MAX_CANDIDATES_DEFAULT = 32`),
  `candidate_selection_scope` (default `per_run`), `agent_output_id`
  (opzionale), `eligible_states` (default `("proposed",)`), `created_by`
  (opzionale). Nessun segreto transita nel contratto.
- **`SourceResolutionPassResult`** — esito del pass e id dei fatti persistiti:
  `status`, `orchestration_run_id`, `source_resolution_ids`,
  `per_candidate_outcomes`, `event_ids`, `counters`, più `publication_status`
  (default `not_evaluated`) e `gate_report_id` (default `None`).
- **`CandidateResolutionDecision`** — la decisione deterministica per un singolo
  candidate: `resolution_target_kind`, `outcome`, `failure_reason` opzionale
  (bounded e redatto).

### 4.2 Entry point — `run_source_resolution_pass(conn, request)`

DB-backed, append-only, idempotente e bounded. Scrive attraverso la `conn`
posseduta dal chiamante e **non esegue `commit()` né `rollback()`**: la
transazione è del chiamante. Sequenza:

1. **Validazione request** (`_validate_pass_request`) senza alcun accesso al DB:
   `tenant_id`, `orchestration_run_id`, `idempotency_key` non vuoti;
   `max_candidates` intero positivo; `candidate_selection_scope` ammesso;
   `agent_output_id` obbligatorio se lo scope è `per_agent_output`. In caso di
   problema ritorna un **failure result controllato** (`_failed_pass_result`)
   che non ha scritto nulla.
2. **Selezione run tenant-scoped** (`_select_run`): il run deve esistere **e**
   appartenere al tenant (`WHERE id = :run AND tenant_id = :tenant`). Run
   assente ⇒ failure result senza scrittura.
3. **Selezione candidate tenant/run scoped** (`_select_scoped_candidates`):
   filtro `tenant_id` + `orchestration_run_id`, con ulteriore filtro
   `agent_output_id` quando lo scope è `per_agent_output`. `candidates_seen`
   conta lo scope prima del bound.
4. **Stable ordering**: i candidate sono ordinati in memoria con
   `_stable_candidate_sort_key`, cioè `(agent_output_id, created_at, id)` ognuno
   coerciato a stringa con fallback a stringa vuota, così l'ordine del DB non
   introduce non-determinismo.
5. **`sequence_no` da `MAX(sequence_no)+1`** (`_next_sequence_no`):
   `COALESCE(MAX(sequence_no), -1) + 1` sullo spazio eventi del run; un run senza
   eventi parte da 0, un run esistente prosegue in modo contiguo, mai
   ripartendo da 0.
6. Per ogni candidate in ordine:
   - **Replay idempotente**: se esiste già una `source_resolutions` per
     `(candidate, idempotency_key candidate-scoped)` (`_existing_resolution`),
     gli id/outcome sono ricostruiti e i counters aggiornati **senza** inserire
     duplicati; gli event id già presenti sono recuperati
     (`_existing_event_id`).
   - **Eleggibilità dallo stato corrente**: lo stato è derivato dalla latest
     `source_resolutions` se esiste, altrimenti dallo `status` iniziale
     (append-only, mai mutato) del candidate (vedi §5). Se lo stato derivato non
     è in `eligible_states`, il candidate è contato in `skipped_count` e
     saltato.
   - **Bounded `max_candidates`**: oltre il bound i candidate eleggibili sono
     contati come `skipped_count` e lasciati a un pass successivo.
   - **Decisione mock/local-only** (`_classify_candidate`, §9), poi:
     - emissione **`source_resolution_started`** (riusata se già presente sotto
       la stessa chiave);
     - **insert append-only** in `source_resolutions`
       (`_insert_source_resolution`) con `retrieved_artifact_ref` /
       `retrieved_artifact_hash` a `NULL` (nessun recupero);
     - emissione **`source_resolution_completed`** con payload redatto.
   - Aggiornamento `per_candidate_outcomes` e dei **counters**
     (`candidates_attempted` + `_increment_counters_for_outcome`).
7. **Risultato**: un run esistente rende il pass `succeeded` anche con zero
   candidate. `publication_status` resta `not_evaluated`, `gate_report_id`
   resta `None` in ogni percorso.

### 4.3 Idempotenza

- **`idempotency_key` candidate-scoped** (`_build_candidate_scoped_idempotency_key`):
  `f"{base_key}:{kind}:{candidate_id}"`. La radice è
  `source_resolutions UNIQUE (source_candidate_id, idempotency_key)` di `0011`;
  le chiavi degli eventi includono il discriminante per candidate così da non
  collidere su `orchestration_events_run_type_idem_uq`.
- **Replay senza duplicati**: un secondo pass con le stesse chiavi non inserisce
  righe `source_resolutions` né eventi duplicati; ricostruisce e ritorna i fatti
  già persistiti.

### 4.4 Redaction (self-contained)

`_redact_failure_reason` maschera i frammenti `name=value` / `name: value`
sensibili con `[REDACTED]` e tronca la stringa a una lunghezza bounded
(`_MAX_FAILURE_REASON_LEN = 500`). Il modulo mantiene il proprio helper senza
accoppiarsi a simboli privati cross-module.

---

## 5. Fix B2A — eleggibilità dallo stato iniziale del candidate

- `_select_scoped_candidates` ora legge anche `source_candidates.status` (la
  `SELECT` include `status` accanto a `url`, `provenance`,
  `raw_citation_payload`, ecc.).
- `_derive_current_candidate_state` usa la **latest `source_resolutions`** se
  presente: l'esito più recente è autoritativo e determina lo stato derivato.
- Se **non** esiste alcuna `source_resolutions`, lo stato corrente coincide con
  lo **status iniziale** del candidate (append-only, mai mutato).
- Un candidate con status iniziale **non eleggibile** (per esempio `rejected`,
  o `insufficient_metadata`) e senza resolution **non** viene trattato come
  `proposed` e quindi non viene preso in carico dal pass: è contato in
  `skipped_count`. Fallback difensivo a `proposed` solo quando la riga non
  porta uno status utilizzabile.
- `source_candidates` **non viene mai aggiornata**: nessun `UPDATE`/`DELETE`;
  lo stato corrente è una vista di lettura derivata dai fatti append-only.

---

## 6. Invarianti preservati

Distinzioni semantiche (vincolanti):

- **source_candidate ≠ evidence.**
- **provider citation/source ≠ evidence verificata.**
- **source resolution ≠ source verification.**
- **resolved source ≠ `evidence_span`.**
- **resolved source ≠ `claim_evidence_link`.**
- **resolved source ≠ claim pubblicabile.**
- **provider output ≠ final answer.**

Un `outcome='resolved'` significa **solo** che il target del candidate è stato
individuato o normalizzato localmente secondo la policy; non significa che la
fonte sia stata recuperata, controllata, collegata a un claim o approvata dal
Gate.

Esecuzione e scrittura — ciò che il pass **non** fa:

- **Il Final Answer Gate non è eseguito.**
- **Nessuna** scrittura in `source_verifications`.
- **Nessun** `evidence_spans`.
- **Nessun** `claim_evidence_links`.
- **Nessun** `final_gate_reports`.
- **Nessun** `published_answers`.
- **`publication_status` resta `not_evaluated`.**
- **`gate_report_id` resta `None`.**
- **No network, no HTTP, no browser.**
- **Nessuna invocazione di provider.**
- **Nessuna scrittura di `token_usage_records`** da questo pass.
- **Nessun `UPDATE` di `source_candidates`.**

---

## 7. Test

### 7.1 Compilazione

- `py_compile` del service: **OK** (dichiarato).
- `py_compile` del file di test: **OK** (dichiarato).

### 7.2 Esecuzione

- `PYTHONPATH=apps/worker python -m pytest apps/worker/tests/test_orchestration_source_resolution_service.py -q`
  → **47 passed** (dichiarato).
- `PYTHONPATH=apps/worker python -m pytest apps/worker/tests/test_orchestration_runner_service.py -q`
  → **17 passed** (dichiarato; nessuna regressione sul runner esistente).

I due totali sono **coerenti con un conteggio statico** dei casi presenti nei
file di test: la suite di source resolution conta 36 casi puri (incluse le
parametrizzazioni `_derive_state_non_success` ×5 e `_classify_external_http_url`
×4) più 11 casi DB-backed = 47; la suite del runner conta 17 funzioni di test.

### 7.3 Copertura principale

Logica pura (ORCH-MULTI-B1):

- **pure helper tests** — stato derivato, classificazione, sort key stabile,
  chiave di idempotenza candidate-scoped, counters, contratti frozen.
- **classification external URL → unreachable** (mai `resolved`).
- **insufficient metadata** → `insufficient_metadata`.
- **local marker → resolved** come **sola** normalizzazione locale
  (`uploaded_document` / `internal_document`), con il marker locale che prevale
  su un eventuale URL esterno; un valore di marker vuoto non vale come marker.
- **idempotency key candidate-scoped** — include base key, kind e candidate id;
  distinta per candidate e per kind.

DB-backed (ORCH-MULTI-B2):

- **DB creates `source_resolutions`** per i candidate, con `outcome` e
  `resolution_target_kind` dal codominio `0011`.
- **no mutation `source_candidates`** — righe byte-per-byte invariate, nessuna
  nuova riga.
- **idempotent replay** — nessun duplicato in `source_resolutions` né in
  `orchestration_events`; stessi id ricostruiti.
- **only resolution events** — gli `event_type` emessi sono un sottoinsieme di
  `{source_resolution_started, source_resolution_completed}`; in particolare
  nessun evento di verifica.
- **no `source_verifications` / `evidence_spans` / `claim_evidence_links` /
  `final_gate_reports` / `published_answers`** — delta zero; il run non collega
  alcun gate report.
- **external URL never resolved in mock-only mode** —
  `unreachable`/`insufficient_metadata`, mai `resolved`, sia in memoria sia
  persistito.
- **bounded `max_candidates`** — oltre il bound, `skipped_count` cresce e viene
  persistita una sola resolution per un pass con `max_candidates=1`.
- **tenant isolation** — candidate di un altro tenant/run non sono elaborati;
  nessuna scrittura cross-tenant.
- **per_agent_output scope** — solo il candidate dell'`agent_output_id` in scope
  produce una resolution.
- **initial non-proposed candidate skipped** — un candidate con status iniziale
  `rejected` e senza resolution è saltato (`skipped_count = 1`,
  `candidates_attempted = 0`), senza riga né evento.

---

## 8. Limiti espliciti

- **no retrieval reale** — nessun recupero di rete o di fonte.
- **no source verification** — nessuna verifica di fonti.
- **no evidence extraction** — nessun `evidence_span`.
- **no claim binding** — nessun `claim_evidence_links`.
- **no synthesis** — nessuna `candidate_syntheses`.
- **no Gate** — il Final Answer Gate non è eseguito.
- **no publication** — nessuna `published_answers`.
- **no production provider** — nessun provider esterno reale.
- **no network.**
- **`resolved` significa solo individuazione/normalizzazione locale** secondo la
  policy mock/local-only, non "fonte verificata" e non "evidenza".

---

## 9. Rischi residui / future work

- **retrieval / evidence extraction** restano fuori scope, in
  `ORCH-MULTI-C-PRE` / `ORCH-MULTI-C`.
- **source verification / claim binding** restano fuori scope, in
  `ORCH-MULTI-D-PRE` / `ORCH-MULTI-D` (lì entrerebbero in scope
  `source_verifications` e il ponte verso `evidence_spans`).
- **eventuale token usage audit futuro** deve restare distinto dal token usage
  reale di un provider LLM: se mai registrato, `pass_kind = 'source_resolution'`
  (già nel codominio `0011`), marcato come non-provider, senza presentarlo come
  consumo reale.
- **integrazione nella pipeline `task.created`** è futura e non inclusa: il pass
  resta separato dalla pipeline evidence-gated esistente.

---

## 10. Comandi di verifica

> Comandi di sola lettura / verifica. *Nessun commit è eseguito.*

```bash
python3 -m py_compile apps/worker/app/services/orchestration_source_resolution.py
python3 -m py_compile apps/worker/tests/test_orchestration_source_resolution_service.py

PYTHONPATH=apps/worker python -m pytest \
  apps/worker/tests/test_orchestration_source_resolution_service.py -q

PYTHONPATH=apps/worker python -m pytest \
  apps/worker/tests/test_orchestration_runner_service.py -q

git diff --check
git diff --stat
git diff --name-only

grep -RniE "[t]ruth score|[v]erified true|[v]erified answer|[A]I verified|[f]actually true|[h]allucination eliminated|[h]allucination-free|[g]uaranteed truth|[z]ero hallucinations|[e]ntailed = true|[s]ource quality proves claim|[C]VE-lite proves support|[r]eal NLI|[c]ontradiction detector|[c]itation-to-claim validator" \
  ORCH_MULTI_B_IMPLEMENTATION_REPORT.md || true
```

`git diff --check` non deve segnalare errori di whitespace; il controllo
`grep` del wording vietato deve restituire nulla.

---

*Fine `ORCH_MULTI_B_IMPLEMENTATION_REPORT.md`. Nessun commit, nessun push.*
