# PHASE ORCH-RUNNER-PRE

> **Documento di design del primo orchestration runner single-agent mock.**
> Questo blocco è **solo progettazione**. Non implementa codice di produzione,
> non crea né modifica migration, non modifica `apps/api/*`, `apps/worker/*`,
> `apps/web/*`, `packages/shared/*`, non tocca i test, non aggiunge
> dipendenze, non aggiunge SDK provider, non aggiunge segreti, non modifica
> `.env`, non modifica `README.md` né `PROJECT_STATE.md` né alcun altro
> `PHASE_*_PRE.md` né alcun `*_IMPLEMENTATION_REPORT.md`. L'unico deliverable
> è questo file `PHASE_ORCH_RUNNER_PRE.md`.
>
> Lingua: italiano tecnico, registro da System Architect.
>
> **Promemoria di linguaggio (vincolante per tutta la fase).** Il sistema è
> evidence-first ed evidence-gated. Non promette verità assoluta, non
> promette l'eliminazione totale delle allucinazioni, non dichiara che le
> sue risposte siano "vere". Il runner serve a trasformare una configurazione
> di agente e un MockProviderAdapter in un run persistito e auditabile: non
> decide publishability, non verifica fonti, non produce risposte
> pubblicabili, non sostituisce il Final Answer Gate, non integra
> CandidateSynthesis al gate in questa fase.
>
> **Nota di coerenza architetturale (vincolante).** Quando un'entità di
> questo documento registra un *fatto* (creazione di un run, invocazione di
> un provider, output di un agente, consumo di token, transizione di stato),
> quel fatto è **append-only**: una sua "modifica" è una nuova riga, mai una
> riscrittura silenziosa. La sola eccezione ammessa dallo schema
> `ORCH-SCHEMA-A` (migration `0011_orchestration_schema.sql`) è il campo
> `orchestration_runs.status` materializzato; ogni sua transizione deve
> comunque generare un evento corrispondente in `orchestration_events`. Il
> runner descritto qui rispetta integralmente questa disciplina.

---

## Indice

1. Scopo della fase
2. Stato attuale del sistema
3. Obiettivo prodotto del runner
4. Runner mode MVP-0
5. Input contract futuro
6. Output contract futuro
7. Stato e transizioni del run
8. Mapping tabella orchestration_runs
9. Mapping agent_config_snapshots
10. Mapping orchestration_agent_runs
11. Mapping orchestration_agent_messages
12. Mapping provider_invocations
13. Mapping token_usage_records
14. Mapping orchestration_agent_outputs
15. Source candidates nel runner
16. Idempotenza
17. Transaction model
18. Failure handling
19. Budget model
20. Event sequence
21. Relationship with Final Answer Gate
22. Relationship with existing closed-corpus pipeline
23. Worker implications future
24. Test strategy future
25. Security and redaction
26. Non-goals
27. Acceptance criteria
28. Comandi di verifica

---

## 1. Scopo della fase

La fase **ORCH-RUNNER-PRE** progetta, **solo a livello di design**, il primo
orchestration runner del sistema Evidence-First MVP-0: il servizio
worker-level che, in una fase futura di codice (`ORCH-RUNNER-A`), saprà
ricevere o costruire una richiesta di run mock e persistere il run end-to-end
sulle tabelle introdotte da `ORCH-SCHEMA-A`, usando il `MockProviderAdapter`
di `ORCH-PROVIDER-A`.

Questa fase è **esclusivamente di design**. Non:

- scrive codice di backend, worker, frontend o pacchetti condivisi;
- crea o modifica migration (`0001`-`0011` sono applicate e immutabili);
- modifica i test esistenti o ne aggiunge di nuovi;
- introduce provider AI reali, SDK provider, secret, credenziali o
  configurazioni di rete;
- introduce un local LLM;
- introduce route HTTP o pagine UI;
- aggiunge dipendenze;
- crea source retrieval reale, source verification reale o web retrieval;
- crea orchestrazione multi-agent;
- crea candidate synthesis reale;
- integra il Final Answer Gate;
- modifica `README.md`, `PROJECT_STATE.md`, `PHASE_*_PRE.md` o
  `*_IMPLEMENTATION_REPORT.md`.

Lo scopo è **fissare un disegno condiviso del primo runner** che la fase di
codice `ORCH-RUNNER-A` potrà implementare senza dover reinventare contratti.
Il documento descrive: contratto di input e di output, modalità di
esecuzione, transizioni di stato del run, mapping concettuale verso le
tabelle dello schema `ORCH-SCHEMA-A`, sequenza di eventi (limitata ai soli
`event_type` realmente presenti nella migration `0011`), idempotenza,
transaction model, failure handling, budget model, relazione con la pipeline
closed-corpus esistente e con il Final Answer Gate, strategia di test.

La fase si colloca nella roadmap incrementale di
`PHASE_PRODUCT_ORCHESTRATION_PRE.md §19`: è il blocco **§19.6 (ORCH-RUNNER-A)**
in versione `*-PRE`, segue `ORCH-SCHEMA-PRE` / `ORCH-SCHEMA-A` (schema
applicato come `0011`) e `ORCH-PROVIDER-PRE` / `ORCH-PROVIDER-A` (provider
abstraction mock-first già implementata in
`apps/worker/app/services/orchestration_provider.py`), e precede direttamente
`ORCH-RUNNER-A` (la fase che scriverà il servizio runner reale).

`ORCH-RUNNER-A` dovrà essere **mock-first**: nessun provider reale, nessun
SDK, nessuna rete, nessun local LLM, nessuna API, nessuna UI, nessun
multi-agent, nessun source retrieval reale, nessun source verification
reale, nessuna integrazione con il Final Answer Gate. Ogni decisione qui
presa è una raccomandazione di design per quella fase, non un impegno di
implementazione.

---

## 2. Stato attuale del sistema

Questa sezione distingue ciò che **esiste oggi**, dopo `ORCH-PROVIDER-A`, da
ciò che **non esiste ancora** e da ciò che è **fuori scope** per questa
fase. La fase di codice successiva dovrà riverificare ogni elemento contro
il proprio HEAD prima di basarsi su di esso.

### 2.1 Esistente

- **schema ORCH-SCHEMA-A** — la migration `0011_orchestration_schema.sql` è
  applicata, additiva, DDL-only; introduce 19 tabelle nuove e non tocca
  `0001`-`0010`. Tutte le tabelle sono vuote: nessun runner le popola.
- **provider abstraction ORCH-PROVIDER-A** — il modulo worker-level puro
  `apps/worker/app/services/orchestration_provider.py` espone l'interfaccia
  logica `ProviderAdapter`, i contratti dati `ProviderRequest` /
  `ProviderResult` / `ProviderError` / `ProviderUsage` /
  `ProviderSourceCandidate`, un `ProviderRegistry` minimale, costanti di
  codominio coerenti con `0011`, hashing deterministico request/response,
  redaction sicura dei payload, normalizzazione degli errori, stima mock di
  usage/costo, preflight budget check mock (`enforce_mock_budget`).
- **`MockProviderAdapter`** — adapter deterministico, senza rete, senza SDK,
  che produce `ProviderResult` con `is_mock=True`, contenuto derivato dal
  `request_hash`, usage stimata mock, `cost_estimate=Decimal("0")`,
  `latency_ms` mock fisso. Supporta error injection via
  `request.constraints["mock_error_code"]` e source candidate injection via
  `request.source_policy["mock_source_candidates"]`. Le source candidate
  prodotte sono unverified (`status='proposed'`, `is_verified=False`).
- **mapping pure functions** — `to_provider_invocation_record`,
  `to_token_usage_record`, `source_candidates_to_records`: funzioni pure
  worker-level che producono dict in memoria con le chiavi delle colonne
  reali di `provider_invocations`, `token_usage_records` e
  `source_candidates` (incluso `tenant_id` NOT NULL). Non scrivono nel DB.
- **pipeline evidence-gated legacy closed-corpus** — `task.created` consumer
  con 15 eventi audit worker-side, Claim Ledger, CVE-lite, Source Quality,
  Claim Entailment, Final Answer Gate, `published_answers`, Anti-Hallucination
  Report API (8.8B-REPORT). Tutto invariato e parallelo.

### 2.2 Non ancora esistente

- **orchestration runner** — nessun servizio worker-level guida un
  `orchestration_runs`.
- **writer DB per `orchestration_runs`** — nessun componente inserisce o
  aggiorna righe in `orchestration_runs`.
- **writer DB per `orchestration_events`** — nessun componente appende
  righe in `orchestration_events`.
- **writer DB per `agent_config_snapshots`** — nessun componente fissa
  snapshot immutabili di config agente all'avvio di un run.
- **writer DB per `orchestration_agent_runs` / `orchestration_agent_messages`
  / `orchestration_agent_outputs`** — nessun componente persiste l'esecuzione
  concreta di un singolo agente né i suoi messaggi/output.
- **writer DB per `provider_invocations` / `token_usage_records`** — i record
  esistono come dict in memoria prodotti dalle pure functions di
  `ORCH-PROVIDER-A`, ma nessun componente li persiste.
- **source candidate persistence da runner** — nessun componente trasforma le
  `ProviderSourceCandidate` di un `ProviderResult` in righe di
  `source_candidates`.
- **candidate synthesis** — nessun componente produce `candidate_syntheses`.
- **multi-agent** — nessun coordinamento di più agenti dentro un run.
- **API/UI orchestration** — nessuna route HTTP, nessuna pagina UI per
  l'orchestrazione.
- **gate integration da `CandidateSynthesis`** — nessun ponte fra il futuro
  runner e il Final Answer Gate esistente.

### 2.3 Fuori scope per questa fase

- **codice** — `ORCH-RUNNER-PRE` non scrive codice;
- **provider reale** — nessun OpenAI, Anthropic, Gemini introdotto;
- **local LLM** — nessun modello AI locale introdotto;
- **source retrieval reale** — nessun recupero/risoluzione reale di fonti;
- **source verification reale** — nessuna verifica reale di fonti;
- **API/UI** — nessuna superficie HTTP o frontend;
- **Final Answer Gate integration** — nessun innesto della linea multi-AI
  nella catena Claim Extraction → Evidence Binding → Final Answer Gate.

---

## 3. Obiettivo prodotto del runner

Il runner serve a trasformare:

```
MasterPromptVersion        (snapshot immutabile del prompt consumato dal run)
+ AgentConfigSnapshot      (snapshot immutabile della config dell'agente)
+ TokenBudget              (limite pre-run; opzionale)
+ ProviderRequest          (richiesta strutturata redatta e hashabile)
+ MockProviderAdapter      (l'unico provider operativo in MVP-0)
```

in un **run persistito e auditabile**, riconducibile alle tabelle di
`ORCH-SCHEMA-A`:

```
orchestration_runs
  ▼
orchestration_events           (log append-only delle transizioni)
  ▼
orchestration_agent_runs       (esecuzione concreta di un singolo agente)
  ▼
orchestration_agent_messages   (messaggi a livello provider)
  ▼
provider_invocations           (invocazione del provider come fatto auditabile)
  ▼
orchestration_agent_outputs    (output strutturato consumabile)
  ▼
token_usage_records            (consumo mock di token)
  ▼
source_candidates              (eventuali fonti proposte dal mock, non verificate)
```

**Cosa il runner NON fa.** Vincoli centrali per la coerenza del prodotto
evidence-first, da rispettare in `ORCH-RUNNER-A`:

- **il runner non decide publishability.** Il Final Answer Gate resta
  l'unico gate di pubblicazione; `ORCH-RUNNER-A` non lo integra.
- **il runner non verifica fonti.** Le eventuali source candidate prodotte
  dal mock restano unverified (`status='proposed'`, `is_verified=False`);
  non possono contribuire al gate.
- **il runner non produce published answer.** Un `orchestration_agent_outputs`
  succeeded è un output candidato; non è una risposta pubblicabile.
- **il runner non sostituisce gate.** Non emette decisioni di
  publication-allowed / publication-held.
- **il runner crea fatti auditabili.** La sua promessa è la persistenza
  append-only e idempotente di un mock run end-to-end, con audit completo.

Chiarimenti vincolanti:

- `orchestration_agent_outputs.status='succeeded'` (o equivalente esito
  riuscito sull'`agent_run`) **non implica** che il contenuto sostenga un
  claim; non implica supporto semantico, non implica verità nel mondo.
- `provider_invocations.status='succeeded'` **non implica** che il contenuto
  della risposta sia corretto né supportato.
- `orchestration_runs.status='completed'` **non implica** che il run abbia
  prodotto una risposta pubblicabile; significa solo che il mock run è
  terminato senza errore di runner/provider.
- `request_hash` / `response_hash` servono audit, debug e idempotenza; non
  provano il contenuto.
- Il token usage prodotto dal mock non è un costo reale; è esplicitamente
  marcato `is_mock=true`.

---

## 4. Runner mode MVP-0

Il primo runner (`ORCH-RUNNER-A`) deve essere progettato in modalità
**MVP-0**, deliberatamente minima e collaudabile:

- **single-agent** — un solo `orchestration_agent_runs` per
  `orchestration_runs`; nessun coordinamento di più agenti.
- **single-pass** — un solo pass di esecuzione (independent answer); nessun
  reviewer pass, nessun critic pass, nessun synthesis pass.
- **mock-only** — l'unico `provider_name` ammesso è `mock`; l'unico `model`
  è `mock-model`. Nessuna chiamata di rete, nessun SDK, nessun local LLM.
- **synchronous service function** — il runner è una funzione di servizio
  worker-level invocabile in modo sincrono; nessuna coda dedicata per
  `ORCH-RUNNER-A`, salvo scelta motivata.
- **DB transaction boundary esplicito** — la transazione che persiste il
  run è esplicita e bounded (vedi §17); nessun commit implicito disperso.
- **no queue integration per `ORCH-RUNNER-A`** — il runner non si registra
  come consumer su Redis salvo decisione motivata di `ORCH-RUNNER-A`; il
  collaudo iniziale può avvenire come chiamata diretta in test e in un
  futuro thin wrapper.
- **no API** — nessuna route HTTP nuova.
- **no UI** — nessuna pagina o componente frontend nuovo.
- **no multi-agent** — nessuna estensione a più agenti, riservata a
  `ORCH-MULTI-A`.
- **no provider reale** — nessuna integrazione con provider esterni o local
  LLM.

**Perché questa scelta.** La modalità MVP-0:

- **riduce rischio** — concentra il lavoro sul minimo necessario per
  esercitare l'intero ciclo di vita del run (creazione → invocazione mock →
  persistenza fatti → terminazione) senza moltiplicare i punti di
  fallimento;
- **usa `ORCH-SCHEMA-A`** — le 19 tabelle sono pronte e vuote; il runner le
  popola per la prima volta, validando indirettamente le UNIQUE, le FK, gli
  invarianti append-only e i CHECK del codominio;
- **usa `ORCH-PROVIDER-A`** — `MockProviderAdapter`, le pure functions di
  mapping (`to_provider_invocation_record`, `to_token_usage_record`,
  `source_candidates_to_records`) e `enforce_mock_budget` sono pronti e
  testati; il runner li compone, non li reinventa;
- **crea base testabile** — un runner deterministico mock-driven è
  esercitabile in modo riproducibile in unit/service test e in DB test,
  senza rete, senza Redis, senza FastAPI.

---

## 5. Input contract futuro

Il runner riceve (o costruisce internamente) una richiesta logica di run.
Questo è un **contratto concettuale**, non un'API definitiva: nomi e tipi
sono indicativi e soggetti a revisione in `ORCH-RUNNER-A`.

Campi probabili dell'input:

- `tenant_id` — il tenant del run; obbligatorio.
- `project_id` — il progetto, se mantenuto come contenitore organizzativo
  (decisione aperta in `PHASE_PRODUCT_ORCHESTRATION_PRE.md §20`).
- `master_prompt_id` — l'id del `master_prompts` consumato.
- `master_prompt_version_id` — l'id della `master_prompt_versions` snapshot
  immutabile del prompt; questo è ciò che `orchestration_runs` referenzia
  via FK.
- `agent_config_id` — l'id del `agent_configs` da snapshottare in
  `agent_config_snapshots` al run start.
- `token_budget_id` — riferimento opzionale a `token_budgets` per il
  preflight budget check; può essere `None` (nessun limite).
- `idempotency_key` — chiave opaca di idempotenza del run (vedi §16);
  obbligatoria.
- `mode` — modalità di orchestrazione dal codominio
  `orchestration_runs.mode`: `multi_ai_orchestration` / `local_evidence` /
  `hybrid`. In `ORCH-RUNNER-A` (single-agent, mock-only) è tipicamente
  `multi_ai_orchestration`, ma il runner non lo presuppone.
- `execution_mode` — dal codominio `orchestration_runs.execution_mode`:
  `independent` / `coordinated`. In `ORCH-RUNNER-A` è sempre `independent`.
- `prompt_text` — il testo del prompt da inviare al provider. Deriva dallo
  snapshot di `master_prompt_versions.prompt_text` o da una vista
  equivalente; il runner non legge live `master_prompts.prompt_text`.
- `system_instructions` — istruzioni di sistema dell'agente; derivano dal
  `snapshot_payload` di `agent_config_snapshots`.
- `task_instructions` — istruzioni di compito dell'agente; idem.
- `output_contract` — forma attesa dell'output (`dict` o JSON); deriva dallo
  snapshot.
- `source_policy` — quali fonti l'agente può vedere; deriva dallo snapshot.
  Può contenere `mock_source_candidates` per esercitare l'estrazione
  candidate del mock.
- `constraints` — vincoli dell'agente; deriva dallo snapshot. Può contenere
  `mock_error_code` per esercitare l'error injection del mock.
- `max_tokens` — limite di token per l'invocazione; deriva dallo snapshot.
- `mock_source_candidates` — alias esplicito del campo
  `source_policy["mock_source_candidates"]`, per ergonomia di test. Lista di
  dict `{title, url, locator, raw_text}` opzionali.
- `mock_error_code` — alias esplicito del campo
  `constraints["mock_error_code"]`, per ergonomia di test. Codice di errore
  normalizzato dal codominio di `ORCH-PROVIDER-A`.
- `created_by` — l'attore che avvia il run (utente o servizio); opzionale.

Chiarimenti vincolanti:

- **nessun secret nell'input.** Il contratto non porta API key, token di
  autenticazione, credenziali, Authorization header. Coerente con
  `provider_invocations` (0011) e con `ORCH-PROVIDER-A`.
- **nessuna API key provider.** Il provider è mock; non serve alcuna chiave.
- **`provider_name` deve essere `mock` in `ORCH-RUNNER-A`.** Qualsiasi altro
  valore va rifiutato dal runner (`invalid_request` o equivalente).
- **`model` deve essere `mock-model` in `ORCH-RUNNER-A`.** Stesso vincolo.
- **input idempotente.** Lo stesso `tenant_id` + `idempotency_key`,
  ripresentato, deve produrre lo stesso esito logico, senza duplicare righe
  di fatto (vedi §16).

---

## 6. Output contract futuro

Il runner restituisce (in memoria, in un eventuale wrapper sincrono) un
output logico che descrive lo stato finale del run e gli id dei fatti
persistiti. Campi probabili:

- `status` — esito sintetico del runner: `succeeded` / `failed`. Coerente
  con lo stato finale di `orchestration_runs.status` (`completed` o
  `failed`).
- `orchestration_run_id` — id della riga `orchestration_runs` creata o
  riconciliata.
- `agent_run_id` — id della riga `orchestration_agent_runs` creata; può
  essere `None` se il run è fallito prima della creazione dell'agent run.
- `provider_invocation_id` — id della riga `provider_invocations` creata;
  `None` se il provider non è mai stato invocato (per esempio budget
  exceeded prima dell'invocazione).
- `token_usage_record_ids` — lista degli id `token_usage_records` creati;
  `[]` se nessun consumo è stato registrato.
- `agent_message_ids` — lista degli id `orchestration_agent_messages`
  creati, in ordine di `sequence_no`.
- `agent_output_id` — id della riga `orchestration_agent_outputs` creata;
  `None` se il provider è fallito o se non è stato prodotto un output.
- `source_candidate_ids` — lista degli id `source_candidates` creati; `[]`
  se il `ProviderResult` non portava candidate o se la persistenza è stata
  intenzionalmente rinviata.
- `event_ids` — lista degli id `orchestration_events` creati durante il run.
- `error_code` — codice di errore normalizzato del runner, dal codominio di
  `ORCH-PROVIDER-A` (`timeout`, `budget_exceeded`, `invalid_request`,
  `unknown_error`, ecc.); `None` se `status='succeeded'`.
- `error_message` — messaggio redatto e bounded, coerente con la redaction
  di `ORCH-PROVIDER-A`; `None` se `status='succeeded'`.
- `is_mock` — sempre `True` in `ORCH-RUNNER-A`. Coerente con
  `orchestration_runs.is_mock` e con `provider_invocations.is_mock`.
- `publication_status` — sempre `not_evaluated` in `ORCH-RUNNER-A`: il gate
  non è integrato.
- `gate_report_id` — sempre `None` in `ORCH-RUNNER-A`: nessun
  `final_gate_reports` è prodotto da questa linea.

Chiarimenti vincolanti:

- **`publication_status='not_evaluated'` perché il gate non è integrato.**
  Il runner non valuta publishability. `final_gate_report_id` su
  `orchestration_runs` resta `NULL`.
- **`status='succeeded'` significa solo che il runner ha completato il mock
  run.** Non implica che il contenuto sia supportato, supportato
  semanticamente, o pubblicabile.
- **`status='failed'` significa fallimento del runner o del provider mock**
  (error injection, budget exceeded, DB integrity error, ecc.). Non
  significa che il claim sia falso nel mondo.

---

## 7. Stato e transizioni del run

Le transizioni di stato di `orchestration_runs` per `ORCH-RUNNER-A`,
limitate dal CHECK `orchestration_runs_status_chk` della migration `0011`:

**Transizioni nominali (happy path):**

```
pending  →  running  →  completed
```

**Transizioni di fallimento:**

```
pending  →  failed
running  →  failed
```

**Transizioni di cancellazione (opzionali, future):**

```
pending  →  cancelled
running  →  cancelled
```

`ORCH-RUNNER-A` può **non** implementare la cancellazione attiva; il valore
`cancelled` resta riservato a una fase futura. Gli stati `pending`,
`running`, `failed`, `completed` sono il minimo operativo.

Gli stati `waiting_source_resolution`, `synthesizing`, `submitted_to_gate`
del codominio sono **fuori scope per `ORCH-RUNNER-A`**: corrispondono a
capacità (recupero fonti, synthesis pass, gate submission) che il runner
single-agent mock non esercita.

### 7.1 Mappa eventi richiesti dal prompt

Il prompt `ORCH-RUNNER-PRE` elenca, nella propria §20, una sequenza
nominale ideale di eventi. Va però rispettato il codominio reale di
`orchestration_events.event_type` definito dalla migration `0011`. Il
codominio reale è chiuso a 14 valori:

```
run_created
agent_run_started
agent_run_completed
agent_run_failed
source_candidate_created
source_resolution_started
source_resolution_completed
source_verification_completed
synthesis_created
submitted_to_gate
gate_completed
token_budget_exceeded
run_cancelled
run_failed
```

Confronto fra la sequenza nominale del prompt e il codominio reale:

| Evento nominale (prompt §20)        | event_type reale in `0011`            | Uso in ORCH-RUNNER-A |
|---|---|---|
| `run_created`                       | `run_created`                          | scrivere |
| `run_started`                       | **non esiste**                         | non scrivere; transizione `pending → running` è solo `orchestration_runs.status` materializzato |
| `agent_run_started`                 | `agent_run_started`                    | scrivere |
| `provider_invocation_started`       | **non esiste**                         | non scrivere; il fatto è la riga `provider_invocations` |
| `provider_invocation_completed`     | **non esiste**                         | non scrivere; il fatto è la riga `provider_invocations` con `status` finale |
| `agent_output_recorded`             | **non esiste**                         | non scrivere; il fatto è la riga `orchestration_agent_outputs` |
| `token_usage_recorded`              | **non esiste**                         | non scrivere; il fatto è la riga `token_usage_records` |
| `source_candidates_recorded`        | `source_candidate_created`             | scrivere **una volta per candidate** se `ORCH-RUNNER-A` decide di persisterle, con **un `idempotency_key` distinto per ogni evento** (vedi §15, §16, §20) |
| `run_completed`                     | **non esiste**                         | non scrivere; lo stato terminale `completed` è solo `orchestration_runs.status` materializzato |
| `run_failed`                        | `run_failed`                           | scrivere su fallimento del run |

Eventi ulteriori del codominio `0011` non usati da `ORCH-RUNNER-A`:
`source_resolution_started`, `source_resolution_completed`,
`source_verification_completed` (nessun source retrieval/verification reale
in questa fase); `synthesis_created`, `submitted_to_gate`, `gate_completed`
(nessuna synthesis né gate integration); `run_cancelled` (cancellazione
opzionale rinviata); `token_budget_exceeded` (scritto solo se il preflight
budget check fallisce, vedi §19); `agent_run_completed` /
`agent_run_failed` (scritti alla terminazione dell'agent run, vedi §10
e §18).

**Invariante operativo.** Ogni transizione di `orchestration_runs.status`
deve avere un evento di `orchestration_events` corrispondente quando il
codominio lo prevede; quando il codominio NON prevede un event_type per quella
transizione (per esempio `running` o `completed`), la transizione resta
tracciata dal campo `status` materializzato e dagli event_type adiacenti che
la circondano (`run_created`, `agent_run_started`, `agent_run_completed`,
`run_failed`). Il runner **non inventa event_type** che lo schema non
consente.

La sequenza concreta consigliata per `ORCH-RUNNER-A` è dettagliata in §20.

---

## 8. Mapping tabella orchestration_runs

Mapping concettuale dei campi di `orchestration_runs` che il runner popola
all'avvio di un run (in `ORCH-RUNNER-A`):

- `tenant_id` — da `input.tenant_id`.
- `project_id` — da `input.project_id` (nullable).
- `master_prompt_version_id` — da `input.master_prompt_version_id`; punta
  alla `master_prompt_versions` snapshot immutabile del prompt consumato.
- `mode` — da `input.mode`; in `ORCH-RUNNER-A` tipicamente
  `multi_ai_orchestration`.
- `execution_mode` — da `input.execution_mode`; in `ORCH-RUNNER-A` sempre
  `independent`.
- `status` — inizializzato a `pending` alla creazione, aggiornato a
  `running` quando il runner inizia, e a `completed` o `failed` a
  terminazione. Materializzato.
- `master_prompt_text_hash` — denormalizzato per ergonomia di verifica;
  derivato dallo stesso testo del prompt consumato.
- `bounding_parameters` — JSONB; in `ORCH-RUNNER-A` può contenere
  `{"max_agents": 1, "pass_kinds": ["independent_answer"]}` o simile,
  fissato come snapshot all'avvio.
- `idempotency_key` — da `input.idempotency_key`.
- `policy_name` / `policy_version` — identità della policy di
  orchestrazione adottata; `ORCH-RUNNER-A` può adottare un'identità mock
  esplicita, per esempio `("mvp0_mock_orchestration_runner", "0.1.0")`.
- `is_mock` — `True` in `ORCH-RUNNER-A` (provider mock, runner mock-first).
- `started_at` — popolato quando il runner passa a `running`.
- `completed_at` — popolato a terminazione (`completed` o `failed`).
- `failure_reason` — popolato su `failed`, redatto e bounded, coerente con
  la redaction di `ORCH-PROVIDER-A`.
- `final_gate_report_id` — **NULL** in `ORCH-RUNNER-A`: nessuna gate
  integration in questa fase.
- `created_at` — gestito dal default `NOW()`.

Chiarimenti vincolanti:

- **`final_gate_report_id` resta NULL in `ORCH-RUNNER-A`.** Il runner non
  integra il gate.
- **`status` materializzato è aggiornabile secondo schema** (lo schema non
  applica `reject_modify_append_only` su `orchestration_runs`), ma ogni
  transizione coerente con il codominio degli `event_type` reali deve
  generare un `orchestration_events` corrispondente. Per le transizioni che
  non hanno un `event_type` dedicato (per esempio `pending → running` e
  `running → completed`), la transizione è tracciata da event_type
  adiacenti (`run_created`, `agent_run_started`, `agent_run_completed`,
  `run_failed`) come dettagliato in §20.
- **run `completed` non implica pubblicazione.** È un mock run terminato
  senza errore di runner/provider; non c'è alcun `final_gate_reports`
  associato.

---

## 9. Mapping agent_config_snapshots

All'avvio del run il runner fissa uno snapshot immutabile della
configurazione di ogni agente partecipante. In `ORCH-RUNNER-A`, single-agent,
viene creata **una sola riga** `agent_config_snapshots` per run.

Mapping concettuale (campi reali letti dalla migration `0011`):

- `orchestration_run_id` — id della riga `orchestration_runs` appena creata.
- `agent_config_id` — id dell'`agent_configs` snapshottato.
- `snapshot_payload` — JSONB con copia integrale e immobilizzata della
  configurazione dell'agente al momento dell'avvio: `provider="mock"`,
  `model="mock-model"`, ruolo + testo dei prompt (system e task)
  effettivamente usati, `output_contract`, `constraints`,
  `temperature_config`, `retry_policy`, `source_access`, `reviewer_flag`,
  `synthesizer_flag`, `order_index`, eventuale budget per-agent rilevante.
  **Nessun secret.**
- `agent_role_prompt_text_hash` — hash del prompt di ruolo consumato, come
  campo di verifica denormalizzato.
- `created_at` — gestito dal default `NOW()`.

Chiarimenti vincolanti:

- **snapshot immutabile.** `agent_config_snapshots` riceve il trigger
  `reject_modify_append_only()` (vedi `0011`): una volta scritta, la riga
  non si modifica e non si cancella.
- **provider mock.** Il payload dichiara esplicitamente `provider="mock"`.
- **model `mock-model`.** Il payload dichiara esplicitamente
  `model="mock-model"`.
- **role/prompt/output_contract copiati.** Il run è auditabile rispetto al
  testo *esatto* dei prompt che ha consumato; una modifica successiva al
  catalogo non altera retroattivamente il run.
- **nessun secret.** Il payload non contiene API key, Authorization,
  credential, password.
- **append-only.** Coerente con il principio 1 di `ORCH-SCHEMA-PRE`.

`ORCH-RUNNER-A` non inventa colonne: rispetta esattamente lo schema di
`agent_config_snapshots` come definito da `0011`.

---

## 10. Mapping orchestration_agent_runs

In `ORCH-RUNNER-A`, single-agent, viene creata **una sola riga**
`orchestration_agent_runs` per run. La riga è scritta **una volta sola,
con lo status finale del tentativo**, dopo che l'invocazione mock è
terminata. Questo vincolo è strutturale, non stilistico: la tabella riceve
il trigger `reject_modify_append_only()` (`0011`) e qualunque UPDATE
successivo sarebbe rifiutato dal DB.

**Disciplina insert-once / final-status (vincolante per `ORCH-RUNNER-A`).**

- **non si inserisce `orchestration_agent_runs` con `status='running'`.**
  Lo `status` materializzato `pending`/`running` esisterebbe come fatto
  storico append-only che non potrebbe essere superato.
- **non si aggiorna `orchestration_agent_runs`.** Il trigger append-only
  rifiuta UPDATE/DELETE.
- **`agent_run_started` event rappresenta lo start logico.** L'evento
  `orchestration_events.event_type='agent_run_started'` traccia l'inizio
  logico dell'agent run; può essere appeso PRIMA della scrittura della
  riga `orchestration_agent_runs`, referenziando l'`agent_run_id`
  **preallocato in memoria** (vedi §17). `related_entity_id` su
  `orchestration_events` non è una FK, quindi può portare un UUID
  preallocato la cui riga `orchestration_agent_runs` corrispondente sarà
  inserita più tardi nella stessa transazione.
- **la riga `orchestration_agent_runs` rappresenta il fatto storico
  finale del tentativo.** Viene scritta con `status` finale (`succeeded`
  / `failed` / eventualmente `cancelled`), `started_at` / `completed_at`
  entrambi valorizzati, ed eventuali `error_code` / `failure_reason`
  redatti e bounded, in un'unica `INSERT`.
- **questo è necessario perché `orchestration_agent_runs` è append-only.**

Mapping concettuale dei campi (campi reali da `0011`):

- `orchestration_run_id` — id della riga `orchestration_runs`.
- `agent_config_snapshot_id` — id della riga `agent_config_snapshots`
  appena creata (FK verso lo snapshot, non verso `agent_configs` live).
- `id` — preallocato in memoria all'inizio del run (per esempio via
  `uuid.uuid4()`) così che gli eventi `agent_run_started` /
  `agent_run_failed` / `agent_run_completed` possano referenziarlo via
  `related_entity_id` prima dell'`INSERT` della riga di fatto.
- `status` — dal codominio `orchestration_agent_runs.status` di `0011`:
  `pending` / `running` / `succeeded` / `failed` / `cancelled`. In
  `ORCH-RUNNER-A` la riga è scritta direttamente con uno status finale
  fra `succeeded` o `failed` (e in futuro `cancelled` se il runner
  supporterà l'annullamento attivo). Gli stati `pending` e `running` non
  vengono **mai persistiti**: rappresentano fasi logiche tracciate dagli
  event_type e dalla mancanza temporanea della riga.
- `attempt_no` — `1` in `ORCH-RUNNER-A`: nessun retry esplicito in questa
  fase. La UNIQUE
  `orchestration_agent_runs_attempt_uq (orchestration_run_id,
  agent_config_snapshot_id, attempt_no)` rende ogni tentativo distinto e
  rende il runner replay-safe sotto redelivery.
- `is_mock` — `True`.
- `error_code` / `failure_reason` — popolati quando `status='failed'`,
  redatti e bounded via `_safe_error_message` di `ORCH-PROVIDER-A`.
- `started_at` / `completed_at` — entrambi valorizzati al momento
  dell'`INSERT`. `started_at` riflette il momento logico dello start
  (coerente con il tempo dell'evento `agent_run_started`), `completed_at`
  il momento di terminazione.
- `created_at` — gestito dal default `NOW()`.

Chiarimenti vincolanti:

- **append-only, scritta una volta sola.** `orchestration_agent_runs`
  riceve il trigger `reject_modify_append_only()`. Le transizioni di
  stato sono rappresentate **integralmente** da eventi
  (`agent_run_started`, `agent_run_completed`, `agent_run_failed`); la
  riga porta solo lo status finale del tentativo, scritto in un'unica
  `INSERT`.
- **agent_run succeeded non implica contenuto supportato.** Significa
  solo che l'esecuzione concreta dell'agente è terminata senza errore di
  runner/provider; non dice nulla sul merito dell'output.
- **`attempt_no = 1`.** Ogni eventuale retry futuro creerà un nuovo
  `orchestration_agent_runs` con `attempt_no` incrementato, non un
  update.

---

## 11. Mapping orchestration_agent_messages

Il runner registra i messaggi minimi a livello provider che compongono
l'interazione con il `MockProviderAdapter`. Per `ORCH-RUNNER-A`,
single-agent, single-pass, il numero di messaggi è bounded e tipicamente
piccolo (almeno 3: system, user, assistant).

**Ordine di scrittura (vincolo FK).** `orchestration_agent_messages.agent_run_id`
è NOT NULL e referenzia `orchestration_agent_runs(id)` con `ON DELETE
RESTRICT` (vedi `0011`). I messaggi possono quindi essere persistiti
**solo dopo** che la riga `orchestration_agent_runs` è stata inserita.
Coerentemente con la disciplina insert-once / final-status di §10, le
righe `orchestration_agent_messages` vengono scritte nella stessa
transazione, **dopo** l'`INSERT` di `orchestration_agent_runs`.

Mapping concettuale per ciascun messaggio (campi reali da `0011`):

- `agent_run_id` — id della riga `orchestration_agent_runs`, **già
  inserita**. In `ORCH-RUNNER-A` corrisponde all'`agent_run_id`
  preallocato in memoria all'inizio del run e materializzato come riga di
  fatto al termine dell'invocazione mock.
- `orchestration_run_id` — denormalizzato (nullable nello schema) per
  ergonomia di query.
- `message_role` — dal codominio `orchestration_agent_messages_role_chk` di
  `0011`: `system` / `user` / `assistant` / `review` / `tool`. In
  `ORCH-RUNNER-A`:
  - `system` per le istruzioni di sistema;
  - `user` per il prompt utente derivato da `master_prompt_versions`;
  - `assistant` per il contenuto del `ProviderResult` riuscito;
  - `review` / `tool` non usati in questa fase.
- `content_text` — testo del messaggio.
- `content_hash` — hash del contenuto, per tracciabilità.
- `sequence_no` — ordinamento dei messaggi dentro l'agent run, monotòno
  crescente da 0; UNIQUE
  `orchestration_agent_messages_run_sequence_uq (agent_run_id,
  sequence_no)` enforced da `0011`.
- `tokens` — eventuale conteggio token associato al messaggio (mock).
- `created_at` — gestito dal default `NOW()`.

Chiarimenti vincolanti:

- **inseriti dopo `orchestration_agent_runs`.** La FK NOT NULL verso
  `orchestration_agent_runs(id)` impone l'ordine. Tentare di scrivere un
  messaggio prima della riga `orchestration_agent_runs` fa fallire la
  transazione per FK violation.
- **assistant message è provider output candidato, non final answer.** È
  output candidato di un agente; deve attraversare CandidateSynthesis →
  Claim Extraction → Evidence Binding → Final Answer Gate prima di poter
  diventare pubblicabile (e nessuno di questi passi è in scope per
  `ORCH-RUNNER-A`).
- **append-only.** `orchestration_agent_messages` riceve il trigger
  `reject_modify_append_only()`; i messaggi non vanno mai riscritti né
  cancellati.

---

## 12. Mapping provider_invocations

Il runner usa la funzione pura `to_provider_invocation_record` di
`ORCH-PROVIDER-A` come design reference per costruire il record dict da
persistere in `provider_invocations`. La funzione produce un dict con le
chiavi delle colonne reali; il runner ne ricava un INSERT.

**Ordine di scrittura (vincolo FK).** `provider_invocations.agent_run_id`
è NOT NULL e referenzia `orchestration_agent_runs(id)` con `ON DELETE
RESTRICT` (vedi `0011`). La riga `provider_invocations` può quindi essere
scritta **solo dopo** che la riga `orchestration_agent_runs` è stata
inserita, anche se l'invocazione mock è avvenuta in memoria prima.
Coerentemente con la disciplina insert-once / final-status di §10:
l'invocazione del `MockProviderAdapter` produce in memoria un
`ProviderResult`, da quel risultato si decide lo status finale
dell'`orchestration_agent_runs`, si inserisce la riga
`orchestration_agent_runs` con quello status, e **solo allora** si scrive
`provider_invocations`. `ProviderRequest.orchestration_agent_run_id`
porta l'`agent_run_id` preallocato in memoria (vedi §17), così
`request_hash` è già consistente con la riga di fatto.

Mapping concettuale (campi reali da `0011`, coerenti con il dict prodotto da
`to_provider_invocation_record`):

- `tenant_id` — da `ProviderRequest.tenant_id`. NOT NULL nello schema.
- `orchestration_run_id` — da `ProviderRequest.orchestration_run_id`.
- `agent_run_id` — da `ProviderRequest.orchestration_agent_run_id`; FK
  NOT NULL verso `orchestration_agent_runs(id)` già inserito.
- `provider_name` — da `ProviderRequest.provider_name`; `mock`.
- `model` — da `ProviderRequest.model`; `mock-model`.
- `request_hash` — da `build_request_hash(request)`; deterministico.
- `response_hash` — da `ProviderResult.response_hash`; deterministico.
- `status` — da `ProviderResult.status`; codominio `pending`, `succeeded`,
  `failed`, `cancelled`.
- `error_code` — da `ProviderResult.error.error_code` se presente, altrimenti
  `None`.
- `error_message` — da `ProviderResult.error.error_message` se presente,
  redatto e bounded da `_safe_error_message` di `ORCH-PROVIDER-A`.
- `tokens_input` / `tokens_output` — da `ProviderResult.usage`.
- `cost_estimate` — da `ProviderResult.usage.cost_estimate` (Decimal reso
  come stringa stabile).
- `latency_ms` — da `ProviderResult.latency_ms`; mock fisso.
- `attempt_no` — da parametro di `to_provider_invocation_record`; in
  `ORCH-RUNNER-A` è `1`.
- `is_mock` — `True` in `ORCH-RUNNER-A`.
- `redaction_strategy` — da `ProviderRequest.redaction_policy.strategy`;
  `hash_only` di default.
- `idempotency_key` — da `ProviderRequest.idempotency_key`.

Chiarimenti vincolanti:

- **inserito dopo `orchestration_agent_runs`.** La FK NOT NULL verso
  `orchestration_agent_runs(id)` impone l'ordine. L'invocazione mock può
  avvenire in memoria prima, ma la persistenza di `provider_invocations`
  segue la persistenza della riga `orchestration_agent_runs` con status
  finale.
- **nessun secret.** `provider_invocations`, per costruzione dello schema
  `0011`, non ha colonne per API key, secret, credenziali, token di
  autenticazione. Il dict prodotto da `to_provider_invocation_record` non
  contiene tali campi, e un test di `ORCH-PROVIDER-A` lo verifica
  meccanicamente.
- **`is_mock=True`.** Finché il provider è mock, ogni riga porta
  `is_mock=True`.
- **append-only.** `provider_invocations` riceve il trigger
  `reject_modify_append_only()`. Un retry futuro crea una nuova riga con
  `attempt_no` incrementato; mai un update di una riga fallita.
- **provider invocation succeeded non implica claim support.**
  `status='succeeded'` significa solo che l'invocazione si è completata
  senza errore di trasporto/provider; non dice nulla sul merito del
  contenuto.

---

## 13. Mapping token_usage_records

Il runner usa la funzione pura `to_token_usage_record` di `ORCH-PROVIDER-A`
come design reference. Per `ORCH-RUNNER-A`, single-agent single-pass, viene
appeso almeno **un** record di consumo dopo l'invocazione mock riuscita; un
fallimento provider può comunque produrre un record minimo che conta il
consumo del tentativo.

Mapping concettuale (campi reali da `0011`):

- `tenant_id` — da `ProviderRequest.tenant_id`. NOT NULL nello schema.
- `orchestration_run_id` — da `ProviderRequest.orchestration_run_id`. NOT
  NULL nello schema.
- `agent_run_id` — da `ProviderRequest.orchestration_agent_run_id`;
  nullable nello schema.
- `provider_invocation_id` — nullable; in `ORCH-RUNNER-A` può essere
  valorizzato dopo l'INSERT in `provider_invocations`, o lasciato `None` se
  il runner decide di persistere il `token_usage_records` prima
  dell'invocazione (sconsigliato — vedi §17). Lo schema fornisce due indici
  UNIQUE parziali (`provider_invocation_id IS NOT NULL` e `IS NULL`) per
  enforced idempotency in entrambi i casi.
- `pass_kind` — dal codominio `token_usage_records.pass_kind` di `0011`:
  `independent_answer` / `reviewer` / `critic` / `synthesis` /
  `second_check` / `source_resolution`. In `ORCH-RUNNER-A` è
  `independent_answer`.
- `tokens_input` / `tokens_output` — da `ProviderResult.usage`.
- `cost_estimate` — da `ProviderResult.usage.cost_estimate` (`Decimal("0")`
  nel mock).
- `attempt_no` — `1` in `ORCH-RUNNER-A`.
- `is_mock` — `True`.
- `idempotency_key` — da `ProviderRequest.idempotency_key`.

Chiarimenti vincolanti:

- **token usage mock non è costo reale.** `is_mock=True`; `cost_estimate`
  è zero o simulato. Un consumer/UI/report deve dichiararlo onestamente.
- **append-only.** `token_usage_records` riceve il trigger
  `reject_modify_append_only()`. Un nuovo consumo è una nuova riga.
- **aggregazioni future derivano dai fatti.** Un eventuale
  `total_tokens`/`total_cost` materializzato (su `orchestration_runs` o
  `orchestration_agent_runs`) è una proiezione dei record append-only,
  mai una fonte di verità indipendente.

---

## 14. Mapping orchestration_agent_outputs

Per `ORCH-RUNNER-A`, single-agent single-pass, viene creata **una sola
riga** `orchestration_agent_outputs` dopo un'invocazione mock riuscita. Su
fallimento del provider, nessuna riga di output viene creata.

**Ordine di scrittura (vincolo FK).** `orchestration_agent_outputs.agent_run_id`
è NOT NULL e referenzia `orchestration_agent_runs(id)` con `ON DELETE
RESTRICT` (vedi `0011`). La riga `orchestration_agent_outputs` può quindi
essere scritta **solo dopo** che la riga `orchestration_agent_runs` è
stata inserita. Coerentemente con la disciplina insert-once /
final-status di §10, l'output viene persistito nella stessa transazione,
dopo l'`INSERT` della riga di fatto dell'agent run.

Mapping concettuale (campi reali da `0011`):

- `agent_run_id` — id della riga `orchestration_agent_runs`, **già
  inserita**.
- `output_kind` — coerente con `output_contract` (testo libero, lista di
  affermazioni, formato strutturato). In `ORCH-RUNNER-A` è tipicamente un
  valore mock esplicito, per esempio `"mock_candidate_text"`.
- `sequence_no` — `0` per il primo output del run; UNIQUE
  `orchestration_agent_outputs_run_sequence_uq (agent_run_id,
  sequence_no)` enforced da `0011`.
- `content_text` — da `ProviderResult.content_text`.
- `content_hash` — derivato da `ProviderResult.response_hash` o da un hash
  diretto di `content_text`, secondo scelta di `ORCH-RUNNER-A`.
- `structured_payload` — da `ProviderResult.structured_payload` (contiene
  `mock=True`, `semantic_warning`, `is_publishable_answer=False`,
  `is_evidence=False`).
- `tokens` — eventuale conteggio token dell'output.
- `created_at` — gestito dal default `NOW()`.

Nota di schema: lo schema `0011` **non** ha una colonna
`provider_invocation_id` su `orchestration_agent_outputs`. Il legame fra
output e invocazione passa per il comune `agent_run_id`. Se in una fase
futura quel legame esplicito sarà ritenuto utile, sarà materia di una
migration additiva, non di `ORCH-RUNNER-A`.

Chiarimenti vincolanti:

- **agent_output non è evidence.** È output candidato di un agente.
- **agent_output non è `candidate_syntheses`.** La sintesi multi-AI è
  un'entità distinta, prodotta da un futuro synthesis pass.
- **agent_output non è `published_answers`.** Per diventare pubblicabile
  deve attraversare Claim Extraction → Evidence Binding → Final Answer
  Gate. Nessuno di questi passi è in scope per `ORCH-RUNNER-A`.
- **append-only.** `orchestration_agent_outputs` riceve il trigger
  `reject_modify_append_only()`.

---

## 15. Source candidates nel runner

Se il `ProviderResult` contiene `source_candidates` (perché l'input ha
incluso `mock_source_candidates` o perché un futuro provider reale le ha
prodotte), il runner futuro **può** persisterle in `source_candidates`. In
`ORCH-RUNNER-A` la persistenza è opzionale e va decisa esplicitamente: il
collaudo end-to-end del runner non la richiede.

Mapping concettuale (campi reali da `0011`, coerenti con
`source_candidates_to_records` di `ORCH-PROVIDER-A`):

- `tenant_id` — da `ProviderRequest.tenant_id`. NOT NULL.
- `orchestration_run_id` — da `ProviderRequest.orchestration_run_id`.
- `master_prompt_id` — `None` in `ORCH-RUNNER-A`: il modulo mock-first non
  ha un `master_prompt_id` sui propri input; la colonna è nullable nello
  schema.
- `candidate_type` — `agent_cited`.
- `status` — `proposed`. Mai diverso da `proposed` in `ORCH-RUNNER-A`.
- `agent_output_id` — id della riga `orchestration_agent_outputs` di
  riferimento, quando il runner persiste le candidate dopo aver creato
  l'output. Nullable nello schema.
- `title` / `url` / `citation_text` / `quoted_text` /
  `declared_confidence` — derivati da `ProviderSourceCandidate` come da
  decisione di mapping documentata in `ORCH_PROVIDER_A_IMPLEMENTATION_REPORT.md`
  §8: `raw_text → citation_text`, `locator → provenance["locator"]`,
  `quoted_text = None`, `declared_confidence = None`.
- `provenance` — JSONB con la provenance mock (`mock=True`,
  `semantic_warning="unverified provider source candidate; not evidence"`,
  `provider_name`, `model`, `locator`, `is_verified=False`).
- `created_by` — `request.provider_name` (`mock`).
- `raw_citation_payload` — JSONB con il payload grezzo della candidate
  (`mock=True`, `title`, `url`, `locator`, `raw_text`).

Chiarimenti vincolanti:

- **status proposed.** Ogni candidate persistita ha `status='proposed'`.
- **is_verified false nel payload/provenance.** La provenance porta
  esplicitamente `is_verified=False`.
- **nessun evidence_span_id.** `source_candidates` non ha la colonna
  `evidence_span_id` (vedi `ORCH-SCHEMA-A` e
  `ORCH_SCHEMA_A_IMPLEMENTATION_REPORT.md` §4); il ponte verso
  `evidence_spans` passa esclusivamente per `source_verifications`.
- **nessun claim link.** Il dict prodotto da `source_candidates_to_records`
  non contiene `claim_id`, `logical_claim_id`, `claim_evidence_link_id`,
  `claim_ledger_entry_id`. Una source candidate non partecipa al Claim
  Ledger finché non è risolta e verificata.
- **nessuna source_resolution in `ORCH-RUNNER-A`.** Nessun recupero reale.
- **nessuna source_verification in `ORCH-RUNNER-A`.** Nessuna verifica
  reale.
- **source candidates restano non verificate.** Non possono contribuire al
  gate, per costruzione dello schema (assenza di FK dirette candidate →
  evidence/claim, presenza obbligatoria della catena
  resolution→verification→evidence_span).

### 15.1 Evento `source_candidate_created` e idempotency_key per candidate

Quando il runner decide di persistere le source candidate, **per ogni
candidate persistita** appende un evento `orchestration_events` con
`event_type='source_candidate_created'`. Lo schema `0011` impone una
UNIQUE composita `orchestration_events_run_type_idem_uq
(orchestration_run_id, event_type, idempotency_key)`: tutti gli eventi
dello stesso `event_type` dentro lo stesso run devono avere
`idempotency_key` distinti, altrimenti il secondo INSERT fallisce per
UNIQUE violation.

**Regola vincolante per `ORCH-RUNNER-A`.** Per ogni evento
`source_candidate_created` il runner costruisce un `idempotency_key`
**derivato dalla chiave di idempotenza del run più un suffisso stabile
per la candidate**. Due forme ammesse:

- **per indice posizionale**:
  `<run_idempotency_key>:source_candidate:<candidate_index>`, con
  `candidate_index` intero crescente da `0` per la prima candidate del
  run;
- **per hash della candidate**:
  `<run_idempotency_key>:source_candidate:<candidate_hash>`, con
  `candidate_hash` derivato deterministicamente dal contenuto della
  candidate (per esempio `stable_hash` dei campi `title`, `url`,
  `locator`, `raw_text`).

Entrambe le forme sono **deterministiche** e **replay-safe**: un secondo
delivery dello stesso run con lo stesso input ricalcolerà gli stessi
`idempotency_key` per le stesse candidate e collegherà via UNIQUE alla
riga già esistente, senza duplicazioni. La scelta fra indice e hash è
decisione di `ORCH-RUNNER-A`; la forma per hash è raccomandata quando il
runner garantisce un ordine stabile delle candidate (lo fa
`MockProviderAdapter` per costruzione), perché è insensibile a eventuali
riordinamenti futuri.

Campi obbligatori dell'evento:

- `event_type` — `source_candidate_created`.
- `sequence_no` — crescente, parte del normale ordinamento monotòno degli
  eventi del run.
- `related_entity_type` — `source_candidate`.
- `related_entity_id` — id della riga `source_candidates` appena
  inserita.
- `idempotency_key` — distinto per ogni candidate, costruito come sopra.

Vincolo strutturale: l'evento `source_candidate_created` referenzia via
`related_entity_id` una riga `source_candidates`, che a sua volta può
referenziare (FK nullable) `agent_output_id`. Per coerenza
dell'ordinamento delle scritture (vedi §17), la candidate viene inserita
**dopo** l'`orchestration_agent_outputs` quando `agent_output_id` viene
valorizzato; l'evento `source_candidate_created` segue immediatamente
l'`INSERT` della candidate.

---

## 16. Idempotenza

`ORCH-RUNNER-A` deve essere **idempotente sotto redelivery** dell'avvio del
run. Le chiavi di idempotenza coinvolte:

- **`idempotency_key` del run** — `orchestration_runs.idempotency_key`,
  enforced da UNIQUE `orchestration_runs_idempotency_uq (tenant_id,
  idempotency_key)`. Lo stesso `tenant_id + idempotency_key` ripresentato
  deve ritornare il run esistente o uno stato `already_started` /
  `already_completed`, non creare un secondo `orchestration_runs`.
- **`idempotency_key` di `provider_invocations`** — enforced da UNIQUE
  `provider_invocations_attempt_idem_uq (agent_run_id, attempt_no,
  idempotency_key)`. Un doppio delivery dello stesso tentativo non duplica
  la riga.
- **`idempotency_key` di `token_usage_records`** — enforced dai due indici
  UNIQUE parziali introdotti dal microfix ORCH-SCHEMA-A:
  `token_usage_records_provider_idem_uq (orchestration_run_id,
  provider_invocation_id, idempotency_key) WHERE provider_invocation_id IS
  NOT NULL` e `token_usage_records_no_provider_idem_uq
  (orchestration_run_id, idempotency_key) WHERE provider_invocation_id IS
  NULL`. Un doppio delivery non duplica il consumo in nessuno dei due casi.
- **event idempotency** — `orchestration_events_run_type_idem_uq
  (orchestration_run_id, event_type, idempotency_key)` impedisce che un
  redelivery di un evento duplichi una riga. **Importante**: questa
  UNIQUE è composita su `(orchestration_run_id, event_type,
  idempotency_key)`; più eventi dello **stesso `event_type`** dentro lo
  stesso run devono usare **`idempotency_key` distinti**, altrimenti il
  secondo INSERT viene rifiutato dal DB. Il runner deve quindi:
  - per eventi che si scrivono una sola volta per run (`run_created`,
    `agent_run_started`, `agent_run_completed`, `agent_run_failed`,
    `run_failed`, `token_budget_exceeded`): usare un suffisso stabile a
    partire dalla chiave di run, per esempio
    `<run_idempotency_key>:<event_type>`;
  - per eventi che possono ripetersi nello stesso run con lo stesso
    `event_type` — in `ORCH-RUNNER-A` il caso operativo è
    `source_candidate_created`, uno per candidate (vedi §15.1) — usare
    una chiave **distinta per occorrenza**, per esempio
    `<run_idempotency_key>:source_candidate:<candidate_index>` oppure
    `<run_idempotency_key>:source_candidate:<candidate_hash>`. Le forme
    sono deterministiche: un secondo delivery dello stesso run
    ricostruirà gli stessi `idempotency_key` e l'INSERT colliderà con la
    riga esistente, deduplicando.

Politica di gestione del conflitto:

- **replay safe.** Lo stesso input ripresentato deve produrre lo stesso
  esito logico osservabile; le UNIQUE sopra elencate fanno il lavoro di
  deduplica.
- **duplicate delivery.** Un secondo delivery dell'avvio di un run con la
  stessa `idempotency_key`:
  - se il run è già `completed` → il runner ritorna l'output esistente
    (`already_completed`), non crea righe nuove;
  - se il run è `running` → il runner ritorna `already_started`, non
    avvia un secondo run;
  - se il run è `failed` → il runner ritorna lo stato fallito esistente;
    un retry intenzionale con stessa chiave non lo riconverte in
    `succeeded`.
- **conflict handling.** Eventuali violazioni di UNIQUE intercettate dal
  DB vanno gestite come `IntegrityError` controllati che riconducono al
  comportamento `already_*` sopra; mai una scrittura silenziosa, mai un
  bypass della UNIQUE.

Retry vs idempotenza:

- **retry futuro crea `attempt_no` nuovo.** Un retry intenzionale di un
  `orchestration_agent_runs` fallito o di una `provider_invocations`
  fallita non è un update: è una nuova riga con `attempt_no` incrementato.
  In `ORCH-RUNNER-A` (`attempt_no = 1`) il retry non è esercitato; resta
  riservato a una fase futura (`ORCH-MULTI-A` o specifica).
- **nessun update silenzioso.** Mai un UPDATE su righe append-only; lo
  schema lo impedisce comunque tramite i trigger.

---

## 17. Transaction model

Date le condizioni di `ORCH-RUNNER-A`:

- `MockProviderAdapter` **non usa rete** e **non blocca** (deterministico,
  in-process, latenza mock fissa);
- il run è single-agent, single-pass, bounded;
- le scritture sono tutte sullo stesso DB Postgres;
- `orchestration_agent_runs` è **append-only** e non ammette UPDATE
  successivi (vedi §10);
- `orchestration_agent_messages`, `provider_invocations` e
  `orchestration_agent_outputs` hanno FK NOT NULL verso
  `orchestration_agent_runs(id)`. `token_usage_records.agent_run_id` è
  nullable nello schema, ma `ORCH-RUNNER-A` lo valorizza quando
  registra usage attribuito all'agent run. In tutti e quattro i casi
  le righe vengono scritte **dopo** la riga `orchestration_agent_runs`:
  per le prime tre per vincolo FK, per `token_usage_records` per
  coerenza con `agent_run_id` valorizzato;

la **strategia raccomandata** per il transaction model è la **singola
transaction per MVP-0 mock runner**, con la sequenza di INSERT vincolata
dall'ordine delle FK e dalla disciplina insert-once / final-status
sull'agent run.

### 17.1 Sequenza ordinata (vincolante per `ORCH-RUNNER-A`)

```
BEGIN
  # 1) Creazione del run e setup pre-invocazione.
  agent_run_id = preallocate_uuid()
  INSERT orchestration_runs (status='pending', ...)
  INSERT orchestration_events (event_type='run_created',
                               sequence_no=0,
                               idempotency_key=<run_idem>:run_created)

  # 2) Transizione pending -> running materializzata (nessun event_type
  #    dedicato nel codominio).
  UPDATE orchestration_runs SET status='running', started_at=NOW()

  # 3) Snapshot immutabile della config dell'agente.
  INSERT agent_config_snapshots (...)

  # 4) Evento di start logico dell'agent run. La riga
  #    orchestration_agent_runs NON esiste ancora: l'evento referenzia
  #    l'agent_run_id preallocato in memoria via related_entity_id
  #    (NON una FK).
  INSERT orchestration_events (event_type='agent_run_started',
                               sequence_no=1,
                               related_entity_type='orchestration_agent_run',
                               related_entity_id=agent_run_id,
                               idempotency_key=<run_idem>:agent_run_started)

  # 5) Costruzione della ProviderRequest. Porta agent_run_id preallocato
  #    in orchestration_agent_run_id, così request_hash è già consistente
  #    con la riga di fatto che verrà scritta al punto 7.
  provider_request = build_provider_request(
      orchestration_agent_run_id=agent_run_id, ...)

  # 6) Invocazione mock IN MEMORIA, deterministica, senza rete.
  result = MockProviderAdapter.invoke(provider_request)

  # 7) INSERT UNICO di orchestration_agent_runs con lo status finale del
  #    tentativo. Nessun UPDATE successivo: lo schema (trigger append-only)
  #    lo rifiuterebbe.
  IF result.status == 'succeeded':
    INSERT orchestration_agent_runs (id=agent_run_id,
                                     status='succeeded',
                                     attempt_no=1,
                                     started_at=<run.started_at>,
                                     completed_at=NOW(),
                                     is_mock=TRUE,
                                     ...)
  ELSE:
    INSERT orchestration_agent_runs (id=agent_run_id,
                                     status='failed',
                                     attempt_no=1,
                                     started_at=<run.started_at>,
                                     completed_at=NOW(),
                                     is_mock=TRUE,
                                     error_code=<redacted>,
                                     failure_reason=<redacted>,
                                     ...)

  # 8) Solo DOPO l'INSERT della riga orchestration_agent_runs si possono
  #    inserire le righe che hanno FK verso agent_run_id. L'ordine fra
  #    queste è libero, salvo dipendenze ulteriori (per esempio una
  #    eventuale source_candidates.agent_output_id richiede
  #    orchestration_agent_outputs già inserito).

  # 8a) Messaggi del provider (system, user, e — se succeeded —
  #     assistant). FK NOT NULL verso orchestration_agent_runs(id).
  INSERT orchestration_agent_messages (system, sequence_no=0)
  INSERT orchestration_agent_messages (user,   sequence_no=1)
  IF result.status == 'succeeded':
    INSERT orchestration_agent_messages (assistant, sequence_no=2)

  # 8b) Invocazione provider come fatto auditabile. FK NOT NULL verso
  #     orchestration_agent_runs(id).
  INSERT provider_invocations (status=result.status, request_hash=...,
                               response_hash=..., is_mock=TRUE, ...)

  # 8c) Output e consumo, solo su success.
  IF result.status == 'succeeded':
    INSERT orchestration_agent_outputs (sequence_no=0, ...)
    INSERT token_usage_records (provider_invocation_id=<id from 8b>,
                                pass_kind='independent_answer', ...)
    # Opzionale: persistenza delle source candidates non verificate.
    # agent_output_id (FK nullable) richiede orchestration_agent_outputs
    # già inserito al passo precedente.
    # Nota: source_candidates NON ha una colonna is_verified; il flag
    # vive solo dentro provenance / raw_citation_payload. Lo status
    # 'proposed' e l'assenza di evidence_span_id / claim link sono la
    # forma strutturale del "non verificato" (vedi §15).
    FOR i, cand IN enumerate(result.source_candidates):
      INSERT source_candidates (
          status='proposed',
          candidate_type='agent_cited',
          agent_output_id=<id from above>,
          provenance={
              "mock": true,
              "semantic_warning": "unverified provider source candidate; not evidence",
              "provider_name": "mock",
              "model": "mock-model",
              "locator": <cand.locator>,
              "is_verified": false
          },
          raw_citation_payload={
              "mock": true,
              "title": <cand.title>,
              "url": <cand.url>,
              "locator": <cand.locator>,
              "raw_text": <cand.raw_text>
          },
          ...)
      INSERT orchestration_events (
          event_type='source_candidate_created',
          sequence_no=<crescente>,
          related_entity_type='source_candidate',
          related_entity_id=<source_candidate_id>,
          idempotency_key=<run_idem>:source_candidate:<i_or_hash>)

  # 9) Eventi di terminazione dell'agent run.
  IF result.status == 'succeeded':
    INSERT orchestration_events (event_type='agent_run_completed',
                                 sequence_no=<crescente>,
                                 related_entity_type='orchestration_agent_run',
                                 related_entity_id=agent_run_id,
                                 idempotency_key=<run_idem>:agent_run_completed)
  ELSE:
    INSERT orchestration_events (event_type='agent_run_failed',
                                 sequence_no=<crescente>,
                                 related_entity_type='orchestration_agent_run',
                                 related_entity_id=agent_run_id,
                                 idempotency_key=<run_idem>:agent_run_failed)
    INSERT orchestration_events (event_type='run_failed',
                                 sequence_no=<crescente>,
                                 idempotency_key=<run_idem>:run_failed)

  # 10) Transizione finale di orchestration_runs.status.
  IF result.status == 'succeeded':
    UPDATE orchestration_runs SET status='completed', completed_at=NOW()
  ELSE:
    UPDATE orchestration_runs SET status='failed', completed_at=NOW(),
                                  failure_reason=<redacted>
COMMIT
```

### 17.2 Perché questo ordine

- **FK non si possono invertire.** `orchestration_agent_messages.agent_run_id`,
  `provider_invocations.agent_run_id`, `orchestration_agent_outputs.agent_run_id`
  e `token_usage_records.agent_run_id` (quest'ultima nullable ma sempre
  valorizzata in `ORCH-RUNNER-A`) sono FK verso
  `orchestration_agent_runs(id)`. Scriverle prima farebbe fallire la
  transazione per FK violation. Da qui la sequenza: agent run **prima**,
  righe che la referenziano **dopo**.
- **`orchestration_agent_runs` è append-only.** Lo schema applica
  `reject_modify_append_only()` su questa tabella; non si può scrivere
  una riga `running` e poi aggiornarla a `succeeded` / `failed`. La
  scrittura è una sola, con lo status finale.
- **`agent_run_started` referenzia l'agent_run_id preallocato.**
  `orchestration_events.related_entity_id` **non è una FK** (vedi `0011`):
  è un UUID libero che può puntare a una riga futura. Il runner pre-alloca
  l'`agent_run_id` in memoria all'inizio del run, lo usa nell'evento
  `agent_run_started` e nella `ProviderRequest`, e lo materializza come
  riga di fatto solo al passo 7. `request_hash` calcolato sulla
  `ProviderRequest` resta consistente con la riga finale.
- **invocazione mock in memoria prima della scrittura della riga di
  fatto.** Il `MockProviderAdapter` è in-process e deterministico: il
  `ProviderResult` è disponibile prima di iniziare le scritture che
  dipendono dallo status finale.

### 17.3 Vantaggi della singola transaction in MVP-0

- atomicità totale: o tutto il run è persistito coerentemente, o niente;
- nessuna finestra di stato parzialmente persistito;
- semplicità di analisi e collaudo;
- coerente con il consumer `task.created` esistente che usa SAVEPOINT per
  i singoli step ma resta in una transazione ben definita.

### 17.4 Strategia alternativa scartata per il mock

Separare "setup + provider call + persistence" in due o tre transazioni
con la provider call fuori dalla transazione è **motivatamente
sconsigliato** in `ORCH-RUNNER-A` perché il `MockProviderAdapter` non
blocca; sarà invece **necessaria** quando i provider reali introdurranno
chiamate di rete potenzialmente lente o fallibili (timeout, rate limit),
nel qual caso una scelta possibile è:

1. TX1: `INSERT orchestration_runs`, `INSERT agent_config_snapshots`,
   `INSERT orchestration_events (run_created)`,
   `INSERT orchestration_events (agent_run_started)`. COMMIT.
2. Provider call **fuori** dalla transazione.
3. TX2: `INSERT orchestration_agent_runs (status finale)` come prima
   scrittura, poi le righe che dipendono da essa
   (`orchestration_agent_messages`, `provider_invocations`,
   `orchestration_agent_outputs`, `token_usage_records`,
   `source_candidates` opzionali), poi gli eventi di terminazione e
   `UPDATE orchestration_runs`. COMMIT.

Questa è una **decisione di design per il futuro provider reale**, non
per `ORCH-RUNNER-A`. La motivazione va documentata in quella fase, non
anticipata qui. La disciplina "agent run scritta una sola volta con lo
status finale, prima delle righe che la referenziano" resta valida anche
in questo scenario.

### 17.5 Note operative

- **nessun commit interno se il service riceve `conn`.** Se la
  signature del service runner è `run_orchestration(conn, request)`, il
  service **non** chiama `conn.commit()`: il chiamante (test o consumer)
  possiede la transazione.
- **`engine.begin()` se service owner della transazione.** Se la signature
  è `run_orchestration(engine, request)`, il service apre la transazione
  con `engine.begin()` (autocommit/auto-rollback all'uscita del context
  manager). Coerente con il pattern dei service worker esistenti.
- **savepoint per step opzionali.** Coerentemente con il consumer
  `task.created`, un `SAVEPOINT` può proteggere lo step di persistenza
  delle `source_candidates` (opzionale in `ORCH-RUNNER-A`): un fallimento
  della persistenza candidate non deve rollback-are il resto del run.
- **coerenza con pattern worker existing.** `ORCH-RUNNER-A` deve usare le
  stesse convenzioni di transazione del consumer `task.created` per non
  introdurre disuniformità nel worker.

---

## 18. Failure handling

`ORCH-RUNNER-A` deve gestire in modo esplicito e auditabile i fallimenti.
Coerentemente con la disciplina insert-once / final-status di §10 e §17,
quando viene scritta, la riga `orchestration_agent_runs` porta sempre uno
status finale (`succeeded` o `failed`); non esistono righe `pending` /
`running` persistite. Il fatto che il fallimento sia accaduto dopo lo
start logico dell'agent run (cioè dopo un evento `agent_run_started`) è
registrato dalla **presenza** della riga con `status='failed'`; se invece
il fallimento accade **prima** che il runner abbia attivato lo start
logico, la riga `orchestration_agent_runs` non viene inserita affatto.

Per ogni classe di errore, la tabella seguente fissa l'esito atteso:

| Caso | Stato `orchestration_runs` | Riga `orchestration_agent_runs` | `provider_invocations` | Event(s) | Error redaction | Note |
|---|---|---|---|---|---|---|
| provider error injection (es. `mock_error_code='rate_limited'`) | `failed` | inserita una sola volta con `status='failed'`, `error_code` normalizzato, `started_at`/`completed_at` entrambi valorizzati, `attempt_no=1` | scritto con `status='failed'`, `error_code`, `error_message` redatto (dopo l'INSERT della riga agent run) | `agent_run_started` (prima dell'INSERT della riga di fatto), `agent_run_failed`, `run_failed` | sì, via `_safe_error_message` | retryable per la policy, ma non ritentato in `ORCH-RUNNER-A` |
| `budget_exceeded` (preflight) | `failed` | non inserita (il runner fallisce prima dello start logico dell'agent run) | non scritto (il provider non è mai invocato) | `token_budget_exceeded` (vedi §19), `run_failed` | sì | preflight via `enforce_mock_budget` |
| invalid input (es. `provider_name != 'mock'`, `model != 'mock-model'`, idempotency_key mancante) | `failed` o rifiuto immediato senza creazione del run | non inserita | non scritto | `run_failed` se il run è stato creato, oppure nessun evento se rifiuto pre-creazione | sì | il runner valida l'input prima di toccare il DB quando possibile |
| DB integrity error (UNIQUE violation idempotente sulla creazione `orchestration_runs`) | invariato: il runner ritorna `already_*` (vedi §16) | invariato | invariato | invariato | n/a | comportamento `already_*`, non `failed` |
| DB integrity error (altro, es. FK rotta) | `failed` se intercettato dopo la creazione del run | inserita con `status='failed'` se il punto di fallimento è dopo il passo 7 di §17; non inserita se prima | scritto con `status='failed'` solo se la sequenza di §17 aveva già superato il passo 8b | `run_failed`, eventuale `agent_run_failed` se la riga di agent run è stata inserita | sì | in singola transaction un FK error causa rollback totale; il run risulta "non scritto" salvo che il rollback parziale tramite SAVEPOINT consenta di registrare il fallimento |
| unknown provider (es. `registry.get` fallisce) | `failed` | non inserita (lo start logico dell'agent run non avviene) | non scritto | `run_failed` | sì | `ValueError` controllato di `ProviderRegistry.get` |
| malformed result (`parse_response` riceve un raw non-`ProviderResult`) | `failed` | inserita con `status='failed'`, `error_code='malformed_response'` | scritto con `status='failed'`, `error_code='malformed_response'` (dopo l'INSERT della riga agent run) | `agent_run_started`, `agent_run_failed`, `run_failed` | sì | non retryable per la policy |
| token budget failure post-result | `failed` | inserita con `status='failed'` | scritto se l'invocazione era avvenuta | `token_budget_exceeded`, `run_failed` | sì | rilevante con provider reali; in `ORCH-RUNNER-A` con mock il consumo è zero e questo caso non si attiva spontaneamente |

Principi trasversali:

- **insert-once / final-status invariato.** Anche su fallimento, la riga
  `orchestration_agent_runs` — quando viene scritta — è scritta una sola
  volta con `status='failed'`, `error_code` / `failure_reason` redatti e
  `completed_at` valorizzato. Mai una scrittura intermedia in `running`
  seguita da update.
- **status run failed o agent failed**, mai update silenziosi che
  cancellano la traccia del tentativo. Le tabelle append-only impediscono
  comunque update in place; l'unica eccezione operativa è
  `orchestration_runs.status`, scritto una volta al termine.
- **`provider_invocations failed`** se l'invocazione è arrivata fino alla
  scrittura del record di invocazione — quindi se la riga
  `orchestration_agent_runs` è stata inserita (vincolo FK). Non scritto
  se il provider non è mai stato invocato (per esempio budget_exceeded
  pre-invocazione, unknown provider, invalid input).
- **`run_failed` event sempre.** Ogni fallimento del runner appende un
  evento `run_failed` con payload diagnostico redatto.
- **`agent_run_failed` event quando applicabile.** Solo se lo start
  logico dell'agent run è stato attivato (cioè se è stato emesso un
  `agent_run_started`) e di conseguenza la riga `orchestration_agent_runs`
  con status finale `failed` è stata inserita.
- **error redaction.** Ogni `error_message` persistito su
  `orchestration_runs.failure_reason`, `orchestration_agent_runs.failure_reason`,
  `provider_invocations.error_message`, e su qualunque payload di evento è
  passato attraverso `_safe_error_message` di `ORCH-PROVIDER-A` (mascheramento
  di segreti `name=value`/`name: value` e troncamento a `_ERROR_MESSAGE_MAX_LEN`).
- **no published answer.** Nessun `published_answers` è creato; nessun
  `final_gate_reports` è creato.
- **no gate.** Il gate non è invocato in nessun branch.

---

## 19. Budget model

Il runner riconosce il `TokenBudget` configurato pre-run e lo usa come
preflight check. In `ORCH-RUNNER-A`, single-agent single-pass:

- **`token_budgets` come config pre-run.** Il runner riceve, opzionalmente,
  un `token_budget_id` nell'input (vedi §5). Il limite è letto dalla riga
  `token_budgets`, non da una vista live: per fissarlo immutabilmente per
  il run, `ORCH-RUNNER-A` può copiare i valori (`token_limit`,
  `overflow_policy`) dentro lo `snapshot_payload` di
  `agent_config_snapshots` (vedi §9) o dentro `bounding_parameters` di
  `orchestration_runs`. Decisione raccomandata: copia in
  `snapshot_payload`, coerente con il principio "snapshot immutabile al
  run start" di `ORCH-SCHEMA-PRE`.
- **`budget_limit_tokens` semplice.** `ORCH-RUNNER-A` può supportare un
  singolo budget per-agent o per-orchestration; più granularità è
  rinviata.
- **preflight via `enforce_mock_budget`.** Il runner chiama
  `enforce_mock_budget(provider_request,
  budget_limit_tokens=<from snapshot>)` prima dell'invocazione del mock.
  La funzione è pura e deterministica; ritorna `None` se entro budget, o
  un `ProviderError(error_code='budget_exceeded', retryable=False)` se
  fuori budget.
- **budget_exceeded non retryable.** Coerentemente con il codominio di
  `ORCH-PROVIDER-A`: `budget_exceeded` è in `NON_RETRYABLE_ERROR_CODES`.
- **`token_budget_exceeded` event solo se applicabile.** L'event_type
  `token_budget_exceeded` esiste nel codominio reale di
  `orchestration_events` (`0011`); il runner lo appende quando il
  preflight rifiuta l'invocazione. È l'unico event_type del codominio che
  si attiva sul budget; non esistono varianti per "budget warning" o
  "budget soft".
- **nessun costo reale.** Il `cost_estimate` di `MockProviderAdapter` è
  `Decimal("0")`; il budget di costo (`cost_limit`) può esistere come
  configurazione ma non è attivamente esercitato in `ORCH-RUNNER-A`.

Comportamento al raggiungimento del limite:

- se `overflow_policy='hard_stop'` (default raccomandato per
  `ORCH-RUNNER-A`): il preflight fallisce, il runner termina il run con
  `status='failed'`, `error_code='budget_exceeded'`, appende
  `token_budget_exceeded` e `run_failed`;
- se `overflow_policy='warn'`: rinviato a una fase futura;
  `ORCH-RUNNER-A` può scegliere di trattare il `warn` come `hard_stop`
  per semplicità, documentandolo.

---

## 20. Event sequence

La sequenza nominale di eventi appesi a `orchestration_events` durante un
run riuscito in `ORCH-RUNNER-A`, usando **solo `event_type` reali presenti
nel codominio di `0011`** e rispettando la disciplina insert-once /
final-status di §10 e l'ordine FK di §17:

1. **`run_created`** — appeso subito dopo l'`INSERT orchestration_runs`
   (status `pending`). `sequence_no = 0`,
   `idempotency_key = <run_idempotency_key>:run_created`.
2. *(transizione `pending → running`)* — **nessun event_type dedicato nel
   codominio**: la transizione è materializzata da `UPDATE
   orchestration_runs SET status='running', started_at=NOW()`. La
   tracciabilità è data dall'event_type adiacente successivo
   (`agent_run_started`).
3. **`agent_run_started`** — appeso dopo l'`INSERT agent_config_snapshots`
   e **prima** dell'invocazione mock. `sequence_no = 1`,
   `related_entity_type = 'orchestration_agent_run'`,
   `related_entity_id = <agent_run_id preallocato in memoria>` (vedi §10
   e §17: la riga `orchestration_agent_runs` non esiste ancora; il
   campo `related_entity_id` di `orchestration_events` non è una FK,
   quindi può portare un UUID preallocato).
   `idempotency_key = <run_idempotency_key>:agent_run_started`.
4. *(invocazione `MockProviderAdapter` in memoria)* — **nessun event_type
   `provider_invocation_started` nel codominio**: la riga
   `provider_invocations` (scritta più tardi al passo 6) è essa stessa
   il fatto auditabile, e `request_hash` ne registra l'identità prima
   ancora della scrittura.
5. **(scrittura riga `orchestration_agent_runs` con status finale
   `succeeded`)** — **non è un `orchestration_events`**; è l'`INSERT`
   della riga di fatto, scritta una sola volta dopo l'invocazione,
   coerentemente con la disciplina insert-once / final-status di §10.
6. *(scritture FK-dipendenti dalla riga `orchestration_agent_runs`)* —
   `INSERT orchestration_agent_messages (system, user, assistant)`,
   `INSERT provider_invocations`, `INSERT orchestration_agent_outputs`,
   `INSERT token_usage_records`. **Nessun event_type dedicato nel
   codominio** per `provider_invocation_completed` /
   `agent_output_recorded` / `token_usage_recorded`: i record append-only
   sono i fatti, e la loro presenza è sufficiente per l'audit. Non
   scrivere event_type inesistenti.
7. **`source_candidate_created`** — appeso **una volta per candidate
   persistita** se e solo se il `ProviderResult` portava
   `source_candidates` e `ORCH-RUNNER-A` decide di persisterle (vedi
   §15). Ogni evento:
   - referenzia la riga `source_candidates` corrispondente via
     `related_entity_type = 'source_candidate'` e
     `related_entity_id = <source_candidate_id>`;
   - ha `sequence_no` crescente nella sequenza del run;
   - ha **`idempotency_key` distinto per ogni candidate**, costruito
     come
     `<run_idempotency_key>:source_candidate:<candidate_index>`
     oppure
     `<run_idempotency_key>:source_candidate:<candidate_hash>`
     (vedi §15.1, §16). Una stessa
     `<run_idempotency_key>:source_candidate:...` chiave **non può**
     ripetersi nello stesso run: la UNIQUE
     `orchestration_events_run_type_idem_uq` ne rifiuterebbe il
     secondo INSERT. Per N candidate il runner produce quindi N
     eventi con N `idempotency_key` distinti.
8. **`agent_run_completed`** — appeso dopo l'`INSERT
   orchestration_agent_runs (status='succeeded', ...)` e dopo le
   scritture FK-dipendenti. `sequence_no` incrementato.
   `related_entity_type = 'orchestration_agent_run'`,
   `related_entity_id = <agent_run_id>`,
   `idempotency_key = <run_idempotency_key>:agent_run_completed`.
9. *(transizione `running → completed`)* — **nessun event_type dedicato
   nel codominio**: la transizione è materializzata da `UPDATE
   orchestration_runs SET status='completed', completed_at=NOW()`. La
   tracciabilità è data da `agent_run_completed` immediatamente
   precedente.

Sequenza nominale su fallimento (provider error o budget exceeded):

1. **`run_created`**.
2. *(transizione `pending → running`)* materializzata se il runner è
   arrivato a iniziare; non garantita se il preflight fallisce subito.
3. **`agent_run_started`** se lo start logico dell'agent run è stato
   attivato (`agent_run_id` preallocato).
4. *(invocazione mock in memoria)* — il `ProviderResult` porta
   `status='failed'` con `error` normalizzato.
5. **(scrittura riga `orchestration_agent_runs` con status finale
   `failed`)** — se lo start logico era stato attivato. Non è un
   evento; è l'INSERT della riga di fatto.
6. *(scritture FK-dipendenti dalla riga `orchestration_agent_runs`)* —
   `INSERT orchestration_agent_messages (system, user)`, `INSERT
   provider_invocations (status='failed')`. Nessun
   `orchestration_agent_outputs`, nessun `token_usage_records` di
   competenza, nessun `source_candidates`.
7. **`token_budget_exceeded`** se il fallimento è per budget (preflight
   o post-result).
   `idempotency_key = <run_idempotency_key>:token_budget_exceeded`.
8. **`agent_run_failed`** se la riga `orchestration_agent_runs` con
   `status='failed'` è stata inserita.
   `related_entity_id = <agent_run_id>`,
   `idempotency_key = <run_idempotency_key>:agent_run_failed`.
9. **`run_failed`** sempre.
   `idempotency_key = <run_idempotency_key>:run_failed`.

Note importanti:

- **Sequenza monotòna di `sequence_no`.** Il runner garantisce
  `sequence_no` crescente all'interno del run, enforced dalla UNIQUE
  `orchestration_events_run_sequence_uq (orchestration_run_id, sequence_no)`.
- **Idempotency event-by-event.** Ogni evento ha la propria
  `idempotency_key`. Per eventi che si scrivono una sola volta per run
  (`run_created`, `agent_run_started`, `agent_run_completed`,
  `agent_run_failed`, `run_failed`, `token_budget_exceeded`) il suffisso
  stabile sopra elencato è sufficiente. Per eventi che si ripetono nello
  stesso run con lo stesso `event_type` — in `ORCH-RUNNER-A` solo
  `source_candidate_created` — la chiave **deve** portare un
  discriminante per occorrenza (`<candidate_index>` o
  `<candidate_hash>`), altrimenti la UNIQUE
  `orchestration_events_run_type_idem_uq` rifiuta il secondo INSERT.
- **Eventi del codominio non usati da `ORCH-RUNNER-A`.**
  `source_resolution_started`, `source_resolution_completed`,
  `source_verification_completed`, `synthesis_created`,
  `submitted_to_gate`, `gate_completed`, `run_cancelled` restano riservati
  a fasi future.

`ORCH-RUNNER-A` **non inventa event_type** che lo schema non consente.
Quando il prompt operativo elenca un evento ideale che non esiste nel
codominio, il runner lo **omette** e si appoggia all'event_type adiacente
reale e ai record append-only di fatto come traccia di audit.

---

## 21. Relationship with Final Answer Gate

**`ORCH-RUNNER-A` non integra il Final Answer Gate.** Vincoli espliciti:

- **`final_gate_report_id` resta NULL.** La colonna
  `orchestration_runs.final_gate_report_id` (FK nullable verso
  `final_gate_reports`) è valorizzata a `NULL` per ogni run prodotto da
  `ORCH-RUNNER-A`.
- **`publication_status = not_evaluated`.** Nell'output logico del runner
  (vedi §6), `publication_status` è sempre `not_evaluated`.
- **`CandidateSynthesis` non ancora prodotta.** Nessuna riga di
  `candidate_syntheses` è creata; nessun synthesis pass è eseguito.
- **Claim Extraction non ancora avviata.** Nessuna trasformazione output →
  claim.
- **Evidence Binding non ancora avviato.** Nessun collegamento claim →
  evidence_span.
- **gate integration futura sarà `ORCH-GATE-A`.** L'innesto fra una
  `candidate_syntheses` multi-AI e la catena `Claim Extraction → Evidence
  Binding → Final Answer Gate` esistente è materia della fase
  `ORCH-GATE-A` (vedi `PHASE_PRODUCT_ORCHESTRATION_PRE.md §19.11`), non di
  `ORCH-RUNNER-A`.
- **runner completed non significa publication allowed.** Un
  `orchestration_runs.status='completed'` indica solo che il mock run è
  terminato senza errore; non è una decisione di pubblicabilità.
- **Final Answer Gate resta l'unico gate.** `ORCH-RUNNER-A` non emette
  decisioni di publication-allowed / publication-held, non scrive
  `final_gate_reports`, non scrive `published_answers`. Lo schema `0011`
  rende strutturalmente impossibile pubblicare una risposta saltando il
  gate.

---

## 22. Relationship with existing closed-corpus pipeline

`ORCH-RUNNER-A` è una **fondazione parallela**, non un sostituto della
pipeline esistente. Vincoli espliciti:

- **closed-corpus pipeline esistente resta invariata.** Tutti i componenti
  della linea closed-corpus MVP-0 (8.4 + 8.5 + 8.6 + 8.7 + 8.8A +
  8.8B-REPORT) continuano a funzionare esattamente come oggi.
- **non modifica `task.created` consumer.** Il consumer single-consumer
  FK-safe / resume-safe / idempotente con 15 eventi audit worker-side
  resta invariato. `ORCH-RUNNER-A` non vi aggiunge step.
- **non modifica compiler.** `apps/worker/app/services/compiler.py` non è
  toccato.
- **non modifica Final Answer Gate.** `apps/worker/app/services/final_answer_gate.py`
  non è toccato; il gate continua a comporre CVE-lite > Claim Entailment >
  Source Quality come oggi.
- **non modifica Anti-Hallucination Report API (8.8B-REPORT).** La vista
  read-only aggregata task-level resta invariata.
- **non modifica API.** Nessuna route esistente è modificata.
- **non modifica UI.** Nessuna pagina o componente frontend è toccato.

`ORCH-RUNNER-A` introduce un nuovo servizio worker-level che lavora su
tabelle nuove (`orchestration_*`, `master_prompts`, `agent_configs`,
`agent_role_prompts`, `token_budgets`, `master_prompt_versions`,
`agent_config_snapshots`, `provider_invocations`, `token_usage_records`,
`source_candidates`, `candidate_syntheses` e le join, tutte introdotte da
`ORCH-SCHEMA-A`). Le due linee sono **disgiunte a livello di tabella di
fatto**: la pipeline closed-corpus scrive su `task_masters`, `draft_final_answers`,
`final_gate_reports`, `published_answers`, `agent_runs` (0005),
`agent_outputs` (0005, vuota), ecc.; `ORCH-RUNNER-A` scrive sulle tabelle
`orchestration_*` di `0011`. L'unico punto di contatto futuro sarà il punto
di giunzione `ORCH-GATE-A`, fuori scope per questa fase.

`ORCH-RUNNER-A` è quindi una **nuova foundation parallela per il futuro
orchestration product flow** descritto in
`PHASE_PRODUCT_ORCHESTRATION_PRE.md §4`.

---

## 23. Worker implications future

Le implicazioni a livello worker per `ORCH-RUNNER-A` e per le fasi
successive:

- **service worker-level.** `ORCH-RUNNER-A` introduce un nuovo servizio in
  `apps/worker/app/services/orchestration_runner.py` (nome indicativo). È
  un servizio puro che riceve un input e una connessione/engine e produce
  un output; non è un consumer Redis di per sé.
- **eventual event consumer.** Una fase futura può introdurre un consumer
  `orchestration_run.requested` (o simile) che riceve eventi di avvio di
  un run e invoca il service. `ORCH-RUNNER-A` **non** lo introduce: il
  collaudo iniziale può avvenire come chiamata diretta del service in
  test.
- **eventual enqueue.** Una fase futura può introdurre un endpoint API
  `POST /api/v1/orchestration-runs` che enqueue un evento sul consumer
  futuro. Fuori scope per `ORCH-RUNNER-A`.
- **resumability.** Coerentemente con il consumer `task.created`
  esistente, un futuro consumer `orchestration_run.requested` deve essere
  resume-safe: se interrotto e riavviato, riprende dallo stato persistito
  senza ripetere il lavoro già completato. L'idempotenza descritta in §16
  è il meccanismo che lo abilita.
- **retry.** Una fase futura può introdurre retry automatici degli
  `orchestration_agent_runs` falliti con `attempt_no` incrementato.
  `ORCH-RUNNER-A` non implementa retry.
- **partial failures.** Una fase futura (`ORCH-MULTI-A`) introdurrà il
  multi-agent: alcuni agenti possono fallire mentre altri riescono. Il
  partial failure handling è fuori scope per `ORCH-RUNNER-A`.
- **multi-agent expansion.** `ORCH-MULTI-A` estende il runner a più
  agenti; `ORCH-REVIEW-A` aggiunge reviewer/critic pass; `ORCH-SYNTHESIS-A`
  aggiunge il synthesis pass. Tutte fuori scope per `ORCH-RUNNER-A`.

In `ORCH-RUNNER-A`:

- **no Redis consumer obbligatorio.** Il service è collaudabile in test
  unit/service e DB test senza Redis.
- **no API trigger obbligatorio.** Il service è collaudabile via chiamata
  diretta in test.
- **service unit/DB tests sufficienti.** La copertura di test per la
  chiusura tecnica di `ORCH-RUNNER-A` può limitarsi a test unit (logica
  del service) e DB test (verifica delle scritture e dei vincoli).

---

## 24. Test strategy future

Strategia di test per `ORCH-RUNNER-A`, **mock-first, no rete, no Redis, no
FastAPI**, sul modello dei test worker-level esistenti
(`apps/worker/tests/test_orchestration_provider_service.py`) e dei test
root DB-only (`tests/test_orch_schema_constraints.py`).

### 24.1 Test unit / service

Test di logica pura del runner, senza DB (o con DB ridotto a mocking):

1. **successful single-agent mock run.** Input valido → output con
   `status='succeeded'`, `orchestration_run_id` non vuoto,
   `provider_invocation_id` non vuoto, `agent_output_id` non vuoto,
   `event_ids` non vuoto, `publication_status='not_evaluated'`,
   `gate_report_id=None`.
2. **provider error injection failed run.** `mock_error_code='timeout'`
   nell'input → output con `status='failed'`,
   `error_code='timeout'`, `provider_invocation_id` non vuoto (lo schema
   richiede di registrare l'invocazione fallita).
3. **budget exceeded failed/prevented run.** `budget_limit_tokens` molto
   basso → preflight fallisce, output con `status='failed'`,
   `error_code='budget_exceeded'`, `provider_invocation_id=None` (il
   provider non è mai stato invocato).
4. **idempotency replay same key.** Lo stesso `(tenant_id,
   idempotency_key)` ripresentato dopo un run `completed` → ritorna lo
   stesso `orchestration_run_id`, non ne crea uno nuovo; nessuna nuova
   `provider_invocations`, nessuna nuova `token_usage_records`.
5. **no duplicate `provider_invocations` / `token_usage_records`.**
   Verifica meccanica del count pre/post sul redelivery.
6. **source candidates remain proposed/unverified.** Quando l'input porta
   `mock_source_candidates` e `ORCH-RUNNER-A` persiste le candidate: ogni
   riga ha `status='proposed'` e `provenance.is_verified=False`.
7. **no `evidence_span_id` on `source_candidates`.** Verifica meccanica
   che `source_candidates` non abbia la colonna né un valore correlato
   (lo schema lo impone strutturalmente, ma il test lo asserisce
   esplicitamente).
8. **`final_gate_report_id` NULL.** Ogni `orchestration_runs` prodotto da
   `ORCH-RUNNER-A` ha `final_gate_report_id IS NULL`.
9. **no `published_answers`.** Verifica meccanica che il runner non
   inserisca righe in `published_answers`.
10. **no `final_gate_reports`.** Idem per `final_gate_reports`.
11. **event sequence monotonic.** Per ogni run, gli `orchestration_events`
    associati hanno `sequence_no` strettamente crescente.
12. **no secret persistence.** Lessicale: nessuna colonna persistita né
    nessun payload JSONB contiene API key, secret, credenziali,
    Authorization header.

### 24.2 Test DB

Test root DB-only (stile di
`tests/test_orch_schema_constraints.py`):

- **real Postgres.** Connessione reale via fixture `db_conn`.
- **use existing migration stack.** Esegue `scripts/migrate.py` se
  necessario (helper `_ensure_migrations`).
- **skip cleanly if DB unavailable.** La fixture deve fallire pulita-mente
  se il DB non è disponibile.
- **worker-level service test under `apps/worker/tests/`** (per la
  logica del service) **or root test if DB-heavy** (per i test che
  esercitano molte tabelle e vincoli simultaneamente).
- **no Redis.** Nessun client Redis nei test.
- **no FastAPI.** Nessuna istanza app, nessuna route.
- **no network.** Nessuna chiamata HTTP esterna.

### 24.3 Comandi

```bash
# Compilazione del modulo (atteso OK)
python3 -m py_compile apps/worker/app/services/orchestration_runner.py

# Test specifici del runner (atteso N passed)
pytest -q apps/worker/tests/test_orchestration_runner_service.py

# Eventuali test DB-level del runner (atteso N passed)
pytest -q tests/test_orch_runner_db.py

# Regressione opzionale dopo review (atteso green)
make test-db
```

I nomi di file sono indicativi; `ORCH-RUNNER-A` deciderà la struttura
esatta.

---

## 25. Security and redaction

Regole obbligatorie per `ORCH-RUNNER-A`, coerenti con `ORCH-PROVIDER-A` §6
e `PHASE_ORCH_PROVIDER_PRE.md §15`:

- **no secrets in runner input.** Il contratto di input (§5) non porta
  API key, token di autenticazione, credenziali, Authorization header,
  password.
- **no provider API keys.** Il provider è mock; nessuna chiave esiste o è
  attesa. Quando un provider reale sarà introdotto, le credenziali
  vivranno nell'implementazione del provider, non nell'input del runner.
- **no Authorization headers.** Nessun header di autenticazione è
  persistito.
- **error_message redaction via provider module.** Ogni `error_message`
  scritto su `orchestration_runs.failure_reason`,
  `orchestration_agent_runs.failure_reason`,
  `provider_invocations.error_message`, e su payload di evento è passato
  attraverso `_safe_error_message` di
  `apps/worker/app/services/orchestration_provider.py`, che maschera
  segreti in stringhe `name=value` / `name: value` e tronca a
  `_ERROR_MESSAGE_MAX_LEN`.
- **no raw unredacted payload persisted.** Se in futuro si vorrà
  conservare un payload, lo si conserva solo nella forma redatta dalla
  `ProviderRedactionPolicy` (`hash_only` di default).
- **hashes for audit only.** `request_hash` e `response_hash` su
  `provider_invocations`, `prompt_text_hash` su `master_prompt_versions`,
  `content_hash` su `orchestration_agent_messages` /
  `orchestration_agent_outputs`, `synthesis_text_hash` su
  `candidate_syntheses` (futuro): tutti per audit/idempotenza/debug, mai
  per provare il contenuto.
- **no logs with secrets.** I log emessi dal runner non devono contenere
  segreti. I log dei test non devono contenere segreti. I payload di
  `orchestration_events.event_payload` non devono contenere segreti.
- **redaction_strategy persisted.** Ogni `provider_invocations` porta
  `redaction_strategy` (default `hash_only`), così un revisore può sapere
  con quale strategia il payload è stato trattato.

---

## 26. Non-goals

Questa fase, e il documento che produce, hanno i seguenti **non-goals
espliciti**. Nessuno di questi va perseguito in `ORCH-RUNNER-PRE`:

- **no code in `ORCH-RUNNER-PRE`** — nessun file di codice di produzione,
  worker, backend, frontend o pacchetto condiviso è scritto o modificato;
- **no provider real** — nessun OpenAI, Anthropic, Gemini introdotto;
- **no SDK** — nessun SDK di provider aggiunto;
- **no network** — nessuna chiamata di rete;
- **no local LLM** — nessun modello AI locale introdotto;
- **no API** — nessuna route HTTP aggiunta;
- **no UI** — nessuna pagina o componente frontend aggiunto;
- **no migrations** — nessun file in `migrations/`;
- **no tests** — nessun file di test scritto o modificato;
- **no source retrieval** — nessun recupero reale di fonti;
- **no source verification** — nessuna verifica reale di fonti;
- **no web retrieval** — nessuna capacità di recupero web;
- **no multi-agent** — single-agent only in `ORCH-RUNNER-A`;
- **no `CandidateSynthesis` integration** — nessuna sintesi multi-AI in
  `ORCH-RUNNER-A`;
- **no Claim Extraction** — nessuna scomposizione di un output in claim;
- **no Evidence Binding** — nessun collegamento claim → evidence_span;
- **no Final Answer Gate** — nessuna integrazione con il gate;
- **no `published_answers`** — il runner non scrive su `published_answers`.

---

## 27. Acceptance criteria

Il documento `PHASE_ORCH_RUNNER_PRE.md` è accettabile se e solo se:

- **crea solo `PHASE_ORCH_RUNNER_PRE.md`** — nessun altro file del
  repository è creato o modificato;
- **è in italiano tecnico** — l'intero documento è redatto in italiano
  tecnico;
- **legge solo i file indicati o dichiara eventuali extra** — i file letti
  sono quelli elencati nel prompt operativo §1; eventuali file aggiuntivi
  sono dichiarati nell'output finale;
- **usa solo schema/event_type reali da migration `0011`** — gli
  `event_type` usati appartengono al codominio chiuso di 14 valori di
  `orchestration_events_event_type_chk`; le tabelle, le colonne, i CHECK,
  le UNIQUE referenziati corrispondono a quelli reali di `0011`;
- **distingue esistente / futuro / fuori scope** — per ogni elemento il
  documento dichiara se è esistente, futuro o fuori scope;
- **non implementa codice** — nessun file di codice è scritto;
- **non promette provider reale** — il documento dichiara esplicitamente
  che `ORCH-RUNNER-A` è mock-first e che nessun provider reale è
  introdotto;
- **non tratta provider output come answer pubblicabile** — un
  `ProviderResult` resta candidato; un `orchestration_agent_outputs` resta
  output candidato; nessuna pubblicabilità è derivata da uno stato del
  runner;
- **non tratta source candidates come evidence** — le `source_candidates`
  restano unverified (`status='proposed'`, `is_verified=False`); nessun
  ponte verso `evidence_spans` è creato dal runner;
- **non bypassa Final Answer Gate** — `final_gate_report_id` resta NULL;
  `publication_status='not_evaluated'`; nessun `published_answers` o
  `final_gate_reports` è creato;
- **prepara chiaramente `ORCH-RUNNER-A`** — il documento è leggibile come
  istruzione di design per la fase successiva di codice;
- **contiene comandi di verifica** — la §28 fornisce i comandi che un
  revisore può eseguire meccanicamente;
- **non usa wording vietato** — il documento non contiene i termini della
  lista vietata fuori da elenchi espliciti o dal comando grep di
  controllo.

---

## 28. Comandi di verifica

I comandi seguenti permettono a un revisore di verificare i criteri di
accettazione della §27 in modo meccanico. Sono comandi di **sola lettura**:
non modificano il repository. Vanno eseguiti dalla radice del repository.

### 28.1 Comandi base

```bash
git diff --check
git diff --stat
git diff --name-only
git status -sb
```

`git diff --check` non deve segnalare errori di whitespace; `git diff
--stat` e `git diff --name-only` mostrano il perimetro delle modifiche;
`git status -sb` mostra lo stato sintetico del branch.

### 28.2 Controllo file singolo

```bash
git diff --name-only
```

Deve mostrare solo:

```
PHASE_ORCH_RUNNER_PRE.md
```

Nessun altro file del repository deve comparire.

### 28.3 Controllo wording vietato

Il controllo usa pattern con parentesi quadre sul primo carattere, così
che il comando non intercetti se stesso:

```bash
grep -niE "[t]ruth score|[v]erified true|[v]erified answer|[A]I verified|[f]actually true|[h]allucination eliminated|[h]allucination-free|[g]uaranteed truth|[z]ero hallucinations|[e]ntailed = true|[s]ource quality proves claim|[C]VE-lite proves support|[r]eal NLI|[c]ontradiction detector|[c]itation-to-claim validator" PHASE_ORCH_RUNNER_PRE.md || true
```

Deve restituire nulla.
