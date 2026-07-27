"""Shared fixtures.

Integration tests run against a **real temp SQLite file with real Alembic
migrations applied**, never `Base.metadata.create_all()`. That is deliberate:
`create_all` builds the schema the models describe, so it can never catch the
case where a migration and a model have drifted apart — which is the single
most likely schema bug in a project with hand-edited migrations. It is also the
only way the ledger's append-only triggers (created in a migration, invisible to
the models) exist during a test.
"""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.db.session import get_session_factory, reset_engine_for_testing

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    # env.py reads `-x url=...` in preference to app.config, so the test database
    # is selected without mutating process-wide environment state.
    cfg.cmd_opts = Namespace(x=[f"url={database_url}"])
    return cfg


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'almagest-test.db'}"


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    command.upgrade(_alembic_config(database_url), "head")
    eng = reset_engine_for_testing(database_url)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
