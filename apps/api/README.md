# apps/api

Servizio HTTP gateway (FastAPI). Non implementato in Fase 8.1a.

Verrà aggiunto in **Fase 8.1b** con:
- factory FastAPI, configurazione, connessione DB e Redis;
- endpoint `health/live`, `health/ready`, `health/db`, `health/queue`, `health/storage`;
- endpoint minimi `projects`, `tasks` (stub closed_corpus), `documents` (upload `.txt`/`.md`);
- error model normalizzato.

In MVP-0 nessuna chiamata a provider esterni: `MAX_COST_PER_TASK=0`, `PROVIDERS_ENABLED=mock`.