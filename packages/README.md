# packages/shared

Pacchetto Python condiviso tra `apps/api` e `apps/worker`. Non implementato in Fase 8.1a.

Verrà aggiunto in **Fase 8.1b** con:
- `canonical_json.py`: canonicalizzazione JSON deterministica (chiavi ordinate ricorsivamente, UTF-8, null normalizzati, timestamp ISO Z, no whitespace) per il calcolo dell'`event_hash` dell'audit chain;
- `errors.py`: codici di errore normalizzati (`AUTH_REQUIRED`, `RBAC_FORBIDDEN`, `APPEND_ONLY_VIOLATION`, ecc.);
- `schemas.py`: schemi Pydantic comuni (Project, Task, Document, AuditEvent).