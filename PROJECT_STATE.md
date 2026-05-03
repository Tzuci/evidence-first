# PROJECT_STATE — Evidence-First MVP-0

Documento di onboarding tecnico, una pagina, leggibile dal collaboratore al primo accesso senza dover leggere il codice. Riflette lo stato del repo al commit finale **Fase 8.4**: `f3ee50d91ddcff4d8e4c850465708734eade0de3`.

---

## Cosa è il progetto

Piattaforma multi-AI **evidence-first** ed **evidence-gated**. Nessun claim fattuale può finire nella risposta finale se non è collegato a evidenze tracciabili, registrate nel Claim Ledger, verificate e approvate dal Final Answer Gate. La verità non è ciò che dice un modello AI: è ciò che le evidenze recuperate, archiviate, tracciate e verificate dal sistema supportano.

In MVP-0 il nucleo evidence-gated è costruito **prima** della visione multi-AI. Provider AI reali, Verified Web Mode, Hybrid Mode, consensus engine, contradiction detector avanzato, source quality evaluator e critical reviewer sono fasi future.

---

## Stato migration

| Migration | Stato |
|---|---|
| `0001_foundation.sql` | applicata, immutabile |
| `0002_storage.sql` | applicata, immutabile |
| `0003_documents.sql` | applicata, immutabile |
| `0004_claim_ledger.sql` | applicata, immutabile |
| `0005_answers_gate.sql` | applicata, immutabile (in 8.4) |
| `0006_lifecycle.sql` | da scrivere (Fase 8.5) |
| `0007_evaluation_retention.sql` | da scrivere |

---

## Cosa esiste oggi (Fase 8.4 conclusa)

- **DB foundation multi-tenant**: `tenants`, `users`, `projects`, `sessions`, `task_masters`, `event_processing_records`, `policy_versions`.
- **Audit chain hash-linked, append-only, verificabile end-to-end** via `verify_audit_chain` / `verify_task_audit_chain`. Append-only enforced a DB tramite trigger comune `reject_modify_append_only`.
- **Storage layer content-addressed, deduplicato, refcount-based**: `storage_blobs`, `storage_objects`. Dedup global concorrenza-safe via `INSERT ... ON CONFLICT DO NOTHING` sull'indice parziale `sb_global_uq`.
- **Document store** con upload reale `.txt`/`.md`, chunking deterministico, `evidence_spans` minimali, `task_documents` per associazione task ↔ documenti. `evidence_spans` append-only.
- **Claim Ledger append-only stretto**: `logical_claims`, `raw_claims`, `classified_claims`, `claim_ledger_entries` (append-only via trigger), `claim_lineage`, `claim_evidence_links`, `verification_records`. Supersede esclusivamente via `claim_lineage.relation_kind='supersedes'`. Nessuna colonna `superseded_by_id` su `claim_ledger_entries`.
- **Extractor mock-driven** deterministico (`apps/worker/app/services/extractor.py`).
- **CVE-lite mock-driven** deterministico (`apps/worker/app/services/cve_lite.py`).
- **Compiler mock-driven** deterministico (`apps/worker/app/services/compiler.py`). `COMPILER_NAME="mvp0_compiler_v1"`, `COMPILER_VERSION="0.1.0"`. Produce `draft_final_answers` v1, `final_answer_spans` 1:1 sui claim `verified_fact`, `final_answer_span_claim_links` con `link_role='primary_support'`.
- **Final Answer Gate mock-driven** deterministico (`apps/worker/app/services/final_answer_gate.py`). `GATE_NAME="mvp0_gate_v1"`, `GATE_VERSION="0.1.0"`. Append-only su `final_gate_reports`. Su `approved` inserisce `published_answers` v1 con `status='published'` e `content_hash = sha256(summary_text)`.
- **Worker single-consumer FK-safe e resume-safe** (`apps/worker/app/consumers/task_created.py`). Idempotente su redelivery.
- **API HTTP**: `health`, `projects`, `tasks`, `audit`, `documents`, `claims`, `answers`. Errori normalizzati con envelope `{"error": {...}}`.
- **Coerenza referenziale stretta a DB** tra `task_masters` ↔ `draft_final_answers` ↔ `final_gate_reports` ↔ `published_answers` via UNIQUE composite e FK composite. Impossibile a DB un gate report o un published answer con `task_id` non coerente con il draft sottostante.
- **Test rerun-safe** suddivisi in: root DB (`tests/`), shared (`packages/shared/tests/`), API (`apps/api/tests/`), worker (`apps/worker/tests/`), web (`apps/web/tests/`). I test API non importano dal worker e seedano DB direttamente.

---

## Pipeline worker 8.4 (sintesi audit)

### Task con documenti, approved scenario (claim verificati)

`task.created` → `task.docs_attached` (API) → `task.analyzing` → `task.docs_loaded` → `task.claims_extracted` → `task.claims_classified` → `task.claims_ledger_initialized` → `task.cve_lite_started` → `task.cve_lite_completed` → `task.analyzed_partial` → `task.compiling` → `task.draft_compiled` → `task.final_gate_started` → `task.final_gate_completed` → `task.published`.

Stato finale `task_masters.status`: `published`. `published_answers` v1 presente con `status='published'`.

### Task con documenti, rejected zero-verified

`task.created` → `task.docs_attached` (API) → `task.analyzing` → `task.docs_loaded` → `task.claims_extracted` → `task.claims_classified` → `task.claims_ledger_initialized` → `task.cve_lite_started` → `task.cve_lite_completed` → `task.analyzed_partial` → `task.compiling` → `task.draft_compiled` → `task.final_gate_started` → `task.final_gate_completed` → **`task.publication_held`**.

Stato finale `task_masters.status`: `analyzed_partial`. `final_gate_reports` con `decision='rejected'` presente. `published_answers` assente.

### Task senza documenti

`task.created` (API) → `task.analyzing` → `task.blocked`.

Stato finale `task_masters.status`: `blocked`. Nessuna pipeline claim/compiler/gate eseguita.

### Note su `task.publication_held`

**`task.publication_held` è esclusivamente un evento audit.** Non corrisponde ad alcuno status di `task_masters` e non è ammesso dal CHECK constraint `task_masters_status_check`. La task in DB resta in `analyzed_partial`. Il messaggio audit serve a rendere ricostruibile il fatto che il gate ha deciso di non pubblicare, distinguendolo da una task che sta semplicemente attendendo di essere processata.

### Note su `analyzed_partial` come terminale condizionato

`analyzed_partial` è terminale **solo se** esiste già una riga in `final_gate_reports` per quel `task_id`. Senza gate report, `analyzed_partial` indica una task in attesa che il consumer farà proseguire verso `compiling`. La distinzione è applicativa, gestita dal worker; non è codificata a livello DB.

### Note su `compiling`

`compiling` **non è mai** terminale di propria iniziativa. La guardia `WORKER_PIPELINE_INCOMPLETE` blocca `mark_succeeded` se la task resta in `compiling` senza un `final_gate_reports`. Una redelivery riprende il pipeline da dove era stato interrotto: se esiste un gate report, la task viene finalizzata usandolo (drive a `published` per approved, drive a `analyzed_partial` con `task.publication_held` per rejected); se non esiste, compiler e gate vengono rieseguiti idempotentemente.

---

## Final Answer Gate — regola di verifica

Uno span è **verified-backed** se e solo se esiste almeno un `final_answer_span_claim_links` tale che:

````
link.claim_ledger_entry_id == latest_entry_id_for(claim_logical_id)
AND latest_entry_state_for(claim_logical_id) == 'verified_fact'
````

Non è sufficiente che l'ultima entry del claim sia `verified_fact`: il link deve puntare esattamente a quella entry. Un link a una entry più vecchia (es. v1 candidate quando esiste v2 verified) **non è** verified-backed e produce rifiuto del gate con `reason_code='unverified_spans_present'`.

### Branch decisionali

| Condizione del draft | `decision` | `reason_code` | Coverage gap | `published_answers` |
|---|---|---|---|---|
| Zero spans (nessun `verified_fact`) | `rejected` | `no_verified_claims` | `kind='missing_evidence'`, `severity='block'`, `gap_key='no_verified_claims'` | assente |
| Tutti gli spans verified-backed | `approved` | `all_spans_verified` | nessuno | v1 con `status='published'`, `content_hash=sha256(summary_text)` |
| Almeno uno span non verified-backed | `rejected` | `unverified_spans_present` | un gap per ogni span scoperto: `kind='unverified_claim'`, `severity='block'`, `gap_key='span:<final_answer_span_id>'` | assente |

---

## API endpoints attivi (read-only per answers)

| Endpoint | Descrizione |
|---|---|
| `POST /api/v1/projects` | Crea progetto |
| `GET /api/v1/projects` | Lista progetti |
| `GET /api/v1/projects/{id}` | Dettaglio progetto |
| `POST /api/v1/projects/{id}/documents` | Upload documento `.txt`/`.md` |
| `GET /api/v1/projects/{id}/documents` | Lista documenti progetto |
| `GET /api/v1/documents/{id}` | Dettaglio documento |
| `GET /api/v1/documents/{id}/chunks` | Chunks del documento |
| `POST /api/v1/tasks` | Crea task closed_corpus |
| `GET /api/v1/tasks/{id}` | Dettaglio task |
| `GET /api/v1/tasks/{id}/documents` | Documenti collegati al task |
| `GET /api/v1/tasks/{id}/audit` | Catena audit del task |
| `GET /api/v1/tasks/{id}/raw-claims` | Raw claims del task |
| `GET /api/v1/tasks/{id}/classified-claims` | Classified claims del task |
| `GET /api/v1/tasks/{id}/claims` | Latest ledger entry per logical claim |
| `GET /api/v1/claims/{logical_id}/history` | Storia di un logical claim |
| `GET /api/v1/claims/{logical_id}/evidence` | Aggregato latest + links + verifications |
| `GET /api/v1/tasks/{id}/draft` | Draft v1 + spans (8.4) |
| `GET /api/v1/tasks/{id}/final-gate-report` | Gate report + coverage gaps (8.4) |
| `GET /api/v1/tasks/{id}/published-answer` | Published answer per task (8.4) |
| `GET /api/v1/published-answers/{id}` | Published answer per id (8.4) |
| `GET /health/live` / `/health/db` / `/health/queue` / `/health/storage` / `/health/ready` | Health checks |

### Convenzione errori

`ErrorCode.NOT_PUBLISHED` **non esiste** in MVP-0. Per il caso "task esiste ma non è ancora pubblicato" si restituisce `RESOURCE_NOT_FOUND` con `details.resource='published_answers'`. Per task inesistente: `RESOURCE_NOT_FOUND` con `details.resource='task_masters'`. Per draft o gate non ancora prodotti su task esistente: `details.resource='draft_final_answers'` o `'final_gate_reports'`.

---

## Cosa è ancora rinviato (non implementato in 8.4)

- **Provider AI reali** (Claude, ChatGPT, Gemini o equivalenti). MVP-0 gira con `PROVIDERS_ENABLED=mock` e `MAX_COST_PER_TASK=0`.
- **MockProvider funzionante con prompt template**. I template `prompts/` esistono come cartella ma non sono popolati.
- **Verified Web Mode** e **Hybrid Mode**.
- **Consensus engine**, **contradiction detector** avanzato, **source quality evaluator**, **critical reviewer**.
- **Renderer Markdown/HTML/PDF/DOCX/JSON-LD ed export verso filesystem o storage cloud**. Fuori scope per MVP-0.
- **Lifecycle eventi pubblicazione** (`published_answer_lifecycle_events`, transizioni `withdrawn`/`superseded` automatizzate).
- **Source Loss propagator** completo e relativa tabella `source_loss_events`.
- **PDF/OCR**, vector store cloud, S3/GCS/Azure.
- **Human Review UI** completa, retention/eval/export jobs.

---

## Vincoli sempre validi (MVP-0)

- Nessun provider AI reale.
- Nessun riferimento operativo a OpenAI, Anthropic, Google o altri provider esterni nel codice di MVP-0.
- `PROVIDERS_ENABLED=mock`, `MAX_COST_PER_TASK=0`.
- Pipeline mock-driven / deterministica.
- Closed Corpus only (nessun retrieval esterno, nessun web search).
- SQLAlchemy 2.0 Core: usare `Connection`, non `Engine.execute`.
- Migration applicate (0001–0005) sono immutabili. Modifiche schema solo via nuove migration.
- Test rerun-safe con UUID/hash/marker unici per invocazione.
- Append-only enforced a DB su `audit_records`, `evidence_spans`, `claim_ledger_entries`, `final_answer_spans`, `final_gate_reports`.

---

## Prossimo passo

Fase 8.5 in pianificazione: `0006_lifecycle.sql` con `published_answer_lifecycle_events` e `source_loss_events`, primo propagator e prima retention pass minimale. Nessuna parte di 8.5 è già scritta. Pianificazione separata in `PHASE_8_5_PLAN.md` (non ancora generato).
