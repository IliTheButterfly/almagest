"""Declarative base and the constraint naming convention.

The naming convention is **load-bearing on SQLite**, not cosmetic. SQLite cannot
`ALTER TABLE ... DROP CONSTRAINT`, so Alembic emulates it with `batch_alter_table`,
which rebuilds the table. A rebuild has to reproduce every constraint by name, and
an unnamed constraint cannot be dropped or altered at all. Setting the convention
before the first migration is written is the only cheap moment to do it.

See also: no `CHECK`-constraint enums and never `sa.Enum` (which silently emits
`VARCHAR + CHECK`). Use `sa.String` plus a Python `StrEnum` validated at the model
layer — docs/PLAN.md, "Conventions that matter later".
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
