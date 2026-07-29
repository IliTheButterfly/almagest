SHELL := /bin/bash
UV    ?= uv
BE    := backend
FE    := frontend
AG    := deviceagent
IC    := idcodec

.DEFAULT_GOAL := help
.PHONY: help bootstrap sync test test-live lint fmt typecheck check migrate revision \
        check-migrations run openapi clean fe-install fe-dev fe-check fe-api \
        agent-sync agent-lint agent-typecheck agent-test agent-test-live agent-check agent-run \
        idcodec-sync idcodec-lint idcodec-typecheck idcodec-test idcodec-check

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

bootstrap: ## Clone submodules, create the venv, install deps, seed .env
	git submodule update --init --recursive
	git config submodule.recurse true
	@test -f .env || { cp .env.example .env; echo "created .env from .env.example"; }
	$(MAKE) idcodec-sync
	$(MAKE) sync
	$(MAKE) agent-sync
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

fmt: ## Autoformat and autofix (idcodec + backend + deviceagent)
	cd $(IC) && $(UV) run ruff check --fix .
	cd $(IC) && $(UV) run ruff format .
	cd $(BE) && $(UV) run ruff check --fix .
	cd $(BE) && $(UV) run ruff format .
	cd $(AG) && $(UV) run ruff check --fix .
	cd $(AG) && $(UV) run ruff format .

typecheck: ## mypy
	cd $(BE) && $(UV) run mypy app

# `idcodec-check` and `agent-check` are folded in rather than left siblings like
# `fe-check`: all three are Python on the same toolchain (uv, ruff, mypy, pytest),
# and both the API and the agent import `idcodec`, so a change to the short-ID or
# tag payload rules must fail here rather than in a target somebody remembers to
# run. The frontend stays separate because it is a different runtime entirely.
#
# `idcodec-check` goes **first**: it is the fastest of the three by an order of
# magnitude and both others depend on it, so a broken codec should be named as
# such rather than as fifty failing backend tests.
check: idcodec-check lint typecheck test agent-check ## Everything CI runs

migrate: ## Apply migrations up to head
	cd $(BE) && $(UV) run alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add widgets"
	@test -n "$(m)" || { echo 'usage: make revision m="description"'; exit 1; }
	cd $(BE) && $(UV) run alembic revision --autogenerate -m "$(m)"

check-migrations: ## Fail if the models have drifted from the migrations
	cd $(BE) && $(UV) run alembic upgrade head && $(UV) run alembic check

# ---------------------------------------------------------------------------
# idcodec — short IDs and tag payloads, standard library only. A path dependency
# of both the backend and the deviceagent, with its own venv so its "no
# dependencies" claim is checked in an environment that has none.
# ---------------------------------------------------------------------------

idcodec-sync: ## Install/refresh idcodec dependencies
	cd $(IC) && $(UV) sync --all-extras --dev

idcodec-lint: ## ruff check + format check for idcodec
	cd $(IC) && $(UV) run ruff check .
	cd $(IC) && $(UV) run ruff format --check .

idcodec-typecheck: ## mypy for idcodec
	cd $(IC) && $(UV) run mypy idcodec

idcodec-test: ## idcodec tests
	cd $(IC) && $(UV) run pytest -q

idcodec-check: idcodec-lint idcodec-typecheck idcodec-test ## Everything CI runs for idcodec

# ---------------------------------------------------------------------------
# deviceagent — runs on the station Pi, not in the cluster
# ---------------------------------------------------------------------------

agent-sync: ## Install/refresh deviceagent dependencies
	cd $(AG) && $(UV) sync --all-extras --dev

agent-lint: ## ruff check + format check for the deviceagent
	cd $(AG) && $(UV) run ruff check .
	cd $(AG) && $(UV) run ruff format --check .

agent-typecheck: ## mypy for the deviceagent
	cd $(AG) && $(UV) run mypy agent

agent-test: ## deviceagent tests (hardware tests excluded)
	cd $(AG) && $(UV) run pytest -q

agent-test-live: ## Only the tests that need a real PN532 wired up
	cd $(AG) && $(UV) run pytest -q -m live

agent-check: agent-lint agent-typecheck agent-test ## Everything CI runs for the deviceagent

agent-run: ## Run the device agent against the fake reader (no hardware needed)
	cd $(AG) && $(UV) run almagest-deviceagent --fake

run: ## Run the API with autoreload
	cd $(BE) && $(UV) run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

openapi: ## Regenerate openapi.json (source for the generated API clients)
	cd $(BE) && $(UV) run python -m app.scripts.export_openapi ../openapi.json

clean: ## Remove caches and build artefacts
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(BE)/.pytest_cache $(BE)/.ruff_cache $(BE)/.mypy_cache
	rm -rf $(AG)/.pytest_cache $(AG)/.ruff_cache $(AG)/.mypy_cache
	rm -rf $(IC)/.pytest_cache $(IC)/.ruff_cache $(IC)/.mypy_cache

certs: ## Generate a local private CA + dev certificate (ADR 0001; certs/ is gitignored)
	@./scripts/make-certs.sh
