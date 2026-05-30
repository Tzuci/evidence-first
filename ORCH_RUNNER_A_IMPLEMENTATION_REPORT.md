# ORCH-RUNNER-A IMPLEMENTATION REPORT

> Relazione finale della fase **ORCH-RUNNER-A**, prodotta per revisione
> umana. Repo: `Tzuci/evidence-first`, branch `main`. Lingua: italiano
> tecnico. **Nessun commit è stato eseguito.**

---

## 1. Scope

ORCH-RUNNER-A implementa il **primo orchestration runner single-agent
mock** progettato in `PHASE_ORCH_RUNNER_PRE.md`. È un servizio
worker-level che esegue **un** run mock end-to-end, single-agent,
single-pass, e ne persiste tutti i fatti sulle tabelle introdotte da
ORCH-SCHEMA-A (migration `0011_orchestration_schema.sql`), componendo il
`MockProviderAdapter` e le funzioni pure di mapping di ORCH-PROVIDER-A
(`apps/worker/app/services/orchestration_provider.py`).

La fase consiste esclusivamente in:

- un modulo worker-level `apps/worker/app/services/orchestration_runner.py`;
- un file di test worker-level, DB-backed,
  `apps/worker/tests/test_orchestration_runner_service.py`;
- questa relazione.

Il runner è: deterministico dove il mock è deterministico, idempotente,
DB-backed, auditabile, single-agent, single-pass, mock-only, no-network,
no-Redis, no-FastAPI, no-UI, no-final-gate.

ORCH-RUNNER-A **non** implementa: provider reali, SDK provider, rete,
Redis, FastAPI, endpoint, UI, migration, modifiche allo schema,
CandidateSynthesis, Claim Extraction, Evidence Binding, Source
Resolution, Source Verification, Final Answer Gate, multi-agent,
reviewer/critic/synthesizer, retry reale, local LLM. Non aggiunge
dipendenze.

---

## 2. Files created/modified

Esattamente tre file, tutti nuovi:

- `apps/worker/app/services/orchestration_runner.py` — **nuovo**. Il
  servizio runner.
- `apps/worker/tests/test_orchestration_runner_service.py` — **nuovo**.
  Test worker-level DB-backed, 10 test richiesti dal prompt §12.
- `ORCH_RUNNER_A_IMPLEMENTATION_REPORT.md` — **nuovo**. Questa relazione.

Nessun altro file del repository è stato creato o modificato. In
particolare non sono stati toccati: `migrations/*`, i test root
(`tests/*`), `apps/api/*`, `apps/web/*`, `packages/shared/*`,
`README.md`, `PROJECT_STATE.md`, i file `PHASE_*_PRE.md`, gli altri
`*_IMPLEMENTATION_REPORT.md`, i file di package/lock, `.env*`, i file
Docker, il `Makefile`. Nessuna dipendenza è stata aggiunta.

---

## 3. Public API del runner

Il modulo espone due dataclass `frozen` e una funzione di servizio.

`OrchestrationRunnerRequest` — input logico del run:
`tenant_id`, `project_id`, `master_prompt_version_id`, `agent_config_id`,
`idempotency_key`, `mode="multi_ai_orchestration"`,
`execution_mode="independent"`, `token_budget_id=None`,
`mock_source_candidates=()`, `mock_error_code=None`,
`mock_error_message=None`, `created_by=None`. Nessun secret viaggia nel
contratto: nessuna API key, nessun token di autenticazione, nessun
Authorization header.

`OrchestrationRunnerResult` — output logico del run:
`status`, `orchestration_run_id`, `agent_run_id`,
`provider_invocation_id`, `agent_output_id`, `token_usage_record_ids`,
`agent_message_ids`, `source_candidate_ids`, `event_ids`, `error_code`,
`error_message`, `is_mock`, `publication_status`, `gate_report_id`.
`publication_status` è sempre `not_evaluated` e `gate_report_id` è sempre
`None`: il gate non è integrato.

Funzione principale:

```python
def run_single_agent_mock_orchestration(
    conn: Connection,
    request: OrchestrationRunnerRequest,
) -> OrchestrationRunnerResult:
    ...
```

Riceve una `sqlalchemy.engine.Connection` **posseduta dal chiamante**;
usa `sqlalchemy.text`; **non** usa l'ORM; **non** apre connessioni
proprie; **non** fa `commit()` né `rollback()` (il chiamante possiede la
transazione, coerente con `PHASE_ORCH_RUNNER_PRE.md §17.5`). Costanti
d'identità: `RUNNER_NAME="mvp0_mock_orchestration_runner"`,
`RUNNER_VERSION="0.1.0"`.

---

## 4. Transaction model implementato

Il runner segue la **sequenza ordinata vincolante** di
`PHASE_ORCH_RUNNER_PRE.md §17.1`, in un'unica transazione posseduta dal
chiamante. L'`agent_run_id`, il `run_id` e lo `snapshot_id` sono
**preallocati in memoria** (`uuid.uuid4()`) prima di qualunque scrittura.

Path nominale (success):

1. `INSERT orchestration_runs` con `status='pending'`.
2. evento `run_created` (`sequence_no=0`,
   `idempotency_key=<run_idem>:run_created`).
3. `UPDATE orchestration_runs SET status='running', started_at=NOW()`
   (transizione materializzata; il codominio `0011` non ha un event_type
   dedicato).
4. `INSERT agent_config_snapshots` (snapshot immutabile della config).
5. costruzione della `ProviderRequest` con l'`orchestration_agent_run_id`
   preallocato, così `request_hash` è già consistente con la riga di
   fatto.
6. **preflight budget** via `op.enforce_mock_budget` PRIMA
   dell'invocazione.
7. evento `agent_run_started` (`sequence_no=1`,
   `related_entity_type='orchestration_agent_run'`,
   `related_entity_id=<agent_run_id preallocato>`).
8. invocazione `MockProviderAdapter.invoke` **in memoria** (deterministica,
   senza rete).
9. `INSERT orchestration_agent_runs` **una sola volta** con lo status
   finale (`succeeded`/`failed`), `attempt_no=1`, `started_at`/
   `completed_at` valorizzati. Append-only: nessuna riga `running`
   intermedia, nessun UPDATE successivo (`§10`).
10. righe FK-bound (solo dopo la riga agent_run):
    `orchestration_agent_messages` (`system` seq0, `user` seq1,
    `assistant` seq2 solo su success); `provider_invocations` (sempre,
    anche su failure); `token_usage_records` (sempre dopo l'invocazione,
    **sia su success sia su provider failure**, `pass_kind='independent_answer'`,
    `provider_invocation_id` valorizzato); e **solo su success**:
    `orchestration_agent_outputs` (seq0, `output_kind='mock_candidate_text'`)
    e `source_candidates` (+ evento `source_candidate_created` per candidate).
11. evento terminale `agent_run_completed` (success) oppure
    `agent_run_failed` + `run_failed` (failure).
12. `UPDATE orchestration_runs` a `completed` o `failed`,
    `completed_at=NOW()`, `failure_reason` redatto su failed.

Path budget exceeded (preflight fallito): eventi `token_budget_exceeded`
+ `run_failed`, `status='failed'`; **nessuna** riga
`orchestration_agent_runs`, **nessuna** `provider_invocations`,
**nessuna** `token_usage_records`, **nessun** output; ritorno failed.

**Ordine FK rispettato**: `orchestration_agent_messages`,
`provider_invocations`, `orchestration_agent_outputs` e
`token_usage_records` hanno FK verso `orchestration_agent_runs(id)` e
sono scritte solo dopo l'`INSERT` della riga agent_run. L'evento
`agent_run_started` precede la riga di fatto referenziandone via
`related_entity_id` l'UUID preallocato (`related_entity_id` non è una FK
in `0011`).

I JSONB (`bounding_parameters`, `event_payload`, `snapshot_payload`,
`structured_payload`, `provenance`, `raw_citation_payload`) sono inseriti
via `json.dumps(...)` con `CAST(:p AS JSONB)`. `cost_estimate` è
convertito con `float(...)` prima dell'INSERT (colonna
`DOUBLE PRECISION`).

---

## 5. Idempotency behavior

L'idempotenza è ancorata alla UNIQUE `(tenant_id, idempotency_key)` di
`orchestration_runs`. All'inizio il runner esegue un `SELECT` del run
esistente per `(tenant_id, idempotency_key)`:

- se esiste, **non scrive nulla** e ritorna un risultato di replay
  ricostruito ri-interrogando i fatti già persistiti (run, agent_run,
  provider_invocation, output, messaggi, usage, source candidates,
  eventi). Lo `status` di replay mappa `completed → 'succeeded'`,
  `failed → 'failed'`, e `pending`/`running` sono esposti grezzi (scelta
  documentata, `§16`).
- se non esiste, procede a creare ed eseguire il run.

Le chiavi di idempotenza degli eventi: `<run_idem>:<event_type>` per gli
eventi scritti una sola volta per run (`run_created`,
`agent_run_started`, `agent_run_completed`, `agent_run_failed`,
`run_failed`, `token_budget_exceeded`); `<run_idem>:source_candidate:<index>`
per gli eventi `source_candidate_created`, uno per candidate, così la
UNIQUE composita `(orchestration_run_id, event_type, idempotency_key)` di
`0011` non rifiuta il secondo evento dello stesso tipo. Un secondo
delivery dello stesso run con lo stesso input non duplica run, eventi,
provider_invocations, token_usage_records o source_candidates.

---

## 6. Source candidates behavior

Le source candidate sono persistite **solo su success e solo se l'output
esiste**, usando `op.source_candidates_to_records(...)` con
`agent_output_id` valorizzato. Ogni riga ha `status='proposed'`,
`candidate_type='agent_cited'`, `master_prompt_id=None`, e una
`provenance` che dichiara esplicitamente `is_verified=False`. Nessun
`evidence_span_id` (la colonna non esiste in `source_candidates`), nessun
claim link, nessuna `source_resolutions`, nessuna `source_verifications`.
Per ogni candidate viene appeso un evento `source_candidate_created` con
`related_entity_type='source_candidate'`,
`related_entity_id=<source_candidate_id>` e `idempotency_key` distinto.
Una source candidate resta una **risposta candidata non verificata**: non
è evidence e non può contribuire al gate.

---

## 7. Budget behavior

Se l'input porta un `token_budget_id`, il runner legge la riga
`token_budgets`, ne valida la compatibilità (tenant; e, se valorizzati,
`agent_config_id` e `master_prompt_id`), e ne copia `token_limit` /
`overflow_policy` sia in `bounding_parameters` di `orchestration_runs`
sia nello `snapshot_payload` di `agent_config_snapshots` (snapshot
immutabile al run start). Il preflight è eseguito via
`op.enforce_mock_budget(provider_request, budget_limit_tokens=<limite>)`
**prima** dell'invocazione del mock. Se il budget è superato, il provider
**non** viene invocato: vengono appesi `token_budget_exceeded` e
`run_failed`, lo stato diventa `failed`, e non si scrivono agent_run,
provider_invocation, usage o output. `budget_exceeded` è non-retryable.
Senza `token_budget_id` il preflight è un no-op. Il costo mock è
`Decimal("0")`: il budget di costo non è esercitato in ORCH-RUNNER-A.

Semantica dell'usage rispetto al fallimento: un **provider failure dopo
l'invocazione** del mock persiste `provider_invocations` **e**
`token_usage_records` (l'invocazione è avvenuta); un **`budget_exceeded`
in preflight** non persiste né `provider_invocations` né
`token_usage_records` (il provider non è mai invocato). In entrambi i
casi l'usage eventualmente registrato è **mock, solo per audit/debug,
non un costo reale** (`is_mock=True`, `tokens_output=0` sul failed
result).

---

## 8. Failure behavior

Coerente con `PHASE_ORCH_RUNNER_PRE.md §18`:

- **provider error injection** (`mock_error_code`): la riga
  `orchestration_agent_runs` è scritta una sola volta con `status='failed'`,
  `error_code` normalizzato e `failure_reason` redatto; `provider_invocations`
  è scritto con `status='failed'`; **`token_usage_records` è scritto** (il
  provider è stato invocato: usage mock, `provider_invocation_id`
  valorizzato, `pass_kind='independent_answer'`, `tokens_output=0`,
  `is_mock=True`); eventi `agent_run_started`, `agent_run_failed`,
  `run_failed`; nessun output, nessuna source candidate.
- **budget exceeded (preflight)**: il provider **non** è invocato, quindi
  nessuna riga agent_run, nessuna provider_invocation e **nessuna
  `token_usage_records`**; eventi `token_budget_exceeded` + `run_failed`.
- **input non valido** (`idempotency_key` vuota, `mode` fuori codominio,
  `execution_mode != independent`, provider/model non mock, tenant
  mismatch, master_prompt mismatch, budget incompatibile, entità non
  trovate): il runner fallisce **prima** di qualunque scrittura DB e
  ritorna failed con `orchestration_run_id=None`. Nessuna riga creata.

`run_failed` è sempre appeso a fallimento del run; `agent_run_failed`
solo se la riga agent_run con `status='failed'` è stata inserita (cioè se
lo start logico era stato attivato). Ogni `error_message` persistito su
`orchestration_runs.failure_reason`,
`orchestration_agent_runs.failure_reason`,
`provider_invocations.error_message` e sui payload di evento passa per la
redaction (vedi §10). Nessun `published_answers`, nessun
`final_gate_reports` in alcun branch.

---

## 9. No gate / no publication invariant

Il runner **non integra il Final Answer Gate** e non valuta la
pubblicabilità:

- `orchestration_runs.final_gate_report_id` resta **NULL** per ogni run;
- `OrchestrationRunnerResult.publication_status` è sempre
  `not_evaluated` e `gate_report_id` è sempre `None`;
- nessuna riga in `final_gate_reports`, `published_answers`,
  `candidate_syntheses`, `source_resolutions`, `source_verifications`;
- nessuna Claim Extraction, nessun Evidence Binding, nessun synthesis
  pass.

`orchestration_runs.status='completed'` significa solo che il mock run è
terminato senza errore di runner/provider: **non** implica che il
contenuto sia supportato, né che la pubblicazione sia consentita. Il
Final Answer Gate resta l'unico gate di pubblicazione; lo schema `0011`
rende strutturalmente impossibile pubblicare una risposta saltando il
gate.

---

## 10. Security/redaction

- **Nessun secret nell'input** del runner né nello `snapshot_payload`:
  nessuna API key, token di autenticazione, credenziale, Authorization
  header, password. Il provider è mock; nessuna chiave è attesa.
- **Redaction degli error message.** Ogni messaggio d'errore persistito
  passa per un'unica funzione `_redact`, che delega a
  `op._safe_error_message` di ORCH-PROVIDER-A: maschera i segreti nella
  forma `name=value` / `name: value` con `[REDACTED]` e tronca a
  `_ERROR_MESSAGE_MAX_LEN`. La redaction è applicata a
  `provider_invocations.error_message` (sovrascritto rispetto al record
  prodotto dalle funzioni pure quando il runner usa il `mock_error_message`
  fornito dal chiamante), `orchestration_agent_runs.failure_reason`,
  `orchestration_runs.failure_reason` e ai payload degli eventi di
  fallimento.
- **`provider_invocations` senza colonne segrete**: lo schema `0011` non
  ha colonne per credenziali; il dict prodotto da
  `to_provider_invocation_record` non contiene campi segreti.
- **Hash per audit**: `request_hash`/`response_hash` su
  `provider_invocations`, `content_hash` su messaggi/output,
  `master_prompt_text_hash` su `orchestration_runs` servono audit,
  debugging e idempotenza; non provano il contenuto e non garantiscono la
  verità fattuale.
- `redaction_strategy` (`hash_only` di default) è persistito su ogni
  `provider_invocations`.

---

## 11. Tests implemented

File `apps/worker/tests/test_orchestration_runner_service.py`. Test
worker-level, **DB-backed, mock-first, senza API, senza Redis, senza
FastAPI, senza rete**. Il runner riceve una `Connection` posseduta dal
test; ogni test apre una connessione, apre una transazione, esegue il
runner, rilegge i fatti nella **stessa** transazione e fa rollback al
teardown (le scritture del runner sono non committate ma visibili alle
letture in-transaction; il DB resta pulito fra i test). Le migration sono
applicate una volta per sessione tramite `scripts/migrate.py`
(`cmd_apply`) su una connessione psycopg una-tantum, come fa il sibling
`tests/test_orch_schema_constraints.py`. Se `DATABASE_URL` è assente o il
DB non è raggiungibile, i test DB-backed fanno **skip pulito**. I test
normalizzano `DATABASE_URL` per supportare sia l'URL SQLAlchemy
(`postgresql+psycopg://`) sia l'URL psycopg/libpq (`postgresql://`):
la forma libpq è usata per `psycopg.connect` (probe e migration), la
forma SQLAlchemy per `create_engine`, coerentemente con
`tests/conftest.py`.

I 10 test richiesti dal prompt §12:

1. `test_successful_single_agent_mock_run_persists_auditable_facts` — run
   `completed`, `final_gate_report_id` NULL, 1 snapshot, 1 agent_run
   `succeeded` `attempt_no=1`, messaggi `system`/`user`/`assistant`,
   provider_invocation `succeeded` `is_mock`, 1 token_usage
   `independent_answer` con `provider_invocation_id` valorizzato, 1 output,
   eventi `run_created`/`agent_run_started`/`agent_run_completed`.
2. `test_provider_error_injection_fails_run_without_agent_output` —
   `mock_error_code='invalid_request'`: run `failed`, agent_run `failed`
   con `error_code`, provider_invocation `failed`, **1 `token_usage_records`
   con `provider_invocation_id` valorizzato, `pass_kind='independent_answer'`,
   `tokens_input>=1`, `tokens_output=0`, `is_mock=True`**, eventi `run_failed`
   e `agent_run_failed`, nessun output, nessuna source candidate.
3. `test_budget_preflight_blocks_before_provider_invocation` —
   `token_budget` con `token_limit=0`: run `failed`,
   `error_code='budget_exceeded'`, eventi `token_budget_exceeded` e
   `run_failed`, nessun agent_run / provider_invocation / token_usage,
   nessun `agent_run_started`.
4. `test_idempotency_replay_returns_existing_run_without_duplicates` —
   doppia chiamata con stessa `(tenant_id, idempotency_key)`: stesso
   `orchestration_run_id`, count run/provider_invocations/token_usage/output
   tutti `=1`, stessi `event_ids`.
5. `test_source_candidates_are_persisted_as_unverified_proposed_candidates`
   — 2 candidate: `status='proposed'`, `candidate_type='agent_cited'`,
   `provenance.is_verified=False`, 2 eventi `source_candidate_created` con
   `idempotency_key` distinti, nessuna resolution.
6. `test_runner_does_not_create_gate_or_publication_rows` — delta zero su
   `final_gate_reports` e `published_answers`; zero
   `candidate_syntheses`/`source_resolutions`/`source_verifications` per il
   run; `final_gate_report_id` NULL.
7. `test_runner_rejects_non_mock_provider_or_model` — provider non mock →
   `invalid_request`; model non mock → `invalid_model`; rifiuto prima di
   qualunque scrittura (`orchestration_run_id=None`). Conteggi
   `orchestration_runs` e `provider_invocations` invariati misurati
   before/after, così il test è **rerun-safe** su DB dev non vuoto.
8. `test_error_messages_are_redacted_before_persistence` —
   `mock_error_message` con `api_key=`, `authorization: Bearer`,
   `password=`: i segreti non compaiono e `[REDACTED]` compare in
   `provider_invocations.error_message`,
   `orchestration_agent_runs.failure_reason`,
   `orchestration_runs.failure_reason` e nel `result.error_message`.
9. `test_event_sequence_numbers_are_monotonic_and_event_types_are_schema_allowed`
   — `sequence_no` contigui da 0; event_type ⊆ insieme usato dal runner
   (tutti nel codominio `0011`); assenza esplicita di event_type inventati
   (`run_started`, `provider_invocation_started`,
   `provider_invocation_completed`, `run_completed`).
10. `test_module_uses_no_network_redis_fastapi_or_provider_sdk_imports` —
    test AST/import-level: ispeziona il sorgente del modulo e vieta import
    di `requests`/`httpx`/`aiohttp`/`urllib`/`socket`/`openai`/
    `anthropic`/`google.generativeai`/`subprocess`/`fastapi`/`redis`. Non
    usa la fixture DB. La lista dei token vietati è assemblata da frammenti
    a runtime così che un `grep` ingenuo del file di test non
    auto-intercetti la lista letterale (coerente con l'avvertenza §14).

---

## 12. Commands run and results

> **Nota di ambiente — importante per la revisione.** L'ambiente di
> lavorazione **non dispone di un checkout git del repository né di un
> server PostgreSQL**, e `sqlalchemy`/`psycopg` non sono installati. Di
> conseguenza i comandi `git`, `pytest` e quelli che richiedono un DB
> vivo **non sono stati eseguiti qui**. Questo è dichiarato apertamente,
> coerentemente con le relazioni delle fasi precedenti
> (`ORCH_PROVIDER_A` e `ORCH_SCHEMA_A`) che hanno dovuto fare la stessa
> dichiarazione. I file sono stati validati con tutti i controlli statici
> disponibili offline. I comandi DB e `pytest` vanno eseguiti dal
> revisore in un ambiente con Postgres attivo e `DATABASE_URL`
> raggiungibile.

| Comando | Eseguito | Esito / atteso |
|---|---|---|
| `python3 -m py_compile apps/worker/app/services/orchestration_runner.py` | **Sì** | OK, nessun errore. `py_compile` valida la sintassi senza importare `sqlalchemy`. |
| `python3 -m py_compile apps/worker/tests/test_orchestration_runner_service.py` | **Sì** | OK, nessun errore. |
| `grep -niE "^[[:space:]]*(import\|from)[[:space:]]+(requests\|httpx\|aiohttp\|urllib\|socket\|openai\|anthropic\|google\.generativeai\|subprocess\|fastapi\|redis)\b"` sui 2 file `.py` | **Sì** | **Nessun risultato.** |
| `grep -niE "[t]ruth score\|..."` (wording vietato) sui 3 file deliverable | **Sì** | **Nessuna occorrenza** al di fuori del comando stesso citato in questa relazione. |
| `PYTHONPATH=$(pwd)/apps/worker:$(pwd)/packages/shared python3 -m pytest -q apps/worker/tests/test_orchestration_runner_service.py` | **No** | Nessun DB / `sqlalchemy` nell'ambiente. Atteso in revisione: **10 test verdi** con `DATABASE_URL` raggiungibile (i test fanno skip pulito se il DB è assente). I test normalizzano `DATABASE_URL` per `psycopg` (libpq) e SQLAlchemy. |
| `git diff --check` / `git diff --stat` / `git diff --name-only` / `git status -sb` | **No** | L'ambiente non è un checkout git. Atteso: `git diff --name-only` mostra esattamente i 3 file di §2; `git diff --check` nessun errore di whitespace. |

**Controlli statici aggiuntivi eseguiti offline.** Il sorgente del runner
è stato ispezionato: importa solo `json`, `uuid`, `dataclasses`,
`typing`, `sqlalchemy` (`text`, `Connection`) e
`app.services.orchestration_provider`; nessun modulo di rete, nessun SDK
provider, nessun `subprocess`/`fastapi`/`redis`. Gli `event_type`
emessi sono tutti nel codominio chiuso di
`orchestration_events_event_type_chk` (`0011`); i nomi di colonna usati
negli INSERT corrispondono a quelli reali di `0011`.

---

## 13. Limitations

- **Comandi DB e `pytest` non eseguiti in questa lavorazione.** Per
  assenza di Postgres e di `sqlalchemy`/`psycopg` nell'ambiente, la suite
  non è stata eseguita qui. È il rischio residuo più rilevante: la
  validazione finale (`pytest` sul nuovo file, regressione `make
  test-db`) va eseguita dal revisore in un ambiente con DB. I controlli
  statici offline coprono sintassi, import-level, codominio degli eventi e
  nomi di colonna, ma non sostituiscono l'esecuzione reale.
- **Mock-only.** Il `MockProviderAdapter` non produce intelligenza reale;
  l'output è una risposta candidata, non una risposta pubblicabile, e non
  garantisce la verità fattuale. Usage e costo sono mock
  (`is_mock=True`, `cost_estimate=0`).
- **Single-agent, single-pass, `attempt_no=1`.** Nessun retry, nessun
  multi-agent, nessun reviewer/critic/synthesizer. Un retry futuro sarà
  una nuova riga con `attempt_no` incrementato, mai un update.
- **Replay in-flight.** Per un run ripresentato mentre è `pending` o
  `running`, il replay espone lo `status` grezzo (scelta documentata,
  §16): ORCH-RUNNER-A non implementa la concorrenza attiva.
- **Budget di costo non esercitato.** Con il mock `cost_estimate=0`; il
  preflight è solo sui token.
- **`overflow_policy='warn'`.** Trattata di fatto come `hard_stop` in
  ORCH-RUNNER-A (il preflight rifiuta l'invocazione); il comportamento
  `warn` differenziato è rinviato.

---

## 14. Future phases

- **ORCH-MULTI-A** — estensione a più agenti per run, partial failure
  handling, retry con `attempt_no` incrementato.
- **ORCH-REVIEW-A** — reviewer/critic pass.
- **ORCH-SYNTHESIS-A** — synthesis pass e produzione di
  `candidate_syntheses`.
- **ORCH-SOURCES-A** — source resolution e source verification reali
  (catena `source_candidates → source_resolutions →
  source_verifications → evidence_spans`).
- **ORCH-GATE-A** — innesto della `candidate_syntheses` nella catena
  Claim Extraction → Evidence Binding → Final Answer Gate esistente.
- **Provider reali** — `FutureRemoteProviderAdapter` /
  `FutureLocalLLMAdapter` con trasporto, secret management, cost policy,
  timeout/rate-limit/retry reali e transaction model a più fasi (§17.4):
  fase dedicata e successiva.

---

## 15. Conferme finali

- **Nessun provider reale, nessun SDK, nessuna rete, nessuna API, nessuna
  UI, nessun Redis, nessun FastAPI, nessun local LLM.** Il provider è il
  mock deterministico di ORCH-PROVIDER-A.
- **Nessuna CandidateSynthesis, nessuna Source Resolution, nessuna Source
  Verification, nessun Final Answer Gate.** `final_gate_report_id` resta
  NULL; `publication_status='not_evaluated'`.
- **Provider output ≠ risposta pubblicabile.** Un
  `orchestration_agent_outputs` succeeded è un output candidato, non una
  risposta pubblicabile.
- **Source candidate ≠ evidence.** Le candidate restano `proposed` e non
  verificate.
- **Run completed ≠ publication allowed.** Significa solo che il mock run
  è terminato senza errore.
- Il record tecnico persistito serve audit/debugging e **non garantisce
  la verità fattuale**.

**Nessun commit è stato eseguito.** Commit suggerito, solo dopo revisione
umana: `Add single-agent mock orchestration runner`.
