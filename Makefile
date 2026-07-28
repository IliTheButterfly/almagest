SHELL := /bin/bash
UV    ?= uv
BE    := backend
FE    := frontend

.DEFAULT_GOAL := help
.PHONY: help bootstrap sync test test-live lint fmt typecheck check migrate revision \
        check-migrations run openapi clean fe-install fe-dev fe-check fe-api

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

bootstrap: ## Clone submodules, create the venv, install deps, seed .env
	git submodule update --init --recursive
	git config submodule.recurse true
	@test -f .env || { cp .env.example .env; echo "created .env from .env.example"; }
	$(MAKE) sync
	$(MAKE) fe-install

sync: ## Install/refresh backend dependencies
	cd $(BE) && $(UV) sync --all-extras --dev

fe-install: ## Install frontend dependencies
	cd $(FE) && pnpm install

fe-dev: ## Vite dev server, proxying /api to the backend
	cd $(FE) && pnpm dev

fe-check: ## Frontend lint, typecheck, tests and build
	cd $(FE) && pnpm lint && pnpm typecheck && pnpm test && pnpm build

fe-api: ## Regenerate the typed API client from openapi.json
	$(MAKE) openapi
	cd $(FE) && pnpm generate:api

test: ## Run the backend test suite (network tests excluded)
	cd $(BE) && $(UV) run pytest -q

test-live: ## Run only the tests that hit real network providers
	cd $(BE) && $(UV) run pytest -q -m live

lint: ## ruff check + format check
	cd $(BE) && $(UV) run ruff check .
	cd $(BE) && $(UV) run ruff format --check .

fmt: ## Autoformat and autofix
	cd $(BE) && $(UV) run ruff check --fix .
	cd $(BE) && $(UV) run ruff format .

typecheck: ## mypy
	cd $(BE) && $(UV) run mypy app

check: lint typecheck test ## Everything CI runs

migrate: ## Apply migrations up to head
	cd $(BE) && $(UV) run alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add widgets"
	@test -n "$(m)" || { echo 'usage: make revision m="description"'; exit 1; }
	cd $(BE) && $(UV) run alembic revision --autogenerate -m "$(m)"

check-migrations: ## Fail if the models have drifted from the migrations
	cd $(BE) && $(UV) run alembic upgrade head && $(UV) run alembic check

run: ## Run the API with autoreload
	cd $(BE) && $(UV) run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

openapi: ## Regenerate openapi.json (source for the generated API clients)
	cd $(BE) && $(UV) run python -m app.scripts.export_openapi ../openapi.json

clean: ## Remove caches and build artefacts
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(BE)/.pytest_cache $(BE)/.ruff_cache $(BE)/.mypy_cache
