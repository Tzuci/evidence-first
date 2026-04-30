# closed_corpus_basic

Fixture di sviluppo per la Fase 8.2 (Sprint 1). Contiene due documenti `.txt`
innocui in italiano e inglese, usati dai test e2e di ingestione documentale.

I file:
- `doc_it.txt` — riassunto numerico fittizio in italiano.
- `doc_en.txt` — equivalente in inglese.

Vincoli:
- Nessun contenuto offensivo.
- Nessun tentativo di prompt injection.
- Numeri e percentuali sono inventati e usati solo come "ancore" per i test.

I file vengono caricati via `POST /api/v1/projects/{id}/documents` durante i
test e2e. La pipeline di estrazione claim NON è ancora implementata in 8.2:
arriverà nella Fase 8.3 con `0004_claim_ledger.sql`.