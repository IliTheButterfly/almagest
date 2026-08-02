SHELL := /bin/bash
UV    ?= uv
BE    := backend
FE    := frontend
AG    := deviceagent
IC    := idcodec
MCP   := mcpserver

.DEFAULT_GOAL := help
.PHONY: help bootstrap sync test test-live lint fmt typecheck check migrate revision \
        check-migrations run openapi clean fe-install fe-dev fe-check fe-api \
        agent-sync agent-lint agent-typecheck agent-test agent-test-live agent-check agent-run \
        idcodec-sync idcodec-lint idcodec-typecheck idcodec-test idcodec-check \
        mcp-sync mcp-lint mcp-typecheck mcp-test mcp-test-live mcp-check mcp-run \
        k8s-tls k8s-secrets k8s-deploy k8s-status k8s-logs k8s-shell k8s-diff \
        k8s-backup-now k8s-backup-pull k8s-maintenance-now k8s-caches

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
	$(MAKE) mcp-sync
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

fmt: ## Autoformat and autofix (idcodec + backend + deviceagent + mcpserver)
	cd $(IC) && $(UV) run ruff check --fix .
	cd $(IC) && $(UV) run ruff format .
	cd $(BE) && $(UV) run ruff check --fix .
	cd $(BE) && $(UV) run ruff format .
	cd $(AG) && $(UV) run ruff check --fix .
	cd $(AG) && $(UV) run ruff format .
	cd $(MCP) && $(UV) run ruff check --fix .
	cd $(MCP) && $(UV) run ruff format .

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
check: idcodec-check lint typecheck test agent-check mcp-check ## Everything CI runs

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
# mcpserver — the inventory as tools an agent can call. Its own venv because the
# MCP SDK has no business in the API image, and because it needs no submodules:
# it talks to the API over HTTP and its tests read the committed openapi.json.
#
# `mcp-check` is what keeps the tool surface honest as the API grows —
# `tests/test_coverage_manifest.py` fails when a route is added, renamed or
# removed without a decision in `mcpserver/almagest_mcp/coverage.py`. That is why
# it is folded into `check` rather than left a sibling: forgetting to run it is
# exactly the failure it exists to prevent.
# ---------------------------------------------------------------------------

mcp-sync: ## Install/refresh mcpserver dependencies
	cd $(MCP) && $(UV) sync --all-extras --dev

mcp-lint: ## ruff check + format check for the MCP server
	cd $(MCP) && $(UV) run ruff check .
	cd $(MCP) && $(UV) run ruff format --check .

mcp-typecheck: ## mypy for the MCP server
	cd $(MCP) && $(UV) run mypy almagest_mcp

mcp-test: ## MCP server tests (the live API test excluded)
	cd $(MCP) && $(UV) run pytest -q

mcp-test-live: ## Only the tests that need a running API
	cd $(MCP) && $(UV) run pytest -q -m live

mcp-check: mcp-lint mcp-typecheck mcp-test ## Everything CI runs for the MCP server

mcp-run: ## Run the MCP server on stdio (an MCP client normally launches this itself)
	cd $(MCP) && $(UV) run almagest-mcp

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

agent-test-live: ## Only the tests that need a real reader (PN532 or RC522) wired up
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
	rm -rf $(MCP)/.pytest_cache $(MCP)/.ruff_cache $(MCP)/.mypy_cache

certs: ## Generate a local private CA + dev certificate (ADR 0001; certs/ is gitignored)
	@./scripts/make-certs.sh

# --- Kubernetes (cluster `aether`, namespace `ili`) -------------------------
# `ili` is shared with unrelated production workloads, so every target below
# names its resources explicitly. Never add one that uses a bare selector,
# `--all`, or `--prune`.

k8s-tls: ## Push certs/ into secret/almagest-tls (run `make certs` first)
	@test -f certs/server.crt || { echo "certs/ is missing — run 'make certs'"; exit 1; }
	kubectl create secret tls almagest-tls \
	  --cert=certs/server.crt --key=certs/server.key \
	  --dry-run=client -o yaml | kubectl apply -n ili -f -
	@echo "certificate updated; roll nginx with: kubectl rollout restart deployment/almagest-web"

k8s-secrets: ## Push the provider API keys from .env into secret/almagest-secrets
	@test -f .env || { echo ".env is missing — copy .env.example and fill it in"; exit 1; }
	@# Only the keys the API actually reads in-cluster. Deliberately not the
	@# whole file: DEVICEAGENT_* belongs to the Pi, and the ALMAGEST_* paths are
	@# set by the ConfigMap.
	kubectl create secret generic almagest-secrets \
	  $$(grep -hE '^(MOUSER_API_KEY|DIGIKEY_|NEXAR_|ANTHROPIC_API_KEY)' .env \
	     | grep -v '=$$' | sed 's/^/--from-literal=/') \
	  --dry-run=client -o yaml | kubectl apply -n ili -f -

k8s-deploy: ## Deploy/update the cluster (make k8s-deploy TAG=sha-... to pin)
	@./scripts/k8s-deploy.sh $(TAG)

k8s-diff: ## Show what a deploy would change, without changing it
	kubectl diff -k deploy/overlays/aether || true

k8s-status: ## Everything Almagest owns in the shared namespace
	kubectl get all,pvc,cm,secret,ingress -n ili -l app.kubernetes.io/part-of=almagest

k8s-logs: ## Follow the API log
	kubectl logs -n ili -f deployment/almagest-api

k8s-shell: ## Shell into the running API pod
	kubectl exec -n ili -it deployment/almagest-api -- bash

k8s-backup-now: ## Run the nightly backup immediately
	kubectl create job -n ili --from=cronjob/almagest-backup \
	  almagest-backup-manual-$$(date +%s)

k8s-backup-pull: ## Copy the newest backup off the cluster into ./data/backups/
	@mkdir -p data/backups
	@pod=$$(kubectl get pod -n ili -l app.kubernetes.io/component=api \
	         -o jsonpath='{.items[0].metadata.name}'); \
	 latest=$$(kubectl exec -n ili $$pod -- sh -c 'ls -1 /data/backups/*.db | tail -1'); \
	 echo "pulling $$latest"; \
	 kubectl cp -n ili "$$pod:$$latest" "data/backups/$$(basename $$latest)"

k8s-maintenance-now: ## Run the nightly cache maintenance immediately
	kubectl create job -n ili --from=cronjob/almagest-maintenance \
	  almagest-maintenance-manual-$$(date +%s)

k8s-caches: ## What each derived cache's last check found
	kubectl exec -n ili deployment/almagest-api -- \
	  python -c "import urllib.request,json;print(json.dumps(json.load(urllib.request.urlopen('http://localhost:8000/api/system/caches')),indent=2))"
