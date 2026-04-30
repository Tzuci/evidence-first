# apps/worker

Consumer di eventi Redis Streams. Non implementato in Fase 8.1a.

Verrà aggiunto in **Fase 8.1b** con:
- consumer stub `task.created` che transita lo stato del task in `analyzing` → `blocked`;
- idempotenza Redis SET NX (upgrade a `event_processing_records` persistente già pronto: la tabella esiste in 0001).

Nessuna chiamata a provider esterni in MVP-0.