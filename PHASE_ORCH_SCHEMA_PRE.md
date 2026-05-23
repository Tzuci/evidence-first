# PHASE ORCH-SCHEMA-PRE

> **Documento di design schema per ORCH-SCHEMA-PRE.**
> Questo blocco è **solo progettazione di schema**. Non implementa codice di
> produzione, non crea migration, non modifica `apps/api/*`, `apps/worker/*`,
> `apps/web/*`, `packages/shared/*`, non aggiunge dipendenze, non tocca i test,
> non modifica `README.md` né `PROJECT_STATE.md` né alcun altro `PHASE_*_PRE.md`.
> L'unico deliverable è questo file.
>
> Lingua: italiano tecnico, registro da System Architect.
>
> Il documento è completo e copre le sezioni 1-24: dallo scopo della fase e dal
> contesto di prodotto, attraverso i principi di schema e il dettaglio delle
> entità proposte, fino alle API implicate solo a livello concettuale, alle
> implicazioni per il worker e per la UI, ai non-goals, agli acceptance criteria
> e ai comandi di verifica.
>
> **Promemoria di linguaggio (vincolante per tutta la fase).** Il sistema è
> evidence-first ed evidence-gated. Non promette verità assoluta, non promette
> l'eliminazione totale delle allucinazioni, non dichiara che le sue risposte
> siano "vere". Produce risposte basate sulle evidenze disponibili e può
> trattenere la pubblicazione quando il supporto è insufficiente. Una fonte
> citata da una AI non è un'evidenza valida finché non è stata recuperata,
> risolta, verificata e collegata a evidence span. Una source candidate non è
> evidence. Il Final Answer Gate decide *publication allowed* / *publication
> held*, non verità assoluta.
>
> **Nota di coerenza architetturale (vincolante).** In tutto il documento,
> quando un'entità ha "stati" o "transizioni", quelle transizioni vanno
> registrate come **eventi auditabili, versioni o snapshot immutabili**, mai
> come riscrittura silenziosa di un fatto storico già registrato. La distinzione
> operativa da tenere sempre presente è: una *configurazione* (la formulazione
> di un prompt, i parametri di un agente, un budget) può essere mutabile
> **finché un run non l'ha consumata**; un *fatto* (un output prodotto, un
> messaggio scambiato, una decisione del gate, un consumo di token, una
> transizione di stato di un run, una source candidate proposta) è
> **append-only dopo essere accaduto**, e una sua "modifica" è sempre una nuova
> riga, un nuovo evento o una nuova versione, mai un update in place.

---

## Indice

1. Scopo della fase
2. Contesto prodotto
3. Stato attuale del sistema
4. Principi schema
5. Entità proposte — panoramica
6. MasterPrompt e snapshot
7. AgentConfig, AgentRolePrompt e snapshot
8. OrchestrationRun
9. OrchestrationEvents
10. AgentRun, AgentMessage, AgentOutput
11. SourceCandidate
12. SourceResolution e SourceVerification
13. TokenBudget e TokenUsageRecord
14. ProviderInvocation
15. CandidateSynthesis
16. Collegamento con Claim Ledger e Gate
17. Relazione con schema esistente
18. Strategia di migrazione futura
19. API implicate, solo concettuali
20. Worker implications
21. UI implications
22. Non-goals
23. Acceptance criteria
24. Comandi di verifica

---

## 1. Scopo della fase

La fase **ORCH-SCHEMA-PRE** progetta lo **schema minimo** necessario alla futura
orchestrazione multi-AI dell'Evidence-First MVP-0: le entità che rappresenteranno
il Prompt Master, gli agenti AI configurabili, le esecuzioni di orchestrazione,
gli output degli agenti, le fonti candidate, il loro recupero e la loro verifica,
fino al punto di giunzione con la pipeline evidence-gated già esistente.

Questa fase è **esclusivamente di design di schema**. Non:

- crea migration di database (nessun file in `migrations/`);
- implementa codice di backend, worker, frontend o pacchetti condivisi;
- modifica lo schema esistente o aggiunge tabelle;
- aggiunge dipendenze a qualunque manifest;
- introduce provider AI reali o riferimenti operativi a provider esterni;
- introduce route HTTP o pagine UI;
- modifica i test esistenti o ne aggiunge di nuovi;
- modifica `README.md`, `PROJECT_STATE.md` o documenti `PHASE_*_PRE.md`
  preesistenti.

Lo scopo è **fissare un disegno di schema condiviso** che le fasi di codice
successive potranno implementare senza dover reinventare contratti. Il documento
descrive entità, campi probabili, vincoli concettuali, invarianti append-only,
relazioni e stati. **I "campi probabili" sono indicativi** e soggetti a revisione
nella fase di implementazione.

La fase ORCH-SCHEMA-PRE si colloca nella roadmap incrementale definita da
`PHASE_PRODUCT_ORCHESTRATION_PRE.md §19`: è il blocco **§19.2 (ORCH-SCHEMA-PRE)**,
e precede direttamente **ORCH-SCHEMA-A** (§19.3), che sarà la fase che scrive le
migration reali. ORCH-SCHEMA-A è quindi la fase successiva eventuale a questo
documento; ogni decisione qui presa è una raccomandazione di design per quella
fase, non un impegno di implementazione.

ORCH-SCHEMA-PRE eredita inoltre il vincolo, dichiarato in
`PHASE_PRODUCT_ORCHESTRATION_PRE.md §6.5` e §15, di **decidere il destino delle
tabelle placeholder** introdotte dalla migration `0005` (`agent_runs`,
`agent_outputs`, `truncation_events`, `continuation_attempts`): se adottarle,
ridefinirle o sostituirle. Questa decisione, e il suo razionale, sono parte del
deliverable e vengono affrontati nelle sezioni 5, 7 e 10 (panoramica e dettaglio)
e ratificati nella sezione 17.

---

## 2. Contesto prodotto

La direzione di prodotto è stata fissata da `PHASE_PRODUCT_ORCHESTRATION_PRE.md`
e corretta dalla micro-fase `PRODUCT-ORCHESTRATION-PRE-FIX-A` (commit
`1e307fa`, "Clarify product orchestration source model"). Il **core corretto del
prodotto** è il seguente flusso:

```
Prompt Master
   │  ◄── (opzionale, laterale) fonti utente / fonti interne / fonti web future
   ▼
AI Agents (configurazione: nome, provider, modello, ruolo, prompt, vincoli, budget)
   ▼
Agent Answers + Source Candidates (ogni agente risponde e propone/cita fonti)
   ▼
Source Retrieval / Source Resolution (recupero o risoluzione delle fonti reali)
   ▼
Source Verification / Evidence Extraction (estrazione quote/evidence span, hash)
   ▼
Cross Review (confronto bounded fra agenti, critic review, contraddizioni/gap)
   ▼
Candidate Synthesis (convergenza verso una risposta unica candidata)
   ▼
Claim Extraction (la sintesi viene scomposta in claim verificabili)
   ▼
Evidence Binding (i claim vengono collegati a evidence span verificati)
   ▼
Final Answer Gate (CVE-lite + Source Quality + Claim Entailment → decisione)
   ▼
Articulated Answer + Publication Status + Technical Report
```

**Le fonti caricate dall'utente sono opzionali.** L'orchestrazione multi-AI può
partire dal solo Prompt Master: sono gli agenti a produrre risposte e a proporre
fonti candidate. Fornire fonti è utile ma non è un passo obbligatorio.

**Le fonti proposte dagli agenti sono source candidates.** Una citazione o un
riferimento prodotto da un'AI **non è un'evidenza valida**. È un *source
candidate* che deve essere recuperato, risolto, verificato e collegato a evidence
span prima di poter partecipare al gate. Saltare uno di questi passi
significherebbe trattare la citazione di un modello come un'evidenza verificata,
cosa che il prodotto evidence-first non fa.

### 2.1 Le tre modalità di prodotto

Il prodotto si articola in tre modalità (vedi `PHASE_PRODUCT_ORCHESTRATION_PRE.md
§3.5`). Distinguerle è essenziale per non confondere il core con una modalità
secondaria.

- **Multi-AI Orchestration Mode — il core del prodotto.** È la modalità
  centrale. Parte dal Prompt Master e **non richiede che l'utente fornisca
  fonti iniziali**. Più agenti AI configurati producono ciascuno una risposta e
  propongono o citano fonti candidate; il sistema porta gli output a convergere
  in una risposta unica candidata, recupera e verifica le fonti candidate, e
  fa decidere al Final Answer Gate. Questa modalità richiede provider AI reali o
  un modello AI locale integrato per produrre una vera sintesi multi-AI.
- **Local Evidence Mode — modalità secondaria ma utile.** L'utente fornisce un
  Prompt Master *insieme* a fonti locali. È utile, in particolare per testare la
  pipeline, il gate e il report in modo deterministico, ed è la forma a cui il
  sistema closed-corpus attuale è più vicino. **Non è il core finale del
  prodotto.**
- **Hybrid Mode — probabilmente la modalità più potente.** Combina le due
  strade: parte dal Prompt Master, usa le fonti fornite dall'utente *e* integra
  le fonti proposte dagli agenti e quelle recuperate/verificate dal sistema.

Lo schema progettato in questo documento deve poter rappresentare tutte e tre le
modalità senza che ciascuna richieda tabelle proprie: la modalità è una
caratteristica di un `OrchestrationRun`, non una biforcazione di schema.

### 2.2 Promessa epistemica invariata

Anche a regime, con provider reali, la promessa del prodotto resta evidence-first:
produce una *sintesi multi-AI controllata rispetto alle evidenze disponibili*,
può *trattenere la pubblicazione quando il supporto è insufficiente*, e *non
garantisce la verità fattuale nel mondo*. Lo schema non introduce alcun campo che
rappresenti "verità del claim" o "verità multi-AI": i sei assi già esistenti
(claim correctness, evidence support, CVE-lite verification, source quality,
claim entailment, decisione di pubblicazione del Final Answer Gate) restano
separati, e nessuna entità di questo schema li collassa in un punteggio unico.

---

## 3. Stato attuale del sistema

Questa sezione distingue ciò che **esiste oggi** (al commit `af74187`, stato
post-8.8B-REPORT descritto da `PROJECT_STATE.md`) da ciò che **non esiste**. La
fase di codice successiva dovrà riverificare ogni elemento contro il proprio HEAD.

### 3.1 Cosa esiste oggi

Componenti reali, implementati, testati, riusabili dalla linea multi-AI:

- **projects** — entità progetto con endpoint `POST /api/v1/projects`,
  `GET /api/v1/projects`, `GET /api/v1/projects/{id}`. Multi-tenant a livello DB,
  un solo tenant `dev` seeded in MVP-0.
- **documents `.txt`/`.md`** — upload reale, `uploaded_documents`,
  `document_versions` `parsed`, `document_chunks` deterministici, uno
  `evidence_spans` per chunk. Estensioni `.txt`/`.md`, limite 50 MiB.
- **tasks closed_corpus** — `POST /api/v1/tasks` crea un task `closed_corpus`
  da `project_id`, `objective`, `mode`, `document_ids`, `policy` opzionale, con
  supporto `Idempotency-Key`.
- **worker `task.created`** — consumer single-consumer, FK-safe, resume-safe,
  idempotente, che processa l'evento `task.created` pubblicato su Redis. La
  pipeline approved produce 15 eventi audit worker-side.
- **Claim Ledger** — base append-only stretta: `logical_claims`, `raw_claims`,
  `classified_claims`, `claim_ledger_entries`, `claim_lineage`,
  `claim_evidence_links`, `verification_records`. Supersede esclusivamente via
  `claim_lineage(relation_kind='supersedes')`.
- **evidence spans** — `evidence_spans` append-only, uno per chunk, con `quote`
  e `quote_hash`.
- **CVE-lite** — controllo mock-driven di presenza testuale della quote nel
  chunk e di hash della quote; scrive `verification_records`. Non valuta
  supporto semantico.
- **Source Quality** — `source_quality_assessments` append-only, evaluator mock
  deterministico (produce sempre `overall_quality='unknown'`,
  `contradiction_status='unchecked'`). Consultata dal Final Answer Gate (8.7G).
- **Claim Entailment** — `claim_entailment_checks` append-only, checker mock
  heuristic deterministico a tre regole (containment / numeric mismatch /
  default uncertain). Non emette mai `contradicted` né `partially_supported`.
  Consultata dal Final Answer Gate (8.8A-GATE).
- **Final Answer Gate** — gate mock-driven che compone CVE-lite, Claim
  Entailment e Source Quality (priorità: CVE-lite > Claim Entailment > Source
  Quality), decide `approved`/`rejected`, scrive `final_gate_reports`
  append-only e, su approved, inserisce `published_answers` v1.
- **published answers** — `published_answers` con `summary_text` e
  `content_hash`, stati lifecycle `published`/`withdrawn`/`superseded`.
- **Anti-Hallucination Report** — `GET /api/v1/tasks/{task_id}/anti-hallucination-report`,
  vista task-level read-only aggregata su publication, gate, claims, evidence,
  CVE-lite, Source Quality, Claim Entailment, axis_summary, mock_indicators,
  limitations. Non ricalcola il gate, non muta DB.
- **`/requests/new`** — flusso guidato di creazione task a quattro sezioni
  (Project, Sources, Request, Create task), implementato da `NewRequestFlow`
  (UI-CREATE-FLOW-A, commit `1594d21`).
- **same-origin proxy `/api/ef/*`** — route handler Next.js server-side che
  inoltrano al backend in modo verbatim, aggirando l'assenza di CORS.
- **infrastruttura trasversale** — audit chain hash-linked append-only
  verificabile end-to-end, storage content-addressed deduplicato refcount-based,
  `event_processing_records` con idempotenza per consumer, `policy_versions`
  come meccanismo di versionamento di policy.

### 3.2 Cosa non esiste

Entità e capacità che la visione multi-AI richiede e che oggi **non esistono**:

- **MasterPrompt entity** — non esiste un'entità persistente "Prompt Master".
  La domanda dell'utente vive oggi come campo `objective` di un task.
- **AgentConfig reale** — non esiste alcuna entità di configurazione di un
  agente AI.
- **AgentRolePrompt** — non esiste un'entità che rappresenti ruolo e prompt
  specifico assegnati a un agente.
- **OrchestrationRun** — non esiste un'entità che rappresenti una esecuzione di
  orchestrazione multi-AI.
- **source candidates** — non esiste alcun concetto di fonte proposta o citata
  da un agente AI come candidato da recuperare e verificare.
- **source resolution** — non esiste alcun recupero o risoluzione di una fonte
  candidata in una fonte reale ingeribile.
- **source verification per fonti candidate** — non esiste un passo di verifica
  dedicato alle fonti candidate (CVE-lite oggi verifica solo evidence span
  derivati dall'upload dell'utente).
- **provider invocation** — non esiste alcuna invocazione di provider AI; in
  MVP-0 vige `PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0`.
- **token usage / token accounting** — non esiste contabilizzazione di token o
  costo per agente o per run.
- **candidate synthesis** — non esiste un'entità di sintesi multi-AI candidata.
- **orchestration events** — non esiste un log append-only di eventi di
  orchestrazione.
- **UI progress run** — non esiste una superficie che mostri il progresso di un
  run o gli eventi intermedi di un'orchestrazione.
- **provider reali** — nessun OpenAI, Anthropic, Gemini integrato.
- **local LLM integration** — nessun modello AI locale integrato.

### 3.3 Le tabelle placeholder della migration 0005

La migration `0005_answers_gate.sql` ha creato quattro tabelle placeholder:
`agent_runs`, `agent_outputs`, `truncation_events`, `continuation_attempts`.

**Queste tabelle NON sono un'implementazione dell'orchestrazione multi-AI.** Allo
stato attuale:

- `agent_runs` esiste ma ha una semantica ristretta al solo tracking di
  `compile_draft` e `final_answer_gate` (CHECK `run_kind ∈ {compile_draft,
  final_answer_gate}`): è usata dal compiler e dal gate mock-driven, **non** da
  un orchestratore di agenti AI.
- `agent_outputs` esiste ma è **vuota in 8.4** e in tutte le fasi successive: il
  pipeline mock-driven non passa per agent completions.
- `truncation_events` e `continuation_attempts` esistono ma sono **vuote** e
  prive di semantica operativa.

Coerentemente con `PHASE_PRODUCT_ORCHESTRATION_PRE.md §15`, una tabella diventa
un componente reale solo quando ha, tutti insieme: una **semantica definita**,
**servizi** che la popolano e la leggono, **API** che la espongono, e **test**
che la coprono. Le quattro tabelle placeholder non hanno nessuno di questi quattro
elementi per la semantica multi-AI.

**Distinzione di stato**, da preservare in tutto il documento:

- `agent_runs` — **esistente ma con semantica diversa** (tracking compiler/gate),
  **non** una tabella di orchestrazione multi-AI. La fase ORCH-SCHEMA-A dovrà
  decidere se la nuova entità `agent_runs` multi-AI riusa il nome (con rischio
  di collisione semantica), usa un nome diverso, o convive.
- `agent_outputs` — **placeholder vuoto**. Stesso problema di naming.
- `truncation_events`, `continuation_attempts` — **placeholder vuoti**, candidati
  a essere riusati come base per token accounting e retry tracking, o a essere
  abbandonati.

La decisione finale (adottare / ridefinire / sostituire) è una **decisione aperta**
che questo documento istruisce e che viene ratificata nella sezione 17
e in ORCH-SCHEMA-A. Per le sezioni 1-12 si assume, come ipotesi di lavoro, che le
nuove entità multi-AI vengano introdotte come **tabelle nuove con nomi propri**,
lasciando le placeholder `0005` come debito di naming da risolvere; le sezioni 5,
7 e 10 discutono il punto e la sezione 17 lo chiude.

---

## 4. Principi schema

I principi seguenti sono **vincolanti** per il disegno delle entità multi-AI e
per la fase ORCH-SCHEMA-A. Sono coerenti con gli invarianti già adottati dal
Claim Ledger, dall'audit chain e dalle tabelle append-only esistenti
(`evidence_spans`, `claim_ledger_entries`, `final_answer_spans`,
`final_gate_reports`, `source_quality_assessments`, `claim_entailment_checks`).

1. **Append-only per i fatti accaduti.** Ogni entità che registra un *fatto*
   — un output prodotto, un messaggio scambiato, una decisione presa, un consumo
   di token, una transizione di stato di un run, una source candidate proposta,
   un esito di risoluzione o verifica — è append-only. Una sua "modifica" è
   sempre una nuova riga. L'enforcement raccomandato è il trigger comune
   `reject_modify_append_only()` definito in `0001_foundation.sql`, già usato da
   nove tabelle del sistema.

2. **Configurazione mutabile solo prima del run.** Un'entità di *configurazione*
   — il testo di un MasterPrompt, i parametri di un AgentConfig, il prompt di un
   AgentRolePrompt, il limite di un budget — può essere modificata **finché
   nessun `OrchestrationRun` l'ha consumata**. Dopo la consumazione, la
   configurazione è congelata per quel run.

3. **Snapshot immutabili al momento dell'avvio run.** Quando un
   `OrchestrationRun` parte, fissa uno **snapshot immutabile** della
   configurazione che consuma (MasterPrompt, ogni AgentConfig, ogni
   AgentRolePrompt, ogni TokenBudget). Il run deve poter essere auditato rispetto
   alla configurazione *esatta* che ha usato, e una modifica successiva alla
   configurazione non deve mai alterare retroattivamente ciò che il run ha
   eseguito.

4. **No update silenzioso di fatti storici.** Nessuna entità di fatto viene mai
   aggiornata in place. Le transizioni di stato di un run o di un agent run sono
   registrate come **eventi o nuove versioni**, mai come riscrittura del campo
   `status` di una riga storica.

5. **Eventi/versioni per le transizioni.** Le transizioni di stato hanno due
   rappresentazioni ammesse: (a) un log append-only di eventi (vedi §9,
   `orchestration_events`); (b) un versionamento append-only (nuova riga con
   `version_no` incrementato). Lo schema deve scegliere, per ogni entità con
   stati, quale delle due usare, e dichiararlo. La raccomandazione di questo
   documento è privilegiare gli eventi per le transizioni di run e agent run.

6. **Idempotenza.** Ogni entità che può essere scritta in risposta a un evento
   redeliverato deve avere una chiave di idempotenza, enforced da un UNIQUE (o
   UNIQUE composito / partial UNIQUE). Pattern coerente con `event_processing_records`,
   con le UNIQUE di `0005` (`coverage_gap_statements_idem_uq` ecc.) e con
   `cec_entry_span_idem_uq` di `0009`. Un doppio delivery non deve duplicare
   `AgentRun`, `AgentOutput`, `SourceCandidate`, invocazioni di provider o
   eventi.

7. **Auditabilità.** Ogni fatto rilevante di un run deve essere riconducibile
   all'audit chain hash-linked esistente. Le transizioni di stato di un
   `OrchestrationRun` e di un `AgentRun` sono fatti che devono entrare
   nell'audit, coerentemente con quanto il consumer `task.created` già fa per la
   pipeline 8.4/8.7/8.8A.

8. **Separazione tra config e run facts.** La configurazione (MasterPrompt,
   AgentConfig, AgentRolePrompt, TokenBudget come limite) vive in tabelle
   distinte dai fatti del run (OrchestrationRun, AgentRun, AgentMessage,
   AgentOutput, eventi, token usage). Lo snapshot è il ponte fra le due: copia o
   versione immobilizzata della config dentro/accanto al run.

9. **Separazione tra source candidate e verified evidence.** Una
   `source_candidate` (fonte proposta da un agente, dall'utente o dal sistema)
   **non è** un `evidence_span`. Sono entità distinte, in tabelle distinte. Un
   evidence span nasce solo dopo che una source candidate è stata risolta e
   verificata. Lo schema non deve permettere di trattare una source candidate
   come evidenza.

10. **Separazione tra candidate synthesis e published answer.** Una
    `candidate_synthesis` è una sintesi multi-AI *candidata*, non pubblicata e
    non verificata. È un'entità distinta da `published_answers`. Una candidate
    synthesis diventa una risposta pubblicabile solo dopo Claim Extraction,
    Evidence Binding e Final Answer Gate.

11. **Nessun bypass del Final Answer Gate.** Nessuna entità dello schema e
    nessun percorso che lo schema rende possibile deve consentire la
    pubblicazione di una risposta saltando il Final Answer Gate. Il reviewer
    pass, il critic pass e l'optional second check pass non sono sostituti del
    gate. Lo schema deve rendere strutturalmente naturale che ogni risposta
    multi-AI passi per la catena Claim Extraction → Evidence Binding →
    GateEvaluation prima di diventare un `published_answers`.

---

## 5. Entità proposte — panoramica

La tabella seguente elenca le entità proposte per lo schema di orchestrazione
multi-AI. Per ciascuna: il **tipo** (configurazione mutabile / fatto append-only
/ snapshot immutabile / join / derived), lo **scopo** sintetico, se è
**esistente o nuova**, e la **priorità MVP** (alta = necessaria al primo runner
single-agent; media = necessaria al multi-agente / review / synthesis; bassa =
necessaria solo con provider reali o per osservabilità avanzata).

| Entità | Tipo | Scopo | Esistente / nuova | Priorità MVP |
|---|---|---|---|---|
| `master_prompts` | configurazione mutabile | Input primario del prodotto: domanda/obiettivo dell'utente; entità centrale persistente. | nuova | alta |
| `master_prompt_versions` / `master_prompt_snapshots` | snapshot immutabile | Versione/snapshot immutabile del testo del prompt consumato da un run. | nuova | alta |
| `agent_role_prompts` | configurazione mutabile (catalogo) + versionabile | Ruolo e prompt specifico assegnabili a un agente; riusabile e versionabile. | nuova | media |
| `agent_configs` | configurazione mutabile | Configurazione di un agente AI: provider, modello, ruolo, prompt, budget, contract, flag reviewer/synthesizer. | nuova (collide di nome con nulla; vedi nota placeholder) | alta |
| `agent_config_snapshots` | snapshot immutabile | Snapshot immutabile della configurazione di ogni agente al momento dell'avvio del run. | nuova | alta |
| `orchestration_runs` | fatto append-only (radice) | Radice di una esecuzione di orchestrazione multi-AI; lega master prompt, agenti, output, gate. | nuova | alta |
| `orchestration_events` | fatto append-only | Log append-only delle transizioni e degli eventi di un run; alimenta la UI progress. | nuova | alta |
| `agent_runs` (multi-AI) | fatto append-only | Esecuzione concreta di un singolo agente dentro un run. | **nome esistente con semantica diversa in `0005`** (vedi nota) | alta |
| `agent_messages` | fatto append-only | Messaggi scambiati/prodotti a livello provider durante un agent run. | nuova | media |
| `agent_outputs` (multi-AI) | fatto append-only | Output strutturato e consumabile prodotto da un agent run. | **nome esistente come placeholder vuoto in `0005`** (vedi nota) | alta |
| `source_candidates` | fatto append-only | Fonti proposte da agenti, dall'utente o dal sistema; non sono evidence. | nuova | alta |
| `source_resolutions` | fatto append-only | Tentativo di recuperare/risolvere la fonte reale di una source candidate. | nuova | media |
| `source_verifications` | fatto append-only | Verifica di presenza/quote/hash su una fonte risolta; estrazione evidence span. | nuova | media |
| `token_budgets` | configurazione mutabile (limite) + snapshot | Budget di token/costo per agente, per run o per orchestrazione. | nuova | media |
| `token_usage_records` | fatto append-only | Consumo reale di token e costo per agent run, per pass, per invocazione. | nuova | media |
| `candidate_syntheses` | fatto append-only | Sintesi multi-AI candidata; input della Claim Extraction. | nuova | media |
| `synthesis_source_links` | join (append-only) | Collegamento fra una candidate synthesis e le source candidate / evidence span usate. | nuova | media |
| `synthesis_claim_links` | join (append-only) | Collegamento fra una candidate synthesis e i claim estratti (ponte verso il Claim Ledger). | nuova | media |
| `provider_invocations` | fatto append-only | Ogni chiamata all'astrazione di provider come fatto auditabile. | nuova | bassa (alta solo con provider reali) |

### 5.1 Note sulla panoramica

- **Tipi.** "Configurazione mutabile" = modificabile finché non consumata da un
  run. "Fatto append-only" = registra qualcosa di accaduto, mai modificato.
  "Snapshot immutabile" = copia/versione congelata di una configurazione al run
  start. "Join" = tabella di collegamento fra due entità; in questo schema le
  join sono anch'esse append-only perché registrano un fatto (un legame
  stabilito). "Derived" = vista calcolata, non una tabella; in questo schema
  nessuna delle entità sopra è una derived table — le viste derivate
  (es. un futuro report di run) sono fuori scope di schema e verranno trattate
  come read API.

- **`master_prompt_versions` vs `master_prompt_snapshots`.** I due nomi nella
  panoramica indicano la stessa funzione (immobilizzare il testo del prompt
  consumato da un run) con due modellazioni alternative discusse in §6: una
  tabella di versioni esplicite del prompt, oppure uno snapshot JSON inline nel
  run. La panoramica le elenca insieme per non anticipare la decisione; la
  raccomandazione motivata è in §6.

- **`agent_runs` e `agent_outputs` — collisione di nome con `0005`.** Come
  dichiarato in §3.3, la migration `0005` ha già `agent_runs` (con `run_kind ∈
  {compile_draft, final_answer_gate}`) e `agent_outputs` (placeholder vuoto). Le
  entità di orchestrazione multi-AI descritte nelle sezioni 7 e 10 hanno una
  semantica **diversa e più ricca**. ORCH-SCHEMA-A dovrà scegliere fra tre
  opzioni: (a) **riusare e ridefinire** le tabelle `0005` estendendone i CHECK e
  le colonne — sconsigliato, perché mescolerebbe il tracking compiler/gate con
  l'orchestrazione multi-AI su una stessa tabella, violando il principio 8
  (separazione config/fatti) e rendendo ambigua la lettura; (b) **nomi nuovi**
  per le entità multi-AI (per esempio `orchestration_agent_runs`,
  `orchestration_agent_outputs`) lasciando intatte le placeholder `0005` —
  raccomandato in prima istanza per chiarezza semantica; (c) **deprecare** le
  placeholder `0005` e migrare il tracking compiler/gate altrove — fuori scope
  per ORCH-SCHEMA-A, troppo invasivo. La sezione 17 ratifica la
  scelta; nelle sezioni 7 e 10 le entità sono descritte con i nomi `agent_*`
  per coerenza con il prompt operativo, ma con la clausola esplicita che il nome
  fisico finale potrà essere prefissato (`orchestration_*`) per evitare la
  collisione.

- **`truncation_events` e `continuation_attempts`.** Le altre due placeholder
  `0005` non hanno un'entità dedicata in questa panoramica. La loro funzione
  concettuale — registrare tagli di output e tentativi di continuazione — è
  coperta dai principi della token budget strategy (vedi `PHASE_PRODUCT_ORCHESTRATION_PRE.md
  §11`) e potrà essere materializzata, in una fase futura, come eventi dentro
  `orchestration_events` o come tabelle dedicate. ORCH-SCHEMA-PRE non le adotta
  e non le ridefinisce: le lascia come debito esplicito, da chiudere quando i
  provider reali renderanno il truncation un fatto concreto.

- **Priorità MVP.** Le entità a priorità *alta* sono il minimo per far girare un
  `OrchestrationRun` a singolo agente sul mock provider (fase ORCH-RUNNER-A). Le
  entità a priorità *media* servono al multi-agente, alla cross review, alla
  synthesis e al recupero/verifica delle fonti candidate. `provider_invocations`
  è a priorità *bassa* finché il provider è mock (l'invocazione è deterministica
  e priva di rete) e diventa *alta* quando arrivano i provider reali.

---

## 6. MasterPrompt e snapshot

### 6.1 `master_prompts` — configurazione mutabile

`master_prompts` rappresenta l'**input primario del prodotto**: la domanda, il
problema o l'obiettivo dell'utente. È l'entità centrale persistente attorno alla
quale ruotano fonti, agenti ed esecuzioni. Sostituisce, come unità mentale
primaria, l'attuale `objective` di un `task_masters`.

Il `master_prompts` in sé è **configurazione mutabile**: il testo, il titolo e lo
stato sono modificabili **finché nessun `OrchestrationRun` lo ha consumato**.

**Campi probabili** (indicativi):

- `id` — UUID, PK.
- `tenant_id` — UUID NOT NULL, FK `tenants`, ON DELETE RESTRICT.
- `project_id` — UUID nullable, FK `projects`, ON DELETE RESTRICT, se la nozione
  di progetto viene mantenuta come contenitore organizzativo (decisione aperta,
  vedi `PHASE_PRODUCT_ORCHESTRATION_PRE.md §20`).
- `prompt_text` — TEXT NOT NULL, il testo del prompt.
- `title` — TEXT nullable, label breve opzionale.
- `status` — TEXT NOT NULL con CHECK, per esempio `∈ {draft, ready, archived}`.
- `created_by` — UUID nullable, FK `users`, ON DELETE RESTRICT.
- `created_at`, `updated_at` — TIMESTAMPTZ.

**Relazioni:**

- un `master_prompts` è associato a zero o più `source_candidates` di tipo
  `user_supplied` (le fonti utente opzionali);
- a zero o più `agent_configs`;
- a zero o più `orchestration_runs`.

**Stati.** Lo stato proposto è un piccolo codominio (`draft` / `ready` /
`archived`). Lo stato è **configurazione mutabile**: l'utente può portare un
prompt da `draft` a `ready` e viceversa finché non è stato consumato. Una volta
che un run lo ha consumato, il *testo* del prompt è congelato per quel run via
snapshot (vedi §6.2), ma il `master_prompts` come record di configurazione può
continuare a evolvere per run futuri.

**Cosa è auditabile.** La *creazione* di un `master_prompts` e la sua consumazione
da parte di un run sono fatti auditabili. La modifica del testo prima della
consumazione è una mutazione di configurazione lecita e non genera necessariamente
un evento di audit — ma se si vuole tracciabilità completa delle revisioni del
prompt, conviene materializzarle come versioni (vedi §6.2 opzione A).

**Cosa non va fatto.** Non si deve permettere a un run di leggere il testo del
prompt "live" dalla tabella `master_prompts` al momento dell'esecuzione: se il
prompt cambiasse durante o dopo il run, l'audit del run diventerebbe incoerente.
Il run deve sempre lavorare su uno snapshot immutabile (vedi §6.2). Non si deve
introdurre un campo `superseded_by` mutabile su `master_prompts`: la storia delle
revisioni, se serve, è una catena di versioni append-only.

### 6.2 Snapshot del prompt consumato da un run

Quando un `OrchestrationRun` parte, deve **fissare uno snapshot immutabile** del
testo del prompt. Tre opzioni di modellazione:

- **Opzione A — `master_prompt_versions`.** Una tabella append-only di versioni
  del prompt. Ogni volta che il prompt viene modificato (o, in una variante più
  economica, ogni volta che viene consumato per la prima volta da un run), si
  appende una riga `master_prompt_versions` con `master_prompt_id`,
  `version_no`, `prompt_text`, `prompt_text_hash`, `created_at`. Un
  `orchestration_runs` referenzia una `master_prompt_version_id` immutabile.
  - *Pro.* Storia completa e auditabile delle revisioni del prompt; più run che
    consumano la stessa versione la condividono senza duplicare testo; pattern
    coerente con `document_versions` (0003) e con il versionamento append-only
    già adottato altrove.
  - *Contro.* Una tabella in più; il run dipende da una FK verso una riga di
    versione.

- **Opzione B — snapshot JSON inline dentro `orchestration_runs`.** Il run non
  referenzia una versione: copia il testo del prompt (e il suo hash) in un campo
  del proprio record, per esempio `master_prompt_snapshot JSONB` o
  `master_prompt_text TEXT` + `master_prompt_text_hash TEXT`, fissati all'avvio.
  - *Pro.* Zero tabelle aggiuntive; lo snapshot è atomico col run; il run è
    self-contained e auditabile leggendo una sola riga.
  - *Contro.* Duplicazione del testo se più run consumano lo stesso prompt
    identico; nessuna storia esplicita delle revisioni del prompt; il JSONB
    inline è meno ispezionabile di una tabella normalizzata.

- **Opzione C — entrambi.** `master_prompt_versions` per la storia delle
  revisioni del prompt come configurazione, *e* uno snapshot inline (almeno
  l'hash, o l'intero testo) dentro `orchestration_runs` come ridondanza
  difensiva e per rendere il run leggibile senza join.

**Raccomandazione per MVP.** **Opzione A**, con una clausola di ridondanza
minima presa dall'Opzione C: introdurre `master_prompt_versions` come tabella
append-only, far referenziare al run una `master_prompt_version_id` immutabile,
e **memorizzare comunque sul run almeno l'hash del prompt** (`master_prompt_text_hash`)
come campo denormalizzato di verifica. Motivazione: (1) il versionamento
append-only è il pattern già rodato del sistema (`document_versions`,
`claim_ledger_entries`, `source_quality_assessments` con `version_no`); (2)
l'hash sul run permette di verificare a colpo d'occhio quale prompt ha girato
senza join; (3) evita la duplicazione integrale del testo che l'Opzione B pura
comporterebbe. L'Opzione B pura (snapshot JSON inline senza tabella versioni) è
accettabile solo se ORCH-SCHEMA-A decidesse che la storia delle revisioni del
prompt non ha valore in MVP-0 — ma è un'economia che rende più difficile
l'audit, quindi sconsigliata.

In tutte le opzioni vale l'invariante del principio 3: **lo snapshot consumato da
un run è immutabile**. Una `master_prompt_versions` è append-only via trigger
`reject_modify_append_only()`; uno snapshot inline su `orchestration_runs` è
immutabile perché `orchestration_runs` è esso stesso append-only nei campi di
fatto (vedi §8).

---

## 7. AgentConfig, AgentRolePrompt e snapshot

### 7.1 `agent_role_prompts` — ruolo e prompt, catalogo versionabile

`agent_role_prompts` rappresenta il **ruolo e il prompt specifico** assegnabili a
un agente: *che cosa* l'agente deve fare e *con quali istruzioni*. È separato da
`agent_configs` perché un ruolo/prompt può essere riusabile o versionabile
indipendentemente dai parametri operativi del singolo agente (provider, modello,
budget).

**Campi probabili:**

- `id` — UUID, PK.
- `tenant_id` — UUID NOT NULL, FK `tenants`.
- `name` / `label` — TEXT, nome del ruolo.
- `role_category` — TEXT con CHECK, per esempio `∈ {researcher, critic,
  synthesizer, generic}`.
- `system_prompt_text` — TEXT, il prompt di sistema.
- `task_prompt_text` — TEXT, il prompt di compito (può essere parametrico
  rispetto al MasterPrompt).
- `version_no` — INTEGER, per versionamento append-only del catalogo.
- `created_at` — TIMESTAMPTZ.

**Mutabilità.** Il *catalogo* dei ruoli/prompt è configurazione mutabile: si
possono aggiungere ruoli, e una nuova revisione di un ruolo è una nuova riga con
`version_no` incrementato (append-only sul catalogo). La **versione consumata da
un run** è immutabile: una volta che un run ha consumato un `agent_role_prompts`,
quel testo non deve cambiare per quel run.

### 7.2 `agent_configs` — configurazione di un agente

`agent_configs` rappresenta la configurazione di un agente AI: il "chi" e il
"come" di un partecipante all'orchestrazione. È l'entità che l'utente compila
quando aggiunge un agente.

> **Nota di naming.** Come dichiarato in §3.3 e §5.1, il nome `agent_configs`
> non collide con tabelle `0005`, ma le entità vicine `agent_runs` e
> `agent_outputs` sì. ORCH-SCHEMA-A potrà decidere un prefisso coerente per
> l'intera famiglia (per esempio `orchestration_agent_configs`,
> `orchestration_agent_runs`, `orchestration_agent_outputs`) per evitare
> ambiguità. In questa sezione si usa `agent_configs` per leggibilità.

**Campi probabili:**

- `id` — UUID, PK.
- `tenant_id` — UUID NOT NULL, FK `tenants`.
- `master_prompt_id` — UUID NOT NULL, FK `master_prompts` (l'agente è configurato
  per un master prompt; in alternativa, FK verso un contenitore di orchestrazione
  se ORCH-SCHEMA-A introduce un livello intermedio).
- `name` — TEXT, nome leggibile dell'agente.
- `provider` — TEXT, riferimento all'astrazione di provider; in MVP-0 l'unico
  valore operativo è `mock`.
- `model` — TEXT, identificativo del modello presso il provider; campo opaco a
  livello di schema.
- `agent_role_prompt_id` — UUID, FK `agent_role_prompts` (la versione di ruolo
  associata).
- `task_summary` — TEXT, compito sintetico dell'agente.
- `output_contract` — TEXT/JSONB, forma attesa dell'output (testo libero, lista
  di affermazioni, formato strutturato).
- `constraints` — JSONB, vincoli (formato, lunghezza, divieti).
- `token_budget` — riferimento o valore del budget per-agente (vedi §11 e
  `token_budgets`).
- `temperature_config` — JSONB, parametri di campionamento, opaco a livello di
  schema.
- `retry_policy` — JSONB, politica di retry (max tentativi, backoff).
- `source_access` — TEXT/JSONB, quali fonti l'agente può vedere.
- `reviewer_flag` — BOOLEAN, l'agente svolge un ruolo di reviewer.
- `synthesizer_flag` — BOOLEAN, l'agente svolge un ruolo di synthesizer.
- `order_index` / `priority` — INTEGER, ordine o priorità nell'orchestrazione.
- `created_at`, `updated_at` — TIMESTAMPTZ.

**Mutabilità.** Tutti i campi di `agent_configs` sono **configurazione
mutabile** finché nessun `OrchestrationRun` ha consumato l'agente.

### 7.3 `agent_config_snapshots` — snapshot immutabile al run start

Quando un `OrchestrationRun` parte, deve **fissare uno snapshot immutabile della
configurazione di ogni agente** che partecipa al run. `agent_config_snapshots` è
la tabella append-only che materializza questi snapshot.

**Campi probabili:**

- `id` — UUID, PK.
- `orchestration_run_id` — UUID NOT NULL, FK `orchestration_runs`.
- `agent_config_id` — UUID NOT NULL, FK `agent_configs` (riferimento alla config
  di origine, per tracciabilità).
- `snapshot_payload` — JSONB NOT NULL, copia integrale e immobilizzata della
  configurazione dell'agente al momento dell'avvio del run: provider, model,
  ruolo + testo dei prompt (system e task) effettivamente usati, output
  contract, constraints, budget, flag reviewer/synthesizer, parametri.
- `agent_role_prompt_text_hash` — TEXT, hash del prompt di ruolo consumato, come
  campo di verifica denormalizzato.
- `created_at` — TIMESTAMPTZ.

`agent_config_snapshots` è **append-only** (trigger `reject_modify_append_only()`):
una volta scritto, lo snapshot non cambia. Un `AgentRun` (vedi §10) consuma uno
`agent_config_snapshots`, non un `agent_configs` "live".

**Relazione con la provider abstraction futura.** Lo snapshot registra `provider`
e `model` come stringhe opache. Quando, in una fase futura, l'astrazione di
provider verrà implementata (fasi `ORCH-PROVIDER-PRE` / `ORCH-PROVIDER-A` della
roadmap), le invocazioni concrete verranno tracciate in `provider_invocations`
(vedi §5 e §14): lo snapshot resta il contratto di "cosa il run intendeva
usare", `provider_invocations` registra "cosa è stato realmente invocato". Le due
cose non vanno confuse.

### 7.4 Cosa NON fare

- **Non salvare solo un riferimento mutabile.** Un `AgentRun` non deve
  referenziare direttamente `agent_configs` come unica fonte della
  configurazione usata: se la config cambiasse dopo il run, l'audit del run
  diventerebbe incoerente. Il run consuma uno `agent_config_snapshots`
  immutabile.
- **Non perdere il prompt esatto usato nel run.** Lo snapshot deve contenere il
  *testo* dei prompt (system e task) effettivamente inviati, non solo un
  riferimento a `agent_role_prompts`. Se il catalogo dei ruoli evolvesse, il run
  deve restare auditabile rispetto al testo che ha davvero usato.
- **Non fingere un provider reale se è mock.** In MVP-0 `provider='mock'`. Lo
  schema non deve avere campi che suggeriscano che un agente abbia parlato a un
  provider reale quando non è così. Coerentemente con i mock indicators già
  adottati dall'Anti-Hallucination Report, ogni snapshot e ogni invocazione
  devono poter dichiarare la propria natura mock. Lo schema deve prevedere un
  flag o un campo (per esempio dentro `snapshot_payload` o su
  `provider_invocations`) che renda esplicito che l'esecuzione è mock-driven.

---

## 8. OrchestrationRun

`orchestration_runs` è la **radice** di una singola esecuzione di orchestrazione
multi-AI: l'entità che lega un MasterPrompt (snapshot), gli agenti configurati
(snapshot), e tutto ciò che accade da Agent Outputs fino a Published/Held. È
l'analogo, per la linea multi-AI, di ciò che `task_masters` è per la linea
closed-corpus.

`orchestration_runs` registra un **fatto** ed è quindi **append-only nei suoi
campi di fatto**: la configurazione (master prompt snapshot, agent config
snapshots, parametri di bounding) è fissata all'avvio e non cambia; le transizioni
di stato sono registrate come `orchestration_events` (vedi §9) e/o come campi
terminali scritti una sola volta.

**Campi probabili:**

- `id` — UUID, PK.
- `tenant_id` — UUID NOT NULL, FK `tenants`, ON DELETE RESTRICT.
- `project_id` — UUID nullable, FK `projects`, ON DELETE RESTRICT (se la nozione
  di progetto è mantenuta).
- `master_prompt_snapshot_id` — UUID NOT NULL, riferimento allo snapshot
  immutabile del prompt consumato (una `master_prompt_versions` per l'Opzione A
  di §6.2; in alternativa lo snapshot è inline). FK ON DELETE RESTRICT.
- `mode` — TEXT NOT NULL con CHECK, la modalità di orchestrazione di prodotto.
  Codominio proposto coerente con `PHASE_PRODUCT_ORCHESTRATION_PRE.md §3.5`:
  `∈ {multi_ai_orchestration, local_evidence, hybrid}`. In alternativa, una
  distinzione più fine fra modalità di esecuzione (`independent` / `coordinated`)
  può essere un campo separato `execution_mode`; la sezione 17
  chiarisce se servono uno o due campi.
- `status` — TEXT NOT NULL con CHECK, lo stato complessivo del run (codominio
  sotto).
- `bounding_parameters` — JSONB, i parametri di bounding dell'orchestrazione
  (numero massimo di passi di confronto, pass abilitati, ecc.). Fissati come
  snapshot all'avvio.
- `started_at` — TIMESTAMPTZ, momento di avvio.
- `completed_at` — TIMESTAMPTZ nullable, momento di completamento.
- `failure_reason` — TEXT nullable, motivo di fallimento se `status='failed'`.
- `idempotency_key` — TEXT NOT NULL, chiave di idempotenza per la creazione del
  run (coerente con `Idempotency-Key` di `POST /api/v1/tasks`).
- `policy_version` — TEXT, identità della policy di orchestrazione adottata, come
  coppia opaca `(policy_name, policy_version)` sul modello di
  `source_quality_assessments` e `claim_entailment_checks`.
- `total_tokens` / `total_cost` — aggregati di token e costo del run, calcolati
  dai `token_usage_records`; possono essere campi denormalizzati scritti al
  completamento, oppure derivati a read time (decisione di ORCH-SCHEMA-A).
- `mock_indicators` / `is_mock` — BOOLEAN o JSONB, indicatori che dichiarano che
  il run è mock-driven (in MVP-0 sempre true).
- `final_gate_report_id` — UUID nullable, riferimento all'eventuale
  `final_gate_reports` prodotto dalla catena di integrazione (vedi §16).
- `created_at` — TIMESTAMPTZ.

**Stati proposti** (codominio del CHECK su `status`):

- `pending` — il run è stato creato ma non è ancora partito.
- `running` — gli agent run sono in esecuzione.
- `waiting_source_resolution` — il run è in attesa che le source candidate
  vengano recuperate/risolte/verificate.
- `synthesizing` — il run sta eseguendo il synthesis pass.
- `submitted_to_gate` — la candidate synthesis è stata sottoposta alla catena di
  integrazione (Claim Extraction → Evidence Binding → Final Answer Gate).
- `completed` — il run è terminato con esito (published o held).
- `failed` — il run è terminato per errore.
- `cancelled` — il run è stato annullato.

**Transizioni di stato.** Coerentemente con il principio 4 e 5, le transizioni di
stato di un `orchestration_runs` **non** devono essere riscritture silenziose del
campo `status`. Sono ammesse due rappresentazioni:

- la rappresentazione **primaria e raccomandata** è un record append-only in
  `orchestration_events` (§9) per ogni transizione (`run_created`,
  `agent_run_started`, …, `run_failed`);
- il campo `status` su `orchestration_runs` può esistere come **vista comoda
  dello stato corrente**, ma in tal caso ORCH-SCHEMA-A deve decidere come
  conciliarlo con l'append-only: o `orchestration_runs` non è append-only sul
  solo campo `status` (e allora `status` è derivabile e ridondante rispetto agli
  eventi), oppure lo stato corrente è interamente derivato dall'ultimo
  `orchestration_events` e `orchestration_runs` non porta affatto un campo
  `status` mutabile.

La raccomandazione di questo documento è: **lo stato
corrente del run è derivato dagli `orchestration_events`**; `orchestration_runs`
porta solo campi fissati all'avvio (config, idempotency, snapshot) e campi
terminali scritti una sola volta (`completed_at`, `failure_reason`). Questo
mantiene `orchestration_runs` interamente append-only e fa degli eventi l'unica
fonte di verità delle transizioni, coerentemente con l'audit chain hash-linked
già esistente. Se invece ORCH-SCHEMA-A preferisse un campo `status` materializzato
per ergonomia di query, allora quel singolo campo è l'unica eccezione all'append-only
e ogni sua transizione deve comunque generare un `orchestration_events`
corrispondente — mai un update senza evento.

---

## 9. OrchestrationEvents

`orchestration_events` è un **log append-only** di eventi di orchestrazione: la
registrazione, riga per riga, di tutto ciò che accade durante un
`OrchestrationRun`. È l'entità che rende un run ispezionabile e che alimenta la
futura UI progress senza che la UI debba ricalcolare nulla.

`orchestration_events` è **append-only** (trigger `reject_modify_append_only()`).
Ogni evento è un fatto: una volta scritto, non si modifica e non si cancella.

**Campi probabili:**

- `id` — UUID, PK.
- `orchestration_run_id` — UUID NOT NULL, FK `orchestration_runs`, ON DELETE
  RESTRICT.
- `event_type` — TEXT NOT NULL con CHECK (codominio sotto).
- `sequence_no` — INTEGER, ordinamento monotòno degli eventi dentro il run.
- `related_entity_type` / `related_entity_id` — riferimenti opzionali all'entità
  toccata dall'evento (un `agent_runs`, un `source_candidates`, una
  `candidate_syntheses`, ecc.).
- `event_payload` — JSONB, dettaglio strutturato dell'evento.
- `idempotency_key` — TEXT, per assorbire redelivery.
- `created_at` — TIMESTAMPTZ.

**Tipi di evento proposti** (codominio del CHECK su `event_type`):

- `run_created` — il run è stato creato.
- `agent_run_started` — un agent run è partito.
- `agent_run_completed` — un agent run è terminato con successo.
- `agent_run_failed` — un agent run è fallito.
- `source_candidate_created` — un agente (o l'utente, o il sistema) ha proposto
  una source candidate.
- `source_resolution_started` — è iniziato il recupero/risoluzione di una source
  candidate.
- `source_resolution_completed` — il recupero/risoluzione è terminato (con esito
  successo o fallimento dentro il payload).
- `source_verification_completed` — la verifica di una fonte risolta è terminata.
- `synthesis_created` — è stata prodotta una candidate synthesis.
- `submitted_to_gate` — la candidate synthesis è stata sottoposta alla catena di
  integrazione.
- `gate_completed` — il Final Answer Gate ha prodotto la sua decisione.
- `run_failed` — il run è terminato per errore.

ORCH-SCHEMA-A potrà aggiungere ulteriori tipi (per esempio `cross_review_started`
/ `cross_review_completed`, `token_budget_exceeded`, `run_cancelled`); il
codominio sopra è il minimo coerente con le sezioni 8, 10, 11, 12.

**Idempotenza.** Un UNIQUE composito su `(orchestration_run_id, event_type,
idempotency_key)` — o su `(orchestration_run_id, sequence_no)` — impedisce che un
redelivery dell'evento di pipeline duplichi una riga. Pattern coerente con
`pale_idempotency_uq` di `0006` e con `cec_entry_span_idem_uq` di `0009`.

**Come gli eventi alimentano la UI progress senza ricalcolo.** Il Run progress
panel descritto in `PHASE_PRODUCT_ORCHESTRATION_PRE.md §8.1` è una **vista
derivata read-only**. Con `orchestration_events` come log append-only, la UI
ottiene lo stato di avanzamento di un run **leggendo gli eventi in ordine di
`sequence_no`** e proiettandoli in una timeline: quali agent run sono partiti,
quali completati, quali falliti, quante source candidate sono state proposte e a
che punto è il loro recupero, se la synthesis è stata creata, se il gate ha
deciso. La UI **non ricalcola** lo stato del run: lo *legge* dagli eventi. Questo
è esattamente il pattern già adottato dall'Anti-Hallucination Report e dai
guardrail UI (`PHASE_PRODUCT_ORCHESTRATION_PRE.md §13.3`): la UI è una vista
derivata, non prende nuove decisioni e non ricompone uno stato. Un futuro
endpoint `GET /api/v1/orchestration-runs/{id}/events` (anticipato in
`PHASE_PRODUCT_ORCHESTRATION_PRE.md §17`) esporrà questo log, e la UI lo
consumerà direttamente.

---

## 10. AgentRun, AgentMessage, AgentOutput

Questa sezione descrive le tre entità che registrano l'esecuzione concreta di un
singolo agente dentro un `OrchestrationRun`. Tutte e tre sono **fatti
append-only**.

> **Nota di naming (ripetuta).** I nomi `agent_runs` e `agent_outputs` collidono
> con le tabelle placeholder di `0005` (vedi §3.3 e §5.1). ORCH-SCHEMA-A dovrà
> risolvere la collisione, preferibilmente con un prefisso (`orchestration_agent_runs`,
> `orchestration_agent_outputs`). In questa sezione si usano i nomi `agent_*`
> per coerenza con il prompt operativo.

### 10.1 `agent_runs` (multi-AI) — esecuzione concreta di un agente

`agent_runs` rappresenta l'**esecuzione concreta di un singolo agente**: l'istanza
in cui un `agent_config_snapshots` viene attivato e produce output.

**Campi probabili:**

- `id` — UUID, PK.
- `orchestration_run_id` — UUID NOT NULL, FK `orchestration_runs`, ON DELETE
  RESTRICT.
- `agent_config_snapshot_id` — UUID NOT NULL, FK `agent_config_snapshots` (il run
  consuma lo snapshot immutabile, non la config live — vedi §7.3 e §7.4).
- `status` — TEXT con CHECK, per esempio `∈ {pending, running, succeeded,
  failed}`.
- `attempt_no` — INTEGER, numero del tentativo (vedi nota sui retry).
- `tokens_consumed` / `cost` — aggregati di consumo, oppure derivati dai
  `token_usage_records` collegati.
- `started_at`, `completed_at` — TIMESTAMPTZ.
- `error_code` / `failure_reason` — TEXT nullable, per gli esiti falliti.
- `is_mock` — BOOLEAN, indicatore che l'esecuzione è mock-driven.
- `created_at` — TIMESTAMPTZ.

`agent_runs` è **append-only**. Le transizioni di stato del singolo agent run
sono registrate come `orchestration_events` (`agent_run_started`,
`agent_run_completed`, `agent_run_failed`); valgono per `agent_runs` le stesse
considerazioni della §8 sul campo `status` derivato vs materializzato.

**Retry.** Un retry **non** è un update di un `agent_runs` fallito. Coerentemente
col principio 4, un nuovo tentativo è un **nuovo `agent_runs`** (con `attempt_no`
incrementato e riferimento, se utile, all'agent run precedente), oppure un
tentativo tracciato come evento/record append-only collegato. La retry policy
dell'agente (campo `retry_policy` di `agent_configs`, §7.2) governa quanti
tentativi sono ammessi; ogni tentativo, riuscito o fallito, è un fatto
auditabile. ORCH-SCHEMA-A deciderà se modellare i retry come `agent_runs`
distinti con `attempt_no` o come una tabella `continuation_attempts` dedicata
(potenziale riuso della placeholder `0005` omonima); la raccomandazione di questo
documento è la prima opzione (un `agent_runs` per tentativo), più semplice e
coerente con `agent_runs_attempt_uq` già presente in `0005`.

### 10.2 `agent_messages` — input/output a livello provider

`agent_messages` rappresenta i **messaggi scambiati o prodotti a livello
provider** durante un `agent_runs`: la richiesta inviata al provider, la risposta
ricevuta, eventuali messaggi di confronto. È il livello più fine di audit di un
run.

**Campi probabili:**

- `id` — UUID, PK.
- `agent_run_id` — UUID NOT NULL, FK `agent_runs`, ON DELETE RESTRICT.
- `orchestration_run_id` — UUID nullable, FK `orchestration_runs` (denormalizzato
  per query ergonomiche, opzionale).
- `message_role` — TEXT con CHECK, per esempio `∈ {system, user, assistant,
  review, tool}`.
- `content_text` — TEXT, il contenuto del messaggio.
- `content_hash` — TEXT, hash del contenuto.
- `sequence_no` — INTEGER, ordinamento dei messaggi dentro l'agent run.
- `tokens` — INTEGER nullable, token associati al messaggio.
- `created_at` — TIMESTAMPTZ.

`agent_messages` è **append-only**. I messaggi non vanno mai riscritti né
cancellati: sono il materiale grezzo che rende un'orchestrazione ispezionabile.

### 10.3 `agent_outputs` (multi-AI) — risultato strutturato consumabile

`agent_outputs` rappresenta l'**output reale e strutturato** prodotto da un
agente: il risultato di un `agent_runs`, nella forma che le fasi successive
(Cross Review, Candidate Synthesis) consumano.

**Campi probabili:**

- `id` — UUID, PK.
- `agent_run_id` — UUID NOT NULL, FK `agent_runs`, ON DELETE RESTRICT.
- `output_kind` / `format` — TEXT, tipo o formato dell'output (testo libero,
  lista di affermazioni, formato strutturato — coerente con `output_contract` di
  `agent_configs`).
- `content_text` — TEXT, il contenuto dell'output.
- `content_hash` — TEXT, hash del contenuto.
- `structured_payload` — JSONB nullable, metadati strutturati se l'output è una
  lista di affermazioni o un formato strutturato.
- `tokens` — INTEGER nullable, token dell'output.
- `created_at` — TIMESTAMPTZ.

`agent_outputs` è **append-only**, coerente con la natura append-only di
`final_answer_spans` e `published_answers`. Un output registrato è un fatto: non
si riscrive. Una nuova versione di output corrisponde a un nuovo `agent_runs`
(nuovo tentativo).

**Relazione con le source candidate.** Un `agent_outputs` può portare con sé un
insieme di **source candidate** proposte o citate dall'agente. Il legame fra un
`agent_outputs` e le sue `source_candidates` è descritto in §11: ogni
`source_candidates` di tipo `agent_cited` referenzia l'`agent_output_id` da cui
proviene.

### 10.4 Errori provider, mock indicators, token usage

- **Errori provider.** Un fallimento di invocazione (timeout, errore del
  provider, rate limit) è registrato sull'`agent_runs` come `status='failed'` +
  `error_code`/`failure_reason`, e come `orchestration_events`
  (`agent_run_failed`). Un eventuale dettaglio dell'errore a livello di
  invocazione vive su `provider_invocations` (vedi §14). Coerentemente col
  principio 4, un errore non viene mascherato: è un fatto registrato.
- **Mock indicators.** Finché `provider='mock'`, ogni `agent_runs` e ogni
  `agent_messages`/`agent_outputs` derivano da esecuzione mock-driven. Lo schema
  deve permettere di dichiararlo (campo `is_mock` o equivalente), coerentemente
  con i mock indicators dell'Anti-Hallucination Report. Lo schema **non** deve
  far sembrare reale un'esecuzione mock.
- **Token usage collegato.** Il consumo di token di un `agent_runs` è
  registrato in `token_usage_records` (vedi §5 e §13), collegati
  all'`agent_run_id`. L'aggregato per agent run e per orchestration run è
  derivabile da questi record.

**Non implementare provider reali.** Questa fase, e lo schema che progetta, non
introducono provider AI reali. `provider` resta `mock` in MVP-0. Lo schema è
disegnato perché, quando un provider reale o un modello locale verrà integrato
(fasi `ORCH-PROVIDER-A` e successive), non serva alterare le tabelle: cambierà il
valore di `provider`/`model`, si popoleranno `provider_invocations` e
`token_usage_records` con dati reali, e i mock indicators passeranno da true a
false. Lo schema deve rendere questo passaggio un cambiamento di dato, non di
struttura.

---

## 11. SourceCandidate

`source_candidates` è una delle entità **centrali** dello schema di orchestrazione,
e quella che esprime la correzione di prodotto introdotta da
`PRODUCT-ORCHESTRATION-PRE-FIX-A`: le fonti proposte dagli agenti **non sono
evidenze**, sono *candidati* da recuperare e verificare.

`source_candidates` rappresenta una **fonte proposta** per un MasterPrompt o per
un OrchestrationRun. La fonte può essere proposta da un agente AI, fornita
dall'utente, o recuperata dal sistema.

`source_candidates` è un **fatto append-only**: la proposta di una fonte è un
fatto accaduto. Lo stato della candidate (proposed → resolved → verified …)
evolve, ma — coerentemente col principio 4 — l'evoluzione di stato non è una
riscrittura silenziosa: o è registrata come eventi/versioni, o le tappe
(risoluzione, verifica) vivono in tabelle separate (`source_resolutions`,
`source_verifications`, §12) che si limitano ad appendere fatti, lasciando alla
`source_candidates` un campo di stato derivabile dall'ultima tappa. La
raccomandazione è far derivare lo stato della candidate dalle sue
resolution/verification append-only.

**`candidate_type`** — TEXT NOT NULL con CHECK. Codominio proposto:

- `agent_cited` — fonte proposta o citata da un agente AI;
- `user_supplied` — fonte caricata o indicata dall'utente (le fonti utente
  opzionali);
- `system_retrieved` — fonte recuperata dal sistema;
- `internal` — fonte interna/locale;
- `future_web` — fonte web, riservata a una capacità non ancora disponibile.

**`status`** — TEXT NOT NULL con CHECK (o stato derivato; vedi sopra). Codominio
proposto:

- `proposed` — la candidate è stata proposta, nessuna azione ancora intrapresa;
- `resolution_pending` — il recupero/risoluzione è in coda;
- `resolved` — la fonte reale è stata recuperata/risolta;
- `resolution_failed` — il recupero/risoluzione è fallito;
- `verification_pending` — la verifica è in coda;
- `verified_as_retrieved` — la fonte risolta è stata verificata (presenza,
  quote, hash) e ha prodotto evidence span;
- `rejected` — la candidate è stata scartata;
- `insufficient_metadata` — la candidate non ha metadati sufficienti per essere
  recuperata o risolta.

**Campi probabili:**

- `id` — UUID, PK.
- `tenant_id` — UUID NOT NULL, FK `tenants`.
- `orchestration_run_id` — UUID nullable, FK `orchestration_runs` (la candidate
  nasce nel contesto di un run; per le fonti utente caricate prima di un run,
  può essere collegata al `master_prompt_id`).
- `master_prompt_id` — UUID nullable, FK `master_prompts`.
- `candidate_type` — TEXT NOT NULL con CHECK (codominio sopra).
- `status` — TEXT con CHECK (codominio sopra), oppure derivato.
- `title` — TEXT nullable, titolo dichiarato della fonte.
- `url` — TEXT nullable, URL dichiarato della fonte.
- `citation_text` — TEXT nullable, il testo della citazione così come prodotto.
- `quoted_text` — TEXT nullable, l'eventuale quote che l'agente attribuisce alla
  fonte.
- `agent_output_id` — UUID nullable, FK `agent_outputs`, popolato quando
  `candidate_type='agent_cited'`: l'output dell'agente da cui la candidate
  proviene.
- `declared_confidence` — DOUBLE PRECISION nullable, la confidenza dichiarata
  dall'agente sulla fonte, se presente. **Non è una misura affidabile del
  supporto, della qualità della fonte o della pubblicabilità**: è un dato
  dichiarato dall'agente, da trattare con cautela.
- `provenance` — TEXT/JSONB, la provenienza della candidate (chi/cosa l'ha
  proposta, attraverso quale percorso).
- `created_by` — TEXT/UUID nullable, l'attore che ha creato la candidate (un
  agente, l'utente, il sistema).
- `raw_citation_payload` — JSONB, il payload grezzo della citazione così come
  ricevuto, per audit e per ricostruzione.
- `created_at` — TIMESTAMPTZ.

**`source_candidate` NON è `evidence_span`.** Questo è l'invariante centrale
della sezione, e va ribadito esplicitamente nello schema e in tutta la
documentazione futura:

- una `source_candidates` è una fonte *proposta*; un `evidence_span` è un
  estratto *verificato* di un documento reale;
- una `source_candidates` non ha (e non deve avere) un `quote_hash` verificato:
  il campo `quoted_text` è ciò che l'agente *afferma*, non ciò che è stato
  *verificato presente* in un chunk;
- un `evidence_span` nasce **solo** dopo che una `source_candidates` è stata
  risolta (`source_resolutions`) e verificata (`source_verifications`, §12);
- lo schema non deve avere una FK diretta che colleghi `source_candidates` a
  `claim_evidence_links` o che permetta a una candidate di partecipare al gate
  prima della verifica. Il ponte verso il Claim Ledger passa obbligatoriamente
  per `source_verifications` → `evidence_spans` → `claim_evidence_links`.

Trattare una `source_candidates` come se fosse evidence — saltare resolution e
verification — equivarrebbe a fidarsi della citazione di un modello senza
controllarla, ed è esattamente ciò che il prodotto evidence-first non fa.

---

## 12. SourceResolution e SourceVerification

Le source candidate diventano evidenze utilizzabili solo attraverso due tappe
distinte e append-only: **risoluzione** (recuperare la fonte reale) e
**verifica** (controllare presenza/quote/hash ed estrarre evidence span).

### 12.1 `source_resolutions` — recupero/risoluzione della fonte reale

`source_resolutions` rappresenta un **tentativo di recuperare o risolvere la
fonte reale** indicata da una `source_candidates`: trasformare una citazione, un
URL o un riferimento bibliografico in un artefatto concreto e ingeribile.

`source_resolutions` è un **fatto append-only**: ogni tentativo di risoluzione è
un fatto. Un nuovo tentativo è una nuova riga, non un update.

**Campi probabili:**

- `id` — UUID, PK.
- `source_candidate_id` — UUID NOT NULL, FK `source_candidates`, ON DELETE
  RESTRICT.
- `orchestration_run_id` — UUID nullable, FK `orchestration_runs` (denormalizzato
  per query).
- `resolution_target_kind` — TEXT con CHECK, il tipo di fonte verso cui si
  risolve: per esempio `∈ {url, web_page, internal_document, uploaded_document,
  retrieved_document}`.
- `outcome` — TEXT NOT NULL con CHECK, l'esito della risoluzione: per esempio
  `∈ {resolved, failed, partial, unreachable, not_found}`.
- `failure_reason` — TEXT nullable, motivo del fallimento.
- `retrieved_artifact_ref` — TEXT/UUID nullable, riferimento all'artefatto
  recuperato: idealmente un `uploaded_documents` / `document_versions` /
  `storage_objects` esistente quando la fonte risolve a un documento ingerito,
  così l'evidence extraction può riusare la pipeline di chunking e
  `evidence_spans` già esistente.
- `retrieved_artifact_hash` — TEXT nullable, hash del contenuto recuperato, per
  tracciabilità e per il confronto in fase di verifica.
- `idempotency_key` — TEXT, per assorbire redelivery.
- `created_at` — TIMESTAMPTZ.

### 12.2 `source_verifications` — verifica della fonte risolta

`source_verifications` rappresenta la **verifica** di una fonte risolta: il
controllo che la quote dichiarata sia effettivamente presente nel contenuto
recuperato e che gli hash corrispondano, e l'**estrazione dell'evidence span**
verificato.

`source_verifications` è un **fatto append-only**.

**Campi probabili:**

- `id` — UUID, PK.
- `source_candidate_id` — UUID NOT NULL, FK `source_candidates`, ON DELETE
  RESTRICT.
- `source_resolution_id` — UUID NOT NULL, FK `source_resolutions`, ON DELETE
  RESTRICT (la verifica opera su una risoluzione riuscita).
- `verification_outcome` — TEXT NOT NULL con CHECK, l'esito: per esempio
  `∈ {quote_present, quote_absent, hash_mismatch, inconclusive}`.
- `evidence_span_id` — UUID nullable, FK `evidence_spans`, ON DELETE RESTRICT:
  popolato quando la verifica riesce e produce (o aggancia) un evidence span
  reale. Questo è il punto in cui la catena multi-AI rientra nel mondo degli
  `evidence_spans` già esistenti.
- `document_version_id` / `document_chunk_id` — UUID nullable, FK verso
  `document_versions` / `document_chunks`, quando la fonte risolta è stata
  ingerita come documento.
- `quote_hash_checked` — TEXT nullable, l'hash effettivamente verificato.
- `failure_reason` — TEXT nullable.
- `idempotency_key` — TEXT.
- `created_at` — TIMESTAMPTZ.

**`source_verifications` non prova il supporto semantico.** La verifica controlla
**presenza e hash** della quote nella fonte risolta — esattamente la natura di
CVE-lite (`verification_records`, `check_kind='cve_lite'`): la quote è
testualmente presente e l'hash corrisponde. La verifica **non** stabilisce che la
quote *sostenga* il claim: quella è la competenza dell'asse Claim Entailment
(`claim_entailment_checks`), che opera più a valle, dopo l'Evidence Binding. Lo
schema deve mantenere questa separazione: `source_verifications` produce o
aggancia un `evidence_span`, e nient'altro; non emette verdetti di entailment, non
giudica la qualità della fonte (asse Source Quality), non giudica la verità del
claim.

**Relazione con `document_versions` / `document_chunks` / `evidence_spans`.** Lo
schema raccomanda fortemente che, quando una `source_resolutions` recupera una
fonte che può essere ingerita come documento, l'artefatto venga normalizzato
nelle tabelle di documento già esistenti (`uploaded_documents`,
`document_versions` `parsed`, `document_chunks`, `evidence_spans`). In questo modo
`source_verifications` può **agganciare** un `evidence_span` reale invece di
inventare una rappresentazione parallela delle evidenze. Questo è il principio di
riuso descritto in `PHASE_PRODUCT_ORCHESTRATION_PRE.md §4.3` e §13: la metà destra
del flusso (da Claim Extraction in poi) esiste già e va *alimentata*, non
riscritta.

### 12.3 Il flusso source candidate → evidence span → claim

Il flusso completo, che lo schema deve rendere strutturalmente naturale e che
nessun percorso deve poter scavalcare:

```
source_candidate
   ▼  (un agente / l'utente / il sistema propone una fonte — NON è evidence)
source_resolution
   ▼  (il sistema recupera o risolve la fonte reale; esito append-only)
source_verification
   ▼  (verifica presenza/quote/hash; estrae o aggancia un evidence span)
evidence_span
   ▼  (estratto verificato di un documento reale — entità già esistente)
claim_evidence_link
   ▼  (collegamento claim ↔ evidence_span — tabella già esistente, 0004)
[CVE-lite → Source Quality → Claim Entailment → Final Answer Gate]
```

Solo gli `evidence_span` prodotti o agganciati da una `source_verifications`
riuscita possono entrare in `claim_evidence_links` e quindi partecipare al gate.
Una `source_candidates` ferma a `proposed`, `resolution_failed`,
`insufficient_metadata` o `rejected` **non** ha un evidence span e **non** può
contribuire a una pubblicazione. Lo schema, per costruzione (assenza di FK dirette
candidate→evidence/claim, presenza obbligatoria della catena
resolution→verification→evidence_span), deve rendere impossibile saltare le
tappe.


---

## 13. TokenBudget e TokenUsageRecord

Questa sezione progetta le due entità che governano il consumo di token e di
costo di un'orchestrazione multi-AI: `token_budgets`, che è **configurazione**
(un limite fissato prima del run), e `token_usage_records`, che è un **fatto
append-only** (il consumo realmente avvenuto). La separazione fra le due è
un'applicazione diretta del principio 8 (separazione tra config e run facts) e
del principio 1 (append-only per i fatti accaduti): un budget è un'intenzione, un
consumo è un fatto, e i due non vanno confusi né mescolati in una sola tabella.

La strategia di budget di cui queste entità sono il supporto di schema è
descritta a livello di prodotto in `PHASE_PRODUCT_ORCHESTRATION_PRE.md §11`
(budget globale, budget per-agente, prompt packing, evidence compression,
summarization checkpoints, truncation policy, audit dei tagli, prevenzione del
context explosion, gestione retry, gestione output troppo lunghi). Lo schema qui
proposto non implementa quella strategia: fornisce le tabelle su cui la strategia
poggerà.

### 13.1 `token_budgets` — configurazione del limite, prima del run

`token_budgets` rappresenta il **budget di token (e, per estensione, di costo)**
associato a un livello dell'orchestrazione: l'intera orchestrazione, un singolo
agente, oppure — se ORCH-SCHEMA-A lo riterrà utile — un singolo pass.

`token_budgets` è **configurazione mutabile** finché un run non l'ha consumato.
Coerentemente col principio 2, il limite configurato può essere modificato prima
dell'avvio di un `OrchestrationRun`; una volta che un run lo consuma, il valore
che quel run ha usato deve restare immutabile per quel run (vedi §13.3, snapshot
del budget).

**Livelli di budget.** Lo schema deve poter rappresentare tre granularità di
budget, coerenti con `PHASE_PRODUCT_ORCHESTRATION_PRE.md §11.1` e §11.2:

- **budget globale del run** — il tetto complessivo di token/costo che l'intera
  esecuzione non deve superare;
- **budget per agente** — un sottoinsieme del budget globale, per evitare che un
  singolo agente consumi una quota sproporzionata;
- **budget per pass** — opzionale, un limite sul singolo pass di orchestrazione
  (independent answers, reviewer, critic, synthesis, optional second check). La
  granularità per-pass è la meno critica per MVP-0 e ORCH-SCHEMA-A può decidere
  di rinviarla.

**Campi probabili:**

- `id` — UUID, PK.
- `tenant_id` — UUID NOT NULL, FK `tenants`, ON DELETE RESTRICT.
- `budget_level` — TEXT NOT NULL con CHECK, il livello a cui il budget si
  applica: `∈ {per_orchestration, per_agent, per_pass}`.
- `master_prompt_id` — UUID nullable, FK `master_prompts`: presente quando il
  budget è definito come configurazione associata a un master prompt prima
  dell'avvio di un run.
- `agent_config_id` — UUID nullable, FK `agent_configs`: presente quando
  `budget_level='per_agent'` e il budget è la configurazione di un agente
  specifico.
- `orchestration_run_id` — UUID nullable, FK `orchestration_runs`: presente
  quando il budget è già legato a un run concreto (vedi §13.3).
- `token_limit` — BIGINT NOT NULL, il limite di token.
- `cost_limit` — DOUBLE PRECISION nullable, l'eventuale limite di costo stimato.
  In MVP-0, con `MAX_COST_PER_TASK=0` e provider mock, il costo è zero o
  simulato; il campo esiste per il futuro, non per dichiarare un costo reale
  oggi.
- `overflow_policy` — TEXT NOT NULL con CHECK, la politica di superamento:
  `∈ {hard_stop, warn}`. `hard_stop` significa che, raggiunto il limite,
  l'orchestrazione si ferma in modo controllato; `warn` significa che il
  superamento viene segnalato come evento ma l'esecuzione prosegue (tipicamente
  riservato a budget per-pass non critici).
- `created_at`, `updated_at` — TIMESTAMPTZ.

Esattamente uno fra `agent_config_id` e gli altri riferimenti deve essere
coerente con `budget_level` (per esempio, un `per_agent` budget deve avere
`agent_config_id` NOT NULL). ORCH-SCHEMA-A può enforced questa coerenza con un
CHECK condizionale, sul modello di `sqa_target_xor` di `0007`.

**Hard stop vs warn.** La distinzione `hard_stop` / `warn` è un campo di
configurazione, non un comportamento implicito. Lo schema la rende esplicita
perché il comportamento al raggiungimento del limite è una decisione che deve
essere auditabile: un revisore deve poter sapere se un run si è fermato perché
il budget era `hard_stop` o se ha proseguito con un `warn`.

### 13.2 `token_usage_records` — il consumo reale, append-only

`token_usage_records` rappresenta il **consumo reale di token (e costo stimato)**
registrato dopo che è avvenuto. È un **fatto append-only**: ogni record è la
registrazione di un consumo accaduto e non viene mai modificato. Un nuovo
consumo è una nuova riga.

`token_usage_records` è il contraltare di fatto del `token_budgets`
configurato: il budget dice "quanto era ammesso", il usage record dice "quanto è
stato realmente consumato".

**Campi probabili:**

- `id` — UUID, PK.
- `tenant_id` — UUID NOT NULL, FK `tenants`, ON DELETE RESTRICT.
- `orchestration_run_id` — UUID NOT NULL, FK `orchestration_runs`, ON DELETE
  RESTRICT.
- `agent_run_id` — UUID nullable, FK `agent_runs`, ON DELETE RESTRICT: presente
  quando il consumo è attribuibile a un agent run specifico.
- `provider_invocation_id` — UUID nullable, FK `provider_invocations` (§14):
  presente quando il consumo è attribuibile a una singola invocazione di
  provider; è la granularità più fine.
- `pass_kind` — TEXT nullable con CHECK, il pass a cui il consumo si riferisce:
  `∈ {independent_answer, reviewer, critic, synthesis, second_check, source_resolution}`.
- `tokens_input` — BIGINT NOT NULL, token di input consumati.
- `tokens_output` — BIGINT NOT NULL, token di output consumati.
- `cost_estimate` — DOUBLE PRECISION nullable, costo stimato del consumo.
- `attempt_no` — INTEGER NOT NULL DEFAULT 1, il numero del tentativo: i token
  consumati da un tentativo fallito e poi riprovato vanno contati, e
  `attempt_no` li distingue (vedi §13.4, retry accounting).
- `is_mock` — BOOLEAN NOT NULL, indicatore che il consumo è stato stimato o
  simulato in modalità mock e **non** è un costo provider reale (vedi §13.5).
- `idempotency_key` — TEXT NOT NULL, per assorbire redelivery: un doppio delivery
  dell'evento che registra un consumo non deve duplicare il record.
- `recorded_at` — TIMESTAMPTZ NOT NULL, momento della registrazione.

**Idempotenza.** Un UNIQUE — per esempio su `(orchestration_run_id,
provider_invocation_id, idempotency_key)`, o su `(agent_run_id, pass_kind,
attempt_no, idempotency_key)` quando l'invocazione non è disponibile — impedisce
che lo stesso consumo venga contato due volte. Pattern coerente con
`cec_entry_span_idem_uq` di `0009` e con le UNIQUE di idempotenza già adottate.

### 13.3 Budget consumato da un run: snapshot e immutabilità

Il principio 3 richiede che un `OrchestrationRun` fissi uno snapshot immutabile
della configurazione che consuma. Il budget non fa eccezione: **il budget
consumato da un run deve essere reso immutabile per quel run**.

Due modellazioni ammesse, analoghe a quelle discusse per il prompt in §6.2:

- il run referenzia una riga `token_budgets` la cui mutabilità è cessata
  all'atto della consumazione (la riga, da quel momento, non va più modificata —
  comportamento garantito a livello applicativo dal worker, eventualmente
  rafforzato da un meccanismo di versioning);
- oppure il run copia i valori di budget (`token_limit`, `cost_limit`,
  `overflow_policy`) in uno snapshot — inline su `orchestration_runs` o dentro
  `agent_config_snapshots` per la quota per-agente.

La raccomandazione di questo documento, coerente con §6.2 e §7.3, è la
**seconda**: il budget effettivamente in vigore per un run (globale e
per-agente) viene **copiato negli snapshot** che il run fissa all'avvio — il
budget globale nello snapshot del run, il budget per-agente dentro
`agent_config_snapshots`. Così il run è auditabile rispetto al budget *esatto*
che ha usato, e una modifica successiva alla configurazione `token_budgets` non
altera retroattivamente ciò che il run ha eseguito. La riga `token_budgets` resta
la configurazione "viva" per i run futuri.

### 13.4 Retry accounting, aggregazione, audit

**Retry accounting.** I retry consumano budget. Coerentemente con
`PHASE_PRODUCT_ORCHESTRATION_PRE.md §11.9`, i token dei tentativi falliti vanno
contati contro il budget del run. Lo schema lo rende possibile con il campo
`attempt_no` su `token_usage_records`: ogni tentativo, riuscito o fallito,
appende il proprio record di consumo. La somma su tutti gli `attempt_no` è il
consumo reale totale. Un tentativo fallito non "cancella" il proprio consumo: il
suo `token_usage_records` resta, append-only, e concorre all'aggregato.

**Aggregation.** L'aggregato di consumo a livello di agent run e a livello di
orchestration run è **derivabile** sommando i `token_usage_records` collegati.
ORCH-SCHEMA-A deciderà se l'aggregato resta puramente derivato a read time,
oppure se viene materializzato come campo denormalizzato `total_tokens` /
`total_cost` su `agent_runs` e `orchestration_runs` (campi già anticipati in §8
e §10), scritto una sola volta al completamento. In entrambi i casi vale
l'invariante: il campo materializzato, se esiste, è una proiezione dei record
append-only, mai una fonte di verità indipendente, e non viene aggiornato in
place più volte — è scritto una volta al completamento del run.

**Audit.** Il consumo di token è un fatto e, come tutti i fatti rilevanti di un
run (principio 7), deve essere riconducibile all'audit. Le tappe significative
del consumo — in particolare un superamento di budget — vanno registrate come
`orchestration_events` (vedi §9 e §13.6). I `token_usage_records` stessi, essendo
append-only, sono già una traccia auditabile; un revisore può ricostruire il
consumo riga per riga.

### 13.5 Modalità mock: consumo stimato o simulato, mai presentato come reale

In MVP-0 `provider='mock'`, `PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0`. In
questa modalità il consumo di token registrato in `token_usage_records` può
essere **stimato o simulato** — per esempio derivato dalla lunghezza del testo
mock prodotto — ma **non è un costo provider reale**.

Lo schema rende questa distinzione esplicita e non bypassabile con il campo
`is_mock` (BOOLEAN NOT NULL) su `token_usage_records`. Finché il provider è mock,
ogni record porta `is_mock=true`. Un consumer, un report o una UI che leggano
questi record devono poter dichiarare onestamente che il consumo è mock-driven,
coerentemente con i mock indicators dell'Anti-Hallucination Report. Lo schema
**non deve** permettere di presentare un consumo mock come se fosse un costo
provider reale: è la stessa onestà che `PHASE_PRODUCT_ORCHESTRATION_PRE.md §10.5`
impone per la distinzione fra output mock e sintesi multi-AI reale.

### 13.6 Cosa succede se il budget termina

Il comportamento al raggiungimento o al superamento del budget deve essere
**rappresentato come evento o stato auditabile, non come update silenzioso**.
Questo è un'applicazione diretta del principio 4 (no update silenzioso di fatti
storici).

Lo schema raccomanda:

- quando il consumo aggregato di un run si avvicina o raggiunge il
  `token_limit`, viene appeso un `orchestration_events` dedicato — il codominio
  degli `event_type` (§9) va esteso da ORCH-SCHEMA-A con almeno
  `token_budget_exceeded` (e, opzionalmente, un `token_budget_warning` per il
  caso `overflow_policy='warn'` o per la soglia di avvicinamento);
- se la `overflow_policy` del budget è `hard_stop`, l'orchestrazione degrada in
  modo controllato: i pass opzionali (reviewer, critic, optional second check)
  non vengono avviati, gli agent run in eccesso non partono, e il run transita
  verso uno stato terminale (`completed` con esito parziale, o `failed`,
  secondo la decisione di ORCH-SCHEMA-A e ORCH-RUNNER-A). La transizione è essa
  stessa un `orchestration_events`;
- se la `overflow_policy` è `warn`, viene appeso l'evento di warning e
  l'esecuzione prosegue; il warning resta un fatto auditabile.

In nessun caso il superamento del budget deve essere un fatto che "scompare":
non si sovrascrive un contatore, non si tronca silenziosamente un consumo. Il
budget terminato è un evento registrato, e la reazione dell'orchestrazione a
quell'evento è anch'essa registrata. Coerentemente con
`PHASE_PRODUCT_ORCHESTRATION_PRE.md §11.7` (audit dei tagli), ogni operazione che
riduce informazione per restare entro budget — compressione di evidenze,
riassunto, troncamento — è anch'essa un fatto append-only, registrato come
evento o record, mai applicato riscrivendo silenziosamente il materiale
originale.

---

## 14. ProviderInvocation

`provider_invocations` registra, come **fatto append-only**, ogni chiamata
all'astrazione di provider effettuata per conto di un `agent_runs`. È l'entità
che rende auditabile *cosa è stato realmente invocato*, distinta dallo snapshot
di configurazione che registra *cosa il run intendeva usare* (§7.3).

A livello di prodotto, l'astrazione di provider è progettata in
`PHASE_PRODUCT_ORCHESTRATION_PRE.md §10`; questa sezione progetta solo il
supporto di schema per l'auditabilità delle invocazioni. **Questa fase non
implementa provider reali** e `provider_invocations` non introduce credenziali,
secret, SDK o chiamate di rete: descrive una tabella.

### 14.1 Anche il mock provider produce invocation records

Un punto centrale: **anche il mock provider deve produrre invocation records**.
Non è una tabella che "si attiva" solo con i provider reali. Finché
`provider='mock'`, ogni volta che un `agent_runs` invoca l'astrazione di
provider — anche se l'astrazione mock risponde in modo deterministico e senza
rete — viene appeso un `provider_invocations` con `is_mock=true`.

Questo per due ragioni: (1) l'orchestrazione viene sviluppata e collaudata
mock-first (`PHASE_PRODUCT_ORCHESTRATION_PRE.md §10.3`), e il collaudo
dell'auditabilità delle invocazioni deve avvenire sul mock, non essere rinviato;
(2) il passaggio da mock a provider reale deve essere un cambiamento di *dato*
(`is_mock` da true a false, `provider`/`model` reali, hash di payload reali), non
di *struttura* — coerentemente con quanto §10.4 dichiara per l'intero schema
multi-AI.

**Una invocation mock non equivale a una chiamata provider reale.** Lo schema lo
rende esplicito con `is_mock`. Un'invocation mock è la registrazione di un
passaggio attraverso l'astrazione di provider in modalità deterministica; una
futura invocation reale sarà la registrazione di una chiamata di rete a un
provider esterno. Le due cose condividono la tabella ma non la natura, e
`is_mock` impedisce di confonderle.

### 14.2 Campi probabili

- `id` — UUID, PK.
- `tenant_id` — UUID NOT NULL, FK `tenants`, ON DELETE RESTRICT.
- `agent_run_id` — UUID NOT NULL, FK `agent_runs`, ON DELETE RESTRICT:
  l'invocazione è effettuata per conto di un agent run.
- `orchestration_run_id` — UUID nullable, FK `orchestration_runs` (denormalizzato
  per query ergonomiche).
- `provider_name` — TEXT NOT NULL, il nome del provider invocato; in MVP-0
  `mock`.
- `model` — TEXT NOT NULL, l'identificativo del modello presso il provider;
  campo opaco a livello di schema.
- `request_hash` — TEXT nullable, hash del payload di richiesta inviato
  all'astrazione di provider.
- `response_hash` — TEXT nullable, hash del payload di risposta ricevuto.
- `status` — TEXT NOT NULL con CHECK, l'esito dell'invocazione: per esempio
  `∈ {succeeded, failed, timeout, rate_limited}`.
- `error_code` / `error_message` — TEXT nullable, dettaglio dell'errore quando
  `status` non è `succeeded`.
- `tokens_input` / `tokens_output` — BIGINT nullable, token consumati,
  riportati in modo omogeneo così da alimentare `token_usage_records` (§13.2).
- `cost_estimate` — DOUBLE PRECISION nullable, costo stimato dell'invocazione.
- `latency_ms` — INTEGER nullable, latenza dell'invocazione.
- `attempt_no` — INTEGER NOT NULL DEFAULT 1, il numero del tentativo (coerente
  con il retry accounting di §13.4).
- `is_mock` — BOOLEAN NOT NULL, indicatore che l'invocazione è mock-driven.
- `redaction_strategy` — TEXT nullable, l'identificativo della strategia di
  redaction applicata al payload, quando un payload viene conservato (vedi
  §14.3).
- `idempotency_key` — TEXT NOT NULL, per assorbire redelivery.
- `created_at` — TIMESTAMPTZ NOT NULL.

`provider_invocations` è **append-only** (trigger `reject_modify_append_only()`):
una chiamata, una volta avvenuta, è un fatto. Un nuovo tentativo è una nuova
riga, con `attempt_no` incrementato — non un update di una riga fallita.

### 14.3 Segreti, redaction, retention: cosa lo schema NON fa

Tre vincoli espliciti, da rispettare in ORCH-SCHEMA-A e nelle fasi provider:

- **Non salvare segreti.** Lo schema di `provider_invocations` **non deve**
  prevedere alcuna colonna che contenga API key, token di autenticazione,
  credenziali o secret di provider. L'autenticazione vive dentro
  l'implementazione del provider, non nella tabella di audit. Questo è coerente
  con `PHASE_PRODUCT_ORCHESTRATION_PRE.md §10.4`, che esclude credenziali e
  secret da questa linea di lavoro.
- **Request/response hash, non payload in chiaro per default.** I campi
  `request_hash` e `response_hash` servono **per audit** — verificare che una
  richiesta o una risposta sia quella attesa, rilevare divergenze — **non** per
  rendere supportato o pubblicabile il contenuto. Un hash non prova nulla sul
  merito della risposta: è un'impronta. Lo schema privilegia gli hash; un
  eventuale payload completo è opzionale e soggetto al punto seguente.
- **Payload completo: strategia di redaction e retention dedicata, futura.** Se
  in futuro si vorrà conservare il payload completo di richiesta/risposta (per
  debugging approfondito o per replay), questo dovrà avvenire con una
  **strategia di redaction** esplicita — il campo `redaction_strategy` ne
  registra l'identità — e con una **strategia di retention dedicata** (i payload
  completi non possono crescere indefinitamente). Né la redaction né la
  retention sono progettate in questa fase: sono debito esplicito, da affrontare
  quando i provider reali renderanno il payload un dato concreto e sensibile.

**Una invocation provider reale futura dovrà essere auditabile ma senza esporre
segreti.** Lo schema progettato qui regge questo requisito: l'auditabilità è
data da `request_hash`/`response_hash`, `status`, `error_*`, token, latenza e
`attempt_no`; i segreti non hanno una colonna in cui finire. Il passaggio ai
provider reali popola la tabella con dati reali, non la riscrive.

---

## 15. CandidateSynthesis

`candidate_syntheses` rappresenta la **risposta unica candidata** prodotta da un
`OrchestrationRun`: la sintesi multi-AI originale che converge dagli output
degli agenti, dalle cross review e dalle fonti candidate verificate. È
**esplicitamente una candidate**: non pubblicata, non verificata, non passata per
il gate.

`candidate_syntheses` è un **fatto append-only**, l'analogo multi-AI di un
`draft_final_answers` (`0005`): una candidate registrata è un fatto e non si
riscrive.

### 15.1 Da cosa deriva, e cosa non è

Una `candidate_syntheses` **deriva da**: gli `agent_outputs` (§10.3) prodotti
dagli agenti del run, gli esiti di cross review (reviewer pass / critic pass) e
le `source_candidates` (§11) — più precisamente, le fonti candidate che hanno
attraversato risoluzione e verifica (§12) e hanno prodotto evidence span. La
sintesi è la convergenza di questi materiali in una risposta unica, non un
elenco di output paralleli.

Tre invarianti negativi, da rendere strutturalmente veri nello schema e nelle
fasi che lo implementano:

- **`candidate_synthesis` non è `published_answers`.** Sono entità distinte, in
  tabelle distinte. Una candidate synthesis è una risposta *proposta*; un
  `published_answers` è una risposta che ha attraversato il Final Answer Gate ed
  è stata pubblicata. Lo schema non deve avere una FK o un percorso che trasformi
  una candidate synthesis in published answer senza passare per il gate.
- **`candidate_synthesis` non è una final answer.** Non è nemmeno un
  `FinalAnswerCandidate` (l'entità della metà destra del flusso, descritta in
  `PHASE_PRODUCT_ORCHESTRATION_PRE.md §7.16`): quella è la candidate *dopo* che
  ha attraversato Claim Extraction, Evidence Binding e GateEvaluation. La
  `candidate_syntheses` è *prima* di tutto questo.
- **`candidate_synthesis` non bypassa il gate.** Deve attraversare Claim
  Extraction, Evidence Binding e Final Answer Gate prima di poter diventare
  pubblicabile (vedi §16). Nessun campo e nessuna FK di `candidate_syntheses`
  deve rendere possibile una pubblicazione che salti questa catena.

### 15.2 Campi probabili

- `id` — UUID, PK.
- `tenant_id` — UUID NOT NULL, FK `tenants`, ON DELETE RESTRICT.
- `orchestration_run_id` — UUID NOT NULL, FK `orchestration_runs`, ON DELETE
  RESTRICT.
- `version_no` — INTEGER NOT NULL, per il versionamento append-only (vedi
  §15.4).
- `synthesis_text` — TEXT NOT NULL, il testo della risposta candidata.
- `synthesis_text_hash` — TEXT NOT NULL, hash del testo, per tracciabilità.
- `synthesizer_agent_run_id` — UUID nullable, FK `agent_runs`: l'agent run con
  ruolo synthesizer responsabile della sintesi, quando la sintesi è prodotta da
  un agente con `synthesizer_flag` (§7.2); nullo quando la sintesi è prodotta da
  un passo dedicato non agentico.
- `output_kind` / `format` — TEXT, forma della sintesi (tipicamente testo
  articolato; lo schema non vincola).
- `is_mock` — BOOLEAN NOT NULL, indicatore che la sintesi deriva da strumenti
  mock (in MVP-0 sempre true). Coerente con i mock indicators: una sintesi mock
  non va presentata come una sintesi multi-AI reale
  (`PHASE_PRODUCT_ORCHESTRATION_PRE.md §10.5`).
- `idempotency_key` — TEXT NOT NULL, per assorbire redelivery.
- `created_at` — TIMESTAMPTZ NOT NULL.

`candidate_syntheses` è **append-only** (trigger `reject_modify_append_only()`).

### 15.3 Relazione con i materiali di origine e con la futura Claim Extraction

La relazione fra una `candidate_syntheses` e i materiali da cui deriva
(`agent_outputs`, cross review, `source_candidates`/evidence span) non va
modellata come un campo JSONB opaco dentro la sintesi, ma come **join
append-only esplicite**, coerenti con la panoramica della §5:

- `synthesis_source_links` — collega una `candidate_syntheses` agli
  `agent_outputs` e/o agli `evidence_spans` (verificati, prodotti dalla catena
  source candidate → resolution → verification, §12) che la sintesi ha usato.
  Rende tracciabile *su quali materiali* la sintesi si è basata.
- `synthesis_claim_links` — è il ponte verso la futura Claim Extraction: collega
  una `candidate_syntheses` ai claim estratti da essa. È descritto in dettaglio
  in §16, perché è il punto di giunzione con il Claim Ledger.

La **relazione con la futura claim extraction** è quindi: la
`candidate_syntheses` è l'**input** che la Claim Extraction consuma. La Claim
Extraction scompone il `synthesis_text` in claim verificabili; ogni claim
estratto viene materializzato come `logical_claims` / `claim_ledger_entries` del
Claim Ledger esistente, e il legame fra la sintesi e i suoi claim è registrato in
`synthesis_claim_links`. Lo schema di `candidate_syntheses` non contiene i claim:
li *precede* e li alimenta.

### 15.4 Append-only e versioning di una sintesi rigenerata

`candidate_syntheses` è append-only. Se una sintesi viene **rigenerata** — per
esempio perché l'optional second check pass (`PHASE_PRODUCT_ORCHESTRATION_PRE.md
§12.1`) richiede una revisione, o perché un nuovo synthesis pass produce una
versione migliore — **non** si sovrascrive la riga esistente. Si crea una **nuova
versione**: una nuova riga `candidate_syntheses` con `version_no` incrementato,
oppure (se ORCH-SCHEMA-A preferisse una sintesi per run) una nuova
`candidate_syntheses` collegata a un nuovo `orchestration_runs`.

La raccomandazione di questo documento è il **versionamento append-only per run**:
un `orchestration_runs` può avere più `candidate_syntheses` con `version_no`
crescente, e l'ultima versione è quella che procede verso la Claim Extraction. Un
UNIQUE su `(orchestration_run_id, version_no)` enforced il versionamento, sul
modello di `draft_final_answers_version_uq` (`0005`) e del `version_no` di
`source_quality_assessments` e `claim_entailment_checks`. La sintesi precedente
resta, immutata, come fatto storico: un revisore deve poter vedere che una
sintesi è stata rigenerata e confrontare le versioni.

---

## 16. Collegamento con Claim Ledger e Gate

Questa sezione descrive il **punto di giunzione** fra la catena multi-AI nuova e
la pipeline evidence-gated esistente. È il punto, già anticipato in
`PHASE_PRODUCT_ORCHESTRATION_PRE.md §4.3` e §13, in cui una `candidate_syntheses`
multi-AI viene trasformata nell'input che la Claim Extraction già sa consumare, e
percorre la catena fino al Final Answer Gate.

ORCH-SCHEMA-PRE non scrive SQL e non implementa questa giunzione: descrive come
lo schema deve essere disegnato perché la giunzione sia **strutturalmente
naturale e non scavalcabile**.

### 16.1 La catena di innesto

Il futuro innesto, in forma lineare:

```
candidate_synthesis
   ▼  (la sintesi multi-AI candidata — §15)
extracted claims
   ▼  (Claim Extraction scompone synthesis_text in claim verificabili)
logical_claims / claim_ledger_entries
   ▼  (i claim entrano nel Claim Ledger esistente — 0004, append-only)
claim_evidence_links
   ▼  (i claim vengono collegati a evidence span verificati — 0004)
CVE-lite
   ▼  (verification_records — controllo presenza/quote/hash)
Source Quality
   ▼  (source_quality_assessments — qualità della fonte)
Claim Entailment
   ▼  (claim_entailment_checks — relazione claim ↔ evidence span)
Final Answer Gate
   ▼  (final_gate_reports — decisione di pubblicabilità)
published / held answer
```

Da `extracted claims` in poi la catena coincide **integralmente** con la
pipeline evidence-gated già esistente e descritta in `PROJECT_STATE.md`. La parte
nuova è la trasformazione `candidate_synthesis → extracted claims` e i suoi due
ponti di schema: `synthesis_claim_links` (collega la sintesi ai `logical_claims`
estratti) e il riuso degli evidence span verificati prodotti dalla catena
`source_candidates → source_resolutions → source_verifications → evidence_spans`
(§11-§12).

### 16.2 Nessun nuovo gate, nessun ricalcolo UI

Vincoli vincolanti per ORCH-SCHEMA-A e per tutte le fasi di codice successive:

- **Nessun nuovo gate parallelo.** La catena multi-AI **non** introduce un
  secondo gate accanto al Final Answer Gate esistente. Il fatto che una risposta
  provenga da più AI coordinate non aggiunge un asse di "verità multi-AI" né un
  gate dedicato: la decisione di pubblicabilità resta quella del Final Answer
  Gate già implementato (8.7G + 8.8A-GATE). Lo schema non deve prevedere una
  tabella di "multi-AI gate report" distinta da `final_gate_reports`.
- **Nessun ricalcolo UI.** Coerentemente con i guardrail già stabiliti
  (`PHASE_PRODUCT_ORCHESTRATION_PRE.md §13.3`, `PHASE_UI_PRE.md §3`), la UI e i
  report non ricalcolano alcuna decisione del backend. Il Run progress panel e
  il futuro report multi-AI sono viste derivate read-only.
- **Il report resta una derived read-only view.** L'osservabilità multi-AI —
  l'analogo dell'Anti-Hallucination Report per la linea di orchestrazione — è una
  vista derivata: aggrega e rende leggibili fatti già persistiti, non prende
  nuove decisioni, non muta DB, non ricalcola il gate. Non è un'entità di schema:
  è una read API, fuori scope di questo documento di schema (vedi §5.1, nota sui
  tipi "derived").
- **`publication held` non significa falso nel mondo.** Lo stato di hold di una
  risposta multi-AI è uno stato di prodotto evidence-first: significa che il
  supporto disponibile è risultato insufficiente per la pubblicazione, **non**
  che la risposta sia falsa nel mondo. Lo schema non introduce alcun campo che
  rappresenti "falsità nel mondo".

### 16.3 La source candidate deve passare per resolution/verification

Un invariante che lo schema deve rendere impossibile da scavalcare, già fissato
dai principi 9 e dalle §11-§12:

- una `source_candidate` deve passare per `source_resolutions` (recupero della
  fonte reale) e `source_verifications` (verifica presenza/quote/hash) **prima**
  di poter contribuire a un evidence span e quindi a un `claim_evidence_links`;
- solo gli `evidence_spans` prodotti o agganciati da una `source_verifications`
  riuscita possono entrare nella catena di binding dei claim;
- lo schema, per costruzione — assenza di FK dirette `source_candidates →
  claim_evidence_links`, presenza obbligatoria della catena
  resolution→verification→evidence_span — rende impossibile che una citazione di
  un'Ai diventi evidenza senza essere verificata.

Una `candidate_syntheses` può quindi citare o appoggiarsi a una fonte solo
attraverso evidence span verificati. La Claim Extraction estrae claim dal testo
della sintesi; l'Evidence Binding li collega a evidence span che esistono solo
perché una source candidate è stata risolta e verificata. Il percorso "una AI ha
citato una fonte → la fonte è evidenza" è chiuso dallo schema.

### 16.4 Tre assi distinti, e un gate che non decide verità assoluta

L'innesto preserva le separazioni semantiche già stabilite dal progetto:

- **CVE-lite, Source Quality e Claim Entailment restano assi distinti.** CVE-lite
  (`verification_records`) verifica la presenza testuale della quote e il match
  dell'hash; **non** prova il supporto semantico. Source Quality
  (`source_quality_assessments`) valuta la qualità della fonte che ospita la
  quote; **non** prova un claim. Claim Entailment (`claim_entailment_checks`)
  valuta la relazione fra il claim e l'evidence span; **non** prova la verità del
  claim nel mondo. Lo schema multi-AI non collassa questi assi né ne aggiunge un
  quarto di "verità multi-AI".
- **Il Final Answer Gate decide publication allowed / publication held, non
  verità assoluta.** Il gate compone gli assi secondo una policy versionata e
  decide la pubblicabilità. Non è un giudice di verità nel mondo. Una risposta
  multi-AI che attraversa il gate riceve uno stato di pubblicazione
  evidence-first, non un verdetto di verità.

Lo schema di `candidate_syntheses`, di `synthesis_claim_links` e degli evidence
span verificati alimenta il gate esistente; non lo modifica, non lo duplica, non
ne cambia la semantica.

---

## 17. Relazione con schema esistente

Questa sezione confronta le entità proposte (§5-§16) con lo schema attualmente
applicato (migration `0001`-`0010`, stato `af74187`), e ratifica le decisioni di
naming e di riuso lasciate aperte nelle sezioni precedenti.

### 17.1 Confronto entità proposte ↔ schema attuale

| Entità proposta (multi-AI) | Corrispondenza nello schema attuale | Natura del rapporto |
|---|---|---|
| `master_prompts` | nessuna; concettualmente sostituisce `task_masters.objective` | entità nuova |
| `master_prompt_versions` | pattern analogo a `document_versions` (0003) | entità nuova, pattern riusato |
| `agent_role_prompts` | nessuna | entità nuova |
| `agent_configs` | nessuna (il nome non collide) | entità nuova |
| `agent_config_snapshots` | nessuna | entità nuova |
| `orchestration_runs` | concettualmente l'analogo di `task_masters` per la linea multi-AI | entità nuova |
| `orchestration_events` | pattern analogo all'audit chain / `audit_records` (0001) | entità nuova, pattern riusato |
| `agent_runs` (multi-AI) | **collisione di nome** con `agent_runs` di `0005` (semantica diversa) | vedi §17.2 |
| `agent_messages` | nessuna | entità nuova |
| `agent_outputs` (multi-AI) | **collisione di nome** con `agent_outputs` placeholder di `0005` | vedi §17.2 |
| `source_candidates` | nessuna | entità nuova |
| `source_resolutions` | nessuna | entità nuova |
| `source_verifications` | concettualmente vicina a CVE-lite (`verification_records`, 0004) ma su fonti candidate | entità nuova; riusa `evidence_spans` |
| `token_budgets` | nessuna | entità nuova |
| `token_usage_records` | nessuna; placeholder `truncation_events`/`continuation_attempts` vagamente affini | entità nuova; vedi §17.2 |
| `candidate_syntheses` | concettualmente l'analogo di `draft_final_answers` (0005) per la linea multi-AI | entità nuova, pattern riusato |
| `synthesis_source_links`, `synthesis_claim_links` | join nuove; `synthesis_claim_links` è il ponte verso il Claim Ledger | entità nuove |
| `provider_invocations` | nessuna | entità nuova |

Entità dello schema esistente che la linea multi-AI **riusa senza modificarle**:

- `tenants`, `projects`, `users` — multi-tenancy e contenitori. `master_prompts`
  e `orchestration_runs` vi si agganciano via FK; `project_id` è mantenuto
  nullable in attesa della decisione su §17.4.
- `uploaded_documents`, `document_versions`, `document_chunks`, `evidence_spans` —
  le tabelle di documento. La catena di verifica delle fonti candidate (§12)
  **normalizza** le fonti risolte ingeribili in queste tabelle e **aggancia** o
  produce `evidence_spans` reali, invece di creare una rappresentazione parallela
  delle evidenze. Questo è il principio di riuso.
- `logical_claims`, `raw_claims`, `classified_claims`, `claim_ledger_entries`,
  `claim_lineage`, `claim_evidence_links`, `verification_records` — il Claim
  Ledger. La catena multi-AI vi si innesta da `synthesis_claim_links` in poi
  (§16): i claim estratti da una `candidate_syntheses` diventano `logical_claims`
  / `claim_ledger_entries` ordinari.
- `source_quality_assessments`, `claim_entailment_checks` — gli assi Source
  Quality e Claim Entailment, consultati dal gate. La catena multi-AI li alimenta
  esattamente come la pipeline closed-corpus attuale.
- `draft_final_answers`, `final_answer_spans`, `final_answer_span_claim_links`,
  `coverage_gap_statements`, `final_gate_reports`, `published_answers` — la metà
  destra del flusso. La catena multi-AI vi arriva attraverso il punto di
  giunzione (§16): una `candidate_syntheses`, scomposta in claim e legata a
  evidence span verificati, percorre questa pipeline senza modificarla.
- `audit_records`, `audit_chain_heads`, `event_processing_records`,
  `policy_versions` — l'infrastruttura trasversale. Le transizioni di
  `orchestration_runs` e `agent_runs` entrano nell'audit chain esistente; le
  scritture multi-AI usano `event_processing_records` per l'idempotenza per
  consumer; le policy di orchestrazione e di gate restano versionate.

### 17.2 Le tabelle placeholder `0005`: decisione ratificata

Le sezioni 3.3, 5.1, 7.2 e 10 hanno descritto la collisione di naming con le
quattro tabelle placeholder introdotte da `0005` (`agent_runs`, `agent_outputs`,
`truncation_events`, `continuation_attempts`). Qui la decisione viene **ratificata
come raccomandazione per ORCH-SCHEMA-A** — fermo restando che la ratifica
definitiva, essendo una decisione di schema con impatto su migration, spetta
formalmente a ORCH-SCHEMA-A dopo revisione umana.

**Descrizione dello stato reale delle placeholder.** Verificato sul codice di
`0005_answers_gate.sql`:

- `agent_runs` (`0005`) **esiste e non è vuota in esercizio**: ha un CHECK
  `run_kind ∈ {compile_draft, final_answer_gate}`, una UNIQUE
  `agent_runs_attempt_uq (task_id, run_kind, attempt_no)`, e il compiler e il
  Final Answer Gate mock-driven la usano per tracciare i propri run. La sua
  semantica è il *tracking di compilazione e gate della pipeline closed-corpus*,
  **non** l'orchestrazione di agenti AI.
- `agent_outputs` (`0005`) **esiste ed è vuota**: la migration la dichiara
  placeholder, il pipeline mock-driven non passa per "agent completions" e non
  la popola in 8.4 né nelle fasi successive.
- `truncation_events` e `continuation_attempts` (`0005`) **esistono e sono
  vuote**: placeholder dichiarati, mai popolati.

**Limiti delle placeholder rispetto al modello multi-AI.** `agent_runs` di
`0005` è troppo stretta per la linea multi-AI: il suo `run_kind` ammette solo
`compile_draft` e `final_answer_gate`, è ancorata a `task_id` (non a un
`orchestration_run_id`), e la sua UNIQUE è costruita attorno al tracking
compiler/gate. Estenderla per ospitare anche l'esecuzione di agenti AI
significherebbe mescolare due semantiche diverse su una stessa tabella —
violando il principio 8 (separazione config/fatti, e per estensione separazione
di responsabilità) e rendendo ambigua ogni query. `agent_outputs` di `0005`,
benché vuota, ha già un CHECK `role ∈ {assistant, tool, gate}` e una FK verso
`agent_runs(0005)`: adottarla per gli output multi-AI erediterebbe quei vincoli
e quella FK verso la tabella sbagliata.

**Raccomandazione: riuso, estensione o sostituzione.** Coerentemente con la
discussione di §5.1 e con il principio per cui le nuove entità future devono
essere **additive**:

- **`agent_runs` e `agent_outputs` (`0005`): non riusare, non ridefinire.** Si
  raccomanda di **lasciarle intatte** (sono migration applicate e immutabili —
  vedi §17.3) e di introdurre le entità multi-AI con **nomi propri distinti**,
  prefissati per chiarezza semantica: `orchestration_agent_runs`,
  `orchestration_agent_outputs` (e, per coerenza di famiglia,
  `orchestration_agent_messages`, `orchestration_agent_configs`,
  `orchestration_agent_config_snapshots`). Questo evita la collisione, mantiene
  le due semantiche separate, e rispetta l'immutabilità delle migration
  applicate. Nelle sezioni 5, 7 e 10 i nomi `agent_*` sono stati usati per
  leggibilità e per coerenza con il prompt operativo; il **nome fisico finale
  raccomandato è prefissato `orchestration_*`**.
- **`truncation_events` e `continuation_attempts` (`0005`): non adottare in
  ORCH-SCHEMA-A.** La loro funzione concettuale — registrare tagli di output e
  tentativi di continuazione — è coperta, in questo schema, da
  `token_usage_records` con `attempt_no` (per il retry/continuation accounting,
  §13.4) e da `orchestration_events` (per gli eventi di troncamento/budget,
  §13.6). Si raccomanda di **lasciarle come placeholder inerti** e di non
  costruirvi sopra: se in una fase futura il truncation diventerà un fatto
  concreto (con i provider reali), ORCH-SCHEMA-A o una fase successiva deciderà
  se popolarle, ridefinirle con una migration additiva, o introdurre tabelle
  dedicate.
- **Sostituzione/deprecazione delle placeholder: fuori scope.** Deprecare le
  placeholder `0005` o migrarne il contenuto/semantica è un intervento invasivo
  sullo schema closed-corpus esistente e **non** è scope di ORCH-SCHEMA-A. Le
  placeholder restano dove sono, come debito di naming dichiarato.

In sintesi: per la linea multi-AI **si introducono tabelle nuove con nomi propri
prefissati `orchestration_*`**; le placeholder `0005` non vengono né riusate né
ridefinite né rimosse. Se ORCH-SCHEMA-A, dopo revisione umana, valutasse che i
placeholder sono comunque troppo stretti o ambigui da lasciare, la scelta
alternativa va comunque rinviata e documentata in ORCH-SCHEMA-A, non anticipata
qui.

### 17.3 Le migration applicate non vanno riscritte; le nuove entità sono additive

Due vincoli, ereditati da `docs/migration_plan.md` ("Regola d'oro") e da
`PROJECT_STATE.md`:

- **Le migration applicate (`0001`-`0010`) sono immutabili e non vanno
  riscritte.** ORCH-SCHEMA-A **non** modifica `0005` per estendere `agent_runs` o
  `agent_outputs`, non modifica nessuna delle migration esistenti. Ogni
  cambiamento di schema avviene tramite **nuove migration additive** (`0011_*` o
  successive — il numero `0011` è già preannunciato in `PROJECT_STATE.md` per la
  retention; ORCH-SCHEMA-A assegnerà i numeri liberi successivi).
- **Le nuove entità future devono essere additive.** Le tabelle
  `orchestration_*`, `master_prompts`, `source_candidates` ecc. sono tutte
  tabelle **nuove**, introdotte da migration nuove, che non alterano lo schema
  closed-corpus esistente. Il riuso di `evidence_spans`, del Claim Ledger e della
  metà destra del flusso avviene per **innesto** (FK dalle tabelle nuove verso le
  tabelle esistenti, e popolamento delle tabelle esistenti tramite la pipeline
  esistente), non per modifica delle tabelle esistenti.
- **Eventuale riuso di `agent_runs`/`agent_outputs` deve rispettare il loro stato
  reale.** Poiché la raccomandazione di §17.2 è di **non** riusarle, il punto è
  in larga parte preventivo; ma resta valido come vincolo: se ORCH-SCHEMA-A
  decidesse comunque un qualche riuso, dovrebbe rispettare che `agent_runs(0005)`
  è una tabella *in esercizio* con una semantica compiler/gate attiva (non la si
  può svuotare o ridefinirne il CHECK senza rompere il compiler e il gate), e che
  `agent_outputs(0005)` ha già una FK verso `agent_runs(0005)` e un CHECK
  `role`. Il riuso non è una pagina bianca.

### 17.4 Decisioni di relazione lasciate aperte per ORCH-SCHEMA-A

- **Nozione di progetto.** Resta aperto (eredità da
  `PHASE_PRODUCT_ORCHESTRATION_PRE.md §20`) se `projects` resti come contenitore
  organizzativo del `master_prompts` o venga reso superfluo. Lo schema mantiene
  `project_id` nullable su `master_prompts` e `orchestration_runs`, senza
  pregiudicare la decisione.
- **Coesistenza vs sostituzione di `task_masters`.** Resta aperto se
  `orchestration_runs` (la linea multi-AI) debba coesistere stabilmente con
  `task_masters` (la linea closed-corpus) o assorbirlo. ORCH-SCHEMA-A dovrà
  decidere; questo documento non lo presuppone, e progetta `orchestration_runs`
  come entità indipendente.
- **Granularità del legame synthesis ↔ claim.** Resta aperto se
  `synthesis_claim_links` colleghi la sintesi ai `logical_claims` o alle
  `claim_ledger_entries`; la scelta dipende da come la Claim Extraction
  materializzerà i claim estratti, ed è una decisione di ORCH-SCHEMA-A /
  ORCH-GATE-A.

Nessuna di queste decisioni è risolta qui: questo documento le **istruisce** e
le lascia esplicitamente aperte, coerentemente con il vincolo di non inventare
stato implementato che non esiste.

---

## 18. Strategia di migrazione futura

Questa sezione propone, **senza scrivere SQL**, un ordine sicuro per le migration
che ORCH-SCHEMA-A scriverà. È una raccomandazione di sequenza e di vincoli, non
una migration. **ORCH-SCHEMA-PRE non scrive SQL**: ORCH-SCHEMA-A sarà la prima
fase candidata a scrivere migration, e solo dopo revisione umana del design qui
proposto.

### 18.1 Premessa: additività e immutabilità

Coerentemente con §17.3 e con `docs/migration_plan.md`:

- le migration applicate (`0001`-`0010`) **non vanno riscritte**;
- le nuove migration multi-AI sono **additive**: introducono tabelle nuove,
  nessuna ALTER distruttiva sulle tabelle esistenti;
- gli stati e le transizioni delle entità di fatto devono essere **append-only o
  event-based**: nessun campo di stato storico viene aggiornato in place;
- l'eventuale riuso di `agent_runs`/`agent_outputs` di `0005` — sconsigliato da
  §17.2 — dovrebbe comunque rispettare lo stato reale di quelle tabelle.

I numeri di migration concreti (`0011_*` e successivi) saranno assegnati da
ORCH-SCHEMA-A in base ai numeri liberi al suo HEAD; `PROJECT_STATE.md` preannuncia
`0011_*` come candidato per la retention, quindi ORCH-SCHEMA-A coordinerà la
numerazione.

### 18.2 Ordine sicuro proposto per ORCH-SCHEMA-A

L'ordine segue le dipendenze di FK: una tabella va creata dopo le tabelle che
referenzia. I nove gruppi proposti:

**Gruppo 1 — tabelle di configurazione.** `master_prompts`, `agent_role_prompts`,
`agent_configs`, `token_budgets` (come limite configurato).

- *Dipendenze FK.* Verso `tenants`, `projects` (nullable), `users` — tutte
  esistenti. `agent_configs` → `master_prompts` e → `agent_role_prompts` (interne
  al gruppo, da ordinare: `master_prompts` e `agent_role_prompts` prima di
  `agent_configs`).
- *Append-only.* Sono configurazione mutabile, **non** append-only; nessun
  trigger `reject_modify_append_only()`. `agent_role_prompts` è append-only
  *come catalogo versionato* (nuova revisione = nuova riga), ma la sua mutabilità
  pre-consumo va gestita applicativamente.
- *Unique.* `agent_role_prompts` UNIQUE su `(tenant_id, name, version_no)` per il
  versionamento del catalogo.
- *Idempotency.* Non critica in creazione (sono entità create da azione utente),
  ma una UNIQUE naturale sui nomi è consigliata.
- *Indici minimi.* FK index su `tenant_id`; su `agent_configs(master_prompt_id)`.
- *Rischi.* Collisione di naming con `0005` se non si adotta il prefisso
  `orchestration_*` (§17.2); va deciso prima di scrivere la migration.

**Gruppo 2 — snapshot.** `master_prompt_versions` (o lo snapshot inline, §6.2),
`agent_config_snapshots`.

- *Dipendenze FK.* `master_prompt_versions` → `master_prompts`;
  `agent_config_snapshots` → `agent_configs` e → `orchestration_runs` (FK verso
  il Gruppo 3 — vedi nota sull'ordine sotto).
- *Append-only.* **Sì**, entrambe append-only via `reject_modify_append_only()`:
  uno snapshot è immutabile per definizione.
- *Unique.* `master_prompt_versions` UNIQUE `(master_prompt_id, version_no)`.
  `agent_config_snapshots` UNIQUE `(orchestration_run_id, agent_config_id)` — uno
  snapshot per agente per run.
- *Idempotency.* La UNIQUE sopra funge anche da chiave di idempotenza sotto
  redelivery.
- *Indici minimi.* FK index su `master_prompt_id`, `orchestration_run_id`,
  `agent_config_id`.
- *Rischi.* `agent_config_snapshots` ha una FK verso `orchestration_runs` (Gruppo
  3): o si crea il Gruppo 3 prima del Gruppo 2, o si crea `agent_config_snapshots`
  insieme al Gruppo 3. La dipendenza ciclica apparente (un run consuma snapshot,
  uno snapshot referenzia il run) si risolve perché lo snapshot è creato *dopo*
  il run, all'avvio: l'ordine fisico di `CREATE TABLE` è Gruppo 3 prima del
  pezzo di Gruppo 2 che dipende da esso. ORCH-SCHEMA-A può fondere i due gruppi
  in una singola migration per evitare ambiguità.

**Gruppo 3 — `orchestration_runs`.** La radice.

- *Dipendenze FK.* Verso `tenants`, `projects` (nullable),
  `master_prompt_versions` (Gruppo 2, per lo snapshot del prompt). Eventuale FK
  nullable `final_gate_report_id` → `final_gate_reports` (esistente).
- *Append-only.* `orchestration_runs` è append-only nei campi di fatto; lo stato
  corrente è derivato dagli eventi (§8). Se ORCH-SCHEMA-A materializza un campo
  `status`, quello è l'unica eccezione, e ogni transizione genera comunque un
  evento.
- *Unique.* UNIQUE su `idempotency_key` (per tenant), per la creazione
  idempotente del run, coerente con `Idempotency-Key` di `POST /api/v1/tasks`.
- *Idempotency.* La UNIQUE su `idempotency_key` è la chiave.
- *Indici minimi.* FK index su `tenant_id`, `master_prompt_version_id`; indice su
  `(tenant_id, created_at)` per liste.
- *Rischi.* Decidere `mode` vs `execution_mode` (§8) prima di fissare il CHECK.

**Gruppo 4 — `orchestration_events`.**

- *Dipendenze FK.* → `orchestration_runs` (Gruppo 3).
- *Append-only.* **Sì**, via `reject_modify_append_only()`.
- *Unique.* UNIQUE `(orchestration_run_id, sequence_no)` per l'ordinamento, e/o
  `(orchestration_run_id, event_type, idempotency_key)` per l'idempotenza.
- *Idempotency.* La UNIQUE con `idempotency_key` assorbe i redelivery.
- *Indici minimi.* `(orchestration_run_id, sequence_no)` — già coperto dalla
  UNIQUE.
- *Rischi.* Il codominio degli `event_type` va fissato includendo gli eventi di
  budget (§13.6) e di cross review (§9).

**Gruppo 5 — `agent_runs` / `agent_messages` / `agent_outputs` multi-AI.** Con i
nomi prefissati raccomandati: `orchestration_agent_runs`,
`orchestration_agent_messages`, `orchestration_agent_outputs`.

- *Dipendenze FK.* `agent_runs` → `orchestration_runs` (G3) e →
  `agent_config_snapshots` (G2). `agent_messages` → `agent_runs`. `agent_outputs`
  → `agent_runs`.
- *Append-only.* **Sì**, tutte e tre, via `reject_modify_append_only()`.
- *Unique.* `agent_runs` UNIQUE `(orchestration_run_id, agent_config_snapshot_id,
  attempt_no)` (un tentativo per agente per run). `agent_messages` UNIQUE
  `(agent_run_id, sequence_no)`. `agent_outputs` UNIQUE per agent run secondo la
  scelta di ORCH-SCHEMA-A (per esempio `(agent_run_id, sequence_no)` se più
  output per run, o un singolo output per run).
- *Idempotency.* Le UNIQUE sopra; in più una `idempotency_key` per assorbire i
  redelivery dell'evento che crea l'agent run.
- *Indici minimi.* FK index su `orchestration_run_id`, `agent_run_id`.
- *Rischi.* Collisione di naming con `0005` (§17.2): adottare il prefisso.

**Gruppo 6 — `source_candidates` / `source_resolutions` / `source_verifications`.**

- *Dipendenze FK.* `source_candidates` → `orchestration_runs` (nullable),
  `master_prompts` (nullable), `agent_outputs` (G5, nullable per
  `agent_cited`). `source_resolutions` → `source_candidates`.
  `source_verifications` → `source_candidates`, → `source_resolutions`, →
  `evidence_spans` (esistente, nullable), → `document_versions`/`document_chunks`
  (esistenti, nullable).
- *Append-only.* **Sì**, tutte e tre.
- *Unique.* Chiavi di idempotenza per assorbire redelivery: per esempio
  `source_resolutions` UNIQUE `(source_candidate_id, idempotency_key)`,
  `source_verifications` UNIQUE `(source_resolution_id, idempotency_key)`.
- *Idempotency.* Le UNIQUE sopra.
- *Indici minimi.* FK index su `source_candidate_id`, `orchestration_run_id`,
  `source_resolution_id`, `evidence_span_id`.
- *Rischi.* Il riuso delle tabelle di documento per le fonti risolte ingeribili
  (§12.2) va progettato con cura; le fonti risolte *non* ingeribili come
  documenti (fonti web future) restano una decisione aperta rinviata a
  `ORCH-SOURCES-A`.

**Gruppo 7 — `token_usage_records` / `provider_invocations`.**

- *Dipendenze FK.* `provider_invocations` → `agent_runs` (G5),
  `orchestration_runs` (G3, nullable). `token_usage_records` →
  `orchestration_runs` (G3), `agent_runs` (G5, nullable), `provider_invocations`
  (nullable). Nota: `token_budgets` come *limite configurato* sta nel Gruppo 1;
  qui è il **consumo reale** e l'**invocazione**.
- *Append-only.* **Sì**, entrambe.
- *Unique.* `provider_invocations` UNIQUE `(agent_run_id, attempt_no,
  idempotency_key)`. `token_usage_records` UNIQUE secondo §13.2 (per esempio
  `(orchestration_run_id, provider_invocation_id, idempotency_key)`).
- *Idempotency.* Le UNIQUE sopra impediscono il doppio conteggio di un consumo o
  la duplicazione di un'invocazione sotto redelivery.
- *Indici minimi.* FK index su `agent_run_id`, `orchestration_run_id`,
  `provider_invocation_id`.
- *Rischi.* Nessun campo di segreto su `provider_invocations` (§14.3); il vincolo
  va rispettato in fase di scrittura della migration.

**Gruppo 8 — `candidate_syntheses` e le join.** `candidate_syntheses`,
`synthesis_source_links`.

- *Dipendenze FK.* `candidate_syntheses` → `orchestration_runs` (G3),
  `agent_runs` (G5, nullable, per il synthesizer). `synthesis_source_links` →
  `candidate_syntheses`, → `agent_outputs` (G5) e/o → `evidence_spans`
  (esistente).
- *Append-only.* **Sì**, entrambe.
- *Unique.* `candidate_syntheses` UNIQUE `(orchestration_run_id, version_no)` per
  il versionamento append-only (§15.4). `synthesis_source_links` UNIQUE sulla
  coppia `(candidate_synthesis_id, evidence_span_id)` / `(candidate_synthesis_id,
  agent_output_id)`.
- *Idempotency.* Le UNIQUE sopra.
- *Indici minimi.* FK index su `orchestration_run_id`, `candidate_synthesis_id`.
- *Rischi.* La scelta fra "una sintesi per run" e "più versioni per run" (§15.4)
  va fissata prima della UNIQUE.

**Gruppo 9 — link verso la claim pipeline.** `synthesis_claim_links`.

- *Dipendenze FK.* → `candidate_syntheses` (G8), → `logical_claims` (esistente,
  `0004`) o → `claim_ledger_entries` (esistente, `0004`), secondo la decisione
  aperta di §17.4.
- *Append-only.* **Sì**.
- *Unique.* UNIQUE sulla coppia `(candidate_synthesis_id, logical_claim_id)` (o
  `claim_ledger_entry_id`).
- *Idempotency.* La UNIQUE sopra.
- *Indici minimi.* FK index su `candidate_synthesis_id` e sul riferimento al
  claim.
- *Rischi.* È il **punto di giunzione** con la pipeline esistente (§16): la
  migration di questo gruppo non deve introdurre un gate parallelo né alterare il
  Claim Ledger; aggiunge solo una join. La granularità del link
  (`logical_claims` vs `claim_ledger_entries`) dipende da come la Claim
  Extraction materializza i claim, e va coordinata con `ORCH-GATE-A`.

### 18.3 Vincoli trasversali alla sequenza

- **Append-only constraints.** Tutte le tabelle dei Gruppi 2, 4, 5, 6, 7, 8, 9
  sono fatti append-only e ricevono il trigger comune
  `reject_modify_append_only()` di `0001_foundation.sql`, esattamente come le
  nove tabelle append-only già esistenti. Le tabelle del Gruppo 1 sono
  configurazione mutabile e **non** ricevono il trigger.
- **Unique / idempotency constraints.** Ogni tabella di fatto scrivibile in
  risposta a un evento redeliverato porta una chiave di idempotenza enforced da
  UNIQUE (principio 6). Il pattern è quello già adottato da
  `coverage_gap_statements_idem_uq` (`0005`), `pale_idempotency_uq` (`0006`),
  `cec_entry_span_idem_uq` (`0009`).
- **FK constraints.** Tutte le FK verso tabelle esistenti e fra le tabelle nuove
  sono `ON DELETE RESTRICT`, coerentemente con la disciplina di tutte le
  migration `0001`-`0010`. Nessuna `ON DELETE CASCADE` su entità di fatto.
- **Indici minimi.** Per ogni tabella, almeno gli indici sulle FK più
  interrogate e gli indici a supporto delle UNIQUE; ORCH-SCHEMA-A calibrerà gli
  indici di lettura aggiuntivi sui pattern di query reali (liste di run, timeline
  di eventi, drill-down per agente).
- **Stati ed event-based.** Gli stati di `orchestration_runs` e `agent_runs`
  sono derivati dagli `orchestration_events` (raccomandazione §8); se
  ORCH-SCHEMA-A materializza un campo `status`, ogni transizione genera comunque
  un evento — mai un update senza evento.

### 18.4 Rischi della migrazione, in sintesi

- **Naming.** La collisione con le placeholder `0005` (§17.2) va risolta *prima*
  di scrivere le migration, scegliendo il prefisso `orchestration_*`. Risolverla
  dopo costerebbe una migration di rinomina, più invasiva.
- **Dipendenza ciclica apparente run ↔ snapshot.** Gestita ordinando i `CREATE
  TABLE` (run prima dello snapshot che lo referenzia) o fondendo i Gruppi 2 e 3
  in una migration.
- **Punto di giunzione con il Claim Ledger.** Il Gruppo 9 tocca il confine con
  la pipeline esistente: la migration deve restare additiva e non introdurre un
  gate parallelo (§16.2).
- **Volume.** Le tabelle di fatto (eventi, messaggi, usage records, invocations)
  crescono senza pruning; la retention reale distruttiva è un debito già noto
  (`0011_*` in `PROJECT_STATE.md`) e va coordinata, ma è fuori scope sia di
  ORCH-SCHEMA-PRE sia di ORCH-SCHEMA-A.
- **Provider reali.** Lo schema è disegnato perché il passaggio a provider reali
  sia un cambiamento di dato, non di struttura (§10.4, §14.1); il rischio è che
  ORCH-SCHEMA-A introduca per errore campi che presuppongano un provider reale
  (o, peggio, segreti). I vincoli di §14.3 vanno rispettati.

---

## 19. API implicate, solo concettuali

Questa sezione descrive gli endpoint HTTP che la futura linea di orchestrazione
multi-AI presumibilmente richiederà. È una descrizione **solo concettuale**:
nessun endpoint viene implementato, nessuna route viene aggiunta, le firme sono
indicative e soggette a revisione nelle fasi di design di dettaglio e di codice.

### 19.1 Endpoint futuri (descrizione concettuale)

Gli endpoint che la linea multi-AI presumibilmente esporrà, raggruppati per
funzione:

- **`POST /api/v1/master-prompts`** — crea un MasterPrompt (l'input primario
  dell'utente). Write endpoint.
- **`GET /api/v1/master-prompts/{id}`** — legge un MasterPrompt e i suoi
  metadati. Read endpoint.
- **`POST /api/v1/orchestration-runs`** — crea un `OrchestrationRun` a partire
  da un MasterPrompt, dalle sue fonti e dalla configurazione di esecuzione.
  Write endpoint.
- **`GET /api/v1/orchestration-runs/{id}`** — legge lo stato aggregato di un
  run. Read endpoint.
- **`GET /api/v1/orchestration-runs/{id}/events`** — legge il log di
  `orchestration_events` di un run, in ordine di `sequence_no`. Read endpoint.
- **`GET /api/v1/orchestration-runs/{id}/agent-runs`** — legge gli `agent_runs`
  di un run e il loro stato. Read endpoint.
- **`GET /api/v1/orchestration-runs/{id}/outputs`** — legge gli `agent_outputs`
  prodotti durante un run. Read endpoint.
- **`GET /api/v1/orchestration-runs/{id}/source-candidates`** — legge le
  `source_candidates` raccolte durante un run, con il loro stato di risoluzione
  e verifica. Read endpoint.
- **`POST /api/v1/orchestration-runs/{id}/submit-to-gate`** — sottopone la
  `candidate_syntheses` del run alla catena di integrazione: Claim Extraction,
  Evidence Binding, Final Answer Gate. Write endpoint che innesca un'azione
  mutante.

### 19.2 Chiarimenti vincolanti

- **Questa fase non implementa API.** ORCH-SCHEMA-PRE è una fase di design di
  schema. Gli endpoint sopra sono materia delle fasi di design API e di codice
  successive; qui sono nominati solo per rendere esplicito quali superfici lo
  schema dovrà sostenere.
- **Le route Next.js `/api/ef/*` esistenti sono proxy same-origin del
  create-flow attuale.** Sono i route handler server-side introdotti da
  UI-CREATE-FLOW-A per aggirare l'assenza di middleware CORS sul backend;
  inoltrano in modo verbatim le richieste del flusso di creazione task.
- **Le route Next.js esistenti non sono ancora API di orchestrazione.** Non
  esiste oggi alcun endpoint che crei un MasterPrompt, configuri agenti o avvii
  un `OrchestrationRun`: le route `/api/ef/*` coprono solo il ciclo
  progetto → documenti → task closed-corpus.
- **Gli endpoint futuri dovranno separare write endpoint e read endpoint.** Le
  superfici che creano o mutano fatti (creazione di un run, configurazione di un
  agente, submit-to-gate) vanno tenute distinte dalle superfici che espongono
  stati derivati: la separazione mantiene chiaro quali endpoint hanno effetti e
  quali no.
- **I read endpoint futuri devono esporre stati derivati senza ricalcolare
  decisioni.** Un read endpoint legge fatti già persistiti — eventi, output,
  esiti del gate — e li proietta; non ricompone un punteggio, non rivaluta claim
  o fonti, non riemette una decisione di pubblicabilità. Lo stato di
  avanzamento di un run è derivato leggendo `orchestration_events` in ordine di
  `sequence_no`; la decisione di pubblicabilità è letta verbatim da
  `final_gate_reports`.
- **`submit-to-gate` non deve bypassare Claim Extraction, Evidence Binding e
  Final Answer Gate.** L'endpoint non pubblica una risposta: innesca la catena
  di integrazione che scompone la `candidate_syntheses` in claim verificabili,
  li collega agli evidence span verificati e li sottopone al gate esistente. Non
  esiste, e non deve esistere, un endpoint che pubblichi una risposta saltando
  il gate.
- **Gli endpoint devono conservare idempotenza per creazione run e azioni
  mutanti.** La creazione di un `OrchestrationRun` e le azioni mutanti come
  `submit-to-gate` devono accettare una chiave di idempotenza, coerentemente con
  l'header `Idempotency-Key` già usato da `POST /api/v1/tasks`: un doppio invio
  non deve creare due run né innescare due volte la stessa azione.

---

## 20. Worker implications

Questa sezione descrive i componenti worker che la futura linea di
orchestrazione multi-AI presumibilmente richiederà. È una descrizione di
implicazioni: nessun worker viene modificato o implementato in questa fase.

### 20.1 Worker futuri (descrizione concettuale)

I componenti worker che la linea multi-AI presumibilmente introdurrà:

- **orchestration consumer** — un consumer, analogo all'attuale consumer
  `task.created`, che riceve l'evento di avvio di un `OrchestrationRun` e ne
  guida l'esecuzione.
- **provider caller** — il componente che invoca l'astrazione di provider per
  conto di un `AgentRun`, traducendo una richiesta di agente in un'invocazione
  e raccogliendone l'esito.
- **source resolver** — il componente che recupera o risolve le
  `source_candidates` proposte, trasformando una citazione in una fonte reale.
- **source verifier** — il componente che verifica presenza, quote e hash di
  una fonte risolta e produce o aggancia un evidence span.
- **synthesis worker** — il componente che combina gli output degli agenti e
  gli esiti di cross review in una `candidate_syntheses`.
- **gate submitter** — il componente che innesta la `candidate_syntheses` nella
  catena di integrazione esistente.
- **event publisher** — il componente che emette gli `orchestration_events` man
  mano che il run avanza.

### 20.2 Chiarimenti vincolanti

- **Questa fase non modifica worker.** ORCH-SCHEMA-PRE è una fase di design di
  schema; i componenti worker sopra sono materia delle fasi di codice
  successive.
- **Il provider caller reale richiederà una fase dedicata.** Con il mock
  provider l'invocazione è deterministica e priva di rete; l'integrazione di
  provider reali — con credenziali, trasporto, rate limit — è lavoro di una fase
  separata e successiva.
- **Il source resolver reale richiederà una fase dedicata.** Il recupero e la
  risoluzione delle fonti candidate, in particolare per le fonti web, dipendono
  da capacità di ingestione non ancora disponibili; è lavoro di una fase
  dedicata (`ORCH-SOURCES-A`).
- **I worker devono scrivere eventi auditabili.** Ogni transizione significativa
  di un `OrchestrationRun` o di un `AgentRun` è un fatto append-only e va
  registrata come evento o record, coerentemente con l'audit chain hash-linked
  esistente; mai come riscrittura silenziosa di uno stato.
- **I retry devono essere idempotenti o tracciati come nuovi tentativi.** Un
  doppio delivery dell'evento di avvio di un run, o di un passo, non deve
  duplicare `agent_runs`, `agent_outputs` o invocazioni di provider; un nuovo
  tentativo di un agente fallito è un nuovo `AgentRun`, non un update di quello
  fallito.
- **Un worker non deve sovrascrivere fatti già accaduti.** Output, messaggi,
  esiti di review e decisioni sono fatti append-only: una loro "modifica" è
  sempre una nuova riga, mai un update in place.
- **Il gate submitter non deve creare un gate parallelo.** La decisione di
  pubblicabilità di una risposta multi-AI è una riga di `final_gate_reports`
  prodotta dal Final Answer Gate esistente; il gate submitter non introduce una
  seconda autorità di decisione.
- **Il gate submitter deve innestare la `candidate_synthesis` nella pipeline
  esistente: Claim Extraction → Evidence Binding → Final Answer Gate.** Il
  percorso verso la pubblicazione passa per la scomposizione della sintesi in
  claim verificabili, per il loro collegamento agli evidence span verificati e
  per il gate; il gate submitter non offre alcuna scorciatoia che salti uno di
  questi passi.

---

## 21. UI implications

Questa sezione descrive come lo schema progettato in questo documento sosterrà
la futura UI di orchestrazione multi-AI. È una descrizione di implicazioni:
nessuna pagina o componente UI viene creato o modificato in questa fase.

### 21.1 Superfici UI future (descrizione concettuale)

Le superfici UI che la linea multi-AI presumibilmente introdurrà:

- **Prompt Master panel** — la superficie di input primario, in cui l'utente
  scrive la domanda, il problema o l'obiettivo.
- **Agents panel** — la superficie in cui l'utente configura uno o più agenti
  AI con nome, provider/modello, ruolo, prompt, vincoli e budget.
- **Source Candidates panel** — la superficie che mostra le fonti candidate
  proposte dagli agenti o fornite dall'utente, con il loro stato di risoluzione
  e verifica.
- **Run Progress panel** — la superficie che mostra l'avanzamento di un
  `OrchestrationRun` in corso.
- **Results panel** — la superficie che mostra il risultato finale: la risposta
  articolata multi-AI e il suo stato di pubblicazione.
- **Technical Report** — la superficie di audit e debug, analoga all'attuale
  Anti-Hallucination Report, estesa al contesto multi-AI.

### 21.2 Chiarimenti vincolanti

- **La UI legge eventi e stati derivati.** Le superfici di osservabilità
  proiettano gli `orchestration_events` e gli stati già persistiti; non
  ricostruiscono lo stato applicando una propria logica.
- **La UI non inventa stati.** Ogni stato mostrato corrisponde a un fatto
  registrato nel backend; la UI non fabbrica stati di run, di agente o di
  pubblicazione che il backend non ha prodotto.
- **La UI non ricalcola il gate.** La decisione di pubblicabilità che la UI
  mostra è letta verbatim da `final_gate_reports`; la UI non ricompone un
  punteggio né riemette una decisione.
- **La UI non presenta mock come reale.** Quando un run è stato eseguito sul
  mock provider, la UI deve dichiararlo; un output mock non va presentato come
  una sintesi multi-AI reale.
- **`/requests/new` resta base tecnica/dev flow, non esperienza finale Prompt
  Master → agents.** Il flusso di creazione task attuale è utile come
  riferimento implementativo e come dev flow, ma non è la futura esperienza
  principale orientata al Prompt Master e agli agenti.
- **Il report tecnico è audit/debug, non output principale.** Il Technical
  Report — con claim, fonti, evidence span, limiti, gap e decisione del gate —
  è un livello di audit e debug; non è l'output principale del prodotto.
- **L'output principale futuro è una risposta articolata controllata rispetto
  alle evidenze disponibili.** L'esito che l'utente attende è una risposta
  leggibile, controllata rispetto alle fonti disponibili, non un report
  tecnico; la risposta non promette verità assoluta.
- **La UI deve mostrare chiaramente quando le source candidates sono non ancora
  verificate.** Una fonte proposta da un agente è una `source_candidate` finché
  non è stata risolta e verificata; la UI deve distinguere visivamente le fonti
  candidate non verificate dalle evidenze verificate.
- **La UI deve distinguere risposta candidata, risposta pubblicabile e
  publication held.** Una `candidate_syntheses` è una risposta candidata finché
  non ha attraversato il gate; la UI deve distinguere chiaramente lo stato di
  risposta candidata, lo stato di risposta pubblicabile e lo stato di
  publication held.

---

## 22. Non-goals

Questa fase, e il documento che produce, hanno i seguenti **non-goals
espliciti**. Nessuno di questi va perseguito in ORCH-SCHEMA-PRE:

- **nessuna migration** — nessun file viene creato o modificato in `migrations/`;
- **nessun codice** — nessun file di backend, worker, frontend o pacchetto
  condiviso viene scritto o modificato;
- **nessun provider reale** — nessun OpenAI, Anthropic, Gemini o altro provider
  esterno viene introdotto o referenziato in modo operativo;
- **nessun local LLM** — nessun modello AI locale viene introdotto o integrato;
- **nessun endpoint** — nessuna route HTTP viene aggiunta o implementata;
- **nessun worker** — nessun consumer o componente worker viene creato o
  modificato;
- **nessuna UI** — nessuna pagina o componente frontend viene creato o
  modificato;
- **nessuna PDF/image ingestion** — nessuna pipeline di ingestione di PDF o
  immagini viene introdotta;
- **nessun web retrieval reale** — nessun recupero reale di fonti web viene
  implementato;
- **nessun gate parallelo** — nessuna seconda autorità di decisione di
  pubblicabilità viene introdotta accanto al Final Answer Gate esistente;
- **nessuna promessa di verità assoluta** — il documento non dichiara che il
  sistema produca risposte "vere" o che decida vero/falso in senso assoluto;
- **nessun loop agentico non bounded** — lo schema non prevede né incoraggia
  cicli di orchestrazione non bounded; l'orchestrazione resta a passi finiti e
  predeterminati.

---

## 23. Acceptance criteria

Il documento `PHASE_ORCH_SCHEMA_PRE.md` è accettabile se e solo se:

- **crea o modifica solo `PHASE_ORCH_SCHEMA_PRE.md`** — nessun altro file del
  repository è creato o modificato;
- **è in italiano** — l'intero documento è redatto in italiano tecnico;
- **contiene le sezioni 1-24** — l'indice elenca 24 sezioni e tutte sono
  presenti nel corpo, nell'ordine;
- **le sezioni 19-24 sono esattamente:**
  - API implicate, solo concettuali;
  - Worker implications;
  - UI implications;
  - Non-goals;
  - Acceptance criteria;
  - Comandi di verifica;
- **distingue esistente / placeholder / proposto / futuro** — per ogni elemento
  il documento dichiara se è esistente, placeholder, proposto, fuori scope o
  futuro, e non descrive come implementato alcuno stato che non esiste;
- **mantiene le fonti utente opzionali** — le fonti caricate dall'utente sono
  trattate come input opzionale, non come passo obbligatorio centrale;
- **tratta le fonti agentiche come source candidates** — una fonte proposta o
  citata da un agente è una `source_candidate`, non un'evidenza già valida;
- **spiega gli snapshot immutabili** — il documento descrive come MasterPrompt e
  AgentConfig vengono congelati in snapshot immutabili al momento dell'avvio di
  un run;
- **preserva l'append-only audit** — le entità di fatto restano append-only e
  auditabili; le transizioni sono eventi, versioni o snapshot, mai riscritture
  silenziose;
- **non bypassa il Final Answer Gate** — ogni percorso verso la pubblicazione
  passa per Claim Extraction, Evidence Binding e Final Answer Gate;
- **non promette provider reali** — il documento non dichiara che provider reali
  siano disponibili o integrati in questa fase;
- **non promette una risposta articolata senza provider/local LLM** — il
  documento non afferma che una vera sintesi multi-AI sia producibile senza un
  provider reale o un modello locale;
- **non usa wording vietato fuori da lista esplicita o comando grep** — i
  termini della lista di banned wording non compaiono come spiegazione
  ordinaria;
- **non contiene blocchi operativi intermedi** — il documento non contiene
  blocchi di lavorazione parziale di tipo parte/fine/output;
- **non modifica codice/migration/test/README/PROJECT_STATE** — `apps/*`,
  `packages/*`, `migrations/*`, i file di test, `README.md` e `PROJECT_STATE.md`
  restano invariati.

---

## 24. Comandi di verifica

I comandi seguenti permettono a un revisore di verificare i criteri di
accettazione della sezione 23 in modo meccanico. Sono comandi di **sola
lettura**: non modificano il repository. Vanno eseguiti dalla radice del
repository. I pattern grep usano parentesi quadre sul primo carattere così che
il comando non intercetti se stesso.

### 24.1 Comandi base

```bash
git diff --check
git diff --stat
git diff --name-only
git status -sb
```

`git diff --check` non deve segnalare errori di whitespace; `git diff --stat` e
`git diff --name-only` mostrano il perimetro delle modifiche; `git status -sb`
mostra lo stato sintetico del branch.

### 24.2 Controllo file singolo atteso

```bash
git diff --name-only
```

Deve mostrare solo:

```
PHASE_ORCH_SCHEMA_PRE.md
```

Nessun altro file del repository deve comparire.

### 24.3 Controllo sezioni

```bash
grep -nE '^## (19|20|21|22|23|24)\.' PHASE_ORCH_SCHEMA_PRE.md
```

Deve mostrare:

- 19. API implicate, solo concettuali
- 20. Worker implications
- 21. UI implications
- 22. Non-goals
- 23. Acceptance criteria
- 24. Comandi di verifica

### 24.4 Controllo wording vietato

Il controllo usa pattern con parentesi quadre sul primo carattere, così che il
comando non intercetti se stesso:

```bash
grep -niE "[t]ruth score|[v]erified true|[v]erified answer|[A]I verified|[f]actually true|[h]allucination eliminated|[h]allucination-free|[g]uaranteed truth|[z]ero hallucinations|[e]ntailed = true|[s]ource quality proves claim|[C]VE-lite proves support|[r]eal NLI|[c]ontradiction detector|[c]itation-to-claim validator" PHASE_ORCH_SCHEMA_PRE.md || true
```

Deve restituire nulla.

### 24.5 Controllo residui di lavorazione parziale

Il controllo usa pattern con parentesi quadre sul primo carattere, così che il
comando non intercetti se stesso:

```bash
grep -niE "[P]ARTE 1/3|[P]ARTE 2/3|[P]ARTE 3/3|[F]ine PARTE|[O]utput finale|in [c]ostruzione su tre parti" PHASE_ORCH_SCHEMA_PRE.md || true
```

Deve restituire nulla.

### 24.6 Controllo frase rischiosa

```bash
grep -ni "[f]inal gate truth" PHASE_ORCH_SCHEMA_PRE.md || true
```

Deve restituire nulla.
