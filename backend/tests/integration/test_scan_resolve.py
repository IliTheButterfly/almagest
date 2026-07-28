"""`POST /api/scan/resolve` and `POST /api/scan/alias`.

**The ordering tests are the specification.** Everything else in this file could
be re-derived from the code; the order of the handlers cannot, so each shadowing
test asserts one adjacent pair of steps and fails loudly if they swap. The design
fixes that order — internal short ID, alias, ECIA, LCSC, bare MPN, EAN, unknown —
and getting it wrong does not produce an error, it produces a confident wrong
answer that becomes a stock movement.

The second theme is that **decoded and resolved are different facts**. A perfectly
readable DigiKey label for a part nobody has ever stocked is `decoded_kind: ecia`
with `status: unknown`, and telling those apart is what makes "where does intake
actually hurt" answerable from `scan_events`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.catalog import Part
from app.models.enums import (
    AliasKind,
    EntityType,
    ScanAction,
    ScanDecodedKind,
    ScanSourceKind,
)
from app.models.scanning import BarcodeAlias, ScanEvent, ScanSource
from app.models.stock import StockLot
from app.models.storage import Location
from app.services import shortid
from app.services.scanning import codes
from app.services.scanning.resolver import HANDLER_ORDER, HANDLERS
from app.services.tree import location_tree
from tests.factories import make_location, make_lot, make_manufacturer, make_part

#: A valid EAN-13 with a published check digit, and also — in one test — a part
#: number, which is how the MPN/EAN step order gets exercised.
EAN13 = "4006381333931"


def ecia(*fields: str) -> str:
    """A well-formed MH10.8.2 payload: `[)>` RS `06` GS fields... RS EOT."""
    return "[)>\x1e06\x1d" + "\x1d".join(fields) + "\x1e\x04"


def resolve(client: TestClient, code: str, **extra: object) -> dict[str, Any]:
    response = client.post("/api/scan/resolve", json={"code": code, **extra})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def bind(
    client: TestClient,
    code: str,
    entity_type: EntityType,
    entity_pk: int,
    **extra: object,
) -> dict[str, Any]:
    response = client.post(
        "/api/scan/alias",
        json={
            "code": code,
            "symbology": "datamatrix",
            "entity_type": entity_type.value,
            "entity_pk": entity_pk,
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def fresh(db: Session) -> Session:
    """Drop this session's snapshot so it sees what the API committed."""
    db.rollback()
    db.expire_all()
    return db


# ---------------------------------------------------------------------------
# The order
# ---------------------------------------------------------------------------


def test_the_dispatch_table_matches_the_documented_order() -> None:
    """Cheap, and the only test that catches a reorder with no behavioural
    overlap to trip on. `HANDLER_ORDER` is written out as a literal so moving a
    handler cannot pass review as a refactor — it has to be an edit to the
    declared order, which is an edit to the specification."""
    assert tuple(kind for kind, _ in HANDLERS) == HANDLER_ORDER
    assert HANDLER_ORDER == (
        ScanDecodedKind.SHORT_ID,
        ScanDecodedKind.ALIAS,
        ScanDecodedKind.ECIA,
        ScanDecodedKind.LCSC,
        ScanDecodedKind.MPN,
        ScanDecodedKind.EAN,
    )


def test_a_bound_alias_shadows_a_valid_ecia_parse(client: TestClient, db: Session) -> None:
    """Step 2 before step 3, the pair that matters most.

    The payload parses cleanly and its part number names a part we hold, so
    without the binding it resolves to that part. With the binding it must resolve
    to what the *user* said, because the user has the reel in their hand and the
    parser is only reading a label whose DI conventions we inferred.
    """
    catalogued = make_part(db, name="Dual op-amp", mpn="LM358N")
    relabelled = make_part(db, name="What is actually in the bag", mpn="SALVAGE-1")
    db.commit()

    payload = ecia("PLM358N", "Q2500", "1TLOT4711")

    before = resolve(client, payload)
    assert before["decoded_kind"] == ScanDecodedKind.ECIA
    assert before["target"]["entity_pk"] == catalogued.id

    bind(client, payload, EntityType.PART, relabelled.id)

    after = resolve(client, payload)
    assert after["decoded_kind"] == ScanDecodedKind.ALIAS
    assert after["status"] == "resolved"
    assert after["target"]["entity_pk"] == relabelled.id


def test_a_short_id_shadows_an_alias_bound_to_the_same_text(
    client: TestClient, db: Session
) -> None:
    """Step 1 before step 2. Contrived — nobody would bind an alias over their own
    short ID — but it is the only way to observe the boundary, and the printed
    label on a physical drawer has to win over a stray binding."""
    drawer = make_location(db, name="Drawer A1")
    decoy = make_part(db, name="Decoy", mpn="DECOY-1")
    code = shortid.allocate(db, EntityType.LOCATION, drawer.id)
    db.commit()

    bind(client, code, EntityType.PART, decoy.id)

    body = resolve(client, code)
    assert body["decoded_kind"] == ScanDecodedKind.SHORT_ID
    assert body["target"]["entity_type"] == EntityType.LOCATION
    assert body["target"]["entity_pk"] == drawer.id


def test_a_known_part_number_shadows_a_valid_ean(client: TestClient, db: Session) -> None:
    """Step 5 before step 6. The payload is simultaneously a valid EAN-13 and a
    part number we hold, which is the only input that can tell the two apart."""
    part = make_part(db, name="Oddly numbered part", mpn=EAN13)
    db.commit()
    assert codes.is_gtin(EAN13)

    body = resolve(client, EAN13)
    assert body["decoded_kind"] == ScanDecodedKind.MPN
    assert body["status"] == "resolved"
    assert body["target"]["entity_pk"] == part.id


def test_an_ean_matching_nothing_is_classified_not_swallowed(client: TestClient) -> None:
    """The other half of that boundary: with no part behind it, step 5 declines
    rather than claiming, so step 6 is reachable at all. A step 5 that claimed
    every MPN-shaped string would make step 6 dead code."""
    body = resolve(client, EAN13)
    assert body["decoded_kind"] == ScanDecodedKind.EAN
    assert body["status"] == "unknown"
    assert body["parsed"]["ean"] == EAN13
    assert body["suggest_bind"] is True


def test_the_lcsc_step_never_claims_a_payload(client: TestClient) -> None:
    """Step 4 is a documented stub. We have no real samples, and a guessed format
    would return a confidently wrong part identification — worse than nothing,
    because it becomes a movement in an append-only ledger. This asserts the
    endpoint never reports `lcsc`, so implementing the parser has to come with a
    deliberate update here."""
    for payload in ("C25804", "C25804,0603WAF4701T5E,100", "{'pc':'C25804','qty':'100'}"):
        body = resolve(client, payload)
        assert body["decoded_kind"] != ScanDecodedKind.LCSC
        assert body["decoded_kind"] == ScanDecodedKind.UNKNOWN
        assert body["suggest_bind"] is True


def test_the_ecia_step_does_not_claim_a_bare_short_id(client: TestClient, db: Session) -> None:
    """Regression guard for a real trap: `4K` is a genuine Data Identifier, so a
    library whose contract is "degrades, never raises" happily reads the short ID
    `4K7T92M8` as a purchase order. If step 3 accepted that, every unbound short
    ID would be reported as a decoded distributor label."""
    drawer = make_location(db, name="Drawer B2")
    code = shortid.allocate(db, EntityType.LOCATION, drawer.id)
    db.commit()

    assert resolve(client, code)["decoded_kind"] == ScanDecodedKind.SHORT_ID
    # And an unbound one is not claimed by step 3 either.
    assert resolve(client, shortid.generate())["decoded_kind"] == ScanDecodedKind.UNKNOWN


# ---------------------------------------------------------------------------
# Step 1: short IDs and the tag URL
# ---------------------------------------------------------------------------


def test_the_tag_url_resolves_at_step_one(client: TestClient, db: Session) -> None:
    """The payload written to every NFC tag and printed QR is
    `{base_url}/s/{short_id}`, so the URL form is the *normal* input to this
    endpoint, not an edge case."""
    drawer = make_location(db, name="Drawer C3")
    code = shortid.allocate(db, EntityType.LOCATION, drawer.id)
    db.commit()

    body = resolve(client, f"{get_settings().base_url}/s/{shortid.format_display(code)}")
    assert body["decoded_kind"] == ScanDecodedKind.SHORT_ID
    assert body["target"]["entity_pk"] == drawer.id
    assert body["target"]["display"].startswith("BIN ")


def test_a_valid_but_unbound_short_id_falls_through_so_it_can_be_bound(
    client: TestClient, db: Session
) -> None:
    """**Why step 1 claims only what it resolves.** An arbitrary 8-symbol code
    passes the mod-37 check about one time in thirty. If step 1 claimed a
    well-formed miss and stopped the chain, binding that code as an alias would be
    permanently unreachable — step 1 runs first — and the learning loop could
    never repair a misclassification. `/s/{short_id}` remains the dedicated
    provisioning path for a genuinely blank tag, because that URL is proof the
    payload is ours."""
    part = make_part(db, name="Bag of unknowns", mpn="SALVAGE-2")
    db.commit()
    orphan = shortid.generate()

    missed = resolve(client, orphan)
    assert missed["status"] == "unknown"
    assert missed["suggest_bind"] is True

    bind(client, orphan, EntityType.PART, part.id)

    assert resolve(client, orphan)["target"]["entity_pk"] == part.id


# ---------------------------------------------------------------------------
# ambiguous versus unknown
# ---------------------------------------------------------------------------


def test_one_part_number_on_two_parts_is_ambiguous_not_a_guess(
    client: TestClient, db: Session
) -> None:
    """Two manufacturers really do ship `LM358N`. `uq_parts_mpn_norm_manufacturer`
    permits exactly this, so the resolver has to ask rather than pick — a
    plausible wrong part is a field failure."""
    ti = make_manufacturer(db, name="Texas Instruments")
    onsemi = make_manufacturer(db, name="ON Semiconductor")
    first = make_part(db, name="LM358N (TI)", mpn="LM358N", manufacturer_id=ti.id)
    second = make_part(db, name="LM358N (onsemi)", mpn="LM358N", manufacturer_id=onsemi.id)
    db.commit()

    body = resolve(client, "LM358N")
    assert body["status"] == "ambiguous"
    assert body["decoded_kind"] == ScanDecodedKind.MPN
    assert body["target"] is None
    assert {c["target"]["entity_pk"] for c in body["candidates"]} == {first.id, second.id}
    # Ambiguous carries the bind offer too: naming which one it is *is* the fix.
    assert body["suggest_bind"] is True
    assert body["parsed"]["mpn"] == "LM358N"


def test_the_manufacturer_di_breaks_an_mpn_tie(client: TestClient, db: Session) -> None:
    """DI `1V` is the disambiguator by construction, since rows sharing an
    `mpn_norm` differ only by manufacturer. Conservative on purpose: an exact
    match on the squashed name, so a narrowing that fails leaves the choice with
    the user instead of discarding the right candidate."""
    ti = make_manufacturer(db, name="Texas Instruments")
    onsemi = make_manufacturer(db, name="ON Semiconductor")
    wanted = make_part(db, name="LM358N (TI)", mpn="LM358N", manufacturer_id=ti.id)
    make_part(db, name="LM358N (onsemi)", mpn="LM358N", manufacturer_id=onsemi.id)
    db.commit()

    without = resolve(client, ecia("PLM358N", "Q100"))
    assert without["status"] == "ambiguous"

    narrowed = resolve(client, ecia("PLM358N", "Q100", "1VTexas Instruments"))
    assert narrowed["status"] == "resolved"
    assert narrowed["target"]["entity_pk"] == wanted.id

    # An unrecognised manufacturer must not narrow the list to nothing.
    unknown_maker = resolve(client, ecia("PLM358N", "Q100", "1VA Factory We Never Heard Of"))
    assert unknown_maker["status"] == "ambiguous"


def test_a_decoded_label_for_a_part_we_do_not_hold_is_unknown_not_ambiguous(
    client: TestClient,
) -> None:
    """`decoded_kind` and `status` are independent facts, and this is the case that
    proves it: the label read perfectly, the catalogue simply has no such part.
    Conflating the two would make a missing parser and a missing part look the
    same in the mining query, which is the one report that says where intake
    hurts."""
    body = resolve(
        client,
        ecia("PUNSEEN-MPN-9999", "1P999-9999-9-ND", "Q2500", "1TLOT4711", "9D2325", "4LMY"),
        symbology="datamatrix",
    )

    assert body["status"] == "unknown"
    assert body["decoded_kind"] == ScanDecodedKind.ECIA
    assert body["candidates"] == []
    assert body["suggest_bind"] is True

    # ...and every field the label carried is handed back, so the intake form is
    # pre-filled even though nothing resolved. Hints, never authority.
    parsed = body["parsed"]
    assert parsed["mpn"] == "UNSEEN-MPN-9999"
    assert parsed["supplier_part_number"] == "999-9999-9-ND"
    assert parsed["quantity_milli"] == 2_500_000
    assert parsed["lot_code"] == "LOT4711"
    assert parsed["date_code"] == "2325"
    assert parsed["country_of_origin"] == "MY"
    assert parsed["di_fields"]["Q"] == ["2500"]
    assert parsed["confidence"] == pytest.approx(1.0)


def test_one_code_bound_to_two_things_is_ambiguous(client: TestClient, db: Session) -> None:
    """`barcode_aliases.code_norm` is deliberately not unique: two suppliers ship
    the same EAN. That is a question for the user, not a constraint violation."""
    first = make_part(db, name="First", mpn="FIRST-1")
    second = make_part(db, name="Second", mpn="SECOND-1")
    db.commit()

    bind(client, "SHARED-CODE", EntityType.PART, first.id)
    bind(client, "SHARED-CODE", EntityType.PART, second.id)

    body = resolve(client, "SHARED-CODE")
    assert body["status"] == "ambiguous"
    assert body["decoded_kind"] == ScanDecodedKind.ALIAS
    assert len(body["candidates"]) == 2
    assert all(candidate["alias_id"] for candidate in body["candidates"])


def test_an_unparseable_payload_still_carries_a_parsed_object(client: TestClient) -> None:
    """Uniform contract: `ambiguous` and `unknown` always carry `parsed`, even when
    no handler recognised the format, so a client never has to branch on its
    presence to render the bind prompt."""
    body = resolve(client, "@@ nothing recognises this @@")
    assert body["status"] == "unknown"
    assert body["parsed"] is not None
    assert body["parsed"]["mpn"] is None


def test_a_payload_of_only_separators_offers_no_binding(client: TestClient) -> None:
    """It normalises to an empty key, and a binding keyed on nothing would shadow
    every future empty scan. Still recorded, still not an error."""
    body = resolve(client, "  --__  ")
    assert body["status"] == "unknown"
    assert body["normalized"] == ""
    assert body["suggest_bind"] is False


# ---------------------------------------------------------------------------
# Alias learning
# ---------------------------------------------------------------------------


def test_an_unknown_payload_binds_and_then_resolves_for_ever(
    client: TestClient, db: Session
) -> None:
    """The whole feature, in one test. This is what makes an unwritten parser —
    LCSC's, today — cost taps instead of blocking intake."""
    part = make_part(db, name="LCSC bag of 0603 resistors", mpn="RC0603FR-074K7L")
    db.commit()
    payload = "C25804,0603WAF4701T5E,100"

    first = resolve(client, payload)
    assert first["status"] == "unknown"
    assert first["decoded_kind"] == ScanDecodedKind.UNKNOWN
    assert first["suggest_bind"] is True

    taught = bind(
        client,
        payload,
        EntityType.PART,
        part.id,
        hint_qty_milli=100_000,
        hint_batch="C25804",
    )
    assert taught["created"] is True
    assert taught["code_norm"] == codes.normalize_code(payload)
    assert taught["target"]["label"] == "RC0603FR-074K7L"

    for _ in range(2):
        again = resolve(client, payload)
        assert again["status"] == "resolved"
        assert again["decoded_kind"] == ScanDecodedKind.ALIAS
        assert again["target"]["entity_pk"] == part.id
        assert again["suggest_bind"] is False

    alias = fresh(db).execute(select(BarcodeAlias)).scalar_one()
    assert alias.hint_qty_milli == 100_000
    assert alias.hint_batch == "C25804"


def test_binding_a_supplier_sku_resolves_the_next_reel(client: TestClient, db: Session) -> None:
    """Why `AliasKind` distinguishes what was bound.

    A whole-payload binding recognises *that one reel*, because the payload
    carries its lot and quantity. Binding the supplier SKU lifted out of the same
    label is what makes the **next** reel of the same part resolve on its first
    scan — a different payload entirely, sharing only its `1P`.
    """
    part = make_part(db, name="Dual op-amp", mpn="OPAMP-HOUSE-CODE")
    db.commit()

    first_reel = ecia("PMYSTERY-A", "1P296-1395-1-ND", "Q2500", "1TLOT4711")
    second_reel = ecia("PMYSTERY-A", "1P296-1395-1-ND", "Q1000", "1TLOT9902")

    assert resolve(client, first_reel)["status"] == "unknown"

    bind(
        client,
        "296-1395-1-ND",
        EntityType.PART,
        part.id,
        alias_kind=AliasKind.SUPPLIER_SKU.value,
        symbology="datamatrix",
    )

    # The whole payload was never bound, so this can only have come through the
    # SKU — and it is still step 3 that claimed it, since a whole-payload lookup
    # at step 2 finds nothing.
    body = resolve(client, second_reel)
    assert body["status"] == "resolved"
    assert body["decoded_kind"] == ScanDecodedKind.ECIA
    assert body["target"]["entity_pk"] == part.id
    assert body["candidates"][0]["via"].startswith("ecia_alias:")
    # The new reel's own quantity and lot come back, not the taught reel's.
    assert body["parsed"]["quantity_milli"] == 1_000_000
    assert body["parsed"]["lot_code"] == "LOT9902"


def test_symbology_is_recorded_but_is_not_part_of_the_lookup(
    client: TestClient, db: Session
) -> None:
    """The same physical label read by a phone camera and by a HID wedge arrives
    with two different symbology spellings. A binding that only worked with the
    reader that taught it would be a binding the user has to teach twice."""
    part = make_part(db, name="Bag", mpn="BAG-1")
    db.commit()

    bind(client, "VENDOR-CODE-77", EntityType.PART, part.id, symbology="datamatrix")

    body = resolve(client, "vendor code 77", symbology="a-reader-we-have-never-heard-of")
    assert body["status"] == "resolved"
    assert body["target"]["entity_pk"] == part.id


def test_each_unambiguous_hit_is_counted(client: TestClient, db: Session) -> None:
    """`hit_count` ranks candidates and exposes bindings nobody uses, so it has to
    mean "times this binding was the answer"."""
    part = make_part(db, name="Bag", mpn="BAG-2")
    db.commit()
    bind(client, "COUNTED-CODE", EntityType.PART, part.id)

    for expected in (1, 2, 3):
        body = resolve(client, "COUNTED-CODE")
        assert body["candidates"][0]["hit_count"] == expected - 1  # as read before the bump
        alias = fresh(db).execute(select(BarcodeAlias)).scalar_one()
        assert alias.hit_count == expected
        assert alias.last_hit_at is not None


def test_an_ambiguous_match_counts_as_nothing(client: TestClient, db: Session) -> None:
    """Bumping every candidate would raise them all equally and flatten the very
    ordering that exists to break the tie."""
    first = make_part(db, name="First", mpn="FIRST-2")
    second = make_part(db, name="Second", mpn="SECOND-2")
    db.commit()
    bind(client, "TIED-CODE", EntityType.PART, first.id)
    bind(client, "TIED-CODE", EntityType.PART, second.id)

    assert resolve(client, "TIED-CODE")["status"] == "ambiguous"

    counts = fresh(db).execute(select(BarcodeAlias.hit_count)).scalars().all()
    assert list(counts) == [0, 0]


def test_rebinding_the_same_answer_is_an_upsert_and_counts_as_confirmation(
    client: TestClient, db: Session
) -> None:
    """`UNIQUE(code_norm, symbology, entity_type, entity_pk)` makes re-teaching an
    update rather than an error, and a user binding the same label to the same
    part twice is confirming it."""
    part = make_part(db, name="Bag", mpn="BAG-3")
    db.commit()

    created = bind(client, "REBOUND", EntityType.PART, part.id, hint_batch="FIRST")
    assert created["created"] is True
    assert created["hit_count"] == 0

    again = bind(client, "REBOUND", EntityType.PART, part.id, hint_batch="SECOND")
    assert again["created"] is False
    assert again["alias_id"] == created["alias_id"]
    assert again["hit_count"] == 1

    # Hints describe the label most recently taught from, so a reprinted reel
    # label pre-fills the new quantity rather than the old one.
    assert fresh(db).execute(select(BarcodeAlias)).scalar_one().hint_batch == "SECOND"


def test_a_higher_ranked_binding_leads_the_candidate_list(client: TestClient, db: Session) -> None:
    first = make_part(db, name="Rarely meant", mpn="RARE-1")
    second = make_part(db, name="Usually meant", mpn="USUAL-1")
    db.commit()
    bind(client, "RANKED", EntityType.PART, first.id)
    for _ in range(3):
        bind(client, "RANKED", EntityType.PART, second.id)

    body = resolve(client, "RANKED")
    assert body["status"] == "ambiguous"
    assert body["candidates"][0]["target"]["entity_pk"] == second.id


def test_binding_an_empty_code_is_refused(client: TestClient, db: Session) -> None:
    """The one thing this endpoint says no to. A key of `""` would shadow every
    payload that normalises away, which is the opposite of learning."""
    part = make_part(db, name="Bag", mpn="BAG-4")
    db.commit()

    response = client.post(
        "/api/scan/alias",
        json={
            "code": "  --  ",
            "symbology": "qr",
            "entity_type": EntityType.PART.value,
            "entity_pk": part.id,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "empty_code"


def test_binding_to_a_row_that_does_not_exist_is_a_404(client: TestClient) -> None:
    response = client.post(
        "/api/scan/alias",
        json={
            "code": "ORPHAN-BIND",
            "symbology": "qr",
            "entity_type": EntityType.PART.value,
            "entity_pk": 999_999,
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "unknown_target"


def test_an_unmodelled_entity_type_is_accepted_unchecked(client: TestClient) -> None:
    """`supplier_parts` arrives in Phase 5, and the alias table has always been
    polymorphic by design. Refusing a forward-looking binding would be worse than
    a dangling one, which merely displays as a bare type-and-id label."""
    body = bind(client, "FUTURE-SUPPLIER-SKU", EntityType.SUPPLIER_PART, 42)
    assert body["target"]["label"] == "supplier_part 42"


# ---------------------------------------------------------------------------
# existing_lots — workflow 2
# ---------------------------------------------------------------------------


@pytest.fixture
def stocked_part(db: Session) -> tuple[Part, Location]:
    """A part with stock in a nested drawer, plus an empty lot elsewhere."""
    cabinet = make_location(db, name="Cabinet A")
    drawer = make_location(db, name="Drawer 3", parent_id=cabinet.id)
    spare = make_location(db, name="Spare bin")
    location_tree(db).rebuild_paths()

    part = make_part(db, name="Dual op-amp", mpn="LM358N")
    make_lot(db, part, drawer, qty_milli=2_400_000)
    make_lot(db, part, spare, qty_milli=0)
    db.commit()
    return part, drawer


def test_existing_lots_are_attached_so_the_ui_can_skip_ahead(
    client: TestClient, db: Session, stocked_part: tuple[Part, Location]
) -> None:
    """Workflow 2 branches to known-part re-stock **before** enrichment or
    dimensions run, and it can only do that if the resolution already says "you
    have 2400 of these in Cabinet A / Drawer 3". A resolver responsibility, not
    the UI's: leaving it to a follow-up request would mean the client had to know
    to ask, and the fast path would keep paying for screens it should skip.
    """
    part, drawer = stocked_part

    body = resolve(client, part.mpn or "")
    assert body["status"] == "resolved"

    lots = body["existing_lots"]
    # Only the lot with quantity above zero — an empty lot is not somewhere to
    # add stock, it is bookkeeping.
    assert len(lots) == 1
    assert lots[0]["location_id"] == drawer.id
    assert lots[0]["qty_milli"] == 2_400_000
    # The path is derived here and now. Encoding it anywhere durable would make
    # it a lie the moment the drawer changed cabinet.
    assert lots[0]["location_label_path"] == "Cabinet A / Drawer 3"


def test_scanning_one_lot_reports_the_others(
    client: TestClient, db: Session, stocked_part: tuple[Part, Location]
) -> None:
    """A lot resolves to a lot, but the question behind the scan is still about
    the part: the other reel on the shelf is exactly what the user needs to see
    before starting a third one."""
    part, _ = stocked_part
    stocked = db.execute(
        select(StockLot).where(StockLot.part_id == part.id, StockLot.qty_milli_cached > 0)
    ).scalar_one()
    code = shortid.allocate(db, EntityType.STOCK_LOT, stocked.id)
    db.commit()

    body = resolve(client, code)
    assert body["target"]["entity_type"] == EntityType.STOCK_LOT
    assert body["target"]["label"] == "LM358N"
    assert body["target"]["label_path"] == "Cabinet A / Drawer 3"
    assert [lot["lot_id"] for lot in body["existing_lots"]] == [stocked.id]


def test_a_location_scan_carries_no_lots(client: TestClient, db: Session) -> None:
    """A bin is not a part, and the re-stock branch is about parts."""
    drawer = make_location(db, name="Drawer D4")
    code = shortid.allocate(db, EntityType.LOCATION, drawer.id)
    db.commit()

    assert resolve(client, code)["existing_lots"] == []


def test_an_ambiguous_match_attaches_no_lots(client: TestClient, db: Session) -> None:
    """Nothing has been matched yet, so there is no "the matched part" to report
    stock for. Guessing one of the candidates would be the same mistake the
    ambiguous branch exists to avoid."""
    ti = make_manufacturer(db, name="Texas Instruments")
    onsemi = make_manufacturer(db, name="ON Semiconductor")
    drawer = make_location(db, name="Drawer E5")
    first = make_part(db, name="LM358N (TI)", mpn="LM358N", manufacturer_id=ti.id)
    make_part(db, name="LM358N (onsemi)", mpn="LM358N", manufacturer_id=onsemi.id)
    make_lot(db, first, drawer, qty_milli=1_000)
    db.commit()

    body = resolve(client, "LM358N")
    assert body["status"] == "ambiguous"
    assert body["existing_lots"] == []


# ---------------------------------------------------------------------------
# scan_events — the raw log
# ---------------------------------------------------------------------------


def test_every_scan_is_recorded_verbatim_with_its_latency(client: TestClient, db: Session) -> None:
    """Including the ones nothing came of — especially those. An unparsed vendor
    format sitting in a table is a parser waiting to be written; discarded, it is
    gone. The control characters go in too, since stripping them is exactly the
    lossy step that would make the format unmineable."""
    payload = ecia("PUNSEEN-MPN-9999", "Q2500")

    body = resolve(client, payload, symbology="datamatrix")

    event = fresh(db).get(ScanEvent, body["scan_event_id"])
    assert event is not None
    assert event.raw_payload == payload
    assert event.raw_payload.count("\x1d") == 2
    assert event.symbology == "datamatrix"
    assert event.decoded_kind == ScanDecodedKind.ECIA
    assert event.action_taken == ScanAction.UNRESOLVED
    assert event.resolved_entity_type is None
    assert event.latency_ms is not None
    assert event.latency_ms >= 0
    assert len(event.payload_sha256) == 64


def test_a_resolved_scan_records_what_it_resolved_to(client: TestClient, db: Session) -> None:
    drawer = make_location(db, name="Drawer F6")
    code = shortid.allocate(db, EntityType.LOCATION, drawer.id)
    db.commit()

    body = resolve(client, code)

    event = fresh(db).get(ScanEvent, body["scan_event_id"])
    assert event is not None
    assert event.action_taken == ScanAction.RESOLVED
    assert event.decoded_kind == ScanDecodedKind.SHORT_ID
    assert event.resolved_entity_type == EntityType.LOCATION
    assert event.resolved_entity_pk == drawer.id


def test_teaching_a_binding_is_logged_as_bound(client: TestClient, db: Session) -> None:
    """A second row for a payload that already has an `unresolved` one, and that
    pair is the evidence the learning loop closed. Counting only the unresolved
    scan would make intake look permanently broken; counting only the bind would
    hide how long the code went unrecognised."""
    part = make_part(db, name="Bag", mpn="BAG-5")
    db.commit()

    resolve(client, "TAUGHT-CODE")
    taught = bind(client, "TAUGHT-CODE", EntityType.PART, part.id)

    event = fresh(db).get(ScanEvent, taught["scan_event_id"])
    assert event is not None
    assert event.action_taken == ScanAction.BOUND
    assert event.decoded_kind == ScanDecodedKind.ALIAS
    assert event.resolved_entity_pk == part.id

    actions = db.execute(select(ScanEvent.action_taken).order_by(ScanEvent.id)).scalars().all()
    assert list(actions) == [ScanAction.UNRESOLVED, ScanAction.BOUND]


def test_a_registered_reader_is_attributed_and_touched(client: TestClient, db: Session) -> None:
    """Attribution stops sounding like bookkeeping the first time one reader
    starts producing garbage payloads and the log can say which."""
    source = ScanSource(
        slug="bench-phone", display_name="Bench phone", kind=ScanSourceKind.BROWSER_CAMERA
    )
    db.add(source)
    db.commit()

    body = resolve(client, "SOME-CODE", source_slug="bench-phone")

    session = fresh(db)
    event = session.get(ScanEvent, body["scan_event_id"])
    assert event is not None
    assert event.source_id == source.id
    reader = session.execute(select(ScanSource)).scalar_one()
    assert reader.last_seen_at is not None


def test_an_unregistered_reader_still_gets_its_scan_recorded(
    client: TestClient, db: Session
) -> None:
    """`scan_events.source_id` is nullable exactly for this. Refusing the scan
    would mean a new phone cannot be used until someone registers it."""
    body = resolve(client, "SOME-CODE", source_slug="a-phone-nobody-registered")

    event = fresh(db).get(ScanEvent, body["scan_event_id"])
    assert event is not None
    assert event.source_id is None


# ---------------------------------------------------------------------------
# Hostile input
# ---------------------------------------------------------------------------

HOSTILE = {
    "empty": "",
    "whitespace": "   \t\r\n ",
    "bare_separators": "\x1d\x1e\x04",
    "nul_and_binary": "\x00\x01\x02\x1d\xff\x7f",
    "sql_injection": "'; DROP TABLE parts; --",
    "sqlite_master": '" UNION SELECT sql FROM sqlite_master --',
    "fts_syntax": 'parts OR "" NEAR/3 *',
    "unicode": "\u65e5\u672c\u8a9e \u2728 \u03a9 \U0001f600 \u202e",
    "unicode_digits": "\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18",
    "cjk_only": "\u62b5\u6297\u5668",
    "url_shaped": "https://example.test/s/../../etc/passwd",
    "format_string": "%s %d {0} ${PATH}",
    "at_the_payload_limit": "A" * 4096,
    "deep_separators": "\x1d".join(["P" + "x" * 8] * 200),
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_hostile_payloads_never_produce_a_server_error(
    client: TestClient, db: Session, name: str
) -> None:
    """A scan is never rejected, so the failure mode to guard against is not a
    refusal but a 500 — a stack trace on a phone held against a drawer. Every one
    of these has to come back as an ordinary resolution, be recorded verbatim, and
    leave the schema alone."""
    before = db.execute(select(func.count()).select_from(Part)).scalar_one()

    response = client.post("/api/scan/resolve", json={"code": HOSTILE[name]})
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["status"] in {"resolved", "ambiguous", "unknown"}
    assert body["decoded_kind"] in set(ScanDecodedKind)

    session = fresh(db)
    event = session.get(ScanEvent, body["scan_event_id"])
    assert event is not None
    assert event.raw_payload == HOSTILE[name]
    assert session.execute(select(func.count()).select_from(Part)).scalar_one() == before


def test_a_payload_past_the_limit_is_refused_not_stored(client: TestClient, db: Session) -> None:
    """The one refusal on this endpoint, and it happens before the database is
    touched: a dense reel label is a couple of hundred bytes and a QR maxes out
    near 3 kB, so past 4 kB this is a reader fault, and a huge body must not become
    a huge row."""
    response = client.post("/api/scan/resolve", json={"code": "A" * 4097})
    assert response.status_code == 422

    assert fresh(db).execute(select(func.count()).select_from(ScanEvent)).scalar_one() == 0


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_hostile_payloads_never_produce_a_server_error_when_bound(
    client: TestClient, db: Session, name: str
) -> None:
    """The bind endpoint takes the same untrusted text. Anything that normalises
    to a usable key must bind cleanly; anything that does not must be a 422 with a
    reason, never a crash."""
    part = make_part(db, name="Bag", mpn="BAG-6")
    db.commit()

    response = client.post(
        "/api/scan/alias",
        json={
            "code": HOSTILE[name],
            "symbology": "datamatrix",
            "entity_type": EntityType.PART.value,
            "entity_pk": part.id,
        },
    )
    assert response.status_code in {200, 422}, response.text
    if response.status_code == 422:
        assert response.json()["detail"]["reason"] in {"empty_code", "code_too_long"}
