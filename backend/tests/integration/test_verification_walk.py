"""The verification walk, and the tag-resolution routes it leans on.

**This walk is not optional busywork.** No software can stop a person sticking a
tag on the wrong drawer; it can only detect it — and a mis-bound tag is invisible
until it causes a wrong put-away, at which point the stock is somewhere the
system does not think it is. The two properties under test are therefore:

* a mismatch is *named* — expected UID, scanned UID, and the reverse lookup of
  which slot the scanned tag actually belongs to ("this tag belongs to B1");
* a mismatch **changes nothing**. `test_verification_never_mutates_a_binding`
  is the load-bearing one: the two plausible repairs (rebind here, or swap the
  two drawers) have different physical consequences, and only the person holding
  the drawers knows which happened.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _uid(n: int) -> str:
    return f"04{n:012X}"


def _cabinet(client: TestClient, *, slots: int, name: str = "Verify cabinet") -> dict:
    room = client.post("/api/locations", json={"name": f"{name} room"}).json()["location"]
    container_type = client.post(
        "/api/container-types",
        json={
            "slug": f"{name.lower().replace(' ', '-')}-type",
            "display_name": name,
            "child_layout": "grid",
            "grid_rows": slots,
            "grid_cols": 1,
        },
    ).json()["container_type"]
    created = client.post(
        f"/api/locations/{room['id']}/instantiate",
        json={"container_type_id": container_type["id"], "count": 1, "naming_pattern": name},
    )
    assert created.status_code == 201, created.text
    return created.json()["locations"][0]


def _provisioned(client: TestClient, *, slots: int, name: str = "Verify cabinet") -> dict:
    """A cabinet with every drawer bound, keyed by slot label.

    Bound through the real walk rather than by inserting rows, so the fixture
    cannot disagree with what provisioning actually writes.
    """
    cabinet = _cabinet(client, slots=slots, name=name)
    session_id = client.post(
        f"/api/locations/{cabinet['id']}/provisioning-sessions",
        json={"device_kind": "phone_webnfc"},
    ).json()["state"]["session"]["id"]

    tags: dict[str, dict] = {}
    for n in range(1, slots + 1):
        body = client.post(
            f"/api/provisioning-sessions/{session_id}/bind", json={"tag_uid": _uid(n)}
        ).json()
        label = body["tag"]["label_path"].rsplit(" / ", 1)[-1]
        tags[label] = body["tag"]
    return {"cabinet": cabinet, "tags": tags}


def _start_verify(client: TestClient, cabinet_id: int) -> dict:
    response = client.post(
        f"/api/locations/{cabinet_id}/verification-sessions", json={"device_kind": "phone_webnfc"}
    )
    assert response.status_code == 201, response.text
    return response.json()["state"]


def _check(client: TestClient, session_id: int, uid: str, **body: object) -> dict:
    response = client.post(
        f"/api/verification-sessions/{session_id}/check", json={"tag_uid": uid, **body}
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# A clean walk
# ---------------------------------------------------------------------------


def test_the_verify_cursor_covers_only_tagged_slots(client: TestClient) -> None:
    """It re-reads *every tag*, so an untagged drawer is not part of this walk —
    there is nothing to compare it against."""
    fixture = _provisioned(client, slots=3, name="Partly tagged")
    cabinet = fixture["cabinet"]
    # Unbind one drawer, so it stops being part of the verification walk.
    client.post(f"/api/location-tags/{fixture['tags']['A1']['id']}/unbind", json={})

    state = _start_verify(client, cabinet["id"])
    assert state["cursor"]["slot_label"] == "B1"
    assert state["progress"] == {
        "total_tagged": 2,
        "checked": 0,
        "remaining": 2,
        "mismatches": 0,
    }


def test_a_matching_tag_ticks_and_advances(client: TestClient) -> None:
    fixture = _provisioned(client, slots=3)
    session_id = _start_verify(client, fixture["cabinet"]["id"])["session"]["id"]

    first = _check(client, session_id, _uid(1))
    assert first["status"] == "match"
    assert first["expected_tag_uid"] == _uid(1)
    assert first["state"]["cursor"]["slot_label"] == "B1"
    assert first["state"]["stopped"] is False
    assert first["state"]["progress"]["checked"] == 1


def test_a_clean_walk_completes(client: TestClient) -> None:
    fixture = _provisioned(client, slots=3)
    session_id = _start_verify(client, fixture["cabinet"]["id"])["session"]["id"]
    for n in range(1, 4):
        last = _check(client, session_id, _uid(n))

    assert last["state"]["cursor"] is None
    assert last["state"]["session"]["completed_at"] is not None
    assert last["state"]["progress"]["remaining"] == 0
    assert last["state"]["mismatches"] == []

    # The reading is recorded on the tag, which is not the same as changing what
    # the tag means.
    tag = client.post("/api/location-tags/resolve", json={"tag_uid": _uid(1)}).json()["tag"]
    assert tag["last_verified_at"] is not None


# ---------------------------------------------------------------------------
# Two swapped tags
# ---------------------------------------------------------------------------


def test_the_walk_catches_two_swapped_tags_and_names_both_slots(client: TestClient) -> None:
    """B1 and C1 have physically had their tags exchanged.

    This is the failure the walk exists for, and the useful output is not "B1 is
    wrong" — it is "the tag on B1 belongs to C1". A cabinet is fixed from that
    sentence and cannot be fixed from the other one.
    """
    fixture = _provisioned(client, slots=4, name="Swapped cabinet")
    slots = {
        slot["slot_label"]: slot
        for slot in client.get(f"/api/locations/{fixture['cabinet']['id']}/layout").json()["slots"]
    }
    session_id = _start_verify(client, fixture["cabinet"]["id"])["session"]["id"]

    assert _check(client, session_id, _uid(1))["status"] == "match"  # A1 is fine

    # B1 physically carries C1's tag.
    first = _check(client, session_id, _uid(3))
    assert first["status"] == "mismatch"
    assert first["location_id"] == slots["B1"]["location_id"]
    assert first["expected_tag_uid"] == _uid(2)
    assert first["scanned_tag_uid"] == _uid(3)
    assert first["mismatch"]["scanned_resolved_location_id"] == slots["C1"]["location_id"]
    assert first["mismatch"]["scanned_resolved_slot_label"] == "C1"

    # The walk has stopped: the cursor did *not* advance past the drawer nobody
    # has explained yet.
    assert first["state"]["stopped"] is True
    assert first["state"]["cursor"]["slot_label"] == "B1"

    # The human looks at the next drawer anyway, which is a human decision and
    # not the software deciding the first mismatch was fine.
    second = _check(client, session_id, _uid(2), location_id=slots["C1"]["location_id"])
    assert second["status"] == "mismatch"
    assert second["mismatch"]["scanned_resolved_slot_label"] == "B1"

    report = client.get(f"/api/verification-sessions/{session_id}").json()
    assert [(m["slot_label"], m["scanned_resolved_slot_label"]) for m in report["mismatches"]] == [
        ("B1", "C1"),
        ("C1", "B1"),
    ]
    assert report["progress"]["mismatches"] == 2
    assert report["stopped"] is True


def test_verification_never_mutates_a_binding(client: TestClient) -> None:
    """**Do not auto-fix.** The two plausible repairs — rebind this tag here, or
    swap it with the drawer it actually belongs to — have different physical
    consequences, and only the person holding the drawers can tell which
    happened. So a mismatch records and stops.
    """
    fixture = _provisioned(client, slots=3, name="Untouched cabinet")
    before = {
        label: {
            "id": tag["id"],
            "location_id": tag["location_id"],
            "tag_uid": tag["tag_uid"],
            "ndef_url": tag["ndef_url"],
        }
        for label, tag in fixture["tags"].items()
    }
    slots = {
        slot["slot_label"]: slot
        for slot in client.get(f"/api/locations/{fixture['cabinet']['id']}/layout").json()["slots"]
    }
    session_id = _start_verify(client, fixture["cabinet"]["id"])["session"]["id"]

    _check(client, session_id, _uid(3))  # A1 read as C1's tag
    _check(client, session_id, _uid(1), location_id=slots["C1"]["location_id"])

    after = {}
    for label, snapshot in before.items():
        tag = client.post(
            "/api/location-tags/resolve", json={"tag_uid": snapshot["tag_uid"]}
        ).json()["tag"]
        after[label] = {
            "id": tag["id"],
            "location_id": tag["location_id"],
            "tag_uid": tag["tag_uid"],
            "ndef_url": tag["ndef_url"],
        }
    assert after == before


def test_a_re_check_that_now_matches_closes_the_finding(client: TestClient) -> None:
    """Whichever repair the human chose, the drawer reading correctly is what
    resolves the finding — the walk does not need to know which one it was."""
    fixture = _provisioned(client, slots=2, name="Repaired cabinet")
    slots = {
        slot["slot_label"]: slot
        for slot in client.get(f"/api/locations/{fixture['cabinet']['id']}/layout").json()["slots"]
    }
    session_id = _start_verify(client, fixture["cabinet"]["id"])["session"]["id"]

    stopped = _check(client, session_id, _uid(2))  # A1 read as B1's tag
    assert stopped["state"]["stopped"] is True

    # The human repairs it by hand: unbind A1's stale binding and rebind the tag
    # that is physically on it.
    client.post(f"/api/location-tags/{fixture['tags']['A1']['id']}/unbind", json={})
    client.post(f"/api/location-tags/{fixture['tags']['B1']['id']}/unbind", json={})
    provision_id = client.post(
        f"/api/locations/{fixture['cabinet']['id']}/provisioning-sessions", json={}
    ).json()["state"]["session"]["id"]
    client.post(
        f"/api/provisioning-sessions/{provision_id}/bind",
        json={"tag_uid": _uid(2), "location_id": slots["A1"]["location_id"]},
    )
    client.post(
        f"/api/provisioning-sessions/{provision_id}/bind",
        json={"tag_uid": _uid(1), "location_id": slots["B1"]["location_id"]},
    )

    resumed = _check(client, session_id, _uid(2))
    assert resumed["status"] == "match"
    assert resumed["state"]["stopped"] is False
    assert resumed["state"]["mismatches"][0]["resolved_at"] is not None


def test_an_unknown_tag_is_a_mismatch_that_belongs_nowhere(client: TestClient) -> None:
    """A tag from a different cabinet, or a blank one stuck on by mistake. The
    reverse lookup honestly reports "nowhere" rather than inventing a slot."""
    fixture = _provisioned(client, slots=2, name="Stranger cabinet")
    session_id = _start_verify(client, fixture["cabinet"]["id"])["session"]["id"]

    body = _check(client, session_id, _uid(999))
    assert body["status"] == "mismatch"
    assert body["mismatch"]["scanned_resolved_location_id"] is None
    assert body["mismatch"]["scanned_resolved_slot_label"] is None


def test_a_provisioning_session_cannot_be_checked_through(client: TestClient) -> None:
    fixture = _provisioned(client, slots=2, name="Kind confusion")
    provision_id = client.post(
        f"/api/locations/{fixture['cabinet']['id']}/provisioning-sessions", json={}
    ).json()["state"]["session"]["id"]

    response = client.post(
        f"/api/verification-sessions/{provision_id}/check", json={"tag_uid": _uid(1)}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "wrong_session_kind"


# ---------------------------------------------------------------------------
# Resolving a tag: NDEF first, UID fallback
# ---------------------------------------------------------------------------


def test_a_tag_resolves_by_its_ndef_payload(client: TestClient) -> None:
    fixture = _provisioned(client, slots=2, name="Resolvable cabinet")
    tag = fixture["tags"]["A1"]

    body = client.post("/api/location-tags/resolve", json={"ndef_url": tag["ndef_url"]}).json()
    assert body["status"] == "resolved"
    assert body["matched_by"] == "ndef"
    assert body["location"]["location_id"] == tag["location_id"]
    assert body["location"]["label_path"].endswith("A1")
    assert body["disagreement"] is False


def test_a_tag_resolves_by_uid_when_the_ndef_record_is_unreadable(client: TestClient) -> None:
    """The UID lives in factory-locked pages, physically separate from NDEF user
    memory, so the worst case of a failed write is a UID-only tag — which must
    still resolve."""
    fixture = _provisioned(client, slots=2, name="Uid fallback cabinet")
    tag = fixture["tags"]["B1"]

    body = client.post("/api/location-tags/resolve", json={"tag_uid": tag["tag_uid"]}).json()
    assert body["matched_by"] == "uid"
    assert body["location"]["location_id"] == tag["location_id"]


def test_the_payload_host_is_not_part_of_the_match(client: TestClient) -> None:
    """A tag written before a hostname change is still perfectly correct: the
    meaning of the payload was never the host, and rewriting every tag because
    the base URL moved is the failure mode `ALMAGEST_BASE_URL` warns about."""
    fixture = _provisioned(client, slots=2, name="Rehosted cabinet")
    short_id = fixture["tags"]["A1"]["ndef_url"].rsplit("/s/", 1)[-1]

    body = client.post(
        "/api/location-tags/resolve", json={"ndef_url": f"https://moved.example/s/{short_id}"}
    ).json()
    assert body["matched_by"] == "ndef"
    assert body["location"]["location_id"] == fixture["tags"]["A1"]["location_id"]


def test_a_payload_and_a_uid_that_disagree_are_both_reported(client: TestClient) -> None:
    """A tag written for one slot and bound to another. Preferring either answer
    silently would hide the exact condition the verification walk looks for."""
    fixture = _provisioned(client, slots=2, name="Disagreeing cabinet")

    body = client.post(
        "/api/location-tags/resolve",
        json={
            "ndef_url": fixture["tags"]["A1"]["ndef_url"],
            "tag_uid": fixture["tags"]["B1"]["tag_uid"],
        },
    ).json()
    assert body["disagreement"] is True
    # NDEF wins, because it is the payload this system authored.
    assert body["matched_by"] == "ndef"
    assert body["location"]["location_id"] == fixture["tags"]["A1"]["location_id"]


def test_a_blank_tag_is_unknown_rather_than_an_error(client: TestClient) -> None:
    """The normal state of a tag before provisioning. The UI's answer is
    "provision this container now", not a dead end."""
    body = client.post("/api/location-tags/resolve", json={"tag_uid": _uid(4242)}).json()
    assert body["status"] == "unknown"
    assert body["matched_by"] == "none"
    assert body["location"] is None


def test_resolve_needs_at_least_one_carrier(client: TestClient) -> None:
    assert client.post("/api/location-tags/resolve", json={}).status_code == 422


# ---------------------------------------------------------------------------
# Unbinding
# ---------------------------------------------------------------------------


def test_unbinding_frees_the_slot_but_leaves_the_short_id_resolvable(client: TestClient) -> None:
    """A tag is a foreign key, not a record: dropping the binding cannot and
    need not touch the tag, whose payload is an opaque short id."""
    fixture = _provisioned(client, slots=2, name="Unbind cabinet")
    tag = fixture["tags"]["A1"]
    short_id = tag["ndef_url"].rsplit("/s/", 1)[-1]

    response = client.post(f"/api/location-tags/{tag['id']}/unbind", json={})
    assert response.status_code == 200, response.text
    assert response.json()["unbound"]["tag_uid"] == tag["tag_uid"]

    # The slot is back at the front of the next walk's cursor...
    current = client.get(
        f"/api/locations/{fixture['cabinet']['id']}/provisioning-sessions/current"
    ).json()
    assert current["cursor"]["slot_label"] == "A1"
    # ...and the printed/NDEF payload still resolves to the same drawer.
    assert (
        client.get(f"/api/resolve/{short_id}").json()["target"]["entity_pk"] == tag["location_id"]
    )
    assert (
        client.post("/api/location-tags/resolve", json={"tag_uid": tag["tag_uid"]}).json()["status"]
        == "unknown"
    )


def test_unbinding_an_unknown_tag_is_404(client: TestClient) -> None:
    assert client.post("/api/location-tags/999999/unbind", json={}).status_code == 404
