# Evidence-First Multi-AI Platform — MVP-0

Piattaforma multi-AI **evidence-first** ed **evidence-gated**. Nessuna risposta finale può essere pubblicata se non è collegata a evidenze tracciabili.

Repository alla **Fase 8.3** (Sprint 2: Claim Ledger append-only e CVE-lite mock-driven).

Disponibile localmente:
- Postgres 16, Redis 7
- API HTTP (FastAPI) con `health`, `projects`, `tasks`, `audit`, `documents`, `claims`
- Worker Redis Streams difensivo, FK-safe, con pipeline 8.3 (estrattore + CVE-lite)
- Web Next.js minimale (home + `/diagnostic`)
- Storage filesystem deduplicato content-addressed con upsert concorrenza-safe
- Audit chain con payload normalizzato e verificabile end-to-end

La pipeline di **compilazione**, il **Final Answer Gate** e il primo `published_answers` **non sono ancora implementati**: sono il contenuto della Fase 8.4 (`0005_answers_gate.sql`). In 8.3 i task con documenti raggiungono lo stato `analyzed_partial` dopo aver verificato i claim con CVE-lite mock-driven.

---

## Scope MVP-0 (sintesi)

Incluso (oggi, 8.3):
- Closed Corpus, Postgres + Redis + filesystem locale.
- Audit chain hash-linked (append-only, verificabile).
- `event_processing_records` con idempotenza per consumer.
- API: `health/projects/tasks/audit/documents/claims`.
- Storage content-addressed deduplicato, refcount-based.
- Document upload reale `.txt`/`.md` con chunking deterministico ed `evidence_spans` minimali.
- **Claim Ledger append-only** con extractor mock-driven e CVE-lite mock-driven.

Escluso (rinviato a fasi successive):
- Provider AI reali, MockProvider funzionante, Verified Web, Hybrid mode.
- PDF/OCR, vector store cloud, S3/GCS/Azure.
- Final Answer Gate, `published_answers`, renderer.
- Lifecycle eventi pubblicazione, Source Loss propagator completo.
- Human Review UI completa, retention/eval/export jobs.

---

## Fasi precedenti (sintesi)

- **8.1b**: API + Worker + Web stub.
- **8.1c**: audit/idempotency centralizzati in `packages/shared`, `verify_audit_chain`, `/health/ready` con 503 quando non ready.
- **8.1d**: Dockerfile web robusto, payload audit normalizzato, commit-then-publish lato API, worker difensivo.
- **8.1d-patch1**: Makefile default goal corretto, worker FK-safe sul caso task non visibile.
- **8.2**: storage reale, document upload reale, `task_documents`, worker `analyzed_partial` con documenti.
- **8.2a-patch**: stabilizzazione pre-8.3. Test rerun-safe (hash unici per invocazione), dedup storage concorrenza-safe via `INSERT ... ON CONFLICT DO NOTHING`, validazione `document_ids` con `bindparam(expanding=True, type_=Uuid())`.

---

## Fase 8.3 — Claim Ledger e CVE-lite

### Cosa cambia

- **Migration `0004_claim_ledger.sql` applicata.** Introduce `logical_claims`, `raw_claims`, `classified_claims`, `claim_ledger_entries` (append-only), `claim_lineage`, `claim_evidence_links`, `verification_records` e i placeholder `contradiction_records`, `claim_support_links`, `human_review_requests`, `publication_rules`. Nessuna modifica a `0001`, `0002`, `0003`.
- **Append-only stretto su `claim_ledger_entries`.** Trigger `claim_ledger_entries_append_only` (basato sul comune `reject_modify_append_only`) rifiuta `UPDATE` e `DELETE` a livello DB. Nessuna riga del ledger viene mai mutata dopo l'INSERT, da nessun componente.
- **Supersede via `claim_lineage`, mai via UPDATE.** Per rappresentare che una v2 supersede una v1 si fa: (1) `INSERT` di una nuova riga in `claim_ledger_entries` con `version_no = N+1` e lo stato finale; (2) `INSERT` in `claim_lineage` con `parent_entry_id = v1.id`, `child_entry_id = v2.id`, `relation_kind = 'supersedes'`. La v1 resta immutata. Non esiste alcuna colonna `superseded_by_id`.
- **Estrattore mock-driven (`apps/worker/app/services/extractor.py`).** Deterministico, nessuna AI. Per ogni `document_chunks` attaccato al task: split frasi, filtra quelle contenenti cifre o citazioni, normalizza il testo, calcola `canonical_claim_hash`, upserta `logical_claims` (UNIQUE `(task_id, canonical_claim_hash)`), inserisce `raw_claims`, promuove a `classified_claims` (`claim_type='factual'`, `domain_tag='general'`), inserisce `claim_ledger_entries v1` con `state='candidate'` e collega l'evidence.
- **CVE-lite mock-driven (`apps/worker/app/services/cve_lite.py`).** Per ogni v1: PASS se `evidence_spans.quote` è sottostringa di `document_chunks.inline_text` AND `sha256(quote)` corrisponde a `evidence_spans.quote_hash`; FAIL altrimenti. In entrambi i casi inserisce un `verification_records` (UNIQUE su `(claim_ledger_entry_id, check_kind, check_name)`), una `claim_ledger_entries v2` (`verified_fact` su PASS, `unverifiable` su FAIL), una riga di `claim_lineage` `supersedes`, e collega la v2 all'evidence span. **La v1 non viene mai aggiornata.**
- **Worker pipeline 8.3 per task con documenti.** Sequenza audit emessa sulla chain del task:
  1. `task.created` (dall'API)
  2. `task.docs_attached` (dall'API se `document_ids` non vuoto)
  3. `task.analyzing`
  4. `task.docs_loaded`
  5. `task.claims_extracted`
  6. `task.claims_classified`
  7. `task.claims_ledger_initialized`
  8. `task.cve_lite_started`
  9. `task.cve_lite_completed`
  10. `task.analyzed_partial` con `reason='claims_verified_by_cve_lite_compilation_pending'`.
- **Task senza documenti: comportamento invariato.** Sequenza: `task.created`, `task.analyzing`, `task.blocked`. Nessun ramo claim viene mai eseguito.
- **Idempotenza completa.** Doppio delivery dello stesso `task.created` non duplica `raw_claims`, `classified_claims`, `logical_claims`, `claim_ledger_entries`, `claim_lineage`, `claim_evidence_links`, `verification_records`, né eventi audit. Tutti gli INSERT del worker usano `ON CONFLICT DO NOTHING` su vincoli UNIQUE espliciti dichiarati in `0004_claim_ledger.sql`.
- **Endpoint API read-only (nessun side effect).**
  - `GET /api/v1/tasks/{task_id}/raw-claims`
  - `GET /api/v1/tasks/{task_id}/classified-claims`
  - `GET /api/v1/tasks/{task_id}/claims` — ultima `claim_ledger_entries` per ogni `claim_logical_id`
  - `GET /api/v1/claims/{claim_logical_id}/history` — tutte le versioni del ledger
  - `GET /api/v1/claims/{claim_logical_id}/evidence` — `latest_entry` + `evidence_links` + `verification_records`
- **Schemi shared aggiornati**: `RawClaimRead`, `ClassifiedClaimRead`, `ClaimLedgerEntryRead`, `VerificationRecordRead`, `ClaimEvidenceLinkRead`, `ClaimEvidenceRead`.

### Cosa NON cambia in 8.3

- Nessun **renderer**.
- Nessun **`published_answers`**.
- Nessun **`final_gate_reports`**.
- Nessun **Final Answer Gate**.
- Nessun **`draft_final_answers`** o **`final_answer_spans`**.
- Nessun trigger `lc_block_delete_if_published` (verrà installato in 0005 insieme a `published_answers`).
- Nessun provider AI esterno. **Costo API = 0.** `PROVIDERS_ENABLED=mock`.

### Cosa arriva in 8.4

`0005_answers_gate.sql` introdurrà `agent_runs`, `agent_outputs`, `truncation_events`, `continuation_attempts`, `coverage_gap_statements`, `draft_final_answers`, `final_answer_spans`, `final_answer_span_claim_links`, `final_gate_reports` (append-only stretto), `published_answers` (con campi lifecycle minimi). Pipeline worker di compilazione mock-driven sopra il Claim Ledger. Gate finale che pubblica solo se ogni `final_answer_spans` è collegato a una `claim_ledger_entries` verificata oppure gestisce esplicitamente i coverage gap.

---

## Setup e comandi

````bash
cp .env.example .env
make up
make migrate
make seed
make test
````

| Comando | Effetto |
|---|---|
| `make up` | db, redis, api, worker, web |
| `make down` | ferma (mantiene i volumi) |
| `make logs` | tail dei log |
| `make migrate` / `make migrate ARGS="--status"` | migration runner |
| `make seed` | seed di sviluppo |
| `make test` | tutti i test |
| `make test-db` / `test-shared` / `test-api` / `test-worker` / `test-web` | per modulo |
| `make lock-web` | genera `apps/web/package-lock.json` |
| `make psql` / `make redis-cli` | shell |
| `make clean` | distrugge i volumi |

`make migrate` applica `0001`, `0002`, `0003`, `0004` in ordine. Idempotente: rieseguirla è no-op.

---

## Smoke test 8.3

````bash
# 1) Crea progetto
PID=$(curl -s -X POST localhost:8000/api/v1/projects \
  -H 'content-type: application/json' \
  -d '{"name":"smoke-83-demo"}' | jq -r .id)

# 2) Carica un documento .txt con frasi factual (cifre)
DID=$(curl -s -X POST "localhost:8000/api/v1/projects/$PID/documents" \
  -F "file=@evaluation/fixtures/closed_corpus_basic/doc_en.txt;type=text/plain" \
  | jq -r .id)

# 3) Crea il task con documento
TID=$(curl -s -X POST localhost:8000/api/v1/tasks \
  -H 'content-type: application/json' \
  -d "{\"project_id\":\"$PID\",\"objective\":\"smoke 8.3\",\"mode\":\"closed_corpus\",\"document_ids\":[\"$DID\"]}" \
  | jq -r .id)

# 4) Polling fino a analyzed_partial
while true; do
  S=$(curl -s "localhost:8000/api/v1/tasks/$TID" | jq -r .status)
  echo "status=$S"
  [ "$S" = "analyzed_partial" ] && break
  sleep 1
done

# 5) Audit chain (deve elencare 10 eventi: task.created ... task.analyzed_partial)
curl -s "localhost:8000/api/v1/tasks/$TID/audit?limit=500" | jq '.items[].event_type'

# 6) Vista latest del ledger
curl -s "localhost:8000/api/v1/tasks/$TID/claims" | jq

# 7) Storia di un claim
LCID=$(curl -s "localhost:8000/api/v1/tasks/$TID/claims" | jq -r '.items[0].claim_logical_id')
curl -s "localhost:8000/api/v1/claims/$LCID/history" | jq
curl -s "localhost:8000/api/v1/claims/$LCID/evidence" | jq
````

---

## Architettura runtime (sintesi)

````
   ┌────────────┐  POST /tasks (commit-then-publish)
   │    API     │ ──────────────────────────────► Postgres
   │  (FastAPI) │                                  ▲
   │            │ ──xadd──► Redis Stream events:   │
   └────────────┘            task.created          │
                                  │                │
                                  ▼                │
                           ┌──────────────┐        │
                           │   Worker     │───────►┘
                           │   FK-safe    │
                           │  pipeline 8.3│
                           └──────────────┘
````

Pipeline 8.3 nel worker (con documenti):

````
  task.created event
      │
      ▼
  begin_processing(idempotent)
      │
      ▼
  created → analyzing                  audit task.analyzing
      │
      ▼
  load chunks/spans                    audit task.docs_loaded
      │
      ▼
  extractor mock-driven                audit task.claims_extracted
                                       audit task.claims_classified
                                       audit task.claims_ledger_initialized
      │
      ▼
  CVE-lite mock-driven                 audit task.cve_lite_started
                                       audit task.cve_lite_completed
      │
      ▼
  analyzing → analyzed_partial         audit task.analyzed_partial
      │
      ▼
  mark_succeeded
````

---

## Costi

`MAX_COST_PER_TASK=0`, `PROVIDERS_ENABLED=mock`. Nessun provider AI, nessun costo cloud, nessuna chiamata di rete in uscita verso terze parti.

---

## Documenti

- [`docs/migration_plan.md`](docs/migration_plan.md)