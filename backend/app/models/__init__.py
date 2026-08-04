"""SQLAlchemy models.

Every model module must be imported here. `alembic/env.py` imports this package
to populate `Base.metadata`, and a model that is not imported is invisible to
autogenerate — it silently produces an empty migration instead of an error.
"""

from __future__ import annotations

# Imported for its side effect: registering the `before_delete` listener that
# releases a container's identity. Importing `app.models` is what every entry
# point already does — the API, Alembic, the scripts — so the invariant is armed
# wherever a `Location` can be deleted, rather than wherever somebody remembered
# to wire it. Last in the file because it imports from the modules above.
from app.models import events as events
from app.models.captures import Capture, CaptureRegion
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
from app.models.documents import Document, DocumentLink
from app.models.enrichment import ParameterValueCandidate
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
from app.models.projects import (
    BomLine,
    BomLineSubstitute,
    Project,
    ProjectBuild,
    StockAllocation,
)
from app.models.research import ResearchCandidate
from app.models.scanning import BarcodeAlias, PendingIntake, ScanEvent, ScanSource
from app.models.stock import StockLedger, StockLot
from app.models.storage import (
    ContainerType,
    ContainerTypeSlotTemplate,
    Location,
    LocationOccupancy,
    LocationPlanShape,
    LocationPlanShapePoint,
    LocationTag,
)
from app.models.system import CacheState, ClientOperation, Setting

__all__ = [
    "BarcodeAlias",
    "BomLine",
    "BomLineSubstitute",
    "CacheState",
    "Capture",
    "CaptureRegion",
    "ClientOperation",
    "ContainerType",
    "ContainerTypePhysical",
    "ContainerTypeSlotTemplate",
    "Document",
    "DocumentLink",
    "LabelPrint",
    "LabelSheetJob",
    "LayoutSuggestion",
    "Location",
    "LocationOccupancy",
    "LocationPlanShape",
    "LocationPlanShapePoint",
    "LocationTag",
    "Manufacturer",
    "ObjectId",
    "PackageType",
    "Packaging",
    "ParameterChoice",
    "ParameterTemplate",
    "ParameterValue",
    "ParameterValueCandidate",
    "Part",
    "PartCategory",
    "PartKind",
    "PartSubstitute",
    "PartTag",
    "PendingIntake",
    "Project",
    "ProjectBuild",
    "ProvisioningAction",
    "ProvisioningSession",
    "ResearchCandidate",
    "ScanEvent",
    "ScanSource",
    "Setting",
    "StockAllocation",
    "StockLedger",
    "StockLot",
    "Tag",
    "Unit",
    "VerificationMismatch",
]
