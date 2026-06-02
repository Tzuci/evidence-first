# PHASE_ORCH_MULTI_B_PRE — Source Resolution Pass Design

> **Documento di design del futuro source resolution pass mock/local-only per i
> `source_candidates` proposti dagli agent.** Questo blocco è **solo
> progettazione**. Non implementa codice di produzione, non crea né modifica
> migration, non modifica `apps/api/*`, `apps/worker/*`, `apps/web/*`,
> `packages/shared/*`, non tocca i test, non modifica il runner esistente, non
> modifica la provider abstraction, non aggiunge dipendenze, non aggiunge SDK
> provider, non aggiunge segreti, non modifica `.env`, non modifica `README.md`
> né `PROJECT_STATE.md` né alcun altro `PHASE_*_PRE.md` né alcun
> `*_IMPLEMENTATION_REPORT.md`. L'unico deliverable è questo file
> `PHASE_ORCH_MULTI_B_PRE.md`. **Nessun commit è eseguito.**
>
> Lingua: italiano tecnico, registro da System Architect.
>
> **Promemoria di linguaggio (vincolante per tutta la fase).** Il sistema è
> evidence-first ed evidence-gated. Non promette verità assoluta, non promette
> l'eliminazione totale degli output errati di un LLM, non dichiara che le sue
> risposte siano "vere". Il futuro `ORCH-MULTI-B` trasforma un insieme di
> `source_candidates` proposti in un insieme di tentativi di resolution
> persistiti e auditabili: **non recupera realmente le fonti**, **non verifica
> alcuna fonte**, **non collega alcuna fonte a un claim**, **non decide
> publishability**, **non sostituisce il Final Answer Gate**. Le
> `source_candidates` restano proposte non verificate; un esito di resolution è
> un record tecnico append-only che serve audit/debugging e non garantisce la
> verità fattuale né il supporto semantico di alcun claim.
>
> **Nota di coerenza architetturale (vincolante).** Quando un'entità registra
> un *fatto* (creazione di un candidate, tentativo di resolution, transizione di
> stato di un run), quel fatto è **append-only**: una sua "modifica" è una nuova
> riga, mai una riscrittura silenziosa. `source_candidates`, `source_resolutions`
> e `orchestration_events` sono append-only nello schema `0011`. La sola
> eccezione ammessa è il campo `orchestration_runs.status` materializzato; ogni
> sua transizione, quando il codominio degli `event_type` reali lo prevede, deve
> generare un evento corrispondente in `orchestration_events`. `ORCH-MULTI-B`
> rispetta integralmente questa disciplina e in particolare **non aggiorna mai
> `source_candidates.status`**.

---

## Indice

1. Status
2. Baseline
3. Scopo del source resolution pass
4. Principio semantico vincolante
5. Input contract proposto
6. Output contract proposto
7. Candidate selection
8. Resolution policy mock/local-only
9. Stato derivato del candidate
10. Event model
11. Idempotenza
12. Failure semantics
13. Boundedness
14. Budget / usage policy
15. Security / redaction
16. Non-goals
17. Test plan futuro per ORCH-MULTI-B
18. Acceptance criteria
19. Fasi future suggerite
20. Comandi di verifica

---

## 1. Status

- **Phase:** ORCH-MULTI-B-PRE
- **Type:** design only.
- **Target implementation phase:** ORCH-MULTI-B.

Questa fase è esclusivamente di design. In particolare:

- **nessun codice** di produzione, worker, backend, frontend o pacchetto
  condiviso è scritto o modificato;
- **nessuna migration** è creata o modificata (`0001`-`0011` restano invariate);
- **nessun test** è scritto o modificato;
- **nessuna API** HTTP è aggiunta o modificata;
- **nessuna UI** è aggiunta o modificata;
- **nessun commit automatico**, nessun push.

Ogni decisione qui presa è una **raccomandazione di design** per `ORCH-MULTI-B`,
non un impegno di implementazione. Nomi e tipi di contratto sono indicativi e
soggetti a revisione nella fase di codice. Lo schema `0011_orchestration_schema.sql`
fornisce già le tabelle (`source_candidates`, `source_resolutions`,
`source_verifications`, `orchestration_events`) e gli `event_type` necessari:
`ORCH-MULTI-B` non richiede migration.

---

## 2. Baseline

`ORCH-MULTI-A` (commit `28cecbe`, "Add multi-agent mock orchestration runner"; doc
state aggiornato in DOC-ORCH-STATE al commit `2979741`) è la baseline da cui
`ORCH-MULTI-B` parte. In sintesi:

- **ORCH-MULTI-A produce `source_candidates` solo da provider output riusciti.**
  Un agente che fallisce non emette output e non propone candidate; solo gli
  agenti `succeeded` con output possono produrre il proprio gruppo di candidate.
- **I `source_candidates` sono proposte non verificate.** Ogni candidate
  persistita ha `status='proposed'`, `candidate_type='agent_cited'`, una
  `provenance` che dichiara `is_verified=False`, nessun `evidence_span_id`,
  nessun claim link.
- **ORCH-MULTI-A non esegue source resolution.** Nessuna riga `source_resolutions`
  è scritta da `ORCH-MULTI-A`.
- **ORCH-MULTI-A non esegue retrieval.** Nessun recupero reale di fonti, nessuna
  rete.
- **ORCH-MULTI-A non crea `evidence_spans`.**
- **ORCH-MULTI-A non crea `claim_evidence_links`.**
- **ORCH-MULTI-A non esegue `source_verifications`.** Nessuna riga
  `source_verifications` è scritta.
- **ORCH-MULTI-A non esegue il Final Answer Gate.**
- **`publication_status` resta `not_evaluated`** e
  **`gate_report_id` resta `None`** per ogni risultato multi-agent.
- **Test finale ORCH-MULTI-A documentato: 17 passed** (mock-only, DB-backed,
  senza rete, senza Redis, senza FastAPI).

Stato dello schema rilevante per questa fase (migration `0011`, applicata e
immutabile): `source_candidates`, `source_resolutions`, `source_verifications`,
`orchestration_events` esistono già; `token_usage_records.pass_kind` ammette già
`'source_resolution'`. Tutte le tabelle di fatto sono append-only via il trigger
condiviso `reject_modify_append_only`.

---

## 3. Scopo del source resolution pass

Il futuro `ORCH-MULTI-B` dovrà, in modalità **mock/local-only**:

- **leggere i `source_candidates` in stato derivato `proposed`/non risolto** di
  un run;
- **tentare una resolution mock/local-only** secondo una policy onesta e
  deterministica (§8), senza rete e senza retrieval reale;
- **scrivere righe `source_resolutions` append-only**, una o più per candidate,
  con `outcome` dal codominio `0011`;
- **emettere gli eventi `source_resolution_started` e
  `source_resolution_completed`** nello spazio `sequence_no` del run;
- **mantenere l'idempotenza** ancorata a
  `source_resolutions UNIQUE (source_candidate_id, idempotency_key)`;
- **mantenere la provenance** lungo l'asse tenant → run → agent → output →
  candidate, così che ogni `source_resolutions` resti tracciabile alla sua
  origine;
- **non produrre evidence** (nessun `evidence_span`);
- **non produrre claim binding** (nessun `claim_evidence_links`, nessun claim
  link);
- **non produrre alcuna gate decision** (nessun `final_gate_reports`, nessuna
  `published_answers`).

Il pass è bounded e deterministico: termina in un numero finito e predeterminato
di tentativi (al più uno per candidate eleggibile, salvo retry idempotente
esplicito), senza loop e senza ricorsione.

---

## 4. Principio semantico vincolante

Le seguenti distinzioni sono **vincolanti** e devono restare leggibili in tutta
la documentazione e in ogni futura implementazione:

- **source_candidate ≠ evidence.**
- **provider citation/source ≠ evidence verificata.**
- **source resolution ≠ source verification.**
- **source verification ≠ supporto del claim.**
- **resolved source ≠ evidence span.**
- **resolved source ≠ claim pubblicabile.**
- **provider output ≠ final answer.**
- **Il Final Answer Gate non è eseguito in questa fase.**

**Significato preciso di un esito `resolved`.** Un `outcome='resolved'` su una
`source_resolutions` significa **solo** che il target del candidate è stato
individuato o normalizzato secondo la policy del resolver. **Non** significa che
la fonte sia stata recuperata, **non** significa che sia stata verificata, **non**
significa che sia stata collegata a un claim, **non** significa che sia stata
approvata dal Gate. Il ponte verso `evidence_spans` passa esclusivamente per
`source_verifications.evidence_span_id`, che `ORCH-MULTI-B` non scrive e non
consuma. Una citazione/fonte proposta da un agente resta una proposta non
verificata anche dopo essere stata "risolta".

---

## 5. Input contract proposto

`ORCH-MULTI-B` riceve (o costruisce internamente) una richiesta logica di
resolution pass. È un **contratto concettuale**, non un'API definitiva: nomi e
tipi sono indicativi e soggetti a revisione in `ORCH-MULTI-B`. **Nessun endpoint
HTTP è progettato qui.**

```
SourceResolutionPassRequest:
  - orchestration_run_id            # run i cui candidate vanno risolti;
                                     #   obbligatorio.
  - tenant_id                       # tenant del run; obbligatorio; deve
                                     #   coincidere con il tenant del run e dei
                                     #   candidate.
  - max_candidates                  # limite superiore bounded al numero di
                                     #   candidate elaborati in un pass; default
                                     #   costante implementativa (vedi §13).
  - candidate_selection_scope       # 'per_run' (tutti i candidate eleggibili del
                                     #   run) oppure 'per_agent_output'
                                     #   (candidate di uno specifico
                                     #   agent_output_id); design (§7).
  - agent_output_id                 # opzionale; usato solo se
                                     #   candidate_selection_scope =
                                     #   'per_agent_output'.
  - eligible_states                 # filtro sullo stato DERIVATO del candidate;
                                     #   in ORCH-MULTI-B tipicamente
                                     #   {proposed} (cioè non ancora risolto).
  - idempotency_key                 # chiave opaca di idempotenza del pass,
                                     #   componente delle idempotency_key
                                     #   candidate-scoped (vedi §11);
                                     #   obbligatoria.
  - created_by                      # attore che avvia il pass; opzionale, usato
                                     #   solo se coerente con i campi disponibili
                                     #   (es. source_resolutions non ha colonna
                                     #   created_by in 0011; il valore può vivere
                                     #   nel payload evento o restare non
                                     #   persistito).
```

Chiarimenti vincolanti:

- **selezione solo di candidate non risolti.** Vengono elaborati solo i
  candidate il cui **stato derivato** (§9) è non risolto (tipicamente
  `proposed`). Un candidate già risolto con la stessa `idempotency_key` non
  viene ri-elaborato (skip idempotente, §7, §11).
- **tenant + run scoping.** Tutti i candidate elaborati appartengono allo stesso
  `tenant_id` e allo stesso `orchestration_run_id`; un mismatch è scartato.
- **nessun secret nell'input.** Il contratto non porta API key, token di
  autenticazione, credenziali, Authorization header, password.

Questo resta a livello design: non va scritto codice operativo in
`ORCH-MULTI-B-PRE`.

---

## 6. Output contract proposto

`ORCH-MULTI-B` restituisce (in memoria, in un eventuale wrapper sincrono) un
output logico che descrive l'esito del pass e gli id dei fatti persistiti.

```
SourceResolutionPassResult:
  - status                          # esito sintetico del pass: 'succeeded' /
                                     #   'failed' (vedi §12). Per un replay
                                     #   rispecchia i fatti già persistiti.
  - orchestration_run_id            # run elaborato.
  - source_resolution_ids           # tuple degli id source_resolutions creati,
                                     #   in ordine deterministico (§7).
  - per_candidate_outcomes          # mappa candidate_id -> outcome
                                     #   (resolved / failed / insufficient_metadata
                                     #   / partial / unreachable / not_found),
                                     #   dal codominio source_resolutions di 0011.
  - event_ids                       # tuple degli id orchestration_events creati
                                     #   (solo source_resolution_started/completed).
  - counters:
      candidates_seen               # candidate considerati nello scope.
      candidates_attempted          # candidate per cui è stato tentato un
                                     #   resolution (started emesso).
      resolved_count                # outcome = resolved.
      failed_count                  # outcome ∈ {failed, unreachable, not_found}.
      insufficient_metadata_count   # outcome = insufficient_metadata.
      skipped_count                 # candidate saltati (già risolti con stessa
                                     #   key, non eleggibili, oltre il bound).
  - publication_status              # sempre 'not_evaluated'.
  - gate_report_id                  # sempre None.
```

Chiarimenti vincolanti:

- **nessun `published_answer_id`.** Il pass non pubblica nulla; il contratto non
  espone un id di published answer.
- **`gate_report_id` sempre `None`.** Il pass non esegue il Gate; non esiste un
  valore diverso da `None`.
- **`publication_status` sempre `not_evaluated`.** Coerente con l'invariante §4.
- **ordine deterministico degli id.** `source_resolution_ids`, `event_ids` e
  l'ordine di iterazione seguono l'ordinamento deterministico dei candidate
  (§7), così l'output è ispezionabile in modo riproducibile.

Questo resta a livello design.

---

## 7. Candidate selection

Regole di selezione dei candidate da risolvere, valide per `ORCH-MULTI-B`:

- **tenant scoped.** Solo candidate con `tenant_id == request.tenant_id`.
- **run scoped.** Solo candidate con
  `orchestration_run_id == request.orchestration_run_id`. Se lo scope è
  `per_agent_output`, ulteriore filtro su `agent_output_id`.
- **stable ordering.** I candidate sono ordinati in modo deterministico, per
  esempio `(agent_output_id, created_at, id)`, così che
  `source_resolution_ids`, gli `sequence_no` degli eventi e i counters siano
  riproducibili. L'ordine del DB non deve introdurre non-determinismo.
- **bounded.** Al più `max_candidates` candidate elaborati per pass (§13); il
  resto è contato in `skipped_count` e lasciato a un pass successivo.
- **skip dei candidate già risolti con la stessa idempotency key.** Un candidate
  che ha già una `source_resolutions` con la stessa `idempotency_key`
  candidate-scoped non viene ri-elaborato; è uno skip idempotente (§11), non un
  errore.
- **non mutare `source_candidates`.** La selezione legge i candidate; non scrive
  e non aggiorna mai la loro riga. Lo stato è derivato (§9).
- **latest state derivato da `source_resolutions`.** L'eleggibilità di un
  candidate (non ancora risolto) è calcolata dalla sua latest `source_resolutions`
  secondo le regole di ordinamento di §9, non dal campo `source_candidates.status`
  scritto al momento della creazione.

---

## 8. Resolution policy mock/local-only

La policy di resolution di `ORCH-MULTI-B` è **mock/local-only e onesta**:

- **nessuna rete.** Nessuna chiamata di rete, nessun socket.
- **nessun HTTP client.**
- **nessun browser.**
- **nessun provider reale.**
- **nessuna finta risoluzione web.** Il pass **non** dichiara di aver risolto un
  URL web se non esegue alcun recupero: in assenza di rete un URL esterno non
  può essere realmente raggiunto, e l'esito deve dirlo.

Esempi di mappatura deterministica (indicativi, da finalizzare in `ORCH-MULTI-B`):

- **URL esterno senza retrieval reale** → `outcome = unreachable` oppure
  `insufficient_metadata`, mai `resolved`. Il pass mock non raggiunge la rete e
  l'esito lo riflette onestamente.
- **candidate con metadata insufficiente** (nessun locator utile, payload vuoto)
  → `outcome = insufficient_metadata`.
- **target interno / uploaded document già presente e referenziabile localmente**
  → `outcome = resolved`, **solo** se lo schema e i dati locali lo consentono
  davvero (es. il candidate punta in modo non ambiguo a un documento già
  presente nel corpus locale). `resolved` qui significa "target individuato e
  normalizzato localmente", non "fonte verificata" (§4).
- **locator di tipo non supportato dalla policy** → `outcome = failed` oppure
  `insufficient_metadata`, secondo policy documentata.
- **duplicato idempotente** (stesso candidate, stessa `idempotency_key`
  candidate-scoped) → replay/skip senza duplicare la `source_resolutions`
  (§11).

Il `resolution_target_kind` scritto su `source_resolutions` appartiene al
codominio `0011`: `url`, `web_page`, `internal_document`, `uploaded_document`,
`retrieved_document`. In modalità mock/local-only sono coerenti soprattutto
`internal_document` e `uploaded_document`; `url`/`web_page` possono essere
registrati come target dichiarato del candidate ma con un `outcome` onesto
(`unreachable`/`insufficient_metadata`) finché non esiste una fase di retrieval
reale.

---

## 9. Stato derivato del candidate

`source_candidates` è **append-only**. `ORCH-MULTI-B` **non** esegue UPDATE di
`source_candidates.status`: quella colonna conserva il valore scritto alla
creazione del candidate (`proposed`). Lo **stato corrente** di un candidate è
**derivato** dalla sua latest `source_resolutions`, ordinata in modo
deterministico (per esempio `ORDER BY created_at DESC, id DESC`).

Stati concettuali derivati proposti:

- **proposed** — nessuna `source_resolutions` esiste per il candidate (mai
  tentato).
- **resolution_pending** — esiste un `source_resolution_started` ma non un
  esito completato corrispondente (stato transitorio; in un pass sincrono
  bounded può non materializzarsi mai a regime).
- **resolved** — la latest `source_resolutions` ha `outcome = resolved`.
- **resolution_failed** — la latest `source_resolutions` ha
  `outcome ∈ {failed, unreachable, not_found}` (e, per policy documentata,
  eventualmente `partial`).
- **insufficient_metadata** — la latest `source_resolutions` ha
  `outcome = insufficient_metadata`.

La mappatura deve rispettare i codomini già presenti in `0011`:

- `source_resolutions.outcome ∈ {resolved, failed, insufficient_metadata,
  partial, unreachable, not_found}`;
- i nomi degli stati derivati riusano la terminologia già presente nel codominio
  `source_candidates.status` (`proposed`, `resolution_pending`, `resolved`,
  `resolution_failed`, `insufficient_metadata`), **senza scrivere** quei valori
  sulla riga `source_candidates`.

Lo stato derivato è una vista di lettura: non è persistito su `source_candidates`,
è ricalcolato dai fatti append-only ad ogni necessità.

---

## 10. Event model

`ORCH-MULTI-B` usa **solo** due `event_type`, entrambi già ammessi dal codominio
chiuso di `orchestration_events_event_type_chk` (migration `0011`):

- `source_resolution_started` — emesso prima del tentativo di resolution di un
  candidate.
- `source_resolution_completed` — emesso dopo aver persistito l'esito della
  resolution di un candidate.

Regole:

- **`sequence_no` nello spazio del run.** Gli eventi del pass condividono lo
  spazio `sequence_no` di `orchestration_events` per quel run, crescente e
  contiguo, enforced da `orchestration_events_run_sequence_uq`. Un contatore
  unico per il run produce l'ordinamento.
- **`idempotency_key` con discriminante candidate.** Poiché
  `source_resolution_started`/`completed` si ripetono per più candidate con lo
  stesso `event_type`, la loro `idempotency_key` **deve** includere un
  discriminante per candidate (es.
  `<pass_idem>:source_resolution_started:<candidate_id>`), per non collidere su
  `orchestration_events_run_type_idem_uq`.
- **payload redatto.** `event_payload` non contiene segreti né raw payload non
  redatto (§15).
- **nessun nuovo `event_type`.** In particolare `ORCH-MULTI-B`
  **non usa** `source_verification_completed` (verifica fuori scope, §16), né
  inventa event_type non presenti in `0011`.
- **`run_failed` solo per fallimento globale del pass.** L'eventuale evento
  `run_failed` è appeso solo se il pass fallisce globalmente (§12), non perché un
  singolo candidate non è stato risolto. Un singolo `outcome` non-`resolved` è un
  esito normale del candidate, non un fallimento del run.

---

## 11. Idempotenza

- **`source_resolutions UNIQUE (source_candidate_id, idempotency_key)` è la
  radice.** L'idempotenza del tentativo di resolution di un candidate è ancorata
  a questo vincolo di `0011`.
- **replay senza duplicati.** Un secondo pass con le stesse chiavi non inserisce
  righe `source_resolutions` duplicate né eventi duplicati; ricostruisce e
  ritorna i fatti già persistiti.
- **same candidate + same key → stesso risultato persistito.** Il tentativo è
  deduplicato dalla UNIQUE; l'output del pass rispecchia il fatto già presente.
- **same candidate + different key → nuova attempt append-only**, se la policy
  di `ORCH-MULTI-B` ammette retry espliciti. Una nuova `idempotency_key`
  candidate-scoped produce una nuova riga `source_resolutions` (append-only),
  mai un UPDATE della precedente. La latest secondo §9 determina lo stato
  derivato.
- **eventi idempotenti con chiave candidate-scoped.** Le `idempotency_key` di
  `source_resolution_started`/`completed` includono il discriminante candidate
  (§10), così il replay ricalcola chiavi identiche e l'UNIQUE
  `orchestration_events_run_type_idem_uq` deduplica.

---

## 12. Failure semantics

Distinzione degli esiti, dal livello candidate al livello run:

- **invalid candidate** — candidate fuori tenant/run scope o malformato: scartato
  prima del tentativo; contato come skip, non come errore globale.
- **insufficient metadata** — `outcome = insufficient_metadata`: esito normale
  del candidate, non fallimento del run.
- **unsupported locator type** — locator non gestito dalla policy:
  `outcome = failed` o `insufficient_metadata` per quel candidate; non fallimento
  del run.
- **unreachable external target in mock-only mode** — `outcome = unreachable`:
  esito onesto in assenza di rete; non fallimento del run.
- **duplicate candidate** — stesso candidate + stessa key: skip idempotente
  (§11); non errore.
- **internal resolver error** — errore inatteso del resolver su un singolo
  candidate: registrato come `failed` per quel candidate con `failure_reason`
  redatto; può non fallire il run se la policy isola il singolo candidate.
- **global run failure** — solo un fallimento che impedisce l'intero pass (es.
  precondizione invalida sul run, errore non isolabile) porta
  `status='failed'` e l'eventuale `run_failed` (§10).

**Principio chiave:** il fallimento di resolution di un singolo candidate **non**
deve automaticamente far fallire il run. Gli esiti per-candidate sono fatti
append-only indipendenti; il run resta `not_evaluated` sul piano publication
(§4) in ogni caso.

---

## 13. Boundedness

Limiti futuri da fissare in `ORCH-MULTI-B` (valori esatti decisi in fase di
codice; ciò che è vincolante è che il pass sia bounded):

- **max candidates per run** — costante implementativa (`max_candidates`), oltre
  la quale i candidate eccedenti sono `skipped_count` e lasciati a un pass
  successivo.
- **max candidates per agent output** — limite analogo quando lo scope è
  `per_agent_output`.
- **max raw payload bytes** — limite alla dimensione del `raw_citation_payload`
  letto da un candidate, per evitare elaborazioni non bounded.
- **max normalized locator bytes** — limite alla lunghezza del locator
  normalizzato registrato.
- **no network timeout in questa fase** — non esiste rete, quindi nessun timeout
  di rete è applicabile.
- **future timeout solo per la retrieval phase successiva** — eventuali timeout
  apparterranno a `ORCH-MULTI-C` (retrieval/evidence extraction), non a
  `ORCH-MULTI-B`.

---

## 14. Budget / usage policy

- **source resolution mock/local-only non invoca un provider LLM.** Il pass non
  genera testo, non chiama il `MockProviderAdapter` per produrre risposte: la
  resolution è un'operazione locale e deterministica.
- **`provider_invocations` normalmente non devono essere creati** da questo pass,
  perché non c'è invocazione di provider.
- **`token_usage_records` può essere omesso** se non c'è alcun token usage reale
  da registrare.
- **se in futuro si registra usage per audit**, il `pass_kind` deve usare
  `source_resolution` (già presente nel codominio `token_usage_records.pass_kind`
  di `0011`) e deve essere chiaramente marcato come **non** token di provider LLM
  (es. `is_mock=True`, `provider_invocation_id` NULL, usage nominale). Mai
  presentare un usage di resolution come consumo reale di un provider AI.

---

## 15. Security / redaction

- **nessun secret nei candidate payload.** Né il candidate né il suo
  `raw_citation_payload` devono contenere o veicolare API key, token, credenziali.
- **`failure_reason` redatto.** Ogni `failure_reason` persistito su
  `source_resolutions` e ogni payload di evento di fallimento passa per la
  redaction già adottata altrove (mascheramento `name=value` / `name: value`
  con `[REDACTED]` e troncamento bounded), coerente con la disciplina di
  `ORCH-PROVIDER-A`.
- **`raw_citation_payload` trattato come input non fidato.** Proviene dall'output
  di un provider/agente: va trattato come dato non fidato, normalizzato e
  bounded, mai eseguito o interpretato come istruzione.
- **`retrieved_artifact_ref` non è prova né evidenza.** È un riferimento tecnico
  opzionale; non implica che esista un'evidenza, non è un `evidence_span`.
- **`retrieved_artifact_ref` non è una FK forte.** In `0011` è una colonna UUID
  **non** vincolata da foreign key; non deve essere interpretata come FK
  enforced. Il design non deve assumere integrità referenziale su quel campo.

Vincoli ereditati: i log del pass e dei futuri test non devono contenere
segreti; i payload di `orchestration_events.event_payload` non devono contenere
segreti.

---

## 16. Non-goals

`ORCH-MULTI-B` **non** implementa, e `ORCH-MULTI-B-PRE` non progetta come parte
dello scope:

- **no codice** — nessun file di produzione/worker/backend/frontend/shared;
- **no migration** — nessun file in `migrations/`;
- **no test** — nessun file di test;
- **no API** — nessuna route HTTP;
- **no UI** — nessuna pagina o componente frontend;
- **no provider reale** — nessun OpenAI/Anthropic/Gemini o altro provider esterno;
- **no network retrieval** — nessun recupero di rete;
- **no browser**;
- **no source verification** — nessuna verifica di fonti;
- **no `source_verifications` write** — nessuna riga in `source_verifications`;
- **no `evidence_spans`** — nessuna creazione di evidence span;
- **no `claim_evidence_links`**;
- **no claim binding**;
- **no synthesis**;
- **no `candidate_syntheses`**;
- **no Final Answer Gate**;
- **no `final_gate_reports`**;
- **no `published_answers`**;
- **no integration in `task.created`** — il pass resta separato dalla pipeline
  evidence-gated esistente.

---

## 17. Test plan futuro per ORCH-MULTI-B

Solo **piano**, nessun codice. Test futuri richiesti per `ORCH-MULTI-B`
(mock-first, DB-backed dove serve, senza rete, senza Redis, senza FastAPI, sul
modello di `test_orchestration_runner_service.py`):

1. **creates source_resolutions for eligible candidates** — i candidate
   eleggibili producono righe `source_resolutions` append-only con `outcome` dal
   codominio `0011`.
2. **does not mutate source_candidates** — nessun UPDATE/DELETE su
   `source_candidates`; conteggi e righe invariati, stato corrente derivato.
3. **idempotent replay does not duplicate source_resolutions** — replay con
   stesse chiavi: nessun duplicato in `source_resolutions` né in
   `orchestration_events`.
4. **emits source_resolution_started/completed only** — gli `event_type` emessi
   sono un sottoinsieme di `{source_resolution_started,
   source_resolution_completed}`; assenza di altri event_type.
5. **does not create evidence_spans** — delta zero su `evidence_spans`.
6. **does not create claim_evidence_links** — delta zero su
   `claim_evidence_links`.
7. **does not create source_verifications** — delta zero su
   `source_verifications`.
8. **does not create final_gate_reports** — delta zero su `final_gate_reports`.
9. **does not create published_answers** — delta zero su `published_answers`.
10. **unresolved external URL in mock-only mode is honest** — un candidate con
    URL esterno produce `outcome ∈ {unreachable, insufficient_metadata}`, mai
    `resolved`.
11. **insufficient metadata candidate is marked accordingly** — un candidate con
    metadata insufficiente produce `outcome = insufficient_metadata`.
12. **tenant isolation** — candidate di un altro tenant non sono elaborati;
    nessun cross-tenant.
13. **bounded max candidates** — oltre `max_candidates`, i candidate eccedenti
    sono contati come `skipped_count` e non elaborati.
14. **duplicate candidate behavior** — stesso candidate + stessa key: skip
    idempotente senza duplicare.
15. **run remains not_evaluated** — dopo il pass, `publication_status` resta
    `not_evaluated` e `gate_report_id` resta `None`.

Note: i test DB-backed seguiranno il pattern di `ORCH-RUNNER-A` / `ORCH-MULTI-A`
(fixture `conn` con transazione e rollback al teardown, migration applicate una
volta per sessione, skip pulito se `DATABASE_URL` è assente, conteggi
before/after per essere rerun-safe). Nessun test si basa sul tempo reale o sulla
rete.

---

## 18. Acceptance criteria

`PHASE_ORCH_MULTI_B_PRE.md`, per `ORCH-MULTI-B-PRE`, è accettabile se e solo se:

- **documento creato** — `PHASE_ORCH_MULTI_B_PRE.md` esiste;
- **nessun altro file modificato** — nessun altro file del repository è creato o
  modificato (in particolare runner, provider abstraction, consumer, migration,
  test, API, UI, `PROJECT_STATE.md`, `README.md`, altri `PHASE_*` o report);
- **nessuna migration** — nessun file in `migrations/`; `0001`-`0011` invariate;
- **nessun codice** — nessun file di codice scritto o modificato;
- **nessun test** — nessun file di test scritto o modificato;
- **wording vietato assente** — il documento non contiene i termini della lista
  vietata fuori dall'elenco esplicito del comando grep di §20;
- **invarianti preservati** — `source_candidate ≠ evidence`; provider
  output ≠ final answer; nessun Gate eseguito; nessuna publication;
  `publication_status` resta `not_evaluated`, `gate_report_id` resta `None`; le
  `source_candidates` restano `proposed`/non verificate e senza claim link;
  `source_candidates` non è mutata e lo stato corrente è derivato da
  `source_resolutions`.

---

## 19. Fasi future suggerite

Sequenza proposta dopo questo design:

- **ORCH-MULTI-B** — implementazione del source resolution pass mock/local-only
  progettato qui (estensione del runner o modulo sibling + test DB-backed di
  §17). Nessun retrieval reale, nessuna verifica, nessun Gate.
- **ORCH-MULTI-C-PRE** — design di retrieval / evidence extraction.
- **ORCH-MULTI-C** — implementazione di retrieval / evidence extraction.
- **ORCH-MULTI-D-PRE** — design di source verification / claim binding (qui
  entrerebbe in scope `source_verifications` e il ponte verso `evidence_spans`).
- **ORCH-MULTI-D** — implementazione di source verification / claim binding.
- **fase successiva** — design dell'integrazione synthesis + Final Answer Gate
  per la linea di orchestration.

Tutte queste fasi restano fuori scope sia per `ORCH-MULTI-B-PRE` sia per
`ORCH-MULTI-B`.

---

## 20. Comandi di verifica

Comandi di **sola lettura** per verificare i criteri di §18 (eseguiti dalla
radice del repository):

```bash
git status -sb
git diff --check
git diff --stat
git diff --name-only
```

`git diff --check` non deve segnalare errori di whitespace; `git diff --stat` e
`git diff --name-only` devono mostrare un solo file. Controllo wording vietato
(pattern con parentesi quadre sul primo carattere così che il comando non
intercetti se stesso):

```bash
grep -RniE "[t]ruth score|[v]erified true|[v]erified answer|[A]I verified|[f]actually true|[h]allucination eliminated|[h]allucination-free|[g]uaranteed truth|[z]ero hallucinations|[e]ntailed = true|[s]ource quality proves claim|[C]VE-lite proves support|[r]eal NLI|[c]ontradiction detector|[c]itation-to-claim validator" \
  PHASE_ORCH_MULTI_B_PRE.md || true
```

Deve restituire nulla. Controllo file singolo modificato:

```bash
git status --porcelain | grep -vE '^.. PHASE_ORCH_MULTI_B_PRE\.md$' || echo "OK: solo PHASE_ORCH_MULTI_B_PRE.md modificato"
```

*Nessun commit è eseguito in questa fase.*

```
Document source resolution pass design (ORCH-MULTI-B-PRE)
```
