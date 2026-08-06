"""The tools themselves: one function per question worth asking.

Three conventions run through the whole file, and all three are about the fact
that the caller is a language model rather than a program.

**Quantities are in units, never milli.** The API stores and speaks
`qty_milli` — thousandths of the part's unit of measure — because integer
thousandths keep ledger sums exact (`CLAUDE.md`, architecture invariants). That is
a storage invariant, not something to make a model reason about: given
`qty_milli` a model writes `2500` when it means 2500 pieces and takes 2.5 of them.
So every tool takes and returns `quantity` in whole units and converts at the
boundary. `qty_milli` is passed through in read results as well, labelled, for
anyone reconciling against the ledger by hand.

**Vocabulary is discoverable, not guessed.** `list_filterable_fields` and
`list_part_categories` exist because a model that invents the template name
`capacitance_uF` gets an empty result set and reports "you have none" — a wrong
answer that looks exactly like a right one. Both are cheap; call them first.

**Nothing here decides anything the system says a human decides.** The
suggestion tools return ranked proposals and say so, because that is what the API
returns and `CLAUDE.md` is explicit that a plausible substitute with the wrong
voltage rating is a field failure. Docstrings are the model's only instructions,
so each one that returns proposals says the word.

Docstrings are the tool descriptions the model reads. Write them for that reader:
what question it answers, what the arguments mean in electronics terms, and what
it must not conclude from the result.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from idcodec import shortid
from idcodec.shortid import InvalidShortId
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from almagest_mcp.api import ApiClient
from almagest_mcp.config import McpSettings

#: `LedgerSource.API`. A movement this server records was made by a program
#: talking to the HTTP API — not by a person at a keyboard (`manual`), not by a
#: tag read (`scan`). The enum already had the right value; using it makes "which
#: of these did an agent do" a query rather than an investigation.
LEDGER_SOURCE: Final = "api"

#: `app.api.limits.QTY_MILLI_MAX`, restated rather than imported — this is a bound
#: on what this server will *send*, and a request the API would reject as 422
#: should be refused before it becomes a round trip. `tests/test_api_contract.py`
#: asserts the two still agree.
QTY_MILLI_MAX: Final = 10**12

_READ_ONLY: Final = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
#: `destructive_hint=False`: every write here is reversible by a compensating
#: ledger row, never by a delete — the ledger rejects UPDATE and DELETE at the
#: trigger level. `idempotent_hint=True` is earned, not claimed: each write sends
#: a `client_op_id` and the API replays the stored response for a repeat.
_WRITE: Final = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def units_to_milli(quantity: float, *, field: str = "quantity") -> int:
    """Whole units → the API's integer thousandths.

    Rejects anything finer than a thousandth rather than rounding it. A silent
    round would turn "0.0001 kg" into zero and report success, and a movement that
    claims to have happened but moved nothing is worse than a refusal.
    """
    scaled = quantity * 1000
    rounded = round(scaled)
    if abs(scaled - rounded) > 1e-6:
        raise ValueError(
            f"{field}={quantity} is finer than a thousandth of a unit, which the "
            "ledger cannot represent exactly"
        )
    if rounded < 0:
        raise ValueError(f"{field} must not be negative")
    if rounded > QTY_MILLI_MAX:
        raise ValueError(f"{field}={quantity} exceeds the maximum the API accepts")
    return int(rounded)


def milli_to_units(qty_milli: float | None) -> float | None:
    """Thousandths → units. `float` in the signature because a JSON body's numbers
    arrive as whatever the encoder produced, and refusing a `2500.0` that means
    2500 would be pedantry at the wrong boundary."""
    return None if qty_milli is None else qty_milli / 1000


def _with_units(row: dict[str, Any]) -> dict[str, Any]:
    """Add a units-denominated twin for every `*_milli` field in a row.

    Additive rather than a replacement: the milli value stays so a result can be
    reconciled against `stock_ledger` without arithmetic, and the readable one is
    what a model quotes back to a person.
    """
    shaped = dict(row)
    for key, value in row.items():
        if key.endswith("_milli") and isinstance(value, int | float | type(None)):
            shaped[key.removesuffix("_milli")] = milli_to_units(value)
    return shaped


def _search_hit(row: dict[str, Any]) -> dict[str, Any]:
    """One search result in whole units, including the containers it names."""
    shaped = _with_units(row)
    places = row.get("locations")
    if isinstance(places, list):
        shaped["locations"] = [_with_units(place) for place in places]
    return shaped


def _normalize_short_id(raw: str) -> str:
    """Fold a short id the way both other components fold it, or refuse.

    `idcodec` is the shared implementation and its check symbol catches the
    transcription errors that matter (O/0, I/1, mistyped digits). Catching them
    here turns a typo into "that is not a valid code" instead of a 404 that a
    model reads — reasonably — as "no such container exists".
    """
    try:
        return shortid.validate(raw)
    except InvalidShortId as exc:
        raise ValueError(f"{raw!r} is not a valid Almagest short id: {exc}") from exc


def _movement_result(payload: dict[str, Any], client_op_id: str) -> dict[str, Any]:
    """One shape for every write, carrying its own undo handle.

    `client_op_id` is echoed because `undo_movement` takes it: a model that just
    made a movement can reverse exactly that movement without having to find its
    ledger sequence number. `replayed` is passed through because it is the
    difference between "recorded" and "this had already been recorded" — a model
    reporting the second as the first double-counts in its own narration.
    """
    lot = payload.get("lot")
    counterpart = payload.get("counterpart_lot")
    return {
        "client_op_id": client_op_id,
        "replayed": bool(payload.get("replayed")),
        "ledger_seqs": payload.get("seqs", []),
        "group_uuid": payload.get("group_uuid"),
        "lot": _with_units(lot) if isinstance(lot, dict) else None,
        "counterpart_lot": _with_units(counterpart) if isinstance(counterpart, dict) else None,
    }


def _filters_to_list(filters: dict[str, str] | None) -> list[dict[str, str]]:
    """`{"capacitance": "20-30uF"}` → the API's list of template/value pairs.

    A mapping rather than the API's list because `UNIQUE(part_id, template_id)`
    means a part has at most one value per template, so a second predicate on one
    template is never a meaningful query — and a mapping is the shape a model
    produces correctly first time.
    """
    return [{"template": template, "value": value} for template, value in (filters or {}).items()]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_read_tools(server: MCPServer[Any], client: ApiClient) -> None:
    """The always-on surface. Nothing here can change a row."""

    @server.tool(annotations=_READ_ONLY)
    async def list_filterable_fields(
        category: Annotated[
            str | None,
            Field(
                description="Category slug to describe, e.g. 'capacitors'. Includes descendants."
            ),
        ] = None,
        part_kind: Annotated[str | None, Field(description="Narrow to one part kind.")] = None,
    ) -> dict[str, Any]:
        """List the parametric fields `search_parts` can filter on, with their units and choices.

        **Call this before your first `search_parts` filter on an unfamiliar
        category.** Template names are data, not guessable: the field for a
        capacitor's value is whatever this returns, and a filter naming a template
        that does not exist matches nothing while looking like a valid search.

        Also returns each field's `substitution_direction`, which is what decides
        whether a higher-rated part satisfies a lower requirement — the rule
        `search_parts(mode="substitute")` applies.
        """
        payload = await client.call(
            "parameter_facets",
            body={"category": category, "part_kind": part_kind},
        )
        return dict(payload)

    @server.tool(annotations=_READ_ONLY)
    async def list_part_categories() -> dict[str, Any]:
        """List the part category tree, with how many parts sit in each.

        Slugs from here are what `search_parts(category=...)` takes. A slug
        matches its descendants too, so 'passives' finds the capacitors under it.
        """
        payload = await client.call("list_part_categories")
        return {"categories": payload}

    @server.tool(annotations=_READ_ONLY)
    async def list_part_kinds() -> dict[str, Any]:
        """List the part kinds — resistor, capacitor, connector — with how many parts each has.

        Slugs from here are what `search_parts(part_kind=...)` and
        `list_filterable_fields(part_kind=...)` take. A kind decides which
        parametric fields a part can even have, so narrowing by kind first is
        usually what makes a filter work.
        """
        payload = await client.call("list_part_kinds")
        return {"kinds": payload}

    @server.tool(annotations=_READ_ONLY)
    async def get_part_parameters(
        part_id: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        """Read one part's specifications — capacitance, dielectric, voltage, package.

        Every field the part *could* have, with a value where one is recorded and
        empty where none is. An empty field means nobody has filled it in, which is
        why the part may not appear in a parametric search for a value it physically
        has.

        Numeric values come as an interval (`value_min`/`value_max`, equal for a
        scalar) because that is how tolerance is stored and how search compares
        them. `provenance` says where the value came from — a manual entry, a
        datasheet table, a distributor's free text — and is the tiebreaker when two
        sources disagree.
        """
        payload = await client.call("list_part_parameters", path_params={"part_id": part_id})
        return dict(payload)

    @server.tool(annotations=_READ_ONLY)
    async def search_parts(
        text: Annotated[
            str | None,
            Field(description="Free text over name, MPN, description and keywords."),
        ] = None,
        category: Annotated[
            str | None,
            Field(description="Category slug from `list_part_categories`; includes descendants."),
        ] = None,
        filters: Annotated[
            dict[str, str] | None,
            Field(
                description=(
                    "Parametric predicates, keyed by template name from "
                    "`list_filterable_fields`. Values accept electronics shorthand: "
                    "'4k7', '100nF', '20-30uF', '>=50V', 'X7R', '0603'."
                )
            ),
        ] = None,
        in_stock_only: Annotated[
            bool, Field(description="Only parts with a non-zero balance somewhere.")
        ] = False,
        mode: Annotated[
            str,
            Field(
                description=(
                    "'search' matches the requirement as stated. 'substitute' also "
                    "returns parts that would satisfy it — a 100 V capacitor for a "
                    "50 V requirement — using each field's substitution_direction."
                ),
                pattern="^(search|substitute)$",
            ),
        ] = "search",
        include_stubs: Annotated[
            bool,
            Field(description="Include parts known only by name, with no parameters filled in."),
        ] = True,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Find parts by description and electrical parameters. The main catalogue query.

        This is a deterministic SQL filter, not a ranking model: a part comes back
        because its stored parameters overlap what you asked for. That is also its
        limit — a part whose parameters were never filled in (`is_stub`) cannot
        match a parametric filter, so an empty result with `total: 0` means "no
        part *recorded as* matching", which is not the same as "no such part in the
        room". Say which one you mean when reporting it.

        `location_count` and `qty` on each hit tell you whether it is actually in
        stock, and `locations` names the containers it is in — fullest first, and
        capped at a few, so a hit whose `location_count` exceeds that list is in
        more places than are named. Call `get_part` when you need every lot, or a
        container the cap left out.
        """
        payload = await client.call(
            "search_parts",
            body={
                "text": text,
                "category": category,
                "filters": _filters_to_list(filters),
                "in_stock_only": in_stock_only,
                "mode": mode,
                "include_stubs": include_stubs,
                "limit": limit,
                "offset": offset,
            },
        )
        return {
            "total": payload["total"],
            # `_with_units` only reaches a row's own keys, and each named
            # container carries its own `qty_milli`. Without this the tool would
            # hand back whole units at the top level and thousandths one key
            # deeper — the exact inconsistency this translation layer exists to
            # remove, and the sort a model quotes back to a person unconverted.
            "results": [_search_hit(row) for row in payload["results"]],
        }

    @server.tool(annotations=_READ_ONLY)
    async def get_part(
        part_id: Annotated[int, Field(ge=1, description="Part id, from a search result.")],
    ) -> dict[str, Any]:
        """Read one part in full, including every stock lot and where each one is.

        `lots` is the answer to "where is it": one entry per physical package, with
        `location_label_path` spelled out as a human path. Quantity lives on the
        lot, never on the part — a reel and a cut-tape strip of the same MPN in the
        same bin are two lots — so `total_qty` is a sum over lots, and moving stock
        means naming a lot.

        One part's physical size is here too: `length_mm`/`width_mm`/`height_mm`,
        `unit_volume_mm3` and `unit_mass_mg`, which pair with a container's
        `geometry` from `get_location` to answer "does this fit". Read
        `volume_source` before quoting the volume as a fact — it says which rung
        produced it: `override`/`dimensions` came from real measurements, while
        `package_type`/`category`/`size_class` mean it was *estimated* from what
        kind of part this is. Null means unmeasured, never zero.
        """
        payload = await client.call("read_part", path_params={"part_id": part_id})
        shaped = _with_units(payload)
        shaped["lots"] = [_with_units(lot) for lot in payload.get("lots", [])]
        return shaped

    @server.tool(annotations=_READ_ONLY)
    async def resolve_short_id(
        short_id: Annotated[
            str,
            Field(
                description=(
                    "A code from a label or tag, e.g. '4K7T-92M8'. Dashes, case and "
                    "spacing are all optional."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Look up whatever a printed label or NFC tag points at — a container, a part, a lot.

        One shared id space across every object type, so this resolves without
        knowing what kind of thing the code names, and keeps working if the object
        is reclassified. The type prefix some labels show is cosmetic; it is never
        parsed.

        The code carries a check symbol, so a mistyped one is rejected here rather
        than looked up and reported as missing.
        """
        normalized = _normalize_short_id(short_id)
        payload = await client.call("resolve_short_id", path_params={"short_id": normalized})
        return dict(payload)

    @server.tool(annotations=_READ_ONLY)
    async def browse_locations(
        root_id: Annotated[
            int | None,
            Field(ge=1, description="Subtree to return. Omit for the whole tree."),
        ] = None,
        include_retired: Annotated[bool, Field(description="Include retired containers.")] = False,
    ) -> dict[str, Any]:
        """Walk the physical storage tree: rooms, cabinets, drawers, bins.

        Flat rows with `parent_id` and a spelled-out `label_path`, so you can print
        a path without recursing. `fill_ratio` and `is_overfull` are advisory —
        capacity in this system never blocks anything, it only flags — so an
        overfull bin is a real state, not an error.

        The path is always derived here and never encoded in a label or tag:
        containers move, and an encoded path becomes a lie the moment a drawer
        changes cabinet.

        Deliberately the cheap shape: **no dimensions here.** A tree is hundreds
        of rows, and millimetres on every one of them would be paid for by every
        caller to serve the few that ask. `get_location` carries the full
        `geometry` for one container, so narrow with this and then measure with
        that — do not call it across a whole tree to find one drawer that fits.
        """
        payload = await client.call(
            "read_location_tree",
            query={"root_id": root_id, "include_retired": include_retired},
        )
        return {"nodes": [_with_units(node) for node in payload["nodes"]]}

    @server.tool(annotations=_READ_ONLY)
    async def get_location(
        location_id: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        """Read one container: its path, its short id, its size, and every lot in it.

        The "what is in this drawer" question. `lots` is the contents; each entry
        names its `part_id`, so pair this with `get_part` when you need what the
        part actually is.

        `geometry` is how big the container physically is, in millimetres —
        `inner_length_mm`/`inner_width_mm`/`inner_height_mm`, the `inner_volume_mm3`
        they multiply to, `max_item_dimension_mm` for the longest thing it will
        take, and `allowed_part_kinds` when it accepts only some. Paired with a
        part's own `length_mm`/`width_mm`/`height_mm`/`unit_volume_mm3` from
        `get_part`, that is enough to answer "does this fit". Null is the common
        case and means **unmeasured, not zero**: most containers have never had a
        tape measure taken to them, so say "not recorded" rather than treating a
        missing dimension as an answer.

        `geometry` is itself null for a container with no type at all — a room, a
        bare shelf. `capacity` is the *derived* fill state and is advisory
        everywhere: it multiplies the raw envelope by `fill_factor`, because parts
        do not pack perfectly.
        """
        payload = await client.call("read_location", path_params={"location_id": location_id})
        shaped = _with_units(payload)
        shaped["lots"] = [_with_units(lot) for lot in payload.get("lots", [])]
        return shaped

    @server.tool(annotations=_READ_ONLY)
    async def get_lot(
        lot_id: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        """Read one stock lot: one physical package of one part at one location.

        `quantity` is the cached ledger balance, which is the number to trust;
        `qty_reserved` is derived from allocations to builds, so the amount free to
        take is `quantity - qty_reserved`.
        """
        payload = await client.call("read_lot", path_params={"lot_id": lot_id})
        return _with_units(payload)

    @server.tool(annotations=_READ_ONLY)
    async def get_lot_history(
        lot_id: Annotated[int, Field(ge=1)],
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> dict[str, Any]:
        """Read a lot's movement history, newest first — every take, return, move and recount.

        The ledger is append-only: nothing here was ever edited or deleted. An undo
        appears as its own compensating row pointing at what it reversed
        (`reversal_of_seq`), so a corrected mistake shows as two rows rather than
        as none. `source` says how each movement was captured — `manual` typed in,
        `scan` read off a tag, `api` written by a program like this one.
        """
        payload = await client.call(
            "read_lot_history",
            path_params={"lot_id": lot_id},
            query={"limit": limit},
        )
        return {"entries": [_with_units(entry) for entry in payload]}

    @server.tool(annotations=_READ_ONLY)
    async def search_datasheets(
        q: Annotated[str, Field(min_length=1, description="Free text over extracted PDF text.")],
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Full-text search the extracted text of stored datasheets, returning matching passages.

        Only covers PDFs that have been through extraction, so an absent hit may
        mean "not extracted yet" rather than "not in any datasheet". Snippets are
        the matched passages, not the whole document.
        """
        payload = await client.call(
            "search_datasheets", query={"q": q, "limit": limit, "offset": offset}
        )
        return dict(payload)

    @server.tool(annotations=_READ_ONLY)
    async def suggest_parts_for_requirements(
        lines: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=100,
                description=(
                    "One requirement per entry, as a description rather than a part "
                    "number: '3x 10k 1% 0603 resistor', '100nF 50V X7R 0603', "
                    "'a dual op-amp, rail-to-rail, SOIC-8'."
                ),
            ),
        ],
        limit: Annotated[int, Field(ge=1, le=50, description="Candidates per line.")] = 5,
    ) -> dict[str, Any]:
        """Turn written requirements into candidate parts you already own — a whole list at once.

        Each line is parsed into a structured requirement and then answered by the
        same deterministic filter `search_parts` uses, so `requirement` shows what
        your words were understood to mean and `residue` shows what was not
        understood. Read both: a line parsed wrongly returns confident candidates
        for the wrong requirement.

        **Everything returned is a proposal.** Candidates are ranked, `in_stock`
        and `not_stocked` are separated, and nothing is selected or committed. A
        part that looks right but has the wrong voltage rating is a field failure,
        so confirm against `get_part` before telling anyone to use one.

        Batched on purpose: a twenty-line BOM is one call, and the lines share
        parsing state.
        """
        payload = await client.call(
            "suggest_parts",
            body={"lines": [{"text": line} for line in lines], "limit": limit},
        )
        return dict(payload)

    @server.tool(annotations=_READ_ONLY)
    async def list_projects(
        status: Annotated[
            list[str] | None,
            Field(description="Filter by lifecycle: 'planning', 'active', 'archived'."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """List projects — a board or product whose BOM this system tracks."""
        payload = await client.call(
            "list_projects", query={"status": status, "limit": limit, "offset": offset}
        )
        return dict(payload)

    @server.tool(annotations=_READ_ONLY)
    async def get_project(
        project_id: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        """Read one project and its builds.

        A project is the design; a build is one run of making it, with its own
        assembly count. Shortages and pick lists belong to a build, so take the
        `build_id` from here.
        """
        payload = await client.call("read_project", path_params={"project_id": project_id})
        return dict(payload)

    @server.tool(annotations=_READ_ONLY)
    async def get_project_bom(
        project_id: Annotated[int, Field(ge=1)],
        unmatched_only: Annotated[
            bool,
            Field(description="Only lines with no part matched yet — the worklist."),
        ] = False,
        limit: Annotated[int, Field(ge=1, le=1000)] = 200,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Read a project's bill of materials, line by line.

        `part_id` is null on a line nobody has matched to a catalogue part yet, and
        `is_match_confirmed` says whether a human agreed with the match that is
        there. An unconfirmed match is a suggestion someone accepted the shape of,
        not a fact.
        """
        payload = await client.call(
            "list_bom_lines",
            path_params={"project_id": project_id},
            query={"unmatched_only": unmatched_only, "limit": limit, "offset": offset},
        )
        return {
            "total": payload["total"],
            "lines": [_with_units(line) for line in payload["lines"]],
        }

    @server.tool(annotations=_READ_ONLY)
    async def get_bom_suggestions(
        project_id: Annotated[int, Field(ge=1)],
        unmatched_only: Annotated[
            bool, Field(description="Only lines with no part matched yet.")
        ] = True,
        assembly_count: Annotated[
            int, Field(ge=1, description="How many boards the demand is for.")
        ] = 1,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        offset: Annotated[int, Field(ge=0)] = 0,
        candidates: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> dict[str, Any]:
        """Suggest parts you already own for a project's unmatched BOM lines.

        The same machinery as `suggest_parts_for_requirements`, aimed at a BOM
        already in the system and with demand scaled to `assembly_count`.

        **Proposals only.** Confirming a line's match is a human decision made in
        the UI, and this server deliberately cannot make it — a wrong-but-confident
        BOM match propagates into every build of that board.
        """
        payload = await client.call(
            "read_bom_suggestions",
            path_params={"project_id": project_id},
            query={
                "unmatched_only": unmatched_only,
                "assembly_count": assembly_count,
                "limit": limit,
                "offset": offset,
                "candidates": candidates,
            },
        )
        return dict(payload)

    @server.tool(annotations=_READ_ONLY)
    async def get_build_shortages(
        build_id: Annotated[int, Field(ge=1, description="Build id, from `get_project`.")],
    ) -> dict[str, Any]:
        """Answer "can I build this, and what am I missing?" for one build.

        `is_buildable` is the headline; `lines` breaks it down per BOM line with
        what is needed, reserved, staged and short. `is_blocking` distinguishes a
        line that stops the build from one that merely cannot be filled from stock
        (a do-not-populate line, or one with no part matched).
        `substitute_part_ids` lists parts that would satisfy the line under its
        fields' substitution rules — determined by the same SQL filter, not by
        judgement.
        """
        payload = await client.call("read_shortages", path_params={"build_id": build_id})
        shaped = dict(payload)
        shaped["lines"] = [_with_units(line) for line in payload["lines"]]
        return shaped

    @server.tool(annotations=_READ_ONLY)
    async def get_build_pick_list(
        build_id: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        """Get the walk for collecting a build's parts: which container, and how much from each.

        `stops` are in the order to walk them. `gaps` are the lines the walk cannot
        finish and are never omitted — a pick list without its gaps reads as
        complete and the shortfall is discovered at the bench, so report `gaps`
        whenever `is_complete` is false.
        """
        payload = await client.call("read_pick_list", path_params={"build_id": build_id})
        shaped = _with_units(payload)
        shaped["stops"] = [_with_units(stop) for stop in payload["stops"]]
        shaped["gaps"] = [_with_units(gap) for gap in payload["gaps"]]
        return shaped

    @server.tool(annotations=_READ_ONLY)
    async def check_health() -> dict[str, Any]:
        """Check that the Almagest API is reachable, and which schema revision it is on."""
        payload = await client.call("health")
        return dict(payload)

    @server.tool(annotations=_READ_ONLY)
    async def check_caches() -> dict[str, Any]:
        """Report whether the cached stock numbers still agree with the ledger.

        Answers "are these quantities trustworthy?". Every quantity this server
        reports comes from `stock_lots.qty_milli_cached` rather than a sum over the
        ledger, and a nightly job checks the two against each other. A non-zero
        `drift_count` means some write path updated one and not the other, so the
        numbers in this conversation may be wrong — say so rather than working
        around it. Repairing it is deliberately not available here: the drift is
        the evidence.
        """
        return {"caches": list(await client.call("read_caches"))}


def register_write_tools(server: MCPServer[Any], client: ApiClient, settings: McpSettings) -> None:
    """The movement surface. Registered only when `ALMAGEST_MCP_ALLOW_WRITES` is on.

    Not registered *and refusing* — a tool a model can see and cannot use costs it
    a turn to discover, and the refusal reads like a bug. When writes are off the
    tools simply are not in the list.

    Every write here goes through the same `/api/stock/...` route the PWA and the
    bench station use, so `app/services/ledger.py` stays the sole writer and a
    movement made from a chat window is indistinguishable in the ledger from one
    typed by hand except for its `source` (`api`) and `device_id`.
    """

    def _envelope(note: str | None) -> dict[str, Any]:
        """The fields every movement request carries.

        `client_op_id` is minted per call. It is what makes a retry safe: the API
        stores the response against the key and replays it rather than recording a
        second movement, so a transport error a model retries cannot double-take.
        It is also the handle `undo_movement` accepts, which is why each write
        returns it.
        """
        return {
            "client_op_id": uuid.uuid4().hex,
            "device_id": settings.device_id,
            "source": LEDGER_SOURCE,
            "note": note,
        }

    @server.tool(annotations=_WRITE)
    async def consume_stock(
        lot_id: Annotated[
            int, Field(ge=1, description="The lot taken from, e.g. from `get_part`.")
        ],
        quantity: Annotated[
            float,
            Field(gt=0, description="How many units were taken, in the part's unit of measure."),
        ],
        note: Annotated[str | None, Field(description="Why, for the ledger.")] = None,
    ) -> dict[str, Any]:
        """Record that stock was taken out of a lot — parts used, sold or scrapped.

        Records what already happened physically; it does not cause anything to
        move. Reversible with `undo_movement` using the `client_op_id` returned
        here, which writes a compensating row rather than deleting anything.

        Consuming more than the lot holds is the API's decision, not this tool's —
        it may accept it and flag, because refusing a scan teaches people to stop
        recording movements.
        """
        envelope = _envelope(note)
        payload = await client.call(
            "consume_stock",
            path_params={"lot_id": lot_id},
            body={"qty_milli": units_to_milli(quantity), **envelope},
        )
        return _movement_result(payload, envelope["client_op_id"])

    @server.tool(annotations=_WRITE)
    async def return_stock(
        lot_id: Annotated[int, Field(ge=1)],
        quantity: Annotated[
            float, Field(gt=0, description="How many units went back in, in the part's UoM.")
        ],
        note: Annotated[str | None, Field(description="Why, for the ledger.")] = None,
    ) -> dict[str, Any]:
        """Record stock going back into a lot — leftovers returned to the bin.

        The counterpart of `consume_stock`. Use this rather than a negative
        consume: the ledger records which it was, and a history that says
        "returned" reads correctly a year later.
        """
        envelope = _envelope(note)
        payload = await client.call(
            "return_stock",
            path_params={"lot_id": lot_id},
            body={"qty_milli": units_to_milli(quantity), **envelope},
        )
        return _movement_result(payload, envelope["client_op_id"])

    @server.tool(annotations=_WRITE)
    async def move_stock(
        lot_id: Annotated[int, Field(ge=1)],
        to_location_id: Annotated[
            int, Field(ge=1, description="Destination container, from `browse_locations`.")
        ],
        quantity: Annotated[
            float | None,
            Field(
                gt=0,
                description=(
                    "Units to move. Omit to move the whole lot; give a quantity to "
                    "split it, which creates a second lot at the destination."
                ),
            ),
        ] = None,
        note: Annotated[str | None, Field(description="Why, for the ledger.")] = None,
    ) -> dict[str, Any]:
        """Record stock physically moved to a different container.

        A partial move splits the lot, and the response's `counterpart_lot` is the
        new one at the destination. An over-capacity destination is accepted and
        flagged rather than refused — capacity here is advisory by design.
        """
        envelope = _envelope(note)
        payload = await client.call(
            "move_stock",
            path_params={"lot_id": lot_id},
            body={
                "to_location_id": to_location_id,
                "qty_milli": None if quantity is None else units_to_milli(quantity),
                **envelope,
            },
        )
        return _movement_result(payload, envelope["client_op_id"])

    @server.tool(annotations=_WRITE)
    async def recount_stock(
        lot_id: Annotated[int, Field(ge=1)],
        counted_quantity: Annotated[
            float,
            Field(
                ge=0,
                description=(
                    "What was physically counted, in units. This sets the balance; "
                    "it is not a delta."
                ),
            ),
        ],
        note: Annotated[str | None, Field(description="How it was counted.")] = None,
    ) -> dict[str, Any]:
        """Set a lot's balance to what was physically counted, recording the correction.

        Only for a count someone actually performed. Writing a guess here is worse
        than leaving the balance wrong, because a recount is the strongest claim
        this system has about a bin — every later drift check trusts it.
        """
        envelope = _envelope(note)
        payload = await client.call(
            "recount_stock",
            path_params={"lot_id": lot_id},
            body={
                "counted_qty_milli": units_to_milli(counted_quantity, field="counted_quantity"),
                **envelope,
            },
        )
        return _movement_result(payload, envelope["client_op_id"])

    @server.tool(annotations=_WRITE)
    async def undo_movement(
        client_op_id_to_undo: Annotated[
            str | None,
            Field(description="The `client_op_id` returned by the movement to reverse."),
        ] = None,
        seq: Annotated[
            int | None,
            Field(ge=1, description="Alternatively, the ledger sequence number to reverse."),
        ] = None,
        note: Annotated[str | None, Field(description="Why, for the ledger.")] = None,
    ) -> dict[str, Any]:
        """Reverse a movement, by the id it returned or by its ledger sequence number.

        Exactly one handle. This appends a compensating row pointing at what it
        reversed — nothing is edited or deleted, and the history keeps both rows,
        which is deliberate: a corrected mistake that leaves no trace is
        indistinguishable from one that never happened.

        **How far it reaches.** A `client_op_id` reverses the movement it names.
        Where that movement was one indivisible statement written as two rows — a
        partial move, which takes stock out of one bin and puts it into another —
        both rows are reversed, because undoing half of it would leave the same
        stock recorded in two places at once. It will **not** reach sideways into
        other movements that happened to be submitted alongside it.
        """
        if (client_op_id_to_undo is None) == (seq is None):
            raise ValueError("give exactly one of client_op_id_to_undo or seq")
        payload = await client.call(
            "undo_movement",
            body={
                "client_op_id_to_undo": client_op_id_to_undo,
                "seq": seq,
                **_envelope(note),
            },
        )
        return dict(payload)
