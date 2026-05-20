# Evidence-First MVP-0 — Local development runbook

## 1. Purpose

This document is the operational reference for working on the
Evidence-First MVP-0 stack locally. It explains how to:

- start every module in the correct order;
- stop every module cleanly;
- restart a single module without disturbing the others;
- diagnose the most common local development problems.

It is **operational documentation only**. It does not modify the
backend, the worker, the web application, the database schema, or
any Docker / Makefile configuration. Where it cites a command, that
command exists today in the repository and behaves as described.

This runbook is conservative on purpose. It does not promise that
the system "eliminates hallucinations" or that any answer is
"verified true". The platform is **evidence-first**: it tracks the
evidence each claim is based on and may **hold publication** when
the supporting evidence is insufficient. See `README.md` and
`PROJECT_STATE.md` for the architectural commitments.

---

## 2. Module overview

The local stack is composed of seven moving parts. Each row lists
the module, where it lives, the local TCP port (when applicable),
what it does, what depends on it, and what it depends on.

### 2.1 PostgreSQL

- Container service: `db` (image `postgres:16-alpine`).
- Container name: `ef_db`.
- Port: `5432`.
- Role: durable storage for tenants, projects, tasks, documents,
  claim ledger, source quality assessments, claim entailment checks,
  draft/gate/published answers, lifecycle and source-loss events,
  audit chain.
- Depends on: nothing locally (Docker Engine must be running).
- Depended on by: API, Worker, migrations, seed.

### 2.2 Redis

- Container service: `redis` (image `redis:7-alpine`).
- Container name: `ef_redis`.
- Port: `6379`.
- Role: Redis Streams transport between API (event producer) and
  Worker (event consumer). Stream names include
  `app.events.task_created`,
  `app.events.published_answer_withdrawal_requested`,
  `app.events.source_loss_detected`.
- Depends on: Docker Engine.
- Depended on by: API (publishes events), Worker (consumes events).

### 2.3 Database migrations

- Entry point: `scripts/migrate.py`, invoked via `make migrate`.
- Role: applies SQL migrations under `migrations/` in order (0001
  through 0010 at the time of writing). Idempotent: rerunning is a
  no-op.
- Depends on: PostgreSQL reachable on `DATABASE_URL`.
- Depended on by: API (needs the schema to start serving requests),
  Worker (needs the schema to read and write events / rows), Seed.

### 2.4 Development seed

- Entry point: `scripts/seed_dev.py`, invoked via `make seed`.
- Role: inserts development fixtures (default tenant, demo project,
  etc.) into the database so smoke tests and the UI have data to
  operate on. Idempotent in practice.
- Depends on: migrations applied.
- Depended on by: nothing strictly required at runtime, but most
  smoke flows assume the seed has been run at least once.

### 2.5 API FastAPI

- Source: `apps/api/`.
- Entry point: `apps/api/app/main.py` (`app = create_app()`).
- Port: `8000`.
- Role: HTTP surface for projects, tasks, documents, claims,
  answers, lifecycle, source quality, claim entailment, the
  Anti-Hallucination Report aggregated endpoint, and the health
  probes.
- Depends on: PostgreSQL (`DATABASE_URL`), Redis (`REDIS_URL`),
  filesystem storage (`STORAGE_LOCAL_ROOT`), the migrations applied.
- Depended on by: the Web report pages, manual smoke tests, the
  Worker indirectly through the events the API publishes.

### 2.6 Worker Redis Streams

- Source: `apps/worker/`.
- Entry point: `apps/worker/app/main.py` (module run via
  `python -m app.main`).
- No HTTP port: the Worker is a single-consumer loop that calls
  `XREADGROUP` against the configured streams and dispatches events
  to handlers.
- Role: drives the pipeline: extractor → CVE-lite → Source Quality
  → Claim Entailment → Compiler → Final Answer Gate; processes
  withdrawal and source-loss events.
- Depends on: PostgreSQL, Redis, migrations applied.
- Depended on by: any flow that needs a task to progress beyond
  `created`/`analyzing`. The Web home and the report pages do not
  depend on the Worker for rendering, but new tasks and events
  require the Worker to be running.

### 2.7 Web Next.js

- Source: `apps/web/`.
- Entry point: `next dev -p 3000` (script `dev`).
- Port: `3000`.
- Role: minimal browser UI. Today it renders the product home (`/`),
  the diagnostic page (`/diagnostic`), and the technical report
  viewer (`/tasks/<taskId>/report`).
- Depends on:
  - the report page depends on the API (it fetches
    `GET /api/v1/tasks/{task_id}/anti-hallucination-report` from
    `NEXT_PUBLIC_API_BASE_URL`);
  - the home page does not depend on the API at runtime.
- Depended on by: nothing else in the stack.

---

## 3. Dependency graph

The startup ordering follows from these dependencies:

```
PostgreSQL + Redis
        |
        v
Migrations + Seed
        |
        v
API FastAPI            Worker Redis Streams
        |                       ^
        v                       |
Web report pages       (consumes from Redis,
                        writes to PostgreSQL)
```

Concretely:

- **API depends on DB and Redis.** API will refuse to be `ready`
  (`GET /health/ready`) until both are reachable.
- **Worker depends on DB and Redis.** Without either, the Worker
  cannot consume events or persist state.
- **Web home does not depend on the API.** The home renders even
  with the API down.
- **Web report depends on the API.** The page
  `/tasks/<taskId>/report` issues `fetch(... no-store)` against the
  API; without the API it renders an "API unreachable" diagnostic.
- **Creating or progressing tasks requires the Worker.** The API
  accepts the request and publishes the event, but only the Worker
  advances the task through the pipeline.

---

## 4. Startup order

When starting from a cold machine:

1. Enter the repository root.
2. `git pull` if you want the latest tip.
3. Start Docker services (DB and Redis): `make up`.
4. Load `.env` into the current shell.
5. Prepare `DATABASE_URL` and `REDIS_URL` with sensible fallbacks.
6. Apply migrations: `make migrate`.
7. Run the development seed: `make seed`.
8. Start the API on port 8000.
9. Start the Worker.
10. Start the Web on port 3000.
11. Verify health endpoints and that the browser can load `/`.

The next sections give the manual commands and the helper scripts.

---

## 5. Manual startup commands

The commands below are deliberately generic. None of them assumes a
specific user account or absolute path. They assume the current
working directory is the repository root, which the snippet derives
from git:

```
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
```

If you prefer a stable path on your machine, you may use something
like `cd ~/projects/evidence-first` instead. The scripts in
`scripts/` always use `git rev-parse --show-toplevel`.

### 5.1 Docker services

```
make up
```

This starts the `db` and `redis` containers in the background and
creates the `storage/` directory used by the API for local
content-addressed storage.

### 5.2 Environment

```
set -a
source .env
set +a
```

Then prepare the URLs the migration / seed / API / Worker will
read, with fallbacks that match `docker-compose.dev.yml`:

```
export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@localhost:5432/${POSTGRES_DB}}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
```

### 5.3 Migrations and seed

```
PYTHON=.venv/bin/python make migrate
PYTHON=.venv/bin/python make seed
```

If you do not maintain a `.venv`, drop the `PYTHON=...` prefix and
let the Makefile use the system `python3`. The Makefile already
falls back to `python3` when `PYTHON` is unset.

### 5.4 API FastAPI

In a dedicated terminal:

```
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT/apps/api"
DATABASE_URL="$DATABASE_URL" \
REDIS_URL="$REDIS_URL" \
PYTHONPATH="$ROOT/packages/shared:." \
"$ROOT/.venv/bin/python" -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

If you do not have a virtualenv at `.venv`, replace
`"$ROOT/.venv/bin/python"` with `python3`.

### 5.5 Worker Redis Streams

In a dedicated terminal:

```
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT/apps/worker"
DATABASE_URL="$DATABASE_URL" \
REDIS_URL="$REDIS_URL" \
PYTHONPATH="$ROOT/packages/shared:." \
"$ROOT/.venv/bin/python" -m app.main
```

### 5.6 Web Next.js

In a dedicated terminal:

```
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT/apps/web"
NEXT_PUBLIC_API_BASE_URL="http://localhost:8000" npm run dev
```

The `NEXT_PUBLIC_API_BASE_URL` value is read by
`apps/web/lib/api.ts` and falls back to `http://localhost:8000`
when not set, but it is good practice to be explicit.

---

## 6. Manual shutdown commands

Stop in the reverse order of startup. Each foreground process can
be stopped with `Ctrl+C`. If a process has been backgrounded or its
terminal lost, free the ports and kill by pattern:

```
fuser -k 3000/tcp 2>/dev/null || true
fuser -k 8000/tcp 2>/dev/null || true
pkill -f "next dev" 2>/dev/null || true
pkill -f "uvicorn app.main:app" 2>/dev/null || true
pkill -f "python.*-m app.main" 2>/dev/null || true
```

Then stop the Docker services (this preserves the volumes):

```
make down
```

**Do not** use `make clean` as part of a routine shutdown:
`make clean` runs `docker compose down -v` and removes the
`ef_postgres_data` and `ef_redis_data` volumes, destroying all
local data.

---

## 7. Restarting one module

The Docker services, migrations and seed normally stay up for an
entire development session. The three foreground processes (Web,
API, Worker) are the ones you restart most often. Each subsection
below explains when to restart that module and how to do it without
disturbing the others.

### 7.1 Restart Web only

When to restart:

- you changed something under `apps/web/` (a component, a route, a
  style);
- Next prints an error during compilation;
- the `.next` cache is corrupted (typical symptom:
  `Cannot find module './<digit>.js'`);
- port 3000 is occupied by a leftover process.

Sequence:

1. Stop the running `next dev` (Ctrl+C in its terminal, or
   `fuser -k 3000/tcp` if you lost the terminal).
2. Optionally remove the cache: `rm -rf apps/web/.next`.
3. Restart it:
   ```
   cd apps/web
   NEXT_PUBLIC_API_BASE_URL="http://localhost:8000" npm run dev
   ```

Restarting Web does not require restarting the API, the Worker, or
the Docker services.

### 7.2 Restart API only

When to restart:

- you changed something under `apps/api/`;
- the API is returning 500s and you want to clear in-memory state;
- port 8000 is occupied;
- you changed environment variables in `.env`.

Dependencies that must already be up:

- PostgreSQL;
- Redis;
- migrations applied.

Sequence:

1. Stop the running uvicorn (Ctrl+C, or
   `fuser -k 8000/tcp` / `pkill -f "uvicorn app.main:app"`).
2. Confirm DB and Redis are still running: `docker ps`.
3. Reload `.env` if it changed:
   ```
   set -a
   source .env
   set +a
   ```
4. Restart uvicorn with the command from §5.4.

You usually do not need to restart the Worker after restarting the
API: the Worker reads from Redis directly and is not aware of API
process restarts. The exception is when you also changed shared
code under `packages/shared/` that the Worker imports; in that case
restart the Worker too.

### 7.3 Restart Worker only

When to restart:

- you changed something under `apps/worker/` (services, consumers,
  dispatch);
- you restarted Redis;
- pending events have accumulated on a stream and you want a clean
  consumer state;
- the pipeline is not advancing (tasks stuck on `analyzing` or
  `analyzed_partial`).

Dependencies that must already be up:

- PostgreSQL;
- Redis.

Sequence:

1. Stop the Worker (Ctrl+C, or
   `pkill -f "python.*-m app.main"`).
2. Reload `.env` if it changed.
3. Restart the Worker with the command from §5.5.

### 7.4 Restart DB and Redis

PostgreSQL and Redis are strong dependencies of both the API and
the Worker. If you restart either container, the API and the
Worker may still be alive but holding stale connections; the
cleanest path is to stop everything that depends on them first,
restart Docker, then bring the dependents back up in order.

Safe sequence:

1. Stop the Worker.
2. Stop the API.
3. Stop the Web if it is rendering dynamic pages (the report
   page).
4. `make down`.
5. `make up`.
6. `PYTHON=.venv/bin/python make migrate`.
7. `PYTHON=.venv/bin/python make seed`.
8. Start the API.
9. Start the Worker.
10. Start the Web.

---

## 8. Health checks

The fastest way to verify the API is up is to call the FastAPI
health endpoints directly:

```
curl -i http://localhost:8000/health/live
curl -i http://localhost:8000/health/ready
curl -I http://localhost:3000
```

How to interpret the results:

- `GET /health/live` returning **200** means the API process is
  alive and accepting requests.
- `GET /health/ready` returning **200** means DB, Redis and the
  storage filesystem are all reachable from the API.
- `GET /health/ready` returning **503** means the API process is
  alive but at least one dependency is not ready. The response
  body lists the per-dependency status; use it to pinpoint which
  one to inspect.
- `curl -I http://localhost:3000` returning **200** means Next is
  serving.
- `curl: connection refused` means the process behind that port is
  not running, or it is bound to a different host/port.

A note on `/diagnostic`: the page at `apps/web/app/diagnostic/page.tsx`
proxies through a web route called `/api/proxy-health`. That route
does not exist in the current repository, so `/diagnostic` may
display `proxy-health returned status 404`. This is a known UI
quirk; **`/diagnostic` is not a reliable indicator of API health**.
Use the `curl` commands above against `http://localhost:8000/health/*`
instead.

---

## 9. Report testing

The product home at `/` is a static server component and renders
even when the API is unreachable. The technical report at
`/tasks/<taskId>/report` issues an HTTP call to the API and is the
right place to verify end-to-end browser-to-API connectivity.

The all-zero UUID (`00000000-0000-0000-0000-000000000000`) is a
**demo error-state link** exposed from the home page. Opening it
returns `Task not found` because that UUID does not correspond to
any real task in the database; this is the expected behavior and a
useful smoke test that the page can talk to the API.

The same shape can be reached directly via the API:

```
curl -i http://localhost:8000/api/v1/tasks/00000000-0000-0000-0000-000000000000/anti-hallucination-report
```

It returns a normalized 404 envelope with
`details.resource="task_masters"`.

To find a real task id, query the database:

```
docker exec -it ef_db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT id, status, objective, created_at
FROM task_masters
ORDER BY created_at DESC
LIMIT 10;
"'
```

Then open the report at
`http://localhost:3000/tasks/<that-id>/report`. The report is a
read-only derived view: it shows whatever the pipeline has already
persisted. Publication can be **held** when the controls find
insufficient support for one or more claims; that is a normal,
designed outcome, not a system error.

---

## 10. Debug cookbook

### 10.1 Port already in use

Symptom: starting Web prints `EADDRINUSE :::3000`, or starting the
API prints `[Errno 98] Address already in use`.

Diagnostic and remediation:

```
ss -ltnp | grep ':3000'
ss -ltnp | grep ':8000'
fuser -k 3000/tcp 2>/dev/null || true
fuser -k 8000/tcp 2>/dev/null || true
```

Then start the module again.

### 10.2 API unreachable

Symptom: `curl http://localhost:8000/health/live` returns
`connection refused`, or the report page shows "API unreachable".

Diagnostic:

```
ss -ltnp | grep ':8000'
curl -i --max-time 5 http://localhost:8000/health/live
```

If `ss` does not show a listener on `:8000`, the API process is not
running; restart it per §7.2. If `ss` shows a listener but `curl`
times out, inspect the uvicorn terminal for tracebacks.

### 10.3 API returns 500 or `OperationalError`

Symptom: API endpoints return 500. The uvicorn log mentions
`OperationalError`, `connection refused`, or `relation does not
exist`.

Diagnostic and remediation:

```
docker ps
echo "$DATABASE_URL"
PYTHON=.venv/bin/python make migrate
PYTHON=.venv/bin/python make seed
```

`docker ps` should list `ef_db` and `ef_redis` as `Up`. If
`DATABASE_URL` is empty in the API's terminal, the API was started
without the environment variable: stop it and restart per §5.4.

### 10.4 Next.js cache error

Symptom: Next prints something like
`Cannot find module './112.js'` or similar after a hot reload that
went wrong.

Remediation:

```
fuser -k 3000/tcp 2>/dev/null || true
pkill -f "next dev" 2>/dev/null || true
cd apps/web
rm -rf .next
NEXT_PUBLIC_API_BASE_URL="http://localhost:8000" npm run dev
```

### 10.5 `/diagnostic` proxy-health 404

Symptom: visiting `http://localhost:3000/diagnostic` shows
`proxy-health returned status 404`.

This is the known UI quirk described in §8: the page calls a web
proxy route that is not present in the current repository. The
correct way to check API health is the curl on the API health
endpoints, not the `/diagnostic` page.

### 10.6 `role "<name>" does not exist`

Symptom: running `psql -U dev` (or similar) from the host fails
because `dev` is not the Postgres user defined in `.env.example`.

Resolution: do not hardcode a username. Open the psql shell through
the container with the variables it already has:

```
docker exec -it ef_db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

### 10.7 Worker not processing

Symptom: a task stays on `analyzing` or `analyzed_partial`; new
events do not move forward.

Diagnostic:

```
docker exec -it ef_redis redis-cli ping
ps aux | grep -E "python.*-m app.main" | grep -v grep
docker exec -it ef_redis redis-cli XINFO STREAM app.events.task_created
docker exec -it ef_redis redis-cli XPENDING app.events.task_created worker_default
```

If `ping` returns `PONG` but no `python.*-m app.main` process
exists, the Worker is not running; start it per §5.5. If `XPENDING`
lists entries with a high idle time, the Worker may have crashed
mid-event; restart it and inspect the previous Worker log for the
underlying error.

---

## 11. Full startup script

A convenience script is provided at `scripts/dev-start-all.sh`. It
mirrors §4 and §5 of this document. Read the script before running
it; it is short and intentionally generic.

What the script does, in order:

1. Locates the repository root with `git rev-parse --show-toplevel`.
2. Creates `.dev/logs/` and `.dev/pids/` for runtime artifacts.
3. Verifies the basic toolchain (`docker`, `make`, `npm`, `git`).
4. Picks a Python interpreter: `.venv/bin/python` when present,
   otherwise `python3`.
5. Frees ports `API_PORT` (default 8000) and `WEB_PORT` (default
   3000) so a leftover process cannot block the restart.
6. Loads `.env` if present and applies the documented fallbacks for
   `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, then exports
   `DATABASE_URL` and `REDIS_URL`. This happens **before** `make up`
   so Docker Compose receives valid substitution values even on a
   brand-new clone with no `.env`.
7. Brings up Docker services with `make up`.
8. Runs `make migrate` and `make seed`.
9. Starts the API in the background, writing PID and log.
10. Starts the Worker in the background, writing PID and log.
11. If `apps/web/node_modules` is missing, runs `npm install` inside
    `apps/web` once (subsequent runs skip the install).
12. Starts the Web in the background via `npx --no-install next dev
    -p $WEB_PORT`. The package.json `dev` script hardcodes port
    3000, so the script bypasses it to honor the `WEB_PORT`
    override.
13. Waits a few seconds and verifies that all three PIDs (API,
    Worker, Web) are still alive. If any process has already
    terminated, the script points at the relevant log file and
    exits non-zero.
14. Prints the URLs the developer can open in the browser and the
    health endpoints to curl.

The script supports overriding the ports:

```
API_PORT=8001 WEB_PORT=3001 ./scripts/dev-start-all.sh
```

The script **never** removes volumes and **never** runs
`make clean`.

The script content lives in `scripts/dev-start-all.sh`. Inspect it
with `cat scripts/dev-start-all.sh` before running.

---

## 12. Full stop script

A complementary script is provided at `scripts/dev-stop-all.sh`. It
mirrors §6.

What the script does, in order:

1. Locates the repository root with `git rev-parse --show-toplevel`.
2. Reads the PID files written by `dev-start-all.sh` (when
   present) and sends `SIGTERM`, escalating to `SIGKILL` if a
   process refuses to exit.
3. Frees the ports as a fallback for processes that were not
   started by `dev-start-all.sh`.
4. Calls `make down` to stop the Docker services without removing
   the volumes.
5. Prints a final status overview (free ports, `docker ps`).

The script also supports overriding the ports:

```
API_PORT=8001 WEB_PORT=3001 ./scripts/dev-stop-all.sh
```

The script **never** runs `make clean` and **never** passes `-v` to
`docker compose down`.

---

## 13. Script portability and caveats

`scripts/dev-start-all.sh` and `scripts/dev-stop-all.sh` target
**Unix-like shells**: Linux, macOS, and Windows Subsystem for Linux
(WSL). They are plain Bash scripts and rely on standard POSIX tools
(`kill`, `printf`, `cat`, `sleep`, `mkdir`) plus `git`, `make`,
`docker`, `npm`, and either `python3` or a project-local
`.venv/bin/python`.

They are **not** native PowerShell or CMD scripts and will not run
as-is on Windows outside WSL. On a Windows host, the recommended
path is to clone the repository inside WSL and run the scripts
from a WSL shell.

The scripts free TCP ports `3000` and `8000` (or whatever
`WEB_PORT` and `API_PORT` were set to) before starting and as a
fallback during stop. The freeing step uses `fuser -k <port>/tcp`
when available, otherwise `lsof -ti tcp:<port> | xargs kill`. Be
aware that **this will terminate any process holding those ports**,
including unrelated services you may be running locally (a personal
project on port 3000, a system service bound to 8000, and so on).
If you keep other local servers on these ports, either stop them
before running the script, or override `API_PORT` / `WEB_PORT` to
free ports you actually own:

```
API_PORT=8001 WEB_PORT=3001 ./scripts/dev-start-all.sh
API_PORT=8001 WEB_PORT=3001 ./scripts/dev-stop-all.sh
```

A few other behaviors worth knowing:

- `dev-start-all.sh` loads `.env` and exports `POSTGRES_USER`,
  `POSTGRES_PASSWORD`, `POSTGRES_DB`, `DATABASE_URL`, and
  `REDIS_URL` **before** calling `make up`. This is required
  because `docker-compose.dev.yml` substitutes those variables at
  Compose-up time: an empty value during the first
  `docker compose up` would leave the Postgres volume uninitialized
  and would force a `make clean` to recover.
- On a brand-new clone where `apps/web/node_modules` does not yet
  exist, `dev-start-all.sh` runs `npm install` inside `apps/web`
  once. The install is skipped on subsequent runs.
- The Web is launched via `npx --no-install next dev -p $WEB_PORT`
  rather than `npm run dev`. The `dev` script in
  `apps/web/package.json` hardcodes `next dev -p 3000`, so the
  `WEB_PORT` override would otherwise be ignored.
- After spawning API, Worker, and Web, the start script waits a
  few seconds and verifies each PID is still alive. If a process
  has already terminated, the script prints which log file to
  inspect and exits non-zero. The other survivors keep running:
  use `scripts/dev-stop-all.sh` to clean them up before retrying.

---

## 14. Validation

Whenever this document or the scripts are modified, run:

```
git diff --check
git diff --stat
git status -sb
```

For the shell scripts, run a syntax check (no execution):

```
bash -n scripts/dev-start-all.sh
bash -n scripts/dev-stop-all.sh
chmod +x scripts/dev-start-all.sh scripts/dev-stop-all.sh
```

The scripts are not exercised end-to-end by the test suite. They
are local convenience tools; the cost of running them is local
state changes (containers up, ports occupied) and the cost of
running the stop script counterpart afterward.

---

## 15. Wording reminder

This runbook does not promise that the system delivers
"hallucination-free" answers or that any output is "verified true"
or "AI verified". The platform is evidence-first: it persists
evidence, runs deterministic checks against the available sources,
and may **hold publication** when the checks find insufficient
support. The Anti-Hallucination Report is a derived read-only view
over those facts; it is not a new decision and does not recompute
the Final Answer Gate. See `README.md`, `PROJECT_STATE.md` and
`PHASE_8_8B_REPORT_PRE.md` for the architectural commitments behind
this wording.
