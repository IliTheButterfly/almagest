# syntax=docker/dockerfile:1
#
# Build context is the REPO ROOT, not backend/ — the two PyPI-publishable
# submodules (ecia-barcode, elec-value-parser) become path dependencies of the
# backend, so the build needs to see them as siblings.
#
#   docker build -f backend/Dockerfile -t almagest-api .

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    ALMAGEST_DB_PATH=/data/almagest.db \
    ALMAGEST_DATASHEET_DIR=/data/datasheets

# The repo layout is reproduced inside the image rather than flattened, because
# the submodule libraries are path dependencies resolved *relative to*
# backend/pyproject.toml — `../elec-value-parser` has to actually be there.
WORKDIR /app/backend

# Dependency layer first, so editing application code does not re-resolve deps.
# The libraries are copied whole here since they *are* dependencies: changing
# one should invalidate this layer.
COPY elec-value-parser/ /app/elec-value-parser/
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

COPY backend/ ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# /data is the mount point for the SQLite database and the content-addressed
# datasheet store. It must be on a local volume — never network storage.
RUN useradd --system --uid 10001 --create-home almagest \
    && mkdir -p /data \
    && chown -R almagest:almagest /data /app /opt/venv
USER almagest

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request as u,sys; \
sys.exit(0 if u.urlopen('http://127.0.0.1:8000/api/system/health', timeout=2).status == 200 else 1)"

# Migrations are applied explicitly (`alembic upgrade head`), never on startup:
# with `strategy: Recreate` and one replica there is no ordering problem to
# solve, and an automatic migration on boot makes a rollback undiagnosable.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
