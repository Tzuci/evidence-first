# ORCH-SCHEMA-A IMPLEMENTATION REPORT

> Relazione finale della fase **ORCH-SCHEMA-A**, prodotta per essere allegata a
> una revisione QA. Repo: `Tzuci/evidence-first`, branch `main`.
> Lingua: italiano tecnico. Nessun commit è stato eseguito.

---

## 1. Scope della fase

ORCH-SCHEMA-A implementa lo **schema DDL persistente minimo** per il futuro
nucleo di orchestrazione multi-AI dell'Evidence-First MVP-0, più i **test root
DB** che ne verificano i vincoli reali. La fase realizza il piano prodotto dalla
PARTE 1/2, applicando le correzioni QA indicate nel prompt della PARTE 2/2.

La fase consiste **esclusivamente** in:

- una migration additiva `migrations/0011_orchestration_schema.sql` (solo DDL);
- un file di test root `tests/test_orch_schema_constraints.py` (test di schema
  reali, DB-only);
- una voce additiva in `docs/migration_plan.md`;
- questa relazione.

ORCH-SCHEMA-A **non** implementa servizi, worker, route API, superfici UI,
provider AI reali, modelli locali, retrieval web, né alcun gate parallelo.
Tutte le 19 tabelle introdotte sono **vuote** dopo la migration: nessun runner
le popola. La fase resta "minima" perché aggiunge solo schema, non codice
applicativo.

---

## 2. File creati/modificati

Esattamente quattro file, tutti additivi salvo la singola voce aggiunta a
`migration_plan.md`:

- `migrations/0011_orchestration_schema.sql` — **nuovo**. Migration DDL con le
  19 tabelle, gli indici, le UNIQUE, i CHECK, le FK e i 14 trigger append-only.
- `tests/test_orch_schema_constraints.py` — **nuovo**. Test root DB-only, 11
  test di vincolo + 16 helper.
- `docs/migration_plan.md` — **modificato**. Aggiunta di una sola sezione
  (`## 0011_orchestration_schema.sql (Fase ORCH-SCHEMA-A)`) inserita fra la
  sezione `0007` e la `Regola d'oro`. Nessuna riga preesistente è stata
  alterata.
- `ORCH_SCHEMA_A_IMPLEMENTATION_REPORT.md` — **nuovo**. Questa relazione.

Nessun altro file del repository è stato creato o modificato. In particolare
non sono stati toccati: `README.md`, `PROJECT_STATE.md`,
`PHASE_ORCH_SCHEMA_PRE.md`, `PHASE_PRODUCT_ORCHESTRATION_PRE.md`, `apps/api/*`,
`apps/worker/*`, `apps/web/*`, `packages/shared/*`, le migration `0001`-`0010`,
i file di package e le dipendenze.

---

## 3. Tabelle create

La migration `0011` crea **19 tabelle**, tutte nuove e vuote dopo
l'applicazione. Divise per area:

**Configurazione (mutabile, niente trigger append-only)**

- `master_prompts` — input primario del prodotto: domanda/obiettivo dell'utente.
- `agent_role_prompts` — ruolo e prompt assegnabili a un agente; catalogo
  versionato (`version_no`).
- `agent_configs` — configurazione di un agente AI (provider, modello, ruolo,
  contract, flag reviewer/synthesizer).
- `token_budgets` — budget di token/costo pre-run.

**Snapshot (append-only)**

- `master_prompt_versions` — snapshot immutabile del testo di un master prompt.
- `agent_config_snapshots` — snapshot immutabile della configurazione di un
  agente al momento dell'avvio del run.

**Run / events**

- `orchestration_runs` — radice di una esecuzione di orchestrazione multi-AI.
- `orchestration_events` — log append-only delle transizioni del run.

**Agent facts (append-only)**

- `orchestration_agent_runs` — esecuzione concreta di un singolo agente.
- `orchestration_agent_messages` — messaggi a livello provider di un agent run.
- `orchestration_agent_outputs` — output strutturato di un agent run.

**Source candidate flow (append-only)**

- `source_candidates` — fonte proposta/citata; non è evidence.
- `source_resolutions` — recupero/risoluzione della fonte reale.
- `source_verifications` — verifica della fonte risolta; ponte verso
  `evidence_spans`.

**Provider / token (append-only)**

- `provider_invocations` — invocazione del provider come fatto auditabile.
- `token_usage_records` — consumo reale di token e costo.

**Synthesis / link (append-only)**

- `candidate_syntheses` — sintesi multi-AI candidata; non è un
  `published_answers`.
- `synthesis_source_links` — join verso `orchestration_agent_outputs` e/o
  `evidence_spans`.
- `synthesis_claim_links` — join verso `logical_claims` (ponte verso il Claim
  Ledger esistente).

---

## 4. Decisioni schema principali

- **Nomi prefissati `orchestration_agent_*`.** La famiglia agente multi-AI usa
  i nomi `orchestration_agent_runs`, `orchestration_agent_messages`,
  `orchestration_agent_outputs`. Questo evita ogni collisione semantica con le
  tabelle placeholder `agent_runs` / `agent_outputs` introdotte da `0005`
  (ratifica di `PHASE_ORCH_SCHEMA_PRE.md` §17.2).
- **Non riuso di `agent_runs` / `agent_outputs` di `0005`.** Le quattro
  placeholder di `0005` (`agent_runs`, `agent_outputs`, `truncation_events`,
  `continuation_attempts`) non sono riusate, non sono ridefinite, non sono
  rimosse. `agent_runs` di `0005` conserva intatto il suo CHECK
  `run_kind ∈ {compile_draft, final_answer_gate}` e la sua ancora a `task_id`;
  `0011` non la trasforma in tabella di orchestrazione multi-AI.
- **`source_candidates` senza `evidence_span_id`.** La tabella non ha la colonna
  `evidence_span_id` e non porta alcuna FK verso `evidence_spans`,
  `claim_evidence_links` o `logical_claims`. Una source candidate è una fonte
  *proposta*, non un'evidenza: trattarla come evidence sarebbe scorretto per un
  prodotto evidence-first.
- **`source_verifications` come ponte verso `evidence_spans`.** Il passaggio da
  source candidate a evidenza reale avviene esclusivamente attraverso
  `source_verifications.evidence_span_id` (FK nullable verso `evidence_spans`).
  La catena obbligata è `source_candidates → source_resolutions →
  source_verifications → evidence_spans`.
- **`candidate_syntheses` distinta da `published_answers`.** La sintesi
  candidata è un'entità separata; nessuna FK o colonna consente di trasformarla
  in `published_answers` saltando Claim Extraction, Evidence Binding e Final
  Answer Gate. La colonna `status` (`draft`, `ready_for_claim_extraction`,
  `submitted_to_gate`, `superseded`) descrive il suo avanzamento, non una
  decisione di pubblicabilità.
- **`token_budgets` pre-run, senza `orchestration_run_id`.** `token_budgets` è
  configurazione pre-run: può referenziare `tenant_id`, `master_prompt_id` e
  `agent_config_id`, ma **non** referenzia `orchestration_runs`. Il consumo
  effettivo di un run è rappresentato dal fatto append-only
  `token_usage_records`.
- **`orchestration_runs.status` materializzato con transizioni tracciate da
  eventi.** `orchestration_runs` porta un campo `status` materializzato (più
  `started_at`/`completed_at`/`failure_reason`) e per questo **non** riceve il
  trigger append-only: è l'unica eccezione ammessa al modello append-only di
  questa migration. Il commento SQL della migration documenta l'invariante
  operativo: ogni transizione di stato deve avere un evento corrispondente in
  `orchestration_events`. **Nessun trigger custom** è stato creato per questa
  eccezione.
- **`synthesis_claim_links` verso `logical_claims`.** La FK del ponte verso il
  Claim Ledger punta a `logical_claims`. È la scelta verso cui convergono sia
  il piano della PARTE 1/2 sia le correzioni QA della PARTE 2/2. La granularità
  resta una decisione formalmente aperta (`PHASE_ORCH_SCHEMA_PRE.md` §17.4):
  vedi §9.

---

## 5. Vincoli e trigger

**CHECK principali (codomini su TEXT, nessun ENUM PostgreSQL).**

- `master_prompts.status ∈ {draft, ready, archived}`.
- `agent_role_prompts.role_category ∈ {researcher, critic, synthesizer,
  generic}`; `version_no >= 1`.
- `agent_configs.order_index >= 0`.
- `token_budgets.budget_level ∈ {per_orchestration, per_agent, per_pass}`;
  `overflow_policy ∈ {hard_stop, warn}`; `token_limit >= 0`; CHECK condizionale
  `tb_level_target` (`per_agent ⇒ agent_config_id NOT NULL`).
- `orchestration_runs.mode ∈ {multi_ai_orchestration, local_evidence, hybrid}`;
  `execution_mode ∈ {independent, coordinated}`; `status ∈ {pending, running,
  waiting_source_resolution, synthesizing, submitted_to_gate, completed,
  failed, cancelled}`.
- `orchestration_events.event_type` su codominio di 14 valori (`run_created` …
  `gate_completed`, `token_budget_exceeded`, `run_cancelled`, `run_failed`);
  `sequence_no >= 0`.
- `orchestration_agent_runs.status ∈ {pending, running, succeeded, failed,
  cancelled}` (correzione QA §1); `attempt_no >= 1`.
- `orchestration_agent_messages.message_role ∈ {system, user, assistant,
  review, tool}`; `sequence_no >= 0`.
- `orchestration_agent_outputs.sequence_no >= 0`.
- `source_candidates.candidate_type ∈ {agent_cited, user_supplied,
  system_retrieved, internal, future_web}`; `status` su codominio di 8 valori;
  `declared_confidence` in `[0,1]` se non NULL.
- `source_resolutions.outcome ∈ {resolved, failed, insufficient_metadata,
  partial, unreachable, not_found}` (correzione QA §3: i tre valori richiesti
  sono presenti; gli altri tre sono esiti più specifici motivati di un
  tentativo di risoluzione).
- `source_verifications.outcome ∈ {verified_as_retrieved, rejected,
  inconclusive}` (correzione QA §4: colonna principale `outcome`, non
  `verification_outcome`).
- `provider_invocations.status ∈ {pending, succeeded, failed, cancelled}`
  (correzione QA §2: timeout e rate-limit non sono status principali, sono
  rappresentati via `error_code`/`error_message`); `attempt_no >= 1`.
- `token_usage_records.pass_kind` su codominio nullable di 6 valori;
  `tokens_input >= 0`; `tokens_output >= 0`; `attempt_no >= 1`.
- `candidate_syntheses.status ∈ {draft, ready_for_claim_extraction,
  submitted_to_gate, superseded}` (correzione QA §5); `version_no >= 1`.
- `synthesis_source_links.slk_target_present` — almeno uno fra `agent_output_id`
  e `evidence_span_id` NOT NULL.

**UNIQUE principali.**

- Versioning/struttura: `agent_role_prompts(tenant_id, name, version_no)`,
  `master_prompt_versions(master_prompt_id, version_no)`,
  `agent_config_snapshots(orchestration_run_id, agent_config_id)`,
  `orchestration_events(orchestration_run_id, sequence_no)`,
  `orchestration_agent_runs(orchestration_run_id, agent_config_snapshot_id,
  attempt_no)`, `orchestration_agent_messages(agent_run_id, sequence_no)`,
  `orchestration_agent_outputs(agent_run_id, sequence_no)`,
  `candidate_syntheses(orchestration_run_id, version_no)`,
  `synthesis_claim_links(candidate_synthesis_id, logical_claim_id)`, e i due
  indici UNIQUE parziali di `synthesis_source_links`.
- Idempotenza: `orchestration_runs(tenant_id, idempotency_key)`,
  `orchestration_events(orchestration_run_id, event_type, idempotency_key)`,
  `source_resolutions(source_candidate_id, idempotency_key)`,
  `source_verifications(source_resolution_id, idempotency_key)`,
  `provider_invocations(agent_run_id, attempt_no, idempotency_key)`,
  `candidate_syntheses(orchestration_run_id, idempotency_key)` (UNIQUE
  `candidate_syntheses_run_idem_uq`, distinta dalla UNIQUE di versioning
  `candidate_syntheses_run_version_uq`), e — per `token_usage_records` — **due
  indici UNIQUE parziali** anziché una sola UNIQUE: poiché
  `provider_invocation_id` è nullable e in PostgreSQL una UNIQUE su colonna
  NULL ammette duplicati, l'idempotenza è enforced da
  `token_usage_records_provider_idem_uq` su `(orchestration_run_id,
  provider_invocation_id, idempotency_key)` `WHERE provider_invocation_id IS
  NOT NULL` e da `token_usage_records_no_provider_idem_uq` su
  `(orchestration_run_id, idempotency_key)` `WHERE provider_invocation_id IS
  NULL`.

**FK principali.** Tutte `ON DELETE RESTRICT`. FK verso tabelle esistenti
immutate: `tenants`, `projects`, `users`, `evidence_spans`,
`document_versions`, `document_chunks`, `logical_claims`, `final_gate_reports`.
FK interne alla famiglia `0011` ordinate dai `CREATE TABLE`: nessuna dipendenza
ciclica reale (`orchestration_runs` è creata prima delle tabelle che la
referenziano e dopo `master_prompt_versions` che essa referenzia).

**Trigger append-only.** 14 tabelle di fatto ricevono il trigger condiviso
`reject_modify_append_only()` di `0001`: `master_prompt_versions`,
`agent_config_snapshots`, `orchestration_events`, `orchestration_agent_runs`,
`orchestration_agent_messages`, `orchestration_agent_outputs`,
`source_candidates`, `source_resolutions`, `source_verifications`,
`provider_invocations`, `token_usage_records`, `candidate_syntheses`,
`synthesis_source_links`, `synthesis_claim_links`. Le 4 tabelle di
configurazione (`master_prompts`, `agent_role_prompts`, `agent_configs`,
`token_budgets`) e `orchestration_runs` non lo ricevono. Le 3 tabelle di
configurazione con `updated_at` (`master_prompts`, `agent_configs`,
`token_budgets`) ricevono il trigger condiviso `set_updated_at()` di `0001`.
Nessuna funzione nuova è stata creata.

**Idempotency constraints.** Ogni tabella di fatto scrivibile in risposta a un
evento redeliverato porta una chiave di idempotenza enforced da UNIQUE
(elencate sopra), sul pattern di `coverage_gap_statements_idem_uq` (0005),
`pale_idempotency_uq` (0006), `cec_entry_span_idem_uq` (0009).

**Protezione da segreti in `provider_invocations`.** La tabella non ha alcuna
colonna per API key, secret, token di autenticazione o credenziali.
L'auditabilità è data da `request_hash`, `response_hash`, `status`,
`error_code`, `error_message`, `tokens_*`, `latency_ms`, `attempt_no`,
`is_mock`, `redaction_strategy`. Un test verifica meccanicamente l'assenza di
colonne dal nome sospetto.

---

## 6. Test creati

File `tests/test_orch_schema_constraints.py`, root DB-only, stile coerente con
`tests/test_answers_gate_constraints.py` e `tests/test_claim_ledger_constraints.py`:
header con docstring di coverage, helper `_ensure_migrations(db_conn)` (ricarica
`scripts/migrate.py`, `cmd_apply(target=None)`), `_unique_hash()`,
`_seed_dev(cur)`, helper locali di insert per le tabelle `0011`, fixture
`db_conn` da `tests/conftest.py`, psycopg diretto, rerun-safe via UUID/hash per
invocazione, nessun import da `apps/api` o `apps/worker`, nessun servizio
applicativo avviato.

11 test di vincolo:

1. `test_orch_schema_tables_exist` — verifica via `information_schema.tables` la
   presenza delle 19 tabelle `0011`.
2. `test_master_prompt_versions_are_unique_and_append_only` — inserisce
   tenant/project/master_prompt/versione; verifica la UNIQUE
   `(master_prompt_id, version_no)` e che UPDATE/DELETE siano rifiutati dal
   trigger append-only.
3. `test_orchestration_run_idempotency_and_mode_check` — verifica la UNIQUE
   `(tenant_id, idempotency_key)` e i CHECK su `mode`, `execution_mode`,
   `status`.
4. `test_orchestration_events_are_append_only_and_sequence_unique` — verifica
   la UNIQUE `(orchestration_run_id, sequence_no)` e che UPDATE/DELETE siano
   rifiutati.
5. `test_orchestration_agent_tables_do_not_reuse_0005_agent_runs` — verifica
   che `orchestration_agent_runs` esista, che `agent_runs` di `0005` esista
   ancora distinta, che il suo CHECK `run_kind ∈ {compile_draft,
   final_answer_gate}` sia invariato e che non abbia acquisito una colonna
   `orchestration_run_id`.
6. `test_source_candidate_is_not_evidence` — verifica che `source_candidates`
   non abbia colonna `evidence_span_id` né FK verso
   `evidence_spans`/`claim_evidence_links`/`logical_claims`; verifica i CHECK
   su `candidate_type` e `status`; conferma che il ponte verso evidence è
   `source_verifications` (che ha la colonna `evidence_span_id`).
7. `test_source_verification_can_link_to_evidence_span` — crea una catena reale
   documento → versione → chunk → evidence_span (riusando le tabelle
   `0002`/`0003`), poi candidate → resolution → verification con
   `evidence_span_id` valido: insert riuscito; verifica inoltre che la FK verso
   `evidence_spans` rifiuti uno span id inesistente.
8. `test_candidate_synthesis_versioning_status_and_links` — verifica la UNIQUE
   `(orchestration_run_id, version_no)`, il CHECK su `status`, la UNIQUE di
   idempotenza `(orchestration_run_id, idempotency_key)` (due sintesi sullo
   stesso run con la stessa chiave di idempotenza, anche con `version_no`
   diverso, vengono rifiutate), che `synthesis_claim_links` referenzi
   `logical_claims` e non `published_answers`/`final_gate_reports`, e che il
   link sia append-only.
9. `test_provider_invocation_records_are_append_only_and_mock_explicit` —
   verifica il CHECK su `status` (`timeout` rifiutato), la presenza di
   `is_mock`, che UPDATE/DELETE siano rifiutati e l'assenza di colonne dal nome
   `%api_key%`/`%secret%`/`%credential%`/`%token_auth%`/`%password%`.
10. `test_token_usage_records_are_append_only` — esercita la catena di FK verso
    run/agent run/provider invocation, verifica l'idempotenza nei due casi
    coperti dai due indici UNIQUE parziali (con `provider_invocation_id` NOT
    NULL: secondo record con stessa terna rifiutato; con
    `provider_invocation_id` NULL: primo record accettato, secondo record con
    stessa coppia `(orchestration_run_id, idempotency_key)` rifiutato), e
    verifica che UPDATE/DELETE siano rifiutati.
11. `test_token_budgets_are_pre_run_config` — verifica che `token_budgets` non
    abbia colonna `orchestration_run_id` né FK verso `orchestration_runs`,
    verifica i CHECK su `budget_level` e `overflow_policy`, e verifica il CHECK
    condizionale per-agent (`per_agent` senza `agent_config_id` rifiutato,
    con `agent_config_id` accettato).

---

## 7. Comandi eseguiti

> **Nota di ambiente — importante per la revisione QA.** L'ambiente in cui la
> PARTE 2/2 è stata svolta **non dispone di un server PostgreSQL** né del runner
> applicativo collegato a un database: non è stato possibile installare
> `postgresql` (i pacchetti non risultano raggiungibili dalla rete
> dell'ambiente). Di conseguenza tutti i comandi che richiedono un DB vivo
> **non sono stati eseguiti**. Questo è dichiarato apertamente, come previsto
> dal prompt ("Se un comando non viene eseguito, devi dichiararlo chiaramente e
> spiegare perché"). I file sono stati invece validati con tutti i controlli
> statici disponibili offline. I comandi che richiedono il DB vanno eseguiti
> dal revisore in un ambiente con Postgres attivo (`make up` +
> `DATABASE_URL` raggiungibile).

| Comando | Eseguito | Esito / atteso |
|---|---|---|
| `git diff --check` | No | Workspace di lavorazione non è il checkout git. Atteso in revisione: nessun errore di whitespace. |
| `git diff --stat` | No | Stesso motivo. Atteso: solo i 4 file della §2. |
| `git diff --name-only` | No | Stesso motivo. Atteso: esattamente i 4 file della §2. |
| `git status -sb` | No | Stesso motivo. Atteso: solo i 4 file della §2; nessun file di `apps/*` o `packages/*`. |
| `python3 scripts/migrate.py --status` | No | Nessun DB disponibile. Atteso: `0001`-`0010` `applied`, `0011_orchestration_schema.sql` `pending`. |
| `python3 scripts/migrate.py` | No | Nessun DB. Atteso: applicazione di `0011`, esito OK. |
| `python3 scripts/migrate.py` (2ª esecuzione, idempotenza) | No | Nessun DB. Atteso: "Nessuna migration pendente."; checksum di `0011` stabile. |
| `PYTHONPATH=$(pwd)/packages/shared python3 -m pytest -q tests/test_orch_schema_constraints.py` | No | Nessun DB; la fixture `db_conn` richiede una connessione reale. Atteso: 11 test verdi. |
| `PYTHONPATH=$(pwd)/packages/shared python3 -m pytest -q tests/test_answers_gate_constraints.py tests/test_claim_ledger_constraints.py` | No | Nessun DB. Atteso: regressione verde — `0011` è additiva e non tocca lo schema preesistente. |
| `make test-db` | No | Nessun DB. Atteso: suite DB root verde, test preesistenti invariati. |
| `grep` wording vietato sui 4 file | **Sì** | Eseguito offline. Esito: nessuna occorrenza al di fuori della lista esplicita di banned wording (vedi sotto). |

**Controlli statici effettivamente eseguiti offline (in sostituzione, non in
aggiunta, ai comandi DB non disponibili):**

- **`migrations/0011_orchestration_schema.sql`** — nome file conforme alla
  regex del runner (`^\d{4}_[a-z0-9_]+\.sql$`); 19 `CREATE TABLE`; 14 trigger
  `reject_modify_append_only`; 3 trigger `set_updated_at`; parentesi bilanciate;
  zero `CREATE FUNCTION`; zero `CREATE TYPE`/ENUM; zero `ALTER TABLE` su
  tabelle `0001`-`0010`; tutte le FK `ON DELETE RESTRICT` (nessun
  `ON DELETE CASCADE` nel DDL); ordine dei `CREATE TABLE` privo di
  forward-reference (ogni FK punta a una tabella già creata o a una tabella
  `0001`-`0010`); cross-check semantico di tutte le correzioni QA §1-§6 e di
  tutti i CHECK/UNIQUE richiesti dal prompt: tutti verificati.
- **`tests/test_orch_schema_constraints.py`** — `python3 -m py_compile` OK;
  AST: 11 funzioni `test_*` con i nomi esatti richiesti dal prompt + 16 helper;
  nessun import da `apps`/`app`; tutti gli 11 test usano la fixture `db_conn`
  e chiamano `_ensure_migrations`.
- **`docs/migration_plan.md`** — verifica che la modifica sia puramente
  additiva: la nuova sezione `0011` è inserita fra `0007` e `Regola d'oro`; il
  resto del file (sezioni `0001`-`0007`, `Regola d'oro`) è invariato.

**Comando di controllo wording (eseguito):**

```bash
grep -niE "[t]ruth score|[v]erified true|[v]erified answer|[A]I verified|[f]actually true|[h]allucination eliminated|[h]allucination-free|[g]uaranteed truth|[z]ero hallucinations|[e]ntailed = true|[s]ource quality proves claim|[C]VE-lite proves support|[r]eal NLI|[c]ontradiction detector|[c]itation-to-claim validator" migrations/0011_orchestration_schema.sql tests/test_orch_schema_constraints.py docs/migration_plan.md ORCH_SCHEMA_A_IMPLEMENTATION_REPORT.md || true
```

Esito: l'unica riga restituita è la riga del comando stesso, all'interno di
questa relazione (sezione di wording vietato esplicita). Nessuna delle frasi
vietate compare come spiegazione ordinaria in alcuno dei quattro file.

---

## 8. Garanzie di non-scope

ORCH-SCHEMA-A dichiara esplicitamente:

- **Nessun provider reale.** Nessun OpenAI, Anthropic, Gemini o altro provider
  esterno è introdotto o referenziato in modo operativo. `provider` resta una
  stringa opaca; in MVP-0 l'unico valore operativo è `mock`.
- **Nessun local LLM.** Nessun modello AI locale è introdotto o integrato.
- **Nessuna API.** Nessuna route HTTP è aggiunta o implementata. `apps/api/*` è
  invariato.
- **Nessun worker di orchestrazione.** Nessun consumer o componente worker è
  creato o modificato. `apps/worker/*` è invariato. Tutte le 19 tabelle restano
  vuote dopo la migration.
- **Nessuna UI.** Nessuna pagina o componente frontend è creato o modificato.
  `apps/web/*` è invariato.
- **Nessun source retrieval reale.** Nessun recupero o risoluzione reale di
  fonti è implementato; `source_resolutions`/`source_verifications` sono solo
  schema.
- **Nessun web retrieval.** Nessuna capacità di recupero web è introdotta.
- **Nessun gate parallelo.** Nessuna seconda autorità di decisione di
  pubblicabilità è introdotta accanto al Final Answer Gate esistente. Eventuali
  riferimenti a `final_gate_reports` sono FK nullable di solo collegamento.
  `synthesis_claim_links` non porta FK verso `published_answers` né
  `final_gate_reports`.
- **Nessuna modifica a migration `0001`-`0010`.** Le migration applicate sono
  immutabili e non sono state toccate. `0011` non esegue alcuna `ALTER`
  distruttiva. Le placeholder `agent_runs`/`agent_outputs`/`truncation_events`/
  `continuation_attempts` di `0005` non sono riusate, ridefinite o rimosse.
- **Nessuna modifica a `README.md` / `PROJECT_STATE.md`.** Entrambi sono
  invariati, così come `PHASE_ORCH_SCHEMA_PRE.md` e
  `PHASE_PRODUCT_ORCHESTRATION_PRE.md`.
- **Nessuna dipendenza aggiunta.** Nessun manifest o lockfile è stato toccato.

---

## 9. Rischi o decisioni aperte

- **Comandi DB non eseguiti in questa lavorazione.** Per assenza di un server
  PostgreSQL nell'ambiente, la migration non è stata applicata e i test non
  sono stati eseguiti qui. È il rischio residuo più rilevante: la validazione
  finale (`migrate.py`, `pytest`, `make test-db`) va eseguita dal revisore in
  un ambiente con DB. I controlli statici offline coprono sintassi, struttura,
  conteggi, ordine delle FK e cross-check semantico dei vincoli, ma non
  sostituiscono l'esecuzione reale.
- **Granularità di `synthesis_claim_links`.** La FK punta a `logical_claims`,
  come da piano e da correzioni QA. `PHASE_ORCH_SCHEMA_PRE.md` §17.4 lascia la
  scelta formalmente aperta fra `logical_claims` e `claim_ledger_entries`.
  Tensione documentata: `logical_claims` è scoped a un `task_id` (`0004`),
  mentre la linea di orchestrazione non ha una riga `task_masters` — un
  `orchestration_run` è l'analogo multi-AI di un task. La FK è puramente
  additiva, è un semplice join e **non** bypassa il gate; un eventuale cambio
  di granularità sarebbe una migration additiva successiva.
- **`status` materializzato su `orchestration_runs`.** Scelta esplicitamente
  ammessa da `PHASE_ORCH_SCHEMA_PRE.md` §8 ma in tensione con l'append-only
  puro: la riga resta mutabile sul solo `status` (più `started_at`,
  `completed_at`, `failure_reason`). Documentato nel commento della migration
  che ogni transizione richiede un `orchestration_events` corrispondente.
  L'alternativa (stato interamente derivato dagli eventi) resta valida e
  riconsiderabile in revisione umana.
- **Numero di migration `0011` e retention.** `PROJECT_STATE.md` preannunciava
  `0011_*` come candidato per la retention distruttiva. ORCH-SCHEMA-A occupa
  `0011` per lo schema di orchestrazione, come prescritto esplicitamente dal
  prompt. La retention reale slitta a un numero successivo (`0012_*` o oltre).
  Da segnalare in revisione umana.
- **Idempotenza di `candidate_syntheses` e `token_usage_records` — risolta
  (microfix ORCH-SCHEMA-A).** Una revisione successiva ha chiuso due lacune di
  idempotenza del piano iniziale: `candidate_syntheses` ora porta una UNIQUE
  dedicata `(orchestration_run_id, idempotency_key)` accanto alla UNIQUE di
  versioning; `token_usage_records` usa due indici UNIQUE parziali (split sul
  `provider_invocation_id` nullable) al posto di una singola UNIQUE che, su
  colonna NULL, in PostgreSQL avrebbe ammesso duplicati. Entrambe le correzioni
  sono coperte da test. Non resta un rischio aperto su questo asse.
- **Volume.** Le tabelle di fatto (eventi, messaggi, usage records,
  invocations) crescono senza pruning. La retention distruttiva è un debito
  noto, fuori scope per ORCH-SCHEMA-A.
- **`coverage_gap_statements` senza trigger append-only.** Debito preesistente
  (`0005`/`0007`/`0009`/`0010`), non in scope; `0011` non lo tocca.
- **Provider reali futuri.** Lo schema è disegnato perché il passaggio a
  provider reali sia un cambiamento di dato, non di struttura; il rischio è che
  una fase futura introduca per errore colonne che presuppongano un provider
  reale o, peggio, segreti. `provider_invocations` è stata progettata senza
  alcuna colonna di credenziali, e un test lo verifica meccanicamente.

---

## 10. Stato finale

La fase ORCH-SCHEMA-A è **pronta per revisione umana**.

I quattro file deliverable sono stati prodotti e validati con tutti i controlli
statici disponibili offline. **Non è pronta per commit** finché un revisore non
avrà eseguito, in un ambiente con PostgreSQL attivo, la sequenza di verifica
della §7 (`migrate.py --status`, `migrate.py` ×2 per l'idempotenza, `pytest`
sul nuovo file di test, regressione sui test root preesistenti, `make test-db`)
e ne avrà confermato l'esito verde.

**Nessun commit è stato eseguito.** Commit suggerito, solo dopo revisione umana:
`Add orchestration schema foundation`.
