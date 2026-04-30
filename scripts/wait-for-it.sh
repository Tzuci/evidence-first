#!/bin/sh
# wait-for-it.sh — POSIX shell, no bashisms.
#
# Attende che <host>:<port> sia raggiungibile via TCP, poi (opzionale) esegue un comando.
#
# Compatibilità:
# - Il flusso principale è scritto per /bin/sh (POSIX), niente bashismi: nessun array,
#   nessun [[ ]], nessuna sostituzione process <( ).
# - Il probe TCP usa preferibilmente python3 (path atteso in tutti i container Python di
#   questo repository).
# - In assenza di python3 lo script tenta /dev/tcp/<host>/<port>. Questa NON è una
#   funzionalità POSIX: è una feature di bash/zsh ed è disponibile solo in alcuni
#   container; non va considerata garantita. In Alpine puro con sola BusyBox /dev/tcp
#   tipicamente non funziona.
# - In Fase 8.1b i container apps/api e apps/worker DEVONO includere python3 (sono
#   immagini python:3.12-slim), quindi il path atteso è il probe Python. /dev/tcp resta
#   come best-effort di fallback per altri ambienti.
#
# Uso:
#   wait-for-it.sh <host> <port> [-t <timeout_seconds>] [-- <command> [args...]]
#
# Esempi:
#   wait-for-it.sh db 5432
#   wait-for-it.sh db 5432 -t 60
#   wait-for-it.sh db 5432 -- echo "db ready"
#   wait-for-it.sh db 5432 -t 30 -- python scripts/migrate.py
#
# Exit codes:
#   0   ok (oppure exit code del comando se fornito)
#   1   errore di sintassi o ambiente privo di sonde TCP utilizzabili
#   124 timeout

set -eu

usage() {
  echo "Usage: $0 <host> <port> [-t <timeout_seconds>] [-- <command> [args...]]" >&2
  exit 1
}

if [ $# -lt 2 ]; then
  usage
fi

HOST="$1"
PORT="$2"
shift 2

TIMEOUT=30
CMD=""

while [ $# -gt 0 ]; do
  case "$1" in
    -t)
      shift
      [ $# -gt 0 ] || usage
      TIMEOUT="$1"
      shift
      ;;
    --)
      shift
      CMD="$*"
      break
      ;;
    *)
      usage
      ;;
  esac
done

# Validazione numerica timeout
case "$TIMEOUT" in
  ''|*[!0-9]*) echo "ERRORE: timeout non numerico: $TIMEOUT" >&2; exit 1 ;;
esac

probe_python() {
  python3 - "$HOST" "$PORT" <<'PY' >/dev/null 2>&1
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2.0)
try:
    s.connect((host, port))
    s.close()
    sys.exit(0)
except Exception:
    sys.exit(1)
PY
}

probe_devtcp() {
  # /dev/tcp è una feature di bash/zsh, non POSIX e non sempre disponibile.
  # Best-effort fallback: se non funziona, l'utente deve installare python3.
  (echo > "/dev/tcp/$HOST/$PORT") >/dev/null 2>&1
}

probe_once() {
  if command -v python3 >/dev/null 2>&1; then
    probe_python && return 0 || return 1
  fi
  probe_devtcp && return 0 || return 1
}

# Sanity check: se non abbiamo né python3 né alcun /dev/tcp utilizzabile,
# usciamo subito con un messaggio chiaro invece di girare a vuoto fino al timeout.
if ! command -v python3 >/dev/null 2>&1; then
  if ! (echo > /dev/tcp/127.0.0.1/0) >/dev/null 2>&1; then
    echo "ERRORE: né python3 né /dev/tcp disponibili per il probe TCP." >&2
    echo "Installa python3 nel container o usa un'immagine che lo includa." >&2
    exit 1
  fi
fi

elapsed=0
until probe_once; do
  if [ "$elapsed" -ge "$TIMEOUT" ]; then
    echo "TIMEOUT: $HOST:$PORT non raggiungibile entro ${TIMEOUT}s" >&2
    exit 124
  fi
  sleep 1
  elapsed=$((elapsed + 1))
done

echo "OK: $HOST:$PORT raggiungibile dopo ${elapsed}s"

if [ -n "$CMD" ]; then
  # shellcheck disable=SC2086
  exec sh -c "$CMD"
fi