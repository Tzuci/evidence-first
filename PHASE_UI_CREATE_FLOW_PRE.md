# PHASE_UI_CREATE_FLOW_PRE — Progettazione del flusso iniziale di prodotto

Questo è **solo un documento di pianificazione e design**. Definisce il primo
vero flusso iniziale di prodotto per Evidence-First MVP-0. Non implementa
codice di produzione, non modifica `apps/web/*`, non modifica `apps/api/*`,
`apps/worker/*`, `packages/shared/*`, migrations, Docker o script. L’unico
deliverable di questa fase è questo file.

La fase successiva a questo design — **UI-CREATE-FLOW-A** — dovrebbe
implementare la creazione reale di task dal browser, perché l’ispezione del
backend (vedi §3) mostra che la superficie API richiesta esiste già ed è
documentata. Quando un contratto è mancante o debole, questo documento registra
esplicitamente il gap (§7, §10), così la fase successiva non inventa
comportamenti.

Promemoria sul linguaggio. Il sistema è evidence-first ed evidence-gated. Non
promette certezza fattuale. Produce risposte basate sulle evidenze disponibili
e può trattenere la pubblicazione quando il supporto è insufficiente. Le regole
di copy in §11 e la lista di wording vietato in §11.3 sono vincolanti per ogni
pagina introdotta o modificata dalla prossima fase di codice.

---

## 1. Problema UX attuale

La route attuale `/` è un entrypoint usabile per uno sviluppatore, ma non per
un utente di prodotto. Oggi offre una sola azione reale: “Open existing task”,
un form HTML plain GET che prende un task id (UUID) e inoltra a
`/tasks/<taskId>`. La stessa home page dichiara, nella sezione “Not available
in the browser yet”, che creazione task, upload documenti e selezione documenti
sono workflow browser pianificati per fasi successive.

La conseguenza è che un utente normale non può iniziare a usare il prodotto
dal browser. Per ottenere un task id deve avere già eseguito la pipeline backend
in altro modo: uno smoke test `curl`, un realistic-flow test `pytest`, oppure
una query diretta `psql` su `task_masters`. La home glielo dice persino: la
riga di aiuto sotto il form dice “Need a task id? Query your local DB or use an
id from an existing report”, e la card “Inspect local task ids” mostra un
pattern di comando `docker exec`.

L’esperienza attuale parte quindi dalla domanda sbagliata. Chiede “Quale task
id hai già?” quando la vera domanda dell’utente è “Che risposta voglio ottenere
e quali fonti deve usare il sistema?”. Un entrypoint di prodotto deve consentire
all’utente di esprimere un bisogno e trasformare quel bisogno in un task, non
presupporre che un task esista già.

In concreto, l’UI attuale non permette all’utente di capire o fare in modo
diretto nessuna di queste cose: che cosa fa l’app, che cosa deve fornire, che
cosa succede dopo l’invio della richiesta, che cosa l’app può mostrargli, che
cosa significa “publication held” e dove andare dopo. UI-HOME-B ha aggiunto
buona copy esplicativa per diversi di questi punti, ma una spiegazione non è un
workflow. Il pezzo mancante è un percorso guidato da “ho una domanda e alcune
fonti” a “ho un task e una pagina dove seguirlo”.

---

## 2. Obiettivo di prodotto

L’obiettivo del flusso iniziale è permettere a un utente di partire dal proprio
bisogno e arrivare a un task osservabile, senza mai toccare un task id, il
database o una route interna.

Il flusso deve rispondere alle domande dell’utente in questo ordine:

- Che cosa fa l’app? — una frase sobria nella home e in cima al flusso di nuova
  richiesta.
- Che cosa devo fornire? — un progetto, una o più fonti e una richiesta
  (l’obiettivo).
- Che cosa succede dopo l’invio? — viene creato un task e l’utente atterra
  sulla pagina di task summary, che mostra lo stato di elaborazione.
- Che cosa può mostrarmi l’app? — il task summary (user-facing) e il technical
  report (audit/debugging), entrambi già costruiti.
- Che cosa significa “publication held”? — che i controlli disponibili non
  hanno trovato supporto sufficiente per la pubblicazione; è un esito previsto,
  non un errore, e non significa che la risposta sia falsa nel mondo.
- Dove vado dopo? — prima il task summary, poi il technical report se l’utente
  vuole dettagli di audit.

Il flusso deve restare onesto. Non promette verifica della verità. Non promette
certezza fattuale. Inquadra l’input dell’utente come “richiesta” o
“obiettivo”, mai come “claim da verificare come vero”.

Riaffermazione del non-goal. Questa fase non costruisce il flusso. Lo progetta
e decide se la prossima fase debba implementare creazione reale di task o una
pagina guidata statica (vedi §7). La raccomandazione, giustificata da §3, è la
creazione reale di task.

---

## 3. Capacità backend esistenti

Queste capacità sono lette dalla tabella endpoint in `PROJECT_STATE.md` e dai
file route `apps/api/app/routes/projects.py`, `documents.py` e `tasks.py`. La
prossima fase di codice deve riverificare contro il proprio HEAD prima di
basarsi su di esse, ma al commit riflesso dai file forniti sono presenti e si
comportano come descritto sotto.

Endpoint progetto. `POST /api/v1/projects` crea un progetto. Il body è lo
schema `ProjectCreate`, che la route usa per `name` e `mode_default`. In caso
di successo restituisce `201` con un oggetto `ProjectRead`: `id`, `tenant_id`,
`name`, `mode_default`, `created_by`, `created_at`. Se un progetto con lo stesso
`name` esiste già nel dev tenant, la route solleva `RESOURCE_CONFLICT`.
`GET /api/v1/projects` lista i progetti per il dev tenant con paginazione a
cursor (`items`, `next_cursor`). `GET /api/v1/projects/{id}` restituisce un
singolo `ProjectRead` oppure `404 RESOURCE_NOT_FOUND`. Tenant e dev user sono
risolti dai seed; non c’è auth.

Endpoint documenti. `POST /api/v1/projects/{project_id}/documents` accetta un
singolo upload multipart (campo form `file`). Impone estensioni `.txt`/`.md`,
limite 50 MiB e rifiuta file vuoti. In caso di successo restituisce `201` con
un oggetto `DocumentRead`. La route salva il blob, crea una riga
`uploaded_documents`, una `document_versions` `parsed`, `document_chunks`
deterministici e uno `evidence_spans` per chunk. `GET
/api/v1/projects/{id}/documents` lista i documenti del progetto (`{items:
[...]}`).

Endpoint task. `POST /api/v1/tasks` crea un task `closed_corpus`. Il body è lo
schema `TaskCreate`, che la route consuma per `project_id`, `objective`, `mode`
(deve essere `closed_corpus`, altrimenti `VALIDATION_ERROR`), `document_ids`
(lista di UUID) e un oggetto opzionale `policy`. La route valida che ogni
`document_id` esista e appartenga al progetto del task: id mancanti producono
`RESOURCE_NOT_FOUND` con `details.missing`, id di altri progetti producono
`VALIDATION_ERROR` con `details.foreign`, duplicati producono `VALIDATION_ERROR`
con `details.duplicate`. Un header opzionale `Idempotency-Key` permette al
caller di ripetere una creazione in modo sicuro: se un task nello stesso
progetto porta già quella chiave nella sua `policy`, viene restituito il task
esistente invece di crearne uno nuovo. In caso di successo la route restituisce
`201` con un oggetto `TaskRead` il cui `status` è `created`, scrive gli audit
event `task.created` e, quando sono allegati documenti, `task.docs_attached`, e
pubblica un evento `task.created` su Redis per il worker. `GET
/api/v1/tasks/{id}` restituisce un `TaskRead` oppure `404 RESOURCE_NOT_FOUND`.

Endpoint report. `GET /api/v1/tasks/{task_id}/anti-hallucination-report` è la
vista aggregata read-only già consumata dalle pagine di task summary e report.
Restituisce `200` con campi parziali per un task appena creato
(`publication.status='not_ready'`, `claims`/`evidence` vuoti), e `404
RESOURCE_NOT_FOUND` con `details.resource='task_masters'` per un id
sconosciuto.

Cosa significa per il flusso. Creazione progetto, upload documenti, lista
documenti e creazione task da progetto + documenti + obiettivo sono tutti
endpoint HTTP reali e documentati. Il contratto backend è sufficiente per
implementare davvero l’intero journey Step 1 → Step 6. Non serve un mock
statico. I punti deboli noti sono registrati in §7 e §10; nessuno di essi
blocca una implementazione reale.

---

## 4. Capacità frontend esistenti

Queste informazioni derivano dai file `apps/web/*` forniti.

Route esistenti oggi: `/` (home prodotto UI-HOME-B, server component statico
con form GET), `/tasks` (server component che redirecta `?taskId=<id>` a
`/tasks/<id>` e altrimenti renderizza una guida), `/tasks/[taskId]` (pagina
Task Summary user-facing), `/tasks/[taskId]/report` (viewer tecnico
Anti-Hallucination Report), e `/diagnostic` (pagina legacy che chiama una route
`/api/proxy-health` non esistente).

Il client API `apps/web/lib/api.ts` espone esattamente una funzione:
`getAntiHallucinationReport(taskId)`. Risolve la base URL backend da
`NEXT_PUBLIC_API_BASE_URL` con fallback `http://localhost:8000`, usa `cache:
"no-store"` e solleva errori tipizzati — `ApiError` (HTTP non-2xx, con status,
envelope parsato e raw body) e `ApiNetworkError` (fetch fallito). Non esiste
nessuna funzione client per creare progetto, caricare documento, listare
documenti o creare task.

Il modulo tipi `apps/web/lib/reportTypes.ts` copre la shape del report. Il
modulo di formatting `apps/web/lib/reportFormatting.ts` fornisce helper di
label e `isTerminalPublicationStatus`, quest’ultimo scritto esplicitamente con
un futuro consumer di polling in mente.

Componenti esistenti: `ReportStatusBadge`, `PublicationPanel`, `GatePanel`,
`AxisSummaryCards`, `MockIndicatorsPanel`, `LimitationsPanel`,
`RawJsonCollapsible`, `TaskSummaryView`. Sono tutti renderer read-only del
payload report. Nessuno è un form, un collector di input o una superficie di
creazione.

Convenzioni in vigore, che la prossima fase deve mantenere: server components
di default, niente `"use client"` salvo dove un componente interattivo client è
davvero necessario; inline styles only (niente Tailwind, CSS modules,
component libraries, nuove dipendenze); form HTML plain `<form>` con
`method="get"` per navigazioni che non chiamano il backend; gestione errori
tipizzata in linea con la report page; copy sobria e non overclaiming
verificata da test di wording vietato.

Sintesi gap per il flusso. Per implementare lo start flow la prossima fase deve
aggiungere: funzioni API client per lista/creazione progetto, upload/lista
documenti e creazione task; una o più nuove route sotto `/requests/new`; piccoli
componenti client interattivi per gli step del form (upload file e task creation
sono azioni POST e non possono essere un form GET). Tutto il resto — landing
page dopo la creazione, vista di processing — esiste già come `/tasks/[taskId]`.

---

## 5. User journey proposto

Il journey ha sette step. Gli step 1–5 sono il nuovo flusso; lo step 6 è la
pagina task summary esistente; lo step 7 è il technical report esistente.

Step 1 — Avvia richiesta. Da `/`, l’utente vede una call to action primaria,
“New evidence-based request”, che linka a `/requests/new`. La pagina nuova
richiesta si apre con una frase sobria: “Ask a question or describe the answer
you need. Evidence-First will use the available sources attached to the request
and may hold publication when support is insufficient.” Nessuna promessa di
certezza fattuale; nessun linguaggio da truth scoring.

Step 2 — Seleziona o crea progetto. L’utente sceglie un progetto esistente o ne
crea uno minimale. Il backend supporta entrambe le azioni: `GET
/api/v1/projects` lista i progetti e `POST /api/v1/projects` ne crea uno con
solo `name`. Il flusso dovrebbe listare di default i progetti esistenti e
offrire “Create a new project” come azione secondaria. Se la lista è vuota, il
form di creazione viene mostrato per primo. Non si assume un progetto seed di
default; il dev seed può averne creato uno oppure no, e il flusso lo scopre
listando invece di indovinare.

Step 3 — Aggiungi fonti. L’utente allega uno o più documenti `.txt`/`.md` al
progetto scelto. Il backend supporta upload browser via `POST
/api/v1/projects/{id}/documents` e lista via `GET
/api/v1/projects/{id}/documents`. Il flusso dovrebbe permettere all’utente di
caricare un nuovo documento e/o selezionare documenti già presenti nel progetto,
poi raccogliere il set di `document_ids` da allegare al task. L’upload è un POST
multipart reale e richiede quindi un client component.

Step 4 — Scrivi richiesta. L’utente digita l’obiettivo: la domanda o la
descrizione della risposta di cui ha bisogno. Questo mappa a
`TaskCreate.objective`. Il campo è etichettato “Request” o “Objective”. Non è
mai etichettato “truth verification” e la copy intorno non promette mai certezza
fattuale.

Step 5 — Crea task. Con un progetto, un set di document ids e un obiettivo, il
flusso chiama `POST /api/v1/tasks` con `{project_id, objective, mode:
"closed_corpus", document_ids}`. Il flusso deve inviare un header
`Idempotency-Key` (valore generato una volta per tentativo di submit), così un
doppio submit accidentale non crea due task. Su `201`, la response porta il
nuovo task id.

Step 6 — Stato di elaborazione. Il flusso naviga il browser a `/tasks/<taskId>`
— la pagina Task Summary esistente. Un task appena creato ha status `created`;
l’endpoint report restituisce `publication.status='not_ready'`, e
`TaskSummaryView` renderizza già uno stato “Not ready yet” con la spiegazione
che il task non ha ancora raggiunto lo step di pubblicazione. La summary page
mostra l’obiettivo del task, lo stato di pubblicazione (not ready yet / answer
available / publication held) e un link al technical report. Il polling
automatico **non** è richiesto per UI-CREATE-FLOW-A; l’utente può ricaricare per
aggiornare. Se il polling viene aggiunto, deve rispettare i vincoli in §9.

Step 7 — Technical report come secondario. `/tasks/<taskId>/report` resta
disponibile come superficie di audit/debugging. Non è il percorso primario; lo
è il task summary. Entrambe le pagine sono viste derivate read-only e nessuna
prende una nuova decisione.

Il journey non espone mai il task id come qualcosa che l’utente deve conoscere,
digitare o cercare. L’id compare solo dentro un URL verso cui il flusso naviga
per conto dell’utente.

---

## 6. Modello pagine proposto

### `/`

Home prodotto primaria. Esiste già (UI-HOME-B) e verrebbe aggiornata
leggermente dalla prossima fase. Dopo l’aggiornamento dovrebbe mostrare: che
cosa fa l’app in una frase; una CTA primaria “New evidence-based request” verso
`/requests/new`; una CTA secondaria “Open existing task” (il form GET con task
id esistente, mantenuto per utenti che hanno già un id); una breve spiegazione
del workflow; e le limitazioni correnti MVP. Il form esistente “Open existing
task” viene mantenuto, non rimosso — è ancora il percorso più veloce per uno
sviluppatore con un id a disposizione.

### `/requests/new`

Il nuovo flusso richiesta. Per MVP-0 è una singola pagina con sezioni ordinate,
oppure un piccolo insieme di sotto-step; una singola pagina è più semplice ed è
preferita. Sezioni:

- Project — lista dei progetti esistenti (da `GET /api/v1/projects`) con un
  selettore, più azione “Create new project” (`POST /api/v1/projects`).
- Sources — upload documenti `.txt`/`.md` (`POST
  /api/v1/projects/{id}/documents`) e/o selezione documenti esistenti del
  progetto (`GET /api/v1/projects/{id}/documents`); la sezione raccoglie un set
  di `document_ids`.
- Request — input testuale per l’obiettivo.
- Create task — bottone che chiama `POST /api/v1/tasks` e, in caso di
  successo, naviga a `/tasks/<taskId>`.

Poiché upload e POST di task creation richiedono interazione lato client,
`/requests/new` (o i suoi sottocomponenti interattivi) è un client component, a
differenza delle pagine report.

### `/tasks/[taskId]`

Task summary user-facing. Esiste già. Lo stato di processing, lo stato di
pubblicazione e il link al report sono già renderizzati da `TaskSummaryView`.
Potrebbe richiedere piccoli miglioramenti più avanti (per esempio un affordance
“Refresh”), ma nessuna modifica è richiesta perché il flusso vi atterri
correttamente.

### `/tasks/[taskId]/report`

Technical report. Esiste già. Resta la superficie secondaria orientata ad audit.

Il flusso **non** introduce deliberatamente route standalone `/projects/new`,
`/projects/[id]/documents/new` o `/tasks/new`. Creazione progetto e upload
documenti sono sezioni dentro `/requests/new`, perché l’obiettivo di prodotto è
un flusso guidato unico, non un insieme di pagine admin scollegate. Se una fase
successiva richiederà gestione progetto/documenti standalone, potrà aggiungere
quelle route allora; qui sono fuori scope.

---

## 7. Decisione importante: creazione task reale vs mock guidato

La prossima fase di codice, UI-CREATE-FLOW-A, dovrebbe **implementare la
creazione reale di task dal browser**.

Giustificazione. La risposta preferita nel phase brief è: se le API backend
sono sufficienti, implementare creazione reale; solo se i contratti sono
incompleti, costruire una pagina guidata “Start request” che dichiari cosa
manca. L’ispezione del backend (vedi §3) mostra che i contratti sono
sufficienti. `POST /api/v1/projects`, `POST
/api/v1/projects/{id}/documents`, `GET /api/v1/projects`, `GET
/api/v1/projects/{id}/documents` e `POST /api/v1/tasks` esistono tutti, sono
indicati come attivi in `PROJECT_STATE.md` e hanno route source coerenti con il
comportamento descritto. Quindi le condizioni per il fallback “guided mock” non
sono soddisfatte.

Cosa esclude esplicitamente. La prossima fase non deve simulare una creazione
task riuscita. Non deve mostrare un task id fabbricato, una success screen
fabbricata o una navigazione a un URL `/tasks/<taskId>` per un task mai creato.
Ogni esito “task created” nella UI deve corrispondere a un vero `201` da
`POST /api/v1/tasks` con un vero task id.

Fallback condizionale dentro uno step. La raccomandazione è creazione reale per
l’intero flusso, ma la decisione può essere presa per sezione se, quando la
prossima fase verifica contro il proprio HEAD, il contratto di una sezione
risulta più debole di quanto assunto da questo documento. Se l’upload documento
non potesse funzionare dal browser per un motivo tecnico concreto (per esempio
una limitazione CORS, vedi §10), la fase successiva può degradare lo step
Sources a “seleziona solo documenti progetto esistenti” e dichiarare
chiaramente che l’upload browser è rinviato — ma deve comunque creare davvero
il task dai documenti selezionati. Degradare uno step a copy onesta “not
available yet” è accettabile; simulare successo non lo è.

Gap di contratto backend che non bloccano la creazione reale ma vanno annotati
per la prossima fase: non esiste un percorso “crea task senza documenti che
produca comunque una risposta utile” — un task con zero documenti va
`analyzing → blocked`, quindi il flusso deve richiedere almeno un documento
prima di abilitare “Create task”. Non esiste un modo browser-facing per
cancellare progetto o documento, quindi il flusso deve trattare la creazione
come append-only e non offrire undo. Questi punti sono documentati in §10.

---

## 8. Campi dati richiesti dal form

Sezione Project.

- Modalità progetto: selezionare un progetto esistente oppure crearne uno nuovo.
- Selettore progetto esistente: il valore è un `id` progetto (UUID), scelto
  dalla lista restituita da `GET /api/v1/projects`. Visualizzare il `name`; l’id
  resta interno.
- Nome nuovo progetto: stringa non vuota, inviata come `ProjectCreate.name` a
  `POST /api/v1/projects`. Il form deve mostrare il caso `RESOURCE_CONFLICT`
  (“esiste già un progetto con quel nome”) come errore inline recuperabile, non
  come hard failure.

Sezione Sources.

- File caricati: ciascuno è un file `.txt` o `.md` sotto 50 MiB, inviato uno
  alla volta come multipart `file` a `POST
  /api/v1/projects/{id}/documents`. Il form deve rifiutare lato client file
  vuoti ed estensioni non `.txt`/`.md` per un errore rapido, e gestire comunque
  difensivamente il `VALIDATION_ERROR` backend per gli stessi casi.
- Documenti esistenti selezionati: ciascuno è un `id` documento (UUID) scelto da
  `GET /api/v1/projects/{id}/documents`. Visualizzare il `filename`.
- L’output della sezione è `document_ids`: il set combinato degli id dei
  documenti caricati e selezionati. Il flusso ne richiede almeno uno (vedi §7 e
  §10).

Sezione Request.

- Objective: stringa free-text non vuota, inviata come `TaskCreate.objective`.
  Etichetta: “Request” oppure “Objective”. Il form deve richiedere non-vuoto e
  può suggerire una lunghezza massima ragionevole per leggibilità, ma non deve
  troncare silenziosamente.

Creazione task (assemblata, non come campi separati user-visible).

- `project_id`: id del progetto scelto o appena creato.
- `mode`: sempre il literal `"closed_corpus"` (l’unico valore accettato dal
  backend).
- `document_ids`: dalla sezione Sources.
- `policy`: omesso oppure oggetto vuoto; MVP-0 non ha input policy.
- Header `Idempotency-Key`: valore generato una volta per tentativo di submit,
  così un doppio click non crea due task.

Regole di validazione prima di abilitare “Create task”: progetto selezionato o
creato; almeno un document id presente; objective non vuoto.

---

## 9. Modello di elaborazione e navigazione

Su `POST /api/v1/tasks` riuscito (`201`), il flusso legge il task `id` dalla
response `TaskRead` e naviga il browser a `/tasks/<id>`. L’id è dato di path URL
costruito dal flusso; l’utente non lo vede mai come qualcosa da copiare.

`/tasks/<id>` è la pagina Task Summary esistente. Un task appena creato ha
status `created` e l’endpoint report restituisce `publication.status='not_ready'`.
`TaskSummaryView` renderizza già uno stato “Not ready yet” con la spiegazione
che il task non ha raggiunto lo step di pubblicazione. Man mano che il worker
avanza il task, un reload della pagina mostra lo stato aggiornato: “Answer
available” quando pubblicato, oppure “Publication held” quando il gate trattiene
la pubblicazione.

Polling. Il polling automatico **non** è richiesto per UI-CREATE-FLOW-A. La
summary page dice già all’utente di tornare più tardi, e un reload manuale è
sufficiente per MVP-0. Se una fase successiva aggiunge polling, deve: essere
opt-in oppure funzionare solo mentre il task non è terminale; rifare fetch a
intervallo moderato (per esempio una richiesta ogni pochi secondi); fermarsi
appena `publication.status` è terminale — `published`, `publication_held`,
`failed`, `withdrawn` o `superseded` (l’helper `isTerminalPublicationStatus` in
`reportFormatting.ts` già codifica questo set); ed essere cancellabile
dall’utente. Il polling non deve mai ricalcolare o reinterpretare stato backend;
deve solo rifare fetch del report derivato.

La navigazione copre anche i percorsi infelici. Se la creazione task fallisce,
il flusso resta su `/requests/new`, conserva l’input utente e mostra l’errore
(vedi §10). Il flusso non naviga verso una pagina task salvo che sia stato
restituito un vero task id.

Vale la pena dichiarare la dipendenza dal worker: creare il task pubblica solo
un evento `task.created`. Il task avanza solo se il worker è in esecuzione. Il
flusso non deve bloccare sul worker; naviga alla summary page, che mostra
correttamente “Not ready yet” finché il worker non ha fatto il suo lavoro. Se il
worker è down, il task resta semplicemente not ready — è onesto e non è un
errore del flow.

---

## 10. Stati di errore

Sezione Project.

- `POST /api/v1/projects` restituisce `409 RESOURCE_CONFLICT` quando il nome è
  già usato. Mostrare un messaggio inline recuperabile e lasciare l’utente
  scegliere un altro nome o selezionare il progetto esistente.
- `GET /api/v1/projects` non raggiungibile (network error): mostrare “API
  unreachable” con la base URL configurata, in linea con la gestione
  `ApiNetworkError` della report page.
- `GET /api/v1/projects/{id}` restituisce `404`: trattare come “project no
  longer available” e riportare l’utente alla selezione progetto.

Sezione Sources.

- Upload `400 VALIDATION_ERROR`: estensione non supportata, file vuoto o MIME
  non valido. Mostrare inline il messaggio backend; conservare il resto del
  form.
- Upload `413`/`STORAGE_INLINE_TOO_LARGE`: file oltre 50 MiB. Mostrare un
  messaggio sulla dimensione; l’utente può scegliere un file più piccolo.
- Upload network error: “API unreachable”; l’utente può riprovare.
- Zero documenti al momento di “Create task”: il flusso disabilita il bottone e
  spiega che è richiesta almeno una fonte, perché un task senza documenti va
  `analyzing → blocked` e non produce una risposta utile.

Creazione task.

- `POST /api/v1/tasks` `400 VALIDATION_ERROR` con `details.missing`,
  `details.foreign` o `details.duplicate`: problema sugli id documento. Mostrare
  quali id sono coinvolti e riportare l’utente alla sezione Sources. `mode` è
  sempre inviato come `closed_corpus`, quindi l’errore di validazione
  `received_mode` non dovrebbe accadere; se accade, mostrarlo verbatim come
  errore inatteso.
- `POST /api/v1/tasks` `404 RESOURCE_NOT_FOUND` (“Project not found”): il
  progetto è sparito tra selezione e submit; riportare l’utente alla selezione
  progetto.
- `POST /api/v1/tasks` `5xx`: mostrare “Task creation failed” con raw error
  envelope in un blocco collapsible, conservando l’input utente.
- Network error: “API unreachable” con la base URL; l’utente può riprovare.
  Poiché il flusso invia `Idempotency-Key`, un retry dopo un fallimento ambiguo
  è sicuro — se il primo tentativo è in realtà riuscito, il retry restituisce lo
  stesso task.

Regole generali. Non mascherare mai un errore con uno stato vuoto silenzioso;
mostrare sempre almeno l’error `code`. Non navigare mai a una pagina task su un
non-successo. Non usare muri di jargon in bullet nell’error copy user-facing;
tenerla breve e recuperabile. L’envelope normalizzato raw può essere mostrato
in un blocco `<details>` collapsible per diagnosi, esattamente come fanno già
report e summary page.

Limitazioni note del contratto registrate per la prossima fase: non esiste
project/document deletion browser-facing, quindi il flusso è append-only e non
offre undo; non esiste validazione che un `.txt`/`.md` caricato contenga
qualcosa che la pipeline possa supportare, quindi un task creato da un documento
scarno può legittimamente finire in `publication_held` — è un esito previsto e
la copy non deve presentarlo come errore del flusso.

---

## 11. Regole di safe copy

### 11.1 Tono

Il flusso descrive un processo che controlla risposte generate rispetto alle
fonti disponibili e può trattenere la pubblicazione. Non dichiara mai di
stabilire la verità. Non promette mai certezza fattuale. La pubblicazione
trattenuta significa che i controlli disponibili non hanno trovato supporto
sufficiente — non che la risposta sia falsa nel mondo.

### 11.2 Wording consigliato

Usare: “ask a question or describe the answer you need”; “available sources”;
“attach sources to the request”; “answer based on available evidence”; “checked
against available sources”; “publication held”; “support is insufficient”;
“claim-evidence relation”; “quote/hash check”; “source quality signal”;
“technical report for audit and debugging”; “derived read-only view”; “not a
new decision”; “new evidence-based request”.

Il campo input dell’utente è “Request” oppure “Objective”. L’azione è “Create
task” oppure “Start request”. La pagina post-creazione è il “task summary”.

### 11.3 Wording vietato

Le frasi seguenti non devono apparire da nessuna parte nella UI copy del
flusso, costanti, tooltip o fixture di test, eccetto dentro questa sottosezione
di wording vietato: "truth score"; "verified true"; "verified answer"; "AI
verified"; "factually true"; "hallucination eliminated"; "hallucination-free";
"guaranteed truth"; "zero hallucinations"; "entailed = true"; "source quality
proves claim"; "CVE-lite proves support"; "real NLI"; "contradiction detector";
"citation-to-claim validator".

Questa lista è coerente con i test di wording vietato già presenti nel
repository (`home.test.tsx`, `taskSummaryView.test.tsx`,
`no-misleading-labels.test.tsx`). La prossima fase deve aggiungere un test
equivalente per ogni nuova pagina e componente.

---

## 12. Guardrail semantici

Il flusso deve preservare, in copy e comportamento, le stesse distinzioni
semantiche del resto dell’UI:

- CVE-lite è un controllo quote/hash/presenza testuale. Non è supporto
  semantico e non è prova di un claim.
- Source Quality è un segnale sulla qualità di una fonte o di uno span. Non è
  prova che un claim sia corretto.
- Claim Entailment è una relazione locale claim-evidence secondo la
  normalizzazione di un checker mock. Un verdict “entailed” non significa che
  il claim sia vero nel mondo.
- Il Final Gate è una decisione di pubblicazione secondo una policy versionata.
  Non è una decisione di verità.
- “Publication held” non significa che il claim sia falso nel mondo. Significa
  che il supporto era insufficiente per la pubblicazione.
- Il task summary e il technical report sono viste derivate read-only. Non
  ricalcolano decisioni backend e non prendono nuove decisioni.
- L’UI non deve ricalcolare nessuna decisione backend. Lo start flow crea solo
  entità e poi naviga a una vista derivata; non calcola mai gate outcome,
  verdict o status autonomamente.

Il nuovo-request flow aggiunge una superficie dove questi guardrail sono
particolarmente importanti: il campo Step 4 “Request”. La copy intorno non deve
implicare che inviare una richiesta produca una risposta garantita come vera.
Produce un task la cui risposta, se pubblicata, è basata sulle evidenze
disponibili e può essere trattenuta.

---

## 13. Non-goals

Questa fase non implementa codice, e la prossima fase di codice
(UI-CREATE-FLOW-A) è limitata allo start flow. Sono fuori scope per questa fase
e per UI-CREATE-FLOW-A:

- Modificare backend, worker, database o migrations.
- Aggiungere nuovi endpoint API.
- Modificare configurazione Docker o script.
- Introdurre autenticazione o RBAC.
- Introdurre una UI task history / task list (menzionata solo come roadmap
  futura, §14).
- Introdurre un design system, Tailwind, component library UI o nuove
  dipendenze.
- Introdurre chiamate AI esterne.
- Simulare una creazione task riuscita, un task id fabbricato o una success
  screen fabbricata.
- Pagine standalone di project-management o document-management oltre alle
  sezioni dentro `/requests/new`.
- Polling automatico sulla task summary page (consentito solo in una fase
  successiva, secondo i vincoli §9).
- Editing, deletion, withdrawal o republishing di qualunque entità dal browser.

---

## 14. Roadmap di implementazione

UI-CREATE-FLOW-PRE — questo documento. Done dopo acceptance.

UI-CREATE-FLOW-A — implementare il vero start flow. Scope: aggiungere funzioni
API client in `apps/web/lib/` per lista progetto, creazione progetto, upload
documento, lista documenti e creazione task, ciascuna con errori tipizzati in
linea con `getAntiHallucinationReport`; aggiungere la route `/requests/new` e i
componenti client interattivi per le sezioni Project, Sources e Request più
l’azione “Create task”; aggiornare `/` aggiungendo la CTA primaria “New
evidence-based request” mantenendo il form esistente “Open existing task”;
navigare a `/tasks/<taskId>` su un vero `201`. Nessuna modifica backend.
Nessun successo simulato. Test secondo §16.

UI-CREATE-FLOW-B (follow-up opzionale) — rifiniture: un affordance “Refresh”
sulla task summary page; eventualmente polling vincolato secondo le regole §9;
migliore UX multi-document upload. Solo dopo che UI-CREATE-FLOW-A è stabile.

Futuro, non pianificato qui — pagina task history / task list per permettere
agli utenti di ritrovare task passati senza URL; gestione standalone di
progetti e documenti; auth. Sono item di roadmap, non impegni.

Ordine raccomandato: UI-CREATE-FLOW-PRE → UI-CREATE-FLOW-A → (opzionale)
UI-CREATE-FLOW-B.

---

## 15. Acceptance criteria per la prossima fase di codice

UI-CREATE-FLOW-A è accettabile se e solo se:

1. Esiste una route `/requests/new`, compila con `next build` e renderizza le
   sezioni Project, Sources, Request e Create-task.
2. Esistono nuove funzioni API client in `apps/web/lib/` per: list projects,
   create project, upload document, list project documents e create task.
   Ciascuna risolve la base URL da `NEXT_PUBLIC_API_BASE_URL` con fallback
   `http://localhost:8000`, e solleva `ApiError` / `ApiNetworkError` in modo
   coerente con il client esistente.
3. La creazione task esegue un vero `POST /api/v1/tasks` con `{project_id,
   objective, mode: "closed_corpus", document_ids}` e un header
   `Idempotency-Key`, e su `201` naviga il browser a `/tasks/<taskId>` usando
   il vero id restituito.
4. Non avviene mai nessun task id fabbricato, success screen fabbricata o
   navigazione fabbricata; ogni outcome “task created” corrisponde a un vero
   `201`.
5. Il flusso richiede un progetto selezionato-o-creato, almeno un document id e
   un objective non vuoto prima di abilitare “Create task”.
6. Gli stati di errore da §10 sono gestiti: project conflict, validation/size
   errors dell’upload, validation errors sui document id, project not found,
   `5xx` e network errors — nessuno mascherato da stato vuoto silenzioso.
7. `/` mostra la CTA primaria “New evidence-based request” verso
   `/requests/new` e mantiene ancora il form esistente “Open existing task”.
8. Nessuna frase vietata da §11.3 appare nella nuova copy, costanti o fixture,
   verificato da test.
9. Nessun file backend, worker, database, migration, Docker o script viene
   modificato.
10. Nessuna libreria UI o nuova dipendenza viene introdotta; inline styles only.
11. Le pagine esistenti (`/`, `/tasks/[taskId]`, `/tasks/[taskId]/report`,
    `/tasks`, `/diagnostic`) continuano a compilare e i test esistenti
    continuano a passare.

---

## 16. Strategia di test

Tooling invariato: Vitest + Testing Library + jsdom, coerente con
`apps/web/package.json` e `vitest.config.ts`, con `fetch` mockato.

Test API client. Per ogni nuova funzione client, testare success path (URL
corretto, metodo corretto, body o multipart payload corretto, response parsata)
e error paths (`ApiError` su non-2xx con envelope parsato; `ApiNetworkError`
quando `fetch` fallisce). Il test di create task verifica che l’header
`Idempotency-Key` venga inviato e che `mode` sia il literal `closed_corpus`.
Questi test rispecchiano gli esistenti `api.test.ts` e `api-error.test.ts`.

Test componenti form. Sezione Project: selezionare un progetto esistente imposta
il project id; creare un progetto chiama `POST /projects` e mostra inline un
`RESOURCE_CONFLICT`. Sezione Sources: un upload chiama `POST
/projects/{id}/documents`; un `.pdf` o file vuoto viene rifiutato; selezionare
un documento esistente aggiunge il suo id. Sezione Request: objective vuoto
mantiene “Create task” disabilitato. Create-task: con progetto, un documento e
objective, il bottone chiama `POST /tasks` e, su `201`, triggera navigazione a
`/tasks/<id>` (navigation function mockata, come `tasksRedirectPage.test.tsx`
mocka `next/navigation`).

Test error-path componenti. Ogni errore §10 renderizza il messaggio
documentato e non naviga a una pagina task; l’input utente viene preservato.

Test wording vietato. Un test stile `no-misleading-labels` renderizza ogni
nuova pagina e componente e verifica che il DOM renderizzato non contenga
nessuna delle frasi §11.3, case-insensitive.

Fuori scope per questi test: backend reale; run browser end-to-end; visual
regression. Il realistic flow resta coperto dai test backend esistenti
(`tests/test_phase_8_8b_report_flow.py`).

---

## 17. Checklist di validazione manuale

Poiché questa fase è documentation-only, la validazione è:

```bash
git diff --check
git diff --stat
git status -sb
```

L’unica modifica introdotta da questa fase è l’aggiunta di
`PHASE_UI_CREATE_FLOW_PRE.md`. Nessun file sotto `apps/web/`, `apps/api/`,
`apps/worker/`, `packages/shared/`, `migrations/`, Docker o `scripts/` viene
toccato, quindi `npm test` / `npm run build` e le suite Python non sono
richiesti per questa fase.

Per la prossima fase (UI-CREATE-FLOW-A), quando si toccherà codice app:

```bash
cd apps/web
npm test
npm run build
```

Se in quella fase viene toccato qualunque file Python o backend, fermarsi e
spiegare perché — le modifiche backend sono fuori scope per tutta la linea di
lavoro UI-CREATE-FLOW.

Check manuale end-to-end per UI-CREATE-FLOW-A, una volta implementata, con DB,
Redis, API e worker avviati secondo `docs/LOCAL_DEV_RUNBOOK.md`: aprire `/`,
cliccare “New evidence-based request”, creare o scegliere un progetto, caricare
un piccolo `.txt`, digitare un objective, cliccare “Create task”, confermare che
il browser atterri su `/tasks/<id>` mostrando “Not ready yet”, poi ricaricare
dopo l’esecuzione del worker e confermare che lo status diventi “Answer
available” oppure “Publication held”.

---

## Sintesi dell’output atteso

1. File letti: questa fase ha revisionato `README.md`, `PROJECT_STATE.md`,
   `docs/LOCAL_DEV_RUNBOOK.md`, `PHASE_UI_PRE.md`, le pagine web
   `apps/web/app/page.tsx`, `apps/web/app/tasks/page.tsx`,
   `apps/web/app/tasks/[taskId]/page.tsx`,
   `apps/web/app/tasks/[taskId]/report/page.tsx`,
   `apps/web/app/diagnostic/page.tsx`, `apps/web/app/layout.tsx`, le librerie
   web `apps/web/lib/api.ts`, `reportTypes.ts`, `reportFormatting.ts`, i
   componenti web (`TaskSummaryView`, `PublicationPanel`, `GatePanel`,
   `AxisSummaryCards`, `MockIndicatorsPanel`, `LimitationsPanel`,
   `RawJsonCollapsible`, `ReportStatusBadge`), i test e fixture web, e i
   riferimenti di contratto backend `apps/api/app/main.py`,
   `routes/anti_hallucination_report.py`, `routes/projects.py`,
   `routes/documents.py`, `routes/tasks.py`.

2. File creati/modificati: `PHASE_UI_CREATE_FLOW_PRE.md` creato. Nessun altro
   file modificato.

3. User journey proposto: home → “New evidence-based request” →
   `/requests/new` (seleziona/crea progetto → aggiungi fonti → scrivi richiesta
   → crea task) → vero `POST /api/v1/tasks` → naviga a `/tasks/<taskId>`
   (processing/summary) → technical report opzionale.

4. Capacità backend trovate: `POST/GET /api/v1/projects`, `GET
   /api/v1/projects/{id}`, `POST/GET /api/v1/projects/{id}/documents`,
   `POST /api/v1/tasks` (con supporto `Idempotency-Key` e validazione
   documenti), `GET /api/v1/tasks/{id}`, e `GET
   /api/v1/tasks/{task_id}/anti-hallucination-report` — sufficienti per un
   vero start flow browser-driven.

5. Gap o incertezze: nessuna cancellazione progetto/documento browser-facing;
   un task richiede almeno un documento per produrre una risposta utile; CORS
   tra browser e `localhost:8000` deve essere verificato dalla prossima fase,
   con una BFF route come fallback; la prossima fase deve riverificare tutti i
   contratti contro il proprio HEAD.

6. Prossima fase di codice raccomandata: UI-CREATE-FLOW-A — implementare
   creazione task reale dal browser.

7. Comandi di validazione eseguiti: `git diff --check`, `git diff --stat`,
   `git status -sb` (fase documentation-only).

8. Risultati validazione: l’unica modifica è il nuovo
   `PHASE_UI_CREATE_FLOW_PRE.md`; nessun file app, backend, worker, schema,
   Docker o script toccato.

9. Commit message suggerito: `Document product start flow`.
