# PHASE_UI_PRE — Evidence-First UI MVP

Documento **decisionale e di piano** per l'apertura della fase UI. Questo blocco è
**solo progettazione**: non implementa componenti React, non modifica `apps/web/*`,
non modifica `apps/api/*`, non modifica `apps/worker/*`, non modifica
`packages/shared/*`, non modifica migrations, non modifica `README.md` né
`PROJECT_STATE.md` né `PHASE_8_8B_REPORT_PRE.md`. Il solo deliverable è questo
file.

Stile: italiano tecnico da System Architect + Senior Lead Frontend Engineer.
Per dettagli backend già consolidati questo documento rimanda ai file letti
(`PROJECT_STATE.md`, `README.md`, `PHASE_8_8B_REPORT_PRE.md`,
`apps/api/app/routes/anti_hallucination_report.py`) anziché replicarne il
contenuto.

**Promessa anti-allucinazione (ribadita, vincolante per la UI).** Il sistema è
progettato per impedire che claim fattuali non supportati, contraddetti o basati
su fonti inadeguate vengano pubblicati come affidabili. Non promette di eliminare
le allucinazioni in senso assoluto. Una fonte citata non implica un claim vero.
Una quote testualmente presente non implica che la quote sostenga il claim. Un
verdict `entailed` non implica che il claim sia vero nel mondo. La UI MVP deve
riflettere fedelmente questa promessa, deve mantenere separati gli assi
(CVE-lite, Source Quality, Claim Entailment, Final Gate, Coverage gaps,
Publication status) e **non deve mai** comporre un punteggio unico di verità,
non deve mai mostrare un'etichetta "verified true", non deve mai trattare il
report come autorità decisionale.

---

## 0. Stato corrente

- **Commit di partenza:** `39cdb576bba71069dbbfcc08bb06563364569c6e`
  ("Document anti-hallucination report completion").
- **Backend:** la sotto-fase 8.8B-REPORT è tecnicamente chiusa. L'endpoint
  aggregato task-level `GET /api/v1/tasks/{task_id}/anti-hallucination-report`
  è attivo, read-only stretto, validato da 13 test API + 2 realistic flow
  test. Vedi `PHASE_8_8B_REPORT_PRE.md` (con appendice "Implementation status")
  per la chiusura tecnica e i risultati di test.
- **Endpoint specialistici disponibili e stabili** (vedi `PROJECT_STATE.md`):
  answers (`/draft`, `/final-gate-report`, `/published-answer`,
  `/published-answers/{id}`), claims (`/raw-claims`, `/classified-claims`,
  `/claims`, `/claims/{id}/history`, `/claims/{id}/evidence`), documents
  (`/projects/{id}/documents`, `/documents/{id}`, `/documents/{id}/chunks`),
  source quality (`/evidence-spans/{id}/source-quality`,
  `/tasks/{id}/source-quality`), claim-entailment task-level
  (`/tasks/{id}/claim-entailment`), lifecycle/source-loss (8.6), audit
  (`/tasks/{id}/audit`).
- **Decisione architetturale già presa:** la UI deve consumare primariamente
  l'Anti-Hallucination Report aggregato invece di orchestrare manualmente 8
  endpoint specialistici, almeno per la vista audit/report. Gli endpoint
  specialistici restano superficie secondaria per il drill-down e per fasi UI
  future.
- **Frontend reale al commit `39cdb57`:**
  - `apps/web/package.json` presente: `next 15.0.3`, `react 18.3.1`, `vitest 2.1.4`,
    `@testing-library/react 16.0.1`, `jsdom 25.0.1`, `typescript 5.5.4`.
    Niente Tailwind, niente shadcn/ui, niente design system esterno.
  - `apps/web/app/page.tsx` esiste con wording di Fase 8.1b ("API + Worker +
    Web stub. No real document upload yet (Phase 8.2).") — wording **legacy
    da aggiornare** in UI-HOME-DIAGNOSTIC, non in UI-PRE.
  - `apps/web/app/diagnostic/page.tsx` esiste e chiama
    `http://localhost:${WEB_PORT}/api/proxy-health` con
    `cache: "no-store"` — **route proxy non presente** nel repo al commit
    corrente.
  - `apps/web/vitest.config.ts` presente, `environment: "jsdom"`, include
    `tests/**/*.test.{ts,tsx}` — directory `apps/web/tsconfig.json` **non**
    presente.
  - `apps/web/app/api/*` **non** presente al commit `39cdb57`.
  - Nessuna pagina reale per project/task/document/report.

### File letti durante la preparazione di questo blocco

- `PROJECT_STATE.md`
- `README.md`
- `PHASE_8_8B_REPORT_PRE.md` (corpo storico + appendice "Implementation status")
- `apps/api/app/routes/anti_hallucination_report.py`
- `apps/api/tests/test_anti_hallucination_report_endpoint.py`
- `tests/test_phase_8_8b_report_flow.py`
- `apps/api/app/routes/projects.py`
- `apps/api/app/routes/tasks.py`
- `apps/api/app/routes/documents.py`
- `apps/api/app/routes/answers.py`
- `apps/api/app/routes/source_quality.py`
- `apps/api/app/routes/claim_entailment.py`
- `apps/api/app/routes/claims.py`
- `apps/api/app/routes/audit.py`
- `apps/web/package.json`
- `apps/web/app/page.tsx`
- `apps/web/app/diagnostic/page.tsx`
- `apps/web/vitest.config.ts`
- `apps/web/README.md`

### File assenti verificati al commit `39cdb57`

- `apps/web/tsconfig.json` — assente.
- `apps/web/app/api/` (directory route proxy/BFF) — assente.
- `apps/web/app/api/proxy-health/route.ts` (o equivalente) — assente: la
  pagina `/diagnostic` punta a una route che oggi non esiste.
- Nessun client API frontend (`apps/web/lib/api.ts` o simile) — assente.
- Nessuna pagina `tasks/`, `projects/`, o `documents/` lato web — assente.

### Stato reale frontend (sintetico)

Il frontend Next.js esistente è uno stub di Fase 8.1b con due pagine
(`/` e `/diagnostic`), nessun client API tipizzato, nessuna struttura
`components/` o `lib/`, nessun `tsconfig.json` esplicito, e una dipendenza
rotta su una route BFF mai introdotta. Vitest è configurato a livello di
config, ma nel repo non risultano test reali per il frontend al commit
corrente (la directory `tests/` referenziata da `vitest.config.ts` non
esiste sotto `apps/web/`). Il frontend, per ogni intento pratico, è
**fermo al 8.1b**: pronto a essere aperto in modo additivo, senza dover
prima ripulire codice complesso.

### Stato reale backend utile alla UI

Il backend espone già tutto ciò che serve a una UI MVP read-only:

- contratto aggregato task-level read-only stabile
  (`GET /api/v1/tasks/{task_id}/anti-hallucination-report`) con shape
  documentata in `PHASE_8_8B_REPORT_PRE.md §4` e implementata in
  `apps/api/app/routes/anti_hallucination_report.py`;
- envelope errori normalizzato (`{"error": {"code": ..., "message": ...,
  "details": {...}}}`), con `RESOURCE_NOT_FOUND` +
  `details.resource="task_masters"` per task inesistente;
- endpoint specialistici read-only per drill-down (answers, claims,
  source quality, claim entailment, lifecycle/source-loss, audit);
- `POST /api/v1/projects`, `POST /api/v1/projects/{id}/documents`,
  `POST /api/v1/tasks` operativi (necessari per fasi UI future
  UI-CREATE-FLOW; **fuori scope** di UI-PRE);
- nessuna RBAC reale, nessuna autenticazione reale: la UI MVP opera in
  dev mode contro un tenant 'dev' seeded.

### Rischi semantici principali identificati

I rischi semantici principali per la UI MVP sono i seguenti, e la
progettazione di questo blocco è organizzata per neutralizzarli:

1. **collasso degli assi in un singolo score** ("truth score", "AI
   verified", "evidence score 73%"): vietato. Vedi §3.
2. **confondere CVE-lite, Source Quality, Claim Entailment**: la UI deve
   tenerli separati per nome, per card, per icona; nessun aggregato.
3. **interpretare `entailed` come "claim vero"**: vietato. Il valore è
   solo "relazione locale claim ↔ quote sotto la normalizzazione del
   checker mock"; questo deve apparire come disclaimer leggibile.
4. **trattare `publication.status = published` come "true"**: la UI
   deve mostrare lo stato come "published" senza alcun aggettivo
   epistemico aggiuntivo; deve restare visibile la lista delle
   limitations.
5. **non distinguere `publication_held` da uno status DB di
   `task_masters`**: `publication_held` è uno **stato derivato** dal
   report, non un valore di `task_masters.status`. La UI deve copiare
   il disclaimer della §7 di `PHASE_8_8B_REPORT_PRE.md`.
6. **proporre che il report sia ricalcolato dalla UI o aggiornato in
   real-time**: il report è una vista derivata read-only. La UI può
   pollare, ma non rifletterà nuove decisioni se non emesse dal Gate
   nel backend.
7. **mostrare i `mock_indicators` solo come dettaglio nascosto**: i
   flag mock sono parte della promessa anti-allucinazione e devono
   essere visibili senza interazione (banner / fascia / chip), non
   solo in tooltip.
8. **riscrivere o parafrasare il payload JSONB**: la UI deve mostrare
   testo originale verbatim quando lo espone (raw JSON collapsible),
   senza interpretazioni semantiche.

### Proposta di scope UI-PRE (sintesi)

UI MVP a 3 livelli (vedi §5), con primo blocco implementativo **piccolo,
testabile e basato esclusivamente sul report aggregato**:

1. **Livello 1 (UI-REPORT-A + UI-REPORT-B):** viewer del report
   aggregato `GET /tasks/{taskId}/report`. Read-only. Mock-aware. Nessuna
   creazione di entità.
2. **Livello 2 (UI-HOME-DIAGNOSTIC):** aggiornamento home wording,
   correzione `/diagnostic`, input task id e linking al report.
3. **Livello 3 (UI-CREATE-FLOW-PRE + UI-CREATE-FLOW-CODE):**
   progettazione e poi implementazione del flusso project → document →
   task → polling report. Solo dopo che il viewer del report è stabile.

### Piano patch

Un solo file viene creato in questo blocco:

- `PHASE_UI_PRE.md` (questo documento).

Nessun altro file viene toccato. Tutti i gap legacy frontend (home
wording, `/diagnostic` rotto, assenza di `tsconfig.json`, assenza di
`apps/web/app/api/*`) sono **annotati come follow-up** e mappati a
blocchi futuri (UI-HOME-DIAGNOSTIC e UI-REPORT-A); non vengono
risolti qui.

---

## 1. Obiettivo UI MVP

L'obiettivo della UI MVP è **rendere consumabile da occhi umani la
promessa evidence-gated del sistema**, esponendo in modo onesto e
disambiguato lo stato anti-allucinazione di un task: cosa è stato
pubblicato, perché o perché no, su quali claim, supportati da quali
evidenze, valutati da quali assi, con quali warning, con quali block,
con quali limitazioni note.

La UI MVP **non** sostituisce gli endpoint specialistici. **Non** è una
nuova decisione. **Non** è un product showcase. **Non** è una
dashboard di health globale. È un **report viewer evidence-first** che
serve a tre scopi:

1. dare al collaboratore tecnico un'interfaccia per verificare a
   colpo d'occhio se un task ha attraversato correttamente la
   pipeline 8.4 + 8.7E + 8.8A;
2. dare al revisore umano una vista che renda **leggibili** i risultati
   degli assi anti-allucinazione senza richiedere di leggere
   `psql` o curl;
3. dare al system architect una palette di componenti riutilizzabili
   per il consumo futuro di endpoint claim-level, lifecycle/source-loss,
   e — quando arriveranno — Contradiction Detector, Citation-to-Claim
   Validator, NLI reale.

L'obiettivo NON è:

- nessuna garanzia di verità;
- nessuna pubblicazione di nuovi claim dalla UI;
- nessuna modifica di entità DB dalla UI;
- nessuna autenticazione reale, nessuna RBAC reale, nessun multi-tenant
  selector reale;
- nessuna animazione, transition o effetto che possa indurre
  l'impressione di "intelligenza" non presente.

---

## 2. Utenti e job-to-be-done

Tre profili utente, nessuno autenticato (MVP-0 dev mode), tutti
operatori del repository:

### 2.1 Engineer / collaboratore tecnico

- **Job-to-be-done:** "ho appena fatto girare un task end-to-end via
  smoke test o pytest e voglio capire se è andato bene e, se no, dove
  si è bloccato."
- **Comportamento atteso:** apre `/tasks/<id>/report`, vede subito
  publication status, gate decision, gate reason_code, coverage gaps
  ordinati per severity, axis_summary, mock indicators.
- **Esigenze UX:** velocità, densità informativa, raw JSON
  collapsible per inspection, link bidirezionali con gli endpoint
  specialistici se vuole approfondire.

### 2.2 Revisore umano / domain expert

- **Job-to-be-done:** "voglio capire se mi posso fidare di questo
  published_answer."
- **Comportamento atteso:** apre `/tasks/<id>/report`, legge prima
  publication status (published / publication_held), poi la lista
  delle limitations, poi scorre i claim con le rispettive evidenze,
  CVE-lite, Source Quality, Claim Entailment.
- **Esigenze UX:** chiarezza semantica assoluta, nessun jargon di
  branch-name esposto senza definizione, disclaimer sempre
  presenti, mock indicators chiaramente visibili.

### 2.3 System architect / project owner

- **Job-to-be-done:** "voglio una palette di componenti riutilizzabili
  per le prossime fasi UI (UI-REPORT-B, UI-HOME-DIAGNOSTIC, UI-CREATE-FLOW)
  e voglio essere sicuro che non stiamo introducendo wording
  fuorviante."
- **Comportamento atteso:** guarda lo storybook implicito (cioè le
  pagine demo del viewer) e legge il codice dei componenti per
  estrarre pattern.
- **Esigenze UX:** struttura modulare, separazione netta delle
  responsabilità di rendering, naming inequivoco.

Out-of-scope per MVP-0:

- end user finale / cliente esterno;
- LLM operator con prompt panel;
- annotator umano con interazione di review;
- amministratore con dashboard multi-progetto.

---

## 3. Principi semantici obbligatori

Questi principi sono **vincolanti** per qualunque blocco UI futuro.
Vanno copiati o riferiti esplicitamente in ogni PRE successivo.

### 3.1 Vocabolario vietato

La UI **non deve mai** scrivere, mostrare, codificare in costanti, o
proporre tramite tooltip i seguenti termini:

- "truth score";
- "verified true";
- "hallucination eliminated";
- "factually true" (come stato del claim);
- "AI verified";
- "entailed = true" (come affermazione di verità);
- "source quality proves claim";
- "CVE-lite proves claim support";
- "contradiction detector" (perché non esiste in MVP-0);
- "citation-to-claim validator" (perché non esiste in MVP-0);
- "real NLI" (perché non esiste in MVP-0);
- "evidence score X%" come singolo aggregato di verità;
- "confidence" presentata come singolo numero di affidabilità
  globale del claim (la `confidence` di entailment/SQ è uno score
  interno al checker, non un truth score).

### 3.2 Vocabolario raccomandato

La UI **dovrebbe** usare:

- "Publication status";
- "Gate decision";
- "Gate reason code";
- "Evidence checks";
- "Quote/hash check" (per CVE-lite);
- "Source quality";
- "Claim ↔ evidence relation" (per Claim Entailment);
- "Final Gate";
- "Coverage gaps";
- "Blocking gaps";
- "Warnings";
- "Mock evaluator" / "Mock checker" / "Mock compiler" / "Mock CVE-lite";
- "Limitations".

### 3.3 Distinzioni semantiche da preservare sempre

La UI deve **sempre** distinguere visivamente:

| Asse | Cosa misura | Cosa NON misura |
|---|---|---|
| **CVE-lite** | quote substring del chunk + match SHA256 della quote | supporto semantico, qualità della fonte, verità |
| **Source Quality** | qualità strutturale della fonte (mock: sempre `unknown`) | se la fonte sostiene il claim, verità nel mondo |
| **Claim Entailment** | relazione claim ↔ evidence_span per la singola pair, sotto normalizzazione del checker mock | verità del claim nel mondo, cross-source contradiction |
| **Final Gate** | decisione di **pubblicabilità** per policy versionata (8.7G + 8.8A-GATE) | verità assoluta |
| **Publication** | stato del `published_answers` v1 o stato derivato dal report | qualità o verità del contenuto pubblicato |
| **Coverage gaps** | motivi persistiti dal Gate sulla draft v1 | nuovi giudizi della UI |
| **Report** | vista aggregata read-only | nuova decisione, ricalcolo, retroazione |

### 3.4 Frasi-disclaimer obbligatorie

Le frasi seguenti (o equivalenti semantici stretti) devono comparire
testualmente in almeno un punto leggibile di ogni rendering del
report:

- "Una fonte citata non implica che il claim sia vero."
- "Una quote testualmente presente non implica supporto semantico del
  claim."
- "Un verdict 'entailed' non implica verità nel mondo."
- "Claim status is ledger state, not truth guarantee."
- "Report is a derived read-only view; not a new decision."

Queste frasi possono essere prese verbatim dal campo `limitations`
del response del report (vedi `apps/api/app/routes/anti_hallucination_report.py
::_limitations`).

### 3.5 Mock indicators come cittadini di prima classe

I quattro flag (`uses_mock_source_quality`, `uses_mock_claim_entailment`,
`uses_mock_compiler`, `uses_mock_cve_lite`) e il campo `notes` di
`mock_indicators` devono essere **sempre visibili**, non in tooltip e
non in footer di fine pagina. Quando uno è `true`, l'asse
corrispondente è in modalità mock e la UI deve dichiararlo accanto al
valore — per esempio una pillola "mock" accanto al titolo del card o
una stripe esplicativa.

---

## 4. Contratto backend principale

### 4.1 Endpoint primario consumato dalla UI MVP

```
GET /api/v1/tasks/{task_id}/anti-hallucination-report
```

Fonte: `apps/api/app/routes/anti_hallucination_report.py` (commit
`39cdb57`).

Shape attesa (top-level, da `PHASE_8_8B_REPORT_PRE.md §4` e dal codice
reale `_TOP_LEVEL_REPORT_KEYS` in
`tests/test_phase_8_8b_report_flow.py`):

- `task_id`, `project_id`, `tenant_id` — stringhe UUID;
- `task` — `{status, objective, mode, created_at, updated_at}`;
- `publication` — `{status, published_answer_id,
  published_answer_status, summary_text, content_hash,
  final_gate_report_id}`;
- `gate` — `{decision, reason_code, payload, coverage_gaps[]}`;
- `claims[]` — uno per `logical_claim` del task, ognuno con
  `logical_claim_id`, `latest_entry_id`, `latest_state`,
  `canonical_claim_text`, `claim_type` (oggi `null` in MVP-0),
  `support_scope`, `evidence_links[]`, `cve_lite[]`,
  `source_quality[]`, `entailment[]`;
- `evidence[]` — uno per `evidence_span` task-attached, con quote,
  quote_hash, document_id, document_filename;
- `axis_summary` — `{cve_lite, source_quality, claim_entailment,
  final_gate}` con counters;
- `mock_indicators` — `{uses_mock_source_quality,
  uses_mock_claim_entailment, uses_mock_compiler, uses_mock_cve_lite,
  notes[]}`;
- `limitations[]` — lista di stringhe testuali sempre presente.

### 4.2 Perché il report aggregato è il contratto principale

Tre motivi tecnici, uno strategico:

1. **Riduzione superficie API.** Senza il report, la UI dovrebbe
   orchestrare almeno 6 endpoint (task, draft, gate, claims,
   source-quality, claim-entailment) e tre semantiche di "latest"
   diverse (vedi `PHASE_8_8B_REPORT_PRE.md §11`). Con il report ne
   consuma uno solo per la vista riassuntiva.
2. **Coerenza latest.** Il report usa il `latest assoluto DB-level`
   per target/pair, coerente col Gate. Gli endpoint specialistici
   (8.7F, 8.8A-READ-A) usano semantiche diverse, valide ma
   facilmente confuse da un consumer naive.
3. **Decorazioni utili.** Il report aggiunge l'`axis` derivato sui
   coverage gaps, ordina severity-first, e produce
   `axis_summary` + `mock_indicators` già pronti.
4. **Strategico (vedi `PROJECT_STATE.md` §Prossimo passo):** la
   decisione architetturale è già stata presa. La UI MVP è il primo
   consumer e la sua adozione del report stabilizza il contratto.

### 4.3 Endpoint specialistici come superficie secondaria

Gli endpoint specialistici esistono e restano fonte di verità, ma in
UI-REPORT-A/UI-REPORT-B vengono **soltanto referenziati** (link o
"open in raw" leggibile), non orchestrati. Il loro consumo è
**rinviato** a:

- UI-CLAIM-DETAIL (futuro): drill-down su un singolo `logical_claim`,
  consumando `/claims/{id}/history` e `/claims/{id}/evidence`.
- UI-AUDIT (futuro): vista audit chain consumando
  `/tasks/{id}/audit`.
- UI-SOURCE-LOSS (futuro): vista lifecycle/source-loss consumando le
  4 superfici 8.6.

Per UI-REPORT-A/B nessuno di questi è in scope.

### 4.4 Convenzione errori

Tutti gli endpoint backend usano l'envelope normalizzato
`{"error": {"code", "message", "details", ...}}`. La UI deve
riconoscere almeno:

- `RESOURCE_NOT_FOUND` con `details.resource="task_masters"` →
  "Task not found";
- `RESOURCE_NOT_FOUND` con `details.resource="published_answers"`
  → "Task has no published answer yet" (per drill-down futuri);
- altri codici → "Error <code>: <message>" + collapsible raw envelope.

La UI **non deve mai** mascherare un errore con stato vuoto
silenzioso: deve sempre mostrare almeno il `code` ricevuto.

---

## 5. Stato reale frontend e gap legacy

Riferimenti puntuali allo stato del repository al commit `39cdb57`.

### 5.1 Gap 1 — home wording pre-8.2

`apps/web/app/page.tsx` contiene il testo:

> "Phase 8.1b: API + Worker + Web stub. No real document upload yet
> (Phase 8.2)."

Questo wording è **obsoleto** post-8.8B-REPORT. UI-PRE annota il
gap; **non lo corregge** in questo blocco. La correzione è scope di
**UI-HOME-DIAGNOSTIC**.

### 5.2 Gap 2 — `/diagnostic` chiama una route inesistente

`apps/web/app/diagnostic/page.tsx` chiama
`http://localhost:${WEB_PORT}/api/proxy-health` con `cache: "no-store"`.
La directory `apps/web/app/api/` **non esiste** al commit `39cdb57`,
quindi la pagina in produzione restituirebbe `404` o un errore di
fetch. UI-PRE annota il gap; **non lo corregge** in questo blocco.

La correzione (scope di **UI-HOME-DIAGNOSTIC**) ha tre opzioni
implementative, non vincolate da questo PRE:

1. **Creare la route BFF** `apps/web/app/api/proxy-health/route.ts`
   che proxy verso l'API backend (`GET /health/ready` o equivalente).
   Pro: la pagina `/diagnostic` resta server-side rendered e il
   browser non vede direttamente il backend; Con: introduce un nuovo
   layer da mantenere.
2. **Chiamare direttamente l'API backend** rimuovendo la chiamata a
   `/api/proxy-health` e usando
   `NEXT_PUBLIC_API_BASE_URL`. Pro: meno superficie; Con: espone CORS
   verso il browser.
3. **Rimuovere temporaneamente la dipendenza** mostrando un placeholder
   ("Diagnostic disabled in MVP-0 UI viewer"). Pro: zero rischio;
   Con: regressione UX rispetto al 8.1b.

La scelta operativa sarà fatta in UI-HOME-DIAGNOSTIC, non qui.

### 5.3 Gap 3 — assenza di `apps/web/tsconfig.json`

Non esiste un `tsconfig.json` esplicito in `apps/web/`. `Next.js 15` lo
genera automaticamente al primo build, ma per ergonomia di
sviluppo (e per IDE come VS Code) la sua introduzione esplicita è
necessaria. UI-PRE annota il gap; la creazione di `tsconfig.json`
**fa parte di UI-REPORT-A** (perché UI-REPORT-A introduce TypeScript
reale con tipi del report, non solo `.tsx` di superficie).

### 5.4 Gap 4 — assenza di `apps/web/app/api/*`

La directory delle route API/BFF di Next.js non esiste. UI-PRE
**non** la crea. La sua eventuale creazione è scope di
UI-HOME-DIAGNOSTIC (opzione 1 della §5.2) **se** quella opzione viene
scelta. UI-REPORT-A **non** richiede `app/api/*`: il client API
chiama direttamente il backend tramite `NEXT_PUBLIC_API_BASE_URL`
(vedi §9).

### 5.5 Gap 5 — assenza di test frontend

`apps/web/vitest.config.ts` punta a `tests/**/*.test.{ts,tsx}` ma
non esistono test reali. UI-PRE annota; UI-REPORT-A introduce i
primi test (vedi §12).

### 5.6 Gap 6 — assenza di client API tipizzato

Nessun file `apps/web/lib/api.ts` o `apps/web/lib/reportTypes.ts`
esiste. Tutta la struttura `apps/web/lib/`, `apps/web/components/`,
`apps/web/app/tasks/` deve essere creata da zero. UI-PRE propone la
struttura (§7, §8); UI-REPORT-A la implementa.

### 5.7 Gestione dei gap

I gap 1, 2, 4 sono **legacy noti**. UI-PRE li registra qui e li
mappa esplicitamente a blocchi successivi (UI-HOME-DIAGNOSTIC). I
gap 3, 5, 6 sono **assenze attese** del frontend stub e vengono
risolti naturalmente dai blocchi UI-REPORT-A/UI-REPORT-B. Nessun gap
viene corretto in UI-PRE.

---

## 6. Information architecture

### 6.1 Tre viste, una superficie primaria

| Vista | Route proposta | Stato in UI MVP |
|---|---|---|
| Home | `/` | UI-HOME-DIAGNOSTIC: aggiornata, con link a `/tasks` e `/diagnostic` |
| Diagnostic | `/diagnostic` | UI-HOME-DIAGNOSTIC: fixed |
| Task list / lookup | `/tasks` | UI-HOME-DIAGNOSTIC: minimale, input task_id |
| **Task report (vista primaria)** | `/tasks/[taskId]/report` | **UI-REPORT-A + UI-REPORT-B** |
| Task create | `/tasks/new` | UI-CREATE-FLOW (rinviato) |
| Project create | `/projects/new` | UI-CREATE-FLOW (rinviato) |
| Document upload | `/projects/[projectId]/documents/new` | UI-CREATE-FLOW (rinviato) |

### 6.2 Information density della vista primaria

La vista `/tasks/[taskId]/report` è organizzata in **sezioni
verticalmente ordinate**, ognuna corrispondente a una sezione del
response. L'ordine è:

1. **Header** (task identity + status badge).
2. **Publication panel** (publication.status + dettagli published_answer).
3. **Gate panel** (decision + reason_code + disclaimer).
4. **Axis summary cards** (4 card: CVE-lite, Source Quality, Claim
   Entailment, Final Gate).
5. **Coverage gaps panel** (lista ordinata severity-first).
6. **Claims panel** (uno per `logical_claim`, espandibili).
7. **Evidence panel** (lista evidence, in ordering deterministico
   backend).
8. **Mock indicators panel** (sempre visibile, in alto del fold
   inferiore).
9. **Limitations panel** (sempre visibile).
10. **Raw JSON** (collapsible, default chiuso).

UI-REPORT-A copre §1-§4 + §8 + §9 + §10. UI-REPORT-B aggiunge §5, §6, §7.

### 6.3 Non si introduce navigazione laterale

UI MVP **non** introduce sidebar, menu di navigazione persistente,
breadcrumbs complessi. La home ha tre link, il report è una pagina
verticale leggibile. Navigazione laterale arriverà solo se UI-CREATE-FLOW
richiederà più tab persistenti.

---

## 7. Route plan

Proposta Next.js App Router al commit corrente (15.0.3, app router già
in uso). Tutti i path sono `apps/web/`-relative.

```
apps/web/
├── app/
│   ├── page.tsx                            (esistente; aggiornare in UI-HOME-DIAGNOSTIC)
│   ├── layout.tsx                          (introdurre se non esiste; UI-REPORT-A)
│   ├── diagnostic/
│   │   └── page.tsx                        (esistente; fix in UI-HOME-DIAGNOSTIC)
│   ├── tasks/
│   │   ├── page.tsx                        (UI-HOME-DIAGNOSTIC: task id input)
│   │   └── [taskId]/
│   │       └── report/
│   │           └── page.tsx                (UI-REPORT-A: vista primaria)
│   └── (api/proxy-health/route.ts)         (eventuale; UI-HOME-DIAGNOSTIC se scelta opzione 1)
├── components/
│   ├── ReportStatusBadge.tsx               (UI-REPORT-A)
│   ├── PublicationPanel.tsx                (UI-REPORT-A)
│   ├── GatePanel.tsx                       (UI-REPORT-A)
│   ├── AxisSummaryCards.tsx                (UI-REPORT-A)
│   ├── CoverageGapsPanel.tsx               (UI-REPORT-B)
│   ├── ClaimsPanel.tsx                     (UI-REPORT-B)
│   ├── ClaimCard.tsx                       (UI-REPORT-B)
│   ├── EvidencePanel.tsx                   (UI-REPORT-B)
│   ├── MockIndicatorsPanel.tsx             (UI-REPORT-A)
│   ├── LimitationsPanel.tsx                (UI-REPORT-A)
│   └── RawJsonCollapsible.tsx              (UI-REPORT-A)
├── lib/
│   ├── api.ts                              (UI-REPORT-A: client minimale fetch)
│   ├── reportTypes.ts                      (UI-REPORT-A: TS types del response)
│   └── reportFormatting.ts                 (UI-REPORT-A: helpers di rendering)
├── tests/
│   ├── ReportStatusBadge.test.tsx          (UI-REPORT-A)
│   ├── report-page.test.tsx                (UI-REPORT-A: rendering with mocked fetch)
│   ├── report-not-ready.test.tsx           (UI-REPORT-A)
│   ├── report-publication-held.test.tsx    (UI-REPORT-A)
│   ├── report-coverage-gaps.test.tsx       (UI-REPORT-B)
│   ├── report-mock-indicators.test.tsx     (UI-REPORT-A)
│   └── no-misleading-labels.test.tsx       (UI-REPORT-A)
├── tsconfig.json                           (UI-REPORT-A: introdurre)
├── vitest.config.ts                        (esistente, eventualmente esteso)
├── package.json                            (esistente)
└── README.md                               (esistente; aggiornare in UI-REPORT-B o UI-HOME-DIAGNOSTIC)
```

Se la review preferisce una struttura alternativa (`src/` layout,
co-location `app/tasks/[taskId]/report/components/`), questa è
ammissibile a patto che:

- ogni sezione del report resti un componente isolato e testabile;
- nessun componente venga riusato fuori contesto del report (per ora);
- nessuna dipendenza venga introdotta oltre quelle già in
  `package.json` (eccetto eventualmente `clsx` o equivalente
  minimale, ma vedi §11 — sconsigliato).

---

## 8. Component architecture

Specifica funzionale dei componenti, **non implementativa**. Tutti
read-only, tutti server component (Next.js App Router) salvo dove
indicato.

### 8.1 ReportStatusBadge

Input: `status: PublicationStatus` (uno di `published`, `withdrawn`,
`superseded`, `publication_held`, `not_ready`, `failed`, `unknown`).

Output: pillola visiva con etichetta testuale. Nessuna icona di "spunta
verde" per `published` (per evitare connotazione di "true"). Vedi §11.

### 8.2 PublicationPanel

Input: `publication: PublicationSection` (top-level del response).

Render:
- `status` come `ReportStatusBadge`;
- `published_answer_id` e `published_answer_status` se presenti;
- `content_hash` (utile, breve, monospace);
- `final_gate_report_id` come stringa UUID;
- disclaimer breve sotto: "Publication status is ledger-level, not
  truth-level."

### 8.3 GatePanel

Input: `gate: GateSection` (esclusi `coverage_gaps`, gestiti separatamente).

Render:
- `decision` come label;
- `reason_code` come label codice (monospace, con tooltip o glossario);
- disclaimer: "Decision read from persisted Final Gate report; not
  recomputed by UI.";
- `payload` non renderizzato qui; visibile solo via Raw JSON
  collapsible.

Nota: la UI **non deve** parafrasare `reason_code` in modo che cambi
semantica. Es. `all_spans_verified_with_warnings` può essere etichettato
"Approved with warnings", ma la stringa originale del backend deve
restare visibile.

### 8.4 AxisSummaryCards

Input: `axis_summary: AxisSummary`.

Render: 4 card affiancate (responsive: stack verticale su mobile):

1. **CVE-lite card.** Mostra `verified_claims_count`,
   `unverified_claims_count`, `inconclusive_count`. Chip "mock" se
   `mock_indicators.uses_mock_cve_lite=true`.
2. **Source Quality card.** Mostra i 5 counters di `overall_quality` +
   `missing_count`. Chip "mock" se applicabile. Disclaimer breve:
   "Quality of the source hosting the quote. Does not measure claim
   truth."
3. **Claim Entailment card.** Mostra i 5 counters di `verdict` +
   `missing_count`. Chip "mock". Disclaimer: "Local relation
   claim ↔ quote. 'Entailed' does not mean true."
4. **Final Gate card.** Mostra `has_blocking_gaps`, `has_warnings`,
   `blocking_gap_count`, `warning_gap_count`. Nessun chip mock (il
   Gate non è "mock" nel senso degli altri assi: è una policy
   versionata).

**Vincolo:** nessuna card mostra un singolo score aggregato dei 4
assi. La UI MVP **non** somma counters tra assi diversi.

### 8.5 CoverageGapsPanel

Input: `coverage_gaps: CoverageGap[]` (già ordinati severity-first
dal backend).

Render: lista flat o tabella con colonne `severity`, `axis`, `kind`,
`gap_key`, e dettagli collapsible (`details` JSONB verbatim).

Severity badge con tre stati: `block` / `warn` / `info`. Vedi §11
per i colori.

### 8.6 ClaimsPanel + ClaimCard

Input: `claims: Claim[]`.

Render: lista di `ClaimCard`, uno per `logical_claim`. Ogni card:

- header: `logical_claim_id` (breve), `latest_state` (badge),
  `canonical_claim_text`;
- disclaimer riga: "Claim status is ledger state, not truth
  guarantee.";
- sub-sezione `evidence_links` (lista `evidence_span_id` + role);
- sub-sezione `cve_lite` (per ogni record: `check_name`, `outcome`,
  `verification_record_id`);
- sub-sezione `source_quality` (per ogni slot: `evidence_span_id`,
  `overall_quality` o "missing", `contradiction_status`, chip
  "mock");
- sub-sezione `entailment` (per ogni slot: `evidence_span_id`,
  `verdict` o "missing", `confidence`, chip "mock");
- bottone "Show raw" per espandere il claim object intero come
  JSON.

I 4 assi sub-sezione devono essere visivamente separati. Non
collassare in un'unica riga: la chiarezza dell'ortogonalità è il
punto.

### 8.7 EvidencePanel

Input: `evidence: EvidenceItem[]`.

Render: lista con quote, document filename, quote_hash (monospace,
breve), evidence_span_id collapsible.

**Quote rendering:** la UI può troncare visivamente quote molto
lunghe ma deve sempre offrire "show full" per ottenere il testo
verbatim del backend.

### 8.8 MockIndicatorsPanel

Input: `mock_indicators: MockIndicators`.

Render: card o banner persistente con i 4 flag booleani come pillole
"mock"/"real" e la lista `notes`. Visibilità: **sempre on**, mai
nascosto sotto un click.

Se uno qualsiasi dei flag è `true`, la UI deve mostrare un banner
informativo in alto della pagina: "This task ran on mock evaluator(s).
See Mock indicators below."

### 8.9 LimitationsPanel

Input: `limitations: string[]`.

Render: lista (ul) di stringhe verbatim, con titolo "Limitations".
Visibilità: **sempre on**, non collapsible (o, se collapsible, default
expanded).

### 8.10 RawJsonCollapsible

Input: l'intero response del report.

Render: `<details>` HTML nativo, chiuso di default, con `<pre><code>`
del JSON formattato (2-space indent). Utile in dev mode; nascosto ma
non rimosso. Per UI-REPORT-A è sufficiente un `<details>` nativo
senza syntax highlighter.

---

## 9. Data fetching and error handling

### 9.1 Client API

Un solo helper minimale, da introdurre in UI-REPORT-A:

```
// apps/web/lib/api.ts (proposta)
// non scrivere in UI-PRE
const BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export async function getAntiHallucinationReport(taskId: string): Promise<...> {
  const res = await fetch(`${BASE}/api/v1/tasks/${taskId}/anti-hallucination-report`, {
    cache: "no-store",
  });
  if (!res.ok) throw new ApiError(res.status, await res.json());
  return res.json();
}
```

### 9.2 Variabile ambiente

- `NEXT_PUBLIC_API_BASE_URL` proposta come fonte unica del backend
  URL.
- Fallback dev: `http://localhost:8000`.
- Non introdurre runtime config server-side per ora: il client è dev
  mode, server component fa fetch verso un URL deterministico.

### 9.3 Mapping errori → rendering

| HTTP | Backend code | Backend details.resource | UI rendering |
|---|---|---|---|
| 200 | (none) | — | report renderizzato normalmente |
| 200 con publication.status='not_ready' | — | — | banner "Report not ready yet" + report parziale (la UI **non** tratta `not_ready` come errore) |
| 200 con publication.status='publication_held' | — | — | banner severity warn "Publication held" + report completo (la UI **non** tratta `publication_held` come errore) |
| 200 con publication.status='unknown' | — | — | banner difensivo "Publication status unknown" |
| 404 | `RESOURCE_NOT_FOUND` | `task_masters` | full-page "Task not found" |
| 404 | `RESOURCE_NOT_FOUND` | altro | mostrare `details.resource` letteralmente |
| 5xx | qualunque | — | full-page "Server error" + raw envelope collapsible |
| network error | — | — | full-page "API unreachable" + endpoint URL visibile |

### 9.4 Caching

- `cache: "no-store"` sul fetch verso il report. Il report è una
  vista derivata di stato che può cambiare per effetto del worker
  asincrono, e una cache stale produrrebbe confusione.
- Nessun caching aggressivo lato Next.js (no `revalidate`).
- **Nessun polling automatico in UI-REPORT-A.** Refresh è manuale
  (bottone "Refresh" o reload pagina).
- Eventuale polling esplicito è scope di **UI-CREATE-FLOW** (dove
  serve seguire la transizione `not_ready → published/publication_held`).
  Quando introdotto, deve essere:
  - opt-in dal click utente o automatico solo finché il task è in
    stato terminale `not_ready`;
  - limitato (es. 1 chiamata ogni 3 secondi, stop dopo
    publication.status terminale: `published`, `publication_held`,
    `failed`, `withdrawn`, `superseded`);
  - cancellabile dall'utente.

### 9.5 `/diagnostic` rispetto al data fetching

Il fix di `/diagnostic` è scope di **UI-HOME-DIAGNOSTIC**, non
UI-REPORT-A. La pagina attuale chiama `/api/proxy-health` ma la
route non esiste. UI-PRE registra: la correzione deve scegliere una
delle tre opzioni di §5.2 e implementarla coerentemente. Non
introdurre `app/api/*` in UI-REPORT-A se non strettamente necessario
per il report (e non lo è).

---

## 10. Report rendering semantics

Per sezione, una specifica leggibile di cosa va mostrato e come.

### 10.1 Header

- `task_id` in alto, monospace, copy-button opzionale;
- `project_id` come testo secondario;
- `task.status` come pillola (codomio: `created`, `analyzing`,
  `analyzed_partial`, `compiling`, `published`, `blocked`, `failed`,
  `cancelled`, `archived`);
- `task.mode` ("closed_corpus") come chip neutro;
- `task.objective` come paragrafo (può essere lungo);
- timestamps `created_at` / `updated_at` in formato leggibile + ISO
  tooltip.

### 10.2 Publication panel

- `publication.status` come `ReportStatusBadge` (§8.1);
- se `status='published'`, mostrare anche `published_answer_id`,
  `content_hash` (breve), `final_gate_report_id`;
- se `status='withdrawn'` o `'superseded'`, etichettarli
  ESPLICITAMENTE come stati lifecycle (non flatten a "published"). Il
  backend **non** li flatten; la UI deve rispettare la stessa scelta;
- se `status='publication_held'`, etichettare "Held — see blocking
  gaps below" con un link in-page alla `CoverageGapsPanel`;
- se `status='not_ready'`, etichettare "Pending — task has not reached
  the gate yet";
- se `status='failed'`, etichettare "Task failed" con suggerimento
  di guardare `/tasks/{id}/audit`;
- se `status='unknown'`, etichettare "Status unknown (defensive
  fallback)" — l'occorrenza di `unknown` è un segnale di
  inconsistenza DB che merita attenzione operatore.

### 10.3 Gate panel

- `gate.decision` come label: `approved`, `rejected`, o `null`
  (rendered as "no gate report yet");
- `gate.reason_code` come codice monospace + label umana opzionale
  (`all_spans_verified` → "All spans verified",
  `all_spans_verified_with_warnings` → "Approved with warnings",
  `no_verified_claims` → "No verified claims",
  `unverified_spans_present` → "Unverified spans present",
  `entailment_block` → "Entailment block",
  `source_quality_block` → "Source quality block");
- disclaimer fisso: "Decision read from persisted Final Gate report;
  not recomputed by UI.";
- nessun rendering del `payload` qui (sta in raw JSON).

### 10.4 Axis summary

4 card, descritte in §8.4. Vincoli espliciti:

- nessuna card mostra una percentuale aggregata cross-axis;
- nessuna card mostra un singolo score di "trust";
- ogni card mostra il nome dell'asse, i counters codomain-completi
  (cioè TUTTI i bucket, anche se a 0), e — se applicabile — il chip
  "mock";
- la card Final Gate è **derivata** dai coverage gaps, non un asse
  parallelo; la UI può etichettarla "Gate outcome (derived)" per
  enfatizzare la distinzione.

### 10.5 Coverage gaps

Tabella o lista, ordinata come ricevuta dal backend
(severity-first → created_at ASC → id ASC). Per ogni gap:

- severity badge (`block`/`warn`/`info`);
- axis chip (`cve_lite`, `source_quality`, `claim_entailment`,
  `coverage`, `source_loss`, `other`);
- kind code (monospace);
- gap_key (monospace, breve);
- created_at (ISO, leggibile);
- details collapsible (JSON verbatim, default chiuso).

Se la lista è vuota e `gate.decision='approved'`, mostrare un
messaggio sobrio: "No coverage gaps." Se vuota e `gate.decision='null'`,
mostrare: "No gate report yet."

### 10.6 Claims

Vedi §8.6. Ulteriori vincoli rendering:

- l'ordering dei claim è quello del backend (`created_at ASC,
  id ASC`). La UI **non** riordina;
- gli evidence_links sono uno per `(latest_entry, evidence_span)`;
  l'ordering è quello del backend;
- gli slot `source_quality` e `entailment` con `latest_*_id=null`
  vanno etichettati esplicitamente "missing" con uno stato visivo
  neutro (non rosso, non "error" — è una mancanza legittima per task
  pre-8.7E / pre-8.8A o per pair non valutate);
- se nessun claim è presente (es. `claims=[]`), mostrare "No claims
  yet" e — se applicabile — un suggerimento "Task may still be in
  analyzing state."

### 10.7 Evidence

Vedi §8.7. Quote sempre renderizzata verbatim (eventualmente
troncata con "show full"). Quote hash visibile come monospace breve
(primi 12 caratteri + tooltip con hash completo) — utile per
verifiche CVE-lite manuali.

### 10.8 Mock indicators

Sempre visibile. Mai sotto un click. Se uno dei flag è `true`, banner
in alto della pagina (sopra il publication panel) con testo:

> "This task ran on mock evaluator(s). Output is for system
> validation only and does not reflect production-quality
> evidence-gating."

### 10.9 Limitations

Sempre visibile. Bullet list. Verbatim dal response (la UI **non
deve** riscrivere o parafrasare le frasi del backend).

### 10.10 Raw JSON

`<details>` nativo, chiuso di default. Utile in dev mode. Non
default primary UI ma sempre presente.

---

## 11. Visual language

Specifica leggera, **non** un design system. Tutto motivato dalla
costrizione di non introdurre librerie UI esterne in MVP-0.

### 11.1 Stato della libreria

`apps/web/package.json` al commit `39cdb57` non include Tailwind,
nessun design system, nessuna UI library. La UI MVP **rispetta** questa
scelta: introduzione di Tailwind/shadcn/Mui/Chakra è **fuori scope** e
va motivata in un blocco dedicato (UI-DESIGN-SYSTEM-PRE) se mai
diventerà necessaria.

Soluzione raccomandata: **CSS modules** o inline minimal in JSX. Già
oggi `apps/web/app/diagnostic/page.tsx` usa inline style su elementi
HTML. UI-REPORT-A può continuare con questo pattern, eventualmente
spostandosi a CSS modules quando il numero di stili cresce.

### 11.2 Tassonomia colori (raccomandazione, non vincolante)

Senza dover scegliere una palette definitiva (non è scope di
UI-PRE), si raccomanda:

| Concetto | Colore semantico raccomandato |
|---|---|
| Published / OK / approved | verde scuro / muted green (non saturo, non "celebratorio") |
| Held / blocked / rejected | rosso / arancione caldo |
| Warning | giallo / ambra |
| Info / neutral | grigio / blu freddo |
| Not ready / unknown | grigio chiaro |
| Mock | viola muted (per distinguerlo dal "real") |

**Vincolo:** evitare verde brillante o icone di "checkmark trionfali"
per `published`. La promessa anti-allucinazione richiede sobrietà,
non celebrazione.

### 11.3 Tipografia

System font stack è sufficiente. Monospace per UUID, hash, codici
backend (es. `reason_code`, `kind`, `check_name`). Non introdurre
custom font.

### 11.4 Layout

Container max-width fisso (es. 1200px), padding laterale generoso,
sezioni con margine verticale netto. No multi-column ambizioso. La
densità informativa è alta ma la lettura deve restare lineare.

### 11.5 Accessibilità (vedi §13)

Contrasto AA minimo per i badge. Le pillole status devono avere
testo leggibile, non solo colore (non basarsi solo sul colore per
trasmettere semantica — vedi §13).

### 11.6 Se proporre Tailwind?

No, non in UI-REPORT-A né UI-REPORT-B. Motivazione:

- Tailwind richiede build pipeline + configurazione iniziale;
- il valore aggiunto per 8-10 componenti read-only è basso;
- la library non vincola design system, quindi non sblocca
  riusabilità reale;
- introduzione di Tailwind è un blocco a sé (UI-TAILWIND o
  UI-DESIGN-SYSTEM-PRE) se mai diventa necessario.

Stesso vale per shadcn/ui, Radix, Mui, Chakra.

---

## 12. Testing strategy

Pianificazione test, **da scrivere** in UI-REPORT-A/UI-REPORT-B, non in
questo blocco. Vitest + Testing Library, coerente con
`apps/web/package.json` (vitest 2.1.4 + @testing-library/react 16.0.1
+ jsdom 25.0.1 già presenti).

### 12.1 Test rendering — happy paths

- `report-page.test.tsx`: render del report `publication.status='published'`
  con claims/evidence non vuoti, axis_summary coerente; verifica
  rendering di header, publication panel, gate panel, axis cards,
  claims panel, evidence panel, mock indicators, limitations.
- `report-not-ready.test.tsx`: render del report con
  `publication.status='not_ready'`, claims=[], evidence=[]; verifica
  banner "Report not ready yet", absence di publication details,
  mock indicators fallback presente.
- `report-publication-held.test.tsx`: render del report con
  `publication.status='publication_held'`, gate rejected,
  coverage_gaps non vuoto; verifica rendering severity-first dei
  gaps, banner "Publication held", presenza del link in-page ai gaps.

### 12.2 Test errori API

- `report-404.test.tsx`: mock fetch che risponde 404 con envelope
  `RESOURCE_NOT_FOUND` + `details.resource="task_masters"`; verifica
  rendering "Task not found".
- `report-api-unreachable.test.tsx`: mock fetch che lancia
  `TypeError` (network error); verifica rendering "API unreachable"
  + URL endpoint visibile.
- `report-5xx.test.tsx`: mock fetch 500; verifica rendering server
  error + raw envelope.

### 12.3 Test semantici (anti-misleading)

- `no-misleading-labels.test.tsx`: dato un response valido (uno
  qualsiasi tra published/held/not_ready), il DOM finale NON deve
  contenere nessuna delle stringhe vietate elencate in §3.1:
  - "truth score";
  - "verified true";
  - "hallucination eliminated";
  - "AI verified";
  - "factually true";
  - "entailed = true" (come affermazione di verità);
  - "real NLI";
  - "contradiction detector" (oggi non esiste);
  - "citation-to-claim validator" (oggi non esiste).

  Implementazione attesa: render del componente + assertion su
  `document.body.textContent` con `.not.toContain(...)` per ogni
  stringa vietata.

### 12.4 Test componenti di asse

- `axis-summary-cards.test.tsx`: render delle 4 card; verifica che
  ogni codomain bucket sia presente anche se a 0; verifica chip
  "mock" quando `mock_indicators.uses_mock_*=true`.
- `coverage-gaps-panel.test.tsx`: render con gaps in ordine
  severity-first (input dal backend, verifica ordering preservato);
  verifica rendering del campo `axis`; verifica details collapsible.
- `mock-indicators-panel.test.tsx`: verifica che i 4 flag siano
  sempre visibili; verifica che `notes` siano sempre renderizzate.

### 12.5 Test che NON sono in scope di UI-REPORT-A/B

- end-to-end con backend reale (rinviato; richiede docker compose
  pre-UI test);
- visual regression (rinviato);
- accessibility automatizzata (axe) — raccomandata ma non
  obbligatoria in MVP-0;
- performance budget — non rilevante per MVP read-only.

### 12.6 Strategia fixture

Fixture JSON del response del report da mantenere in
`apps/web/tests/fixtures/`, una per ogni scenario rappresentativo:

- `report_published_warning.json`;
- `report_published_clean.json` (se mai produrribile);
- `report_publication_held_entailment_block.json`;
- `report_publication_held_source_quality_block.json`;
- `report_publication_held_no_verified_claims.json`;
- `report_not_ready_empty.json`;
- `report_not_ready_with_documents.json`;
- `report_withdrawn.json`;
- `report_superseded.json`;
- `report_failed.json`.

I JSON dovrebbero essere generati una tantum esercitando
`tests/test_phase_8_8b_report_flow.py` e copiando il response, oppure
costruiti manualmente seguendo `PHASE_8_8B_REPORT_PRE.md §4` e i 13
test API. **UI-PRE non genera le fixture**: è scope di UI-REPORT-A.

### 12.7 Test per `/diagnostic`

Non in scope UI-REPORT-A/B. Sono scope di **UI-HOME-DIAGNOSTIC**:

- `diagnostic-proxy-health-ok.test.tsx`;
- `diagnostic-proxy-health-fail.test.tsx`;
- `diagnostic-api-unreachable.test.tsx`.

---

## 13. Accessibility / usability baseline

Baseline minima MVP-0, non esaustiva.

### 13.1 Contrasto colore

- Tutti i badge status (Published, Held, Not ready, Failed, etc.)
  devono raggiungere WCAG AA (4.5:1 testo normale, 3:1 large text).
- Severity badges (block/warn/info) devono avere contrasto AA.
- Mock pill (viola muted o equivalente) deve essere distinguibile
  da "real".

### 13.2 Non solo colore

- Lo stato del severity badge **non** deve essere comunicato dal solo
  colore: aggiungere etichetta testuale ("BLOCK" / "WARN" / "INFO") o
  glyph testuale (es. "!" per block, "?" per info).
- Lo stato publication **non** deve essere comunicato solo da uno
  sfondo colorato: deve avere etichetta testuale leggibile.

### 13.3 Semantica HTML

- Header `<h1>` per il task id o il titolo della pagina;
- header `<h2>` per ogni sezione (Publication, Gate, Axis summary,
  Coverage gaps, Claims, Evidence, Mock indicators, Limitations,
  Raw JSON);
- liste `<ul>` per limitations, coverage gaps, claims, evidence;
- `<details>` HTML nativo per collapsible (no custom JS
  collapse — semplifica accessibilità e SSR);
- `<table>` solo se semanticamente tabella (coverage gaps può
  essere lista o tabella; entrambe accettabili).

### 13.4 Tastiera

- Tutti i `<details>` devono essere navigabili da tastiera (HTML
  nativo già lo è).
- Nessun custom dropdown o popover in MVP-0.

### 13.5 Screen reader

- Pillole status devono avere `aria-label` esplicito quando l'etichetta
  testuale è abbreviata (es. badge solo "OK" o emoji).
- Disclaimers non devono essere `aria-hidden`. Sono parte del contenuto.

### 13.6 Non in scope MVP-0

- Localizzazione (UI in inglese tecnico; i disclaimer dal backend
  possono essere italiani/inglesi misti — è ammesso in MVP-0);
- dark mode;
- responsive avanzato (mobile-first detail);
- print stylesheet.

---

## 14. Security and privacy caveats

### 14.1 Nessuna autenticazione reale

UI MVP gira in dev mode contro tenant 'dev'. Non c'è login, non c'è
RBAC. Conseguenze:

- la UI **non deve** assumere user identity nei suoi rendering;
- la UI **non deve** mostrare la frase "logged in as ..." o avatar;
- la UI **non deve** filtrare i dati per "tenant utente": il backend
  resolve `tenant 'dev'` come tenant unico.

### 14.2 Payload JSONB esposto verbatim

Il report espone `payload` e `details` JSONB verbatim. La UI **non
applica RBAC redaction** (debito noto del backend; vedi
`PHASE_8_8B_REPORT_PRE.md §10`). Conseguenze:

- UI MVP non è adatta a deploy esterno multi-tenant;
- documentare in README post-MVP-0 il debito;
- raw JSON collapsible è esplicitamente un meccanismo dev;
  un futuro UI-RBAC dovrà nasconderlo per ruoli non-admin.

### 14.3 CORS / endpoint base URL

Se la UI chiama il backend direttamente (no BFF — opzione 2 della
§5.2), il backend deve esporre CORS abilitato per
`NEXT_PUBLIC_API_BASE_URL`. Questo è già il caso in dev su
`localhost:8000` ↔ `localhost:3000`? Va **verificato** in
UI-REPORT-A; se non è il caso, UI-REPORT-A può aggiungere una route
BFF minima (`apps/web/app/api/anti-hallucination-report/route.ts`)
solo per il report, senza affrontare il fix di `/diagnostic`.

### 14.4 Dati personali

Nessuno in MVP-0. I documenti caricati sono `.txt`/`.md` di test.
Nessun PII reale. La UI non introduce campi user-facing oltre quello
che il backend già espone.

### 14.5 Injection-safe rendering

- Quote testuale e canonical_claim_text vanno renderizzati come
  testo, non come HTML (React lo fa di default con `{value}`).
- **Non usare** `dangerouslySetInnerHTML` da nessuna parte.

---

## 15. Out of scope

Lista esplicita di ciò che è **fuori scope** di UI-PRE e dei blocchi
UI-REPORT-A/UI-REPORT-B/UI-HOME-DIAGNOSTIC:

- auth/RBAC reale;
- multi-tenant selector reale;
- role-based redaction sui payload JSONB;
- editing di task / document / claim / gate report;
- deleting di qualsiasi entità;
- source-loss lifecycle UI dettagliata (rinviata a UI-LIFECYCLE);
- published-answer-level report (`/published-answers/{id}/report`)
  perché l'endpoint non esiste in v1;
- real-time websocket / SSE;
- worker progress live stream;
- background polling aggressivo (vedi §9.4 per la nota su polling
  esplicito in UI-CREATE-FLOW);
- export PDF/HTML/DOCX/JSON-LD;
- chart sofisticati (timeseries, treemap, sunburst, sankey, ecc.);
- dashboard globale multi-task;
- claim-level entailment endpoint dedicato (non esiste in v1, scope
  di 8.8A-READ-B);
- Citation-to-Claim Validator (8.8B storico, non implementato);
- Contradiction Detector reale (8.8C, non implementato);
- NLI reale / LLM reale;
- external web verification;
- implementazione del fix di `/diagnostic` (scope di
  UI-HOME-DIAGNOSTIC);
- creazione di `apps/web/app/api/*` (scope di UI-HOME-DIAGNOSTIC se
  opzione 1 della §5.2);
- creazione di `apps/web/tsconfig.json` (scope di UI-REPORT-A);
- introduzione di Tailwind / shadcn / Mui / Chakra (scope di un
  futuro UI-DESIGN-SYSTEM-PRE);
- ridenominazione delle stringhe di `reason_code` dal backend;
- riscrittura di `limitations` o `mock_indicators.notes`;
- modifiche all'endpoint `/api/v1/tasks/{task_id}/anti-hallucination-report`;
- modifiche al backend.

---

## 16. Implementation sequence

Sequenza rigorosa post-UI-PRE. Ogni blocco è piccolo, indipendente e
testabile.

### 16.1 UI-REPORT-A — Report viewer skeleton

Scope:

- introdurre `apps/web/tsconfig.json` se non viene auto-generato;
- introdurre `apps/web/lib/api.ts` con `getAntiHallucinationReport`;
- introdurre `apps/web/lib/reportTypes.ts` con TypeScript types
  inline (no shared schema, no client-side validation con Pydantic
  equivalents — solo TS interfaces basate su
  `PHASE_8_8B_REPORT_PRE.md §4` e su
  `apps/api/app/routes/anti_hallucination_report.py`);
- introdurre `apps/web/lib/reportFormatting.ts` con helper per
  rendering;
- introdurre route `apps/web/app/tasks/[taskId]/report/page.tsx`;
- introdurre componenti core: `ReportStatusBadge`, `PublicationPanel`,
  `GatePanel`, `AxisSummaryCards`, `MockIndicatorsPanel`,
  `LimitationsPanel`, `RawJsonCollapsible`;
- tests Vitest con fetch mockato per: published warning,
  not_ready empty, 404 task, 5xx, no-misleading-labels;
- **no** project creation, **no** upload, **no** task creation,
  **no** claims/evidence rendering, **no** coverage gaps rendering;
- **no** modifiche backend.

Acceptance criteria UI-REPORT-A → vedi §17.

### 16.2 UI-REPORT-B — Claims, evidence, coverage gaps

Scope:

- introdurre componenti: `CoverageGapsPanel`, `ClaimsPanel`,
  `ClaimCard`, `EvidencePanel`;
- estendere `apps/web/app/tasks/[taskId]/report/page.tsx` con le 3
  nuove sezioni;
- aggiungere test fixture: publication_held entailment_block,
  publication_held source_quality_block, no_verified_claims;
- aggiungere test su severity-first ordering dei coverage gaps;
- aggiungere test su slot "missing" per source_quality e entailment;
- aggiungere test su "Show raw" del claim;
- **no** project creation, **no** upload;
- **no** modifiche backend.

### 16.3 UI-HOME-DIAGNOSTIC — Home wording fix + diagnostic fix +
task lookup

Scope:

- aggiornare `apps/web/app/page.tsx` con wording post-8.8B-REPORT
  (eliminare riferimento "Phase 8.1b ... no real document upload yet");
- decidere e implementare uno dei tre fix di `/diagnostic` (vedi
  §5.2);
- introdurre `apps/web/app/tasks/page.tsx` con input task_id +
  link a `/tasks/[taskId]/report`;
- tests minimal per il task lookup;
- **no** project/document/task creation;
- **no** modifiche backend.

### 16.4 UI-CREATE-FLOW-PRE — Progettazione flusso creazione

Scope: documento di progettazione (analogo a UI-PRE per il flusso
project → document → task → report). Definisce le rotte
`/projects/new`, `/projects/[id]/documents/new`, `/tasks/new`. Definisce
polling strategy per task non terminali. Definisce gestione errori
upload, FK validation. **No codice**.

### 16.5 UI-CREATE-FLOW-CODE — Implementazione

Scope: implementazione del flusso secondo UI-CREATE-FLOW-PRE. Solo
dopo che UI-REPORT-A/B sono stabili e accettati.

### 16.6 Ordine consigliato

1. UI-PRE (questo blocco) → done.
2. UI-REPORT-A.
3. UI-REPORT-B.
4. UI-HOME-DIAGNOSTIC.
5. UI-CREATE-FLOW-PRE.
6. UI-CREATE-FLOW-CODE.

Non saltare da UI-PRE a upload/task creation. Il viewer del report è
il fondamento.

---

## 17. Acceptance criteria for UI-REPORT-A

Il blocco UI-REPORT-A è accettabile se e solo se:

1. **`apps/web/app/tasks/[taskId]/report/page.tsx`** esiste,
   compila con `next build` senza errori, ed è un server component
   che chiama
   `GET /api/v1/tasks/{taskId}/anti-hallucination-report` tramite
   `apps/web/lib/api.ts`.

2. **`apps/web/lib/api.ts`** esporta una funzione
   `getAntiHallucinationReport(taskId: string)` che:
   - usa `NEXT_PUBLIC_API_BASE_URL` con fallback
     `http://localhost:8000`;
   - usa `cache: "no-store"`;
   - distingue 404 (RESOURCE_NOT_FOUND task_masters) da altri errori;
   - lancia eccezioni tipizzate o restituisce un union type
     `Report | ApiError`.

3. **`apps/web/lib/reportTypes.ts`** definisce TypeScript interfaces
   per il response del report, basate su
   `PHASE_8_8B_REPORT_PRE.md §4`. Niente codegen, niente Pydantic
   import. Solo interfaces TS.

4. **Componenti** `ReportStatusBadge`, `PublicationPanel`,
   `GatePanel`, `AxisSummaryCards`, `MockIndicatorsPanel`,
   `LimitationsPanel`, `RawJsonCollapsible` esistono e sono testati.

5. **Tests Vitest** passano con i seguenti scenari:
   - happy path published warning;
   - not_ready empty;
   - publication_held (anche senza coverage gaps panel
     completo, lo status badge va comunque renderizzato);
   - 404 task → "Task not found";
   - API unreachable → "API unreachable";
   - 5xx → "Server error" + raw envelope;
   - no-misleading-labels (vedi §12.3).

6. **Mock indicators** sono sempre visibili nel DOM finale (verificato
   da test), non sotto un click.

7. **Limitations** sono sempre visibili nel DOM finale, verbatim dal
   response.

8. **Raw JSON** è presente come `<details>` chiuso di default, con
   il response intero.

9. **Nessuna stringa vietata** appare nel DOM (vedi §3.1, §12.3).

10. **Nessuna modifica** a `apps/api/*`, `apps/worker/*`,
    `packages/shared/*`, migrations, README, PROJECT_STATE,
    PHASE_8_8B_REPORT_PRE.md.

11. **`apps/web/tsconfig.json`** è introdotto esplicitamente
    (anche se Next 15 può auto-generarlo, l'introduzione esplicita
    semplifica IDE).

12. **Nessuna libreria UI esterna** è introdotta (no Tailwind, no
    shadcn, no Mui, no Chakra, no Radix, no Lucide-react, no
    Recharts, etc.).

13. **`/diagnostic`** non è toccato da UI-REPORT-A. Il gap legacy
    resta documentato; il fix è scope di UI-HOME-DIAGNOSTIC.

14. **`/tasks` (lista o lookup)** non è introdotto da UI-REPORT-A:
    la sola route ammessa è `/tasks/[taskId]/report`. La home page
    non è modificata. L'accesso al report avviene via URL diretto.

---

## 18. Risks and mitigations

| Rischio | Probabilità | Impatto | Mitigazione |
|---|---|---|---|
| La UI introduce wording fuorviante ("verified true", "AI verified") | media | alto (rompe promessa anti-allucinazione) | test `no-misleading-labels.test.tsx` obbligatorio; review semantica in §3.1; checklist di vocabolario in §3.2 |
| La UI collassa gli assi in un singolo score | bassa | alto | vincolo esplicito in §8.4: nessuna card cross-axis; review architetturale |
| La UI tratta il report come autorità decisionale o ricalcola il Gate | bassa | alto | UI è strettamente read-only; nessuna funzione di "recompute" in `lib/api.ts`; disclaimer in §10.3 |
| La UI maschera errori 404 / 5xx con stati vuoti silenziosi | media | medio | mapping esplicito in §9.3; test 404 e 5xx obbligatori in §12.2 |
| La UI introduce Tailwind / shadcn senza motivazione | media | basso (rallenta) | divieto in §11.6; review in UI-REPORT-A acceptance |
| La UI fa polling aggressivo che sovraccarica il backend | bassa | basso | nessun polling in UI-REPORT-A/B; polling scope di UI-CREATE-FLOW con vincoli espliciti in §9.4 |
| `/diagnostic` non viene mai corretto e resta rotto | alta | basso (UI cosmetico) | UI-HOME-DIAGNOSTIC esplicitamente in roadmap §16.3 con tre opzioni motivate |
| La UI espone payload JSONB con dati sensibili a utenti non autorizzati | bassa in MVP-0 | medio in deploy futuro | RBAC è debito noto del backend; documentare in §14.2; nasconedere raw JSON in un futuro UI-RBAC |
| La UI fa CORS verso `localhost:8000` ma il backend non lo permette | media | medio | verificare in UI-REPORT-A; fallback a route BFF se necessario (§14.3) |
| La shape del report cambia (8.8B-REPORT v2 published-answer-level) | bassa nell'immediato | medio | wrapper inline in `lib/reportTypes.ts`, non shared; pronti a evolvere quando lo shared schema viene promosso |
| Il consumer naive confonde latest semantics tra report (DB-level), 8.7F (slice-level), 8.8A-READ-A (chronological) | media | medio | UI MVP consuma SOLO il report; gli endpoint specialistici non sono orchestrati; drill-down rinviato a UI futura |
| `claim_type` è null nel report (perché non persistito su logical_claims) | alta | basso | UI non assume non-null; render "claim_type unknown" o omette il campo |
| Banner `mock` viene ignorato dall'utente o nascosto da uno stile CSS sbagliato | media | alto (perde la promessa) | test esplicito che il banner sia presente nel DOM; mai sotto click |
| Le frasi di `limitations` arrivano in italiano (dal backend) e l'UI è in inglese | bassa | basso | accettato in MVP-0; localizzazione fuori scope |
| `apps/web/tsconfig.json` viene generato male da Next | bassa | basso | introduzione esplicita in UI-REPORT-A con paths espliciti |
| Manca un test backend che documenti la shape per il consumo UI | bassa | medio | i 13 test API + 2 realistic flow sono sufficienti; UI mantiene fixture indipendenti |

---

## FILE_COMPLETATI

- `PHASE_UI_PRE.md`

## FILE_NON_MODIFICATI

- `apps/web/*`
- `apps/api/*`
- `apps/worker/*`
- `packages/shared/*`
- `migrations/*`
- `tests/*`
- `README.md`
- `PROJECT_STATE.md`
- `PHASE_8_8B_REPORT_PRE.md`

## FILE_DA_FARE_PROSSIMO_BLOCCO

- **UI-REPORT-A**: introdurre `apps/web/tsconfig.json` esplicito,
  `apps/web/lib/api.ts`, `apps/web/lib/reportTypes.ts`,
  `apps/web/lib/reportFormatting.ts`, route
  `apps/web/app/tasks/[taskId]/report/page.tsx`, e i componenti
  `ReportStatusBadge`, `PublicationPanel`, `GatePanel`,
  `AxisSummaryCards`, `MockIndicatorsPanel`, `LimitationsPanel`,
  `RawJsonCollapsible`. Test Vitest con fetch mockato per happy
  path, not_ready, 404, API unreachable, 5xx,
  no-misleading-labels. Nessuna modifica backend. Nessuna creazione
  di entità dalla UI. Nessun fix di `/diagnostic`. Nessuna creazione
  di `app/api/*`.

## GAP_LEGACY_FRONTEND_ANNOTATI

- `apps/web/app/page.tsx` ha wording pre-8.2 ("Phase 8.1b: API +
  Worker + Web stub. No real document upload yet (Phase 8.2)."). Fix
  in **UI-HOME-DIAGNOSTIC**.
- `apps/web/app/diagnostic/page.tsx` chiama
  `/api/proxy-health` ma la route non esiste al commit `39cdb57`.
  Fix in **UI-HOME-DIAGNOSTIC** scegliendo una delle tre opzioni
  della §5.2 di questo documento.
- `apps/web/tsconfig.json` non esiste al commit `39cdb57`.
  Introduzione in **UI-REPORT-A**.
- `apps/web/app/api/*` non esiste al commit `39cdb57`. Eventuale
  introduzione **solo** se UI-HOME-DIAGNOSTIC sceglie l'opzione 1
  della §5.2.
- Nessun client API tipizzato, nessun `lib/`, nessun
  `components/`, nessun test reale frontend. Tutto introdotto da
  **UI-REPORT-A** in modo additivo.

## RISCHI_RESIDUI

- **Wording fuorviante** è il rischio principale; mitigato da
  test `no-misleading-labels.test.tsx` obbligatorio in UI-REPORT-A.
- **Confusione tra latest semantics** del report (DB-level) e degli
  endpoint specialistici (slice-level / chronological); mitigato
  dall'uso esclusivo del report in UI MVP.
- **Shape evolutions** del report (8.8B-REPORT v2 published-answer-level,
  promozione a shared schema) richiederanno aggiornamento di
  `lib/reportTypes.ts`; wrapper inline mantiene la flessibilità.
- **Banner mock** è un single point of failure semantico: se uno
  stile CSS lo nasconde inavvertitamente, la promessa
  anti-allucinazione si rompe. Mitigato da test esplicito.
- **CORS** verso `localhost:8000` va verificato in UI-REPORT-A; se
  non funziona, route BFF minima dedicata al report (NON a
  `/diagnostic`).
- **RBAC** rinviata; UI MVP non è production-grade per multi-tenant.
- **Provider AI reali, Contradiction Detector, NLI reale, Citation-to-Claim
  Validator, Final Answer Sentence Gate, External Verification / Web-RAG**
  ancora mancanti nel backend; quando arriveranno, la UI dovrà
  aggiornare i mock indicators flip da `true` a `false` automaticamente
  (il report lo fa già grazie alla derivazione da identità servizio
  + `payload.mock`).
- **`/diagnostic` rotto** è un rischio cosmetico ma visibile; mitigato
  dall'esistenza di UI-HOME-DIAGNOSTIC in roadmap.
- **No realistic backend per i test frontend**: i test Vitest mockano
  il fetch; non c'è verifica end-to-end UI ↔ API reale. Mitigato
  dall'esistenza di `tests/test_phase_8_8b_report_flow.py` che valida
  il contratto backend; smoke manuale via `curl` documentato in
  `README.md §Smoke test 8.8B-REPORT`.
