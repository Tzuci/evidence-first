# Test — Evidence-First MVP-0 (Fase 8.1a)

I test in questa cartella richiedono Postgres in esecuzione locale.

## Setup

```bash
cp .env.example .env
make up
```

Attendi che `db` sia healthy:

```bash
docker compose -f docker-compose.dev.yml ps
```

## Esecuzione

```bash
pip install 'psycopg[binary]>=3.1' 'pytest>=8.0'
make test
```

## Cosa coprono

- `test_migrate.py`: applicazione, idempotenza, checksum mismatch del migration runner.
- `test_db_basic.py`: estensioni, vincoli UNIQUE, append-only su `audit_records`, immutabilità campi su `event_processing_records`.

## Cosa NON coprono (verrà aggiunto nelle fasi successive)

- API HTTP (Fase 8.1b).
- Worker e idempotenza event processing end-to-end (Fase 8.1b / Sprint 1).
- Pipeline AI (Sprint 1+).
- Renderer (Sprint 3).
- Source loss e lifecycle events (Sprint 4).