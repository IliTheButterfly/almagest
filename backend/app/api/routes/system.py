"""System routes: health, and (later) backup/restore."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import __version__
from app.db.session import get_db

router = APIRouter(prefix="/api/system", tags=["system"])


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str
    #: Current Alembic revision, or None when migrations have never been applied.
    #: A running API on `None` means `alembic upgrade head` has not been run.
    schema_revision: str | None


@router.get("/health", response_model=HealthResponse)
def health(db: Session = Depends(get_db)) -> HealthResponse:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return HealthResponse(
            status="degraded",
            version=__version__,
            database="unreachable",
            schema_revision=None,
        )

    try:
        revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    except SQLAlchemyError:
        # Table absent: a database that exists but has never been migrated.
        revision = None

    return HealthResponse(
        status="ok",
        version=__version__,
        database="ok",
        schema_revision=revision,
    )
