# Test root — Evidence-First MVP-0 (Fase 8.4)

Questa cartella (`tests/`) ospita la **suite root**: test che esercitano direttamente il database (Postgres 16) per verificare migration, foundation, storage layer, claim ledger e answers/gate a livello di **schema, vincoli, trigger e semantica DDL**.

I test root non avviano l'API, non avviano il worker e non fanno end-to-end di pipeline applicativa. Per quei livelli esistono suite dedicate, elencate sotto in "Altre suite di test del repository".

Lo stato del repository riflette la **Fase 8.4** conclusa, testata, pushata e documentata. Le migration applicate sono `0001_foundation.sql`, `0002_storage.sql`, `0003_documents.sql`, `0004_claim_ledger.sql`, `0005_answers_gate.sql`. Le migration successive (`0006_lifecycle.sql`, `0007_evaluation_retention.sql`) sono pianificate per la Fase 8.5 e fasi successive: i relativi test non esistono ancora e non sono parte di questa suite.

---

## Setup

```bash
cp .env.example .env
make up
make migrate
```

`make up` avvia Postgres e Redis. `make migrate` applica in ordine `0001` … `0005`. Il runner è idempotente: rieseguirlo è no-op.

`tests/conftest.py` espone una fixture `database_url` (con skip automatico se `DATABASE_URL` non è impostata o se Postgres non è raggiungibile) e una fixture `db_conn` che apre una connessione `psycopg` con `autocommit=False` e fa rollback al teardown. Lo schema URL accettato è sia in stile SQLAlchemy (`postgresql+psycopg://…`) sia in stile libpq (`postgresql://…`); viene normalizzato in `tests/conftest.py`.

Le dipendenze Python richieste per la suite root sono `psycopg[binary]>=3.1`, `pytest>=8.0` e `sqlalchemy>=2.0,<2.1` (importata indirettamente da alcuni test root).

---

## Esecuzione

Suite completa root:

```bash
make test-db
```

Equivale, internamente, a:

```bash
PYTHONPATH=$(pwd)/packages/shared python3 -m pytest -q tests/
```

Singolo file:

```bash
python3 -m pytest -q tests/test_db_basic.py
```

Filtro per nome di test:

```bash
python3 -m pytest -q tests/test_answers_gate_constraints.py -k append_only
```

Per eseguire **tutte** le suite del repository (root + shared + api + worker + web):

```bash
make test
```

---

## Cosa coprono i test root (Fase 8.4)

### `test_migrate.py` — migration runner
Verifica `scripts/migrate.py`:
- applicazione di `0001_foundation.sql` e registrazione corretta in `schema_migrations` con il `sha256` del file;
- idempotenza: una seconda esecuzione non riapplica nulla;
- `--target=0001` è no-op se la migration è già applicata;
- `--target` con prefisso inesistente esce con codice di errore;
- alterazione del checksum salvato in DB rispetto al file su disco viene rilevata come errore prima di qualunque tentativo di riapplicazione;
- `--status` produce output e ritorna 0 quando lo stato è coerente.

### `test_db_basic.py` — foundation (`0001`)
Verifica le fondazioni multi-tenant:
- presenza delle estensioni `pgcrypto` e `citext`;
- INSERT di `tenants`, `users`, `projects` (idempotente via ON CONFLICT, rerun-safe);
- UNIQUE su `tenants.slug`;
- append-only di `audit_records`: UPDATE e DELETE rifiutati dal trigger;
- CHECK `audit_scope_consistency` su `audit_records`: `chain_scope='task'` con `scope_id <> task_id` è rifiutato;
- immutabilità dei campi protetti su `event_processing_records` (`event_id`, `event_type`, `consumer_name`, `idempotency_key`, `first_seen_at`, `tenant_id`): UPDATE legittimo dello status passa, UPDATE su un campo immutabile fallisce.

### `test_storage_dedup.py` — storage layer (`0002`)
Verifica lo storage content-addressed:
- dedup globale via indice parziale `sb_global_uq (content_hash, hash_algorithm) WHERE tenant_namespace_id IS NULL`: il secondo INSERT sullo stesso hash fallisce con `UniqueViolation`;
- refcount mantenuto dai trigger su `storage_objects`: INSERT incrementa, DELETE decrementa;
- `reject_delete_blob_with_refs`: DELETE su `storage_blobs` con `refcount > 0` viene rifiutato;
- `evidence_spans` append-only: UPDATE e DELETE rifiutati dal trigger.

I test usano `content_hash`, `text_hash` e `quote_hash` derivati da `uuid.uuid4()` per essere rerun-safe su un dev DB di lunga durata.

### `test_claim_ledger_constraints.py` — claim ledger (`0004`)
Verifica gli invarianti append-only e di unicità del Claim Ledger:
- `claim_ledger_entries` append-only: UPDATE e DELETE rifiutati;
- UNIQUE `(claim_logical_id, version_no)`: due entry alla stessa versione per lo stesso logical claim sono rifiutate;
- v1 e v2 distinte sullo stesso logical claim sono ammesse;
- `claim_lineage`: CHECK no-self-reference (`parent_entry_id <> child_entry_id`) e UNIQUE `(parent_entry_id, child_entry_id, relation_kind)`;
- `verification_records` UNIQUE `(claim_ledger_entry_id, check_kind, check_name)`;
- `claim_evidence_links`: CHECK `cel_origin_xor` impone, in MVP-0, `evidence_span_id NOT NULL` e `retrieved_source_span_id IS NULL`. Tentare di inserire con entrambi valorizzati è rifiutato dal CHECK prima ancora di toccare le FK.

### `test_answers_gate_constraints.py` — answers e gate (`0005`)
Verifica la coerenza referenziale stretta tra task ↔ draft ↔ gate ↔ published e gli invarianti append-only introdotti in 8.4:
- `task_masters.status` accetta `compiling` e `published` (entrambi presenti già prima di 0005, confermati dal CHECK ricreato in modo difensivo) e **rifiuta** valori arbitrari come `publication_held`. Lo stato `publication_held` non esiste a livello DB: `task.publication_held` è soltanto un evento audit;
- `draft_final_answers`: UNIQUE `(task_id, version_no)`;
- `final_answer_spans`: append-only via trigger e UNIQUE `(draft_final_answer_id, span_index)`;
- `final_gate_reports`: append-only via trigger e UNIQUE `(draft_final_answer_id)` (un solo report per draft);
- `published_answers`: UNIQUE `(task_id, version_no)` e CHECK `no_self_supersede` (`superseded_by_id <> id`);
- `coverage_gap_statements`: UNIQUE `(draft_final_answer_id, kind, gap_key)` per idempotenza dei gap; combinazioni distinte di `(kind, gap_key)` sullo stesso draft coesistono;
- presenza dei vincoli composite usati come target di FK: `draft_final_answers_id_task_uq`, `final_gate_reports_id_task_draft_uq`, `published_answers_id_task_uq`;
- presenza delle FK composite di consistenza: `final_gate_reports_draft_consistency`, `published_answers_draft_consistency`, `published_answers_gate_consistency`;
- `final_gate_reports` con `task_id` diverso dal `task_id` del draft sottostante è rifiutato dalla FK composita (test esplicito che inserisce un draft di task A e tenta un report con task B);
- `lc_block_delete_if_published`: DELETE su `logical_claims` è bloccato quando esiste una `published_answers` attiva (`status='published'`) la cui catena draft → spans → span_claim_links lo referenzia; viceversa, DELETE su un `logical_claims` non referenziato passa effettivamente.

---

## Convenzioni della suite root

- **Rerun-safety**: ogni test invocato più volte sullo stesso dev DB deve produrre lo stesso esito. Per ottenerlo i test usano UUID, hash e marker generati per invocazione tramite `uuid.uuid4()` invece di valori statici. Questo vale per `content_hash`, `text_hash`, `quote_hash`, `canonical_claim_hash`, `transition_reason`, nomi di progetto, e qualunque altro campo che potrebbe collidere su un DB di lunga vita.
- **Confinamento DDL**: i test root verificano lo schema, non la pipeline. Non importano `app.*` né dell'API né del worker. Per la coerenza pipeline → DB esistono le suite di `apps/api/tests/` e `apps/worker/tests/`.
- **`psycopg` diretto**: i test root usano `psycopg.connect()` direttamente, non SQLAlchemy `Engine`. La fixture `db_conn` apre la connessione, lascia in mano al test la gestione di `commit/rollback`, e fa rollback finale al teardown.
- **Migration garantite**: i singoli test che dipendono dallo schema chiamano un helper `_ensure_migrations(db_conn)` che ricarica `scripts/migrate.py` come modulo e applica `cmd_apply` con `target=None`. Questo significa che un test root passa anche se `make migrate` non è stato eseguito a mano, purché Postgres sia raggiungibile.

---

## Altre suite di test del repository

I test root non sono l'unico livello di copertura. Le suite seguenti vivono fuori da `tests/` e hanno scopi distinti.

| Suite | Cartella | Comando | Cosa copre |
|---|---|---|---|
| Shared library | `packages/shared/tests/` | `make test-shared` | `canonical_json` (vettori deterministici per `canonical_dumps` e `canonical_sha256`) e helper unit-level di `evidencefirst_shared.db.audit` (`_resolve_scope_id`). Niente DB. |
| API | `apps/api/tests/` | `make test-api` | E2E HTTP via FastAPI `TestClient`: health, projects, tasks (con e senza `Idempotency-Key`), upload documenti, audit endpoint con polling fino a stato terminale, claims endpoints, answers endpoints (`/draft`, `/final-gate-report`, `/published-answer`, `/published-answers/{id}`). I test API seedano direttamente il DB e **non** importano dal worker (sotto `apps/api`, `app` risolve all'API). |
| Worker | `apps/worker/tests/` | `make test-worker` | Pipeline single-consumer 8.4 end-to-end: idempotency, FK-safety, branch no-docs (`task.blocked`), pipeline 8.3 invariata (extractor + CVE-lite), compiler + final answer gate (approved, rejected zero verified, unverified spans via stale link, resume da `compiling` con e senza gate report preesistente), redelivery terminale. I test worker possono importare `app.consumers.task_created` e i servizi worker (sotto `apps/worker`, `app` risolve al worker). |
| Web | `apps/web/tests/` | `make test-web` | Smoke Vitest sul componente di diagnostica della UI minimale Next.js. |

`make test` aggrega le cinque suite (`test-db`, `test-shared`, `test-api`, `test-worker`, `test-web`) in un unico run.

---

## Cosa **non** è coperto qui (e non lo sarà in `tests/`)

- **Pipeline applicativa end-to-end**: è coperta da `apps/worker/tests/` e `apps/api/tests/`. La suite root resta DDL/constraint-only.
- **Lifecycle pubblicazione** (`published_answer_lifecycle_events`, transizioni `withdrawn`/`superseded`) — pianificato per la Fase 8.5 con `0006_lifecycle.sql`.
- **`source_loss_events`** e propagator dedicato — pianificato per la Fase 8.5.
- **Retention/eval/export jobs**, `0007_evaluation_retention.sql` — pianificato per fasi successive.
- **Provider AI reali, MockProvider con prompt template, Verified Web Mode, Hybrid Mode, consensus engine, contradiction detector avanzato, source quality evaluator, critical reviewer**.
- **Renderer/export Markdown/HTML/PDF/DOCX/JSON-LD**.

Quando arriveranno le migration `0006` e `0007`, i corrispondenti vincoli, trigger e invarianti DDL saranno coperti da nuovi file in questa cartella, allineati alle convenzioni descritte sopra.
