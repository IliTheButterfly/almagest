"""SQLAlchemy models.

Every model module must be imported here. `alembic/env.py` imports this package
to populate `Base.metadata`, and a model that is not imported is invisible to
autogenerate — it silently produces an empty migration instead of an error.
"""

from __future__ import annotations

from app.models.catalog import (
    Manufacturer,
    PackageType,
    Packaging,
    Part,
    PartCategory,
    PartKind,
    PartSubstitute,
    PartTag,
    Tag,
    Unit,
)
from app.models.identity import ObjectId
from app.models.parameter import ParameterChoice, ParameterTemplate, ParameterValue
from app.models.stock import StockLedger, StockLot
from app.models.storage import ContainerType, ContainerTypeSlotTemplate, Location, LocationTag
from app.models.system import CacheState, ClientOperation, Setting

__all__ = [
    "CacheState",
    "ClientOperation",
    "ContainerType",
    "ContainerTypeSlotTemplate",
    "Location",
    "LocationTag",
    "Manufacturer",
    "ObjectId",
    "PackageType",
    "Packaging",
    "ParameterChoice",
    "ParameterTemplate",
    "ParameterValue",
    "Part",
    "PartCategory",
    "PartKind",
    "PartSubstitute",
    "PartTag",
    "Setting",
    "StockLedger",
    "StockLot",
    "Tag",
    "Unit",
]
