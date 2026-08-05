"""Every API operation, and what this server decided to do about it.

## Why this file exists

A curated MCP server has one predictable failure mode: the API grows and the
server does not. Six months later it exposes a stale sixth of the routes, nobody
remembers which sixth, and the only way to find out is to read both. The usual
mitigation — a line in a contributing guide asking people to remember — does not
survive contact with anyone, human or model.

So the decision is *forced* instead. Every operation id in `openapi.json` must
appear below exactly once, either `Exposed` by a named tool or `Excluded` with a
reason and a note. `tests/test_coverage_manifest.py` diffs the keys here against
the committed schema and fails on any difference, in `make check` and in CI.

**Adding a route to the backend therefore breaks this package until somebody
decides.** That is the intended cost. Deciding is cheap — one line, and
`Excluded` is a perfectly good answer for most routes — but it cannot be skipped,
and the reason gets written down while the person writing it still knows it.

## If a test sent you here

You added, renamed or removed a backend route. Add or fix its line below:

* Should an agent be able to call it? → add a tool in `tools.py`, add the route
  to `routes.py`, and mark it `Exposed("your_tool_name")`.
* Not for agents? → `Excluded(Reason.SOMETHING, "one sentence on why")`. Pick the
  reason that is actually true; the note is read by the next person, so write the
  *why*, not a restatement of the route name.
* Renamed a handler? The operation id is the function name
  (`backend/app/main.py` sets `generate_unique_id_function`), so rename the key.

## The exclusion reasons are a closed set on purpose

An open-ended "not needed" grows into a dumping ground where every future route
lands by default. Each reason below names a *category of thing an agent should not
be doing*, so an operation that fits none of them is a hint that it probably
should be exposed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class Reason(StrEnum):
    """Why an operation is not exposed."""

    #: Needs a person physically at the container: sticking a label, holding a
    #: phone against a tag, walking a cabinet. An agent calling it produces a
    #: database state that no longer describes the room.
    HANDS_ON = "hands_on"

    #: Another machine's door. The station's scan resolver needs a reader; the
    #: extraction worker's claim/submit pair is a work queue with a lease. A
    #: second caller on either is a race, not a feature.
    MACHINE_DOOR = "machine_door"

    #: The "never auto-accept" rules (`CLAUDE.md`, architecture invariants). A
    #: human decides what an OCR'd part number, a BOM line match, or an
    #: enrichment candidate means. Exposing the *decision* endpoint to a model is
    #: precisely the thing those rules forbid, and exposing the *queue* invites
    #: it to offer.
    HUMAN_JUDGEMENT = "human_judgement"

    #: Defining or restructuring things — container types, layouts, taxonomy,
    #: project and build lifecycle. These are consequential, visual, and cheap to
    #: get subtly wrong; the UI shows what a change does to the tree before it
    #: happens and a tool call does not.
    AUTHORING = "authoring"

    #: A sequenced bench process (allocate → stage → pick → consume) where
    #: entering halfway leaves reservations that only the build screen can see
    #: and unwind. The reads over it are exposed; the steps are not.
    SEQUENCED_WORKFLOW = "sequenced_workflow"

    #: PDF and image bytes, in or out. Not a JSON tool result, and a datasheet's
    #: full text is not something to paste into a context window — that is what
    #: `search_datasheets` returns snippets for.
    BINARY_PAYLOAD = "binary_payload"

    #: An exposed tool already answers it, or this is a narrower/convenience
    #: variant of one. Two tools for one question makes a model pick, and it will
    #: sometimes pick wrong.
    SUBSUMED = "subsumed"


@dataclass(frozen=True, slots=True)
class Exposed:
    """Reachable as `tool`. Must also appear in `routes.ROUTES`."""

    tool: str


@dataclass(frozen=True, slots=True)
class Excluded:
    """Deliberately unreachable."""

    reason: Reason
    note: str


Disposition = Exposed | Excluded


# ---------------------------------------------------------------------------
# Shared notes. Grouped because the reasoning is genuinely per-surface rather
# than per-route, and twelve paraphrases of one sentence would be twelve places
# to update when the reasoning changes.
# ---------------------------------------------------------------------------

_BUILD_PIPELINE: Final = (
    "One step of the build pipeline. `read_shortages` and `read_pick_list` say "
    "what a build needs and where to walk; allocating, staging and consuming are "
    "the physical follow-through, and a half-entered sequence leaves reservations "
    "the build screen owns."
)
_BUILD_LIFECYCLE: Final = (
    "Build and project lifecycle. Which iteration is current, and what its "
    "assembly count is, is a decision about what the human is making next."
)
_CONTAINER_TYPES: Final = (
    "Container geometry authoring (ADR 0002). A type's slot template decides how "
    "every container cloned from it subdivides — visual, consequential, and the "
    "sort of thing you want to see rendered before you save it."
)
_BLOBS: Final = "PDF or image bytes; see Reason.BINARY_PAYLOAD."
_CAPTURES: Final = (
    "A still taken at the scanner, and the barcodes and OCR'd lines read off it "
    "in the browser that held the pixels. Writing one means asserting geometry "
    "over an image the caller never saw; reading one hands back exactly the "
    "OCR'd text that `CLAUDE.md` forbids auto-accepting as a part number. The "
    "capture exists so a *person* can tap the value they recognise."
)
_ENRICHMENT: Final = (
    "The review queue for everything `parameter_value_candidate` refused to "
    "auto-promote. It exists *because* a model's confident answer is not good "
    "enough on its own, so it is the one surface a model must not be given."
)
_EXTRACTION: Final = (
    "The extraction worker's queue door (ADR 0005): claim a lease, submit a "
    "result, requeue a failure. One consumer, and it is not this."
)
_INTAKE: Final = (
    "The intake desk pass: a scan is parked by a phone at the bench and resolved "
    "later by a human deciding what the barcode actually was."
)
_MAINTENANCE: Final = (
    "The nightly CronJob's door. `check_caches` is the read and is exposed; "
    "running the pass or rebuilding a cache is not, for two separate reasons. The "
    "pass is `concurrencyPolicy: Forbid` because a second concurrent caller "
    "contends with the one SQLite writer, and a rebuild *destroys evidence* — it "
    "overwrites the drift that proves a write path is broken, which is the one "
    "thing that must survive until a person has looked at it."
)
_SCRUB: Final = (
    "The same CronJob's other door, and heavier: it reads every stored blob in "
    "full, so an agent that called it out of curiosity would occupy the single "
    "API replica for as long as the datasheet store takes to hash. Its finding "
    "is bit rot, which is an operator's problem and not an inventory question."
)
_LABELS: Final = (
    "Label sheets exist to be printed and stuck onto containers. Generating one "
    "no person is standing by to print wastes short ids on paper that never gets "
    "applied."
)
_WALK: Final = (
    "The provisioning and verification walks (PLAN.md workflow 4): a human moves "
    "along a cabinet touching a phone to each tag. Every state transition is a "
    "physical act."
)
_LOCATION_AUTHORING: Final = (
    "Storage tree authoring — creating containers, layouts, glyphs, child views. "
    "The tree describes a real room; changing it without looking at the room is "
    "how it stops matching."
)
_PART_DEFINITION: Final = (
    "Creating or editing a part definition. `CLAUDE.md`: never auto-accept an "
    "OCR'd or model-read part number, and a wrong-but-confident part is worse "
    "than 'unknown'. A model may search for parts; a human names them."
)
_BOM_MATCHING: Final = (
    "Matching a BOM line to a part in the catalogue. `read_bom_suggestions` is "
    "the exposed half and it returns proposals on purpose — confirming one is "
    "the decision the suggestion exists to inform, not to replace."
)
_TAG_BINDING: Final = (
    "Binding an identity to a physical carrier — an NFC tag, a distributor "
    "barcode alias. Wrong here means a container that answers as another one."
)
_FIELD_AUTHORING: Final = (
    "Authoring the parametric schema itself: which fields exist, their base "
    "units, their choices, their substitution direction (ADR 0011). This is what "
    "makes a part kind searchable at all — a wrong base unit makes every part of "
    "that kind invisible to the range queries that should find it, silently. "
    "`list_filterable_fields` is the read a caller actually needs."
)
_TAXONOMY: Final = (
    "Restructuring the part taxonomy — kinds, categories, moving a subtree. "
    "Categories are inherited down the tree and a move re-parents everything "
    "under it, so the consequences are only visible looking at the tree."
)
_ROOM_PLAN: Final = (
    "The drawn room plan: shapes and where containers sit on it (ADR 0009). It is "
    "a drawing of a physical space, authored by dragging things around on it."
)


#: operation id → what this server does about it. **Must** match `openapi.json`
#: exactly; see the module docstring.
COVERAGE: Final[Mapping[str, Disposition]] = MappingProxyType(
    {
        # -- search and the catalogue ----------------------------------------
        "search_parts": Exposed("search_parts"),
        "search_parts_by_querystring": Excluded(
            Reason.SUBSUMED,
            "The GET convenience form of `search_parts`, for a shareable URL. A "
            "tool call has no URL to share.",
        ),
        "parameter_facets": Exposed("list_filterable_fields"),
        "list_part_categories": Exposed("list_part_categories"),
        "list_part_kinds": Exposed("list_part_kinds"),
        "read_part": Exposed("get_part"),
        "list_part_parameters": Exposed("get_part_parameters"),
        "create_part": Excluded(Reason.HUMAN_JUDGEMENT, _PART_DEFINITION),
        "update_part": Excluded(Reason.HUMAN_JUDGEMENT, _PART_DEFINITION),
        "set_part_parameter": Excluded(
            Reason.HUMAN_JUDGEMENT,
            "Writes a parameter value straight onto a part. Enrichment is "
            "explicitly forbidden from doing this — it writes "
            "`parameter_value_candidate` and waits to be promoted — and a model is "
            "the caller that rule was written about.",
        ),
        "clear_part_parameter": Excluded(
            Reason.HUMAN_JUDGEMENT,
            "Erases a parameter value. Search is an interval-overlap test, so a "
            "cleared field makes the part invisible to every range query that "
            "should find it, with no error anywhere.",
        ),
        # -- the parametric schema (ADR 0011) ---------------------------------
        "list_parameter_fields": Excluded(
            Reason.SUBSUMED,
            "The authoring view of the field definitions. "
            "`list_filterable_fields` returns the same vocabulary with live counts "
            "and choices, which is the form a query needs.",
        ),
        "read_parameter_field": Excluded(Reason.SUBSUMED, _FIELD_AUTHORING),
        "create_parameter_field": Excluded(Reason.AUTHORING, _FIELD_AUTHORING),
        "update_parameter_field": Excluded(Reason.AUTHORING, _FIELD_AUTHORING),
        "delete_parameter_field": Excluded(Reason.AUTHORING, _FIELD_AUTHORING),
        "add_parameter_choice": Excluded(Reason.AUTHORING, _FIELD_AUTHORING),
        "update_parameter_choice": Excluded(Reason.AUTHORING, _FIELD_AUTHORING),
        "delete_parameter_choice": Excluded(Reason.AUTHORING, _FIELD_AUTHORING),
        "list_parameter_quantities": Excluded(Reason.SUBSUMED, _FIELD_AUTHORING),
        "create_parameter_quantity": Excluded(Reason.AUTHORING, _FIELD_AUTHORING),
        "delete_parameter_quantity": Excluded(Reason.AUTHORING, _FIELD_AUTHORING),
        "list_base_units": Excluded(
            Reason.SUBSUMED,
            "The unit picker for the field editor. `list_filterable_fields` "
            "already reports each field's base unit, which is what a caller "
            "formatting a value needs.",
        ),
        # -- taxonomy ----------------------------------------------------------
        "create_part_kind": Excluded(Reason.AUTHORING, _TAXONOMY),
        "update_part_kind": Excluded(Reason.AUTHORING, _TAXONOMY),
        "create_part_category": Excluded(Reason.AUTHORING, _TAXONOMY),
        "update_part_category": Excluded(Reason.AUTHORING, _TAXONOMY),
        "move_part_category": Excluded(Reason.AUTHORING, _TAXONOMY),
        "search_datasheets": Exposed("search_datasheets"),
        "parse_requirements": Excluded(
            Reason.SUBSUMED,
            "`suggest_parts` parses the same text and then answers it. Parsing "
            "alone is the UI's chips-and-corrections affordance, which a model "
            "does not have.",
        ),
        "suggest_parts": Exposed("suggest_parts_for_requirements"),
        # -- identity, place, and balance ------------------------------------
        "resolve_short_id": Exposed("resolve_short_id"),
        "resolve_scan": Excluded(
            Reason.MACHINE_DOOR,
            "The scan resolver chain: a camera or wedge scanner produced a "
            "payload. `resolve_short_id` is the part of it a model can reach "
            "without hardware.",
        ),
        "bind_barcode_alias": Excluded(Reason.HUMAN_JUDGEMENT, _TAG_BINDING),
        "read_location": Exposed("get_location"),
        "read_location_tree": Exposed("browse_locations"),
        "read_lot": Exposed("get_lot"),
        "read_lot_history": Exposed("get_lot_history"),
        "suggest_location": Excluded(
            Reason.HANDS_ON,
            "Where to put a package that is in your hand. The answer is only "
            "actionable while holding it, and accepting one means walking there.",
        ),
        # -- movements: the exposed write surface -----------------------------
        "consume_stock": Exposed("consume_stock"),
        "return_stock": Exposed("return_stock"),
        "move_stock": Exposed("move_stock"),
        "recount_stock": Exposed("recount_stock"),
        "undo_movement": Exposed("undo_movement"),
        "adjust_stock": Excluded(
            Reason.SUBSUMED,
            "A signed delta with no stated intent. `consume_stock` and "
            "`return_stock` say the same thing in the ledger *and* record which "
            "it was, so a history stays readable.",
        ),
        "receive_stock": Excluded(
            Reason.HANDS_ON,
            "Creates a lot — a physical package arriving at a location. Intake "
            "is workflow 1: a package, a label, a bin.",
        ),
        "empty_bin": Excluded(
            Reason.HUMAN_JUDGEMENT,
            "Zeroes every lot in a container in one call. Reversible in the "
            "ledger, but a wrong one silently rewrites a whole bin's worth of "
            "balances and nobody notices until they need one of them.",
        ),
        "batch_movements": Excluded(
            Reason.HUMAN_JUDGEMENT,
            "Many movements, one call, one group_uuid. The exposed single-lot "
            "tools make an agent's writes individually visible and individually "
            "undoable, which is the property worth keeping.",
        ),
        # -- projects, BOMs, builds ------------------------------------------
        "list_projects": Exposed("list_projects"),
        "read_project": Exposed("get_project"),
        "create_project": Excluded(Reason.AUTHORING, _BUILD_LIFECYCLE),
        "update_project": Excluded(Reason.AUTHORING, _BUILD_LIFECYCLE),
        "delete_project": Excluded(
            Reason.AUTHORING,
            "Deletes a project and its BOM. The one operation here with no "
            "compensating row to undo it.",
        ),
        "list_bom_lines": Exposed("get_project_bom"),
        "update_bom_lines": Excluded(Reason.HUMAN_JUDGEMENT, _BOM_MATCHING),
        "import_bom": Excluded(Reason.HUMAN_JUDGEMENT, _BOM_MATCHING),
        "read_bom_suggestions": Exposed("get_bom_suggestions"),
        "create_build": Excluded(Reason.AUTHORING, _BUILD_LIFECYCLE),
        "read_build": Excluded(
            Reason.SUBSUMED,
            "`get_build_shortages` carries the build id, assembly count and "
            "whether it is buildable, which is what a build is asked about.",
        ),
        "update_build": Excluded(Reason.AUTHORING, _BUILD_LIFECYCLE),
        "read_shortages": Exposed("get_build_shortages"),
        "read_pick_list": Exposed("get_build_pick_list"),
        "read_roster": Excluded(
            Reason.SUBSUMED,
            "What is staged, per line. Meaningful only inside the staging "
            "sequence this server stays out of.",
        ),
        "allocate_stock": Excluded(Reason.SEQUENCED_WORKFLOW, _BUILD_PIPELINE),
        "allocate_stock_batch": Excluded(Reason.SEQUENCED_WORKFLOW, _BUILD_PIPELINE),
        "release_stock": Excluded(Reason.SEQUENCED_WORKFLOW, _BUILD_PIPELINE),
        "stage_stock": Excluded(Reason.SEQUENCED_WORKFLOW, _BUILD_PIPELINE),
        "unstage_stock": Excluded(Reason.SEQUENCED_WORKFLOW, _BUILD_PIPELINE),
        "consume_staged_stock": Excluded(Reason.SEQUENCED_WORKFLOW, _BUILD_PIPELINE),
        "record_used_stock": Excluded(Reason.SEQUENCED_WORKFLOW, _BUILD_PIPELINE),
        # -- the storage tree, authored ---------------------------------------
        "create_location": Excluded(Reason.AUTHORING, _LOCATION_AUTHORING),
        "instantiate_containers": Excluded(Reason.AUTHORING, _LOCATION_AUTHORING),
        "instantiate_containers_at_top": Excluded(Reason.AUTHORING, _LOCATION_AUTHORING),
        "read_location_layout": Excluded(Reason.AUTHORING, _LOCATION_AUTHORING),
        "reapply_layout": Excluded(Reason.AUTHORING, _LOCATION_AUTHORING),
        "set_location_child_view": Excluded(Reason.AUTHORING, _LOCATION_AUTHORING),
        "set_location_glyph": Excluded(Reason.AUTHORING, _LOCATION_AUTHORING),
        "set_location_details": Excluded(Reason.AUTHORING, _LOCATION_AUTHORING),
        "read_location_plan": Excluded(Reason.AUTHORING, _ROOM_PLAN),
        "set_location_plan_shapes": Excluded(Reason.AUTHORING, _ROOM_PLAN),
        "set_location_plan_placements": Excluded(Reason.AUTHORING, _ROOM_PLAN),
        "preview_location_removal": Excluded(
            Reason.AUTHORING,
            "What retiring a container would take with it. The preview half of a "
            "removal, and the removal is not exposed either.",
        ),
        "remove_location": Excluded(
            Reason.AUTHORING,
            "Retires a container and everything under it. The stock inside is real "
            "and still somewhere, so this is a decision about a physical room being "
            "rearranged.",
        ),
        "restore_location": Excluded(
            Reason.AUTHORING,
            "Un-retires a container. Only ever the correction of a removal that "
            "is itself not exposed here.",
        ),
        "assign_location_short_id": Excluded(
            Reason.HANDS_ON,
            "Mints the code that goes on a label. Minting one nobody prints "
            "leaves a container whose id exists only in the database.",
        ),
        # -- container types ---------------------------------------------------
        "list_container_types": Excluded(Reason.AUTHORING, _CONTAINER_TYPES),
        "read_container_type": Excluded(Reason.AUTHORING, _CONTAINER_TYPES),
        "create_container_type": Excluded(Reason.AUTHORING, _CONTAINER_TYPES),
        "update_container_type": Excluded(Reason.AUTHORING, _CONTAINER_TYPES),
        "clone_container_type": Excluded(Reason.AUTHORING, _CONTAINER_TYPES),
        "read_slot_template": Excluded(Reason.AUTHORING, _CONTAINER_TYPES),
        "write_slot_template": Excluded(Reason.AUTHORING, _CONTAINER_TYPES),
        # -- tags and the walks ------------------------------------------------
        "resolve_location_tag": Excluded(
            Reason.MACHINE_DOOR,
            "Takes a tag UID and an NDEF URL read off a physical tag. The "
            "station and the PWA have a reader; this process does not.",
        ),
        "unbind_location_tag": Excluded(Reason.HANDS_ON, _TAG_BINDING),
        "start_provisioning_session": Excluded(Reason.HANDS_ON, _WALK),
        "read_current_provisioning_session": Excluded(Reason.HANDS_ON, _WALK),
        "bind_tag": Excluded(Reason.HANDS_ON, _WALK),
        "skip_slot": Excluded(Reason.HANDS_ON, _WALK),
        "undo_action": Excluded(Reason.HANDS_ON, _WALK),
        "start_verification_session": Excluded(Reason.HANDS_ON, _WALK),
        "read_verification_session": Excluded(Reason.HANDS_ON, _WALK),
        "check_tag": Excluded(Reason.HANDS_ON, _WALK),
        "record_tag_write_result": Excluded(
            Reason.MACHINE_DOOR,
            "Reports what a device read back off a tag after writing it. The "
            "whole point of the call is that only the device physically holding "
            "the tag can know; a process with no reader answering it would be "
            "inventing the one fact the server cannot observe.",
        ),
        "handoff_qr": Excluded(
            Reason.BINARY_PAYLOAD,
            "Renders an SVG for a human to point a phone camera at. Nothing to "
            "hand a model, which already has the URL it would encode.",
        ),
        "create_label_sheet": Excluded(Reason.HANDS_ON, _LABELS),
        "read_label_sheet": Excluded(Reason.HANDS_ON, _LABELS),
        # -- documents ---------------------------------------------------------
        "upload_document": Excluded(Reason.BINARY_PAYLOAD, _BLOBS),
        "read_document": Excluded(Reason.BINARY_PAYLOAD, _BLOBS),
        "read_document_text": Excluded(
            Reason.SUBSUMED,
            "Every extracted page of one PDF. `search_datasheets` returns the "
            "matching passages, which is the answerable form of the question.",
        ),
        "read_part_datasheet": Excluded(Reason.BINARY_PAYLOAD, _BLOBS),
        "read_part_documents": Excluded(Reason.BINARY_PAYLOAD, _BLOBS),
        "attach_part_document": Excluded(Reason.BINARY_PAYLOAD, _BLOBS),
        "detach_part_document": Excluded(Reason.BINARY_PAYLOAD, _BLOBS),
        "read_location_documents": Excluded(Reason.BINARY_PAYLOAD, _BLOBS),
        "attach_location_document": Excluded(Reason.BINARY_PAYLOAD, _BLOBS),
        "detach_location_document": Excluded(Reason.BINARY_PAYLOAD, _BLOBS),
        "read_container_type_documents": Excluded(Reason.BINARY_PAYLOAD, _BLOBS),
        "attach_container_type_document": Excluded(Reason.BINARY_PAYLOAD, _BLOBS),
        "detach_container_type_document": Excluded(Reason.BINARY_PAYLOAD, _BLOBS),
        # -- enrichment review -------------------------------------------------
        "list_enrichment_queue": Excluded(Reason.HUMAN_JUDGEMENT, _ENRICHMENT),
        "accept_enrichment_candidate": Excluded(Reason.HUMAN_JUDGEMENT, _ENRICHMENT),
        "bulk_accept_enrichment_candidates": Excluded(Reason.HUMAN_JUDGEMENT, _ENRICHMENT),
        "correct_enrichment_candidate": Excluded(Reason.HUMAN_JUDGEMENT, _ENRICHMENT),
        "dismiss_enrichment_candidate": Excluded(Reason.HUMAN_JUDGEMENT, _ENRICHMENT),
        # -- extraction --------------------------------------------------------
        "claim_extraction_work": Excluded(Reason.MACHINE_DOOR, _EXTRACTION),
        "submit_extraction_result": Excluded(Reason.MACHINE_DOOR, _EXTRACTION),
        "requeue_extraction": Excluded(Reason.MACHINE_DOOR, _EXTRACTION),
        "read_extraction_status": Excluded(Reason.MACHINE_DOOR, _EXTRACTION),
        # -- captures ----------------------------------------------------------
        "create_capture": Excluded(Reason.MACHINE_DOOR, _CAPTURES),
        "append_capture_regions": Excluded(Reason.MACHINE_DOOR, _CAPTURES),
        "read_capture": Excluded(Reason.HUMAN_JUDGEMENT, _CAPTURES),
        "list_captures": Excluded(Reason.HUMAN_JUDGEMENT, _CAPTURES),
        "delete_capture": Excluded(Reason.HUMAN_JUDGEMENT, _CAPTURES),
        # -- intake ------------------------------------------------------------
        "list_pending": Excluded(Reason.HUMAN_JUDGEMENT, _INTAKE),
        "park_scan": Excluded(Reason.HUMAN_JUDGEMENT, _INTAKE),
        "resolve_entry": Excluded(Reason.HUMAN_JUDGEMENT, _INTAKE),
        "dismiss_entry": Excluded(Reason.HUMAN_JUDGEMENT, _INTAKE),
        "reopen_entry": Excluded(Reason.HUMAN_JUDGEMENT, _INTAKE),
        # -- diagnostics -------------------------------------------------------
        "health": Exposed("check_health"),
        "read_caches": Exposed("check_caches"),
        "run_maintenance": Excluded(Reason.MACHINE_DOOR, _MAINTENANCE),
        "rebuild_caches": Excluded(Reason.MACHINE_DOOR, _MAINTENANCE),
        "scrub_blobs": Excluded(Reason.MACHINE_DOOR, _SCRUB),
    }
)


#: The tools that only exist when `ALMAGEST_MCP_ALLOW_WRITES` is on. Named here
#: rather than inferred from the HTTP method so that the manifest test can check
#: the split is honest: nothing in this set may be registered by the read pass,
#: and nothing outside it may write.
WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "consume_stock",
        "return_stock",
        "move_stock",
        "recount_stock",
        "undo_movement",
    }
)


def exposed_tools() -> frozenset[str]:
    """Every tool name the manifest claims exists."""
    return frozenset(
        disposition.tool for disposition in COVERAGE.values() if isinstance(disposition, Exposed)
    )


def operations_for(tool: str) -> frozenset[str]:
    """Which API operations back `tool`. Used by the contract test."""
    return frozenset(
        operation_id
        for operation_id, disposition in COVERAGE.items()
        if isinstance(disposition, Exposed) and disposition.tool == tool
    )
