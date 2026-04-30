.DEFAULT_GOAL := help

# Carica .env se presente, ma non blocca se assente (fallback ai default)
ifneq (,$(wildcard ./.env))
	include .env
	export
endif

PYTHON ?= python3
COMPOSE ?= docker compose -f docker-compose.dev.yml
ARGS ?=

# Path al pacchetto condiviso. Permette di eseguire i test locali senza dover
# `pip install -e packages/shared` nell'ambiente Python host.
SHARED_PKG_PATH := $(CURDIR)/packages/shared

.PHONY: help up down logs migrate seed test test-db test-shared test-api test-worker test-web lint fmt clean psql redis-cli lock-web

help: ## Mostra questa lista di comandi
	@echo "Evidence-First MVP-0 — comandi disponibili:"
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Variabili:"
	@echo "  ARGS=\"...\"   passa argomenti agli script Python (es. ARGS=\"--status\" o ARGS=\"--target=0001\")"

up: ## Avvia db, redis, api, worker, web in background
	@mkdir -p storage
	$(COMPOSE) up -d --build
	@echo "Servizi avviati. Verifica: make logs"

down: ## Ferma i servizi (mantiene i volumi)
	$(COMPOSE) down

logs: ## Mostra i log dei servizi (Ctrl+C per uscire)
	$(COMPOSE) logs -f

migrate: ## Applica le migration pendenti (ARGS="--status" | "--dry-run" | "--target=0001")
	$(PYTHON) scripts/migrate.py $(ARGS)

seed: ## Esegue il seed di sviluppo
	$(PYTHON) scripts/seed_dev.py $(ARGS)

test: test-db test-shared test-api test-worker test-web ## Esegue tutti i test (DB up + tutti i moduli)

test-db: ## Esegue i test root che richiedono il DB
	PYTHONPATH=$(SHARED_PKG_PATH) $(PYTHON) -m pytest -q tests/

test-shared: ## Esegue i test di packages/shared
	PYTHONPATH=$(SHARED_PKG_PATH) $(PYTHON) -m pytest -q packages/shared/tests/

test-api: ## Esegue i test di apps/api (richiede stack avviato)
	cd apps/api && PYTHONPATH=$(SHARED_PKG_PATH):. $(PYTHON) -m pytest -q tests/

test-worker: ## Esegue i test di apps/worker (richiede stack avviato)
	cd apps/worker && PYTHONPATH=$(SHARED_PKG_PATH):. $(PYTHON) -m pytest -q tests/

test-web: ## Esegue i test di apps/web (Vitest)
	cd apps/web && npm test --silent

lint: ## Lint Python (placeholder; non vincolante in 8.1d-patch1)
	@echo "lint: placeholder. Aggiungere ruff/black in Sprint 1."

fmt: ## Formattazione (placeholder)
	@echo "fmt: placeholder. Aggiungere ruff/black in Sprint 1."

lock-web: ## Genera apps/web/package-lock.json (richiede npm locale; commit del file dopo)
	cd apps/web && npm install --package-lock-only --no-audit --no-fund
	@echo "package-lock.json generato. Esegui git add apps/web/package-lock.json e committa."

psql: ## Apre psql sul container db
	$(COMPOSE) exec db psql -U $(POSTGRES_USER) -d $(POSTGRES_DB)

redis-cli: ## Apre redis-cli sul container redis
	$(COMPOSE) exec redis redis-cli

clean: ## Ferma i servizi e rimuove i volumi (DISTRUTTIVO)
	$(COMPOSE) down -v
	@echo "Volumi rimossi. Tutti i dati locali sono stati cancellati."