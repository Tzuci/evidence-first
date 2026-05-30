# PHASE ORCH-MULTI-A-PRE

> **Documento di design della futura orchestrazione mock multi-agente.**
> Questo blocco è **solo progettazione**. Non implementa codice di produzione,
> non crea né modifica migration, non modifica `apps/api/*`, `apps/worker/*`,
> `apps/web/*`, `packages/shared/*`, non tocca i test, non modifica il runner
> esistente, non modifica la provider abstraction, non aggiunge dipendenze,
> non aggiunge SDK provider, non aggiunge segreti, non modifica `.env`, non
> modifica `README.md` né `PROJECT_STATE.md` né alcun altro `PHASE_*_PRE.md`
> né alcun `*_IMPLEMENTATION_REPORT.md`. L'unico deliverable è questo file
> `PHASE_ORCH_MULTI_PRE.md`. **Nessun commit è eseguito.**
>
> Lingua: italiano tecnico, registro da System Architect.
>
> **Promemoria di linguaggio (vincolante per tutta la fase).** Il sistema è
> evidence-first ed evidence-gated. Non promette verità assoluta, non promette
> l'eliminazione totale delle allucinazioni, non dichiara che le sue risposte
> siano "vere". La futura `ORCH-MULTI-A` trasforma più configurazioni di agente
> e un `MockProviderAdapter` in un singolo run persistito e auditabile: non
> decide publishability, non verifica fonti, non produce risposte pubblicabili,
> non sostituisce il Final Answer Gate, non integra alcuna sintesi candidata al
> gate in questa fase. Le source candidates restano proposte e non verificate;
> il record tecnico persistito serve audit/debugging e non garantisce la verità
> fattuale.
>
> **Nota di coerenza architetturale (vincolante).** Quando un'entità registra
> un *fatto* (creazione di un run, invocazione di un provider, output di un
> agente, consumo di token, transizione di stato), quel fatto è **append-only**:
> una sua "modifica" è una nuova riga, mai una riscrittura silenziosa. La sola
> eccezione ammessa dallo schema `ORCH-SCHEMA-A` (migration
> `0011_orchestration_schema.sql`) è il campo `orchestration_runs.status`
> materializzato; ogni sua transizione, quando il codominio degli `event_type`
> reali lo prevede, deve generare un evento corrispondente in
> `orchestration_events`. `ORCH-MULTI-A` rispetta integralmente questa
> disciplina.

---

## Indice

1. Titolo e stato fase
2. Baseline attuale
3. Obiettivo di ORCH-MULTI-A
4. Non-goals
5. Proposta di input contract
6. Proposta di output contract
7. Selezione e validazione agenti
8. Transaction model
9. Failure policy
10. Budget policy
11. Idempotenza
12. Event model
13. Source candidates
14. Invariante no gate / no publication
15. Sicurezza e redaction
16. Test richiesti per la futura ORCH-MULTI-A
17. Acceptance criteria
18. Comandi di verifica
19. Fase futura di implementazione
20. Commit message suggerito

---

## 1. Titolo e stato fase

- **Phase:** ORCH-MULTI-A-PRE
- **Type:** design only (nessun codice, nessuna migration, nessun test, nessuna
  API, nessuna UI).
- **Target implementation phase:** ORCH-MULTI-A.

Questa fase progetta, **solo a livello di design**, l'estensione multi-agente
del runner mock. È il blocco `*-PRE` che precede direttamente `ORCH-MULTI-A`
nella roadmap incrementale di `PHASE_PRODUCT_ORCHESTRATION_PRE.md §19.7`. Segue
`ORCH-RUNNER-A` (single-agent mock runner implementato in
`apps/worker/app/services/orchestration_runner.py`), che a sua volta segue
`ORCH-PROVIDER-A` (provider abstraction mock-first) e `ORCH-SCHEMA-A` (schema
applicato come migration `0011`).

Ogni decisione qui presa è una **raccomandazione di design** per `ORCH-MULTI-A`,
non un impegno di implementazione. Nomi e tipi di contratto sono indicativi e
soggetti a revisione nella fase di codice. Il documento non introduce
comportamenti che non siano già presenti, in forma estendibile, nei file di
riferimento (`PHASE_ORCH_RUNNER_PRE.md`, `ORCH_RUNNER_A_IMPLEMENTATION_REPORT.md`,
`orchestration_runner.py`, `test_orchestration_runner_service.py`,
`0011_orchestration_schema.sql`, `orchestration_provider.py`,
`test_orchestration_provider_service.py`,
`ORCH_PROVIDER_A_IMPLEMENTATION_REPORT.md`,
`ORCH_SCHEMA_A_IMPLEMENTATION_REPORT.md`,
`PHASE_PRODUCT_ORCHESTRATION_PRE.md`).

---

## 2. Baseline attuale

`ORCH-RUNNER-A` è la baseline da cui `ORCH-MULTI-A` parte. In sintesi, il runner
attuale (`run_single_agent_mock_orchestration(conn, request)`):

- **single-agent mock runner.** Esegue **un** run con **un** solo
  `orchestration_agent_runs`, single-pass (`pass_kind='independent_answer'`),
  mock-only (`provider='mock'`, `model='mock-model'`), componendo il
  `MockProviderAdapter` e le pure functions di mapping di `ORCH-PROVIDER-A`.
- **fatti persistiti.** Persiste end-to-end e in modo auditabile, nell'ordine
  imposto dalle FK e dalla disciplina insert-once / final-status:
  `orchestration_runs`, `orchestration_events`, `agent_config_snapshots`,
  `orchestration_agent_runs` (scritta **una sola volta** con lo status finale),
  `orchestration_agent_messages`, `provider_invocations`, `token_usage_records`,
  `orchestration_agent_outputs` (solo su success) e `source_candidates`
  opzionali (solo su success e solo se l'output esiste).
- **idempotenza.** È idempotente sotto redelivery, ancorata alla UNIQUE
  `orchestration_runs_idempotency_uq (tenant_id, idempotency_key)`. Un secondo
  delivery dello stesso input ritorna il run esistente ricostruendone i fatti
  già persistiti, senza duplicare righe o eventi.
- **nessun gate.** Non integra il Final Answer Gate, non esegue Claim
  Extraction, Evidence Binding, synthesis pass.
- **nessuna pubblicazione.** Non scrive `final_gate_reports` né
  `published_answers`; `orchestration_runs.final_gate_report_id` resta NULL;
  l'output logico porta `publication_status='not_evaluated'` e
  `gate_report_id=None`.
- **source candidates solo proposte / non verificate.** Le candidate persistite
  hanno `status='proposed'`, `candidate_type='agent_cited'`,
  `master_prompt_id=None`, e una `provenance` che dichiara `is_verified=False`.
  Nessun `evidence_span_id`, nessun claim link, nessuna `source_resolutions`,
  nessuna `source_verifications`.

`ORCH-RUNNER-A` resta **single-agent, single-pass, attempt_no=1**, senza retry,
senza multi-agent, senza reviewer/critic/synthesizer. La sua relazione finale
(`ORCH_RUNNER_A_IMPLEMENTATION_REPORT.md §14`) elenca esplicitamente
`ORCH-MULTI-A` come l'estensione a più agenti per run.

---

## 3. Obiettivo di ORCH-MULTI-A

`ORCH-MULTI-A` estende `ORCH-RUNNER-A` da:

```
un run  →  un agente  →  una invocazione mock
```

a:

```
un run  →  più agent_config  →  una invocazione mock per agente
        →  persistenza di agent runs, messaggi, provider invocations,
           token usage, output, source candidates per ciascun agente
        →  completamento deterministico e bounded
```

Gli obiettivi vincolanti di `ORCH-MULTI-A` sono:

- **eseguire più agenti mock dentro un solo `orchestration_run`.** Ogni agente
  richiesto produce, in ordine deterministico, una propria invocazione mock e i
  propri fatti append-only, tutti scoped allo stesso `orchestration_runs`.
- **preservare audit append-only.** Ogni fatto (agent run, messaggio,
  invocazione, usage, output, source candidate, evento) resta append-only; la
  sola eccezione operativa resta `orchestration_runs.status` materializzato,
  con eventi adiacenti che ne tracciano le transizioni.
- **esecuzione deterministica e bounded.** Il run termina in un numero finito e
  predeterminato di passi (un passo `independent_answer` per agente), senza loop
  fra agenti, senza chat infinita, senza auto-recursion. Dove il
  `MockProviderAdapter` è deterministico, il run è riproducibile.
- **nessun provider reale.** L'unico provider operativo resta il mock
  (`provider='mock'`, `model='mock-model'`); nessuna rete, nessun SDK, nessun
  local LLM.
- **nessuna sintesi candidata in questa fase.** `ORCH-MULTI-A` esegue gli agenti
  in modalità *independent parallel answers* (`PHASE_PRODUCT_ORCHESTRATION_PRE.md
  §12.1`); non confronta gli output, non produce `candidate_syntheses`, non
  esegue reviewer/critic/synthesis pass. La sintesi è materia di
  `ORCH-SYNTHESIS-A`.

Chiarimenti vincolanti, ereditati dalla baseline:

- `orchestration_agent_runs.status='succeeded'` per un agente non implica che il
  suo output sostenga un claim, né che sia corretto, né che sia pubblicabile.
- `orchestration_runs.status='completed'` significa solo che tutti gli agenti
  richiesti hanno terminato senza errore di runner/provider; non implica
  pubblicazione consentita.
- `request_hash` / `response_hash` servono audit, debug e idempotenza; non
  provano il contenuto.
- Il token usage prodotto dal mock non è un costo reale; è marcato
  `is_mock=true`.

---

## 4. Non-goals

`ORCH-MULTI-A` **non** implementa, e questa fase di design non li progetta come
parte dello scope di `ORCH-MULTI-A`:

- **nessun provider reale** — nessun OpenAI, Anthropic, Gemini o altro provider
  esterno introdotto o referenziato in modo operativo;
- **nessuna rete** — nessuna chiamata di rete, nessun socket, nessun client HTTP;
- **nessun Redis** — nessun consumer, nessuna coda, nessun client Redis;
- **nessuna API** — nessuna route HTTP nuova o modificata;
- **nessuna UI** — nessuna pagina o componente frontend;
- **nessuna migration** — nessun file in `migrations/`, nessuna modifica a
  `0001`-`0011`, nessuna modifica di schema;
- **nessuna `CandidateSynthesis`** — nessuna sintesi multi-AI, nessuna riga in
  `candidate_syntheses`;
- **nessuna `SourceResolution`** — nessun recupero/risoluzione reale di fonti,
  nessuna riga in `source_resolutions`;
- **nessuna `SourceVerification`** — nessuna verifica reale di fonti, nessuna
  riga in `source_verifications`;
- **nessun Final Answer Gate** — nessuna integrazione, nessuna riga in
  `final_gate_reports`;
- **nessuna decisione di pubblicazione** — nessuna riga in `published_answers`;
  `publication_status` resta `not_evaluated`;
- **nessun loop multi-turn tra agenti** — nessuno scambio di messaggi fra
  agenti, nessun confronto, nessun dibattito, nessuna ricorsione; ogni agente
  esegue un solo pass `independent_answer`.

Ulteriori esclusioni coerenti con lo scope: nessuna claim extraction, nessun
evidence binding, nessun reviewer/critic/synthesis pass, nessun retry, nessuna
async job queue, nessun local LLM.

---

## 5. Proposta di input contract

`ORCH-MULTI-A` riceve (o costruisce internamente) una richiesta logica di run
multi-agente. È un **contratto concettuale**, non un'API definitiva: nomi e tipi
sono indicativi e soggetti a revisione in `ORCH-MULTI-A`. Estende
`OrchestrationRunnerRequest` di `ORCH-RUNNER-A` sostituendo il singolo
`agent_config_id` con una tupla ordinata di `agent_config_ids`.

```
MultiAgentMockOrchestrationRequest:
  - tenant_id                       # tenant del run; obbligatorio.
  - project_id                      # progetto; nullable.
  - master_prompt_version_id        # snapshot immutabile del prompt; FK su
                                     #   master_prompt_versions; obbligatorio.
  - agent_config_ids: tuple[str, ...]  # >= 1 agent_config; ordine = ordine di
                                     #   esecuzione deterministico (vedi §7).
  - idempotency_key                 # chiave opaca di idempotenza del run;
                                     #   obbligatoria (vedi §11).
  - mode                            # codominio orchestration_runs.mode:
                                     #   multi_ai_orchestration / local_evidence /
                                     #   hybrid; tipicamente
                                     #   multi_ai_orchestration.
  - execution_mode                  # codominio orchestration_runs.execution_mode;
                                     #   'independent' in ORCH-MULTI-A.
  - token_budget_id                 # riferimento opzionale a token_budgets per
                                     #   il preflight di budget globale; nullable.
  - mock_source_candidates_by_agent # mappa opzionale agent_config_id ->
                                     #   lista di dict {title, url, locator,
                                     #   raw_text}, per ergonomia di test;
                                     #   esercita l'estrazione candidate del mock
                                     #   per il singolo agente.
  - mock_error_by_agent             # mappa opzionale agent_config_id ->
                                     #   {error_code, error_message}, per
                                     #   ergonomia di test; esercita l'error
                                     #   injection del mock per il singolo agente.
  - created_by                      # attore che avvia il run; opzionale.
```

Chiarimenti vincolanti:

- **nessun secret nell'input.** Il contratto non porta API key, token di
  autenticazione, credenziali, Authorization header, password. Coerente con
  `provider_invocations` (0011, nessuna colonna di credenziali) e con
  `ORCH-PROVIDER-A`.
- **provider/model mock.** Tutti gli agenti devono risolvere a `provider='mock'`
  e `model='mock-model'` (vedi §7). Qualsiasi altro valore è rifiutato.
- **alias per-agente.** `mock_source_candidates_by_agent` e `mock_error_by_agent`
  sono alias di test per i campi `source_policy["mock_source_candidates"]` e
  `constraints["mock_error_code"]` / `constraints["mock_error_message"]` della
  `ProviderRequest` per-agente; restano opzionali e per-agente, così da poter
  esercitare success e failure indipendenti nello stesso run.
- **input idempotente.** Lo stesso `tenant_id` + `idempotency_key`, ripresentato,
  deve produrre lo stesso esito logico senza duplicare righe di fatto (vedi §11).

Questo resta a livello design: non va scritto codice operativo in
`ORCH-MULTI-A-PRE`.

---

## 6. Proposta di output contract

`ORCH-MULTI-A` restituisce (in memoria, in un eventuale wrapper sincrono) un
output logico che descrive lo stato finale del run e gli id dei fatti
persistiti, aggregati su tutti gli agenti. Estende
`OrchestrationRunnerResult` di `ORCH-RUNNER-A` rendendo plurali i riferimenti
agli agenti.

```
MultiAgentMockOrchestrationResult:
  - status                       # esito sintetico del runner: 'succeeded' /
                                 #   'failed' (vedi §9). Per un replay rispecchia
                                 #   lo stato del run esistente.
  - orchestration_run_id         # id del run creato o riconciliato; None se
                                 #   il run è fallito prima della creazione.
  - agent_run_ids                # tuple degli id orchestration_agent_runs
                                 #   creati, in ordine deterministico di
                                 #   richiesta.
  - provider_invocation_ids      # tuple degli id provider_invocations creati.
  - agent_output_ids             # tuple degli id orchestration_agent_outputs
                                 #   creati (solo per gli agenti success).
  - token_usage_record_ids       # tuple degli id token_usage_records creati.
  - source_candidate_ids         # tuple degli id source_candidates creati
                                 #   (solo per gli agenti success con output).
  - event_ids                    # tuple degli id orchestration_events creati.
  - failed_agent_config_ids      # tuple degli agent_config_id i cui agenti
                                 #   sono falliti (vuota se tutti success).
  - error_code                   # codice di errore normalizzato del runner, dal
                                 #   codominio di ORCH-PROVIDER-A; None se
                                 #   status='succeeded'.
  - error_message                # messaggio redatto e bounded; None se success.
  - is_mock                      # sempre True in ORCH-MULTI-A.
  - publication_status           # sempre 'not_evaluated': il gate non è
                                 #   integrato (vedi §14).
  - gate_report_id               # sempre None: nessun final_gate_reports è
                                 #   prodotto da questa linea.
```

Chiarimenti vincolanti:

- **`publication_status='not_evaluated'` perché il gate non è integrato.** Il
  runner multi-agente non valuta publishability; `final_gate_report_id` su
  `orchestration_runs` resta NULL.
- **`status='succeeded'`** significa solo che tutti gli agenti richiesti hanno
  completato il mock run senza errore; non implica supporto semantico né
  pubblicabilità.
- **`status='failed'`** significa che almeno un agente richiesto è fallito (error
  injection, malformed result) oppure che un controllo a monte ha fallito
  (budget globale, validazione). Non significa che un claim sia falso nel mondo.
- **ordine deterministico degli id.** Le tuple `agent_run_ids`,
  `provider_invocation_ids`, ecc. seguono l'ordine deterministico di esecuzione
  (ordine della richiesta, vedi §7), così l'output è ispezionabile in modo
  riproducibile.

Questo resta a livello design.

---

## 7. Selezione e validazione agenti

`ORCH-MULTI-A` deve validare l'insieme di agent_config prima di toccare il DB per
la creazione del run, estendendo le validazioni single-agent di
`ORCH-RUNNER-A §7`. Regole:

- **minimo 1 agent_config.** `agent_config_ids` non vuota; una lista vuota è
  rifiutata con `invalid_request` prima di qualunque scrittura.
- **stesso tenant.** Tutti gli `agent_config` devono condividere lo stesso
  `tenant_id`, e questo deve coincidere con `request.tenant_id`. Un mismatch è
  rifiutato con `invalid_request`.
- **stesso master_prompt.** Tutti gli `agent_config` devono riferirsi allo stesso
  `master_prompt_id`, e quel `master_prompt_id` deve coincidere con
  `master_prompt_versions.master_prompt_id` del `master_prompt_version_id`
  richiesto. Un mismatch è rifiutato con `invalid_request`.
- **provider/model mock.** Per ogni `agent_config`, `provider` deve essere
  `mock` (altrimenti `invalid_request`) e `model` deve essere `mock-model`
  (altrimenti `invalid_model`). Stesso codominio di `ORCH-RUNNER-A`.
- **nessun duplicato.** `agent_config_ids` duplicati nella richiesta sono
  rifiutati con `invalid_request`: un agente non viene eseguito due volte nello
  stesso run. La UNIQUE
  `agent_config_snapshots_run_config_uq (orchestration_run_id, agent_config_id)`
  di `0011` rende strutturalmente impossibile due snapshot dello stesso
  `agent_config` nello stesso run; la validazione applicativa rifiuta il
  duplicato in anticipo per dare un errore chiaro.
- **massimo bounded.** Un limite superiore costante, per esempio
  `MAX_AGENTS = 8` come costante implementativa futura, limita il numero di
  agenti per run; superarlo è rifiutato con `invalid_request`. Il valore esatto
  è decisione di `ORCH-MULTI-A`; ciò che è vincolante è che il run sia bounded.
- **ordine deterministico.** L'ordine di esecuzione è **l'ordine della
  richiesta** (`agent_config_ids`), non l'ordine del DB. Il preload e la
  validazione possono leggere gli `agent_config` con una query unica, ma
  l'esecuzione e la materializzazione dei fatti seguono l'ordine posizionale
  della tupla di input. Questo rende `agent_run_ids`, gli `sequence_no` degli
  eventi e gli id aggregati deterministici e riproducibili.

La validazione legge `agent_configs`, `master_prompt_versions` e
`agent_role_prompts` per ogni agente, e (se valorizzato) `token_budgets`,
esattamente come in `ORCH-RUNNER-A` ma per N agenti. Ogni fallimento di
validazione avviene **prima** della creazione del run e ritorna failed con
`orchestration_run_id=None`, senza creare alcuna riga.

---

## 8. Transaction model

`ORCH-MULTI-A` segue una **singola transaction per run**, posseduta dal
chiamante, estendendo la sequenza ordinata di `ORCH-RUNNER-A §17.1` da uno a N
agenti. La transazione deve preservare:

- **Connection posseduta dal chiamante.** La signature è
  `run_multi_agent_mock_orchestration(conn, request)`; il runner **non** chiama
  `conn.commit()` né `conn.rollback()`: il chiamante (test o futuro consumer)
  possiede la transazione.
- **nessun commit/rollback nel runner.** Coerente con `ORCH-RUNNER-A` e con
  `PHASE_ORCH_RUNNER_PRE.md §17.5`.
- **nessun ORM.** Solo `sqlalchemy.text` e una `Connection`.
- **fatti append-only.** Ogni riga di fatto è scritta una sola volta;
  `orchestration_agent_runs` di ciascun agente porta lo status finale, senza
  riga intermedia `running` e senza UPDATE successivo (lo schema lo rifiuterebbe
  via `reject_modify_append_only()`).
- **`orchestration_runs.status` materializzato via UPDATE, transizioni
  rappresentate da eventi.** La transizione `pending → running` e quella finale
  `running → completed/failed` sono UPDATE materializzati; le transizioni con un
  `event_type` dedicato nel codominio sono tracciate dagli eventi adiacenti
  (`run_created`, `agent_run_started`, `agent_run_completed`/`agent_run_failed`,
  `run_failed`).

### 8.1 Sequenza transazionale (vincolante per `ORCH-MULTI-A`)

```
BEGIN  (transazione posseduta dal chiamante)

  # 1) validare la request (forma + selezione/validazione agenti, §5, §7).
  #    Un fallimento qui ritorna failed senza alcuna scrittura DB.

  # 2) controllare idempotency replay tramite (tenant_id, idempotency_key).
  #    Se il run esiste già: ricostruire e ritornare i fatti persistiti, senza
  #    scrivere nulla (vedi §11).

  # 3) pre-caricare e validare TUTTI gli agent_config (un'unica lettura),
  #    risolvendo per ciascuno master_prompt_version, agent_role_prompt e
  #    l'eventuale token_budget; preallocare in memoria run_id e, per ogni
  #    agente, un agent_run_id e uno snapshot_id (uuid.uuid4()).

  # 4) INSERT orchestration_runs (status='pending', is_mock=TRUE, ...).

  # 5) INSERT orchestration_events (event_type='run_created', sequence_no=0,
  #    idempotency_key=<run_idem>:run_created).

  # 6) UPDATE orchestration_runs SET status='running', started_at=NOW()
  #    (transizione materializzata; nessun event_type dedicato).

  # 7) per ogni agente in ordine deterministico:
  #       INSERT agent_config_snapshots (uno per agent_config; UNIQUE
  #              (orchestration_run_id, agent_config_id)).

  # 8) applicare budget preflight policy (globale, §10) PRIMA di invocare
  #    qualunque agente. Se il budget globale è superato: nessun agent run,
  #    eventi token_budget_exceeded + run_failed, status='failed', return.

  # 9) per ogni agente in ordine deterministico (indice i da 0):
  #       - usare l'agent_run_id preallocato per l'agente i;
  #       - INSERT orchestration_events (event_type='agent_run_started',
  #              sequence_no=<crescente>, related_entity_type=
  #              'orchestration_agent_run', related_entity_id=<agent_run_id_i>,
  #              idempotency_key=<run_idem>:agent_run_started:<i>);
  #       - costruire la ProviderRequest per l'agente i, con
  #              orchestration_agent_run_id=<agent_run_id_i> e
  #              agent_config_snapshot_id=<snapshot_id_i>, così request_hash è
  #              già consistente con la riga di fatto;
  #       - invocare MockProviderAdapter.invoke IN MEMORIA (deterministico,
  #              senza rete);
  #       - INSERT orchestration_agent_runs UNA SOLA VOLTA con lo status finale
  #              (succeeded/failed), attempt_no=1, started_at/completed_at
  #              valorizzati, error_code/failure_reason redatti su failed;
  #       - INSERT orchestration_agent_messages (system seq0, user seq1,
  #              assistant seq2 solo su success); sequence_no è per-agent_run
  #              (UNIQUE (agent_run_id, sequence_no));
  #       - INSERT provider_invocations (sempre, anche su failure; FK NOT NULL
  #              verso l'agent_run appena inserito);
  #       - INSERT token_usage_records se il provider è stato invocato (success
  #              o provider failure), provider_invocation_id valorizzato,
  #              pass_kind='independent_answer';
  #       - INSERT orchestration_agent_outputs SOLO su success (seq0,
  #              output_kind='mock_candidate_text');
  #       - INSERT source_candidates SOLO su success e se l'output esiste, con
  #              agent_output_id valorizzato; per ciascuna candidate INSERT
  #              orchestration_events (event_type='source_candidate_created',
  #              idempotency_key=<run_idem>:source_candidate:<i>:<j>);
  #       - INSERT orchestration_events (event_type='agent_run_completed' su
  #              success, oppure 'agent_run_failed' su failure),
  #              idempotency_key=<run_idem>:agent_run_completed:<i> /
  #              :agent_run_failed:<i>.

  # 10) status terminale del run:
  #        - 'completed' se TUTTI gli agenti richiesti hanno avuto successo;
  #        - 'failed' se ALMENO UN agente richiesto è fallito (§9).

  # 11) INSERT orchestration_events (event_type='run_failed') se terminal failed,
  #        idempotency_key=<run_idem>:run_failed.

  # 12) UPDATE orchestration_runs SET status=<terminale>, completed_at=NOW(),
  #        failure_reason=<redacted se failed>.

COMMIT  (gestito dal chiamante)
```

### 8.2 Note di ordinamento

- **FK rispettato per ogni agente.** `orchestration_agent_messages`,
  `provider_invocations`, `orchestration_agent_outputs` e
  `token_usage_records.agent_run_id` hanno FK verso
  `orchestration_agent_runs(id)`: vengono scritte **dopo** l'`INSERT` della riga
  agent_run del rispettivo agente. L'evento `agent_run_started` precede la riga
  di fatto referenziandone via `related_entity_id` l'UUID preallocato
  (`related_entity_id` non è una FK in `0011`).
- **sequence_no monotòno a livello run.** Gli `orchestration_events` di tutti gli
  agenti condividono lo spazio `sequence_no` del run, crescente e contiguo da 0,
  enforced da `orchestration_events_run_sequence_uq`. Un contatore unico per il
  run produce l'ordinamento; gli agenti, eseguiti in sequenza deterministica,
  interlacciano i propri eventi in ordine.
- **JSONB e cost.** Coerentemente con `ORCH-RUNNER-A`, i JSONB
  (`bounding_parameters`, `event_payload`, `snapshot_payload`,
  `structured_payload`, `provenance`, `raw_citation_payload`) sono inseriti via
  `json.dumps(...)` con `CAST(:p AS JSONB)`, e `cost_estimate` è convertito con
  `float(...)` prima dell'INSERT (colonna `DOUBLE PRECISION`).
- **savepoint opzionale per la persistenza candidate.** Coerentemente con
  `ORCH-RUNNER-A §17.5`, un `SAVEPOINT` può proteggere lo step opzionale di
  persistenza delle `source_candidates` di un agente, senza rollback dell'intero
  run.

---

## 9. Failure policy

Policy MVP per `ORCH-MULTI-A`:

- **terminal run status all-or-nothing.** Il run usa uno status terminale
  all-or-nothing: `completed` se tutti gli agenti richiesti riescono, `failed`
  se almeno uno fallisce.
- **qualsiasi agent failure rende il run failed.** Non esiste, in
  `ORCH-MULTI-A`, un esito "parziale" di run: anche un solo agente fallito porta
  `orchestration_runs.status='failed'`. Il partial-success degradato (sintetizzare
  solo dagli output riusciti) è materia di fasi successive
  (`PHASE_PRODUCT_ORCHESTRATION_PRE.md §16`), fuori scope qui.
- **i fatti degli agenti riusciti restano persistiti.** Un agente che ha avuto
  successo conserva la propria riga `orchestration_agent_runs` `succeeded`, i
  messaggi, la `provider_invocations` `succeeded`, il `token_usage_records`,
  l'`orchestration_agent_outputs` e le eventuali `source_candidates`: sono fatti
  append-only e non vengono cancellati perché un altro agente è fallito.
- **i fatti degli agenti falliti restano persistiti.** Un agente fallito
  conserva la propria riga `orchestration_agent_runs` `failed` con `error_code`
  e `failure_reason` redatti, i messaggi `system`/`user`, e la
  `provider_invocations` `failed`.
- **nessun output per agenti falliti.** Un agente fallito non produce
  `orchestration_agent_outputs` né `source_candidates`, e non emette il messaggio
  `assistant`.
- **provider failure registra comunque token_usage_records.** Se l'invocazione
  del provider è avvenuta e ha restituito un risultato `failed`, il consumo mock
  del tentativo è registrato in `token_usage_records` (il provider è stato
  invocato), con `provider_invocation_id` valorizzato, `tokens_output=0`,
  `is_mock=True`. Coerente con `ORCH-RUNNER-A §8`.
- **budget exceeded prima dell'invocazione non registra usage.** Se il preflight
  di budget globale fallisce prima di invocare gli agenti, non si scrive alcuna
  `provider_invocations` né alcun `token_usage_records` per quel path (il
  provider non è mai invocato).
- **nessun retry in `ORCH-MULTI-A`.** `attempt_no=1` per ogni agente. Un retry
  futuro sarà una nuova riga `orchestration_agent_runs` con `attempt_no`
  incrementato, mai un update; è rinviato a una fase successiva.

Principi trasversali ereditati: l'`agent_run_failed` è appeso solo se la riga
`orchestration_agent_runs` con status `failed` per quell'agente è stata
inserita (cioè se lo start logico dell'agente era stato attivato); il
`run_failed` è appeso una sola volta a fallimento del run; nessun
`published_answers`, nessun `final_gate_reports` in alcun branch.

---

## 10. Budget policy

- **global run token budget opzionale.** `ORCH-MULTI-A` riconosce un budget di
  token globale del run, opzionale, riferito da `token_budget_id` nell'input. Il
  limite è letto dalla riga `token_budgets` e può essere fissato immutabilmente
  per il run copiandone i valori (`token_limit`, `overflow_policy`) in
  `bounding_parameters` di `orchestration_runs` e/o negli snapshot, coerentemente
  con il principio "snapshot immutabile al run start".
- **preflight può stimare l'input totale prima di invocare qualsiasi agente.**
  Il preflight di budget globale stima il totale degli input token sommando la
  stima mock (`count_request_input_tokens` di `ORCH-PROVIDER-A`) delle
  `ProviderRequest` di tutti gli agenti richiesti, prima di invocare il primo
  agente. La stima è deterministica e documentata.
- **se global budget exceeded: nessun agent run.** Se il totale stimato supera
  il budget globale, `ORCH-MULTI-A` non invoca alcun agente: appende
  `token_budget_exceeded` e `run_failed`, porta `orchestration_runs.status` a
  `failed`, e non scrive `orchestration_agent_runs`, `provider_invocations` né
  `token_usage_records`. `budget_exceeded` è non-retryable.
- **per-agent budget rinviato, salvo supporto già disponibile.** Un budget
  per-agente può essere rinviato a una fase successiva. Tuttavia lo schema `0011`
  consente già `token_budgets.agent_config_id` con il CHECK condizionale
  `tb_level_target` (`per_agent ⇒ agent_config_id NOT NULL`): se un budget
  per-agente compatibile è già fornito, `ORCH-MULTI-A` può applicarlo come
  preflight per il singolo agente prima della sua invocazione, registrando
  `token_budget_exceeded` per quell'agente e fallendo l'agente; resta una
  decisione di `ORCH-MULTI-A` se esercitarlo o rinviarlo, purché documentata.
- **nessun cost budget reale col mock.** Il `cost_estimate` del
  `MockProviderAdapter` è `Decimal("0")`; il budget di costo (`cost_limit`) può
  esistere come configurazione ma non è attivamente esercitato in `ORCH-MULTI-A`.
- **mock cost non è costo reale.** Ogni `token_usage_records` e ogni
  `provider_invocations` portano `is_mock=True`; il consumo mock non è un costo
  reale e un consumer/UI/report deve dichiararlo onestamente.

Comportamento di `overflow_policy`: come in `ORCH-RUNNER-A`, `hard_stop` è il
default raccomandato (il preflight rifiuta l'invocazione); il comportamento
`warn` differenziato può essere trattato come `hard_stop` per semplicità,
documentandolo.

---

## 11. Idempotenza

- **`orchestration_runs` UNIQUE (tenant_id, idempotency_key) resta la radice.**
  L'idempotenza del run è ancorata a
  `orchestration_runs_idempotency_uq (tenant_id, idempotency_key)`. Un secondo
  delivery dello stesso `tenant_id + idempotency_key` ritorna il run esistente,
  non ne crea un secondo.
- **replay ritorna i fatti già persistiti.** Su replay, `ORCH-MULTI-A`
  ricostruisce il risultato ri-interrogando i fatti già persistiti per il run
  (tutti gli agent_run in ordine, provider invocations, output, messaggi, usage,
  source candidates, eventi), senza scrivere nulla. La mappatura di status segue
  `ORCH-RUNNER-A`: `completed → 'succeeded'`, `failed → 'failed'`,
  `pending`/`running` esposti grezzi (replay in-flight, scelta documentata).
- **provider_invocation idempotency per agent_run attempt.** Enforced da
  `provider_invocations_attempt_idem_uq (agent_run_id, attempt_no,
  idempotency_key)`. Poiché ogni agente ha il proprio `agent_run_id`, la chiave
  resta distinta per agente; un doppio delivery non duplica l'invocazione di un
  agente.
- **token usage idempotency con provider_invocation_id valorizzato.** Quando
  `provider_invocation_id` è valorizzato (il caso di `ORCH-MULTI-A`), l'indice
  parziale `token_usage_records_provider_idem_uq (orchestration_run_id,
  provider_invocation_id, idempotency_key) WHERE provider_invocation_id IS NOT
  NULL` enforced l'idempotenza. Poiché più agenti dello stesso run condividono
  la stessa `idempotency_key` di run ma hanno `provider_invocation_id` distinti,
  la terna resta unica per agente.
- **event idempotency keys devono includere event type e agent index/config id.**
  La UNIQUE `orchestration_events_run_type_idem_uq (orchestration_run_id,
  event_type, idempotency_key)` impone che più eventi dello stesso `event_type`
  nello stesso run abbiano `idempotency_key` distinti. In `ORCH-MULTI-A` gli
  eventi per-agente (`agent_run_started`, `agent_run_completed`,
  `agent_run_failed`) si ripetono per ogni agente con lo stesso `event_type`:
  la loro chiave **deve** includere un discriminante per agente, per esempio
  `<run_idem>:agent_run_started:<agent_index>` oppure
  `<run_idem>:agent_run_started:<agent_config_id>`. Gli eventi che si scrivono
  una sola volta per run (`run_created`, `run_failed`, `token_budget_exceeded`)
  usano il suffisso stabile `<run_idem>:<event_type>`.
- **source candidate event idempotency deve includere agente e candidate index.**
  Per gli eventi `source_candidate_created`, che si ripetono per ogni candidate
  di ogni agente, la chiave deve includere sia l'agente sia l'indice della
  candidate, per esempio `<run_idem>:source_candidate:<agent_index>:<candidate_index>`
  (oppure un hash della candidate). Così per N agenti × M candidate il runner
  produce chiavi tutte distinte e il replay le ricalcola identiche, deduplicando
  via UNIQUE.

Retry vs idempotenza: come in `ORCH-RUNNER-A`, un retry intenzionale futuro
crea un nuovo `orchestration_agent_runs` con `attempt_no` incrementato, mai un
update; in `ORCH-MULTI-A` (`attempt_no=1`) il retry non è esercitato. Nessun
update silenzioso su righe append-only; lo schema lo impedisce comunque via
trigger.

---

## 12. Event model

`ORCH-MULTI-A` usa **solo** `event_type` già ammessi dal codominio chiuso di
`orchestration_events_event_type_chk` (migration `0011`). Gli event_type
effettivamente emessi:

- `run_created` — una volta per run, dopo l'`INSERT orchestration_runs`.
- `agent_run_started` — una volta per agente, prima della sua invocazione mock;
  referenzia l'`agent_run_id` preallocato via `related_entity_id`.
- `agent_run_completed` — una volta per agente riuscito.
- `agent_run_failed` — una volta per agente fallito (se la riga agent_run
  `failed` è stata inserita).
- `source_candidate_created` — una volta per candidate persistita, per agente.
- `token_budget_exceeded` — una volta, se il preflight di budget globale
  fallisce.
- `run_failed` — una volta, se lo status terminale del run è `failed`.

Esplicitamente, `ORCH-MULTI-A` **NON usa event type inventati** e non presenti
nel codominio `0011`:

- `run_started` — la transizione `pending → running` è solo
  `orchestration_runs.status` materializzato.
- `run_completed` — lo stato terminale `completed` è solo
  `orchestration_runs.status` materializzato.
- `provider_invocation_started` — il fatto è la riga `provider_invocations`.
- `provider_invocation_completed` — il fatto è la riga `provider_invocations`
  con lo `status` finale.

Eventi del codominio `0011` non usati da `ORCH-MULTI-A`:
`source_resolution_started`, `source_resolution_completed`,
`source_verification_completed` (nessun source retrieval/verification reale);
`synthesis_created`, `submitted_to_gate`, `gate_completed` (nessuna synthesis né
gate integration); `run_cancelled` (cancellazione attiva rinviata).

Invariante: `sequence_no` crescente e contiguo da 0 a livello run, enforced da
`orchestration_events_run_sequence_uq`. Il runner non inventa event_type che lo
schema non consente; quando una transizione non ha un `event_type` dedicato, si
appoggia all'event_type adiacente reale e ai record append-only di fatto come
traccia di audit.

---

## 13. Source candidates

- **source candidates restano proposed / unverified.** Ogni candidate persistita
  da `ORCH-MULTI-A` ha `status='proposed'` e una `provenance` che dichiara
  `is_verified=False`, come in `ORCH-RUNNER-A`. Mai uno status diverso da
  `proposed` in questa fase.
- **source candidate non è evidence.** Una candidate proposta da un agente è una
  risposta candidata non verificata, non un'evidenza; non può contribuire al
  gate per costruzione dello schema.
- **nessun evidence_span_id.** `source_candidates` non ha la colonna
  `evidence_span_id` (vedi `ORCH-SCHEMA-A`); il ponte verso `evidence_spans`
  passa esclusivamente per `source_verifications`, che `ORCH-MULTI-A` non scrive.
- **nessuna `source_resolutions`.** Nessun recupero/risoluzione reale di fonti.
- **nessuna `source_verifications`.** Nessuna verifica reale di fonti.
- **nessun claim link.** Il dict prodotto da `source_candidates_to_records` non
  contiene `claim_id`, `logical_claim_id`, `claim_evidence_link_id`,
  `claim_ledger_entry_id`. Una source candidate non partecipa al Claim Ledger
  finché non è risolta e verificata in una fase futura.
- **raggruppamento per agent output.** Le source candidates di `ORCH-MULTI-A`
  sono raggruppate per agente: ogni candidate è scritta con `agent_output_id`
  valorizzato all'output dell'agente che l'ha proposta, così la provenienza
  per-agente resta tracciabile. Ogni agente success con output può quindi
  produrre il proprio gruppo di candidate.
- **provenance deve includere mock / is_verified false.** La `provenance` di ogni
  candidate include esplicitamente `mock=True`, un semantic warning che dichiara
  la candidate non verificata e non evidence, `provider_name`, `model`,
  l'eventuale `locator`, e `is_verified=False`. Coerente con
  `source_candidates_to_records` di `ORCH-PROVIDER-A`.

---

## 14. Invariante no gate / no publication

`ORCH-MULTI-A` **non integra il Final Answer Gate** e non valuta la
pubblicabilità. Invarianti espliciti:

- **`final_gate_report_id` NULL.** La colonna
  `orchestration_runs.final_gate_report_id` (FK nullable verso
  `final_gate_reports`) resta NULL per ogni run prodotto da `ORCH-MULTI-A`.
- **`publication_status` not_evaluated.** Nell'output logico del runner,
  `publication_status` è sempre `not_evaluated` e `gate_report_id` è sempre
  `None`.
- **nessuna `final_gate_reports`.** Il runner non scrive alcuna riga in
  `final_gate_reports` in alcun branch.
- **nessuna `published_answers`.** Il runner non scrive alcuna riga in
  `published_answers` in alcun branch.
- **run completed non significa publication allowed.** Un
  `orchestration_runs.status='completed'` indica solo che tutti gli agenti
  richiesti hanno terminato senza errore; non è una decisione di pubblicabilità.
- **provider output non significa risposta pubblicabile.** Un
  `orchestration_agent_outputs` `succeeded` è un output candidato di un agente,
  non una risposta pubblicabile. Il Final Answer Gate resta l'unica autorità di
  pubblicazione; lo schema `0011` rende strutturalmente impossibile pubblicare
  una risposta saltando il gate.

L'innesto fra una eventuale sintesi multi-AI e la catena Claim Extraction →
Evidence Binding → Final Answer Gate è materia di `ORCH-GATE-A`
(`PHASE_PRODUCT_ORCHESTRATION_PRE.md §19.11`), non di `ORCH-MULTI-A`.

---

## 15. Sicurezza e redaction

- **nessun secret nella request.** Il contratto di input (§5) non porta API key,
  token di autenticazione, credenziali, Authorization header, password.
- **nessuna credenziale di provider reale.** Il provider è mock; nessuna chiave
  esiste o è attesa. Quando un provider reale sarà introdotto, le credenziali
  vivranno nell'implementazione del provider, non nell'input del runner.
- **failure_reason / error_message redatti.** Ogni `error_message` persistito su
  `orchestration_runs.failure_reason`,
  `orchestration_agent_runs.failure_reason`,
  `provider_invocations.error_message` e sui payload degli eventi di fallimento
  passa per `_safe_error_message` di `ORCH-PROVIDER-A`, che maschera i segreti
  nella forma `name=value` / `name: value` con `[REDACTED]` e tronca a
  `_ERROR_MESSAGE_MAX_LEN`. La redaction si applica per ciascun agente fallito.
- **nessun raw provider payload non redatto.** Se in futuro si vorrà conservare
  un payload, lo si conserva solo nella forma redatta dalla
  `ProviderRedactionPolicy` (`hash_only` di default). `request_hash` e
  `response_hash` su `provider_invocations` servono audit/idempotenza/debug, non
  provano il contenuto. `redaction_strategy` (default `hash_only`) è persistito
  su ogni `provider_invocations`.

Vincoli ereditati: i log del runner e dei test non devono contenere segreti; i
payload di `orchestration_events.event_payload` non devono contenere segreti;
`provider_invocations` per costruzione dello schema `0011` non ha colonne per
credenziali.

---

## 16. Test richiesti per la futura ORCH-MULTI-A

Test concreti per `ORCH-MULTI-A`, mock-first, DB-backed dove serve, senza rete,
senza Redis, senza FastAPI, sul modello di
`test_orchestration_runner_service.py`. Minimo 14 test:

1. **`successful_multi_agent_mock_run_persists_one_agent_run_per_config`** —
   con N agent_config validi (es. 3), il run è `completed` e persiste
   esattamente una riga `orchestration_agent_runs` per `agent_config`, ciascuna
   `succeeded` `attempt_no=1` `is_mock`, con il proprio snapshot, messaggi,
   provider invocation, token usage e output. Prova che il multi-agente
   materializza un agent run per config, niente di più, niente di meno.

2. **`deterministic_agent_order_follows_request_order`** — gli `agent_run_ids`
   nell'output e l'ordine degli eventi `agent_run_started` seguono l'ordine
   posizionale di `agent_config_ids` nella richiesta, non l'ordine del DB.
   Prova l'ordine deterministico di §7.

3. **`rejects_empty_agent_config_list`** — una `agent_config_ids` vuota è
   rifiutata con `invalid_request` prima di qualunque scrittura DB
   (`orchestration_run_id=None`, nessuna riga creata). Prova il vincolo "minimo
   1 agent_config".

4. **`rejects_duplicate_agent_config_ids`** — `agent_config_ids` con un duplicato
   è rifiutata con `invalid_request` prima di scrivere; nessun run creato. Prova
   il rifiuto dei duplicati di §7.

5. **`rejects_cross_tenant_agent_configs`** — un insieme di agent_config che non
   condividono lo stesso `tenant_id` (o non coincidono con `request.tenant_id`)
   è rifiutato con `invalid_request` prima di scrivere. Prova il vincolo di
   tenant condiviso.

6. **`rejects_mixed_master_prompt_agent_configs`** — agent_config che riferiscono
   `master_prompt_id` diversi (o non coerenti con il `master_prompt_version_id`
   richiesto) sono rifiutati con `invalid_request` prima di scrivere. Prova il
   vincolo di master_prompt condiviso.

7. **`provider_error_in_one_agent_fails_run_but_persists_successful_agent_facts`**
   — con N agenti di cui uno con `mock_error_by_agent` che inietta un errore, il
   run è `failed`, `failed_agent_config_ids` contiene l'agente fallito, l'agente
   fallito ha `orchestration_agent_runs` `failed` + `provider_invocations`
   `failed` + `token_usage_records` (provider invocato) e nessun output; gli
   agenti riusciti conservano i propri fatti (agent run `succeeded`, output,
   usage). Prova la failure policy all-or-nothing con persistenza dei fatti.

8. **`global_budget_preflight_blocks_before_any_agent_invocation`** — con un
   `token_budget_id` il cui `token_limit` è superato dalla stima totale, il run è
   `failed` con `error_code='budget_exceeded'`, eventi `token_budget_exceeded` +
   `run_failed`, e **nessun** `orchestration_agent_runs`, **nessuna**
   `provider_invocations`, **nessun** `token_usage_records`, **nessun**
   `agent_run_started`. Prova il preflight globale di §10.

9. **`idempotency_replay_returns_existing_multi_agent_run_without_duplicates`** —
   due chiamate con lo stesso `(tenant_id, idempotency_key)` ritornano lo stesso
   `orchestration_run_id`; i conteggi di `orchestration_runs`,
   `orchestration_agent_runs`, `provider_invocations`, `token_usage_records`,
   `orchestration_agent_outputs` e `orchestration_events` restano invariati; gli
   `event_ids` coincidono. Prova l'idempotenza di replay di §11.

10. **`source_candidates_are_grouped_by_agent_output_and_unverified`** — con
    `mock_source_candidates_by_agent` per più agenti, ogni candidate è
    persistita con `status='proposed'`, `candidate_type='agent_cited'`,
    `provenance.is_verified=False`, e `agent_output_id` valorizzato all'output
    del rispettivo agente; gli eventi `source_candidate_created` hanno
    `idempotency_key` distinti per (agente, candidate). Prova §13.

11. **`runner_does_not_create_gate_publication_synthesis_or_resolution_rows`** —
    delta zero su `final_gate_reports` e `published_answers`; zero
    `candidate_syntheses`, `source_resolutions`, `source_verifications` per il
    run; `orchestration_runs.final_gate_report_id` NULL. Prova §14.

12. **`event_sequence_numbers_are_monotonic_and_event_types_allowed`** — gli
    `orchestration_events` del run hanno `sequence_no` contiguo da 0; gli
    `event_type` emessi sono un sottoinsieme di
    `{run_created, agent_run_started, agent_run_completed, agent_run_failed,
    source_candidate_created, token_budget_exceeded, run_failed}`; assenza
    esplicita di event_type inventati (`run_started`, `run_completed`,
    `provider_invocation_started`, `provider_invocation_completed`). Prova §12.

13. **`error_messages_are_redacted_across_agent_failures`** — con
    `mock_error_by_agent` che inietta messaggi contenenti segreti (`api_key=`,
    `authorization: Bearer`, `password=`) per più agenti falliti, i segreti non
    compaiono e `[REDACTED]` compare in `provider_invocations.error_message`,
    `orchestration_agent_runs.failure_reason`,
    `orchestration_runs.failure_reason` e nel `result.error_message`. Prova §15
    su più agenti.

14. **`module_uses_no_network_redis_fastapi_or_provider_sdk_imports`** — test
    AST/import-level che ispeziona il sorgente del modulo e vieta import di
    network client, Redis, FastAPI e SDK provider; conferma che il modulo si
    appoggia solo a `sqlalchemy` e a `orchestration_provider`. La lista dei
    token vietati è assemblata da frammenti a runtime così che un grep ingenuo
    del file di test non auto-intercetti la lista letterale.

Note: i test DB-backed seguono il pattern di `ORCH-RUNNER-A` (fixture
`conn` con transazione e rollback al teardown, migration applicate una volta per
sessione, skip pulito se `DATABASE_URL` è assente, conteggi before/after per
essere rerun-safe su DB dev non vuoto). Nessun test si basa sul tempo reale.

---

## 17. Acceptance criteria

Il documento `PHASE_ORCH_MULTI_PRE.md`, per `ORCH-MULTI-A-PRE`, è accettabile se
e solo se:

- **solo `PHASE_ORCH_MULTI_PRE.md` creato** — nessun altro file del repository è
  creato o modificato;
- **nessun codice modificato** — nessun file di codice di produzione, worker,
  backend, frontend o pacchetto condiviso è scritto o modificato; in particolare
  il runner esistente, la provider abstraction e il consumer non sono toccati;
- **nessun test modificato** — nessun file di test scritto o modificato;
- **nessuna migration modificata** — nessun file in `migrations/`; `0001`-`0011`
  invariate;
- **nessuna API modificata** — nessuna route HTTP aggiunta o modificata;
- **nessuna UI modificata** — nessuna pagina o componente frontend;
- **nessuna dipendenza aggiunta** — nessun manifest o lockfile toccato;
- **nessun wording vietato** — il documento non contiene i termini della lista
  vietata fuori dall'elenco esplicito di §18 (comando grep di controllo) e dalla
  sezione di sicurezza linguistica;
- **design coerente con ORCH-RUNNER-A e schema 0011** — gli `event_type` usati
  appartengono al codominio chiuso di
  `orchestration_events_event_type_chk`; tabelle, colonne, CHECK e UNIQUE
  referenziati corrispondono a quelli reali di `0011`; la disciplina insert-once
  / final-status, l'ordine FK e la transaction model estendono coerentemente
  `ORCH-RUNNER-A`;
- **design mantiene source candidates separate da evidence** — le
  `source_candidates` restano `proposed` / non verificate, senza
  `evidence_span_id`, senza claim link, senza ponte verso `evidence_spans`;
- **design mantiene gate/publication fuori scope** — `final_gate_report_id` NULL,
  `publication_status='not_evaluated'`, nessun `final_gate_reports`, nessun
  `published_answers`.

---

## 18. Comandi di verifica

I comandi seguenti permettono a un revisore di verificare i criteri di
accettazione di §17 in modo meccanico. Sono comandi di **sola lettura**: non
modificano il repository. Vanno eseguiti dalla radice del repository.

### 18.1 Comandi base

```bash
git diff --check
git diff --stat
git diff --name-only
git status -sb
```

`git diff --check` non deve segnalare errori di whitespace; `git diff --stat` e
`git diff --name-only` devono mostrare un solo file modificato; `git status -sb`
mostra lo stato sintetico del branch.

### 18.2 Controllo file singolo

`git diff --name-only` deve mostrare esclusivamente:

```
PHASE_ORCH_MULTI_PRE.md
```

Nessun altro file del repository deve comparire. In particolare non devono
comparire file in `apps/*`, `migrations/*`, `tests/*`, `packages/*`, né altri
`PHASE_*` o `*_IMPLEMENTATION_REPORT.md`.

### 18.3 Controllo wording vietato

Il controllo usa pattern con parentesi quadre sul primo carattere, così che il
comando non intercetti se stesso:

```bash
grep -niE "[t]ruth score|[v]erified true|[v]erified answer|[A]I verified|[f]actually true|[h]allucination eliminated|[h]allucination-free|[g]uaranteed truth|[z]ero hallucinations|[e]ntailed = true|[s]ource quality proves claim|[C]VE-lite proves support|[r]eal NLI|[c]ontradiction detector|[c]itation-to-claim validator" \
  PHASE_ORCH_MULTI_PRE.md || true
```

Deve restituire nulla (al di fuori del comando stesso citato in questo
documento).

---

## 19. Fase futura di implementazione

La fase di codice successiva, dopo questo design, dovrà essere:

**ORCH-MULTI-A**

`ORCH-MULTI-A` implementerà l'orchestrazione mock multi-agente progettata qui,
estendendo `apps/worker/app/services/orchestration_runner.py` (o un modulo
sibling) e aggiungendo i test worker-level DB-backed di §16. Le fasi successive
nella roadmap restano `ORCH-REVIEW-A`, `ORCH-SYNTHESIS-A`, `ORCH-SOURCES-A`,
`ORCH-GATE-A` e, separatamente, l'introduzione dei provider reali — tutte fuori
scope sia per `ORCH-MULTI-A-PRE` sia per `ORCH-MULTI-A`.

---

## 20. Commit message suggerito

```
Document multi-agent mock orchestration design
```

*Nessun commit è eseguito in questa fase.*
