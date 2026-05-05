# PHASE_8_5_PLAN — Evidence-First MVP-0

**Piano Fase 8.5: lifecycle pubblicazione, source loss propagation, retention dry-run.**

- Stato: **pianificazione, non implementato**.
- Base commit: `a2739cd50b5d8581f4a2d3e7c0daa4e8324e4aad` ("Update root tests documentation").
- Nessuna parte di Fase 8.5 è già scritta.
- Le migration `0001_foundation.sql`, `0002_storage.sql`, `0003_documents.sql`, `0004_claim_ledger.sql`, `0005_answers_gate.sql` sono **immutabili**.
- Questo documento è un piano tecnico. Non contiene SQL definitivo. Tutti i nomi di tabelle, colonne, vincoli, endpoint, eventi e servizi che non esistono già nel repo sono marcati come **proposti per 0006**.

---

## 1. Obiettivo Fase 8.5

La Fase 8.5 introduce il primo livello di **lifecycle delle pubblicazioni** e di **propagazione della perdita di fonte**, preservando integralmente le proprietà raggiunte in 8.4:

- evidence-first ed evidence-gated;
- append-only su tutte le tabelle che ne dispongono già (`audit_records`, `evidence_spans`, `claim_ledger_entries`, `final_answer_spans`, `final_gate_reports`) e su tutte le nuove tabelle di lifecycle/source loss;
- audit chain hash-linked verificabile end-to-end via `verify_audit_chain` / `verify_task_audit_chain`;
- idempotenza completa sotto redelivery, sia a livello consumer (`event_processing_records`) sia a livello dominio (UNIQUE constraints applicativi);
- pipeline mock-driven / deterministica, **nessun provider AI reale**, **costo API = 0**;
- closed corpus only, nessun retrieval esterno.

Gli obiettivi concreti della Fase 8.5 sono:

1. tracciare in modo append-only gli eventi lifecycle delle `published_answers` (proposti: `published`, `withdrawal_requested`, `withdrawn`, `superseded`);
2. gestire la **withdrawal asincrona** di un `published_answers` tramite richiesta API che pubblica un evento, processato da un consumer dedicato che è la sola entità autorizzata a mutare i campi lifecycle di `published_answers`;
3. pianificare il **supersede** come effetto di una pubblicazione futura (non endpoint manuale in 8.5);
4. registrare in modo append-only i `source_loss_events`, con granularità canonica `evidence_span_id`;
5. propagare la source loss al **Claim Ledger** in modo append-only, creando una nuova `claim_ledger_entries` v(N+1) con `state='unverifiable'`, `transition_reason='source_lost'` e una `claim_lineage` con `relation_kind='supersedes'`;
6. identificare le `published_answers` **attive** (`status='published'`) impattate da una source loss e registrarle in `source_loss_propagation_records`, **senza** ritirarle automaticamente;
7. produrre solo **retention dry-run reporting** (nessun cleanup distruttivo).

---

## 2. Non-obiettivi Fase 8.5

Esplicitamente fuori scope:

- nessun provider AI reale;
- nessun riferimento operativo a OpenAI, Anthropic, Google o provider equivalenti;
- nessun web retrieval, nessun Source Fetcher web;
- nessun **Verified Web Mode**, nessun **Hybrid Mode**;
- nessun renderer / export Markdown / HTML / PDF / DOCX / JSON-LD;
- nessun cleanup distruttivo: blob orfani, retention reale, export jobs, cancellazioni — tutto rinviato a `0007_evaluation_retention.sql` o oltre;
- nessun nuovo stato `task_masters.status`;
- nessun trigger DB di propagazione `source_loss_events → claim_ledger_entries` né `lifecycle → published_answers`;
- nessuna modifica alle migration `0001`–`0005`;
- nessun endpoint manuale di supersede;
- nessun consensus engine, nessun contradiction detector avanzato, nessun source quality evaluator, nessun critical reviewer;
- nessun OCR, nessun parsing PDF, nessun vector store cloud, nessun supporto S3 / GCS / Azure operativo;
- nessuna estensione di `verification_records.check_kind`;
- nessuna estensione di `claim_lineage.relation_kind`;
- nessun nuovo `ErrorCode` salvo necessità documentata.

---

## 3. Vincoli invarianti da 8.4

I seguenti vincoli, già attivi in 8.4, restano invarianti per tutta la Fase 8.5:

- `PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0`;
- closed corpus only;
- append-only enforced via trigger su `audit_records`, `evidence_spans`, `claim_ledger_entries`, `final_answer_spans`, `final_gate_reports`;
- nessuna `Engine.execute()`: tutti gli accessi DB applicativi usano `Connection` esplicito (`engine.connect()` o `engine.begin()`);
- `verify_task_audit_chain(conn, task_id=<task_id>)` accetta una `Connection`, non un `Engine`;
- i test API (`apps/api/tests/`) **non importano** dal worker (sotto `apps/api` il package `app` risolve all'API);
- i test API seedano direttamente il DB usando le fixture esistenti;
- i test worker (`apps/worker/tests/`) possono importare `app.consumers.*` e `app.services.*`;
- audit chain hash-linked, normalizzazione del payload via `_normalize_payload` in `evidencefirst_shared.db.audit`;
- claim ledger append-only stretto, supersede esclusivamente via `claim_lineage.relation_kind='supersedes'` (mai colonna `superseded_by_id` su `claim_ledger_entries`, che non esiste e non sarà introdotta);
- `final_answer_spans` e `final_gate_reports` append-only;
- `published_answers.status` ammette già `published`, `withdrawn`, `superseded` (definito in 0005). I campi `withdrawn_at`, `superseded_at`, `superseded_by_id` esistono già;
- `task_masters.status` **non** verrà esteso. Il codominio resta quello fissato in 0005: `created, ingesting, analyzing, verifying, compiling, published, blocked, failed, cancelled, archived, analyzed_partial`.

---

## 4. Stato 8.4 su cui 8.5 costruisce

Quanto già implementato in 8.4 e su cui la Fase 8.5 si appoggia senza modifiche:

- compiler mock-driven (`apps/worker/app/services/compiler.py`, `COMPILER_NAME='mvp0_compiler_v1'`, `COMPILER_VERSION='0.1.0'`);
- final answer gate mock-driven (`apps/worker/app/services/final_answer_gate.py`, `GATE_NAME='mvp0_gate_v1'`, `GATE_VERSION='0.1.0'`);
- `published_answers` v1 prodotti dal gate solo nel branch approved, con `status='published'`;
- endpoint API answers read-only (`/api/v1/tasks/{id}/draft`, `/api/v1/tasks/{id}/final-gate-report`, `/api/v1/tasks/{id}/published-answer`, `/api/v1/published-answers/{id}`);
- pipeline worker single-consumer 8.4 in `apps/worker/app/consumers/task_created.py`, con guardia finale `WORKER_PIPELINE_INCOMPLETE`;
- audit event `task.publication_held` come **evento audit-only** (mai status DB);
- nessuna pipeline lifecycle implementata oggi: né withdraw, né supersede, né source loss, né propagator;
- `source_loss_events` non esiste in DB;
- `published_answer_lifecycle_events` non esiste in DB;
- `lc_block_delete_if_published` esiste già (installato in 0005) e protegge esclusivamente `published_answers.status='published'`. Una `published_answers` con `status='withdrawn'` o `status='superseded'` **non blocca** il DELETE del relativo `logical_claims` tramite quel trigger. Questo comportamento è confermato come decisione consapevole per la Fase 8.5.

---

## 5. Schema previsto per `migrations/0006_lifecycle.sql`

Questa sezione è piano architetturale, non SQL definitivo. Tutti i nomi sono **proposti per 0006** se non già esistenti nel repo. La forma SQL precisa, gli indici esatti e i nomi finali dei vincoli verranno decisi nel Blocco 1 della rollout, leggendo il repo aggiornato e seguendo le convenzioni di naming già adottate in 0001–0005 (es. `*_uq` per UNIQUE, `*_consistency` per FK composite, `*_no_*` per CHECK negativi).

### 5.1 `published_answer_lifecycle_events` (proposta per 0006)

**Scopo.** Registrare in modo append-only gli eventi di lifecycle di una `published_answers`. È l'unico storico autoritativo del ciclo di vita della pubblicazione: gli aggiornamenti dei campi lifecycle su `published_answers` (status, withdrawn_at, superseded_at, superseded_by_id) sono permessi al consumer lifecycle, ma la **storia** deve essere ricostruibile esclusivamente da questa tabella.

**Colonne proposte.**

- `id UUID PK DEFAULT app_new_uuid()`
- `published_answer_id UUID NOT NULL`
- `task_id UUID NOT NULL`
- `event_type TEXT NOT NULL`
- `event_reason TEXT NOT NULL`
- `event_payload JSONB NOT NULL DEFAULT '{}'::jsonb`
- `requested_by UUID NULL` (riferimento opzionale a `users(id)` con `ON DELETE RESTRICT`; nullable per richieste system/job)
- `idempotency_key TEXT NOT NULL`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

**Event type proposti (CHECK).**

- `published`
- `withdrawal_requested`
- `withdrawn`
- `superseded`

**Vincoli proposti.**

- `CHECK (event_type IN ('published','withdrawal_requested','withdrawn','superseded'))`;
- `UNIQUE (published_answer_id, event_type, idempotency_key)` per idempotenza fine sotto redelivery;
- FK composita `(published_answer_id, task_id) REFERENCES published_answers (id, task_id)` usando `published_answers_id_task_uq` già presente in 0005. Questo blinda a DB la coerenza tra `task_id` dell'evento lifecycle e `task_id` della pubblicazione;
- trigger `published_answer_lifecycle_events_append_only` basato sul comune `reject_modify_append_only`. Una volta inserito, l'evento non si modifica e non si cancella.

**Indici proposti.**

- `(published_answer_id, created_at)` per la lettura cronologica;
- `(task_id, created_at)` per la query "qual è la storia lifecycle di tutti i published_answers di questo task?".

**Backfill `published`.** La Fase 8.4 inserisce `published_answers` direttamente dal gate, senza passare per un evento `published` in `published_answer_lifecycle_events` (che oggi non esiste). Decisione di piano per 0006: **nessun backfill automatico dentro la migration** per le pubblicazioni create in 8.4. Eventuale backfill esplicito è ammesso solo come script opzionale di dev/test, non come parte della migration. Per le pubblicazioni create dopo l'introduzione della Fase 8.5, l'evento lifecycle `published` è un **requisito runtime della pipeline competente**. Se il Blocco 1 è solo DB/schema, l'obbligo runtime viene implementato nel blocco worker/API appropriato. Il piano non considera `published` opzionale per le nuove pubblicazioni post-8.5.

### 5.2 `source_loss_events` (proposta per 0006)

**Scopo.** Registrare in modo append-only il fatto storico che un `evidence_span` ha perso validità o accessibilità. È la base su cui la propagazione applicativa lavora.

**Granularità canonica.** `evidence_span_id`. I riferimenti `document_chunk_id`, `document_version_id`, `document_id` sono **reporting context** opzionali e **non** costituiscono la base della propagazione, che parte sempre da `evidence_span_id`.

**Colonne proposte.**

- `id UUID PK DEFAULT app_new_uuid()`
- `tenant_id UUID NOT NULL` (FK a `tenants(id)` ON DELETE RESTRICT)
- `project_id UUID NULL` (FK a `projects(id)` ON DELETE RESTRICT)
- `task_id UUID NULL` (FK a `task_masters(id)` ON DELETE RESTRICT)
- `evidence_span_id UUID NOT NULL` (FK a `evidence_spans(id)` ON DELETE RESTRICT)
- `document_chunk_id UUID NULL` (reporting; FK opzionale a `document_chunks(id)` con `ON DELETE RESTRICT`)
- `document_version_id UUID NULL` (reporting; FK opzionale a `document_versions(id)` con `ON DELETE RESTRICT`)
- `document_id UUID NULL` (reporting; FK opzionale a `uploaded_documents(id)` con `ON DELETE RESTRICT`)
- `loss_kind TEXT NOT NULL`
- `loss_reason TEXT NOT NULL`
- `detected_by TEXT NOT NULL`
- `event_payload JSONB NOT NULL DEFAULT '{}'::jsonb`
- `idempotency_key TEXT NOT NULL`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

**`loss_kind` proposti (CHECK).**

- `source_deleted`
- `source_access_lost`
- `quote_mismatch`
- `document_replaced`
- `policy_retraction`

**Vincoli proposti.**

- `CHECK (loss_kind IN ('source_deleted','source_access_lost','quote_mismatch','document_replaced','policy_retraction'))`;
- `UNIQUE (evidence_span_id, loss_kind, idempotency_key)` per idempotenza sotto redelivery dello stesso evento di sorgente;
- trigger `source_loss_events_append_only` basato su `reject_modify_append_only`.

**Indici proposti.**

- `(evidence_span_id)` come accesso primario per il propagator;
- `(task_id, created_at)` per dashboard di task;
- `(project_id, created_at)` per dashboard di progetto.

### 5.3 `source_loss_propagation_records` (proposta per 0006)

**Scopo.** Tracciare cosa è stato propagato a partire da uno specifico `source_loss_events`. Permette al consumer di essere idempotente e ricostruisce l'effetto della propagazione separatamente dall'evento di partenza.

**Colonne proposte.**

- `id UUID PK DEFAULT app_new_uuid()`
- `source_loss_event_id UUID NOT NULL` (FK a `source_loss_events(id)` ON DELETE RESTRICT)
- `claim_logical_id UUID NULL` (FK a `logical_claims(id)` ON DELETE RESTRICT)
- `old_claim_ledger_entry_id UUID NULL` (FK a `claim_ledger_entries(id)` ON DELETE RESTRICT)
- `new_claim_ledger_entry_id UUID NULL` (FK a `claim_ledger_entries(id)` ON DELETE RESTRICT)
- `published_answer_id UUID NULL` (FK a `published_answers(id)` ON DELETE RESTRICT)
- `propagation_kind TEXT NOT NULL`
- `status TEXT NOT NULL`
- `details JSONB NOT NULL DEFAULT '{}'::jsonb`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

**`propagation_kind` proposti (CHECK).**

- `claim_marked_unverifiable`
- `published_answer_impacted`
- `no_claims_impacted`
- `no_active_published_answers_impacted`

**`status` proposti (CHECK).**

- `recorded`
- `skipped`
- `failed`

**Preferenza append-only stretto.** La tabella è **append-only** via trigger comune. Lo `status='failed'` viene registrato come riga nuova; il propagator non muta righe esistenti. Le retry generano nuove righe con `idempotency_key` distinto codificato in `details`.

**Vincoli proposti.**

- `CHECK (propagation_kind IN ('claim_marked_unverifiable','published_answer_impacted','no_claims_impacted','no_active_published_answers_impacted'))`;
- `CHECK (status IN ('recorded','skipped','failed'))`;
- idempotenza proposta tramite più **unique partial indexes** leggibili, da definire nel Blocco 1, invece di una pseudo-constraint con espressioni funzionali;
- pattern raccomandato:
  - unique partial index per `claim_marked_unverifiable` con `claim_logical_id IS NOT NULL`;
  - unique partial index per `published_answer_impacted` con `published_answer_id IS NOT NULL`;
  - unique partial index per `no_claims_impacted` basato su `source_loss_event_id` e `propagation_kind`;
  - unique partial index per `no_active_published_answers_impacted` basato su `source_loss_event_id` e `propagation_kind`;
- il Blocco 1 dovrà scegliere nomi espliciti per questi indici, senza presentarli come table `UNIQUE` constraints;
- trigger `source_loss_propagation_records_append_only`.

### 5.4 Indici utili (proposta per 0006)

In aggiunta agli indici già menzionati, sono proposti almeno:

- `source_loss_events(evidence_span_id)`;
- `source_loss_events(task_id)`;
- `source_loss_propagation_records(source_loss_event_id)`;
- `source_loss_propagation_records(published_answer_id) WHERE published_answer_id IS NOT NULL`;
- `published_answer_lifecycle_events(published_answer_id, created_at)`;
- `published_answer_lifecycle_events(task_id, created_at)`.

### 5.5 Cosa **non** entra in 0006

Esplicitamente fuori da `0006_lifecycle.sql`:

- nessun `ALTER TABLE task_masters` (lo status non si tocca);
- nessuna modifica a `lc_block_delete_if_published` (vedi sezione 5.6);
- nessun trigger DB di propagazione `source_loss_events → claim_ledger_entries` o `lifecycle → published_answers`. La propagazione è solo applicativa, eseguita dal worker;
- nessuna estensione di `verification_records.check_kind`;
- nessuna estensione di `claim_lineage.relation_kind`;
- nessun cleanup blob, nessun retention job;
- nessuna estensione del codominio di `published_answers.status` (basta l'enum esistente).

### 5.6 Trigger `lc_block_delete_if_published` — non si modifica

**Decisione consapevole.** `lc_block_delete_if_published` resta com'è. Continua a proteggere `logical_claims` solo se esiste una `published_answers` collegata con `status='published'`. Una `published_answers` `withdrawn` o `superseded` **non blocca** il DELETE del `logical_claims` tramite quel trigger.

Questa scelta è coerente con la semantica di withdrawal/supersede: una pubblicazione ritirata o sostituita non è più "viva" come fonte di verità autoritativa per l'utente finale. Le tutele storiche (audit chain, lifecycle events, ledger append-only, `source_loss_propagation_records`) restano sufficienti a ricostruire la storia. Il trigger non è il posto giusto per imporre conservazione storica: quel ruolo spetta al lifecycle log.

Il piano documenta esplicitamente questa decisione perché alterare `lc_block_delete_if_published` richiederebbe una migration aggiuntiva e cambierebbe la semantica di un trigger già committato.

---

## 6. Semantica lifecycle pubblicazione

**Principio.** `published_answers.status` resta la fonte dello stato lifecycle. `task_masters.status` resta invariato. La storia append-only del lifecycle vive in `published_answer_lifecycle_events`.

### 6.1 Transizione `published → withdrawn`

1. Il client invia `POST /api/v1/published-answers/{id}/withdraw` (vedi sezione 8). L'API valida l'esistenza e lo stato corrente del `published_answers`, ma **non lo modifica direttamente** nel request path.
2. L'API insert idempotente in `published_answer_lifecycle_events` un evento `withdrawal_requested` (con `idempotency_key` derivato dall'header `Idempotency-Key` se presente, altrimenti dal `published_answer_id` o da uno UUID di richiesta) e pubblica un evento di stream `published_answer.withdrawal_requested`.
3. L'API ritorna `202 Accepted` con un envelope JSON minimale (vedi sezione 8).
4. Il **consumer lifecycle** (vedi sezione 7) legge l'evento, esegue in transazione:
   - INSERT idempotente in `published_answer_lifecycle_events` di un evento `withdrawn` con `event_reason` derivato dalla richiesta;
   - UPDATE su `published_answers` impostando `status='withdrawn'` e `withdrawn_at=NOW()` se e solo se la riga è ancora in `status='published'` (guardia status-based);
   - emissione di un audit event `published_answer.withdrawn` su `chain_scope='task'` legato al `task_id` della pubblicazione;
   - opzionalmente, audit event `task.withdrawn` audit-only (analogo a `task.publication_held` in 8.4): non corrisponde ad alcuno status DB.
5. Se la riga era già `withdrawn` o `superseded`, il consumer logga `skipped_terminal` o equivalente e marca l'EPR come `succeeded` senza ulteriori effetti.

### 6.2 Transizione `published → superseded` (rinviata)

Il supersede **non è** un endpoint manuale in Fase 8.5. È pianificato come effetto automatico di una nuova pubblicazione `v(N+1)` per lo stesso task in fasi successive:

- al momento dell'INSERT di `published_answers` `v(N+1)` con `status='published'`, una pipeline lifecycle (futura, non parte di 8.5) marca `vN` come `superseded`, `superseded_at=NOW()`, `superseded_by_id=v(N+1).id`;
- emette un audit event `published_answer.superseded` e un evento lifecycle `superseded` su `vN`;
- opzionalmente, audit event `task.superseded` audit-only.

In Fase 8.5 il piano si limita a:

- garantire che lo schema 0006 supporti questa transizione futura (lifecycle event `superseded` ammesso nel CHECK; vincoli composite coerenti);
- **non** introdurre endpoint manuale di supersede;
- **non** scrivere codice di supersede automatico.

### 6.3 Cosa non viene cancellato

Withdrawal e supersede non cancellano e non mutano:

- `draft_final_answers`;
- `final_answer_spans`;
- `final_answer_span_claim_links`;
- `final_gate_reports`;
- `coverage_gap_statements`;
- `claim_ledger_entries` (resta append-only);
- `audit_records`;
- `published_answer_lifecycle_events` precedenti.

### 6.4 Note importanti

- `published_answers` **non** è append-only in 0005. I campi `status`, `withdrawn_at`, `superseded_at`, `superseded_by_id` esistono per essere mutati da pipeline lifecycle. L'append-only storico è realizzato dal log `published_answer_lifecycle_events`, non da un trigger su `published_answers`.
- Il **solo** scrittore autorizzato dei campi lifecycle di `published_answers` in Fase 8.5 è il consumer lifecycle. L'API web non li modifica nel request path. In Fase 8.4 il gate è il solo INSERT-er; in 8.5 il gate continua a essere il solo INSERT-er, e il consumer lifecycle è il solo UPDATE-r dei campi lifecycle.

---

## 7. Semantica source loss

**Principio.** La source loss è **perdita o invalidazione di un `evidence_span`**. La registrazione è append-only in `source_loss_events`. La propagazione è applicativa, mai trigger DB.

### 7.1 Pipeline propagator (proposta)

Quando il consumer lifecycle elabora un evento `source_loss.created` (oppure quando processa direttamente un nuovo `source_loss_events` via job di scan):

1. legge `source_loss_events` per `id`;
2. trova tutti i `claim_evidence_links` che referenziano `evidence_span_id`;
3. per ciascun `claim_logical_id` impattato:
   1. recupera la **latest** `claim_ledger_entries` per `claim_logical_id` (ordinamento `version_no DESC`);
   2. se la latest **non** è in `state='verified_fact'`, registra un `source_loss_propagation_records` con `propagation_kind='claim_marked_unverifiable'`, `status='skipped'`, motivo "latest not verified"; **non** crea una nuova entry;
   3. se la latest è `state='verified_fact'`, crea una nuova `claim_ledger_entries` `v(N+1)`:
      - `state='unverifiable'`;
      - `support_scope='unsupported'` (decisione conservativa: la fonte è persa, lo scope di supporto è coerentemente "unsupported");
      - `user_provided_dependency='unsupported'`;
      - `transition_reason='source_lost'`;
      - `payload` deve contenere il riferimento a `source_loss_event_id` e al `claim_ledger_entry_id` precedente;
      - INSERT idempotente via UNIQUE `(claim_logical_id, version_no)` di `claim_ledger_entries` con strategia di calcolo del prossimo `version_no` in transazione (vedi sezione 14 per il rischio concorrenza);
   4. INSERT in `claim_lineage` con `parent_entry_id=old_latest`, `child_entry_id=new_entry`, `relation_kind='supersedes'` (idempotente via UNIQUE `(parent_entry_id, child_entry_id, relation_kind)`);
   5. INSERT in `source_loss_propagation_records` con `propagation_kind='claim_marked_unverifiable'`, `status='recorded'`;
4. trova tutte le `published_answers` **attive** (`status='published'`) collegate ai claim impattati:
   - join `published_answers → draft_final_answers → final_answer_spans → final_answer_span_claim_links → claim_logical_id`;
   - per ogni `published_answers` impattata, INSERT in `source_loss_propagation_records` con `propagation_kind='published_answer_impacted'`, `status='recorded'`, `published_answer_id` valorizzato;
5. se nessun claim è impattato, INSERT singolo `propagation_kind='no_claims_impacted'`;
6. se nessuna pubblicazione attiva è impattata, INSERT singolo `propagation_kind='no_active_published_answers_impacted'`;
7. emette gli audit events corrispondenti su `chain_scope='task'` quando un `task_id` è risolvibile, o `chain_scope='project'`/`tenant` per source loss che non hanno un task chiaro.

### 7.2 Cosa il propagator **non** fa

- non scrive `verification_records` (la source loss non è un check di verifica e non rientra in `cve_lite`/`csv`/`nli`/`judge`);
- non estende `verification_records.check_kind`;
- non estende `claim_lineage.relation_kind` (usa `supersedes` esistente);
- non muta vecchie `claim_ledger_entries` (sono append-only);
- non muta `claim_evidence_links` esistenti né li cancella (resterebbero comunque coerenti con l'entry vecchia, che è ancora valida storicamente);
- non chiama automaticamente withdrawal sulle `published_answers` impattate. Questo è il significato di **cascade soft**: la perdita di fonte è registrata, propagata al ledger, e la lista delle pubblicazioni impattate è esposta. La decisione di ritirare è separata e attualmente manuale via `POST /api/v1/published-answers/{id}/withdraw`.

### 7.3 Cascade soft come scelta esplicita

In Fase 8.5 la pipeline source loss **non** ritira automaticamente le `published_answers` impattate. Ragioni:

- una source loss su un solo span può impattare published answer in cui ci sono altri span ancora verified-backed: il giudizio se ritirare l'intera answer è policy, non meccanico;
- un eventuale ritiro automatico sarebbe non reversibile in modo banale: la withdrawal è già operazione asincrona e auditata;
- il piano preferisce esporre la lista degli impatti via API (vedi sezione 8) e lasciare che la withdrawal sia eseguita coscientemente.

Una policy futura potrà decidere se la cascade hard (ritiro automatico) deve essere abilitata; in 8.5 resta soft.

---

## 8. Worker pipeline prevista

### 8.1 Nuovi moduli proposti

- `apps/worker/app/consumers/lifecycle_events.py` (nuovo consumer, separato da `task_created.py`);
- `apps/worker/app/services/published_answer_lifecycle.py` (servizio: applica withdrawal, emette eventi lifecycle e audit);
- `apps/worker/app/services/source_loss_propagator.py` (servizio: propaga source loss al ledger, registra impatti su published answers, emette audit).

I nomi sono **proposti**. Eventuali rinomi (per esempio `apps/worker/app/services/lifecycle.py` come singolo file con due funzioni separate) sono ammessi nel Blocco 2/3 della rollout, purché restino coerenti con la nomenclatura `services/*` e `consumers/*` già usata in 8.4.

### 8.2 Caratteristiche del nuovo consumer

- separato da `task_created.py` per evitare di mescolare il dominio "task analysis" con il dominio "lifecycle";
- usa `event_processing_records` con `consumer_name` distinto, proposto: `lifecycle_worker`;
- pattern FK-safe identico a quello in `task_created.py`: prima di chiamare `begin_processing` con `task_id != None`, verifica che la riga referente sia visibile;
- idempotente: ogni effetto è guardato da UNIQUE constraint applicativi (`published_answer_lifecycle_events_*`, `source_loss_propagation_records_*`) e da guardie status-based sugli UPDATE di `published_answers`;
- resume-safe: una redelivery di un evento `withdrawal_requested` non duplica eventi lifecycle né audit. Una redelivery di un evento `source_loss.created` non duplica righe in `source_loss_propagation_records`;
- non lascia stati intermedi incoerenti: se la sequenza "INSERT lifecycle event `withdrawn`" + "UPDATE `published_answers.status`" si interrompe a metà, una redelivery successiva applica la sequenza completa idempotentemente. Una guardia analoga a `WORKER_PIPELINE_INCOMPLETE` può essere aggiunta per detectare e rifiutare `mark_succeeded` in stati incoerenti.

### 8.3 Eventi stream proposti

- `published_answer.withdrawal_requested`;
- `source_loss.created`.

Altri eventi stream (`published_answer.superseded`, `source_loss.batch_scan_completed`) sono lasciati al futuro.

### 8.4 Eventi audit proposti

Nuovi `event_type` per `audit_records.event_type` (testuale, non enum):

- `published_answer.withdrawal_requested`;
- `published_answer.withdrawn`;
- `published_answer.superseded` (predisposto, emesso solo dalla pipeline futura di supersede);
- `task.withdrawn` audit-only (analogo a `task.publication_held` in 8.4: nessuno status DB associato);
- `task.superseded` audit-only (predisposto);
- `source_loss.recorded`;
- `source_loss.propagated_to_claim`;
- `source_loss.propagated_to_published_answer`;
- `source_loss.no_impacted_claims`.

Lo `event_type` su `audit_records` è una colonna `TEXT` libera (vedi 0001), quindi non serve estendere alcun enum. Le costanti vanno solo aggiunte come stringhe canoniche nel codice.

### 8.5 Distinzione fra le tre tabelle di tracciamento

- `event_processing_records` resta la tabella di idempotenza del **consumer**: un'unica riga per `(consumer_name, idempotency_key)`, mai duplicata;
- `audit_records` resta la **catena audit hash-linked** del dominio task/project/tenant/global;
- `published_answer_lifecycle_events` e `source_loss_events` sono lo **stato storico di dominio** delle pubblicazioni e delle perdite di fonte. Sono indipendenti dall'audit chain: la stessa transizione genera contemporaneamente una riga in `published_answer_lifecycle_events` e un evento in `audit_records`, senza che le due cose siano una funzione l'altra.

### 8.6 Eventuale impatto su `apps/worker/app/main.py`

Il `main.py` del worker dovrà registrare anche il nuovo consumer lifecycle. Decisione di piano: una sola istanza worker che gestisce **due** stream (`task.created` e i due nuovi stream), oppure due processi worker separati. Nel Blocco 3 della rollout si sceglierà l'approccio più semplice (ipotesi: singolo processo che esegue i consumer in serie nello stesso `xreadgroup` multi-stream, oppure processi separati orchestrati da Compose). Nessuna decisione vincolante in questo piano.

---

## 9. API previste

### 9.1 Endpoint write minimale: `POST /api/v1/published-answers/{published_answer_id}/withdraw`

**Comportamento previsto.**

- valida che `published_answers.id == published_answer_id` esista;
- se non esiste, ritorna `404` con `RESOURCE_NOT_FOUND` e `details.resource='published_answers'` (pattern già usato da `apps/api/app/routes/answers.py`);
- se la riga esiste ed è `status='withdrawn'`, ritorna `200` o `202` idempotente con corpo che indica che la withdrawal è già avvenuta (decisione raccomandata: `202 Accepted` per coerenza con la semantica asincrona);
- se la riga esiste ed è `status='superseded'`, decisione raccomandata: `409 RESOURCE_CONFLICT` con message "already superseded"; in alternativa `202 Accepted` no-op motivato. Da definire nel Blocco 4;
- se la riga è `status='published'`, l'API:
  - inserisce idempotente in `published_answer_lifecycle_events` un evento `withdrawal_requested` (UNIQUE su `(published_answer_id, event_type, idempotency_key)`);
  - pubblica un evento `published_answer.withdrawal_requested` su Redis Stream;
  - emette un audit event `published_answer.withdrawal_requested`;
  - **non modifica** `published_answers` direttamente (il consumer lifecycle se ne occupa);
- ritorna `202 Accepted` con envelope JSON minimale, ad esempio:

  ```json
  {
    "accepted": true,
    "published_answer_id": "<published_answer_id>",
    "task_id": "<task_id>",
    "lifecycle_event_id": "<lifecycle_event_id>",
    "audit_record_id": "<audit_record_id>"
  }
  ```

**Header `Idempotency-Key`.** Il client può fornirlo. Se assente, l'API genera un `idempotency_key` derivato (per esempio dal `published_answer_id` + `withdraw` + UUID di richiesta). La UNIQUE su `published_answer_lifecycle_events_*` garantisce che una doppia richiesta con lo stesso `idempotency_key` non duplichi eventi.

### 9.2 Endpoint read-only proposti

- `GET /api/v1/published-answers/{id}/lifecycle-events` — lista cronologica degli eventi lifecycle di una pubblicazione;
- `GET /api/v1/source-loss-events` — lista paginata (con cursor analogo a `/projects`) dei `source_loss_events`, filtrabile per `task_id`, `project_id`, `evidence_span_id`;
- `GET /api/v1/source-loss-events/{id}` — singolo evento;
- `GET /api/v1/source-loss-events/{id}/propagation` — record di propagazione collegati;
- `GET /api/v1/published-answers/{id}/source-loss-impact` — proposta opzionale: lista delle source loss che hanno impattato la pubblicazione (join su `source_loss_propagation_records`).

### 9.3 Errori normalizzati

Riusare `ErrorCode` esistenti:

- `RESOURCE_NOT_FOUND` con `details.resource` distintivo (`published_answers`, `source_loss_events`, `published_answer_lifecycle_events`);
- `RESOURCE_CONFLICT` per la transizione invalida (es. withdraw su `superseded`);
- `VALIDATION_ERROR` per input malformati;
- `IDEMPOTENCY_REPLAY_MISMATCH` solo se introduciamo conflict semantico su `Idempotency-Key` riutilizzato con corpo diverso. Decisione di piano: in 8.5 evitare di introdurre `IDEMPOTENCY_REPLAY_MISMATCH` se non c'è un test chiaro che lo richieda.

**Nessun nuovo `ErrorCode`.** Se durante l'implementazione del Blocco 4 emerge il bisogno di un codice nuovo, va prima discusso. Non aggiungerlo silenziosamente.

### 9.4 Schemi `evidencefirst_shared.schemas`

Nuovi `BaseModel` proposti per Pydantic, da aggiungere in `packages/shared/evidencefirst_shared/schemas.py`:

- `PublishedAnswerLifecycleEventRead`;
- `WithdrawRequest` (eventuale corpo opzionale per `event_reason`);
- `WithdrawResponse`;
- `SourceLossEventRead`;
- `SourceLossPropagationRecordRead`;
- `PublishedAnswerSourceLossImpactRead` (aggregato).

---

## 10. Test plan

### 10.1 DB tests root (`tests/`)

Nuovo file futuro: `tests/test_lifecycle_constraints.py`.

Coperture:

- `published_answer_lifecycle_events` append-only: UPDATE e DELETE rifiutati dal trigger;
- `published_answer_lifecycle_events`: UNIQUE `(published_answer_id, event_type, idempotency_key)` rifiuta duplicati;
- `published_answer_lifecycle_events`: FK composita `(published_answer_id, task_id) → published_answers(id, task_id)` rifiuta `task_id` mismatch;
- `published_answer_lifecycle_events`: CHECK su `event_type`;
- `source_loss_events` append-only;
- `source_loss_events`: FK su `evidence_span_id`;
- `source_loss_events`: CHECK su `loss_kind`;
- `source_loss_events`: UNIQUE idempotente;
- `source_loss_propagation_records` append-only;
- `source_loss_propagation_records`: UNIQUE idempotente (forma definitiva nel Blocco 1);
- presenza di tutti i constraint nominati in 0006 via `pg_constraint`;
- `task_masters.status` continua a rifiutare `withdrawn` e `superseded` (regressione: garantisce che 0006 non abbia esteso lo status);
- `lc_block_delete_if_published` continua a comportarsi esattamente come in 8.4 (non bloccare withdrawn/superseded).

Convenzioni: rerun-safety con `uuid.uuid4()` per `idempotency_key`, `loss_reason`, `event_reason`, ecc.

### 10.2 Worker tests (`apps/worker/tests/`)

Nuovi file futuri:

- `apps/worker/tests/test_published_answer_lifecycle.py`;
- `apps/worker/tests/test_source_loss_propagator.py`.

Coperture lifecycle:

- evento `withdrawal_requested` produce `published_answers.status='withdrawn'`, `withdrawn_at` valorizzato, evento lifecycle `withdrawn` inserito;
- doppio delivery dello stesso evento non duplica righe in `published_answer_lifecycle_events` né muta nuovamente `published_answers`;
- richiesta withdrawal su una `published_answers` già `withdrawn` esita in `skipped_terminal` o equivalente, senza duplicare;
- richiesta withdrawal su `superseded` esita in stato coerente (`skipped` o `failed` registrato in propagation_records-equivalent o solo nei log/audit, da definire nel Blocco 3);
- audit chain del task resta verificabile via `verify_task_audit_chain`.

Coperture source loss:

- `source_loss_events` su uno `evidence_span_id` collegato a un claim con latest `verified_fact`:
  - viene inserita `claim_ledger_entries v(N+1)` con `state='unverifiable'`, `transition_reason='source_lost'`;
  - vecchia entry resta intoccata (verifica esplicita su `claim_ledger_entries.id` precedente);
  - `claim_lineage` con `relation_kind='supersedes'` viene inserito;
  - `source_loss_propagation_records` registra `claim_marked_unverifiable` e (se ci sono pubblicazioni attive) `published_answer_impacted`;
- redelivery dello stesso `source_loss_events`: nessun duplicato;
- source loss su uno span senza claim collegati: registra `no_claims_impacted`;
- source loss su uno span collegato a claim ma con latest non `verified_fact`: registra `skipped`;
- nessun trigger DB viene invocato per la propagazione (verifica indiretta: le righe vengono inserite solo dal codice del propagator, mai da una mutazione su `source_loss_events`);
- `make test-worker` resta verde anche eseguendo solo i test 8.4 esistenti (regressione).

### 10.3 API tests (`apps/api/tests/`)

Nuovi file futuri:

- `apps/api/tests/test_published_answer_lifecycle_endpoints.py`;
- `apps/api/tests/test_source_loss_endpoints.py`.

Coperture:

- `POST /api/v1/published-answers/{id}/withdraw` ritorna `202` su `published`;
- una seconda chiamata con stesso `Idempotency-Key` ritorna `202` idempotente, senza inserire un secondo `withdrawal_requested` in `published_answer_lifecycle_events`;
- `POST /api/v1/published-answers/{id}/withdraw` su id inesistente ritorna `404` con envelope `{"error": {"code": "RESOURCE_NOT_FOUND", "details": {"resource": "published_answers"}}}`;
- `POST /api/v1/published-answers/{id}/withdraw` su `superseded` (caso seedato direttamente in DB) ritorna `409` o `202` no-op secondo decisione del Blocco 4, con assertion sull'envelope corrispondente;
- `GET /api/v1/published-answers/{id}/lifecycle-events` ritorna la lista ordinata per `created_at`;
- `GET /api/v1/source-loss-events` lista, filtra, pagina;
- `GET /api/v1/source-loss-events/{id}` 200/404;
- `GET /api/v1/source-loss-events/{id}/propagation` 200/404;
- envelope errore letto da `body["error"]["code"]` e `body["error"]["details"]`, mai dal top-level;
- nessun import dal worker;
- seeding diretto in DB con il pattern già usato in `apps/api/tests/test_answers_endpoints.py`;
- regressione: gli endpoint answers esistenti (8.4) restano integri.

### 10.4 Regression tests

L'intera matrice deve restare verde:

- `make test-db`;
- `make test-shared`;
- `make test-api`;
- `make test-worker`;
- `make test-web`;
- `make test`.

Nessun test esistente di 8.4 deve essere modificato per fare passare 8.5.

---

## 11. Rollout plan in blocchi

Vincolo: massimo 3 file integrali per blocco, coerentemente con la regola operativa adottata in 8.4. Ogni blocco si chiude con verifica statica, esecuzione test pertinenti, `git diff --check`, commit dedicato e push.

### Blocco 1 — DB / shared

File:

- `migrations/0006_lifecycle.sql`;
- `packages/shared/evidencefirst_shared/schemas.py` (estensione con i nuovi `Read` schemas);
- `tests/test_lifecycle_constraints.py`.

Test: `make test-db`, `make test-shared`.
Commit message: `Add phase 8.5 lifecycle migration and shared schemas`.

### Blocco 2 — Worker services

File:

- `apps/worker/app/services/published_answer_lifecycle.py`;
- `apps/worker/app/services/source_loss_propagator.py`;
- `apps/worker/tests/test_source_loss_propagator.py`.

Test: `make test-worker` (subset).
Commit message: `Add phase 8.5 worker lifecycle and source loss services`.

### Blocco 3 — Worker consumer

File:

- `apps/worker/app/consumers/lifecycle_events.py`;
- `apps/worker/app/main.py` (modifica: registrazione del nuovo consumer / nuovi stream);
- `apps/worker/tests/test_published_answer_lifecycle.py`.

Test: `make test-worker`.
Commit message: `Add phase 8.5 lifecycle consumer`.

### Blocco 4 — API

File:

- `apps/api/app/routes/published_answer_lifecycle.py` (nuovo router) **oppure** estensione di `apps/api/app/routes/answers.py` se la dimensione del file resta gestibile;
- `apps/api/app/main.py` (modifica: include del nuovo router);
- `apps/api/tests/test_published_answer_lifecycle_endpoints.py`.

Test: `make test-api`.
Commit message: `Add phase 8.5 lifecycle API endpoints`.

Eventuale Blocco 4-bis dedicato a `apps/api/tests/test_source_loss_endpoints.py` se la dimensione del Blocco 4 lo richiede.

### Blocco 5 — Documentazione

File:

- `README.md`;
- `docs/migration_plan.md`;
- `PROJECT_STATE.md`.

Test: `make test` completo come gate finale.
Commit message: `Update phase 8.5 documentation`.

### Blocco 6 — chiusura (eventuale)

File: `tests/README.md` se la suite root cresce con nuovi file e merita un aggiornamento puntuale; `PHASE_8_6_PLAN.md` come output della fase successiva.

Commit message: `Update root tests documentation` o `Add phase 8.6 plan`.

---

## 12. Smoke test previsto 8.5

Smoke test futuro, eseguibile a mano dopo che tutti i blocchi sono stati committati:

```bash
# 1) Crea task approved fino a published (smoke 8.4 invariato).
PID=$(curl -s -X POST localhost:8000/api/v1/projects \
  -H 'content-type: application/json' \
  -d '{"name":"smoke-85-demo"}' | jq -r .id)

DID=$(curl -s -X POST "localhost:8000/api/v1/projects/$PID/documents" \
  -F "file=@evaluation/fixtures/closed_corpus_basic/doc_en.txt;type=text/plain" \
  | jq -r .id)

TID=$(curl -s -X POST localhost:8000/api/v1/tasks \
  -H 'content-type: application/json' \
  -d "{\"project_id\":\"$PID\",\"objective\":\"smoke 8.5\",\"mode\":\"closed_corpus\",\"document_ids\":[\"$DID\"]}" \
  | jq -r .id)

# Polling fino a status='published' (come in 8.4).
while true; do
  S=$(curl -s "localhost:8000/api/v1/tasks/$TID" | jq -r .status)
  [ "$S" = "published" ] && break
  sleep 1
done

# 2) Recupera il published answer.
PAID=$(curl -s "localhost:8000/api/v1/tasks/$TID/published-answer" | jq -r .id)

# 3) Withdrawal asincrona.
curl -s -X POST "localhost:8000/api/v1/published-answers/$PAID/withdraw" \
  -H 'Idempotency-Key: smoke-85-withdraw-1' \
  -d '{"event_reason":"smoke test"}'

# 4) Polling: dopo che il consumer lifecycle ha processato,
#    GET published-answer mostra status='withdrawn'.
while true; do
  S=$(curl -s "localhost:8000/api/v1/published-answers/$PAID" | jq -r .status)
  [ "$S" = "withdrawn" ] && break
  sleep 1
done

# 5) Lifecycle events visibili e cronologici.
curl -s "localhost:8000/api/v1/published-answers/$PAID/lifecycle-events" | jq

# 6) Registra un source_loss_event su un evidence_span del task
#    (la modalità di registrazione dipende dall'API/job che 8.5 esporrà per i source loss;
#     in alternativa via SQL diretto nel job di scan o in un endpoint admin futuro).

# 7) Polling: dopo che il consumer lifecycle ha processato il source loss,
#    GET claims/{logical_id}/history mostra una nuova ledger entry v(N+1)
#    con state='unverifiable' e transition_reason='source_lost'.
# (verifica via /api/v1/claims/{logical_id}/history come in 8.3)

# 8) Verifica audit chain del task.
curl -s "localhost:8000/api/v1/tasks/$TID/audit?limit=500" | jq '.items[].event_type'

# La sequenza finale, oltre ai 13 eventi 8.4 approved, deve includere
# almeno: published_answer.withdrawal_requested, published_answer.withdrawn,
# source_loss.recorded, source_loss.propagated_to_claim,
# source_loss.propagated_to_published_answer.
```

---

## 13. Decisioni congelate per implementazione `0006_lifecycle.sql`

Le sette decisioni vincolanti seguenti sono **già prese**. Il Blocco 1 della rollout le rispetta letteralmente.

1. `task_masters.status` **non** viene esteso in 8.5. Niente `withdrawn`, `superseded`, `publication_held` come status DB. Eventuali `task.withdrawn` / `task.superseded` sono **audit-only**, analoghi a `task.publication_held` in 8.4. Il lifecycle vive esclusivamente su `published_answers.status` e su `published_answer_lifecycle_events`.

2. Source loss → Claim Ledger:
   - propagazione **append-only**;
   - inserisce una nuova `claim_ledger_entries v(N+1)`;
   - `state='unverifiable'`;
   - `transition_reason='source_lost'`;
   - `claim_lineage.relation_kind='supersedes'` come unica forma di collegamento al precedente;
   - **non** estende `claim_lineage.relation_kind`;
   - **non** estende `verification_records.check_kind`;
   - **non** inserisce `verification_records` per source loss.

3. `source_loss_events` è **append-only**. Granularità canonica: `evidence_span_id`. Eventuali riferimenti a `document_id`, `document_version_id`, `document_chunk_id` sono campi reporting opzionali; la propagazione effettiva parte sempre da `evidence_span_id`.

4. `lc_block_delete_if_published` **non** si modifica in 8.5. La semantica resta: blocca DELETE su `logical_claims` solo se esiste una `published_answers` collegata con `status='published'`. Withdrawn/superseded non bloccano. Decisione consapevole, non omissione.

5. La propagazione source loss **non** è trigger DB. È pipeline worker / event-driven, eseguita dal nuovo consumer lifecycle (separato da `task_created.py`), riusando i pattern FK-safe, `event_processing_records` e idempotency già adottati dal worker 8.4.

6. API write minimale ammessa solo come richiesta asincrona:
   - `POST /api/v1/published-answers/{id}/withdraw`;
   - pubblica un evento, ritorna `202`;
   - **non** modifica direttamente `published_answers` nel request path;
   - supersede **non** è endpoint manuale in 8.5; sarà effetto di nuova pubblicazione/versione futura.

7. Retention in 8.5:
   - solo piano e dry-run reporting;
   - nessun cleanup distruttivo;
   - cleanup effettivo rinviato a `0007_evaluation_retention.sql`.

---

## 14. Rischi aperti

Rischi reali individuati leggendo il repo, da gestire durante l'implementazione:

1. **`published_answers` non append-only.** Mitigato dalla coppia "lifecycle log append-only + scrittore unico". Lo scrittore unico dei campi lifecycle è il consumer lifecycle. Il log `published_answer_lifecycle_events` è il vero storico autoritativo. Test di regressione devono verificare che nessun altro codice scrive su `published_answers.status`.

2. **Cascade automatica troppo aggressiva.** Mitigato adottando cascade soft (sezione 7.3): la source loss registra l'impatto, non ritira. La withdrawal resta operazione esplicita.

3. **Concorrenza su `claim_ledger_entries.version_no`.** Due propagator contemporanei sullo stesso `claim_logical_id` potrebbero entrambi calcolare `v(N+1)` e tentare INSERT; il vincolo UNIQUE `cle_logical_version_uq` previene il duplicato a DB ma uno dei due fallirà. Mitigazione di piano: in transazione, eseguire `SELECT MAX(version_no) FOR UPDATE` sul logical claim, oppure usare un retry pattern con backoff su `UniqueViolation` ricalcolando `version_no`. La scelta finale è del Blocco 2.

4. **Concorrenza su withdrawal doppia.** Mitigato dalla UNIQUE `(published_answer_id, event_type, idempotency_key)` su `published_answer_lifecycle_events` e dalla guardia status-based sull'UPDATE di `published_answers` (`WHERE status='published'`). L'UPDATE che non match nulla è un no-op silenzioso; la seconda invocazione del consumer registra `skipped_terminal`.

5. **`task.status` resta `published` anche dopo la withdrawal del relativo answer.** Decisione consapevole: il lifecycle vive su `published_answers`, non su `task_masters`. Da documentare esplicitamente nei test e nei doc utente. Eventuale evento audit `task.withdrawn` audit-only fornisce la lettura cronologica.

6. **`docs/migration_plan.md` parla di "trigger previsti" per la propagazione 8.5.** Verificato: la sezione `0006_lifecycle.sql (Sprint 4 — Fase 8.5, da scrivere)` accenna a "propagatore `source_loss_events → claim_ledger_entries`" e "propagatore `lifecycle → published_answers`" come "trigger previsti". La decisione vincolante 5 di questo piano contraddice quella nota. Mitigazione: la documentazione `docs/migration_plan.md` verrà aggiornata nel Blocco 5 della rollout per riflettere la scelta "propagator pipeline applicativa, non trigger DB". Non modificare `migration_plan.md` adesso, fa parte del Blocco 5.

7. **Backfill `published` lifecycle events per pubblicazioni 8.4.** Non c'è backfill nella migration. Le pubblicazioni 8.4 esistenti restano senza un evento `published` in `published_answer_lifecycle_events`. I test API che leggono `lifecycle-events` per pubblicazioni 8.4 devono essere costruiti tenendo conto di questa asimmetria (per pubblicazioni nuove 8.5 ci sarà l'evento `published`, per quelle pre-esistenti no, oppure il consumer lifecycle del Blocco 3 emette in modalità lazy un `published` event la prima volta che vede una pubblicazione "già pubblicata" senza eventi lifecycle). La scelta operativa è del Blocco 3.

8. **Nessun cleanup distruttivo in 8.5.** Il piano blob orfani, retention, soft-delete dei chunks/versioni è esplicitamente fuori scope. In compenso il volume del DB cresce di tutti i lifecycle events, source loss events e propagation records senza politiche di pruning. Accettabile per MVP-0.

9. **Coerenza FK su `evidence_spans`.** Le source loss puntano a `evidence_span_id` con `ON DELETE RESTRICT`. Una cancellazione di un `evidence_span` cancellerebbe `document_chunks` su CASCADE, ma `evidence_spans` ha già il proprio trigger append-only e una RESTRICT su `source_loss_events` lo rende ulteriormente non cancellabile finché esistono source loss correlate. Coerente, da verificare nel test DB.

10. **Audit chain con scope ambiguo per source loss senza task chiaro.** Una source loss su uno span condiviso da più task richiede una scelta di scope per l'audit event. Decisione di piano: il propagator emette **un audit event per ogni `task_id` impattato** (uno per task), su `chain_scope='task'`. Quando lo scope task non è risolvibile, fallback a `chain_scope='project'`. Nessun audit event a `chain_scope='global'` per source loss in 8.5.

---

## 15. Criteri di accettazione della Fase 8.5

La Fase 8.5 è considerata conclusa quando:

- `migrations/0006_lifecycle.sql` è applicata con successo da `make migrate` su un DB vuoto e su un DB con stato 8.4 preesistente;
- `tests/test_lifecycle_constraints.py` passa;
- `published_answer_lifecycle_events` è effettivamente append-only (verificato da test);
- `source_loss_events` è effettivamente append-only (verificato da test);
- `source_loss_propagation_records` è append-only (verificato da test);
- la withdrawal asincrona via `POST /api/v1/published-answers/{id}/withdraw` è funzionante end-to-end: richiesta → evento → consumer → `published_answers.status='withdrawn'` → audit chain ok;
- una source loss su un `evidence_span_id` collegato a un claim con latest `verified_fact` produce una nuova `claim_ledger_entries v(N+1)` con `state='unverifiable'`, `transition_reason='source_lost'`, e una `claim_lineage` con `relation_kind='supersedes'` collegata correttamente;
- nessun nuovo status su `task_masters`;
- `make test-db`, `make test-shared`, `make test-api`, `make test-worker`, `make test-web`, `make test` tutti verdi;
- `README.md`, `docs/migration_plan.md`, `PROJECT_STATE.md` aggiornati al post-8.5.

---

## 16. Prossimo prompt operativo

Dopo l'approvazione di questo piano, il prossimo prompt operativo (da non eseguire ora) sarà:

> Genera Blocco 1 Fase 8.5: `migrations/0006_lifecycle.sql`, `packages/shared/evidencefirst_shared/schemas.py`, `tests/test_lifecycle_constraints.py`. Rispetta integralmente le decisioni vincolanti elencate in `PHASE_8_5_PLAN.md` sezione 13. Niente trigger DB di propagazione, niente estensione di `claim_lineage.relation_kind` né di `verification_records.check_kind`, niente status nuovi su `task_masters`. Tabelle proposte: `published_answer_lifecycle_events`, `source_loss_events`, `source_loss_propagation_records`. Test rerun-safe. File integrali, niente placeholder.

Quel blocco **non** verrà generato adesso. Il presente file è solo piano.
