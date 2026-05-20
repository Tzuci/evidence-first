#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# scripts/dev-stop-all.sh
#
# Stop the entire local Evidence-First MVP-0 stack started by
# `scripts/dev-start-all.sh`:
#   1. Stop the API, Worker and Web processes by PID (from .dev/pids/),
#      with a fallback by port / pattern for processes started by hand.
#   2. Bring down the Docker services via `make down`.
#
# Strictly non-destructive:
#   - never removes Docker volumes;
#   - never calls `make clean`;
#   - never passes `-v` to `docker compose down`.
#
# Environment overrides:
#   API_PORT    default 8000
#   WEB_PORT    default 3000
# ---------------------------------------------------------------------------

set -euo pipefail

# --- locate the repository root --------------------------------------------
if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: 'git' is required to locate the repository root." >&2
    exit 1
fi

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3000}"

DEV_DIR="$ROOT/.dev"
PID_DIR="$DEV_DIR/pids"

API_PID="$PID_DIR/api.pid"
WORKER_PID="$PID_DIR/worker.pid"
WEB_PID="$PID_DIR/web.pid"

log() {
    printf '[dev-stop-all] %s\n' "$*"
}

# Stop a process tracked by a PID file: SIGTERM first, escalate to
# SIGKILL after a short grace period if the process is still alive.
# The PID file is removed unconditionally at the end.
stop_pidfile() {
    local label="$1"
    local pidfile="$2"

    if [ ! -f "$pidfile" ]; then
        log "${label}: no PID file at ${pidfile}; skipping."
        return 0
    fi

    local pid
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [ -z "$pid" ]; then
        log "${label}: PID file is empty; removing it."
        rm -f "$pidfile"
        return 0
    fi

    if ! kill -0 "$pid" 2>/dev/null; then
        log "${label}: process ${pid} is not running; cleaning up PID file."
        rm -f "$pidfile"
        return 0
    fi

    log "${label}: sending SIGTERM to PID ${pid}."
    kill "$pid" 2>/dev/null || true

    # Wait up to ~5 seconds for a clean shutdown.
    local waited=0
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$waited" -ge 10 ]; then
            log "${label}: PID ${pid} still alive after SIGTERM; sending SIGKILL."
            kill -9 "$pid" 2>/dev/null || true
            break
        fi
        sleep 0.5
        waited=$((waited + 1))
    done

    rm -f "$pidfile"
}

free_tcp_port() {
    # Best-effort: free a TCP port using fuser or lsof if available.
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

# --- stop tracked processes ------------------------------------------------
stop_pidfile "web"    "$WEB_PID"
stop_pidfile "api"    "$API_PID"
stop_pidfile "worker" "$WORKER_PID"

# --- fallback: kill anything else still bound to the ports / patterns ------
log "Freeing ports ${WEB_PORT}/tcp and ${API_PORT}/tcp (fallback)."
free_tcp_port "$WEB_PORT"
free_tcp_port "$API_PORT"

log "Killing stray 'next dev' / 'uvicorn app.main:app' / worker processes (fallback)."
pkill -f "next dev"                2>/dev/null || true
pkill -f "uvicorn app.main:app"    2>/dev/null || true
pkill -f "python.*-m app.main"     2>/dev/null || true

# --- stop Docker services (keep volumes) -----------------------------------
log "Stopping Docker services via 'make down' (volumes preserved)."
make down || true

# --- final status overview -------------------------------------------------
cat <<EOF

[dev-stop-all] Stop sequence complete.

Port check:
EOF

if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | grep -E ":(${API_PORT}|${WEB_PORT})\b" || \
        echo "  ports ${API_PORT} and ${WEB_PORT} are free."
elif command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"${API_PORT}" -sTCP:LISTEN 2>/dev/null || true
    lsof -iTCP:"${WEB_PORT}" -sTCP:LISTEN 2>/dev/null || true
else
    echo "  (no 'ss' or 'lsof' available to verify ports)"
fi

cat <<EOF

Docker overview:
EOF
docker ps 2>/dev/null || echo "  (docker not available)"

cat <<EOF

Volumes are preserved. To destroy local data, run 'make clean' manually.
EOF
