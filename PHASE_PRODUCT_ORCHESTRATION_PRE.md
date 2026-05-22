# PHASE PRODUCT-ORCHESTRATION-PRE

> **Documento di design / architettura — completo.**
> Questo blocco è **solo progettazione**. Non implementa codice di produzione,
> non modifica `apps/web/*`, `apps/api/*`, `apps/worker/*`, `packages/shared/*`,
> non crea migration, non aggiunge dipendenze, non tocca i test, non modifica
> `README.md` né `PROJECT_STATE.md` né alcun altro `PHASE_*_PRE.md`. L'unico
> deliverable è questo file.
>
> Lingua: italiano tecnico, registro da System Architect.
>
> **Promemoria di linguaggio (vincolante per tutta la fase).** Il sistema è
> evidence-first ed evidence-gated. Non promette verità assoluta, non promette
> l'eliminazione totale delle allucinazioni, non dichiara che le sue risposte
> siano "vere". Produce risposte basate sulle evidenze disponibili e può
> trattenere la pubblicazione quando il supporto è insufficiente. Le regole di
> wording (lista vietata e lista sicura) ereditate da `PHASE_UI_CREATE_FLOW_PRE.md`
> §11 e da `PHASE_UI_PRE.md` §3 restano vincolanti per ogni fase di codice che
> seguirà questo documento.
>
> **Nota di coerenza architetturale (vincolante).** In tutto il documento,
> quando un'entità append-only ha "stati" o "transizioni", quelle transizioni
> vanno registrate come **eventi auditabili, versioni o snapshot immutabili**,
> mai come riscrittura silenziosa di un fatto storico già registrato. La
> distinzione operativa da tenere sempre presente è: una *configurazione* (per
> esempio la formulazione di un prompt o i parametri di un agente) può essere
> mutabile **finché un run non l'ha consumata**; un *fatto* (un output prodotto,
> un messaggio scambiato, una decisione del gate, un consumo di token, una
> transizione di stato di un run) è **append-only dopo essere accaduto** e una
> sua "modifica" è sempre una nuova riga, un nuovo evento o una nuova versione,
> mai un update in place.

---

## Indice

1. Scopo della fase
2. Diagnosi dello stato attuale
3. Problema prodotto
4. Visione target
5. Cosa esiste oggi
6. Cosa manca
7. Modello concettuale target
8. UI target
9. Agent configuration
10. Provider abstraction
11. Token budget strategy
12. Multi-agent orchestration strategy
13. Integrazione con Evidence Gate
14. Document ingestion roadmap
15. Backend gaps
16. Worker gaps
17. API roadmap
18. UI roadmap
19. Roadmap incrementale consigliata
20. Non-goals
21. Criteri di accettazione
22. Comandi di verifica

Le sezioni 1-7 definiscono il problema, lo stato attuale e il modello
concettuale target. Le sezioni 8-18 dettagliano UI, configurazione agenti,
astrazione provider, budget token, orchestrazione, integrazione con il gate,
ingestione documenti e i gap di backend/worker/API/UI. Le sezioni 19-22 danno
la roadmap incrementale, i non-goals, i criteri di accettazione e i comandi di
verifica.

---

## 1. Scopo della fase

Questa fase, **PRODUCT-ORCHESTRATION-PRE**, è una fase **esclusivamente di
design e di architettura**. Il suo unico deliverable è il presente documento.
La fase **non**:

- scrive o modifica codice di backend, worker, frontend o pacchetti condivisi;
- crea o modifica migration di database;
- aggiunge dipendenze a qualunque manifest (`package.json`, `pyproject.toml`,
  lockfile);
- introduce provider AI reali o riferimenti operativi a OpenAI, Anthropic,
  Google o altri provider esterni;
- introduce nuove route HTTP o nuove pagine UI;
- modifica i test esistenti o ne aggiunge di nuovi;
- modifica `README.md`, `PROJECT_STATE.md` o documenti `PHASE_*_PRE.md`
  preesistenti.

Lo scopo della fase è **riallineare prodotto e architettura**. Il repository ha
appena chiuso la fase UI-CREATE-FLOW-A (commit `1594d21`, "Add browser request
creation flow"), che ha reso possibile creare un task reale dal browser. Quella
fase è tecnicamente corretta e completa per ciò che si proponeva, ma è stata
deliberatamente costruita attorno al modello *progetto → documenti → task*, che
è il modello dati del nucleo evidence-gated MVP-0 — **non** il modello prodotto
che la visione finale richiede.

Il prodotto desiderato parte da un **Prompt Master** dell'utente, configura più
**agenti AI** con ruoli e prompt specifici, li fa lavorare e confrontare in una
**orchestrazione bounded**, e infine fa passare gli output attraverso l'**engine
Evidence-First** già esistente (Claim Ledger, CVE-lite, Source Quality, Claim
Entailment, Final Answer Gate, Anti-Hallucination Report) prima di pubblicare o
trattenere una risposta.

Esiste quindi un divario tra:

- **dove il prodotto è oggi**: un creatore di task closed-corpus su documenti,
  con una pipeline mock deterministica e un report read-only;
- **dove il prodotto deve arrivare**: un orchestratore multi-AI evidence-gated
  che parte dal Prompt Master.

Questa fase serve a **nominare quel divario in modo preciso**, a definire un
**modello concettuale target** delle entità necessarie, e a creare una base
condivisa su cui le fasi successive (di design di dettaglio prima, e poi di
codice) potranno innestarsi senza inventare comportamenti. È l'equivalente, per
la linea di lavoro multi-AI, di ciò che `PHASE_UI_PRE.md` è stato per la linea
di lavoro UI: un documento decisionale che apre una fase, non la implementa.

**Cosa NON è questa fase.** Non è un impegno di implementazione. Non promette
che le entità descritte nella §7 verranno realizzate nell'ordine o nella forma
qui proposti. Non è un design di schema: descrive le entità in modo testuale e
**non scrive `CREATE TABLE` né migration**. Non sceglie un provider, non sceglie
un formato di prompt, non sceglie un protocollo di orchestrazione. Le decisioni
operative sono rinviate alle sezioni successive di questo documento e alle fasi
`*-PRE` di dettaglio che lo seguiranno.

---

## 2. Diagnosi dello stato attuale

### 2.1 Cosa ha prodotto UI-CREATE-FLOW-A

La fase UI-CREATE-FLOW-A (commit `1594d21`) ha aggiunto al frontend la capacità
di creare un task reale dal browser, senza che l'utente debba conoscere un task
id o interrogare il database. In concreto ha introdotto:

- la route `/requests/new` con un flusso guidato a quattro sezioni — Project,
  Sources, Request, Create task — implementato dal client component
  `NewRequestFlow` e dai suoi sottocomponenti di sezione;
- un client API di create-flow in `apps/web/lib/api.ts`: `listProjects`,
  `createProject`, `listProjectDocuments`, `uploadProjectDocument`,
  `createTask`, ciascuno con il modello di errore tipizzato `ApiError` /
  `ApiNetworkError`;
- un layer di proxy same-origin sotto `apps/web/app/api/ef/*`, necessario
  perché il backend non monta middleware CORS: il browser parla a route handler
  Next.js server-side, che inoltrano al backend in modo verbatim;
- l'invio di un header `Idempotency-Key` su `POST /api/v1/tasks`, generato una
  volta per tentativo di submit e riusato sui retry, così un doppio invio non
  crea due task;
- la navigazione del browser verso `/tasks/<taskId>` su un vero `201`, con
  divieto esplicito di fabbricare task id, success screen o navigazioni.

Questo è un passo tecnico **reale e utile**. Ha eliminato la dipendenza da
`curl`, da `psql` e da test `pytest` per ottenere un task id. Ha stabilito un
pattern di proxy same-origin riutilizzabile. Ha consolidato un client API
tipizzato con un modello di errore coerente.

### 2.2 Perché non è ancora la visione prodotto

Il flusso UI-CREATE-FLOW-A resta però **orientato al modello dati del nucleo
MVP-0**, non al modello prodotto della visione multi-AI. La prima domanda che il
flusso pone all'utente è "in quale **progetto** lavori?", la seconda è "quali
**documenti** alleghi?", la terza è "qual è il tuo **objective**?". Da queste tre
risposte costruisce un `closed_corpus` task e lo invia al worker.

Questo significa che, dal punto di vista del prodotto:

- l'unità mentale primaria che la UI espone è il **task closed-corpus**, non il
  **Prompt Master**. L'objective è un campo testuale dentro un form di
  creazione task; non è ancora l'entità centrale, persistente, configurabile,
  attorno a cui ruota tutta l'esperienza;
- non esiste alcun concetto di **agente AI**: la UI non permette di aggiungere
  agenti, di assegnare loro un provider/modello, un ruolo, un prompt specifico,
  un compito, dei vincoli, un token budget;
- non esiste alcun concetto di **orchestrazione**: non c'è confronto fra
  agenti, non c'è critic review, non c'è sintesi multi-AI, non c'è audit degli
  output intermedi;
- la pipeline che processa il task è interamente **mock/deterministica**:
  extractor mock, CVE-lite mock, Source Quality mock, Claim Entailment mock,
  compiler mock, Final Answer Gate mock. Nessun output proviene da una AI reale,
  perché in MVP-0 vige `PROVIDERS_ENABLED=mock` e `MAX_COST_PER_TASK=0`.

In altre parole: UI-CREATE-FLOW-A ha reso *usabile dal browser* il nucleo
evidence-gated esistente, ma quel nucleo è stato costruito — per scelta
deliberata, documentata in `PROJECT_STATE.md` — **prima** della visione
multi-AI. Il claim "evidence-gated" oggi significa che esiste una base
append-only verificabile end-to-end (draft / gate / published), una pipeline di
controllo deterministica sopra di essa, e una superficie di osservabilità
read-only. **Non** significa ancora che esista un orchestratore di AI.

### 2.3 Inquadramento

UI-CREATE-FLOW-A va quindi letta come l'**ultimo gradino della linea di lavoro
UI sopra il modello closed-corpus**, non come il primo gradino della linea di
lavoro multi-AI. Le due linee non sono in conflitto: la linea multi-AI può
riusare quasi tutto ciò che la linea UI/closed-corpus ha costruito (vedi §5).
Ma vanno tenute concettualmente distinte, perché aprire la linea multi-AI
richiede entità, contratti e superfici che oggi semplicemente non esistono
(vedi §6 e §7).

La diagnosi, in una frase: **il prodotto oggi sa creare e osservare task
evidence-gated; non sa ancora partire da un Prompt Master e coordinare più AI.**

---

## 3. Problema prodotto

### 3.1 La home attuale non rappresenta l'esperienza attesa

La home page attuale (`apps/web/app/page.tsx`, aggiornata da UI-CREATE-FLOW-A) è
sobria, onesta e ben strutturata: ha un hero, una CTA primaria "New
evidence-based request", una CTA secondaria "Open existing task", una
spiegazione del workflow in sette step, le limitazioni MVP-0. È un netto
miglioramento rispetto allo stub di Fase 8.1b.

Ma resta una home **costruita attorno al ciclo progetto → documenti → task →
report**. Un utente umano che arriva con un problema reale — "voglio capire X,
e ho queste fonti, e vorrei che più modelli AI ci lavorassero e si
confrontassero" — non trova un percorso che rifletta quella aspettativa. Trova
invece un percorso che gli chiede prima di tutto di scegliere un *progetto*
amministrativo e di allegare *documenti*, e che tratta la sua domanda come un
campo `objective` di un task.

Il problema non è la qualità dell'implementazione attuale: è che la **forma del
prodotto** non corrisponde ancora al **modello mentale dell'utente**.

### 3.2 Cosa si aspetta un utente umano

Un utente che si avvicina a un prodotto descritto come "piattaforma multi-AI
evidence-first" si aspetta, in ordine naturale, di poter:

1. **scrivere il proprio Prompt Master** — la domanda, il problema o
   l'obiettivo — in un campo centrale e prominente, come input primario del
   prodotto. Non come campo secondario di un form di creazione task;

2. **fornire fonti** — caricare testo e Markdown oggi, PDF e immagini in
   futuro — e capire che queste fonti sono il corpus su cui il sistema lavora
   in modalità controllata;

3. **configurare gli agenti AI** — aggiungere uno o più agenti, e per ciascuno
   indicare nome, provider/modello, ruolo, prompt specifico, compito, vincoli,
   token budget, output atteso, e l'eventuale ruolo di reviewer o di
   synthesizer;

4. **far partire una esecuzione** in cui gli agenti lavorano sul Prompt Master e
   sulle fonti, in modo indipendente o coordinato, con un confronto bounded e
   non una chat infinita;

5. **vedere un risultato evidence-gated** — una risposta originale multi-AI, con
   evidenze tracciabili, limiti dichiarati, contraddizioni o gap individuati, e
   uno stato esplicito di *publication allowed* oppure *publication held*, più
   un report tecnico per audit e debugging.

Oggi l'utente può fare, dal browser, solo i punti 2 e (in forma ridotta) 5: può
caricare documenti `.txt`/`.md` e può osservare il report di un task. Non può
fare nulla dei punti 1, 3, 4 nella forma che il prodotto promette, perché le
entità e le superfici corrispondenti non esistono.

### 3.3 Il divario, in termini di prodotto

Il divario di prodotto si può riassumere così:

| Dimensione | Esperienza attesa | Esperienza attuale |
|---|---|---|
| Punto di partenza | Prompt Master come input centrale | Selezione di un progetto amministrativo |
| Fonti | Testo, Markdown, (futuro) PDF e immagini | Solo testo e Markdown |
| Agenti AI | Più agenti configurabili con ruolo e prompt | Nessun concetto di agente |
| Esecuzione | Orchestrazione bounded multi-AI | Pipeline mock deterministica single-path |
| Verifica | Output AI reali verificati dall'engine | Claim mock verificati dall'engine |
| Risultato | Sintesi originale multi-AI evidence-gated | Task summary derivato da pipeline mock |

Questa fase **non chiude** il divario — chiuderlo è lavoro di molte fasi di
codice. Questa fase lo **descrive con precisione** e prepara il modello
concettuale (§7) necessario a chiuderlo per gradi.

### 3.4 Vincolo di onestà

Va ribadito un vincolo che attraversa tutto il problema prodotto: l'esperienza
target **non deve mai promettere verità assoluta**. Anche quando il prodotto
coordinerà più AI reali, la sua promessa resta quella attuale, estesa al
contesto multi-AI: produce una *sintesi multi-AI controllata rispetto alle fonti
disponibili*, può *trattenere la pubblicazione quando il supporto è
insufficiente*, e *non garantisce la verità fattuale nel mondo*. La forma del
prodotto cambia; la promessa epistemica no.

---

## 4. Visione target

### 4.1 Il flusso target end-to-end

La visione prodotto è un flusso che parte dal Prompt Master e termina con una
risposta finale pubblicata o trattenuta, evidence-gated. In forma lineare:

```
Prompt Master
   │
   ▼
Sources (testo / Markdown / futuro PDF / futuro immagini / web / interne)
   │
   ▼
AI Agents (configurazione: nome, provider, ruolo, prompt, vincoli, token budget)
   │
   ▼
Orchestration Run (esecuzione coordinata o indipendente, bounded)
   │
   ▼
Agent Outputs (output reali per agente, auditabili, con token accounting)
   │
   ▼
Cross Review (confronto bounded fra agenti, critic review, contraddizioni/gap)
   │
   ▼
Candidate Synthesis (sintesi multi-AI originale, ancora non pubblicata)
   │
   ▼
Claim Extraction (la sintesi viene scomposta in claim verificabili)
   │
   ▼
Evidence Binding (i claim vengono collegati a evidence span delle fonti)
   │
   ▼
Final Answer Gate (CVE-lite + Source Quality + Claim Entailment → decisione)
   │
   ▼
Published / Held Final Answer (publication allowed oppure publication held)
```

### 4.2 Lettura del flusso, stadio per stadio

**Prompt Master.** L'utente esprime la domanda, il problema o l'obiettivo. È
l'input primario del prodotto e l'entità centrale persistente a cui tutto il
resto è agganciato.

**Sources.** L'utente allega le fonti su cui il sistema deve lavorare. In MVP-0
e nel breve termine sono testo e Markdown; PDF e immagini, e le modalità web o
interne, sono fasi dedicate successive. Le fonti restano il corpus di
riferimento: il prodotto rimane evidence-gated, quindi la risposta finale deve
essere ancorabile alle fonti disponibili.

**AI Agents.** L'utente configura uno o più agenti. Ogni agente porta una
configurazione: nome, provider/modello, ruolo, prompt specifico, compito,
vincoli, token budget atteso, output atteso, ed eventualmente un ruolo
funzionale di reviewer o di synthesizer. La configurazione è *mutabile* finché
una esecuzione non viene avviata.

**Orchestration Run.** Una esecuzione coordina gli agenti. Gli agenti possono
lavorare in modo indipendente (ognuno produce il proprio output sul Prompt
Master e sulle fonti) oppure coordinato (un confronto strutturato). Il confronto
è **bounded**: un numero finito e predeterminato di passi, non una chat infinita
fra agenti. L'esecuzione è la radice di audit di tutto ciò che accade durante un
run.

**Agent Outputs.** Ogni agente produce uno o più output reali. Gli output sono
**auditabili** e **append-only**: una volta registrati non vengono riscritti.
Ogni output porta un token accounting (token consumati, costo) confrontato con
il token budget configurato.

**Cross Review.** Gli output degli agenti vengono confrontati. Un eventuale
agente con ruolo reviewer (o un passo di critic review) individua punti deboli,
contraddizioni, gap di copertura fra gli output. Anche questo passo è bounded.

**Candidate Synthesis.** Dal confronto emerge una sintesi multi-AI originale: un
*candidate*, non ancora una risposta pubblicata. È esplicitamente una
*candidate answer* finché non ha attraversato il gate.

**Claim Extraction.** La candidate synthesis viene scomposta in claim
verificabili, esattamente come oggi l'extractor scompone un draft in claim. È il
punto di innesto fra la nuova catena multi-AI e il Claim Ledger esistente.

**Evidence Binding.** I claim estratti vengono collegati a evidence span delle
fonti disponibili, popolando `claim_evidence_links` come fa già la pipeline
attuale.

**Final Answer Gate.** Il gate esistente compone gli assi — CVE-lite, Source
Quality, Claim Entailment — e decide la pubblicabilità secondo una policy
versionata. Il gate **non** è una decisione di verità: è una decisione di
*publication allowed* / *publication held*.

**Published / Held Final Answer.** L'esito finale: una risposta originale
multi-AI con evidenze tracciabili, limiti dichiarati, contraddizioni o gap, e
uno stato di pubblicazione esplicito, più il report tecnico per audit.

### 4.3 Principio di riuso

Il punto architetturale chiave della visione target è che **la metà destra del
flusso esiste già**. Da *Claim Extraction* in poi — Claim Extraction, Evidence
Binding, Final Answer Gate, Published/Held — la catena coincide con la pipeline
evidence-gated attuale. La visione target non riscrive l'engine: lo **alimenta
da una nuova sorgente**. La parte nuova è la metà sinistra del flusso — Prompt
Master, Sources estese, AI Agents, Orchestration Run, Agent Outputs, Cross
Review, Candidate Synthesis — e il **punto di giunzione** in cui una Candidate
Synthesis multi-AI viene trasformata nell'input che la Claim Extraction sa già
consumare.

### 4.4 Cosa la visione target NON promette

La visione target, anche a regime, **non**:

- promette verità assoluta o risposte "vere";
- promette l'eliminazione totale delle allucinazioni;
- confonde i sei assi distinti — claim correctness, evidence support, CVE-lite
  verification, source quality, claim entailment, final gate truth — in un unico
  punteggio;
- trasforma il Final Answer Gate in un giudice di verità nel mondo;
- trasforma il report o la UI in superfici che prendono nuove decisioni.

La visione target estende la *forma* del prodotto, non la sua *promessa
epistemica*.

---

## 5. Cosa esiste oggi

Questa sezione elenca i componenti già presenti e **riutilizzabili** dalla linea
di lavoro multi-AI. La fonte è `PROJECT_STATE.md` (stato al commit `af74187`),
`README.md`, `PHASE_UI_CREATE_FLOW_PRE.md` e i file `apps/web/*` forniti. La
fase di codice futura dovrà riverificare ogni elemento contro il proprio HEAD
prima di basarsi su di esso.

### 5.1 Dominio e nucleo dati

- **projects** — entità progetto con `POST /api/v1/projects`, `GET
  /api/v1/projects`, `GET /api/v1/projects/{id}`. Multi-tenant a livello DB, ma
  in MVP-0 con un solo tenant `dev` seeded e nessuna auth.
- **documents .txt/.md** — upload reale via `POST
  /api/v1/projects/{id}/documents`, lista via `GET
  /api/v1/projects/{id}/documents`. L'upload produce `uploaded_documents`, una
  `document_versions` `parsed`, `document_chunks` deterministici e uno
  `evidence_spans` per chunk. Estensioni `.txt`/`.md`, limite 50 MiB.
- **tasks closed_corpus** — `POST /api/v1/tasks` crea un task `closed_corpus`
  da `project_id`, `objective`, `mode`, `document_ids` e una `policy` opzionale;
  supporta `Idempotency-Key`. `GET /api/v1/tasks/{id}` legge il task.

### 5.2 Pipeline e worker

- **worker `task.created`** — consumer single-consumer FK-safe, resume-safe,
  idempotente, che processa l'evento `task.created` pubblicato su Redis. La
  pipeline approved produce 15 eventi audit worker-side.
- **Claim Ledger** — base append-only stretta: `logical_claims`, `raw_claims`,
  `classified_claims`, `claim_ledger_entries`, `claim_lineage`,
  `claim_evidence_links`, `verification_records`. Supersede esclusivamente via
  `claim_lineage(relation_kind='supersedes')`.
- **CVE-lite** — controllo mock-driven di presenza testuale della quote nel
  chunk e di hash della quote. Scrive `verification_records`. Non valuta
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
- **compiler mock** — produce `draft_final_answers` v1 con `final_answer_spans`
  1:1 sui claim `verified_fact`.

### 5.3 Osservabilità

- **Anti-Hallucination Report API** — `GET
  /api/v1/tasks/{task_id}/anti-hallucination-report`, vista task-level
  read-only aggregata su publication, gate, claims, evidence, CVE-lite, Source
  Quality, Claim Entailment, axis_summary, mock_indicators, limitations. Non
  ricalcola il gate, non muta DB.
- **endpoint read specialistici** — answers (`/draft`, `/final-gate-report`,
  `/published-answer`), claims (`/raw-claims`, `/classified-claims`, `/claims`,
  `/claims/{id}/history`, `/claims/{id}/evidence`), documents, source quality
  (8.7F), claim-entailment task-level (8.8A-READ-A), lifecycle/source-loss
  (8.6), audit (`/tasks/{id}/audit`).

### 5.4 Frontend

- **Task Summary UI** — `/tasks/[taskId]`, vista user-facing derivata dal
  report, con `TaskSummaryView` e i suoi quattro check card.
- **Anti-Hallucination Report UI** — `/tasks/[taskId]/report`, viewer tecnico
  read-only, con `PublicationPanel`, `GatePanel`, `AxisSummaryCards`,
  `MockIndicatorsPanel`, `LimitationsPanel`, `RawJsonCollapsible`,
  `ReportStatusBadge`.
- **`/requests/new`** — flusso guidato di creazione task a quattro sezioni
  (Project, Sources, Request, Create task), implementato da `NewRequestFlow`.
- **same-origin proxy `/api/ef/*`** — route handler Next.js server-side che
  inoltrano al backend in modo verbatim, aggirando l'assenza di CORS.
- **client API tipizzato** — `apps/web/lib/api.ts` con modello di errore
  `ApiError` / `ApiNetworkError` e helper di create-flow.

### 5.5 Infrastruttura trasversale

- **audit chain** hash-linked, append-only, verificabile end-to-end via
  `verify_audit_chain` / `verify_task_audit_chain`, con trigger DB
  `reject_modify_append_only`.
- **storage** content-addressed, deduplicato, refcount-based.
- **event_processing_records** con idempotenza per consumer.
- **policy_versions** — meccanismo di versionamento di policy già usato dal
  gate e dai checker.

### 5.6 Sintesi del riuso

La metà destra del flusso target (§4.1, da Claim Extraction in poi) è coperta da
componenti maturi: Claim Ledger, CVE-lite, Source Quality, Claim Entailment,
Final Answer Gate, Anti-Hallucination Report. L'infrastruttura trasversale —
audit append-only, storage, idempotenza, policy versioning, proxy same-origin,
client API tipizzato — è anch'essa riusabile quasi integralmente. La linea di
lavoro multi-AI **non parte da zero**: parte da un nucleo evidence-gated
funzionante e da una superficie UI/observability consolidata.

---

## 6. Cosa manca

Questa sezione elenca i gap: ciò che la visione target richiede e che oggi
**non esiste**. Ogni voce è un candidato a diventare oggetto di una o più fasi
`*-PRE` di dettaglio e poi di codice. Nessuna voce è qui un impegno di
implementazione.

### 6.1 Entità e dominio mancanti

- **master prompt entity** — non esiste un'entità persistente "Prompt Master".
  Oggi la domanda dell'utente vive come campo `objective` di un task.
- **agent config** — non esiste alcuna entità di configurazione di un agente AI.
- **agent role prompt** — non esiste un'entità che rappresenti il ruolo e il
  prompt specifico assegnati a un agente.
- **provider abstraction** — non esiste un'astrazione di provider che permetta
  di parlare in modo uniforme a OpenAI, Anthropic, Gemini o a un modello locale.
- **provider credentials/config** — non esiste alcun meccanismo di gestione di
  credenziali o configurazione di provider esterni.
- **orchestration run** — non esiste un'entità che rappresenti una esecuzione di
  orchestrazione multi-AI.
- **agent messages** — non esiste un'entità per i messaggi scambiati o prodotti
  dagli agenti durante un run.
- **agent outputs** — non esiste un'entità per gli output reali prodotti dagli
  agenti. (La migration `0005` ha creato tabelle placeholder `agent_runs` e
  `agent_outputs`, ma sono **vuote** e non hanno semantica operativa: vanno
  considerate un segnaposto, non un componente esistente.)
- **token accounting** — non esiste contabilizzazione di token o costo per
  agente o per run.
- **bounded debate/review** — non esiste alcun meccanismo di confronto o critic
  review fra agenti.
- **synthesis candidate** — non esiste un'entità di sintesi multi-AI candidata.
- **mapping synthesis → claim pipeline** — non esiste il punto di giunzione che
  trasforma una Candidate Synthesis nell'input che la Claim Extraction consuma.

### 6.2 Capacità di ingestione mancanti

- **PDF ingestion** — non esiste l'ingestione di PDF come fonte nativa.
- **image ingestion** — non esiste l'ingestione di immagini come fonte nativa.
- **OCR/vision** — non esiste alcuna pipeline OCR o vision.
- **web source mode** — non esiste una modalità di fonte web (Verified Web
  Mode / Web-RAG).

### 6.3 Capacità di esecuzione mancanti

- **real provider calls** — non esiste alcuna chiamata a provider AI reali. In
  MVP-0 vige `PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0`.
- **consensus engine reale** — non esiste un motore di consenso multi-AI.
- **critical reviewer reale** — non esiste un reviewer adversariale reale.
- **rilevazione avanzata delle contraddizioni** — non esiste un componente reale per
  individuare contraddizioni (il mock entailment non emette mai `contradicted`).

### 6.4 Superfici UI/observability mancanti

- **progress/events UI** — non esiste una superficie che mostri il progresso di
  un run o gli eventi intermedi di un'orchestrazione. La UI attuale è
  reload-based e non ha polling né streaming.
- non esiste alcuna superficie di configurazione del Prompt Master, degli
  agenti, dell'orchestrazione; non esiste una superficie che mostri output per
  agente, cross-review, candidate synthesis.

### 6.5 Lettura del gap

Il gap è **interamente sulla metà sinistra del flusso target** (§4.1) e sul
punto di giunzione con la metà destra. La metà destra (Claim Extraction →
Published/Held) non ha gap strutturali: ha solo l'esigenza di poter essere
alimentata da una sorgente diversa dal compiler mock attuale. Il lavoro mancante
è quindi, in ordine concettuale: (1) introdurre le entità del dominio multi-AI
(§6.1), (2) introdurre l'astrazione di provider e le chiamate reali (§6.3), (3)
introdurre l'orchestrazione bounded e la sintesi, (4) collegare la sintesi alla
pipeline di claim esistente, (5) estendere l'ingestione (§6.2) e le superfici UI
(§6.4). L'ordinamento operativo di questo lavoro è materia della roadmap
incrementale (§19).

---

## 7. Modello concettuale target

Questa sezione definisce in modo **testuale** le entità del modello concettuale
target. Per ciascuna entità si descrivono: scopo, campi probabili, relazioni,
cosa deve essere append-only o auditabile, e cosa può essere configurazione
mutabile.

> **Avvertenze vincolanti per questa sezione.**
> - Le entità qui descritte sono un **modello concettuale**, non uno schema.
>   Non si scrivono `CREATE TABLE`, non si definiscono tipi SQL, non si crea
>   alcuna migration. I "campi probabili" sono indicativi e soggetti a revisione
>   nelle fasi di design di dettaglio.
> - La distinzione **append-only/auditabile vs configurazione mutabile** è il
>   criterio architetturale centrale: ciò che registra *un fatto accaduto*
>   (un output, un messaggio, una decisione, un consumo di token) deve essere
>   append-only e auditabile, coerentemente con l'invariante del Claim Ledger e
>   dell'audit chain; ciò che registra *un'intenzione configurabile* (la
>   formulazione di un prompt, i parametri di un agente) può essere mutabile
>   finché un run non lo ha consumato.
> - Le entità della metà destra del flusso — `ExtractedClaim`,
>   `EvidenceBinding`, `GateEvaluation` — sono descritte qui per **completezza
>   del modello concettuale**, ma corrispondono in larga parte a entità già
>   esistenti nel Claim Ledger e nel gate (vedi §5). Per esse il lavoro futuro è
>   prevalentemente di *collegamento*, non di creazione.

### 7.1 MasterPrompt

- **Scopo.** Rappresentare l'input primario del prodotto: la domanda, il
  problema o l'obiettivo dell'utente. È l'entità centrale persistente attorno
  alla quale ruotano fonti, agenti ed esecuzioni. Sostituisce, come unità
  mentale primaria, l'attuale `objective` di un task.
- **Campi probabili.** Identificatore; tenant; testo del prompt; titolo o label
  breve opzionale; stato (per esempio *draft* / *ready* / *archived*);
  riferimento all'autore; timestamp di creazione e di ultima modifica;
  eventuale riferimento al progetto se la nozione di progetto viene mantenuta.
- **Relazioni.** Un MasterPrompt è associato a zero o più `SourceInput`; a zero
  o più `AgentConfig`; a zero o più `OrchestrationRun`.
- **Append-only / auditabile.** Il MasterPrompt in sé è **configurazione
  mutabile** finché nessun `OrchestrationRun` lo ha consumato. Tuttavia,
  **ogni `OrchestrationRun` deve fissare uno snapshot immutabile del testo del
  prompt** al momento dell'avvio: il run deve sapere su quale formulazione esatta
  ha lavorato, e quella formulazione non deve poter cambiare retroattivamente.
- **Configurazione mutabile.** Testo, titolo, stato sono modificabili finché il
  prompt non è stato consumato da un run.

### 7.2 SourceInput

- **Scopo.** Rappresentare una fonte fornita per un MasterPrompt: un documento
  di testo o Markdown oggi; un PDF, un'immagine, una fonte web o interna in
  futuro. È il riferimento logico alla fonte, distinto dal contenuto ingerito.
- **Campi probabili.** Identificatore; riferimento al MasterPrompt; tipo di
  fonte (per esempio *text*, *markdown*, *pdf*, *image*, *web*, *internal*);
  riferimento all'eventuale documento già esistente nel document store;
  metadati di origine (nome file, URL, descrizione); stato di ingestione;
  timestamp.
- **Relazioni.** Un SourceInput appartiene a un MasterPrompt; può avere una o
  più `SourceIngestion` associate (per esempio una per versione o per tentativo
  di parsing).
- **Append-only / auditabile.** Il *riferimento* alla fonte e i suoi metadati di
  origine dovrebbero essere trattati come **append-only** una volta che la
  fonte è stata allegata: coerentemente con la natura append-only di
  `uploaded_documents` ed `evidence_spans`, una fonte allegata non viene
  riscritta. Una rimozione, se mai supportata, andrà modellata come evento, non
  come delete.
- **Configurazione mutabile.** Eventuale label o descrizione editoriale può
  restare mutabile; il contenuto e l'origine no.

### 7.3 SourceIngestion

- **Scopo.** Rappresentare l'esito dell'ingestione di un `SourceInput`: il
  parsing, il chunking, e — per PDF e immagini — l'eventuale OCR o vision. È il
  ponte fra la fonte logica e gli `evidence_spans` su cui l'engine lavora.
- **Campi probabili.** Identificatore; riferimento al `SourceInput`; metodo di
  ingestione (per esempio *plain text parse*, *markdown parse*, *pdf text
  extraction*, *ocr*, *vision*); esito (success / partial / failed);
  riferimenti agli artefatti prodotti (versione documento, chunk, evidence
  span); flag che indicano se sono stati usati strumenti mock o reali; note
  diagnostiche; timestamp.
- **Relazioni.** Una SourceIngestion appartiene a un `SourceInput`; produce
  riferimenti a `document_versions`, `document_chunks`, `evidence_spans`
  esistenti.
- **Append-only / auditabile.** **Append-only e auditabile.** Una
  SourceIngestion registra un fatto accaduto — "questa fonte è stata ingerita in
  questo modo con questo esito" — e non deve essere riscritta. Una nuova
  ingestione (per esempio un re-parse) è una nuova riga, non un update.
- **Configurazione mutabile.** Nessuna: è interamente un record di esito.

### 7.4 AgentConfig

- **Scopo.** Rappresentare la configurazione di un agente AI: il "chi" e il
  "come" di un partecipante all'orchestrazione. È l'entità che l'utente compila
  quando aggiunge un agente.
- **Campi probabili.** Identificatore; riferimento al MasterPrompt (o a un
  contenitore di orchestrazione); nome dell'agente; provider/modello scelto
  (riferimento a un'astrazione di provider); riferimento all'`AgentRolePrompt`;
  compito sintetico; vincoli (per esempio formato di output atteso, lunghezza,
  divieti); token budget atteso; ruolo funzionale (per esempio *worker*,
  *reviewer*, *synthesizer*); ordine o priorità nell'orchestrazione; timestamp.
- **Relazioni.** Un AgentConfig appartiene a un MasterPrompt; riferisce un
  `AgentRolePrompt`; viene consumato da uno o più `AgentRun` quando un
  `OrchestrationRun` parte.
- **Append-only / auditabile.** L'AgentConfig è **configurazione mutabile**
  finché nessun run lo ha consumato. Come per il MasterPrompt, **ogni
  `OrchestrationRun` deve fissare uno snapshot immutabile della configurazione
  di ogni agente** al momento dell'avvio, così il run è auditabile rispetto alla
  configurazione esatta che ha usato.
- **Configurazione mutabile.** Tutti i campi, finché non consumati da un run.

### 7.5 AgentRolePrompt

- **Scopo.** Rappresentare il ruolo e il prompt specifico assegnati a un agente:
  *che cosa* quell'agente deve fare e *con quali istruzioni*. È separato da
  `AgentConfig` perché un ruolo/prompt può essere riusabile o versionabile
  indipendentemente dai parametri operativi dell'agente.
- **Campi probabili.** Identificatore; nome o label del ruolo; testo del prompt
  di ruolo; categoria di ruolo (per esempio *researcher*, *critic*,
  *synthesizer*); versione; timestamp.
- **Relazioni.** Un AgentRolePrompt è riferito da uno o più `AgentConfig`.
- **Append-only / auditabile.** Il testo del prompt di ruolo, **una volta
  consumato da un run**, deve essere immobilizzato per quel run (via lo snapshot
  di `AgentConfig`, oppure via un meccanismo di versionamento analogo a
  `policy_versions`). La definizione di ruolo come *catalogo* può essere
  configurazione mutabile; la *versione consumata da un run* no.
- **Configurazione mutabile.** Il catalogo dei ruoli/prompt è mutabile; una
  versione già consumata è immutabile.

### 7.6 AgentRun

- **Scopo.** Rappresentare l'esecuzione di un singolo agente all'interno di un
  `OrchestrationRun`: l'istanza concreta in cui un `AgentConfig` viene attivato
  e produce output.
- **Campi probabili.** Identificatore; riferimento all'`OrchestrationRun`;
  riferimento (snapshot) all'`AgentConfig`; stato (per esempio *pending* /
  *running* / *succeeded* / *failed*); token consumati; costo; timestamp di
  avvio e di completamento; eventuale codice di errore.
- **Relazioni.** Un AgentRun appartiene a un `OrchestrationRun`; consuma uno
  snapshot di `AgentConfig`; produce zero o più `AgentMessage` e uno o più
  `AgentOutput`; ha un `TokenBudget` di riferimento e un consumo registrato.
- **Append-only / auditabile.** **Append-only e auditabile.** Un AgentRun
  registra un fatto accaduto. Le transizioni di stato e i valori finali (token,
  costo, esito) devono essere tracciabili nell'audit chain. Un nuovo tentativo è
  un nuovo AgentRun, non un update di quello fallito.
- **Configurazione mutabile.** Nessuna.

### 7.7 AgentMessage

- **Scopo.** Rappresentare un singolo messaggio scambiato o prodotto nel corso
  di un `AgentRun` o di un confronto fra agenti: una richiesta inviata a un
  provider, una risposta ricevuta, un messaggio di confronto fra agenti.
- **Campi probabili.** Identificatore; riferimento all'`AgentRun` (e/o
  all'`OrchestrationRun`); ruolo del messaggio (per esempio *system*, *user*,
  *assistant*, *review*); contenuto testuale; indice o ordinamento; token
  associati; timestamp.
- **Relazioni.** Un AgentMessage appartiene a un `AgentRun`; può essere
  collegato a uno specifico `AgentOutput`.
- **Append-only / auditabile.** **Append-only e auditabile.** I messaggi sono il
  livello più fine di audit di un run e non devono mai essere riscritti né
  cancellati. Sono il materiale grezzo che rende un'orchestrazione ispezionabile.
- **Configurazione mutabile.** Nessuna.

### 7.8 AgentOutput

- **Scopo.** Rappresentare l'output reale prodotto da un agente: il risultato di
  un `AgentRun`, nella forma che le fasi successive (Cross Review, Candidate
  Synthesis) consumano.
- **Campi probabili.** Identificatore; riferimento all'`AgentRun`; contenuto
  dell'output; tipo o formato; token dell'output; eventuali metadati
  strutturati (per esempio se l'output è una lista di affermazioni); timestamp.
- **Relazioni.** Un AgentOutput appartiene a un `AgentRun`; viene consumato da
  `CrossAgentReview` e da `CandidateSynthesis`.
- **Append-only / auditabile.** **Append-only e auditabile.** Coerente con la
  natura append-only di `final_answer_spans` e `published_answers`. Un output
  registrato è un fatto: non si riscrive. Una nuova versione di output
  corrisponde a un nuovo `AgentRun`.
- **Configurazione mutabile.** Nessuna.

### 7.9 TokenBudget

- **Scopo.** Rappresentare il budget di token (e, per estensione, di costo)
  associato a un agente, a un run o all'intera orchestrazione, e fornire il
  riferimento contro cui il consumo reale viene confrontato.
- **Campi probabili.** Identificatore; livello a cui il budget si applica (per
  esempio *per-agent*, *per-run*, *per-orchestration*); riferimento all'entità a
  cui appartiene; limite di token; eventuale limite di costo; politica di
  superamento (per esempio *hard stop* / *warn*); timestamp.
- **Relazioni.** Un TokenBudget è associato a un `AgentConfig`, a un `AgentRun`
  o a un `OrchestrationRun`; il consumo reale è registrato sugli `AgentRun` e
  aggregato sull'`OrchestrationRun`.
- **Append-only / auditabile.** Il budget *come limite configurato* è
  **configurazione mutabile** finché non consumato da un run; il budget
  *fissato da uno specifico run* (lo snapshot) e il *consumo reale* registrato
  contro di esso sono **append-only e auditabili**.
- **Configurazione mutabile.** Il limite configurato, prima del consumo.

### 7.10 OrchestrationRun

- **Scopo.** Rappresentare una singola esecuzione di orchestrazione multi-AI: la
  radice che lega un MasterPrompt, le sue fonti, gli agenti configurati, e tutto
  ciò che accade da Agent Outputs a Published/Held. È l'analogo, per la linea
  multi-AI, di ciò che `task_masters` è per la linea closed-corpus.
- **Campi probabili.** Identificatore; tenant; riferimento (snapshot) al
  MasterPrompt; riferimenti (snapshot) agli `AgentConfig` partecipanti; modalità
  di orchestrazione (per esempio *independent* / *coordinated*); parametri di
  bounding (numero massimo di passi di confronto); stato complessivo;
  riferimento all'eventuale `CandidateSynthesis` prodotta; riferimento
  all'eventuale `GateEvaluation` e `PublishedOrHeldAnswer`; aggregato di token e
  costo; timestamp di avvio e completamento.
- **Relazioni.** Un OrchestrationRun appartiene a un MasterPrompt; contiene uno
  o più `AgentRun`; può avere uno o più `CrossAgentReview`; produce al più una
  `CandidateSynthesis`; alimenta `ExtractedClaim`, `EvidenceBinding`,
  `GateEvaluation`, `FinalAnswerCandidate`, `PublishedOrHeldAnswer`.
- **Append-only / auditabile.** **Append-only e auditabile.** L'OrchestrationRun
  è la radice di audit dell'intera esecuzione multi-AI; le sue transizioni di
  stato e i suoi esiti devono entrare nell'audit chain hash-linked esistente.
  Un nuovo tentativo è un nuovo OrchestrationRun.
- **Configurazione mutabile.** Nessuna: la configurazione (prompt, agenti,
  budget) è fissata come snapshot all'avvio.

### 7.11 CrossAgentReview

- **Scopo.** Rappresentare un passo di confronto bounded fra gli output degli
  agenti: l'individuazione di contraddizioni, punti deboli, gap di copertura, e
  l'eventuale critic review da parte di un agente con ruolo reviewer.
- **Campi probabili.** Identificatore; riferimento all'`OrchestrationRun`; gli
  `AgentOutput` confrontati; eventuale `AgentRun` reviewer responsabile; esito
  strutturato del confronto (contraddizioni rilevate, gap, note); indice del
  passo (per il bounding); timestamp.
- **Relazioni.** Un CrossAgentReview appartiene a un `OrchestrationRun`; consuma
  più `AgentOutput`; alimenta la `CandidateSynthesis`.
- **Append-only / auditabile.** **Append-only e auditabile.** Il confronto è un
  fatto del run e non si riscrive. Il numero di CrossAgentReview per run deve
  essere finito e predeterminato dal bounding configurato — nessuna chat
  infinita.
- **Configurazione mutabile.** Nessuna; i parametri di bounding sono fissati a
  livello di `OrchestrationRun`.

### 7.12 CandidateSynthesis

- **Scopo.** Rappresentare la sintesi multi-AI originale prodotta da un run: un
  *candidate*, esplicitamente non ancora pubblicato e non ancora verificato. È
  l'input che la Claim Extraction consumerà.
- **Campi probabili.** Identificatore; riferimento all'`OrchestrationRun`; testo
  della sintesi; riferimenti agli `AgentOutput` e ai `CrossAgentReview` da cui
  deriva; eventuale agente synthesizer responsabile; flag che indicano se sono
  stati usati strumenti mock o reali; timestamp.
- **Relazioni.** Una CandidateSynthesis appartiene a un `OrchestrationRun`;
  deriva da `AgentOutput` e `CrossAgentReview`; viene scomposta in
  `ExtractedClaim`.
- **Append-only / auditabile.** **Append-only e auditabile.** È l'analogo
  multi-AI di un `draft_final_answers`: una candidate registrata è un fatto.
  Una nuova sintesi corrisponde a un nuovo `OrchestrationRun` (o a una nuova
  versione esplicitamente tracciata).
- **Configurazione mutabile.** Nessuna.

### 7.13 ExtractedClaim

- **Scopo.** Rappresentare un claim verificabile estratto dalla
  `CandidateSynthesis`. **Corrisponde concettualmente alle entità del Claim
  Ledger già esistenti** (`logical_claims`, `claim_ledger_entries`): per la
  linea multi-AI il lavoro è di *collegamento*, non di creazione di un nuovo
  modello.
- **Campi probabili (riuso del Claim Ledger).** Identificatore di logical
  claim; entry di ledger con stato; testo canonico del claim; support scope;
  riferimento alla `CandidateSynthesis` di origine.
- **Relazioni.** Un ExtractedClaim deriva da una `CandidateSynthesis`; è
  collegato a `EvidenceBinding`; è valutato da `GateEvaluation`.
- **Append-only / auditabile.** **Append-only**, ereditato integralmente
  dall'invariante del Claim Ledger: `claim_ledger_entries` è append-only, il
  supersede avviene solo via `claim_lineage`.
- **Configurazione mutabile.** Nessuna.

### 7.14 EvidenceBinding

- **Scopo.** Rappresentare il collegamento fra un `ExtractedClaim` e gli
  evidence span delle fonti disponibili. **Corrisponde concettualmente a
  `claim_evidence_links` già esistente.**
- **Campi probabili (riuso).** Identificatore del link; riferimento alla entry
  di claim; riferimento all'`evidence_span`; ruolo del link.
- **Relazioni.** Un EvidenceBinding collega un `ExtractedClaim` a un
  `evidence_span` derivato da una `SourceIngestion`.
- **Append-only / auditabile.** **Append-only**, ereditato dall'invariante di
  `claim_evidence_links` ed `evidence_spans`.
- **Configurazione mutabile.** Nessuna.
- **Nota semantica vincolante.** Un `EvidenceBinding` ben formato **non implica**
  che il claim sia supportato semanticamente né che sia vero: è un collegamento
  strutturale, non un giudizio.

### 7.15 GateEvaluation

- **Scopo.** Rappresentare la valutazione del Final Answer Gate sulla catena di
  claim derivata dalla `CandidateSynthesis`. **Corrisponde concettualmente a
  `final_gate_reports` + `coverage_gap_statements` già esistenti.**
- **Campi probabili (riuso).** Decisione (`approved`/`rejected`); reason code;
  payload del gate; coverage gap collegati con axis derivato; identità della
  policy versionata.
- **Relazioni.** Una GateEvaluation valuta gli `ExtractedClaim` e i loro
  `EvidenceBinding` di un `OrchestrationRun`; determina il
  `FinalAnswerCandidate` e quindi il `PublishedOrHeldAnswer`.
- **Append-only / auditabile.** **Append-only**, ereditato dall'invariante di
  `final_gate_reports`.
- **Configurazione mutabile.** Nessuna.
- **Nota semantica vincolante.** La GateEvaluation decide *publication allowed*
  / *publication held*. **Non** è una decisione di verità nel mondo.

### 7.16 FinalAnswerCandidate

- **Scopo.** Rappresentare la risposta finale candidata di un
  `OrchestrationRun`: la `CandidateSynthesis` dopo che ha attraversato Claim
  Extraction, Evidence Binding e GateEvaluation, ma prima dell'esito di
  pubblicazione. È il punto in cui si raccolgono, per la risposta, le evidenze
  tracciabili, i limiti dichiarati, le contraddizioni e i gap.
- **Campi probabili.** Identificatore; riferimento all'`OrchestrationRun` e alla
  `CandidateSynthesis`; riferimento alla `GateEvaluation`; testo della risposta
  candidata; aggregato di evidenze e limiti; aggregato di contraddizioni e gap;
  timestamp.
- **Relazioni.** Un FinalAnswerCandidate deriva da una `CandidateSynthesis` e da
  una `GateEvaluation`; determina un `PublishedOrHeldAnswer`.
- **Append-only / auditabile.** **Append-only e auditabile.** È l'analogo
  multi-AI di un `draft_final_answers` valutato dal gate.
- **Configurazione mutabile.** Nessuna.

### 7.17 PublishedOrHeldAnswer

- **Scopo.** Rappresentare l'esito finale del flusso: la risposta multi-AI nello
  stato di *publication allowed* (pubblicata) oppure *publication held*
  (trattenuta). **Corrisponde concettualmente a `published_answers` e allo stato
  derivato `publication_held` già esposto dall'Anti-Hallucination Report.**
- **Campi probabili (riuso ed estensione).** Identificatore; riferimento al
  `FinalAnswerCandidate` e all'`OrchestrationRun`; stato di pubblicazione
  (`published` / `publication_held`, più gli stati lifecycle `withdrawn` /
  `superseded` già esistenti); testo della risposta pubblicata; content hash;
  riferimento alla `GateEvaluation`; timestamp.
- **Relazioni.** Un PublishedOrHeldAnswer deriva da un `FinalAnswerCandidate`;
  è la sorgente da cui un report aggregato multi-AI (analogo dell'attuale
  Anti-Hallucination Report) verrà derivato.
- **Append-only / auditabile.** **Append-only**, ereditato dall'invariante di
  `published_answers`. Lo stato `publication_held` resta uno **stato derivato**,
  non un valore di stato grezzo, coerentemente con la scelta già fatta nel
  report 8.8B-REPORT.
- **Configurazione mutabile.** Nessuna.
- **Nota semantica vincolante.** *Publication held* significa che il supporto
  disponibile è risultato insufficiente per la pubblicazione. **Non** significa
  che la risposta sia falsa nel mondo.

### 7.18 Sintesi del modello concettuale

Il modello concettuale si divide nettamente in due metà, coerenti con il flusso
target della §4:

- **metà sinistra — da costruire.** `MasterPrompt`, `SourceInput`,
  `SourceIngestion`, `AgentConfig`, `AgentRolePrompt`, `AgentRun`,
  `AgentMessage`, `AgentOutput`, `TokenBudget`, `OrchestrationRun`,
  `CrossAgentReview`, `CandidateSynthesis`. Sono entità nuove. Le entità che
  registrano *configurazione* (`MasterPrompt`, `AgentConfig`, `AgentRolePrompt`,
  `TokenBudget` come limite) sono mutabili finché non consumate da un run, ma
  ogni run ne fissa uno snapshot immutabile. Le entità che registrano *fatti*
  (`SourceIngestion`, `AgentRun`, `AgentMessage`, `AgentOutput`,
  `OrchestrationRun`, `CrossAgentReview`, `CandidateSynthesis`) sono append-only
  e auditabili.

- **metà destra — da collegare.** `ExtractedClaim`, `EvidenceBinding`,
  `GateEvaluation`, `FinalAnswerCandidate`, `PublishedOrHeldAnswer`.
  Corrispondono in larga parte a entità già esistenti (Claim Ledger,
  `claim_evidence_links`, `final_gate_reports`, `draft_final_answers`,
  `published_answers`) e sono integralmente append-only. Il lavoro futuro su
  queste entità è prevalentemente di *innesto* della catena multi-AI nella
  pipeline evidence-gated esistente.

Il punto di giunzione critico fra le due metà è la trasformazione di una
`CandidateSynthesis` nell'input che la Claim Extraction consuma: è lì che la
linea multi-AI incontra il nucleo evidence-gated, ed è una delle decisioni
architetturali principali che le fasi `ORCH-SCHEMA-PRE` e `ORCH-GATE-A` della
roadmap (§19) dovranno affrontare.

---

## 8. UI target

Questa sezione descrive la **futura home** del prodotto: la superficie che
rifletterà il modello mentale dell'utente (Prompt Master → fonti → agenti →
esecuzione → risultato evidence-gated) anziché il ciclo amministrativo progetto
→ documenti → task. È una descrizione di *target di prodotto*, non una specifica
di implementazione: nessuna pagina o componente viene creato in questa fase.

### 8.1 I sei pannelli della home target

La home target è organizzata in sei pannelli, ordinati secondo il flusso
naturale dell'utente.

**Prompt Master panel.** Il pannello centrale e prominente. Contiene una
textarea grande in cui l'utente scrive la domanda, il problema o l'obiettivo. È
l'input primario del prodotto e corrisponde all'entità `MasterPrompt` (§7.1). La
copy intorno resta sobria: inquadra l'input come "richiesta" o "obiettivo", mai
come "claim da verificare come vero", e non promette certezza fattuale.

**Sources panel.** Il pannello in cui l'utente allega le fonti su cui il sistema
deve lavorare. Oggi e nel breve termine: upload di testo e Markdown, e selezione
di fonti già presenti. In futuro: PDF, immagini, fonti web o interne. Ogni fonte
mostrata corrisponde a un `SourceInput` (§7.2). Il pannello deve dichiarare
onestamente quali tipi di fonte sono disponibili oggi e quali sono pianificati.

**AI Agents panel.** Il pannello in cui l'utente configura uno o più agenti. Per
ogni agente l'utente compila la configurazione descritta in §9 (nome, provider,
modello, ruolo, prompt, source access, token budget, output contract,
temperature/config, retry policy, flag reviewer, flag synthesizer). Ogni agente
corrisponde a un `AgentConfig` (§7.4) più il suo `AgentRolePrompt` (§7.5).

**Execution settings panel.** Il pannello in cui l'utente sceglie *come* gli
agenti devono lavorare: modalità di orchestrazione (independent / coordinated),
parametri di bounding (numero massimo di passi di confronto), budget globale del
run, e quali pass abilitare (reviewer, critic, synthesis, eventuale second
check). Questi parametri configurano l'`OrchestrationRun` (§7.10) al momento
dell'avvio e vengono fissati come snapshot immutabile da quel run.

**Run progress panel.** Il pannello che mostra l'avanzamento di un
`OrchestrationRun` in corso: stato del run, stato di ciascun `AgentRun`, eventi
di orchestrazione, consumo di token rispetto al budget. È una vista derivata
read-only di fatti già registrati nel backend; non ricalcola nulla. Sostituisce
il modello reload-based attuale con una superficie pensata per seguire un
processo in più passi (vedi §17 e §18 per gli eventi e la fase UI dedicata).

**Results / Evidence Report panel.** Il pannello che mostra il risultato finale:
la risposta originale multi-AI con evidenze tracciabili, limiti dichiarati,
contraddizioni o gap, e lo stato esplicito *publication allowed* / *publication
held*, più l'accesso al report tecnico per audit. Riusa concettualmente le
superfici già esistenti — `TaskSummaryView`, l'Anti-Hallucination Report viewer
— estese al contesto multi-AI. È una vista derivata read-only: non prende nuove
decisioni e non ricalcola il gate.

### 8.2 Rapporto con `/requests/new`

L'attuale flusso `/requests/new` (UI-CREATE-FLOW-A) **può restare come base
tecnica o come dev flow**. È utile come riferimento implementativo: ha già un
client API tipizzato, un layer di proxy same-origin, un modello di errore
coerente, e un pattern di idempotency-key consolidato. La linea di lavoro
multi-AI potrà riusarne i pattern.

Ma `/requests/new` **non è ancora la futura esperienza principale**: resta un
flusso orientato a progetto/documenti/task closed-corpus. La home target
descritta sopra è una superficie nuova, costruita attorno al Prompt Master, che
le fasi UI della §18 introdurranno gradualmente. Finché quella superficie non
esiste, `/requests/new` resta il punto d'ingresso pratico, ma va inteso come
*base tecnica e dev flow*, non come la home di prodotto definitiva.

---

## 9. Agent configuration

Questa sezione descrive, campo per campo, la **configurazione di un singolo
agente AI**. Corrisponde alle entità `AgentConfig` (§7.4) e `AgentRolePrompt`
(§7.5). È una descrizione concettuale: nessuno schema, nessuna tabella, nessun
codice.

Per ogni agente la configurazione comprende:

- **name** — nome leggibile dell'agente, scelto dall'utente. Serve a
  identificare l'agente nei pannelli UI, nei log e nel report. Configurazione
  mutabile finché non consumata da un run.

- **provider** — il provider a cui l'agente parla, espresso attraverso
  l'astrazione di provider della §10 (per esempio *mock*, *openai*,
  *anthropic*, *gemini*, *local*). In MVP-0 l'unico valore operativo è *mock*.

- **model** — l'identificativo del modello presso quel provider. Resta un campo
  di configurazione opaco a livello concettuale; la sua validità rispetto al
  provider è responsabilità dell'astrazione di provider.

- **role** — il ruolo funzionale dell'agente nell'orchestrazione (per esempio
  *researcher*, *critic*, *synthesizer*). Determina come l'agente partecipa ai
  pass della §12.

- **system prompt** — il prompt di sistema che inquadra il comportamento
  generale dell'agente. Fa parte dell'`AgentRolePrompt` ed è versionabile.

- **task prompt** — il prompt specifico che descrive il compito dell'agente
  rispetto al Prompt Master e alle fonti. Anch'esso parte dell'`AgentRolePrompt`.

- **source access** — quali fonti l'agente può vedere: tutte le fonti del
  MasterPrompt, un sottoinsieme, o nessuna. Permette, per esempio, di dare a un
  reviewer accesso solo agli output degli altri agenti e non al corpus.

- **token budget** — il budget di token (e per estensione di costo) atteso per
  l'agente. È il limite configurato di un `TokenBudget` per-agent (§7.9 e §11).

- **output contract** — la forma attesa dell'output dell'agente: testo libero,
  lista di affermazioni, formato strutturato. Definisce cosa le fasi successive
  (Cross Review, Candidate Synthesis) possono assumere.

- **temperature / config** — i parametri di campionamento e gli altri parametri
  di chiamata del modello. Restano configurazione opaca a livello concettuale.

- **retry policy** — la politica di retry in caso di fallimento della chiamata
  al provider: numero massimo di tentativi, eventuale backoff. I retry devono
  restare bounded (vedi §11 e §16) e ogni tentativo deve essere auditabile.

- **reviewer flag** — indica se l'agente svolge un ruolo di reviewer, cioè se
  partecipa al reviewer pass / critic pass della §12 esaminando gli output degli
  altri agenti.

- **synthesizer flag** — indica se l'agente svolge un ruolo di synthesizer, cioè
  se è responsabile del synthesis pass che produce la `CandidateSynthesis`.

**Mutabilità.** Tutti i campi di configurazione sono mutabili finché un
`OrchestrationRun` non ha consumato l'agente. Al momento dell'avvio del run, la
configurazione di ogni agente viene **fissata come snapshot immutabile**: il run
deve poter essere auditato rispetto alla configurazione esatta che ha usato, e
una modifica successiva alla configurazione non deve mai alterare
retroattivamente ciò che il run ha effettivamente eseguito.

---

## 10. Provider abstraction

Questa sezione progetta, **a livello concettuale**, l'interfaccia di provider
che le fasi future implementeranno. Non implementa alcun provider reale, non
introduce credenziali, secret, chiamate HTTP reali o SDK di provider.

### 10.1 Scopo dell'astrazione

L'astrazione di provider è il punto in cui il sistema parla in modo **uniforme**
a sorgenti AI eterogenee. Senza di essa, ogni componente che invoca un modello
dovrebbe conoscere i dettagli di ogni provider. Con essa, l'orchestratore
conosce una sola interfaccia, e i provider concreti la implementano.

I provider che l'astrazione deve poter coprire in futuro sono:

- **mock provider** — un provider deterministico, senza rete, che produce output
  riproducibili. È l'unico provider operativo in MVP-0 ed è il provider su cui
  l'intera orchestrazione va sviluppata e testata per primo.
- **OpenAI** — provider esterno, futuro.
- **Anthropic** — provider esterno, futuro.
- **Gemini** — provider esterno, futuro.
- **local model** — un modello eseguito localmente, futuro.

### 10.2 Forma concettuale dell'interfaccia

A livello concettuale, l'interfaccia di provider espone una capacità centrale:
*data una richiesta strutturata — system prompt, task prompt, eventuale
contesto di fonti, parametri di campionamento, limite di token — produrre una
risposta strutturata — testo dell'output, token consumati in input e output,
metadati di esito*. Intorno a questa capacità l'interfaccia deve esprimere, in
modo uniforme e indipendente dal provider concreto:

- l'identità del provider e del modello;
- il modo in cui un fallimento viene segnalato (errore tipizzato, distinto da
  una risposta valida);
- il modo in cui il consumo di token viene riportato, così che il token
  accounting (§11) sia alimentabile in modo omogeneo;
- il modo in cui un limite di token viene comunicato alla chiamata.

L'interfaccia **non** deve esporre dettagli di trasporto (HTTP, SDK), di
autenticazione (chiavi, secret) o di formato specifico di un provider: quei
dettagli vivono dentro le implementazioni concrete, non nell'astrazione.

### 10.3 Approccio mock-first / provider-interface-first

L'approccio raccomandato è **mock-first e provider-interface-first**:

1. prima si definisce l'**interfaccia** di provider;
2. poi si implementa il **mock provider** che la rispetta;
3. l'intera orchestrazione (runner, multi-agent, review, synthesis, gate
   integration) viene sviluppata e testata **contro il mock provider**, in modo
   deterministico e senza rete;
4. solo in una fase dedicata e successiva, quando l'orchestrazione è stabile, si
   implementano i provider reali dietro la stessa interfaccia.

Questo approccio mantiene la coerenza con il vincolo MVP-0
`PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0`, e garantisce che l'introduzione
di provider reali sia un cambiamento isolato dietro un'interfaccia già
collaudata.

### 10.4 Cosa questa fase non introduce

Questa fase di design **non** introduce: credenziali o secret di provider;
chiamate HTTP reali; SDK di provider; configurazione di rete verso terze parti.
L'astrazione qui descritta è concettuale. La sua implementazione, e ancor più
l'implementazione dei provider reali, sono materia delle fasi `ORCH-PROVIDER-PRE`
e `ORCH-PROVIDER-A` della roadmap (§19).

---

## 11. Token budget strategy

Questa sezione descrive la strategia concettuale di gestione del budget di
token. È una strategia, non un'implementazione.

### 11.1 Budget globale del run

Ogni `OrchestrationRun` ha un **budget globale**: un limite complessivo di
token (e per estensione di costo) che l'intera esecuzione non deve superare. È
il tetto entro cui tutti gli agenti, tutti i pass e tutti i retry devono stare.
Quando il consumo aggregato si avvicina al tetto, l'orchestrazione deve
degradare in modo controllato (per esempio interrompendo i pass opzionali)
piuttosto che superarlo silenziosamente.

### 11.2 Budget per agente

Ogni agente ha un **budget per-agente**, sottoinsieme del budget globale. È il
limite configurato nel `TokenBudget` per-agent (§7.9) e nel campo *token budget*
della configurazione agente (§9). Permette di evitare che un singolo agente
consumi una quota sproporzionata del budget del run.

### 11.3 Prompt packing

Il **prompt packing** è la strategia di composizione del prompt inviato a un
agente: come si combinano system prompt, task prompt, Prompt Master e contesto
di fonti in un payload che rispetti il limite di token. Il packing deve essere
deterministico e auditabile: dato lo stesso input deve produrre lo stesso
payload.

### 11.4 Evidence compression

La **evidence compression** è la strategia di riduzione del contesto di fonti
quando il corpus è troppo grande per stare nel budget: selezione dei chunk più
rilevanti, riassunto dei chunk, o entrambi. Ogni compressione è una
trasformazione che riduce informazione, quindi deve essere **registrata come
fatto auditabile** (vedi §11.7).

### 11.5 Summarization checkpoints

I **summarization checkpoints** sono punti dell'orchestrazione in cui il
materiale accumulato (output degli agenti, esiti di review) viene riassunto per
restare entro budget nei pass successivi. Anche un riassunto è una
trasformazione che riduce informazione e va auditato.

### 11.6 Truncation policy

La **truncation policy** definisce cosa accade quando un payload o un output
eccede comunque il limite: quali parti vengono tagliate, in quale ordine di
priorità, e con quale segnalazione. Il troncamento non deve mai essere
silenzioso.

### 11.7 Audit dei tagli

Ogni operazione che riduce informazione — compressione di evidenze, riassunto,
troncamento — deve lasciare una traccia auditabile: *che cosa è stato tagliato,
quando, perché, con quale strategia*. Coerentemente con la nota di coerenza
architetturale, questi tagli sono **fatti append-only**: vengono registrati come
eventi o record, non applicati riscrivendo silenziosamente il materiale
originale. Un revisore deve poter ricostruire che un output è stato prodotto a
partire da un contesto compresso o troncato.

### 11.8 Prevenzione del context explosion

La strategia complessiva deve prevenire il **context explosion**: la crescita
incontrollata del contesto man mano che gli output degli agenti si accumulano
attraverso i pass. I budget globale e per-agente, i checkpoint di
summarization, e la natura bounded dell'orchestrazione (§12) sono i meccanismi
che, insieme, mantengono il contesto limitato.

### 11.9 Gestione dei retry

I retry (configurati dalla *retry policy* dell'agente, §9) consumano budget.
La strategia deve: contare i token dei tentativi falliti contro il budget;
limitare il numero di retry; e impedire che i retry, da soli, esauriscano il
budget del run. Ogni tentativo, riuscito o fallito, è un fatto auditabile.

### 11.10 Gestione degli output troppo lunghi

Quando l'output di un agente eccede ciò che i pass successivi possono
consumare, la strategia deve gestirlo esplicitamente: riassunto dell'output,
troncamento secondo la truncation policy, o segnalazione come esito parziale.
Anche qui la riduzione è un fatto auditabile e l'output originale completo resta
registrato come `AgentOutput` append-only (§7.8): la versione ridotta è una
trasformazione tracciata, non una sostituzione dell'originale.

---

## 12. Multi-agent orchestration strategy

Questa sezione descrive la strategia di orchestrazione multi-agente. Tutte le
modalità descritte sono **bounded**: hanno un numero finito e predeterminato di
passi.

### 12.1 Le modalità bounded

**Independent parallel answers.** Ogni agente lavora in modo indipendente sul
Prompt Master e sulle fonti, e produce il proprio output. Non c'è confronto
diretto fra agenti in questa modalità: è il pass di base, e da solo produce un
insieme di `AgentOutput` paralleli.

**Reviewer pass.** Un pass in cui uno o più agenti con *reviewer flag* (§9)
esaminano gli output prodotti dagli altri agenti e producono osservazioni
strutturate. È un passo singolo e bounded, non un dialogo aperto.

**Critic pass.** Un pass in cui un agente (o un passo dedicato) cerca
attivamente punti deboli, contraddizioni e gap di copertura negli output. Si
distingue dal reviewer pass per l'intento adversariale. Anch'esso è un passo
singolo e bounded. Reviewer pass e critic pass alimentano i `CrossAgentReview`
(§7.11).

**Synthesis pass.** Un pass in cui un agente con *synthesizer flag* (§9), o un
passo dedicato, combina gli output e gli esiti di review in un'unica
`CandidateSynthesis` (§7.12) originale.

**Optional second check pass.** Un pass facoltativo, eseguito solo se abilitato
nelle execution settings (§8.1), che ricontrolla la `CandidateSynthesis` prima
che entri nell'evidence gate. È opzionale e, se eseguito, è anch'esso un singolo
passo bounded.

### 12.2 Cosa evitare esplicitamente

La strategia di orchestrazione **deve esplicitamente evitare**:

- **chat infinita fra agenti** — nessuno scambio aperto e indefinito di
  messaggi fra agenti; ogni interazione è un pass con un numero di passi fissato
  in anticipo;
- **loop non bounded** — nessun ciclo di review/synthesis che possa ripetersi un
  numero indeterminato di volte; il numero di pass è parte delle execution
  settings fissate come snapshot dal run;
- **auto-recursion senza budget** — nessun agente può invocare se stesso o
  innescare nuovi pass al di fuori del budget di token (§11) e del bounding
  configurato;
- **output non auditabili** — ogni messaggio (`AgentMessage`, §7.7), ogni output
  (`AgentOutput`, §7.8) e ogni esito di review (`CrossAgentReview`, §7.11) deve
  essere registrato come fatto append-only; nessuna interazione fra agenti può
  restare fuori dall'audit.

### 12.3 Bounding come invariante

Il bounding non è un'ottimizzazione: è un **invariante di sicurezza**. Garantisce
che un `OrchestrationRun` termini in un numero finito di passi, con un consumo di
token limitato, e con una traccia di audit completa. La natura bounded
dell'orchestrazione è anche uno dei meccanismi che prevengono il context
explosion (§11.8).

---

## 13. Integrazione con Evidence Gate

Questa sezione descrive **come gli output AI entrano nella pipeline
evidence-gated esistente**. È il punto di giunzione fra la linea multi-AI nuova e
il nucleo evidence-gated già funzionante (§5).

### 13.1 La catena di integrazione

```
AgentOutput
   │
   ▼
CandidateSynthesis
   │
   ▼
ExtractedClaims        (Claim Extraction)
   │
   ▼
Claim Ledger           (logical_claims, claim_ledger_entries — append-only)
   │
   ▼
Evidence Links         (claim_evidence_links — append-only)
   │
   ▼
CVE-lite               (verification_records — controllo quote/hash)
   │
   ▼
Source Quality         (source_quality_assessments — qualità della fonte)
   │
   ▼
Claim Entailment       (claim_entailment_checks — relazione claim↔evidence)
   │
   ▼
Final Answer Gate      (final_gate_reports — decisione di pubblicabilità)
   │
   ▼
Published / Held Answer
```

Gli `AgentOutput` prodotti dall'orchestrazione confluiscono in una
`CandidateSynthesis`. La `CandidateSynthesis` viene scomposta in claim
verificabili (`ExtractedClaims`), che entrano nel Claim Ledger esistente. Da
quel punto in poi la catena coincide integralmente con la pipeline attuale: i
claim vengono collegati a evidence span, sottoposti a CVE-lite, valutati su
Source Quality e Claim Entailment, e infine sottoposti al Final Answer Gate, che
decide se la risposta multi-AI è pubblicabile o va trattenuta.

### 13.2 Il gate non va bypassato

**Il Final Answer Gate non deve essere bypassato in nessun caso.** Nessun output
AI, per quanto "rivisto" da un reviewer pass o "sintetizzato" da un synthesizer,
può essere pubblicato senza essere passato attraverso Claim Extraction, Evidence
Binding e GateEvaluation. Il fatto che una risposta provenga da più AI coordinate
non la rende più affidabile agli occhi del sistema: resta una *candidate* finché
il gate non decide. Una scorciatoia che pubblicasse una `CandidateSynthesis`
saltando il gate violerebbe l'invariante centrale del prodotto evidence-gated.

In particolare: il *reviewer pass*, il *critic pass* e l'*optional second check
pass* della §12 **non sono sostituti del gate**. Sono passi di orchestrazione
che migliorano la candidate; il gate resta l'unica autorità di pubblicazione.

### 13.3 UI e report non ricalcolano decisioni backend

Coerentemente con i guardrail già stabiliti per la linea UI (`PHASE_UI_PRE.md`
§3, `PHASE_UI_CREATE_FLOW_PRE.md` §12), **la UI e i report non devono
ricalcolare alcuna decisione del backend**. Il Run progress panel e il Results /
Evidence Report panel (§8.1) sono viste derivate read-only: leggono lo stato del
run, gli output, gli esiti del gate, e li presentano. Non ricompongono un
punteggio, non rivalutano claim o fonti, non riemettono una decisione di
pubblicazione. Lo stato *publication held* resta uno stato derivato, non un
verdetto della UI.

### 13.4 Separazione degli assi

L'integrazione preserva la separazione dei sei assi già stabilita dal progetto:
claim correctness, evidence support, CVE-lite verification, source quality,
claim entailment, final gate truth restano distinti. Il fatto che una risposta
sia stata prodotta da più AI non aggiunge un settimo asse di "verità multi-AI" e
non collassa gli altri in un punteggio unico. La sintesi multi-AI è *controllata
rispetto alle fonti disponibili*; non è dichiarata "vera".

---

## 14. Document ingestion roadmap

Questa sezione descrive lo stato attuale e la roadmap futura dell'ingestione di
documenti.

### 14.1 Stato attuale

Oggi il sistema ingerisce, come fonti native:

- **`.txt`** — file di testo semplice;
- **`.md`** — file Markdown.

L'upload produce, in modo deterministico, una versione di documento `parsed`,
chunk deterministici e uno `evidence_span` per chunk. È il corpus su cui CVE-lite
e gli altri assi lavorano.

### 14.2 Roadmap futura

Le capacità di ingestione future, ciascuna da affrontare in una fase dedicata:

- **PDF** — ingestione di PDF come fonte nativa, con estrazione del testo.
- **immagini** — ingestione di immagini come fonte nativa.
- **OCR** — riconoscimento di testo da documenti scansionati o da immagini.
- **vision** — pipeline di analisi visiva del contenuto di immagini.
- **web sources** — ingestione di fonti web in modalità controllata (Verified
  Web Mode / Web-RAG).
- **source provenance** — tracciamento dell'origine di ogni fonte: da dove
  proviene, quando è stata acquisita, attraverso quale percorso.
- **chunking specializzato** — strategie di chunking adattate al tipo di
  documento (un PDF strutturato non si chunka come un `.txt` piatto).
- **hash / tracciabilità** — hashing del contenuto e dei chunk per garantire che
  ogni evidence span sia riconducibile in modo verificabile alla fonte originale.

Ciascuna di queste capacità corrisponde a `SourceInput` (§7.2) e
`SourceIngestion` (§7.3) del modello concettuale.

### 14.3 PDF e immagini non vanno improvvisati nella UI

**PDF e immagini non vanno improvvisati nella UI senza un backend di ingestione
dedicato.** Aggiungere alla UI un campo di upload PDF o immagine prima che esista
una pipeline di ingestione capace di parsare, chunkare, hashare e produrre
evidence span da quei formati creerebbe un'esperienza disonesta: l'utente
caricherebbe una fonte che il sistema non sa realmente usare. L'ordine corretto
è: prima il backend di ingestione per un formato, poi l'esposizione di quel
formato nella UI. La UI delle fonti deve dichiarare chiaramente quali formati
sono realmente supportati in ogni momento.

---

## 15. Backend gaps

Questa sezione elenca i possibili **futuri moduli, schema e API di backend**
necessari alla linea multi-AI. È un elenco di gap, **non un'implementazione**:
nessuna tabella, nessuna migration, nessun modulo viene creato qui.

Possibili futuri elementi di backend:

- **orchestration_runs** — persistenza degli `OrchestrationRun` (§7.10): la
  radice di ogni esecuzione multi-AI.
- **agent_configs** — persistenza degli `AgentConfig` (§7.4) e della loro
  associazione a un MasterPrompt.
- **agent_runs** — persistenza degli `AgentRun` (§7.6): le istanze di esecuzione
  di un singolo agente dentro un run.
- **agent_messages** — persistenza degli `AgentMessage` (§7.7): il livello più
  fine di audit di un run.
- **agent_outputs** — persistenza degli `AgentOutput` (§7.8): gli output reali
  degli agenti.
- **token_usage_records** — persistenza del consumo di token e costo per
  agente, per run e per pass, contro i `TokenBudget` configurati.
- **synthesis_candidates** — persistenza delle `CandidateSynthesis` (§7.12).
- **provider_invocations** — persistenza delle invocazioni di provider: ogni
  chiamata all'astrazione di provider come fatto auditabile.
- **orchestration_events** — un log append-only di eventi di orchestrazione, che
  alimenta il Run progress panel (§8.1) e l'endpoint eventi (§17).

**Nota sulle tabelle placeholder.** La migration `0005` ha già introdotto
tabelle placeholder `agent_runs`, `agent_outputs`, `truncation_events`,
`continuation_attempts`, oggi **vuote e prive di semantica operativa**. La loro
sola esistenza **non va considerata un'implementazione**. Una tabella diventa un
componente reale solo quando ha, tutti insieme: una semantica definita, servizi
che la popolano e la leggono, API che la espongono, e test che la coprono.
Finché questi quattro elementi non esistono, le tabelle placeholder restano
segnaposto, e le fasi `ORCH-SCHEMA-*` della roadmap (§19) dovranno decidere se
adottarle, ridefinirle o sostituirle.

---

## 16. Worker gaps

Questa sezione elenca i possibili **futuri elementi del worker** necessari
all'orchestrazione multi-AI. È un elenco di gap, non un'implementazione.

- **orchestration consumer** — un consumer, analogo all'attuale consumer
  `task.created`, che riceva l'evento di avvio di un `OrchestrationRun` e ne
  guidi l'esecuzione.
- **provider caller** — il componente che invoca l'astrazione di provider (§10)
  per conto di un `AgentRun`, traducendo una richiesta di agente in
  un'invocazione e raccogliendone l'esito.
- **timeout** — gestione dei timeout sulle invocazioni di provider: una chiamata
  che non risponde entro un limite va trattata come fallimento controllato.
- **retries** — esecuzione della retry policy degli agenti (§9), con retry
  bounded e contati contro il budget (§11.9).
- **idempotency** — garanzia che un doppio delivery dell'evento di avvio di un
  run, o di un passo, non duplichi `AgentRun`, `AgentOutput` o invocazioni di
  provider. È l'estensione alla linea multi-AI dell'invariante di idempotenza
  già rispettato dal consumer `task.created`.
- **partial failure** — gestione del fallimento di un sottoinsieme di agenti: un
  run in cui alcuni agenti falliscono deve poter procedere in modo controllato
  (per esempio sintetizzando solo dagli output riusciti) o terminare con un
  esito parziale esplicito, mai con un esito ambiguo.
- **resumability** — capacità di riprendere un run interrotto senza ripetere il
  lavoro già completato, coerente con la natura resume-safe dell'attuale worker.
- **rate limit handling** — gestione dei limiti di frequenza imposti dai
  provider reali (rilevante solo dalla fase provider reali in poi), con backoff
  controllato.
- **token accounting** — registrazione del consumo di token e costo per ogni
  `AgentRun` e ogni invocazione, alimentando i `token_usage_records` (§15).
- **audit events** — emissione di eventi di audit per ogni transizione
  significativa del run, coerente con l'audit chain hash-linked esistente. Le
  transizioni di stato di un `OrchestrationRun` o di un `AgentRun` sono fatti
  append-only: vanno registrate come eventi, non come riscrittura di stato.

---

## 17. API roadmap

Questa sezione propone endpoint futuri, **solo a livello concettuale**. Nessun
endpoint viene implementato; le firme sono indicative e soggette a revisione
nelle fasi di design di dettaglio.

- **`POST /api/v1/orchestration-runs`** — crea un `OrchestrationRun` a partire da
  un MasterPrompt, dalle sue fonti e dalla configurazione di esecuzione.
  Concettualmente l'analogo multi-AI di `POST /api/v1/tasks`; dovrebbe supportare
  un `Idempotency-Key` come la creazione task attuale.
- **`GET /api/v1/orchestration-runs/{id}`** — legge lo stato aggregato di un run:
  stato complessivo, agenti, esito. Vista derivata read-only.
- **`GET /api/v1/orchestration-runs/{id}/events`** — legge il log di
  `orchestration_events` (§15) di un run, alimentando il Run progress panel.
- **`POST /api/v1/orchestration-runs/{id}/agents`** — aggiunge o configura un
  agente per un run, prima dell'avvio (quando la configurazione è ancora
  mutabile, §9).
- **`GET /api/v1/orchestration-runs/{id}/outputs`** — legge gli `AgentOutput` di
  un run. Vista derivata read-only.
- **`POST /api/v1/orchestration-runs/{id}/synthesize`** — avvia il synthesis pass
  che produce la `CandidateSynthesis`.
- **`POST /api/v1/orchestration-runs/{id}/submit-to-gate`** — sottopone la
  `CandidateSynthesis` alla catena di integrazione (§13): Claim Extraction,
  Evidence Binding, Final Answer Gate. Questo endpoint **non bypassa il gate**:
  lo invoca. Non esiste, e non deve esistere, un endpoint che pubblichi una
  risposta saltando il gate.

Tutti gli endpoint di lettura sono read-only e non ricalcolano decisioni
backend. La convenzione di errore dovrebbe riusare l'envelope normalizzato già
in uso (`{"error": {"code", "message", "details"}}`).

---

## 18. UI roadmap

Questa sezione propone le fasi UI future, **solo come elenco di fasi**. Nessuna
pagina o componente viene creato qui.

- **UI-PROMPT-PRE** — fase di design del pannello Prompt Master e della home
  target. Documento di design, nessun codice.
- **UI-PROMPT-A** — implementazione del Prompt Master panel: la superficie di
  input primario del prodotto.
- **UI-AGENTS-A** — implementazione dell'AI Agents panel: configurazione di uno
  o più agenti secondo §9.
- **UI-SOURCES-B** — estensione del Sources panel: gestione delle fonti nel
  contesto del MasterPrompt (il suffisso `-B` segnala che è un'evoluzione della
  gestione fonti già introdotta da UI-CREATE-FLOW-A).
- **UI-RUN-A** — implementazione dell'Execution settings panel e del Run
  progress panel: avvio di un run e osservazione del suo avanzamento.
- **UI-RESULTS-A** — implementazione del Results / Evidence Report panel: la
  vista del risultato finale evidence-gated multi-AI.

Ogni fase UI è una superficie additiva, costruita con i vincoli già in vigore
per la linea UI (inline styles, niente nuove dipendenze, server component dove
possibile, copy sobria verificata da test di wording vietato) e ogni fase di
implementazione va preceduta dalla sua fase `-PRE` di design quando il salto è
significativo.

---

## 19. Roadmap incrementale consigliata

Questa sezione propone una sequenza di **fasi piccole, indipendenti e
testabili**. Per ciascuna fase si indicano scopo, file probabili, cosa la fase
non deve fare, e criteri di accettazione. Le firme di file sono indicative.

### 19.1 PRODUCT-ORCHESTRATION-PRE

- **Scopo.** Questa fase: riallineare prodotto e architettura, definire il
  modello concettuale target e la roadmap. È il presente documento.
- **File probabili.** `PHASE_PRODUCT_ORCHESTRATION_PRE.md`.
- **Cosa non deve fare.** Nessun codice, nessuna migration, nessuna dipendenza,
  nessuna modifica a backend/worker/UI/test.
- **Criteri di accettazione.** Vedi §21.

### 19.2 ORCH-SCHEMA-PRE

- **Scopo.** Progettare lo schema delle entità multi-AI (§7, §15): decidere
  tabelle, vincoli, invarianti append-only, rapporto con le tabelle placeholder
  `0005`. Solo design.
- **File probabili.** `PHASE_ORCH_SCHEMA_PRE.md`.
- **Cosa non deve fare.** Non scrivere migration, non scrivere codice.
- **Criteri di accettazione.** Schema descritto in modo completo; invarianti
  append-only esplicitati; decisione documentata sulle tabelle placeholder;
  nessun file di codice o migration toccato.

### 19.3 ORCH-SCHEMA-A

- **Scopo.** Implementare lo schema: le migration per `orchestration_runs`,
  `agent_configs`, `agent_runs`, `agent_messages`, `agent_outputs`,
  `token_usage_records`, `synthesis_candidates`, `provider_invocations`,
  `orchestration_events`.
- **File probabili.** Nuove migration `migrations/00NN_*.sql`; eventuali test di
  migration.
- **Cosa non deve fare.** Non implementare servizi, worker, API o UI; non
  introdurre provider reali.
- **Criteri di accettazione.** Migration applicabili e idempotenti; trigger
  append-only sulle tabelle di fatti; test di migration verdi; nessuna modifica a
  worker/API/UI.

### 19.4 ORCH-PROVIDER-PRE

- **Scopo.** Progettare l'interfaccia di provider (§10): forma concettuale,
  contratto, modello di errore, riporto del consumo di token.
- **File probabili.** `PHASE_ORCH_PROVIDER_PRE.md`.
- **Cosa non deve fare.** Non implementare provider, non introdurre credenziali,
  secret, SDK o chiamate di rete.
- **Criteri di accettazione.** Interfaccia descritta; approccio mock-first
  confermato; nessun codice toccato.

### 19.5 ORCH-PROVIDER-A

- **Scopo.** Implementare l'interfaccia di provider e il **mock provider**
  deterministico dietro di essa.
- **File probabili.** Modulo provider in `apps/worker/*` o `packages/shared/*`;
  test del mock provider.
- **Cosa non deve fare.** Non implementare provider reali (OpenAI, Anthropic,
  Gemini, local); non introdurre credenziali, secret, SDK o chiamate HTTP reali.
- **Criteri di accettazione.** Interfaccia implementata; mock provider
  deterministico e testato; nessun provider reale; nessuna dipendenza di rete.

### 19.6 ORCH-RUNNER-A

- **Scopo.** Implementare l'orchestration consumer e il provider caller per il
  caso di un **singolo agente** (independent answer): avvio di un
  `OrchestrationRun`, esecuzione di un `AgentRun`, produzione di un
  `AgentOutput`, con token accounting e audit events.
- **File probabili.** Consumer e servizi in `apps/worker/*`; test.
- **Cosa non deve fare.** Non implementare multi-agente, review, synthesis;
  non usare provider reali; non bypassare nulla del modello append-only.
- **Criteri di accettazione.** Un run a singolo agente gira end-to-end sul mock
  provider; output e transizioni append-only e auditabili; idempotente e
  resume-safe; test verdi.

### 19.7 ORCH-MULTI-A

- **Scopo.** Estendere il runner al caso **multi-agente** in modalità
  *independent parallel answers* (§12).
- **File probabili.** Estensione dei servizi worker; test.
- **Cosa non deve fare.** Non implementare review o synthesis; non introdurre
  loop non bounded; non usare provider reali.
- **Criteri di accettazione.** Più agenti producono output paralleli in un run
  bounded; budget per-agente e globale rispettati; audit completo; test verdi.

### 19.8 ORCH-REVIEW-A

- **Scopo.** Implementare reviewer pass e critic pass (§12), che producono
  `CrossAgentReview`.
- **File probabili.** Estensione dei servizi worker; test.
- **Cosa non deve fare.** Non implementare la synthesis; non introdurre chat
  infinita o loop non bounded; non usare provider reali.
- **Criteri di accettazione.** Reviewer e critic pass bounded e a passo singolo;
  `CrossAgentReview` append-only e auditabili; test verdi.

### 19.9 ORCH-SYNTHESIS-A

- **Scopo.** Implementare il synthesis pass (§12) che produce la
  `CandidateSynthesis`, e l'eventuale optional second check pass.
- **File probabili.** Estensione dei servizi worker; test.
- **Cosa non deve fare.** Non collegare ancora la synthesis al gate; non
  bypassare il gate; non usare provider reali.
- **Criteri di accettazione.** Una `CandidateSynthesis` viene prodotta in modo
  bounded e auditabile; nessuna pubblicazione avviene in questa fase; test verdi.

### 19.10 ORCH-GATE-A

- **Scopo.** Implementare il punto di giunzione (§13): trasformare una
  `CandidateSynthesis` in `ExtractedClaims`, collegarli a evidence, e
  sottoporli al Final Answer Gate esistente.
- **File probabili.** Servizio di adattamento synthesis→claim in
  `apps/worker/*`; test end-to-end.
- **Cosa non deve fare.** **Non bypassare il Final Answer Gate**; non modificare
  la semantica del gate; non promettere verità; non usare provider reali.
- **Criteri di accettazione.** Una `CandidateSynthesis` attraversa Claim
  Extraction → Evidence Binding → CVE-lite → Source Quality → Claim Entailment →
  Final Answer Gate ed esita in *published* o *publication held*; il gate non è
  bypassato; test end-to-end verdi.

### 19.11 UI-PROMPT-A

- **Scopo.** Implementare il Prompt Master panel della home target (§8.1).
- **File probabili.** Route e componenti sotto `apps/web/*`; test.
- **Cosa non deve fare.** Non introdurre nuove dipendenze; non promettere
  verità; non ricalcolare decisioni backend.
- **Criteri di accettazione.** Il pannello consente di scrivere e salvare un
  MasterPrompt; copy sobria verificata da test di wording vietato; nessuna nuova
  dipendenza.

### 19.12 UI-AGENTS-A

- **Scopo.** Implementare l'AI Agents panel (§8.1, §9).
- **File probabili.** Componenti sotto `apps/web/*`; test.
- **Cosa non deve fare.** Non introdurre provider reali; non introdurre nuove
  dipendenze.
- **Criteri di accettazione.** L'utente può configurare uno o più agenti secondo
  §9; configurazione mutabile prima del run; test verdi.

### 19.13 UI-RUN-A

- **Scopo.** Implementare l'Execution settings panel e il Run progress panel
  (§8.1).
- **File probabili.** Componenti sotto `apps/web/*`; test.
- **Cosa non deve fare.** Non ricalcolare lo stato del run lato UI; non
  introdurre nuove dipendenze.
- **Criteri di accettazione.** L'utente può avviare un run e seguirne
  l'avanzamento via vista derivata read-only; test verdi.

### 19.14 UI-RESULTS-A

- **Scopo.** Implementare il Results / Evidence Report panel (§8.1).
- **File probabili.** Componenti sotto `apps/web/*`; test.
- **Cosa non deve fare.** Non ricalcolare il gate o gli assi lato UI; non
  promettere verità; non introdurre nuove dipendenze.
- **Criteri di accettazione.** Il pannello mostra risposta multi-AI, evidenze,
  limiti, contraddizioni/gap e stato *published/held* come vista derivata;
  wording sobrio verificato da test; test verdi.

### 19.15 Ordine consigliato

L'ordine consigliato segue le dipendenze: prima lo schema
(`ORCH-SCHEMA-PRE` → `ORCH-SCHEMA-A`), poi il provider
(`ORCH-PROVIDER-PRE` → `ORCH-PROVIDER-A`), poi il runner per gradi
(`ORCH-RUNNER-A` → `ORCH-MULTI-A` → `ORCH-REVIEW-A` → `ORCH-SYNTHESIS-A` →
`ORCH-GATE-A`), e infine le superfici UI (`UI-PROMPT-A` → `UI-AGENTS-A` →
`UI-RUN-A` → `UI-RESULTS-A`). Le fasi UI possono in parte procedere in parallelo
alle fasi worker una volta che lo schema è stabile, ma `UI-RESULTS-A` ha senso
solo dopo `ORCH-GATE-A`.

---

## 20. Non-goals

Questa fase, e il documento che produce, hanno i seguenti **non-goals
espliciti**. Nessuno di questi va perseguito in questa fase:

- **non implementare provider reali in questa fase** — nessun OpenAI,
  Anthropic, Gemini o local model; resta vincolante `PROVIDERS_ENABLED=mock`;
- **non modificare il DB** — nessuna modifica a tabelle o schema esistenti;
- **non creare migration** — nessun file in `migrations/`;
- **non introdurre PDF / immagini ora** — l'ingestione di PDF e immagini è
  roadmap (§14), non lavoro di questa fase, e non va improvvisata nella UI;
- **non creare loop agentici infiniti** — l'orchestrazione è e resta bounded
  (§12); nessuna chat infinita, nessun loop non bounded, nessuna auto-recursion
  senza budget;
- **non bypassare il gate** — nessuna scorciatoia che pubblichi una risposta
  saltando il Final Answer Gate (§13.2);
- **non promettere factual truth** — il documento non dichiara che il sistema
  produca risposte "vere"; mantiene il wording sicuro;
- **non cambiare la UI in questa fase** — nessuna modifica a `apps/web/*`; la UI
  target è descritta, non implementata;
- **non rompere l'append-only audit** — nessuna entità di fatto viene resa
  mutabile; le transizioni restano eventi/versioni/snapshot, mai riscritture
  silenziose.

---

## 21. Criteri di accettazione

Il documento `PHASE_PRODUCT_ORCHESTRATION_PRE.md` è accettabile se e solo se:

1. **crea o modifica solo `PHASE_PRODUCT_ORCHESTRATION_PRE.md`** — nessun altro
   file del repository è creato o modificato;
2. **è in italiano** — l'intero documento è redatto in italiano tecnico;
3. **descrive chiaramente stato attuale vs visione** — la diagnosi dello stato
   attuale (§2), il problema prodotto (§3) e la visione target (§4) sono
   distinti e chiari;
4. **mantiene il wording sicuro** — non usa la terminologia vietata se non
   nell'eventuale elenco esplicito di wording vietato; non promette verità
   assoluta né eliminazione totale delle allucinazioni;
5. **non propone scorciatoie che bypassano il gate** — ogni percorso verso la
   pubblicazione passa per il Final Answer Gate (§13);
6. **definisce una roadmap incrementale** — la §19 propone fasi piccole,
   indipendenti e testabili, con scopo, file probabili, non-goals e criteri di
   accettazione per ciascuna;
7. **non modifica codice** — nessun file di codice è toccato;
8. **non aggiunge dipendenze** — nessun manifest o lockfile è toccato;
9. **non tocca backend / worker / UI / test** — `apps/api/*`, `apps/worker/*`,
   `apps/web/*` e i file di test restano invariati.

---

## 22. Comandi di verifica

Poiché questa fase è **documentation-only**, la verifica consiste nel
controllare che l'unico file toccato sia `PHASE_PRODUCT_ORCHESTRATION_PRE.md`.
Non sono richiesti test runtime.

```bash
git diff --check
git diff --stat
git status -sb
git diff --name-only
```

`git diff --name-only` deve elencare esclusivamente
`PHASE_PRODUCT_ORCHESTRATION_PRE.md`. `git diff --stat` deve mostrare modifiche
solo a quel file. `git status -sb` non deve mostrare file di codice, migration o
manifest modificati. `git diff --check` non deve segnalare errori di whitespace.

**Nessun test runtime è obbligatorio** per questa fase: non essendo stato
toccato alcun file di codice, backend, worker, UI o migration, le suite di test
non sono richieste.

### 22.1 Controllo opzionale di wording vietato

Come controllo opzionale, si può verificare che il documento non contenga
wording vietato:

```bash
grep -niE "truth score|verified true|verified answer|AI verified|factually true|hallucination eliminated|hallucination-free|guaranteed truth|zero hallucinations|entailed = true|source quality proves claim|CVE-lite proves support|real NLI|contradiction detector|citation-to-claim validator" PHASE_PRODUCT_ORCHESTRATION_PRE.md | grep -v "grep -niE" || true
```

Questo comando **deve restituire nulla**, salvo eventuali occorrenze all'interno
di una sezione esplicita di elenco del wording vietato. Se il comando restituisce
righe al di fuori di un tale elenco esplicito, il documento va corretto.

---

## Output finale

**1. Elenco file creati / modificati.**
Un solo file: `PHASE_PRODUCT_ORCHESTRATION_PRE.md`, creato nella PARTE 1 (sezioni
1-7) e completato in questa PARTE 2 (header e indice aggiornati, sezioni 8-22
aggiunte, blocchi di chiusura della PARTE 1 riscritti). Nessun altro file è stato
creato o modificato.

**2. Sintesi del documento finale.**
Il documento è un design di architettura completo, in italiano, che riallinea
prodotto e architettura verso un orchestratore multi-AI evidence-gated. Le
sezioni 1-7 definiscono lo scopo, diagnosticano lo stato attuale (UI-CREATE-FLOW-A
è un passo tecnico utile ma orientato a progetto/documenti/task), inquadrano il
problema prodotto, descrivono la visione target (flusso Prompt Master → Sources
→ AI Agents → Orchestration Run → Agent Outputs → Cross Review → Candidate
Synthesis → Claim Extraction → Evidence Binding → Final Answer Gate →
Published/Held), elencano cosa esiste ed è riusabile, cosa manca, e definiscono
un modello concettuale di diciotto entità. Le sezioni 8-18 dettagliano la UI
target a sei pannelli, la configurazione degli agenti, l'astrazione di provider
mock-first, la strategia di budget di token, la strategia di orchestrazione
bounded, l'integrazione con l'Evidence Gate (che non va mai bypassato), la
roadmap di ingestione documenti, e i gap di backend, worker, API e UI. Le
sezioni 19-22 propongono una roadmap incrementale di quattordici fasi piccole,
i non-goals, i criteri di accettazione e i comandi di verifica.

**3. Conferma che nessun codice è stato toccato.**
Confermato. Non è stato scritto né modificato alcun file di codice. `apps/web/*`,
`apps/api/*`, `apps/worker/*`, `packages/shared/*` sono invariati. Nessuna
dipendenza è stata aggiunta. Nessun test è stato toccato. `README.md` e
`PROJECT_STATE.md` sono invariati.

**4. Conferma che nessuna migration è stata toccata.**
Confermato. Nessun file in `migrations/` è stato creato o modificato. Il
documento descrive possibili futuri schema (§15) solo a livello concettuale, e
rinvia ogni migration alla fase `ORCH-SCHEMA-A` della roadmap.

**5. Comandi eseguiti o da eseguire.**
Comandi di verifica da eseguire (§22): `git diff --check`, `git diff --stat`,
`git status -sb`, `git diff --name-only`. Atteso: l'unico file modificato è
`PHASE_PRODUCT_ORCHESTRATION_PRE.md`. Controllo opzionale di wording vietato via
`grep -i` (§22.1): atteso nessun risultato al di fuori di elenchi espliciti.
Nessun test runtime è richiesto, essendo la fase documentation-only.

**6. Rischi e decisioni rimaste aperte.**

- *Coesistenza vs sostituzione.* Resta aperto se la linea multi-AI
  (`OrchestrationRun`) debba coesistere stabilmente con il modello task
  closed-corpus o assorbirlo. Decisione da prendere in `ORCH-SCHEMA-PRE`.
- *Destino della nozione di progetto.* Resta aperto se `projects` resti come
  contenitore organizzativo o venga reso superfluo dal `MasterPrompt`.
- *Punto di giunzione synthesis → claim.* Resta aperto se la trasformazione
  `CandidateSynthesis` → `ExtractedClaims` debba riusare direttamente il
  compiler/extractor esistente o passare per un adattatore dedicato. Decisione
  da prendere in `ORCH-SCHEMA-PRE` / `ORCH-GATE-A`.
- *Meccanismo di snapshot.* Resta aperto *come* fissare gli snapshot immutabili
  di MasterPrompt, AgentConfig, AgentRolePrompt e TokenBudget (copia inline vs
  versionamento in stile `policy_versions`).
- *Tabelle placeholder `0005`.* Resta aperto se le tabelle placeholder
  `agent_runs` / `agent_outputs` esistenti vadano adottate, ridefinite o
  sostituite. Decisione da prendere in `ORCH-SCHEMA-PRE`.
- *Provider come entità.* Resta aperto se promuovere `ProviderConfig` /
  `ProviderCredential` a entità di prima classe. Decisione da prendere in
  `ORCH-PROVIDER-PRE`.
- *Rischio di context explosion.* Anche con i budget e il bounding, la gestione
  del contesto multi-pass è un rischio concreto: va validata presto, sul mock
  provider, nelle fasi `ORCH-MULTI-A` e successive.
- *Rischio di drift semantico.* Estendendo la UI al multi-AI cresce il rischio
  di wording che implichi "verità multi-AI". I test di wording vietato vanno
  estesi a ogni nuova superficie, come già fatto per la linea UI esistente.

---

*Commit message suggerito:* `Document product orchestration architecture`
