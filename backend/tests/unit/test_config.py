from __future__ import annotations

from pathlib import Path

from app.config import REPO_ROOT, Settings


def test_base_url_trailing_slash_is_stripped() -> None:
    """`{base_url}/s/{short_id}` is written into physical NFC tags; a double
    slash there would be permanent."""
    assert Settings(base_url="https://example.test/").base_url == "https://example.test"


def test_database_url_is_derived_from_db_path() -> None:
    settings = Settings(db_path=Path("/data/almagest.db"))
    assert settings.database_url == "sqlite+pysqlite:////data/almagest.db"


def test_relative_paths_anchor_to_repo_root_not_cwd() -> None:
    """`cd backend && make run` must not create a second database at backend/data/."""
    settings = Settings(db_path=Path("./data/almagest.db"))
    assert settings.db_path == REPO_ROOT / "data" / "almagest.db"


def test_absolute_paths_are_left_alone() -> None:
    """The container sets ALMAGEST_DB_PATH=/data/almagest.db on a mounted volume."""
    assert Settings(db_path=Path("/data/almagest.db")).db_path == Path("/data/almagest.db")
