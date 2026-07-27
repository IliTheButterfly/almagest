"""Runtime configuration, read from the environment.

Every key here is documented in the repo-root `.env.example`. Field names are
snake_case; the environment variable name is the explicit alias, because the
env keys are not uniformly prefixed (`LOG_LEVEL` and `ANTHROPIC_API_KEY` are
shared conventions, not ours).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: backend/app/config.py -> backend/app -> backend -> repo root.
#: Relative paths in the environment are resolved against this, never against the
#: current working directory, so `make run` (cwd=backend) and `docker compose`
#: (cwd=/app) cannot end up pointing at two different database files. In the
#: container every path is absolute anyway, so this only ever affects dev.
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Loaded from the backend/ working directory or the repo root, so both
        # `cd backend && uvicorn ...` and a root-level `make run` see the same file.
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    db_path: Path = Field(default=Path("./data/almagest.db"), alias="ALMAGEST_DB_PATH")
    datasheet_dir: Path = Field(default=Path("./data/datasheets"), alias="ALMAGEST_DATASHEET_DIR")

    #: Physically written into every NFC tag's NDEF URI record and every printed QR,
    #: as ``{base_url}/s/{short_id}``. Changing it after tags exist means rewriting
    #: every tag — see docs/NAMING.md.
    base_url: str = Field(default="http://localhost:8000", alias="ALMAGEST_BASE_URL")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("db_path", "datasheet_dir")
    @classmethod
    def _anchor_relative_to_repo_root(cls, v: Path) -> Path:
        return v if v.is_absolute() else (REPO_ROOT / v).resolve()

    @property
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.db_path}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
