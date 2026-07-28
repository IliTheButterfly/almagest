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
from app.models.layout import LayoutSuggestion
from app.models.layout_authoring import (
    ContainerTypePhysical,
    LabelPrint,
    LabelSheetJob,
    ProvisioningAction,
    ProvisioningSession,
    VerificationMismatch,
)
from app.models.parameter import ParameterChoice, ParameterTemplate, ParameterValue
from app.models.scanning import BarcodeAlias, ScanEvent, ScanSource
from app.models.stock import StockLedger, StockLot
from app.models.storage import (
    ContainerType,
    ContainerTypeSlotTemplate,
    Location,
    LocationOccupancy,
    LocationTag,
)
from app.models.system import CacheState, ClientOperation, Setting

__all__ = [
    "BarcodeAlias",
    "CacheState",
    "ClientOperation",
    "ContainerType",
    "ContainerTypePhysical",
    "ContainerTypeSlotTemplate",
    "LabelPrint",
    "LabelSheetJob",
    "LayoutSuggestion",
    "Location",
    "LocationOccupancy",
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
    "ProvisioningAction",
    "ProvisioningSession",
    "ScanEvent",
    "ScanSource",
    "Setting",
    "StockLedger",
    "StockLot",
    "Tag",
    "Unit",
    "VerificationMismatch",
]
