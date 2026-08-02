"""Bulk tag provisioning: the derived cursor, auto-advance, and undo.

The property under test throughout is that **the cursor is never stored**. Every
assertion about "where the walk is" is really an assertion that the answer was
recomputed — which is why `test_the_cursor_moves_when_a_drawer_is_bound_out_of_band`
binds a drawer without going through the session at all, and why
`test_a_mid_session_abort_costs_nothing` throws the session away and picks it up
from a different one. A stored cursor passes neither.

The other half is that the walk is walking-paced: one request per drawer, and the
response carries the advanced cursor so there is no second round trip. If bind
ever stops returning `state.cursor`, `test_binding_44_drawers_takes_44_requests`
fails on the request count rather than on correctness, which is deliberate — the
2-3 s per drawer target is what makes a 44-drawer cabinet get populated at all.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.storage import LocationTag
from app.services import provisioning


def _uid(n: int) -> str:
    """An NTAG-style 7-byte UID, rendered as the 14 hex digits a reader hands
    over. `04` is the real NXP manufacturer byte, so these look like tags."""
    return f"04{n:012X}"


def _cabinet(client: TestClient, *, slots: int = 6, name: str = "Cabinet") -> dict:
    """A room holding one cabinet whose `slots` drawers are generated cells.

    Built through `instantiate` rather than by hand so `sort_order` is the real
    reading order the cursor depends on.
    """
    room = client.post("/api/locations", json={"name": f"{name} room"}).json()["location"]
    type_response = client.post(
        "/api/container-types",
        json={
            "slug": f"{name.lower().replace(' ', '-')}-type",
            "display_name": name,
            "child_layout": "grid",
            "grid_rows": slots,
            "grid_cols": 1,
            "slot_label_scheme": "row_alpha_col_num",
        },
    )
    assert type_response.status_code == 201, type_response.text
    container_type = type_response.json()["container_type"]

    created = client.post(
        f"/api/locations/{room['id']}/instantiate",
        json={"container_type_id": container_type["id"], "count": 1, "naming_pattern": name},
    )
    assert created.status_code == 201, created.text
    return created.json()["locations"][0]


def _layout_slots(client: TestClient, cabinet_id: int) -> list[dict]:
    return client.get(f"/api/locations/{cabinet_id}/layout").json()["slots"]


def _start(client: TestClient, cabinet_id: int, **body: object) -> dict:
    response = client.post(
        f"/api/locations/{cabinet_id}/provisioning-sessions",
        json={"device_kind": "phone_webnfc", **body},
    )
    assert response.status_code == 201, response.text
    return response.json()["state"]


def _bind(client: TestClient, session_id: int, uid: str, **body: object) -> dict:
    response = client.post(
        f"/api/provisioning-sessions/{session_id}/bind", json={"tag_uid": uid, **body}
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# The cursor is a derivation
# ---------------------------------------------------------------------------


def test_the_cursor_starts_at_the_first_slot_in_reading_order(client: TestClient) -> None:
    cabinet = _cabinet(client, slots=4)
    state = _start(client, cabinet["id"])

    assert state["cursor"]["slot_label"] == "A1"
    assert state["progress"] == {
        "total_slots": 4,
        "bound": 0,
        "unbound": 4,
        "skipped": 0,
        "is_complete": False,
    }
    # A generated cell has no printed identity until something needs one.
    assert state["cursor"]["short_id"] is None


def test_binding_auto_advances_the_cursor(client: TestClient) -> None:
    """One request per drawer. The response carries the next slot, because the
    phone is still holding the tag against the drawer when this returns."""
    cabinet = _cabinet(client, slots=3)
    session_id = _start(client, cabinet["id"])["session"]["id"]

    first = _bind(client, session_id, _uid(1))
    assert first["status"] == "bound"
    assert first["tag"]["tag_uid"] == _uid(1)
    assert first["state"]["cursor"]["slot_label"] == "B1"
    assert first["state"]["progress"]["bound"] == 1

    second = _bind(client, session_id, _uid(2))
    assert second["state"]["cursor"]["slot_label"] == "C1"


def test_the_cursor_moves_when_a_drawer_is_bound_out_of_band(client: TestClient) -> None:
    """The whole reason the cursor is derived rather than stored.

    Someone binds one drawer from their phone in the middle of a station session.
    A stored cursor would still be pointing at B1 — a drawer that already has a
    tag — and the walk would try to bind it twice.
    """
    cabinet = _cabinet(client, slots=4)
    slots = {slot["slot_label"]: slot for slot in _layout_slots(client, cabinet["id"])}
    session_id = _start(client, cabinet["id"])["session"]["id"]

    _bind(client, session_id, _uid(1))  # A1, through the session
    assert _start(client, cabinet["id"])["cursor"]["slot_label"] == "B1"

    # ...and now B1 gets bound from somewhere else entirely: a second session on
    # the same cabinet, which is exactly what a phone joining mid-walk is.
    other_session = client.post(
        f"/api/locations/{cabinet['id']}/provisioning-sessions",
        json={"device_kind": "station_pn532"},
    ).json()["state"]["session"]["id"]
    _bind(client, other_session, _uid(2), location_id=slots["B1"]["location_id"])

    resumed = client.get(f"/api/locations/{cabinet['id']}/provisioning-sessions/current").json()
    assert resumed["cursor"]["slot_label"] == "C1"
    assert resumed["progress"]["bound"] == 2


def test_two_sessions_on_one_cabinet_are_the_same_session(client: TestClient) -> None:
    """`open_session` resumes rather than duplicating: with the cursor derived, a
    second session would report the identical position, and two "current"
    sessions is a state with no correct answer."""
    cabinet = _cabinet(client, slots=2)
    first = _start(client, cabinet["id"])["session"]["id"]
    second = _start(client, cabinet["id"])["session"]["id"]
    assert first == second


def test_a_mid_session_abort_costs_nothing(client: TestClient) -> None:
    """The phone goes in a pocket halfway down the cabinet and never comes back.

    Nothing was saved, so nothing needs restoring: the position is recomputed
    from `location_tags` by whatever picks the walk up next.
    """
    cabinet = _cabinet(client, slots=5)
    session_id = _start(client, cabinet["id"])["session"]["id"]
    _bind(client, session_id, _uid(1))
    _bind(client, session_id, _uid(2))

    # Simulate the abort: a fresh client with no memory of the session at all,
    # asking only "what is the state of this cabinet".
    resumed = client.get(f"/api/locations/{cabinet['id']}/provisioning-sessions/current").json()
    assert resumed["session"]["id"] == session_id
    assert resumed["cursor"]["slot_label"] == "C1"
    assert resumed["progress"] == {
        "total_slots": 5,
        "bound": 2,
        "unbound": 3,
        "skipped": 0,
        "is_complete": False,
    }


def test_the_cabinet_state_is_readable_with_no_walk_open(client: TestClient) -> None:
    """ "12 of 44 tagged" does not depend on a session existing — the cursor never
    did either."""
    cabinet = _cabinet(client, slots=3)
    idle = client.get(f"/api/locations/{cabinet['id']}/provisioning-sessions/current").json()
    assert idle["session"] is None
    assert idle["cursor"]["slot_label"] == "A1"
    assert idle["progress"]["total_slots"] == 3
    assert idle["undo_depth"] == 0


# ---------------------------------------------------------------------------
# Escape hatches: jump, skip, undo
# ---------------------------------------------------------------------------


def test_tapping_any_cell_jumps_the_cursor_there(client: TestClient) -> None:
    """Auto-advance is a fast path, not a lock."""
    cabinet = _cabinet(client, slots=4)
    slots = {slot["slot_label"]: slot for slot in _layout_slots(client, cabinet["id"])}
    session_id = _start(client, cabinet["id"])["session"]["id"]

    jumped = _bind(client, session_id, _uid(9), location_id=slots["D1"]["location_id"])
    assert jumped["tag"]["location_id"] == slots["D1"]["location_id"]
    # The cursor falls back to the first slot still needing a tag, not to E1.
    assert jumped["state"]["cursor"]["slot_label"] == "A1"


def test_a_cell_outside_the_walk_is_refused(client: TestClient) -> None:
    cabinet = _cabinet(client, slots=2)
    other = _cabinet(client, slots=2, name="Other cabinet")
    stranger = _layout_slots(client, other["id"])[0]
    session_id = _start(client, cabinet["id"])["session"]["id"]

    response = client.post(
        f"/api/provisioning-sessions/{session_id}/bind",
        json={"tag_uid": _uid(1), "location_id": stranger["location_id"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "out_of_scope"


def test_skip_leaves_a_slot_empty_and_advances(client: TestClient) -> None:
    cabinet = _cabinet(client, slots=3)
    session_id = _start(client, cabinet["id"])["session"]["id"]

    response = client.post(f"/api/provisioning-sessions/{session_id}/skip", json={})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["skipped"]["slot_label"] == "A1"
    assert body["skipped"]["has_tag"] is False
    assert body["state"]["cursor"]["slot_label"] == "B1"
    assert body["state"]["progress"]["skipped"] == 1
    assert body["state"]["progress"]["bound"] == 0


def test_a_skip_is_scoped_to_its_session(client: TestClient) -> None:
    """ "Left empty for now" is not "never", so a later walk offers it again."""
    cabinet = _cabinet(client, slots=2)
    session_id = _start(client, cabinet["id"])["session"]["id"]
    client.post(f"/api/provisioning-sessions/{session_id}/skip", json={})
    _bind(client, session_id, _uid(2))  # B1, which completes the walk

    # The walk completed, so starting one now is a genuinely new session.
    fresh = _start(client, cabinet["id"])
    assert fresh["session"]["id"] != session_id
    assert fresh["cursor"]["slot_label"] == "A1"


def test_undo_removes_the_last_binding(client: TestClient) -> None:
    cabinet = _cabinet(client, slots=3)
    session_id = _start(client, cabinet["id"])["session"]["id"]
    _bind(client, session_id, _uid(1))
    _bind(client, session_id, _uid(2))

    response = client.post(f"/api/provisioning-sessions/{session_id}/undo", json={})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["undone"]["action_kind"] == "bind"
    assert body["undone"]["slot_label"] == "B1"
    assert body["state"]["cursor"]["slot_label"] == "B1"
    assert body["state"]["progress"]["bound"] == 1
    assert body["state"]["undo_label"] == "A1"


def test_undo_of_a_skip_puts_the_slot_back_in_the_cursor(client: TestClient) -> None:
    cabinet = _cabinet(client, slots=3)
    session_id = _start(client, cabinet["id"])["session"]["id"]
    client.post(f"/api/provisioning-sessions/{session_id}/skip", json={})
    assert _start(client, cabinet["id"])["cursor"]["slot_label"] == "B1"

    undone = client.post(f"/api/provisioning-sessions/{session_id}/undo", json={}).json()
    assert undone["undone"]["action_kind"] == "skip"
    assert undone["state"]["cursor"]["slot_label"] == "A1"
    assert undone["state"]["progress"]["skipped"] == 0


def test_undo_is_exactly_five_deep(client: TestClient) -> None:
    """Six binds, then Undo until it refuses: exactly five come back.

    The naive implementation — "the newest five that are not undone" — passes the
    first five and then keeps going for ever, because each undo promotes an
    older action into the window. That is a bottomless stack wearing a five-deep
    label, so the count here is the assertion that matters.
    """
    cabinet = _cabinet(client, slots=6)
    session_id = _start(client, cabinet["id"])["session"]["id"]
    for n in range(1, 7):
        last = _bind(client, session_id, _uid(n))

    assert last["state"]["undo_depth"] == provisioning.UNDO_DEPTH

    undone = 0
    while True:
        response = client.post(f"/api/provisioning-sessions/{session_id}/undo", json={})
        if response.status_code != 200:
            break
        undone += 1
        assert undone <= 10, "undo is not bounded at all"

    assert undone == provisioning.UNDO_DEPTH
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "nothing_to_undo"
    # The very first bind is out of reach, and still bound.
    remaining = client.get(f"/api/locations/{cabinet['id']}/layout").json()["slots"]
    assert [slot["has_tag"] for slot in remaining] == [True, False, False, False, False, False]


def test_undo_reopens_a_completed_walk(client: TestClient) -> None:
    """Binding the last drawer finishes the walk, and that is exactly the bind
    most likely to need taking back."""
    cabinet = _cabinet(client, slots=2)
    session_id = _start(client, cabinet["id"])["session"]["id"]
    _bind(client, session_id, _uid(1))
    last = _bind(client, session_id, _uid(2))

    assert last["state"]["progress"]["is_complete"] is True
    assert last["state"]["session"]["completed_at"] is not None
    assert last["state"]["cursor"] is None

    undone = client.post(f"/api/provisioning-sessions/{session_id}/undo", json={}).json()
    assert undone["state"]["session"]["completed_at"] is None
    assert undone["state"]["cursor"]["slot_label"] == "B1"


# ---------------------------------------------------------------------------
# A UID already bound elsewhere is reported, never silently rebound
# ---------------------------------------------------------------------------


def test_a_uid_bound_elsewhere_is_reported_not_rebound(client: TestClient) -> None:
    """The modal case: "Already bound to {label_path}", Move here / Cancel.

    A 200 rather than a 409 on purpose — this is an ordinary branch of the walk
    with a two-button answer, and the response has to name the other binding.
    """
    cabinet = _cabinet(client, slots=3)
    slots = {slot["slot_label"]: slot for slot in _layout_slots(client, cabinet["id"])}
    session_id = _start(client, cabinet["id"])["session"]["id"]
    _bind(client, session_id, _uid(1))  # A1

    clash = _bind(client, session_id, _uid(1))  # cursor is B1 now, same tag
    assert clash["status"] == "already_bound_elsewhere"
    assert clash["tag"] is None
    assert clash["conflict"]["location_id"] == slots["A1"]["location_id"]
    assert clash["conflict"]["label_path"].endswith("A1")
    # Nothing moved: the cursor is still on B1 and A1 still has the tag.
    assert clash["state"]["cursor"]["slot_label"] == "B1"
    assert clash["state"]["progress"]["bound"] == 1


def test_move_here_moves_the_binding_and_undo_restores_it(client: TestClient) -> None:
    """The five-deep undo has to put a *displaced* binding back, which is the
    one thing the walk log exists for: the row it came from is gone by then."""
    cabinet = _cabinet(client, slots=3)
    slots = {slot["slot_label"]: slot for slot in _layout_slots(client, cabinet["id"])}
    session_id = _start(client, cabinet["id"])["session"]["id"]
    first = _bind(client, session_id, _uid(1))  # A1
    original_url = first["tag"]["ndef_url"]

    moved = _bind(client, session_id, _uid(1), move=True)  # to B1
    assert moved["status"] == "moved"
    assert moved["tag"]["location_id"] == slots["B1"]["location_id"]
    # A1 is untagged again, so it is back at the front of the cursor.
    assert moved["state"]["cursor"]["slot_label"] == "A1"
    assert moved["state"]["progress"]["bound"] == 1

    undone = client.post(f"/api/provisioning-sessions/{session_id}/undo", json={}).json()
    assert undone["undone"]["action_kind"] == "move"
    assert undone["restored_tag"]["location_id"] == slots["A1"]["location_id"]
    assert undone["restored_tag"]["tag_uid"] == _uid(1)
    # Restored field by field, including the payload that is physically on the
    # tag — a restored binding pointing at a URL nobody wrote would be a lie.
    assert undone["restored_tag"]["ndef_url"] == original_url
    assert undone["state"]["cursor"]["slot_label"] == "B1"


def test_undo_will_not_put_a_tag_back_that_is_now_on_another_container(
    client: TestClient,
) -> None:
    """One physical tag must never be bound to two containers at once.

    `bind` refuses this outright — that is the `already_bound_elsewhere` prompt
    and the `two_conflicts` refusal — but undo's restore path used to check only
    that the *prior slot* was free, never whether the tag it was about to put
    back had gone somewhere else meanwhile. `location_tags.tag_uid` is indexed
    and **not** unique, so nothing below caught it: the duplicate resolved to
    whichever row had the lower id, meaning the station would identify the wrong
    container and commit stock into it while the other drawer's page showed a tag
    it did not own.

    The sequence is the one this module's own premise describes — another device
    binding mid-session — and it takes two overlapping walks:
    """
    cabinet = _cabinet(client, slots=3, name="Alpha")
    other = _cabinet(client, slots=3, name="Beta")
    walk = _start(client, cabinet["id"])["session"]["id"]
    _bind(client, walk, _uid(1))  # A1 of Alpha
    _bind(client, walk, _uid(1), move=True)  # moved to B1, A1 now free

    # A second walk claims the same physical tag for a different cabinet.
    elsewhere = _start(client, other["id"])["session"]["id"]
    stolen = _bind(client, elsewhere, _uid(1), move=True)
    assert stolen["status"] == "moved", stolen

    # Undoing the first walk's move wants to put the tag back on A1.
    undone = client.post(f"/api/provisioning-sessions/{walk}/undo", json={}).json()

    assert undone["restored_tag"] is None
    assert undone["not_restored_reason"] == "prior_tag_bound_elsewhere"

    # And the tag is bound in exactly one place: where the second walk put it.
    bindings = [
        slot for slot in _layout_slots(client, cabinet["id"]) if slot.get("tag_uid") == _uid(1)
    ]
    assert bindings == [], bindings


def test_undo_says_so_when_the_slot_was_rebound_by_somebody_else(
    client: TestClient, db: Session
) -> None:
    """ "Undone" must not be reported for an undo that removed nothing.

    When something rebinds the slot after this walk bound it, the delete is
    correctly skipped — but the action was still marked undone with no caveat, so
    the bench read "undone" and peeled a sticker off a drawer whose binding still
    stood. `bound_count` is left alone too: the binding is gone from the slot, but
    not by this walk's hand.

    The rebind is applied straight to `location_tags` rather than through a second
    walk, because two walks on one cabinet are deliberately the *same* walk
    (`test_two_sessions_on_one_cabinet_are_the_same_session`). What this stands
    for is the case the module's opening premise names: another device — a
    station, a Flipper — binding the slot outside this session.
    """
    cabinet = _cabinet(client, slots=3, name="Gamma")
    walk = _start(client, cabinet["id"])["session"]["id"]
    bound = _bind(client, walk, _uid(1))  # A1
    before = bound["state"]["progress"]["bound"]
    slot_id = bound["tag"]["location_id"]

    existing = db.execute(
        select(LocationTag).where(LocationTag.location_id == slot_id)
    ).scalar_one()
    existing.tag_uid = _uid(2)
    db.commit()

    undone = client.post(f"/api/provisioning-sessions/{walk}/undo", json={}).json()

    assert undone["not_restored_reason"] == "slot_rebound_since"
    assert undone["state"]["progress"]["bound"] == before
    # And the other device's binding is still standing — undo removed nothing.
    db.expire_all()
    still = db.execute(select(LocationTag).where(LocationTag.location_id == slot_id)).scalar_one()
    assert still.tag_uid == _uid(2)


def test_re_tapping_the_same_tag_on_the_same_slot_writes_nothing(client: TestClient) -> None:
    """The 400 ms client debounce loses a race, or the tag is simply tapped
    twice. Writing a second identical binding would add an undo step that undoes
    nothing."""
    cabinet = _cabinet(client, slots=2)
    slots = {slot["slot_label"]: slot for slot in _layout_slots(client, cabinet["id"])}
    session_id = _start(client, cabinet["id"])["session"]["id"]
    _bind(client, session_id, _uid(1))

    again = _bind(client, session_id, _uid(1), location_id=slots["A1"]["location_id"])
    assert again["status"] == "already_bound_here"
    assert again["state"]["undo_depth"] == 1


def test_a_slot_that_already_has_a_tag_is_reported(client: TestClient) -> None:
    cabinet = _cabinet(client, slots=2)
    slots = {slot["slot_label"]: slot for slot in _layout_slots(client, cabinet["id"])}
    session_id = _start(client, cabinet["id"])["session"]["id"]
    _bind(client, session_id, _uid(1))  # A1

    clash = _bind(client, session_id, _uid(2), location_id=slots["A1"]["location_id"])
    assert clash["status"] == "slot_already_bound"
    assert clash["conflict"]["tag_uid"] == _uid(1)

    rebound = _bind(client, session_id, _uid(2), location_id=slots["A1"]["location_id"], move=True)
    assert rebound["status"] == "rebound"
    assert rebound["tag"]["tag_uid"] == _uid(2)

    undone = client.post(f"/api/provisioning-sessions/{session_id}/undo", json={}).json()
    assert undone["undone"]["action_kind"] == "rebind"
    assert undone["restored_tag"]["tag_uid"] == _uid(1)


def test_two_conflicts_at_once_are_refused_rather_than_half_applied(client: TestClient) -> None:
    """One undo slot cannot hold two displaced bindings, and a half-reversible
    action is worse than a refusal the walk can act on."""
    cabinet = _cabinet(client, slots=3)
    slots = {slot["slot_label"]: slot for slot in _layout_slots(client, cabinet["id"])}
    session_id = _start(client, cabinet["id"])["session"]["id"]
    _bind(client, session_id, _uid(1))  # A1
    _bind(client, session_id, _uid(2))  # B1

    response = client.post(
        f"/api/provisioning-sessions/{session_id}/bind",
        json={"tag_uid": _uid(1), "location_id": slots["B1"]["location_id"], "move": True},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "two_conflicts"


# ---------------------------------------------------------------------------
# The payload, and nothing else
# ---------------------------------------------------------------------------


def test_binding_mints_a_short_id_and_writes_only_the_payload(client: TestClient) -> None:
    """One payload, two carriers: the NDEF URI record is the same string a
    printed QR carries, and it contains nothing mutable — no count, no fill
    state. A remote mutation cannot touch a tag it does not hold, so a tag
    carrying state would go stale while still looking authoritative."""
    cabinet = _cabinet(client, slots=2)
    session_id = _start(client, cabinet["id"])["session"]["id"]

    bound = _bind(client, session_id, _uid(1))
    slot_id = bound["tag"]["location_id"]
    short_id = client.get(f"/api/locations/{slot_id}").json()["short_id"]

    assert short_id is not None
    assert bound["tag"]["ndef_url"].endswith(f"/s/{short_id}")
    # The payload resolves through the same route that is stamped into physical
    # objects, and resolves to this slot.
    resolved = client.get(f"/api/resolve/{short_id}").json()
    assert resolved["target"]["entity_pk"] == slot_id


def test_the_tag_records_how_it_was_written(client: TestClient) -> None:
    cabinet = _cabinet(client, slots=2)
    session_id = _start(client, cabinet["id"], device_kind="station_pn532")["session"]["id"]
    bound = _bind(client, session_id, _uid(1))

    assert bound["tag"]["bind_source"] == "station_pn532"
    assert bound["tag"]["written_at"] is not None
    assert bound["tag"]["last_verified_at"] is None


def test_a_uid_is_normalised_so_two_readers_agree(client: TestClient) -> None:
    """A PN532 library prints `04:1A:2B`, Web NFC prints `041a2b`. Recording the
    same physical tag as two tags would make every mismatch a false positive."""
    cabinet = _cabinet(client, slots=3)
    session_id = _start(client, cabinet["id"])["session"]["id"]
    _bind(client, session_id, "04:1A:2B:3C:4D:5E:6F")

    clash = _bind(client, session_id, "041a2b3c4d5e6f")
    assert clash["status"] == "already_bound_elsewhere"


def test_a_malformed_uid_is_422(client: TestClient) -> None:
    cabinet = _cabinet(client, slots=2)
    session_id = _start(client, cabinet["id"])["session"]["id"]

    response = client.post(
        f"/api/provisioning-sessions/{session_id}/bind", json={"tag_uid": "not-a-uid"}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_tag_uid"


# ---------------------------------------------------------------------------
# The whole cabinet
# ---------------------------------------------------------------------------


def test_binding_44_drawers_takes_44_requests(client: TestClient) -> None:
    """A 44-drawer cabinet end to end, at one request per drawer.

    The request count is the point. Target is 2-3 s per drawer — the whole
    cabinet in under two minutes, walking-paced — which only holds if the bind
    response already carries the advanced cursor. A second round trip per drawer
    to ask "where am I now" doubles the latency budget.
    """
    cabinet = _cabinet(client, slots=44, name="Big cabinet")
    state = _start(client, cabinet["id"])
    session_id = state["session"]["id"]
    assert state["progress"]["total_slots"] == 44

    seen: list[str] = []
    for n in range(1, 45):
        body = _bind(client, session_id, _uid(n))
        assert body["status"] == "bound"
        seen.append(body["tag"]["label_path"].rsplit(" / ", 1)[-1])
        cursor = body["state"]["cursor"]
        if n < 44:
            # Every response hands over the next drawer with no extra request.
            assert cursor is not None
        else:
            assert cursor is None

    final = client.get(f"/api/locations/{cabinet['id']}/provisioning-sessions/current").json()
    assert final["session"] is None  # completed, so no walk is current
    layout = client.get(f"/api/locations/{cabinet['id']}/layout").json()
    # Reading order, the same order a person walks the drawers: the walk visited
    # the slots in exactly `sort_order`, which is what the label sheet uses too.
    assert seen == [slot["slot_label"] for slot in layout["slots"]]
    assert all(slot["has_tag"] for slot in layout["slots"])
    assert len({slot["short_id"] for slot in layout["slots"]}) == 44


def test_a_completed_walk_refuses_further_binds(client: TestClient) -> None:
    cabinet = _cabinet(client, slots=1)
    session_id = _start(client, cabinet["id"])["session"]["id"]
    _bind(client, session_id, _uid(1))

    response = client.post(
        f"/api/provisioning-sessions/{session_id}/bind", json={"tag_uid": _uid(2)}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "session_completed"


# ---------------------------------------------------------------------------
# Idempotency and session-kind confusion
# ---------------------------------------------------------------------------


def test_a_retried_bind_replays_instead_of_double_binding(client: TestClient) -> None:
    """A phone on flaky wifi retries a request whose response was lost. Without
    this the retry would land on the *next* drawer, silently offsetting the rest
    of the cabinet by one."""
    cabinet = _cabinet(client, slots=3)
    session_id = _start(client, cabinet["id"])["session"]["id"]

    body = {"tag_uid": _uid(1), "client_op_id": "bind-retry-1"}
    first = client.post(f"/api/provisioning-sessions/{session_id}/bind", json=body).json()
    second = client.post(f"/api/provisioning-sessions/{session_id}/bind", json=body).json()

    assert second["replayed"] is True
    assert second["tag"]["location_id"] == first["tag"]["location_id"]
    assert _start(client, cabinet["id"])["progress"]["bound"] == 1


def test_a_verification_session_cannot_be_bound_through(client: TestClient) -> None:
    """Two kinds with opposite postconditions share a table: provisioning
    creates bindings, verification asserts the existing ones are right and
    creates none."""
    cabinet = _cabinet(client, slots=2)
    verify = client.post(f"/api/locations/{cabinet['id']}/verification-sessions", json={})
    session_id = verify.json()["state"]["session"]["id"]

    response = client.post(
        f"/api/provisioning-sessions/{session_id}/bind", json={"tag_uid": _uid(1)}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "wrong_session_kind"
