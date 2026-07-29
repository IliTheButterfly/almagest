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

from app.config import get_settings
from app.db.session import get_session_factory, reset_engine_for_testing

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    # env.py reads `-x url=...` in preference to app.config, so the test database
    # is selected without mutating process-wide environment state.
    cfg.cmd_opts = Namespace(x=[f"url={database_url}"])
    return cfg


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip `live`-marked tests unless `-m live` asked for them.

    `pyproject.toml` registers the marker as "skipped unless `-m live` is
    passed", and `make test` runs a bare `pytest` — so until this hook existed
    that sentence was a comment rather than a behaviour, and the first
    network-touching test added would have run in CI and failed there.

    **Skipped, not deselected.** A deselected test is invisible: it does not
    appear in the summary, and a live contract test that silently stops being
    collected is one nobody notices the loss of. A skip line in the output is
    the reminder that an unexercised contract exists, and it is also what lets a
    test assert the arrangement is still in place.
    """
    if "live" in str(config.getoption("markexpr", default="") or ""):
        return
    skip_live = pytest.mark.skip(reason="network test: run with `-m live` (make test-live)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'almagest-test.db'}"


@pytest.fixture
def alembic_config(database_url: str) -> Config:
    """The same migration runner `engine` uses, for the rare test that has to
    step between revisions instead of starting at head — a data backfill can
    only be exercised by putting data in *before* the migration runs."""
    return _alembic_config(database_url)


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


@pytest.fixture(autouse=True)
def _isolate_label_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`app.services.labels.render_sheet` writes real PDF/PNG files to
    `Settings.label_output_dir`, which defaults to the repo's own
    `data/labels` — unlike `datasheet_dir`, which nothing reads yet, this is
    a settings-derived path real test runs actually write through today.
    Autouse so a test gains this isolation by writing to the label-printing
    routes at all, with no per-file fixture to remember.

    `get_settings()` is a process-wide `lru_cache` singleton, so this mutates
    its one instance in place rather than swapping in a new one — `monkeypatch`
    still restores the original value at teardown, exactly as if it were a
    plain module attribute.
    """
    monkeypatch.setattr(get_settings(), "label_output_dir", tmp_path / "labels")
