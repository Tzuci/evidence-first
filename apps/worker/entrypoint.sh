#!/bin/sh
set -eu

echo "[entrypoint] waiting for db:5432 ..."
wait-for-it.sh db 5432 -t 60

echo "[entrypoint] waiting for redis:6379 ..."
wait-for-it.sh redis 6379 -t 30

echo "[entrypoint] launching worker: $*"
exec "$@"