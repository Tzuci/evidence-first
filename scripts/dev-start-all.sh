#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# scripts/dev-start-all.sh
#
# Bring up the entire local Evidence-First MVP-0 stack:
#   1. Docker services (PostgreSQL, Redis) via `make up`.
#   2. Database migrations and the development seed.
#   3. The FastAPI server (apps/api), backgrounded.
#   4. The Redis Streams worker (apps/worker), backgrounded.
#   5. The Next.js web app (apps/web), backgrounded.
#
# This script is intentionally generic:
#   - It uses `git rev-parse --show-toplevel` to find the repository root.
#     It never hardcodes a user-specific path.
#   - It uses `.venv/bin/python` if present, otherwise falls back to
#     `python3`.
#   - It never removes Docker volumes. It does not call `make clean` and
#     never passes `-v` to `docker compose down`.
#   - It can be re-run safely: leftover processes bound to the API or Web
#     ports are killed before starting.
#   - It loads .env and exports POSTGRES_USER / POSTGRES_PASSWORD /
#     POSTGRES_DB / DATABASE_URL / REDIS_URL BEFORE 'make up', so a brand
#     new clone with no .env file still bootstraps Postgres correctly.
#   - It runs 'npm install' inside apps/web on a fresh clone (when
#     node_modules is missing), then drives Next via `npx --no-install`
#     so the WEB_PORT override is honored (the package.json `dev` script
#     hardcodes port 3000).
#   - After spawning API / Worker / Web it waits a few seconds and
#     verifies each PID is still alive; it exits non-zero and points at
#     the relevant log if any process has already terminated.
#
# Platform: Unix-like shells (Linux, macOS, WSL). This is NOT a native
# PowerShell or CMD script and will not run as-is on Windows without WSL.
# Requirements: docker, make, npm, git, python3 (or a .venv) on PATH.
#
# Environment overrides:
#   API_PORT    default 8000
#   WEB_PORT    default 3000
#
# PID and log files live under <repo>/.dev:
#   .dev/pids/{api,worker,web}.pid
#   .dev/logs/{api,worker,web}.log
#
# See docs/LOCAL_DEV_RUNBOOK.md for the manual equivalent of each step.
# ---------------------------------------------------------------------------

set -euo pipefail

# --- locate the repository root --------------------------------------------
if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: 'git' is required to locate the repository root." >&2
    exit 1
fi

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

# --- port configuration (overridable) --------------------------------------
API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3000}"

# --- runtime artifact directories ------------------------------------------
DEV_DIR="$ROOT/.dev"
LOG_DIR="$DEV_DIR/logs"
PID_DIR="$DEV_DIR/pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

API_LOG="$LOG_DIR/api.log"
WORKER_LOG="$LOG_DIR/worker.log"
WEB_LOG="$LOG_DIR/web.log"

API_PID="$PID_DIR/api.pid"
WORKER_PID="$PID_DIR/worker.pid"
WEB_PID="$PID_DIR/web.pid"

# --- helpers ---------------------------------------------------------------
log() {
    printf '[dev-start-all] %s\n' "$*"
}

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERROR: required command '$1' is not on PATH." >&2
        exit 1
    fi
}

free_tcp_port() {
    # Best-effort: free a TCP port. Both fuser and lsof are optional;
    # if neither is available we just skip and let the process below
    # surface its own EADDRINUSE.
    local port="$1"
    if command -v fuser >/dev/null 2>&1; then
        fuser -k "${port}/tcp" 2>/dev/null || true
    elif command -v lsof >/dev/null 2>&1; then
        local pids
        pids="$(lsof -ti tcp:"${port}" 2>/dev/null || true)"
        if [ -n "$pids" ]; then
            # shellcheck disable=SC2086
            kill $pids 2>/dev/null || true
        fi
    fi
}

# Pick a Python interpreter. The conventional virtualenv path is
# .venv/bin/python at the repo root; if it is missing we use python3.
pick_python() {
    if [ -x "$ROOT/.venv/bin/python" ]; then
        echo "$ROOT/.venv/bin/python"
    else
        echo "python3"
    fi
}

# --- tool checks -----------------------------------------------------------
require_cmd docker
require_cmd make
require_cmd npm

PYTHON_BIN="$(pick_python)"
log "Using Python interpreter: $PYTHON_BIN"
if ! "$PYTHON_BIN" --version >/dev/null 2>&1; then
    echo "ERROR: '$PYTHON_BIN' is not runnable." >&2
    exit 1
fi

# --- free ports a leftover process might still be holding ------------------
log "Freeing ports ${API_PORT}/tcp and ${WEB_PORT}/tcp (if held)."
free_tcp_port "$API_PORT"
free_tcp_port "$WEB_PORT"

# --- load .env BEFORE 'make up' --------------------------------------------
# docker-compose.dev.yml uses ${POSTGRES_USER}, ${POSTGRES_PASSWORD} and
# ${POSTGRES_DB} verbatim in the 'db' service definition. If these are
# unset when we call `make up`, Compose will substitute the empty string
# and Postgres will fail to initialize a fresh volume. We therefore load
# .env and apply the documented fallbacks BEFORE the first Compose call,
# so a brand-new clone with no .env file still receives valid values.
if [ -f "$ROOT/.env" ]; then
    log "Loading environment from .env."
    set -a
    # shellcheck source=/dev/null
    source "$ROOT/.env"
    set +a
else
    log "No .env file found; relying on existing environment and fallbacks."
fi

# Fallbacks mirror .env.example / docker-compose.dev.yml. We fill in the
# values only when they were not set by .env or the calling shell.
: "${POSTGRES_USER:=evidencefirst}"
: "${POSTGRES_PASSWORD:=devpassword}"
: "${POSTGRES_DB:=evidencefirst}"
export POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB

export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@localhost:5432/${POSTGRES_DB}}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"

log "POSTGRES_USER=$POSTGRES_USER POSTGRES_DB=$POSTGRES_DB"
log "DATABASE_URL=$DATABASE_URL"
log "REDIS_URL=$REDIS_URL"

# --- Docker services -------------------------------------------------------
log "Starting Docker services (db, redis) via 'make up'."
make up

# --- migrations and seed ---------------------------------------------------
log "Applying migrations."
PYTHON="$PYTHON_BIN" make migrate

log "Running development seed."
PYTHON="$PYTHON_BIN" make seed

# --- start API -------------------------------------------------------------
log "Starting API on port ${API_PORT} (log: ${API_LOG})."
(
    cd "$ROOT/apps/api"
    DATABASE_URL="$DATABASE_URL" \
    REDIS_URL="$REDIS_URL" \
    PYTHONPATH="$ROOT/packages/shared:." \
    nohup "$PYTHON_BIN" -m uvicorn app.main:app \
        --host 0.0.0.0 \
        --port "$API_PORT" \
        >"$API_LOG" 2>&1 &
    echo $! >"$API_PID"
)
log "API PID: $(cat "$API_PID")"

# --- start Worker ----------------------------------------------------------
log "Starting Worker (log: ${WORKER_LOG})."
(
    cd "$ROOT/apps/worker"
    DATABASE_URL="$DATABASE_URL" \
    REDIS_URL="$REDIS_URL" \
    PYTHONPATH="$ROOT/packages/shared:." \
    nohup "$PYTHON_BIN" -m app.main \
        >"$WORKER_LOG" 2>&1 &
    echo $! >"$WORKER_PID"
)
log "Worker PID: $(cat "$WORKER_PID")"

# --- start Web -------------------------------------------------------------
# 'npm run dev' uses the package.json script "next dev -p 3000", which
# hardcodes the port; an extra `-- -p $WEB_PORT` is silently ignored by
# some Next/npm combinations. We therefore drive Next directly via npx
# so $WEB_PORT actually takes effect.
#
# We also run `npm install` on a fresh clone (no node_modules yet) before
# the first invocation. The install is skipped when node_modules already
# exists, so subsequent runs stay fast.
if [ ! -d "$ROOT/apps/web/node_modules" ]; then
    log "apps/web/node_modules missing; running 'npm install' (one-time)."
    (
        cd "$ROOT/apps/web"
        npm install --no-audit --no-fund
    )
fi

log "Starting Web on port ${WEB_PORT} (log: ${WEB_LOG})."
(
    cd "$ROOT/apps/web"
    NEXT_PUBLIC_API_BASE_URL="http://localhost:${API_PORT}" \
    nohup npx --no-install next dev -p "$WEB_PORT" \
        >"$WEB_LOG" 2>&1 &
    echo $! >"$WEB_PID"
)
log "Web PID: $(cat "$WEB_PID")"

# --- liveness check --------------------------------------------------------
# Give each backgrounded process a few seconds to either crash early
# (port collision, missing dependency, syntax error...) or stabilize.
# If any of the three is no longer alive when we look, tell the user
# which log to inspect and exit non-zero so callers / CI notice.
log "Waiting briefly to verify the three processes are still alive..."
sleep 3

liveness_failed=0
check_alive() {
    local label="$1"
    local pidfile="$2"
    local logfile="$3"
    if [ ! -s "$pidfile" ]; then
        echo "ERROR: ${label}: no PID recorded at ${pidfile}." >&2
        echo "       Inspect ${logfile} for the underlying error." >&2
        liveness_failed=1
        return
    fi
    local pid
    pid="$(cat "$pidfile")"
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "ERROR: ${label}: process ${pid} is no longer running." >&2
        echo "       Inspect ${logfile} for the underlying error." >&2
        liveness_failed=1
    fi
}

check_alive "api"    "$API_PID"    "$API_LOG"
check_alive "worker" "$WORKER_PID" "$WORKER_LOG"
check_alive "web"    "$WEB_PID"    "$WEB_LOG"

if [ "$liveness_failed" -ne 0 ]; then
    echo "" >&2
    echo "[dev-start-all] One or more processes failed to start." >&2
    echo "Run 'scripts/dev-stop-all.sh' to clean up survivors." >&2
    exit 1
fi
log "All three processes are alive."

# --- final summary ---------------------------------------------------------
cat <<EOF

[dev-start-all] All processes started in the background.

  Home:        http://localhost:${WEB_PORT}
  API live:    http://localhost:${API_PORT}/health/live
  API ready:   http://localhost:${API_PORT}/health/ready
  Report demo: http://localhost:${WEB_PORT}/tasks/00000000-0000-0000-0000-000000000000/report

Logs:
  API:    ${API_LOG}
  Worker: ${WORKER_LOG}
  Web:    ${WEB_LOG}

PID files:
  ${API_PID}
  ${WORKER_PID}
  ${WEB_PID}

To stop everything cleanly:
  scripts/dev-stop-all.sh

Note: this script does NOT remove Docker volumes. Use 'make clean' only
if you intend to destroy local data.
EOF
