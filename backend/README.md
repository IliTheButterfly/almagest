# Almagest backend

FastAPI + SQLAlchemy 2.0 + Alembic over SQLite (WAL, FTS5). The API contract
defined here is generated into the frontend and `deviceagent` clients, so it is
the coupling point that keeps those three in one repo.

See [`../docs/PLAN.md`](../docs/PLAN.md) for the architecture and
[`../CLAUDE.md`](../CLAUDE.md) for the invariants that must not be violated.

```bash
uv sync --all-extras --dev      # or `make bootstrap` from the repo root
uv run alembic upgrade head
uv run uvicorn app.main:app --reload

uv run pytest -q
uv run ruff check . && uv run ruff format --check . && uv run mypy app
```
