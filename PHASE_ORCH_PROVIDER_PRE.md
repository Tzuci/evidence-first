# PHASE ORCH-PROVIDER-PRE

> **Documento di design dell'astrazione provider AI futura.**
> Questo blocco è **solo progettazione**. Non implementa codice di produzione,
> non crea né modifica migration, non modifica `apps/api/*`, `apps/worker/*`,
> `apps/web/*`, `packages/shared/*`, non tocca i test, non aggiunge dipendenze,
> non aggiunge SDK provider, non aggiunge secret, non modifica `.env`, non
> modifica `README.md` né `PROJECT_STATE.md` né alcun altro `PHASE_*_PRE.md`.
> L'unico deliverable è questo file `PHASE_ORCH_PROVIDER_PRE.md`.
>
> Lingua: italiano tecnico, registro da System Architect.
>
> **Promemoria di linguaggio (vincolante per tutta la fase).** Il sistema è
> evidence-first ed evidence-gated. Non promette verità assoluta, non promette
> l'eliminazione totale delle allucinazioni, non dichiara che le sue risposte
> siano "vere". L'astrazione provider serve a ottenere **output candidati**
> dagli agenti AI; non decide se pubblicare, non verifica le fonti, non
> sostituisce il Final Answer Gate. Un output di provider non è un'evidenza,
> non è una risposta pubblicabile, e una citazione prodotta da un provider è
> solo una `source_candidate` da risolvere e verificare.
>
> **Nota di coerenza architetturale (vincolante).** Quando un'entità di questo
> documento registra un *fatto* (un'invocazione di provider, un consumo di
> token, un output di agente, una transizione), quel fatto è **append-only**:
> una sua "modifica" è una nuova riga, mai una riscrittura silenziosa. Una
> *configurazione* (un provider abilitato, una policy di retry, un budget) è
> mutabile solo finché un run non l'ha consumata; dopo, è congelata via
> snapshot. Questa fase prepara `ORCH-PROVIDER-A`, che dovrà essere
> **mock-first**.

---

## Indice

1. Scopo della fase
2. Stato attuale del sistema
3. Obiettivo prodotto della provider abstraction
4. Concetti principali
5. ProviderRegistry
6. ProviderAdapter interface — design logico
7. ProviderRequest
8. ProviderResult
9. ProviderError model
10. provider_invocations mapping
11. token_usage_records mapping
12. Budget enforcement
13. Retry e idempotenza
14. Timeout, rate limit e partial failures
15. Redaction e segreti
16. MockProviderAdapter
17. Remote provider futuri
18. Local LLM futuro
19. Agent message/output mapping
20. Source candidates mapping
21. Candidate synthesis mapping
22. Worker implications future
23. API implications future
24. Test strategy future
25. Security and compliance considerations
26. Non-goals
27. Acceptance criteria
28. Comandi di verifica

---

## 1. Scopo della fase

La fase **ORCH-PROVIDER-PRE** progetta, **solo a livello di design**,
l'astrazione provider AI futura del sistema Evidence-First MVP-0: il layer
attraverso cui, in fasi future, l'orchestrazione potrà parlare in modo uniforme
a un mock provider, a provider remoti e a un local LLM, senza che il resto del
sistema conosca i dettagli di ciascuno.

Questa fase è **esclusivamente di design**. Non:

- scrive codice di backend, worker, frontend o pacchetti condivisi;
- crea migration di database (nessun file in `migrations/`);
- modifica migration esistenti (`0001`-`0011` sono applicate e immutabili);
- modifica i test esistenti o ne aggiunge di nuovi;
- introduce provider AI reali o riferimenti operativi a provider esterni;
- introduce SDK di provider, dipendenze, secret o configurazioni di rete;
- modifica `.env` o qualunque file di configurazione di runtime;
- crea chiamate reali a provider AI;
- crea un local LLM;
- crea source retrieval reale o web retrieval;
- introduce route HTTP o pagine UI;
- introduce un gate parallelo o bypassa il Final Answer Gate;
- modifica `README.md`, `PROJECT_STATE.md` o documenti `PHASE_*_PRE.md`.

Lo scopo è **fissare un disegno condiviso dell'astrazione provider** che la
fase di codice successiva, `ORCH-PROVIDER-A`, potrà implementare senza dover
reinventare contratti. Questo documento descrive responsabilità, confini,
concetti, modelli concettuali (`ProviderRequest`, `ProviderResult`,
`ProviderError`), mapping verso lo schema introdotto da `ORCH-SCHEMA-A`
(migration `0011_orchestration_schema.sql`), e la strategia di test futura.

Si colloca nella roadmap incrementale di
`PHASE_PRODUCT_ORCHESTRATION_PRE.md §19`: è il blocco **§19.4
(ORCH-PROVIDER-PRE)**, segue `ORCH-SCHEMA-PRE` / `ORCH-SCHEMA-A` (lo schema
persistente è già applicato come `0011`) e precede direttamente
**ORCH-PROVIDER-A** (§19.5), la fase che implementa l'interfaccia di provider e
il **mock provider** deterministico dietro di essa. Ogni decisione qui presa è
una raccomandazione di design per quella fase, non un impegno di
implementazione: i "campi probabili" e i "metodi concettuali" sono indicativi e
soggetti a revisione in `ORCH-PROVIDER-A`.

`ORCH-PROVIDER-PRE` **non introduce alcun provider reale e nessun local LLM**.
L'astrazione qui descritta è concettuale. La sua implementazione, e ancor più
l'implementazione dei provider reali, sono materia delle fasi successive. La
fase prepara `ORCH-PROVIDER-A` perché sia **mock-first**: prima l'interfaccia,
poi il mock, e l'intera orchestrazione collaudata contro il mock prima che
qualunque provider reale venga introdotto.

---

## 2. Stato attuale del sistema

Questa sezione distingue ciò che **esiste oggi**, dopo `ORCH-SCHEMA-A`, da ciò
che **non esiste ancora** e da ciò che è **fuori scope** per questa fase. La
fase di codice successiva dovrà riverificare ogni elemento contro il proprio
HEAD prima di basarsi su di esso.

### Esistente

Dopo `ORCH-SCHEMA-A` (commit più recente rilevante `34c0f42`, "Add
orchestration schema foundation") esistono, come **schema persistente vuoto**,
le 19 tabelle della migration `0011_orchestration_schema.sql`. Tutte le tabelle
sono **vuote**: nessun runner le popola. Le famiglie rilevanti per questa fase:

- **schema ORCH-SCHEMA-A** — la migration `0011` è applicata, additiva,
  DDL-only; introduce 19 tabelle nuove e non tocca `0001`-`0010`.
- **`provider_invocations`** — tabella di fatto append-only che registra ogni
  invocazione del provider come fatto auditabile. Esiste già, con colonne
  `tenant_id`, `agent_run_id`, `orchestration_run_id`, `provider_name`,
  `model`, `request_hash`, `response_hash`, `status`, `error_code`,
  `error_message`, `tokens_input`, `tokens_output`, `cost_estimate`,
  `latency_ms`, `attempt_no`, `is_mock`, `redaction_strategy`,
  `idempotency_key`. CHECK `status ∈ {pending, succeeded, failed, cancelled}`.
  **Nessuna colonna per API key, secret o credenziali.** Nessun provider caller
  esiste ancora.
- **`token_usage_records`** — tabella di fatto append-only per il consumo reale
  di token. Esiste già, ma non c'è alcun token accounting reale che la
  popoli. Idempotenza tramite due indici UNIQUE parziali sul
  `provider_invocation_id` nullable.
- **`token_budgets`** — tabella di configurazione mutabile per il budget di
  token/costo **pre-run**. Esiste già, ma non c'è alcun enforcement reale del
  budget.
- **`orchestration_agent_runs` / `orchestration_agent_messages` /
  `orchestration_agent_outputs`** — tabelle di fatto append-only per le
  esecuzioni di agente e i loro messaggi/output. Esistono, ma nessun runner di
  orchestrazione le popola.
- **`source_candidates` / `source_resolutions` / `source_verifications`** — il
  flusso source candidate. `source_candidates` esiste e **non** porta
  `evidence_span_id`; `source_verifications` può collegarsi a `evidence_spans`.
  Esistono come schema, nessun retrieval reale li popola.
- **`candidate_syntheses`** — tabella di fatto append-only per la sintesi
  multi-AI candidata. Esiste, **non è** `published_answers`.
- **infrastruttura preesistente** — Claim Ledger, `evidence_spans`, CVE-lite,
  Source Quality, Claim Entailment, Final Answer Gate, Anti-Hallucination
  Report, audit chain hash-linked, `event_processing_records`,
  `policy_versions`. Tutto riusabile, nulla da modificare in questa fase.

### Non ancora esistente

Entità e capacità che l'astrazione provider richiede e che oggi **non
esistono**:

- **provider adapter** — non esiste alcun adapter che traduca una richiesta di
  agente in un'invocazione di provider e ne normalizzi la risposta.
- **provider registry** — non esiste alcun catalogo dei provider disponibili.
- **provider caller** — non esiste alcun componente worker che invochi
  l'astrazione di provider per conto di un agent run.
- **orchestration runner** — non esiste alcun consumer che guidi l'esecuzione
  di un `orchestration_runs`.
- **token budget enforcement** — non esiste alcun controllo di budget
  preflight o post-result.
- **provider retry scheduler** — non esiste alcuno scheduler di retry per
  invocazioni fallite.
- **provider read endpoint** — non esiste alcun endpoint HTTP che esponga
  `provider_invocations` o registri provider.
- **provider UI** — non esiste alcuna superficie di configurazione o
  osservabilità dei provider.

### Fuori scope per questa fase

Nessuno dei seguenti elementi va perseguito in `ORCH-PROVIDER-PRE`:

- **provider reali** — nessun OpenAI, Anthropic, Gemini o altro provider
  esterno introdotto o referenziato in modo operativo.
- **SDK** — nessun SDK di provider aggiunto come dipendenza.
- **secret management operativo** — nessun meccanismo concreto di gestione di
  credenziali.
- **worker** — nessun componente worker creato o modificato.
- **API** — nessuna route HTTP aggiunta o implementata.
- **UI** — nessuna pagina o componente frontend creato o modificato.
- **local LLM** — nessun modello AI locale introdotto o integrato.
- **source retrieval reale** — nessun recupero o risoluzione reale di fonti.
- **web retrieval** — nessuna capacità di recupero web introdotta.

---

## 3. Obiettivo prodotto della provider abstraction

L'astrazione provider è il layer futuro attraverso cui l'orchestrazione ottiene
**output candidati** dagli agenti AI. La sua funzione, in termini di prodotto,
è trasformare la configurazione e l'intenzione di un agente in un risultato
strutturato, auditabile e normalizzato, senza che il resto del sistema conosca
i dettagli di ogni provider concreto.

A livello concettuale la provider abstraction trasforma:

```
AgentConfigSnapshot      (la config immobilizzata dell'agente al run start)
+ AgentMessage plan      (i messaggi/istruzioni che l'agente deve inviare)
+ ProviderPolicy         (timeout, retry, cost policy applicabili)
+ TokenBudget            (il limite di token/costo in vigore per quel run)
+ RedactionPolicy        (la strategia di redaction dei payload)
```

in:

```
ProviderRequest          (la richiesta strutturata, redatta, hashabile)
   ▼
ProviderResult           (la risposta normalizzata, con usage ed errori)
   ▼
ProviderInvocationRecord (il fatto append-only su provider_invocations)
   ▼
TokenUsageRecord         (il consumo registrato su token_usage_records)
   ▼
AgentMessage / AgentOutput (i fatti su orchestration_agent_messages/outputs)
   ▼
SourceCandidate eventuale  (le fonti citate, come candidate non verificate)
```

**Cosa la provider abstraction NON fa.** È essenziale per la coerenza del
prodotto evidence-first che l'astrazione provider sia tenuta entro confini
stretti:

- **non decide la publishability.** L'astrazione produce output candidati; la
  decisione di pubblicare o trattenere resta del Final Answer Gate esistente.
- **non verifica le fonti.** Una citazione prodotta da un provider è una
  `source_candidate`; il recupero e la verifica sono fasi successive
  (`source_resolutions`, `source_verifications`).
- **non sostituisce il Final Answer Gate.** L'astrazione provider non è un
  gate, non emette decisioni di pubblicabilità, non introduce un asse di
  "verità multi-AI".
- **non produce una published answer.** L'output di un provider non è una
  risposta pubblicata; al massimo alimenta una `candidate_syntheses`, che a sua
  volta deve attraversare Claim Extraction, Evidence Binding e Final Answer
  Gate.
- **produce solo output candidati.** Tutto ciò che esce dall'astrazione
  provider è materiale candidato — risposte di agente, fonti proposte, sintesi
  candidate — mai un verdetto e mai una risposta pubblicabile.

Coerentemente con `PHASE_PRODUCT_ORCHESTRATION_PRE.md §10.5`: una **risposta
articolata reale** richiede un provider AI esterno o un local LLM integrato. In
modalità solo mock l'astrazione è collaudabile end-to-end — il che è prezioso e
necessario — ma il mock non produce intelligenza reale. L'astrazione provider è
disegnata perché il passaggio da mock a provider reale sia un cambiamento di
*dato* (`is_mock` da true a false, `provider_name`/`model` reali, hash di
payload reali), non un cambiamento di *struttura*.

---

## 4. Concetti principali

Questa sezione definisce, in modo testuale, i concetti che compongono
l'astrazione provider. Per ciascuno: scopo, input, output, cosa non deve fare,
e la relazione con lo schema `ORCH-SCHEMA-A`. Sono concetti, non codice:
nessuna firma, nessuna classe definitiva.

### ProviderRegistry

- **Scopo.** Catalogo futuro dei provider disponibili e delle loro capacità.
- **Input.** Una configurazione statica (in `ORCH-PROVIDER-A` può essere
  in-memory) che descrive i provider noti.
- **Output.** Dato un `provider_name`, restituisce l'adapter corrispondente e i
  suoi descrittori di capacità/modello.
- **Cosa non deve fare.** Non conserva segreti; non esegue chiamate; non decide
  publishability.
- **Relazione con lo schema.** Nessuna FK diretta; il `provider_name` registrato
  qui è la stringa opaca che finirà in `provider_invocations.provider_name`.

### ProviderAdapter

- **Scopo.** Interfaccia logica uniforme verso un provider concreto.
- **Input.** Una `ProviderRequest` (più le policy applicabili).
- **Output.** Una `ProviderResult` o un `ProviderError` normalizzato.
- **Cosa non deve fare.** Non scrive su DB direttamente (la persistenza è del
  worker che lo chiama); non decide publishability; non verifica fonti.
- **Relazione con lo schema.** Le sue invocazioni vengono materializzate dal
  worker in `provider_invocations`; il suo usage in `token_usage_records`.

### MockProviderAdapter

- **Scopo.** Adapter deterministico, senza rete, per `ORCH-PROVIDER-A`.
- **Input.** Una `ProviderRequest`.
- **Output.** Una `ProviderResult` deterministica con `is_mock=true`.
- **Cosa non deve fare.** Non chiama provider esterni; non produce intelligenza
  reale; non rende pubblicabile una risposta.
- **Relazione con lo schema.** Produce `provider_invocations` con
  `is_mock=true`; il suo usage è esplicitamente marcato mock.

### FutureRemoteProviderAdapter

- **Scopo.** Adapter astratto per un provider remoto futuro (OpenAI-like,
  Anthropic-like, Gemini-like, generic HTTP).
- **Input.** Una `ProviderRequest`.
- **Output.** Una `ProviderResult` con `is_mock=false`.
- **Cosa non deve fare.** Non esiste in questa fase; non va introdotto senza
  una fase dedicata con secret management, cost policy e test no-network.
- **Relazione con lo schema.** Quando esisterà, popolerà `provider_invocations`
  con dati reali; lo schema non cambia.

### FutureLocalLLMAdapter

- **Scopo.** Adapter astratto per un modello eseguito localmente.
- **Input.** Una `ProviderRequest`.
- **Output.** Una `ProviderResult`; `is_mock=false`; `cost_estimate` può essere
  assente o nullo.
- **Cosa non deve fare.** Non esiste in questa fase; `ORCH-PROVIDER-A` non deve
  assumerlo.
- **Relazione con lo schema.** Quando esisterà, popolerà le stesse tabelle; il
  suo output resta candidato e deve passare dal gate.

### ProviderRequest

- **Scopo.** Modello concettuale della richiesta inviata a un adapter.
- **Input.** Costruita dal worker a partire da `AgentConfigSnapshot`,
  `AgentMessage plan`, policy e budget.
- **Output.** È essa stessa l'input dell'adapter; produce un `request_hash`.
- **Cosa non deve fare.** Non deve contenere API key, Authorization header
  persistito, secret env value; il payload completo solo se redatto.
- **Relazione con lo schema.** `request_hash` finisce in
  `provider_invocations.request_hash`. Dettaglio in §7.

### ProviderResult

- **Scopo.** Modello concettuale della risposta normalizzata di un adapter.
- **Input.** L'esito di una `invoke(...)` riuscita o fallita.
- **Output.** Alimenta `provider_invocations`, `token_usage_records`,
  `orchestration_agent_messages/outputs`, eventuali `source_candidates`.
- **Cosa non deve fare.** `content_text` non è una risposta pubblicabile;
  `structured_payload` non è evidence; `source_candidates` non sono evidence.
- **Relazione con lo schema.** Vedi §8 e §10-§11.

### ProviderError

- **Scopo.** Modello concettuale dell'errore normalizzato di un adapter.
- **Input.** Un fallimento di invocazione.
- **Output.** `error_code` normalizzato + `error_message` redatto.
- **Cosa non deve fare.** Non deve contenere segreti; errori terminali non
  producono output pubblicabile.
- **Relazione con lo schema.** `error_code`/`error_message` finiscono in
  `provider_invocations`. Dettaglio in §9.

### ProviderUsage

- **Scopo.** Modello concettuale del consumo riportato da un'invocazione.
- **Input.** I contatori di token e il costo stimato di una `ProviderResult`.
- **Output.** Alimenta `token_usage_records` e i campi token di
  `provider_invocations`.
- **Cosa non deve fare.** Non deve presentare un consumo mock come reale.
- **Relazione con lo schema.** `tokens_input`, `tokens_output`,
  `cost_estimate`, `is_mock`.

### ProviderTimeoutPolicy

- **Scopo.** Configurazione dei limiti di tempo di un'invocazione.
- **Input.** Un valore di timeout (provider-level e/o orchestration-level).
- **Output.** Determina quando un'invocazione che non risponde diventa un
  fallimento controllato (`error_code='timeout'`).
- **Cosa non deve fare.** Non deve trasformare un timeout in un esito ambiguo.
- **Relazione con lo schema.** Un timeout è `status='failed'` +
  `error_code='timeout'` su `provider_invocations`.

### ProviderRetryPolicy

- **Scopo.** Configurazione di quanti e quali retry sono ammessi.
- **Input.** Numero massimo di tentativi, backoff, classi di errore retryable.
- **Output.** Governa la generazione di nuovi `attempt_no`.
- **Cosa non deve fare.** Non deve fare update silenziosi di record storici;
  ogni tentativo è una nuova riga.
- **Relazione con lo schema.** `provider_invocations.attempt_no` e la UNIQUE
  `(agent_run_id, attempt_no, idempotency_key)`.

### ProviderRedactionPolicy

- **Scopo.** Configurazione di come i payload vengono redatti prima di essere
  conservati o loggati.
- **Input.** Un'identità di strategia di redaction.
- **Output.** Determina cosa finisce in `redaction_strategy` e come.
- **Cosa non deve fare.** Non deve mai lasciar passare segreti.
- **Relazione con lo schema.** `provider_invocations.redaction_strategy`.
  Dettaglio in §15.

### ProviderCapability

- **Scopo.** Descrizione di cosa un provider sa fare (per esempio: testo,
  output strutturato, tool events futuri).
- **Input.** Dichiarata dall'adapter / registry.
- **Output.** Consultata dal runner per decidere se una richiesta è ammissibile.
- **Cosa non deve fare.** Non è una garanzia di correttezza del provider.
- **Relazione con lo schema.** Nessuna FK; informazione di registry.

### ProviderModelDescriptor

- **Scopo.** Descrizione di un modello presso un provider (identificativo,
  limiti, capacità).
- **Input.** Dichiarato dal registry.
- **Output.** Il valore `model` opaco che finisce in `ProviderRequest` e in
  `provider_invocations.model`.
- **Cosa non deve fare.** Non valida il modello a livello di schema; il modello
  resta una stringa opaca.
- **Relazione con lo schema.** `provider_invocations.model`,
  `agent_configs.model`.

### ProviderInvocationRecord

- **Scopo.** Rappresentazione, lato astrazione, del fatto auditabile che
  un'invocazione è avvenuta.
- **Input.** `ProviderRequest` + `ProviderResult`/`ProviderError`.
- **Output.** Una riga append-only in `provider_invocations`.
- **Cosa non deve fare.** Non contiene segreti; non implica verità del
  contenuto.
- **Relazione con lo schema.** È il mapping diretto su `provider_invocations`,
  dettagliato in §10.

### ProviderCostPolicy

- **Scopo.** Configurazione futura di come il costo di un'invocazione viene
  stimato e limitato.
- **Input.** Tariffe per modello, limiti di costo.
- **Output.** Il `cost_estimate` riportato; concorre al budget enforcement.
- **Cosa non deve fare.** In MVP-0, con `MAX_COST_PER_TASK=0` e provider mock,
  non deve dichiarare un costo reale; il costo mock è esplicitamente simulato.
- **Relazione con lo schema.** `provider_invocations.cost_estimate`,
  `token_usage_records.cost_estimate`, `token_budgets.cost_limit`.

### ProviderOutputContract

- **Scopo.** Descrizione della forma attesa dell'output di un agente (testo
  libero, lista di affermazioni, formato strutturato).
- **Input.** Deriva da `agent_configs.output_contract`.
- **Output.** Guida il `parse_response(...)` dell'adapter.
- **Cosa non deve fare.** Non rende l'output pubblicabile né verificato.
- **Relazione con lo schema.** `agent_configs.output_contract`,
  `orchestration_agent_outputs.output_kind`.

---

## 5. ProviderRegistry

Il `ProviderRegistry` è il **catalogo futuro dei provider disponibili**: la
struttura che, dato un `provider_name`, fa risalire all'adapter da usare e ai
suoi descrittori di capacità e modello.

Il registry descrive, per ciascun provider:

- **`provider_name`** — la stringa opaca che identifica il provider; in MVP-0
  l'unico valore operativo è `mock`. È lo stesso valore che finisce in
  `provider_invocations.provider_name` e in `agent_configs.provider`.
- **adapter type** — quale `ProviderAdapter` concreto implementa il provider:
  `MockProviderAdapter`, `FutureRemoteProviderAdapter`, `FutureLocalLLMAdapter`.
- **capabilities** — le `ProviderCapability` dichiarate dal provider (testo,
  output strutturato, eventuali tool events futuri).
- **model descriptors** — i `ProviderModelDescriptor` dei modelli disponibili
  presso quel provider.
- **classificazione mock/remote/local** — se il provider è un mock
  deterministico, un provider remoto, o un local LLM. La classificazione è
  importante perché governa cosa il sistema può promettere e come marca i
  record (`is_mock`).
- **enabled/disabled** — se il provider è attivo. In MVP-0 solo `mock` è
  abilitato.
- **no secret persistence** — il registry **non** contiene credenziali, API
  key o secret. L'autenticazione, quando esisterà, vive nell'implementazione
  del provider remoto, non nel catalogo.

Chiarimenti vincolanti:

- **Il registry non è implementato ora.** `ORCH-PROVIDER-PRE` è una fase di
  design; il registry è materia di `ORCH-PROVIDER-A`.
- **In `ORCH-PROVIDER-A` il registry può essere statico/in-memory** e contenere
  il solo `mock`. Non serve persistenza di registry su DB per il mock.
- **I provider reali richiedono una fase separata.** Aggiungere un provider
  remoto al registry comporta secret management, cost policy, gestione di
  timeout/rate-limit/retry reali e test opt-in/no-network: è lavoro di una fase
  dedicata e successiva, non di `ORCH-PROVIDER-A`.

---

## 6. ProviderAdapter interface — design logico

Questa sezione descrive l'interfaccia logica del `ProviderAdapter`. È un
**design logico, non codice definitivo**: i metodi sono concettuali, le firme
indicative, soggette a revisione in `ORCH-PROVIDER-A`.

Metodi concettuali dell'interfaccia:

- **`provider_name()`** — restituisce il nome opaco del provider.
- **`supported_models()`** — restituisce i `ProviderModelDescriptor`
  disponibili.
- **`capabilities()`** — restituisce le `ProviderCapability` dichiarate.
- **`build_request(...)`** — costruisce una `ProviderRequest` a partire da
  `AgentConfigSnapshot`, `AgentMessage plan`, policy e budget.
- **`estimate_usage(...)`** — stima il consumo di token/costo di una richiesta
  *prima* dell'invocazione, per alimentare il preflight budget check.
- **`enforce_preflight_budget(...)`** — confronta la stima con il
  `TokenBudget` in vigore e segnala se l'invocazione sforerebbe il budget.
- **`invoke(...)`** — esegue l'invocazione vera e propria (per il mock:
  deterministica e senza rete).
- **`parse_response(...)`** — normalizza la risposta grezza in una
  `ProviderResult`, secondo il `ProviderOutputContract`.
- **`normalize_error(...)`** — traduce un fallimento in un `ProviderError` con
  `error_code` normalizzato.
- **`redact_request(...)`** — applica la `ProviderRedactionPolicy` alla
  richiesta prima che venga conservata o loggata.
- **`redact_response(...)`** — applica la redaction alla risposta.
- **`compute_request_hash(...)`** — calcola il `request_hash` deterministico
  della richiesta (per audit, idempotenza, debug).
- **`compute_response_hash(...)`** — calcola il `response_hash` deterministico
  della risposta.

Chiarimenti vincolanti:

- **Nessuna implementazione in `ORCH-PROVIDER-PRE`.** Questa fase descrive
  l'interfaccia; non scrive l'adapter.
- **`ORCH-PROVIDER-A` deve partire da `MockProviderAdapter`.** Prima si fissa
  l'interfaccia, poi si implementa il mock che la rispetta, e l'orchestrazione
  viene collaudata contro il mock.
- **I provider reali non vanno introdotti senza una fase dedicata.** Un
  `FutureRemoteProviderAdapter` o un `FutureLocalLLMAdapter` richiedono lavoro
  di trasporto, autenticazione, cost policy e test che esula da
  `ORCH-PROVIDER-A`.
- **L'interfaccia non espone dettagli di trasporto né di autenticazione.** HTTP,
  SDK, chiavi e secret vivono dentro le implementazioni concrete, mai
  nell'astrazione.

---

## 7. ProviderRequest

`ProviderRequest` è il modello concettuale della richiesta che un agente, via
adapter, invia a un provider. È costruita dal worker a partire dalla
configurazione immobilizzata dell'agente e dal piano di messaggi.

**Campi probabili** (indicativi, soggetti a revisione in `ORCH-PROVIDER-A`):

- `tenant_id` — il tenant del run.
- `project_id` — il progetto, se mantenuto come contenitore.
- `orchestration_run_id` — il run a cui la richiesta appartiene.
- `orchestration_agent_run_id` — l'agent run per conto del quale si invoca.
- `agent_config_snapshot_id` — lo snapshot immutabile della config dell'agente.
- `provider_name` — il provider invocato; in MVP-0 `mock`.
- `model` — l'identificativo del modello presso il provider; stringa opaca.
- `messages` — la lista dei messaggi (system/user/assistant/review/tool) che
  compongono la richiesta.
- `system_instructions` — le istruzioni di sistema dell'agente.
- `task_instructions` — le istruzioni di compito dell'agente.
- `output_contract` — la forma attesa dell'output (`ProviderOutputContract`).
- `constraints` — i vincoli (formato, lunghezza, divieti).
- `source_policy` — quali fonti l'agente può vedere.
- `max_tokens` — il limite di token per l'invocazione.
- `temperature_like_config` — i parametri di campionamento; opachi a livello di
  schema.
- `timeout_ms` — il limite di tempo dell'invocazione.
- `retry_policy` — la `ProviderRetryPolicy` applicabile.
- `redaction_strategy` — l'identità della `ProviderRedactionPolicy` applicata.
- `idempotency_key` — la chiave di idempotenza dell'invocazione.
- `request_hash` — l'hash deterministico della richiesta.
- `is_mock_expected` — indicatore che la richiesta si aspetta un'esecuzione
  mock-driven (in MVP-0 sempre true).

Chiarimenti vincolanti:

- **Non deve contenere API key.** La `ProviderRequest` non porta alcuna chiave
  di provider.
- **Non deve contenere Authorization header persistito.** Eventuali header di
  autenticazione vivono solo nell'implementazione di trasporto del provider
  remoto, non in questo modello e non in ciò che viene conservato.
- **Non deve contenere secret env value.** Nessun valore di variabile di
  ambiente segreta finisce nella richiesta.
- **Payload completo solo se redatto.** Se per debugging si vorrà conservare il
  payload, lo si conserva solo nella forma redatta dalla
  `ProviderRedactionPolicy`.
- **L'hash non prova la verità del contenuto.** `request_hash` è un'impronta:
  serve per audit, idempotenza e debug, non dice nulla sul merito della
  richiesta.

---

## 8. ProviderResult

`ProviderResult` è il modello concettuale della risposta normalizzata di
un'invocazione di provider.

**Campi probabili** (indicativi):

- `status` — l'esito dell'invocazione, coerente con il codominio di
  `provider_invocations.status` (`pending`, `succeeded`, `failed`,
  `cancelled`).
- `content_text` — il contenuto testuale della risposta.
- `structured_payload` — l'eventuale payload strutturato.
- `source_candidates` — le fonti proposte/citate dal provider.
- `tool_events` — eventi di tool, riservati a una capacità futura.
- `tokens_input` — i token di input consumati.
- `tokens_output` — i token di output consumati.
- `cost_estimate` — il costo stimato dell'invocazione.
- `latency_ms` — la latenza dell'invocazione.
- `response_hash` — l'hash deterministico della risposta.
- `raw_response_redacted` — l'eventuale risposta grezza, solo nella forma
  redatta.
- `error_code` — il codice di errore normalizzato, quando `status` non è
  `succeeded`.
- `error_message` — il messaggio di errore redatto.
- `retryable` — indicatore se l'errore è ritentabile secondo la policy.
- `is_mock` — indicatore che il risultato deriva da un'esecuzione mock-driven.

Chiarimenti vincolanti:

- **`content_text` non è una risposta pubblicabile.** È output candidato di un
  agente; diventa pubblicabile solo attraverso `candidate_syntheses` → Claim
  Extraction → Evidence Binding → Final Answer Gate.
- **`structured_payload` non è evidence.** Un payload strutturato è materiale
  candidato, non un'evidenza verificata.
- **`source_candidates` non sono evidence.** Le fonti che il provider cita sono
  `source_candidates`; devono passare da `source_resolutions` e
  `source_verifications` prima di poter contribuire al gate.
- **Usage/cost mock devono essere espliciti.** Quando il risultato deriva dal
  mock, `is_mock=true` e l'usage/cost vanno dichiarati come simulati, mai
  presentati come reali.
- **Gli errori devono essere normalizzati.** Un fallimento produce un
  `error_code` del codominio normalizzato (§9), non un messaggio grezzo
  provider-specifico.

---

## 9. ProviderError model

Il `ProviderError` model normalizza i fallimenti di invocazione in un insieme
chiuso di `error_code`. La normalizzazione è ciò che permette al runner e
all'audit di trattare i fallimenti in modo uniforme, indipendentemente dal
provider concreto.

**`error_code` normalizzati** (codominio proposto):

- `timeout` — l'invocazione non ha risposto entro `timeout_ms`.
- `rate_limited` — il provider ha segnalato un superamento di rate limit.
- `authentication_failed` — l'autenticazione verso il provider è fallita.
- `authorization_failed` — l'autorizzazione verso il provider è fallita.
- `provider_unavailable` — il provider non è raggiungibile o è in errore.
- `invalid_request` — la richiesta inviata è malformata o non accettata.
- `invalid_model` — il modello indicato non è valido presso il provider.
- `content_filter` — il provider ha rifiutato per filtro di contenuto.
- `malformed_response` — la risposta del provider non è interpretabile.
- `budget_exceeded` — l'invocazione è stata bloccata dal budget enforcement.
- `retry_exhausted` — i tentativi ammessi dalla retry policy sono esauriti.
- `network_error` — un errore di rete ha interrotto l'invocazione.
- `unknown_error` — un fallimento non classificabile negli altri codici.

Chiarimenti vincolanti:

- **Timeout e rate limit stanno in `error_code`, non negli status principali.**
  Lo schema `0011` usa un `provider_invocations.status` semplice (`pending`,
  `succeeded`, `failed`, `cancelled`): un timeout o un rate limit è un
  `status='failed'` con `error_code='timeout'` o `error_code='rate_limited'`.
  Questa è esattamente la correzione QA §2 già ratificata nello schema.
- **`error_message` deve essere redatto.** Il messaggio di errore passa per la
  `ProviderRedactionPolicy` prima di essere conservato.
- **Gli errori non devono contenere segreti.** Né `error_code`, né
  `error_message`, né il payload di errore devono mai contenere API key,
  Authorization header o secret env value.
- **Gli errori terminali non producono output pubblicabile.** Un fallimento
  terminale (`retry_exhausted`, `authentication_failed`, ecc.) non genera una
  risposta pubblicabile; al massimo è un fatto auditabile su
  `provider_invocations` e un eventuale evento di orchestrazione.

---

## 10. provider_invocations mapping

Questa sezione mappa concettualmente `ProviderRequest` e `ProviderResult` sulla
tabella `provider_invocations` introdotta da `ORCH-SCHEMA-A` (migration
`0011_orchestration_schema.sql`). Non scrive SQL: descrive come l'astrazione
provider, in una fase di codice futura, dovrà popolare quella tabella.

Mapping concettuale dei campi di `provider_invocations`:

- `orchestration_run_id` — da `ProviderRequest.orchestration_run_id`
  (denormalizzato, nullable nello schema, popolato per ergonomia di query).
- `orchestration_agent_run_id` — nello schema la colonna è `agent_run_id` (FK
  verso `orchestration_agent_runs`); da
  `ProviderRequest.orchestration_agent_run_id`.
- `provider_name` — da `ProviderRequest.provider_name`; in MVP-0 `mock`.
- `model` — da `ProviderRequest.model`; stringa opaca.
- `request_hash` — da `ProviderRequest.request_hash`.
- `response_hash` — da `ProviderResult.response_hash`.
- `status` — da `ProviderResult.status`; codominio `pending`, `succeeded`,
  `failed`, `cancelled`.
- `error_code` — da `ProviderResult.error_code` / `ProviderError`.
- `error_message` — da `ProviderResult.error_message`, redatto.
- `tokens_input` — da `ProviderResult.tokens_input` / `ProviderUsage`.
- `tokens_output` — da `ProviderResult.tokens_output` / `ProviderUsage`.
- `cost_estimate` — da `ProviderResult.cost_estimate` / `ProviderUsage`.
- `latency_ms` — da `ProviderResult.latency_ms`.
- `attempt_no` — da `ProviderRequest.retry_policy` / il tentativo corrente.
- `is_mock` — da `ProviderResult.is_mock`; in MVP-0 sempre true.
- `redaction_strategy` — da `ProviderRequest.redaction_strategy`.
- `idempotency_key` — da `ProviderRequest.idempotency_key`.

Chiarimenti vincolanti:

- **`provider_invocations` è un fatto auditabile append-only.** Lo schema `0011`
  applica il trigger condiviso `reject_modify_append_only()`: una riga, una
  volta scritta, non si modifica e non si cancella.
- **Nessun segreto in DB.** La tabella `provider_invocations` non ha, per
  costruzione dello schema `0011`, alcuna colonna per API key, secret,
  credenziali o token di autenticazione. L'astrazione provider non deve
  introdurne in futuro: l'auditabilità è data da `request_hash`/`response_hash`,
  `status`, `error_*`, token, latenza e `attempt_no`.
- **`is_mock=true` deve essere visibile.** Finché il provider è mock, ogni riga
  porta `is_mock=true`; un consumer, un report o una UI che leggano queste
  righe devono poter dichiarare onestamente che l'invocazione è mock-driven.
- **`request_hash`/`response_hash` non implicano verità.** Sono impronte per
  audit, idempotenza e debug; non dicono nulla sul merito della richiesta o
  della risposta.
- **Ogni retry crea un record distinto o comunque auditabile.** Un nuovo
  tentativo è una nuova riga `provider_invocations` con `attempt_no`
  incrementato; la UNIQUE `(agent_run_id, attempt_no, idempotency_key)` dello
  schema `0011` rende ogni tentativo distinto e idempotente sotto redelivery.
  Mai un update di una riga fallita.

## 11. token_usage_records mapping

Questa sezione descrive come l'astrazione provider, in una fase futura, dovrà
alimentare la tabella `token_usage_records` di `ORCH-SCHEMA-A`.

- **Quando creare `token_usage_records`.** Un record di consumo viene appeso
  dopo che un'invocazione di provider ha prodotto un esito con usage — tipico
  caso: dopo una `ProviderResult` riuscita, ma anche dopo un tentativo fallito
  che ha comunque consumato token (vedi retry accounting).
- **Differenza tra stima e consumo reale.** `estimate_usage(...)` produce una
  *stima* per il preflight budget check; `token_usage_records` registra il
  *consumo realmente avvenuto*. I due non vanno confusi: la stima non viene
  persistita come consumo.
- **Relazione con `orchestration_run_id`.** Ogni record è collegato al run
  (`token_usage_records.orchestration_run_id`, NOT NULL nello schema).
- **Relazione con `orchestration_agent_run_id`.** Nello schema la colonna è
  `agent_run_id` (nullable): popolata quando il consumo è attribuibile a un
  agent run specifico.
- **Relazione con `provider_invocation_id`.** Nullable: popolata quando il
  consumo è attribuibile a una singola invocazione di provider — è la
  granularità più fine.
- **Mock usage.** Quando il consumo deriva dal mock, `is_mock=true`: il consumo
  è stimato o simulato, non un costo provider reale.
- **Cost estimate.** `cost_estimate` riflette la `ProviderCostPolicy`; in MVP-0,
  con `MAX_COST_PER_TASK=0` e provider mock, è zero o simulato.
- **`pass_kind`.** Il pass a cui il consumo si riferisce: codominio dello schema
  `independent_answer`, `reviewer`, `critic`, `synthesis`, `second_check`,
  `source_resolution`.
- **`idempotency_key`.** Per assorbire redelivery: un doppio delivery
  dell'evento che registra un consumo non deve duplicare il record.
- **Partial indexes introdotti in `ORCH-SCHEMA-A`.** Poiché
  `provider_invocation_id` è nullable e in PostgreSQL una UNIQUE su colonna
  NULL ammette duplicati, lo schema `0011` usa **due indici UNIQUE parziali**:
  `token_usage_records_provider_idem_uq` su `(orchestration_run_id,
  provider_invocation_id, idempotency_key)` `WHERE provider_invocation_id IS
  NOT NULL`, e `token_usage_records_no_provider_idem_uq` su
  `(orchestration_run_id, idempotency_key)` `WHERE provider_invocation_id IS
  NULL`. L'astrazione provider deve fornire una `idempotency_key` coerente con
  entrambi i casi.

Chiarimenti vincolanti:

- **`token_usage_records` è append-only.** Trigger `reject_modify_append_only()`
  dallo schema `0011`: ogni consumo è un fatto, non si riscrive.
- **`token_budgets` è configurazione pre-run.** Il budget dice "quanto era
  ammesso"; `token_usage_records` dice "quanto è stato consumato". Sono entità
  distinte.
- **L'usage non deve essere sovrascritto.** Un nuovo consumo è una nuova riga.
- **Le aggregazioni future devono derivare dai fatti.** Un eventuale
  `total_tokens`/`total_cost` materializzato su run o agent run è una
  proiezione dei record append-only, scritta una sola volta al completamento,
  mai una fonte di verità indipendente.

## 12. Budget enforcement

Questa sezione descrive l'uso futuro di `token_budgets` come supporto al budget
enforcement. `ORCH-PROVIDER-PRE` non implementa enforcement: ne fissa il
disegno.

Uso futuro di `token_budgets`:

- **`per_orchestration`** — il tetto complessivo di token/costo dell'intero
  run.
- **`per_agent`** — il sottoinsieme del budget assegnato a un singolo agente.
- **`per_pass`** — il limite opzionale sul singolo pass di orchestrazione.
- **`hard_stop`** — `overflow_policy` per cui, raggiunto il limite,
  l'orchestrazione si ferma in modo controllato.
- **`warn`** — `overflow_policy` per cui il superamento viene segnalato come
  evento ma l'esecuzione prosegue.
- **retry accounting** — i token dei tentativi falliti vanno contati contro il
  budget; vedi §13.
- **preflight budget check** — prima di un'invocazione, `estimate_usage(...)` +
  `enforce_preflight_budget(...)` confrontano la stima con il budget residuo e
  decidono se l'invocazione può partire.
- **post-result budget accounting** — dopo un'invocazione, il consumo reale
  registrato in `token_usage_records` aggiorna il consumo aggregato del run.
- **budget exceeded event** — quando il consumo raggiunge o supera il limite,
  viene appeso un `orchestration_events` di tipo `token_budget_exceeded` (il
  codominio dello schema `0011` include già questo `event_type`).
- **partial run handling** — un run che esaurisce il budget può terminare con
  un esito parziale esplicito (per esempio non avviando i pass opzionali), mai
  con un esito ambiguo.

Chiarimenti vincolanti:

- **`ORCH-PROVIDER-PRE` non implementa enforcement.** Questa fase descrive solo
  il disegno.
- **`ORCH-PROVIDER-A` può testare l'enforcement mock.** Con il mock provider,
  l'enforcement è collaudabile in modo deterministico e senza rete.
- **Un provider reale richiede enforcement prima e dopo la chiamata.** Il
  preflight budget check prima dell'invocazione e il post-result accounting
  dopo sono entrambi necessari quando il costo è reale.

## 13. Retry e idempotenza

Questa sezione descrive il disegno di retry e idempotenza dell'astrazione
provider.

- **`idempotency_key`** — chiave opaca che identifica un'invocazione logica. Un
  redelivery con la stessa chiave non deve duplicare un `provider_invocations`
  né un `token_usage_records`.
- **`attempt_no`** — il numero del tentativo; ogni tentativo, riuscito o
  fallito, appende il proprio record con `attempt_no` distinto.
- **retry policy** — la `ProviderRetryPolicy` definisce max tentativi, backoff
  e classi di errore retryable.
- **retryable errors** — errori per cui un nuovo tentativo è ammesso.
- **non-retryable errors** — errori per cui un nuovo tentativo non è ammesso.
- **backoff futuro** — un backoff (per esempio esponenziale) sarà introdotto
  con i provider reali; con il mock non serve.
- **max attempts** — il numero massimo di tentativi è parte della policy.
- **auditabilità** — ogni tentativo è un fatto auditabile su
  `provider_invocations`.
- **append-only `provider_invocations`** — un retry è una nuova riga, mai un
  update di una riga fallita.
- **dedup sotto redelivery** — la UNIQUE `(agent_run_id, attempt_no,
  idempotency_key)` dello schema `0011` assorbe un doppio delivery dello stesso
  tentativo.

Regole:

- **Retry su `timeout`/`rate_limited`/`network_error` se la policy lo
  consente.** Questi sono gli errori tipicamente ritentabili.
- **No retry su `authentication_failed`/`authorization_failed`/
  `invalid_request`.** Questi errori non si risolvono ripetendo la stessa
  invocazione; ritentarli è uno spreco e un rischio.
- **`retry_exhausted` deve essere tracciabile.** Quando i tentativi ammessi
  sono esauriti, l'ultimo record porta `error_code='retry_exhausted'` (o lo
  registra in modo equivalente), così l'audit mostra che il retry è stato
  esaurito.
- **Nessun update silenzioso di record storici.** Un tentativo fallito resta,
  append-only; il tentativo successivo è una nuova riga.

## 14. Timeout, rate limit e partial failures

- **`timeout_ms`** — il limite di tempo di un'invocazione, parte della
  `ProviderRequest` e della `ProviderTimeoutPolicy`.
- **provider-level timeout** — il timeout applicato alla singola invocazione di
  provider.
- **orchestration-level timeout** — un eventuale timeout complessivo del run o
  di un pass, distinto dal timeout della singola invocazione.
- **rate limit** — un provider può segnalare un superamento di rate limit;
  l'esito è `status='failed'`, `error_code='rate_limited'`.
- **exponential backoff futuro** — con i provider reali, i retry su
  `rate_limited` adotteranno un backoff; con il mock non serve.
- **partial agent failure** — un run in cui un sottoinsieme di agenti
  fallisce: il run deve poter procedere in modo controllato o terminare con un
  esito parziale esplicito.
- **run cancellation** — un run può essere annullato; lo schema prevede
  `orchestration_runs.status='cancelled'` e l'evento `run_cancelled`.
- **graceful degradation** — al raggiungimento di un limite (budget, timeout),
  l'orchestrazione degrada in modo controllato, per esempio non avviando i pass
  opzionali.
- **eventi orchestration futuri** — i fallimenti significativi vanno registrati
  come `orchestration_events` (`agent_run_failed`, `run_failed`,
  `token_budget_exceeded`, ecc.).

Chiarimenti vincolanti:

- **Un errore provider non deve pubblicare una risposta.** Nessun fallimento di
  invocazione produce una risposta pubblicabile.
- **Un agent failure può diventare input per la synthesis solo se una policy
  futura lo consente.** Un output parziale o mancante non entra automaticamente
  nella `candidate_syntheses`; serve una policy esplicita.
- **Un failure deve essere auditabile.** Ogni fallimento è un fatto registrato
  su `provider_invocations` e/o `orchestration_events`, mai un esito che
  "scompare".

## 15. Redaction e segreti

Regole obbligatorie:

- **Mai salvare API key in DB.** Nessuna tabella, in particolare
  `provider_invocations`, deve contenere una chiave di provider.
- **Mai salvare Authorization header.** Gli header di autenticazione non
  vengono persistiti.
- **Mai salvare secret env value.** Nessun valore di variabile di ambiente
  segreta finisce in un record.
- **Mai loggare secrets.** I log non devono contenere segreti.
- **Raw request/response solo redatti.** Se un payload grezzo viene conservato,
  lo è solo nella forma redatta.
- **Preferire hash + payload redatto.** L'auditabilità si appoggia a
  `request_hash`/`response_hash`; un payload completo è opzionale, redatto, e
  soggetto a una retention policy futura.
- **Secret configuration separata da audit.** Le credenziali, quando
  esisteranno, vivono nell'implementazione del provider, non nella tabella di
  audit.
- **Future retention policy.** I payload completi, se conservati, non possono
  crescere indefinitamente: serve una retention policy dedicata, da progettare
  quando i provider reali renderanno il payload un dato concreto.
- **Safe logs.** I log dell'astrazione provider devono essere progettati come
  safe-by-default: nessun campo sensibile loggato.

Definizioni concettuali:

- **`ProviderRedactionPolicy`** — la configurazione che descrive quali campi
  vanno redatti e come; la sua identità finisce in
  `provider_invocations.redaction_strategy`.
- **`SensitiveFieldFilter`** — il componente concettuale che, dato un payload,
  rimuove o maschera i campi sensibili.
- **`HashOnlyMode`** — modalità in cui di un payload si conserva solo l'hash,
  nessun contenuto.
- **`RedactedPayloadMode`** — modalità in cui si conserva il payload, ma nella
  forma redatta da `SensitiveFieldFilter`.
- **`NoRawPayloadMode`** — modalità in cui nessun payload grezzo viene
  conservato, in alcuna forma.

## 16. MockProviderAdapter

Il `MockProviderAdapter` è l'adapter che `ORCH-PROVIDER-A` deve implementare per
primo. È un mock provider futuro con queste proprietà:

- **deterministico** — dato lo stesso input produce lo stesso output;
- **nessuna chiamata esterna** — non esegue rete, non importa SDK;
- **`is_mock=true`** — ogni `provider_invocations` che produce porta
  `is_mock=true`;
- **fake usage/cost esplicitamente marcato** — l'usage e il costo sono
  simulati (per esempio derivati dalla lunghezza del testo mock) e dichiarati
  come tali;
- **`response_hash` calcolabile** — la risposta deterministica ha un hash
  deterministico, utile a test di hashing;
- **`source_candidates` chiaramente non verificate** — le eventuali fonti che
  il mock "cita" sono `source_candidates` marcate come non verificate;
- **output utile per test pipeline** — l'output del mock è abbastanza
  strutturato da esercitare la pipeline a valle (agent message/output, source
  candidate, eventuale synthesis);
- **error injection opzionale per test** — il mock può, su richiesta,
  iniettare errori normalizzati (`timeout`, `rate_limited`, ecc.) per
  esercitare i path di retry e error handling.

Chiarimenti vincolanti:

- **Il mock provider non produce intelligenza reale.** È uno strumento di
  collaudo, non una sintesi multi-AI reale.
- **Il mock provider non sostituisce un provider remoto.** Un'invocazione mock
  non equivale a una chiamata di rete a un provider esterno.
- **Il mock provider non sostituisce un local LLM.** È un adapter distinto, con
  natura distinta.
- **Il mock provider non rende pubblicabile una risposta.** Un output mock,
  come ogni output di provider, è candidato; deve attraversare il gate.

## 17. Remote provider futuri

I provider remoti futuri vanno descritti **solo in modo astratto**. Le forme
previste:

- **OpenAI-like** — un provider remoto con un'API di completamento testuale.
- **Anthropic-like** — un provider remoto con un'API di messaggi.
- **Gemini-like** — un provider remoto con la propria API.
- **generic HTTP adapter** — un adapter generico verso un endpoint HTTP che
  rispetti l'interfaccia `ProviderAdapter`.

Vincoli di questa sezione:

- **Non usare SDK.** Nessun SDK di provider va aggiunto come dipendenza in
  questa fase né in `ORCH-PROVIDER-A`.
- **Non indicare API key.** Nessuna chiave concreta va scritta in alcun file.
- **Non indicare configurazioni segrete concrete.** Nessun endpoint
  autenticato, nessun secret concreto.
- **Non aggiungere dipendenze.** Nessun manifest o lockfile va toccato.

Chiarimenti vincolanti:

- **Ogni provider reale richiede una fase dedicata.** Introdurre anche solo un
  provider remoto è lavoro di una fase separata e successiva a
  `ORCH-PROVIDER-A`.
- **Serve secret management.** Un provider remoto richiede un meccanismo di
  gestione delle credenziali, separato dall'audit.
- **Serve una cost policy.** Un provider remoto ha un costo reale: serve una
  `ProviderCostPolicy` concreta.
- **Servono timeout/rate-limit/retry reali.** Con un provider remoto i timeout,
  i rate limit e i retry diventano comportamenti concreti, con backoff.
- **Servono test opt-in/no-network by default.** I test che esercitano un
  provider remoto devono essere opt-in e, per default, non devono toccare la
  rete.

## 18. Local LLM futuro

Il local LLM futuro va descritto come un adapter separato, distinto sia dal
mock provider sia dai provider remoti:

- **diverso dal mock provider** — il local LLM produce output reale, non
  deterministico-simulato;
- **diverso dal remote provider** — il local LLM non esegue una chiamata di
  rete verso un terzo;
- **può avere un modello locale** — il modello è eseguito localmente;
- **può avere usage stimato** — il consumo di token può essere stimato;
- **può non avere un cost estimate reale** — senza un costo di provider
  esterno, `cost_estimate` può essere assente o nullo;
- **può avere vincoli di risorse locali** — memoria, CPU/GPU, tempo;
- **richiede una fase dedicata** — l'integrazione di un local LLM è lavoro di
  una fase separata.

Chiarimenti vincolanti:

- **Non implementato ora.** `ORCH-PROVIDER-PRE` non introduce alcun local LLM.
- **`ORCH-PROVIDER-A` non deve assumere il local LLM.** La fase mock-first non
  presuppone un modello locale.
- **L'output del local LLM resta candidato e deve passare dal gate.** Come ogni
  output di provider, un output di local LLM è materiale candidato; deve
  attraversare Claim Extraction, Evidence Binding e Final Answer Gate.

## 19. Agent message/output mapping

Questa sezione descrive come `ProviderRequest` e `ProviderResult` si mappano su
`orchestration_agent_messages` e `orchestration_agent_outputs`.

- **`ProviderRequest` deriva da `orchestration_agent_messages` o da un message
  plan futuro.** I messaggi che compongono la richiesta provengono dai messaggi
  già registrati per l'agent run, oppure da un piano di messaggi che una fase
  futura costruirà.
- **`ProviderResult` genera `orchestration_agent_messages`.** La risposta del
  provider viene registrata come messaggio (`message_role='assistant'`, o
  `review`/`tool` secondo il caso) dell'agent run.
- **`ProviderResult` genera `orchestration_agent_outputs`.** Il contenuto
  strutturato consumabile della risposta viene registrato come output
  dell'agent run.
- **`content_text`/`content_hash`.** Il testo del messaggio/output e il suo
  hash; lo schema `0011` prevede `content_text` e `content_hash` su entrambe le
  tabelle.
- **`structured_payload`.** L'eventuale payload strutturato dell'output finisce
  in `orchestration_agent_outputs.structured_payload`.
- **`output_kind`.** La forma dell'output (testo libero, lista di affermazioni,
  formato strutturato), coerente con il `ProviderOutputContract`.
- **`sequence_no`.** L'ordinamento dei messaggi/output dentro l'agent run; lo
  schema impone UNIQUE `(agent_run_id, sequence_no)` su entrambe le tabelle.
- **`role`/`message_role`.** Il ruolo del messaggio, codominio dello schema
  `system`, `user`, `assistant`, `review`, `tool`.

Chiarimenti vincolanti:

- **L'agent output non è una final answer.** È output candidato di un agente.
- **L'agent output non è evidence.** Non è un'evidenza verificata.
- **L'agent output non decide il gate.** Non emette decisioni di pubblicabilità.
- **L'hash serve per audit, non come supporto semantico.** `content_hash` è
  un'impronta; non dice nulla sul fatto che il contenuto sostenga un claim.

## 20. Source candidates mapping

Questa sezione descrive come l'output di un provider può generare
`source_candidates`, e ribadisce l'invariante centrale: una citazione di un
provider non è un'evidenza.

Tipi di source candidate (codominio `candidate_type` dello schema `0011`):

- **`agent_cited`** — fonte proposta o citata da un agente AI; è il caso
  tipico generato dall'output di un provider. Lo schema prevede
  `source_candidates.agent_output_id` come FK nullable verso
  `orchestration_agent_outputs`, popolata per questo tipo.
- **`user_supplied`** — fonte caricata o indicata dall'utente.
- **`system_retrieved`** — fonte recuperata dal sistema.
- **`internal`** — fonte interna/locale.
- **`future_web`** — fonte web, riservata a una capacità non ancora
  disponibile.

Chiarimenti vincolanti:

- **Le citazioni di un provider sono solo source candidates.** Quando un
  provider, nella sua risposta, cita o propone una fonte, quella fonte entra in
  `source_candidates` con `candidate_type='agent_cited'`, **non** come
  evidenza.
- **`source_candidates` non deve avere `evidence_span_id`.** Lo schema `0011`,
  per costruzione, non dà a `source_candidates` la colonna `evidence_span_id`
  né alcuna FK verso `evidence_spans`/`claim_evidence_links`/`logical_claims`.
  L'astrazione provider non deve mai trattare una citazione come se avesse già
  un evidence span.
- **Source resolution/verification sono fasi successive.** Una
  `source_candidate` diventa evidenza solo attraverso `source_resolutions`
  (recupero della fonte reale) e `source_verifications` (verifica di
  presenza/quote/hash), che è l'unico ponte verso `evidence_spans`.
- **Solo una verified source/evidence_span può contribuire alla pipeline
  evidence-gated.** Una fonte ferma a `proposed`, `resolution_failed`,
  `insufficient_metadata` o `rejected` non ha un evidence span e non può
  contribuire a una pubblicazione.
- **Il provider non deve inventare evidenza.** L'astrazione provider raccoglie
  ciò che un agente cita come `source_candidate`; non fabbrica evidence span,
  non aggancia quote a documenti, non emette verdetti di verifica.

## 21. Candidate synthesis mapping

Questa sezione descrive come l'output di provider può, in futuro, alimentare
una `candidate_syntheses`.

- **synthesizer agent futuro** — una `candidate_syntheses` è prodotta da un
  agente con ruolo synthesizer (`agent_configs.synthesizer_flag`) o da un passo
  di synthesis dedicato; in entrambi i casi attraverso l'astrazione provider.
- **`candidate_syntheses`** — la tabella di fatto append-only dello schema
  `0011` che registra la sintesi multi-AI candidata.
- **`synthesis_text`** — il testo della sintesi candidata.
- **`synthesis_text_hash`** — l'hash del testo, per tracciabilità.
- **`status`** — il codominio dello schema: `draft`,
  `ready_for_claim_extraction`, `submitted_to_gate`, `superseded`. Descrive
  l'avanzamento della sintesi, non una decisione di pubblicabilità.
- **source links** — `synthesis_source_links` collega la sintesi agli
  `orchestration_agent_outputs` e/o agli `evidence_spans` verificati usati.
- **claim links futuri** — `synthesis_claim_links` collega la sintesi ai
  `logical_claims` estratti; è il ponte verso il Claim Ledger.

Chiarimenti vincolanti:

- **`candidate_synthesis` non è `published_answers`.** È una risposta candidata;
  lo schema `0011` non ha alcuna FK o percorso che la trasformi in
  `published_answers`.
- **Non salta Claim Extraction.** La sintesi candidata viene scomposta in claim
  verificabili.
- **Non salta Evidence Binding.** I claim estratti vengono collegati a evidence
  span verificati.
- **Non salta il Final Answer Gate.** La sintesi candidata, scomposta e legata,
  attraversa il gate esistente prima di poter diventare pubblicabile.
- **Il provider non decide il publication status.** L'astrazione provider
  produce output candidato; la decisione di pubblicabilità resta del Final
  Answer Gate.

## 22. Worker implications future

Questa sezione descrive i componenti worker che la futura linea di
orchestrazione richiederà attorno all'astrazione provider. È una descrizione di
implicazioni: nessun worker viene modificato o implementato in questa fase.

Worker futuri (descrizione concettuale):

- **provider caller worker** — il componente che invoca l'astrazione provider
  per conto di un `orchestration_agent_runs` e ne raccoglie l'esito.
- **retry scheduler** — il componente che, secondo la `ProviderRetryPolicy`,
  pianifica e conta i tentativi di un'invocazione fallita.
- **token budget checker** — il componente che esegue il preflight budget check
  prima di un'invocazione e il post-result accounting dopo.
- **redaction step** — il passo che applica la `ProviderRedactionPolicy` ai
  payload prima della persistenza e del logging.
- **output normalizer** — il passo che traduce la risposta grezza in una
  `ProviderResult` normalizzata e nei record di `orchestration_agent_*`.
- **source candidate extractor** — il passo che estrae le `source_candidates`
  dall'output di provider.
- **usage recorder** — il passo che appende i `token_usage_records`.
- **orchestration event writer** — il passo che emette gli
  `orchestration_events` man mano che il run avanza.

Chiarimenti vincolanti:

- **`ORCH-PROVIDER-PRE` non modifica alcun worker.** Questa fase è solo design.
- **`ORCH-PROVIDER-A` può implementare solo il service/adapter mock.**
  L'integrazione di un provider caller worker reale può essere parte di
  `ORCH-PROVIDER-A` solo nella forma mock-first; un provider reale è una fase
  successiva.
- **Un provider reale è una fase successiva.** Il provider caller reale, con
  trasporto, autenticazione e rate limit, è lavoro di una fase dedicata.

## 23. API implications future

Questa sezione descrive le API future attorno all'astrazione provider, **solo
a livello concettuale**: nessun endpoint viene implementato.

API future (descrizione concettuale):

- **read endpoint per `provider_invocations`** — un endpoint read-only che
  esponga le invocazioni di provider di un run, per audit e debug.
- **run debug endpoint** — un endpoint che esponga il dettaglio di
  un'esecuzione, comprese le invocazioni e il loro esito.
- **model/provider registry read endpoint** — un endpoint read-only che esponga
  il catalogo dei provider e dei modelli disponibili.
- **admin endpoint futuro per provider config** — un endpoint amministrativo
  per configurare i provider; richiede particolare cautela perché tocca la
  configurazione.

Chiarimenti vincolanti:

- **Nessuna API in questa fase.** `ORCH-PROVIDER-PRE` non aggiunge route.
- **Nessun endpoint deve esporre segreti.** Né un read endpoint né un admin
  endpoint devono esporre API key, Authorization header o secret env value.
- **I read endpoint sono viste derivate.** Espongono fatti già persistiti
  (`provider_invocations`, registro provider) senza ricalcolare nulla.
- **Nessun endpoint deve ricalcolare il gate.** Le API attorno all'astrazione
  provider non emettono decisioni di pubblicabilità; il Final Answer Gate resta
  l'unica autorità.

## 24. Test strategy future

Questa sezione pianifica la strategia di test di `ORCH-PROVIDER-A`. La fase
deve essere **mock-first**: tutti i test girano contro il mock provider, in
modo deterministico e senza rete.

**Test unitari** (su servizio/adapter):

- `MockProviderAdapter deterministic output` — dato lo stesso input, lo stesso
  output.
- `request hashing deterministic` — `compute_request_hash(...)` è
  deterministico.
- `response hashing deterministic` — `compute_response_hash(...)` è
  deterministico.
- `redaction` — la `ProviderRedactionPolicy` rimuove/maschera i campi
  sensibili come atteso.
- `no secret persistence` — nessun campo sensibile finisce in ciò che viene
  conservato.
- `error normalization` — i fallimenti vengono tradotti negli `error_code`
  normalizzati attesi.
- `retry policy` — i retry seguono la `ProviderRetryPolicy` (retry su
  `timeout`/`rate_limited`/`network_error`, no retry su
  `authentication_failed`/`authorization_failed`/`invalid_request`).
- `timeout/rate-limit normalization` — timeout e rate limit producono
  `status='failed'` con `error_code` corretto, non uno status principale
  dedicato.
- `token usage accounting` — l'usage viene riportato e registrato
  correttamente.
- `budget preflight mock` — il preflight budget check funziona con budget mock.
- `provider_invocation mapping` — `ProviderRequest`/`ProviderResult` si mappano
  correttamente sui campi di `provider_invocations`.
- `token_usage_records mapping` — il consumo si mappa correttamente su
  `token_usage_records`.
- `source_candidates marked unverified` — le fonti citate dal provider sono
  `source_candidates` marcate come non verificate, senza `evidence_span_id`.

**Test DB** (sullo schema `0011`, sul modello dei test già esistenti in
`tests/test_orch_schema_constraints.py`):

- `provider_invocations append-only` — UPDATE/DELETE rifiutati dal trigger.
- `token_usage_records append-only` — UPDATE/DELETE rifiutati dal trigger.
- `idempotency` — la UNIQUE su `provider_invocations` e i due indici UNIQUE
  parziali su `token_usage_records` assorbono i redelivery.
- `no secret columns` — `provider_invocations` non ha colonne dal nome
  sospetto (`%api_key%`, `%secret%`, `%credential%`, `%token_auth%`,
  `%password%`).
- `is_mock=true persisted` — le righe mock portano `is_mock=true`.
- `status/error_code consistency` — `status` e `error_code` sono coerenti tra
  loro (per esempio un `error_code='timeout'` su un `status='failed'`).

Regole della strategia di test:

- **no external network calls** — nessun test tocca la rete.
- **no real provider keys** — nessun test usa chiavi di provider reali.
- **no SDK** — nessun test importa un SDK di provider.
- **mock-first** — l'intera strategia è costruita attorno al mock.

## 25. Security and compliance considerations

- **no secrets in DB** — nessuna tabella, in particolare `provider_invocations`,
  contiene credenziali; lo schema `0011` è già progettato così.
- **no secrets in logs** — i log dell'astrazione provider non contengono
  segreti.
- **redaction** — i payload, se conservati, lo sono solo nella forma redatta
  dalla `ProviderRedactionPolicy`.
- **retention** — i payload completi, se conservati in futuro, sono soggetti a
  una retention policy dedicata; non possono crescere indefinitamente.
- **auditability** — ogni invocazione è un fatto auditabile append-only su
  `provider_invocations`, con `request_hash`/`response_hash`, `status`,
  `error_*`, token, latenza, `attempt_no`.
- **least privilege future** — l'accesso alla configurazione dei provider e
  alle credenziali, quando esisteranno, deve seguire il principio del minimo
  privilegio.
- **provider terms/cost risk future** — i provider reali comportano vincoli
  contrattuali e costi; la `ProviderCostPolicy` e il budget enforcement servono
  anche a contenere questo rischio.
- **tenant isolation** — ogni record porta `tenant_id`; l'astrazione provider
  non deve mescolare dati di tenant diversi.
- **project isolation** — ove il progetto è mantenuto come contenitore, i dati
  restano scoped al progetto.
- **deterministic hashes** — `request_hash`/`response_hash` sono deterministici,
  per audit, idempotenza e debug; non implicano verità del contenuto.
- **sensitive payload minimization** — si conserva il minimo necessario;
  l'approccio preferito è hash + payload redatto, non payload grezzo.

## 26. Non-goals

Questa fase, e il documento che produce, hanno i seguenti **non-goals
espliciti**. Nessuno va perseguito in `ORCH-PROVIDER-PRE`:

- **nessun provider reale** — nessun OpenAI, Anthropic, Gemini o altro provider
  esterno introdotto o referenziato in modo operativo;
- **nessuna chiamata esterna** — nessuna chiamata di rete verso terzi;
- **nessun SDK** — nessun SDK di provider aggiunto;
- **nessun local LLM** — nessun modello AI locale introdotto;
- **nessun worker di orchestrazione** — nessun consumer o componente worker
  creato o modificato;
- **nessuna API** — nessuna route HTTP aggiunta o implementata;
- **nessuna UI** — nessuna pagina o componente frontend creato o modificato;
- **nessuna migration** — nessun file in `migrations/` creato o modificato;
- **nessun test** — nessun file di test creato o modificato;
- **nessuna modifica a `.env`** — nessun file di configurazione di runtime
  toccato;
- **nessun secret** — nessuna credenziale introdotta in alcun file;
- **nessun source retrieval reale** — nessun recupero o risoluzione reale di
  fonti;
- **nessun web retrieval** — nessuna capacità di recupero web;
- **nessun gate parallelo** — nessuna seconda autorità di decisione di
  pubblicabilità accanto al Final Answer Gate;
- **nessuna promessa di verità assoluta** — il documento non dichiara che il
  sistema produca risposte "vere";
- **nessuna promessa di eliminazione totale delle allucinazioni** — il
  documento non promette che le allucinazioni siano eliminate.

## 27. Acceptance criteria

Il documento `PHASE_ORCH_PROVIDER_PRE.md` è accettabile se e solo se:

- **crea o modifica solo `PHASE_ORCH_PROVIDER_PRE.md`** — nessun altro file del
  repository è creato o modificato;
- **è in italiano** — l'intero documento è redatto in italiano tecnico;
- **distingue esistente / futuro / fuori scope** — per ogni elemento il
  documento dichiara se è esistente, futuro o fuori scope;
- **non implementa codice** — nessun file di codice è scritto o modificato;
- **non modifica migration** — nessuna migration creata o modificata;
- **non modifica tests** — nessun file di test toccato;
- **non modifica API/worker/UI** — `apps/api/*`, `apps/worker/*`,
  `apps/web/*`, `packages/shared/*` restano invariati;
- **non modifica `README.md`/`PROJECT_STATE.md`** — entrambi restano invariati;
- **non introduce provider reali** — nessun provider esterno introdotto;
- **non introduce local LLM** — nessun modello locale introdotto;
- **non introduce SDK o dipendenze** — nessun manifest o lockfile toccato;
- **non introduce segreti** — nessuna credenziale in alcun file;
- **non promette verità assoluta** — il documento non dichiara risposte "vere";
- **non tratta source candidates come evidence** — una citazione di un provider
  resta una `source_candidate` da risolvere e verificare;
- **non tratta provider output come published answer** — un output di provider
  resta candidato;
- **mantiene il Final Answer Gate come unico gate** — nessun gate parallelo;
- **definisce chiaramente `ORCH-PROVIDER-A` come fase mock-first** — la fase
  successiva parte dall'interfaccia e dal mock provider.

## 28. Comandi di verifica

I comandi seguenti permettono a un revisore di verificare i criteri di
accettazione in modo meccanico. Sono comandi di **sola lettura**: non
modificano il repository. Vanno eseguiti dalla radice del repository.

### 28.1 Comandi base

```bash
git diff --check
git diff --stat
git diff --name-only
git status -sb
```

`git diff --check` non deve segnalare errori di whitespace; `git diff --stat` e
`git diff --name-only` mostrano il perimetro delle modifiche; `git status -sb`
mostra lo stato sintetico del branch.

### 28.2 Controllo file singolo

```bash
git diff --name-only
```

Deve mostrare solo:

```
PHASE_ORCH_PROVIDER_PRE.md
```

Nessun altro file del repository deve comparire.

### 28.3 Controllo wording vietato

Il controllo usa pattern con parentesi quadre sul primo carattere, così che il
comando non intercetti se stesso:

```bash
grep -niE "[t]ruth score|[v]erified true|[v]erified answer|[A]I verified|[f]actually true|[h]allucination eliminated|[h]allucination-free|[g]uaranteed truth|[z]ero hallucinations|[e]ntailed = true|[s]ource quality proves claim|[C]VE-lite proves support|[r]eal NLI|[c]ontradiction detector|[c]itation-to-claim validator" PHASE_ORCH_PROVIDER_PRE.md || true
```

Deve restituire nulla.
