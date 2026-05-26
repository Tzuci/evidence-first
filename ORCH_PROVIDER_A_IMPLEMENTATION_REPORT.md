# ORCH-PROVIDER-A IMPLEMENTATION REPORT

> Relazione finale della fase **ORCH-PROVIDER-A**, prodotta per revisione
> umana. Repo: `Tzuci/evidence-first`, branch `main`. Lingua: italiano
> tecnico. **Nessun commit è stato eseguito.**
>
> **Aggiornata dal microfix ORCH-PROVIDER-A** che ha applicato due
> correzioni bloccanti pre-commit: (1) `tenant_id` aggiunto ai tre dict
> di mapping schema (`provider_invocations`, `token_usage_records`,
> `source_candidates` lo richiedono NOT NULL in `0011`), più
> `orchestration_run_id` e `master_prompt_id` sui record source
> candidate; (2) redaction string-safe dell'`error_message` che maschera
> i segreti `nome=valore` / `nome: valore`. Più l'allineamento della
> documentazione (lista import effettiva, controllo grep import-level).

---

## 1. Scope

ORCH-PROVIDER-A implementa il **primo blocco mock-first dell'astrazione
provider AI** progettata in `PHASE_ORCH_PROVIDER_PRE.md`. È una fase di
**implementazione worker-level, mock-first, senza integrazione runtime**.

La fase consiste esclusivamente in:

- un modulo worker-level puro `apps/worker/app/services/orchestration_provider.py`;
- un file di test worker-level `apps/worker/tests/test_orchestration_provider_service.py`;
- questa relazione.

Il modulo implementa: contratti dati Python (`ProviderRequest`,
`ProviderResult`, `ProviderError`, `ProviderUsage`, e ausiliari);
un'interfaccia logica `ProviderAdapter` astratta; un `ProviderRegistry`
minimale statico; un `MockProviderAdapter` deterministico; hashing
deterministico request/response; redaction sicura dei payload;
normalizzazione degli errori; stima mock di usage/costo; un preflight
budget check mock; le funzioni pure di mapping verso record dict
compatibili con `provider_invocations` e `token_usage_records`; e
l'estrazione di source candidate come candidate **non verificate**.

ORCH-PROVIDER-A **non** implementa: provider reali, SDK provider, rete,
Redis, FastAPI, worker di orchestrazione, API, UI, migration, integrazione
runtime, gate parallelo. Il modulo produce soltanto oggetti e dict in
memoria che un futuro worker potrà persistere.

---

## 2. Files created/modified

Esattamente tre file, tutti nuovi:

- `apps/worker/app/services/orchestration_provider.py` — **nuovo**. Modulo
  worker-level puro dell'astrazione provider mock-first.
- `apps/worker/tests/test_orchestration_provider_service.py` — **nuovo**.
  Test worker-level, senza DB, senza Redis, senza FastAPI, senza rete.
- `ORCH_PROVIDER_A_IMPLEMENTATION_REPORT.md` — **nuovo**. Questa relazione.

Nessun altro file del repository è stato creato o modificato. In
particolare non sono stati toccati: `README.md`, `PROJECT_STATE.md`,
`PHASE_ORCH_PROVIDER_PRE.md`, `PHASE_PRODUCT_ORCHESTRATION_PRE.md`,
`PHASE_ORCH_SCHEMA_PRE.md`, `ORCH_SCHEMA_A_IMPLEMENTATION_REPORT.md`,
`migrations/*`, `tests/*` (root), `apps/api/*`, `apps/web/*`,
`packages/shared/*`, i file di package, i lockfile, `.env*`, i file
Docker, il `Makefile`.

---

## 3. Implemented module

Il modulo `apps/worker/app/services/orchestration_provider.py` è **puro e
testabile senza ambiente**. Usa esclusivamente la standard library Python:
`abc`, `dataclasses`, `decimal`, `enum`, `hashlib`, `json`, `re`, `typing`,
`uuid` (più `from __future__ import annotations`). `re` è usato dalla
redaction string-safe dell'`error_message` (vedi §7). L'import `time`,
ammesso dal prompt, non è necessario e non è importato, per igiene del
codice.

Costanti e codomini definiti (sezione "Constants and codomains"):

- **Identità mock**: `MOCK_PROVIDER_NAME = "mock"`,
  `MOCK_MODEL_NAME = "mock-model"`,
  `SERVICE_NAME = "mvp0_mock_provider_adapter"`,
  `SERVICE_VERSION = "0.1.0"`,
  `DEFAULT_REDACTION_STRATEGY = "hash_only"`.
- **Provider status** (`provider_invocations.status`, codominio 0011):
  `pending`, `succeeded`, `failed`, `cancelled`.
- **Provider error_code** normalizzati: `timeout`, `rate_limited`,
  `authentication_failed`, `authorization_failed`, `provider_unavailable`,
  `invalid_request`, `invalid_model`, `content_filter`,
  `malformed_response`, `budget_exceeded`, `retry_exhausted`,
  `network_error`, `unknown_error`.
- **Retryability**: retryable = {`timeout`, `rate_limited`,
  `provider_unavailable`, `network_error`}; tutti gli altri non
  retryable; `unknown_error` e `retry_exhausted` non retryable per
  default.
- **Source candidate type** (codominio 0011): `agent_cited`,
  `user_supplied`, `system_retrieved`, `internal`, `future_web`.
- **Source candidate status** (codominio 0011): `proposed`,
  `resolution_pending`, `resolved`, `resolution_failed`,
  `verification_pending`, `verified_as_retrieved`, `rejected`,
  `insufficient_metadata`.
- **Redaction mode**: `hash_only`, `redacted_payload`, `no_raw_payload`.
- **Pass kind** (codominio 0011 `token_usage_records.pass_kind`):
  `independent_answer`, `reviewer`, `critic`, `synthesis`,
  `second_check`, `source_resolution`.

API pubblica del modulo:

- Dataclass: `ProviderMessage`, `ProviderRetryPolicy`,
  `ProviderTimeoutPolicy`, `ProviderRedactionPolicy`, `ProviderUsage`,
  `ProviderError`, `ProviderSourceCandidate`, `ProviderRequest`,
  `ProviderResult` (tutte `frozen`).
- Enum: `ProviderCapability` (`text`, `structured_output`,
  `source_candidates`, `error_injection`).
- Funzioni pure: `canonical_json`, `stable_hash`, `redact_payload`,
  `normalize_error`, `estimate_mock_tokens`,
  `count_request_input_tokens`, `build_request_hash`,
  `build_response_hash`, `enforce_mock_budget`.
- Classi: `ProviderAdapter` (ABC), `MockProviderAdapter`,
  `ProviderRegistry`; factory `default_registry()`.
- Mapping schema: `to_provider_invocation_record`,
  `to_token_usage_record`, `source_candidates_to_records`.

---

## 4. Data contracts

Tutti i contratti dati sono `@dataclass(frozen=True)`. Nessun Pydantic,
nessun SQLAlchemy, nessuno shared schema.

- `ProviderMessage` — `role`, `content`.
- `ProviderRetryPolicy` — `max_attempts=1`,
  `retryable_error_codes=<retryable set>`, `backoff_ms=0`.
- `ProviderTimeoutPolicy` — `timeout_ms=30000`.
- `ProviderRedactionPolicy` — `strategy="hash_only"`, `mode="hash_only"`.
- `ProviderUsage` — `tokens_input`, `tokens_output`,
  `cost_estimate: Decimal`, `is_mock`. Il costo è un `Decimal` per
  esattezza e stabilità JSON (`canonical_json` rende il `Decimal` come
  stringa).
- `ProviderError` — `error_code`, `error_message`, `retryable`.
- `ProviderSourceCandidate` — `candidate_type="agent_cited"`,
  `status="proposed"`, `title`, `url`, `locator`, `raw_text`,
  `metadata`, `is_verified=False`.
- `ProviderRequest` — tutti i campi richiesti dal prompt §8:
  `tenant_id`, `project_id`, `orchestration_run_id`,
  `orchestration_agent_run_id`, `agent_config_snapshot_id`,
  `provider_name`, `model`, `messages`, `system_instructions`,
  `task_instructions`, `output_contract`, `constraints`,
  `source_policy`, `max_tokens`, `temperature_like_config`,
  `timeout_policy`, `retry_policy`, `redaction_policy`,
  `idempotency_key`, `is_mock_expected`.
- `ProviderResult` — `status`, `content_text`, `structured_payload`,
  `source_candidates`, `usage`, `latency_ms`, `response_hash`,
  `raw_response_redacted`, `error`, `is_mock`.

---

## 5. MockProviderAdapter behavior

`MockProviderAdapter` estende `ProviderAdapter`. Comportamento:

- `provider_name()` == `"mock"`.
- `supported_models()` contiene `"mock-model"`.
- `capabilities()` include `text`, `structured_output`,
  `source_candidates`, `error_injection`.
- `invoke(request)` è **deterministico**: non usa rete, non usa il
  tempo reale per contenuto o hash. Il contenuto testuale è derivato
  unicamente dal `request_hash`; la `latency_ms` è un valore mock fisso
  (`0`), non derivato dall'orologio.
- Senza errore iniettato: `status='succeeded'`, `content_text`
  deterministico, `structured_payload` con `mock=True` e semantic
  warning, `usage` calcolata deterministicamente, `cost_estimate`
  `Decimal("0")`, `is_mock=True`.
- **Error injection**: se `request.constraints` contiene
  `{"mock_error_code": "<codice>"}`, `invoke()` ritorna un
  `ProviderResult` con `status='failed'`, `content_text=None`,
  `error` normalizzato (codice + retryability coerenti),
  `source_candidates=()`, `usage` mock minima, `is_mock=True`,
  `response_hash` stabile.
- **Source candidate injection**: se `request.source_policy` contiene
  `{"mock_source_candidates": [ ... ]}`, `invoke()` ritorna
  `source_candidates` come tuple di `ProviderSourceCandidate` con
  `candidate_type='agent_cited'`, `status='proposed'`,
  `is_verified=False`, e `metadata` mock con un semantic warning che
  dichiara la candidate non verificata. Entry malformate (non-dict)
  vengono scartate difensivamente, non sollevano eccezione.

`ProviderRegistry` è statico/in-memory: `register(adapter)`,
`get(provider_name)`, `has(provider_name)`, `provider_names()`. Un
provider sconosciuto in `get()` solleva `ValueError` controllato e
testabile. Il registry non contiene segreti. `default_registry()`
ritorna un registry fresco contenente **solo** il `MockProviderAdapter`.

---

## 6. Hashing and redaction

- `canonical_json(value)` — `json.dumps` con `sort_keys=True`,
  `separators=(",", ":")`, `ensure_ascii=False`. Una normalizzazione
  ricorsiva (`_to_jsonable`) converte `Decimal` in stringa, le
  dataclass in dict, gli enum nel loro valore, `uuid.UUID` in stringa,
  tuple/list in modo stabile. Due strutture uguali come dato producono
  la stessa stringa indipendentemente dall'ordine delle chiavi.
- `stable_hash(value)` — `sha256(canonical_json(value))` esadecimale,
  deterministico e order-stable.
- `build_request_hash(request)` — hash stabile costruito su una vista
  della richiesta che include solo i campi di identità logica; non
  include nulla di dipendente dal tempo o dall'ambiente.
- `build_response_hash(payload)` — hash stabile, sulla stessa base.
- `redact_payload(payload, policy)`:
  - `hash_only` → `{"payload_hash": stable_hash(payload),
    "redaction_mode": "hash_only"}`;
  - `no_raw_payload` → `{"redaction_mode": "no_raw_payload"}`;
  - `redacted_payload` → payload con i campi sensibili mascherati
    ricorsivamente con `"[REDACTED]"`, campi non sensibili preservati;
  - qualunque mode non riconosciuto è trattato come `no_raw_payload`
    (default sicuro): il modulo non lascia mai trapelare un payload
    grezzo.
  - Campi sensibili: nome (lowercase) che eguaglia o contiene uno fra
    `api_key`, `secret`, `authorization`, `password`, `credential`,
    `access_token`, `refresh_token`, `bearer_token`, `auth_token`.
    Campi legittimi come `max_tokens`, `tokens_input`,
    `tokens_output` **non** corrispondono ad alcun frammento e non
    vengono redatti.

`request_hash` / `response_hash` servono audit, debug e idempotenza;
non provano il contenuto.

---

## 7. Error normalization

`normalize_error(error_code, error_message=None)` ritorna un
`ProviderError`:

- un `error_code` non noto è mappato a `unknown_error`;
- l'`error_message` è reso **redaction-safe**, ripulito e troncato
  (lunghezza massima 500 caratteri, suffisso `...[truncated]`); `None`
  diventa stringa vuota; un valore non-stringa è prima stringificato e
  poi trattato come gli altri;
- `retryable` segue il codominio: `timeout`, `rate_limited`,
  `provider_unavailable`, `network_error` sono retryable; tutti gli
  altri (compresi `authentication_failed`, `authorization_failed`,
  `invalid_request`, `invalid_model`, `content_filter`,
  `malformed_response`, `budget_exceeded`, `unknown_error`,
  `retry_exhausted`) non lo sono.

**Redaction string-safe dell'`error_message` (microfix).** La funzione
interna `_redact_sensitive_text(text)` maschera i frammenti testuali
sensibili `nome=valore` / `nome: valore` che possono trapelare dentro
un messaggio d'errore libero di un provider. Il nome del campo (uno fra
`api_key`, `secret`, `authorization`, `password`, `credential`,
`access_token`, `refresh_token`, `bearer_token`, `auth_token`) e il
separatore vengono preservati; il valore viene sostituito con
`[REDACTED]`. La cattura del valore assorbe anche un'eventuale seconda
parola (così un secret a due token come `Bearer abc123` viene mascherato
intero), con un lookahead negativo che impedisce di inglobare un campo
sensibile successivo. Usa solo `re` della standard library. Esempi:
`api_key=sk-test` → `api_key=[REDACTED]`; `authorization: Bearer abc123`
→ `authorization: [REDACTED]`; `password=hunter2` → `password=[REDACTED]`.
`_safe_error_message` applica `_redact_sensitive_text` prima dello strip
e del troncamento.

---

## 8. Schema record mapping

Funzioni pure che ritornano dict; **non scrivono nel DB**. I tre record
includono `tenant_id` (microfix): lo schema `0011` richiede `tenant_id`
NOT NULL in `provider_invocations`, `token_usage_records` e
`source_candidates`.

- `to_provider_invocation_record(request, result, *, attempt_no=1)` —
  ritorna esattamente le chiavi compatibili con `provider_invocations`
  (0011): `tenant_id`, `orchestration_run_id`, `agent_run_id`,
  `provider_name`, `model`, `request_hash`, `response_hash`, `status`,
  `error_code`, `error_message`, `tokens_input`, `tokens_output`,
  `cost_estimate`, `latency_ms`, `attempt_no`, `is_mock`,
  `redaction_strategy`, `idempotency_key`. `tenant_id` è preso da
  `request.tenant_id`. Non include API key, authorization, password,
  credential, secret, né request/response grezzi non redatti.
  `cost_estimate` è reso come stringa stabile.
- `to_token_usage_record(request, result, *,
  provider_invocation_id=None, pass_kind=None, attempt_no=1)` — ritorna
  le chiavi compatibili con `token_usage_records` (0011): `tenant_id`,
  `orchestration_run_id`, `agent_run_id`, `provider_invocation_id`,
  `pass_kind`, `tokens_input`, `tokens_output`, `cost_estimate`,
  `attempt_no`, `is_mock`, `idempotency_key`. `tenant_id` è preso da
  `request.tenant_id`. `provider_invocation_id` può essere `None` (lo
  schema 0011 lo consente, con i due indici UNIQUE parziali dedicati).
  Un `pass_kind` fuori codominio solleva `ValueError`, così un typo
  emerge qui e non all'INSERT.
- `source_candidates_to_records(request, result, *,
  agent_output_id=None)` — produce dict con i **nomi reali delle
  colonne** della tabella `source_candidates` come definita in
  `migrations/0011_orchestration_schema.sql`: `tenant_id`,
  `orchestration_run_id`, `master_prompt_id`, `candidate_type`,
  `status`, `agent_output_id`, `title`, `url`, `citation_text`,
  `quoted_text`, `declared_confidence`, `provenance`, `created_by`,
  `raw_citation_payload`. `tenant_id` è preso da `request.tenant_id`;
  `orchestration_run_id` da `request.orchestration_run_id`;
  `master_prompt_id` è `None` (colonna nullable in 0011: il modulo
  mock-first, scoped a un orchestration run, non ha un
  `master_prompt_id` sui propri input). Nessun `evidence_span_id`,
  nessun claim link, nessuna FK verso `evidence_spans`. Tutte le
  candidate hanno `status='proposed'` e provenance che dichiara la
  candidate non verificata.

**Decisione di mapping documentata.** Il modello logico
`ProviderSourceCandidate` porta `locator` e `raw_text`. La tabella
reale `source_candidates` (0011) **non ha** una colonna `locator`
dedicata; ha invece `citation_text` e `quoted_text`. La funzione
`source_candidates_to_records` adotta quindi una scelta conservativa,
verificata leggendo `migrations/0011_orchestration_schema.sql`:
`raw_text` viene mappato su `citation_text`; `locator` viene
trasportato dentro `provenance` (`provenance["locator"]`) così da non
perdere informazione; `quoted_text` resta `None` perché il mock non
asserisce una quote verificata; `declared_confidence` resta `None`.
Un futuro blocco che persista realmente queste righe potrà rivedere il
mapping se lo schema evolverà; nessun dato viene perso, e nessuna
colonna inesistente viene inventata.

---

## 9. Budget preflight

`enforce_mock_budget(request, *, budget_limit_tokens)`:

- `budget_limit_tokens is None` → OK (`None`);
- `count_request_input_tokens(request) > budget_limit_tokens` → ritorna
  un `ProviderError(error_code="budget_exceeded", retryable=False)`;
- altrimenti → OK (`None`).

La funzione non scrive eventi, non scrive `token_usage_records`, non
muta la `request`, non tocca DB o rete. È usata da
`MockProviderAdapter.enforce_preflight_budget`.

`count_request_input_tokens` somma le stime mock di
`system_instructions`, `task_instructions` e del contenuto di ogni
`message`. `output_contract` / `constraints` / `source_policy` sono
**esclusi** dal conteggio token, perché sono configurazione strutturale
della richiesta e non testo in linguaggio naturale inviato al modello;
includerli confonderebbe la forma della richiesta con la lunghezza del
prompt. Restano comunque inclusi in `build_request_hash`, così
l'identità della richiesta resta completa. È una scelta deterministica
e documentata.

---

## 10. Tests created

File `apps/worker/tests/test_orchestration_provider_service.py`. Test
worker-level, **senza DB, senza Redis, senza FastAPI, senza network**.
Usa `pytest`. Non importa `app.db`, non importa SQLAlchemy, non importa
psycopg, non importa client di rete. Il modulo sotto test non importa
rete, quindi non serve alcun monkeypatch di rete. Tutti gli helper sono
locali al file.

13 test richiesti dal prompt §13, più 2 test difensivi:

1. `test_default_registry_contains_only_mock_provider`
2. `test_mock_provider_success_is_deterministic_and_marked_mock`
3. `test_request_and_response_hashes_are_stable_and_canonical`
4. `test_redaction_hash_only_does_not_expose_secrets`
5. `test_redacted_payload_mode_recursively_masks_sensitive_fields`
6. `test_error_normalization_retryable_and_non_retryable`
7. `test_mock_provider_error_injection_returns_failed_result`
8. `test_provider_invocation_record_mapping_matches_schema_shape_and_has_no_secrets`
9. `test_token_usage_record_mapping_supports_nullable_provider_invocation_id`
10. `test_source_candidates_are_unverified_candidates_not_evidence`
11. `test_budget_preflight_blocks_over_budget_without_invoking_real_provider`
12. `test_module_uses_no_network_or_provider_sdk_imports`
13. `test_mock_payload_contains_semantic_warning`

Test difensivi aggiuntivi: `test_capabilities_include_required_set`,
`test_module_is_importable_without_environment`.

**Aggiornamenti dei test introdotti dal microfix:**

- gli expected key set `_PROVIDER_INVOCATION_KEYS` e `_TOKEN_USAGE_KEYS`
  includono ora `tenant_id`;
- `test_provider_invocation_record_mapping_matches_schema_shape_and_has_no_secrets`
  verifica `record["tenant_id"] == request.tenant_id`;
- `test_token_usage_record_mapping_supports_nullable_provider_invocation_id`
  verifica `record["tenant_id"] == request.tenant_id`;
- `test_source_candidates_are_unverified_candidates_not_evidence`
  verifica `tenant_id`, `orchestration_run_id`, `master_prompt_id is
  None`, l'expected key set aggiornato, e conferma che `evidence_span_id`
  resti assente;
- `test_error_normalization_retryable_and_non_retryable` è esteso con un
  caso che chiama `normalize_error(ERROR_INVALID_REQUEST, "api_key=sk-test
  authorization=Bearer abc password=hunter2")` e verifica che `sk-test`,
  `Bearer abc` e `hunter2` non compaiano, che `[REDACTED]` compaia, che
  `error_code` resti `invalid_request` e `retryable` sia `False`; più un
  caso con separatore `:`.

Nota sul test 12: la lista delle stringhe vietate è costruita a runtime
da frammenti concatenati (`_banned_import_fragments()`), così un `grep`
ingenuo di questo file di test per quelle stringhe non auto-intercetta
la lista letterale — coerente con l'avvertenza del prompt §15.

Nessun test si basa sul tempo reale.

---

## 11. Commands executed

| Comando | Eseguito | Esito |
|---|---|---|
| `python3 -m py_compile apps/worker/app/services/orchestration_provider.py` | **Sì** | OK, nessun errore. |
| `python3 -m py_compile apps/worker/tests/test_orchestration_provider_service.py` | **Sì** | OK, nessun errore. |
| `PYTHONPATH=$(pwd)/apps/worker:$(pwd)/packages/shared python3 -m pytest -q apps/worker/tests/test_orchestration_provider_service.py` | **Sì** | **15 passed** (13 richiesti + 2 difensivi). Il modulo non importa nulla da `packages/shared`, quindi la presenza di quel path su `PYTHONPATH` non incide sull'esito. |
| `grep -niE "^[[:space:]]*(import\|from)[[:space:]]+(requests\|httpx\|aiohttp\|urllib\|socket\|openai\|anthropic\|google\.generativeai\|subprocess\|fastapi\|redis\|sqlalchemy\|psycopg)\b"` sui 2 file | **Sì** | **Nessun risultato.** Questo è il controllo import-level preciso suggerito dal microfix §3: ancorato a inizio riga su `import`/`from`, non produce i falsi positivi del grep generico (che intercettava parole normali come `ProviderRequest`, `requests` in prosa, o i nomi provider usati come stringhe nei test negativi). |
| `grep -niE "[t]ruth score\|..."` (wording vietato) sui 3 file deliverable | **Sì** | Nessuna occorrenza. |
| `git diff --check` / `git diff --stat` / `git diff --name-only` / `git status -sb` | **No** | L'ambiente di lavorazione non è un checkout git del repository. Atteso in revisione: `git diff --name-only` deve mostrare esattamente `apps/worker/app/services/orchestration_provider.py`, `apps/worker/tests/test_orchestration_provider_service.py`, `ORCH_PROVIDER_A_IMPLEMENTATION_REPORT.md`; `git diff --check` nessun errore di whitespace. |

**Controllo statico aggiuntivo eseguito offline.** Gli import effettivi
del modulo sono stati ispezionati via AST: il modulo importa
esclusivamente `abc` (`ABC`, `abstractmethod`), `dataclasses`,
`decimal` (`Decimal`), `enum`, `hashlib`, `json`, `re`, `typing`
(`Any`), `uuid`, più `from __future__ import annotations`. Nessun
modulo di rete, nessun SDK provider, nessun `subprocess`, nessun
`fastapi`/`redis`/`sqlalchemy`/`psycopg`. L'import `time` — ammesso dal
prompt ma non necessario — non è presente; `re` è importato perché
usato dalla redaction string-safe dell'`error_message`.

I comandi git non sono stati eseguiti perché l'ambiente di lavorazione
non dispone del checkout git del repository `Tzuci/evidence-first`; vanno
eseguiti dal revisore.

---

## 12. Non-scope guarantees

ORCH-PROVIDER-A dichiara esplicitamente:

- **Nessun provider reale.** Nessun OpenAI, Anthropic, Gemini o altro
  provider esterno è introdotto o referenziato in modo operativo.
- **Nessun SDK provider.** Nessun SDK di provider è importato o aggiunto
  come dipendenza.
- **Nessun secret.** Nessuna credenziale, API key, token di
  autenticazione o secret è introdotto in alcun file; il
  `ProviderRegistry` non contiene segreti; i record di mapping non
  contengono campi segreti.
- **Nessuna rete.** Il modulo non importa `requests`, `httpx`,
  `aiohttp`, `urllib`, `socket`; non apre connessioni; non esegue I/O
  di rete.
- **Nessun DB write.** Il modulo non importa SQLAlchemy, psycopg o
  `app.db`; non scrive su `provider_invocations`, non scrive su
  `token_usage_records`, non scrive da nessuna parte. Le funzioni di
  mapping ritornano dict in memoria.
- **Nessuna migration.** Nessun file in `migrations/` è creato o
  modificato.
- **Nessuna integrazione runtime API/worker.** Nessun consumer, nessun
  worker di orchestrazione, nessuna route HTTP è creato o modificato.
- **Nessuna UI.** Nessuna pagina o componente frontend.
- **Nessun gate parallelo.** Il modulo non introduce alcuna autorità di
  decisione di pubblicabilità; il Final Answer Gate resta l'unico gate.
- **MockProviderAdapter mock/deterministico.** Non produce intelligenza
  reale, non sostituisce un provider remoto, non sostituisce un local
  LLM. Usage e costo sono esplicitamente marcati mock/simulati.
- **Provider output resta candidato.** L'output del provider non è
  verità, non è risposta pubblicabile; le source candidate prodotte
  sono candidate non verificate (`status='proposed'`,
  `is_verified=False`); `request_hash`/`response_hash` servono audit e
  idempotenza, non provano il contenuto.

---

## 13. Risks/open decisions

- **Mapping `locator` per `source_candidates`.** Il modello
  `ProviderSourceCandidate` ha un campo `locator` che non ha una
  colonna dedicata nella tabella `source_candidates` di `0011`. La
  funzione `source_candidates_to_records` adotta una scelta
  conservativa: trasporta `locator` dentro `provenance` e mappa
  `raw_text` su `citation_text`. Se un blocco futuro (ORCH-SOURCES-A o
  simili) decidesse di aggiungere una colonna `locator` con una
  migration additiva, il mapping andrà rivisto. Nessun dato è perso
  oggi.
- **`pass_kind` come parametro esplicito.** `to_token_usage_record`
  accetta `pass_kind` come parametro opzionale e lo valida contro il
  codominio dello schema. Il modulo mock-first non determina da solo a
  quale pass un consumo appartiene: è il futuro worker di
  orchestrazione a doverlo fornire. Decisione coerente con la natura
  worker-level/no-runtime di questa fase.
- **`count_request_input_tokens` esclude `output_contract` /
  `constraints` / `source_policy`.** Scelta documentata (§9): questi
  campi sono configurazione strutturale, non testo del prompt, e non
  inflazionano la stima token. Restano nell'hash della richiesta. Un
  futuro provider reale con un token accounting reale ridefinirà
  comunque questa stima.
- **Stima mock di usage.** `estimate_mock_tokens` è un'euristica locale
  (numero di parole, floor 1). Non è token accounting reale e non va
  mai presentata come tale; il modulo lo dichiara nella docstring e
  marca ogni `ProviderUsage` con `is_mock=True`.
- **`parse_response` del mock.** Per il mock la "risposta grezza"
  canonica è un `ProviderResult` già prodotto da `invoke()`;
  `parse_response` lo restituisce invariato quando ne riceve uno, e
  tratta qualunque altro valore come `malformed_response`. Un provider
  reale futuro implementerà un vero parsing della risposta di
  trasporto.
- **Redaction string-safe dell'`error_message` — over-redaction
  benigna.** `_redact_sensitive_text` assorbe un'eventuale seconda
  parola dopo il valore sensibile per mascherare interi token come
  `Bearer abc123`. In un messaggio come `api_key: sk-9999 at the
  endpoint` questo può assorbire anche la parola successiva non
  sensibile (`at`), producendo `api_key: [REDACTED] the endpoint`. È
  un'over-redaction benigna: erra verso il mascheramento, non verso il
  leak. Un lookahead negativo impedisce comunque di inglobare un campo
  sensibile adiacente, così campi consecutivi sono mascherati ciascuno
  in modo indipendente. Un futuro affinamento potrebbe restringere la
  cattura del valore; per ora la priorità è non far trapelare segreti.

---

## 14. Final state

La fase ORCH-PROVIDER-A è **ready for human review**.

I tre file deliverable sono stati prodotti e validati offline:
`py_compile` su entrambi i file di codice OK; suite `pytest` eseguita,
**15 test verdi** (13 richiesti + 2 difensivi); grep di assenza
rete/SDK provider verificato (nessun import reale vietato); grep di
wording vietato verificato (nessuna occorrenza). I comandi `git` di
verifica del perimetro vanno eseguiti dal revisore in un checkout del
repository: devono mostrare come modificati solo i tre file ammessi.

**Conferme finali**: nessun provider reale; nessun SDK; nessun secret;
nessuna rete; nessun DB write; nessuna migration; nessuna integrazione
runtime API/worker; nessuna UI; nessun gate parallelo; il
`MockProviderAdapter` è mock e deterministico e non produce intelligenza
reale; il provider output resta candidato.

**Nessun commit è stato eseguito.** Commit suggerito, solo dopo revisione
umana: `Add mock provider adapter foundation`.
