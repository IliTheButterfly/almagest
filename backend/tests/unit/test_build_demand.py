"""The derived per-build arithmetic of ADR 0004, exercised with no database.

    demand    = qty_per_assembly_milli * assembly_count
    accounted = reserved + staged + consumed
    needed    = max(0, demand - accounted)

ADR 0007 names *per build* and *in use* as the two numbers the UI has to make
legible, and says they need no schema change because all three are computed on
every read. That claim is exactly what a unit test can prove and an integration
test cannot: the same objects, one integer changed, three different answers, and
**nothing written** — no `stock_allocations` row is touched, so there is no
backfill pass to forget and no event handler to miss.

Both derivations are covered, because there are two readers and they must not
drift: `reservations._net_one_line` behind `GET /shortages`, and
`roster.RosterLine` behind `GET /roster`.
"""

from __future__ import annotations

from app.models.enums import ShortageKind
from app.models.projects import BomLine, ProjectBuild
from app.services.reservations import _EMPTY_HOLDING, _LineHolding, _net_one_line
from app.services.roster import RosterLine


def _line(qty_per_assembly_milli: int = 3_000, *, is_dnp: bool = False) -> BomLine:
    """An unpersisted BOM line. `id` is set by hand because the holdings map is
    keyed by it and nothing here has a session to assign one."""
    line = BomLine(project_id=1, line_no=1, qty_per_assembly_milli=qty_per_assembly_milli)
    line.id = 1
    line.part_id = 7
    line.is_dnp = is_dnp
    return line


def _build(assembly_count: int) -> ProjectBuild:
    build = ProjectBuild(project_id=1, build_no=1, assembly_count=assembly_count)
    build.id = 1
    return build


def _holding(
    *, reserved: int = 0, staged: int = 0, consumed: int = 0, undeliverable: int = 0
) -> dict[int, _LineHolding]:
    return {
        1: _LineHolding(
            held_milli=reserved + staged + consumed,
            undeliverable_milli=undeliverable,
            reserved_milli=reserved,
            staged_milli=staged,
            consumed_milli=consumed,
        )
    }


def test_demand_is_qty_per_assembly_times_assembly_count() -> None:
    line = _line(3_000)
    shortage = _net_one_line(line, _build(4), {7: 100_000}, (), {})

    assert shortage.required_milli == 12_000
    assert shortage.allocated_milli == 0
    assert shortage.needed_milli == 12_000


def test_accounted_is_reserved_plus_staged_plus_consumed() -> None:
    holdings = _holding(reserved=1_000, staged=2_000, consumed=3_000)
    shortage = _net_one_line(_line(3_000), _build(4), {7: 100_000}, (), holdings)

    assert (shortage.reserved_milli, shortage.staged_milli, shortage.consumed_milli) == (
        1_000,
        2_000,
        3_000,
    )
    assert shortage.allocated_milli == 6_000
    assert shortage.needed_milli == 12_000 - 6_000


def test_raising_assembly_count_raises_needed_and_backfills_nothing() -> None:
    """ "Request parts for three more boards" is one column, and this is the
    proof: the *same* holdings object answers both reads."""
    line = _line(3_000)
    holdings = _holding(reserved=3_000)

    one = _net_one_line(line, _build(1), {7: 100_000}, (), holdings)
    three = _net_one_line(line, _build(3), {7: 100_000}, (), holdings)

    assert (one.required_milli, one.needed_milli) == (3_000, 0)
    assert (three.required_milli, three.needed_milli) == (9_000, 6_000)
    # The hold did not move, and neither did what the line is credited with.
    assert one.allocated_milli == three.allocated_milli == 3_000
    assert holdings[1].reserved_milli == 3_000


def test_lowering_assembly_count_cannot_claw_back_a_hold() -> None:
    """It only shrinks what is *needed* — it can never strand real stock, which
    is why `BuildUpdate.assembly_count` needs no companion write."""
    line = _line(3_000)
    holdings = _holding(consumed=9_000)

    lowered = _net_one_line(line, _build(1), {7: 0}, (), holdings)

    assert lowered.required_milli == 3_000
    assert lowered.needed_milli == 0
    assert lowered.consumed_milli == 9_000


def test_needed_clamps_at_zero_rather_than_going_negative() -> None:
    shortage = _net_one_line(_line(1_000), _build(1), {7: 0}, (), _holding(staged=5_000))

    assert shortage.needed_milli == 0


def test_an_undeliverable_hold_is_not_credited_against_demand() -> None:
    """A hold on a bin a recount emptied is still reported, and still leaves the
    line needing the parts — otherwise the build reads as covered off stock that
    is not there."""
    holdings = _holding(reserved=6_000, undeliverable=6_000)
    shortage = _net_one_line(_line(3_000), _build(2), {7: 0}, (), holdings)

    assert shortage.allocated_milli == 6_000
    assert shortage.undeliverable_milli == 6_000
    assert shortage.needed_milli == 6_000
    assert shortage.shortfall_milli == 6_000


def test_a_dnp_line_generates_no_demand_at_any_assembly_count() -> None:
    shortage = _net_one_line(_line(3_000, is_dnp=True), _build(50), {7: 0}, (), {})

    assert shortage.kind is ShortageKind.NOT_FITTED
    assert (shortage.required_milli, shortage.needed_milli) == (0, 0)


def test_an_unmatched_line_still_reports_needed() -> None:
    """The board needs three of *something*: a real number even though
    availability is not computable."""
    line = _line(3_000)
    line.part_id = None
    shortage = _net_one_line(line, _build(3), {}, (), {})

    assert shortage.kind is ShortageKind.UNIDENTIFIED
    assert shortage.needed_milli == 9_000
    assert shortage.available_milli is None


def test_empty_holding_is_the_default_for_a_line_nothing_names() -> None:
    assert _EMPTY_HOLDING.held_milli == 0
    shortage = _net_one_line(_line(1_000), _build(1), {7: 1_000}, (), {})
    assert shortage.allocated_milli == 0


# ---------------------------------------------------------------------------
# The roster's copy of the same two numbers
# ---------------------------------------------------------------------------


def _roster_line(
    required: int, reserved: int = 0, staged: int = 0, consumed: int = 0
) -> RosterLine:
    return RosterLine(
        bom_line_id=1,
        line_no=1,
        designators="R1",
        part_id=7,
        part_name="10k",
        part_mpn="RC0402-10K",
        is_dnp=False,
        is_off_bom=False,
        required_milli=required,
        reserved_milli=reserved,
        staged_milli=staged,
        consumed_milli=consumed,
        after_the_fact_milli=0,
        entries=(),
    )


def test_roster_line_accounted_and_needed_agree_with_the_shortage_report() -> None:
    roster = _roster_line(12_000, reserved=1_000, staged=2_000, consumed=3_000)
    shortage = _net_one_line(
        _line(3_000),
        _build(4),
        {7: 100_000},
        (),
        _holding(reserved=1_000, staged=2_000, consumed=3_000),
    )

    assert roster.accounted_milli == shortage.allocated_milli == 6_000
    assert roster.needed_milli == shortage.needed_milli == 6_000


def test_roster_needed_clamps_at_zero() -> None:
    assert _roster_line(1_000, consumed=5_000).needed_milli == 0


def test_roster_off_bom_line_needs_nothing() -> None:
    """Nobody planned it, so `required` is zero and `needed` cannot be negative."""
    line = _roster_line(0, consumed=2_000)
    assert (line.accounted_milli, line.needed_milli) == (2_000, 0)
