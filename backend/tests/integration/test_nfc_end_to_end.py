"""The whole tag path, simulated end to end against real migrations.

There is no NFC reader on this setup and none on order, so every claim about tag
handling is otherwise untested prose. This walks the entire life of a tag through
the real API — provision, write, verify, resolve, use at a bench — with the reader
simulated at exactly one place: what bytes a tap produces. Everything downstream of
that is the production code path.

The chain, in the order a real cabinet goes through it:

1. **Provision** every drawer, one request per drawer, cursor derived each time.
2. **Report the write result**, which is the half the server cannot observe — one
   tag verifies, one comes back with nothing readable and is recorded as degraded
   rather than silently assumed good.
3. **Resolve** both, proving NDEF-first with a UID fallback: the degraded tag still
   identifies its drawer by UID, which is precisely what makes a failed write a
   rewrite rather than a loss.
4. **Verify**, with two tags deliberately swapped, and check both are caught and
   each names the drawer its tag actually belongs to.
5. **Confirm at the bench** — "is the drawer in my hand the one I was sent for?" —
   which is the daily use and the one that has to answer *wrong* usefully.

What is deliberately *not* asserted here: that Web NFC works, or that a PN532
works. Those are platform and hardware claims that no test on this machine can
make, and pretending otherwise is worse than the gap.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _uid(n: int) -> str:
    """A 7-byte UID as the 14 hex digits a reader hands over. `04` is NXP's real
    manufacturer byte, so these look like tags rather than like test data."""
    return f"04{n:012X}"


def _cabinet(client: TestClient, *, slots: int, name: str = "Cabinet") -> dict:
    """A cabinet whose drawers are generated grid cells, built through
    `instantiate` so `sort_order` is the real reading order the cursor derives
    from."""
    room = client.post("/api/locations", json={"name": f"{name} room"}).json()["location"]
    container_type = client.post(
        "/api/container-types",
        json={
            "slug": f"{name.lower().replace(' ', '-')}-type",
            "display_name": name,
            "child_layout": "grid",
            "grid_rows": slots,
            "grid_cols": 1,
            "slot_label_scheme": "row_alpha_col_num",
        },
    ).json()["container_type"]
    created = client.post(
        f"/api/locations/{room['id']}/instantiate",
        json={"container_type_id": container_type["id"], "count": 1, "naming_pattern": name},
    )
    assert created.status_code == 201, created.text
    return created.json()["locations"][0]


def _provision_all(client: TestClient, cabinet_id: int, count: int) -> list[dict]:
    """Bind `count` drawers in cursor order, returning each bind's tag.

    One request per drawer and no read in between — the response has to carry the
    advanced cursor, or a 44-drawer cabinet costs 88 round trips and stops being
    walking-paced.
    """
    started = client.post(
        f"/api/locations/{cabinet_id}/provisioning-sessions",
        json={"device_kind": "phone_webnfc"},
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["state"]["session"]["id"]

    tags: list[dict] = []
    for index in range(count):
        response = client.post(
            f"/api/provisioning-sessions/{session_id}/bind",
            json={"tag_uid": _uid(index + 1)},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "bound", body
        tags.append(body["tag"])
    return tags


def test_a_tag_is_unverified_until_a_device_says_otherwise(client: TestClient) -> None:
    """Binding is not writing, and the record must not claim it was.

    `written_at` is stamped when the binding row is created, which is necessarily
    before any device has touched the sticker. If that were the only field, a
    binding whose write silently failed would be indistinguishable from one that
    worked.
    """
    cabinet = _cabinet(client, slots=2)
    tags = _provision_all(client, cabinet["id"], 1)

    assert tags[0]["ndef_state"] == "unverified"
    assert tags[0]["ndef_checked_at"] is None
    # And it is stamped, which is exactly the field that would be lying without
    # `ndef_state` beside it.
    assert tags[0]["written_at"] is not None


def test_a_read_back_that_matches_verifies_and_one_that_does_not_degrades(
    client: TestClient,
) -> None:
    cabinet = _cabinet(client, slots=3)
    tags = _provision_all(client, cabinet["id"], 2)
    good, bad = tags[0], tags[1]

    verified = client.post(
        f"/api/location-tags/{good['id']}/write-result",
        json={"read_back_url": good["ndef_url"]},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["verified"] is True
    assert verified.json()["tag"]["ndef_state"] == "verified"

    # The write threw, or user memory came back empty. Same fact about the tag.
    degraded = client.post(
        f"/api/location-tags/{bad['id']}/write-result",
        json={"read_back_url": None},
    )
    assert degraded.status_code == 200, degraded.text
    assert degraded.json()["verified"] is False
    assert degraded.json()["tag"]["ndef_state"] == "degraded"


def test_a_hostname_change_does_not_turn_working_tags_into_rewrites(
    client: TestClient,
) -> None:
    """The read-back is compared by short id, not by string.

    A tag written before the host changed carries a different URL and is still
    perfectly correct. Comparing strings would mark every one of three hundred
    working stickers as needing a rewrite, which is the kind of false alarm that
    makes people stop believing the verify screen.
    """
    cabinet = _cabinet(client, slots=2)
    tag = _provision_all(client, cabinet["id"], 1)[0]
    short_id = tag["ndef_url"].rsplit("/", 1)[-1]

    response = client.post(
        f"/api/location-tags/{tag['id']}/write-result",
        json={"read_back_url": f"https://almagest.example.invalid/s/{short_id}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["verified"] is True


def test_a_degraded_tag_still_identifies_its_drawer_by_uid(client: TestClient) -> None:
    """The whole reason a failed write is not a lost tag.

    The UID lives in factory-locked pages 0-2, physically separate from user
    memory at page 4, so a write that fails partway leaves the tag perfectly
    identifiable — just not tappable with a phone, which needs a URI to open.
    """
    cabinet = _cabinet(client, slots=2)
    tag = _provision_all(client, cabinet["id"], 1)[0]
    client.post(f"/api/location-tags/{tag['id']}/write-result", json={"read_back_url": None})

    # What a reader sees on a degraded tag: a UID, and nothing in user memory.
    resolved = client.post("/api/location-tags/resolve", json={"tag_uid": _uid(1)})
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["status"] == "resolved"
    assert body["matched_by"] == "uid"
    assert body["location"]["location_id"] == tag["location_id"]
    assert body["tag"]["ndef_state"] == "degraded"


def test_ndef_wins_over_uid_when_both_are_present(client: TestClient) -> None:
    cabinet = _cabinet(client, slots=2)
    tag = _provision_all(client, cabinet["id"], 1)[0]

    resolved = client.post(
        "/api/location-tags/resolve",
        json={"tag_uid": _uid(1), "ndef_url": tag["ndef_url"]},
    )
    assert resolved.json()["matched_by"] == "ndef"
    assert resolved.json()["disagreement"] is False


def test_the_verification_walk_catches_two_swapped_tags_and_names_each_one(
    client: TestClient,
) -> None:
    """PLAN.md's own acceptance test: deliberately mis-bind two, confirm both are
    caught and that each names the slot its tag actually belongs to.

    "Something is wrong" is not actionable. "That tag belongs to A2" is, and it is
    the difference between a verification walk and an alarm.
    """
    cabinet = _cabinet(client, slots=4)
    tags = _provision_all(client, cabinet["id"], 4)
    slots = [tag["location_id"] for tag in tags]

    started = client.post(
        f"/api/locations/{cabinet['id']}/verification-sessions",
        json={"device_kind": "phone_webnfc"},
    )
    assert started.status_code == 201, started.text
    walk = started.json()["state"]["session"]["id"]

    # Drawer 1 and 2 read correctly.
    for index in (0, 1):
        response = client.post(
            f"/api/verification-sessions/{walk}/check",
            json={"tag_uid": _uid(index + 1), "location_id": slots[index]},
        )
        assert response.json()["status"] == "match", response.text

    # Drawers 3 and 4 have had their stickers swapped.
    third = client.post(
        f"/api/verification-sessions/{walk}/check",
        json={"tag_uid": _uid(4), "location_id": slots[2]},
    ).json()
    fourth = client.post(
        f"/api/verification-sessions/{walk}/check",
        json={"tag_uid": _uid(3), "location_id": slots[3]},
    ).json()

    assert third["status"] == "mismatch"
    assert fourth["status"] == "mismatch"
    # Each names where the tag really belongs — the reverse lookup is the payload.
    assert third["mismatch"]["scanned_resolved_location_id"] == slots[3]
    assert fourth["mismatch"]["scanned_resolved_location_id"] == slots[2]

    # And nothing was repaired: choosing between "rebind" and "swap the drawers"
    # is a physical claim only the person holding them can make.
    for index, slot in enumerate(slots):
        resolved = client.post("/api/location-tags/resolve", json={"tag_uid": _uid(index + 1)})
        assert resolved.json()["location"]["location_id"] == slot

    state = client.get(f"/api/verification-sessions/{walk}").json()
    assert state["progress"]["mismatches"] == 2
    assert state["stopped"] is True


def test_the_verification_walk_flags_a_right_tag_whose_url_is_gone(
    client: TestClient,
) -> None:
    """Right sticker, dead payload — a `match` that still needs a rewrite.

    Two independent facts, and collapsing them either way is wrong: calling it a
    mismatch sends someone hunting a swap that never happened, and calling it a
    clean match leaves a drawer that no phone tap will ever open.
    """
    cabinet = _cabinet(client, slots=2)
    tag = _provision_all(client, cabinet["id"], 1)[0]
    client.post(
        f"/api/location-tags/{tag['id']}/write-result",
        json={"read_back_url": tag["ndef_url"]},
    )

    walk = client.post(
        f"/api/locations/{cabinet['id']}/verification-sessions",
        json={"device_kind": "phone_webnfc"},
    ).json()["state"]["session"]["id"]

    # The reader looked at user memory and found nothing there.
    response = client.post(
        f"/api/verification-sessions/{walk}/check",
        json={
            "tag_uid": _uid(1),
            "location_id": tag["location_id"],
            "ndef_url": None,
            "carries_ndef": True,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "match"
    assert response.json()["ndef_state"] == "degraded"


def test_a_typed_uid_can_never_mark_a_working_tag_degraded(client: TestClient) -> None:
    """`carries_ndef` is what keeps a keyboard from lying about a sticker.

    A person reading a UID off a tag has said nothing whatsoever about what is in
    its user memory. Without this flag, the honest absence of a URL from a manual
    entry is indistinguishable from a reader finding user memory unreadable, and
    every hand-verified drawer would be marked for a rewrite it does not need.
    """
    cabinet = _cabinet(client, slots=2)
    tag = _provision_all(client, cabinet["id"], 1)[0]
    client.post(
        f"/api/location-tags/{tag['id']}/write-result",
        json={"read_back_url": tag["ndef_url"]},
    )

    walk = client.post(
        f"/api/locations/{cabinet['id']}/verification-sessions",
        json={"device_kind": "manual"},
    ).json()["state"]["session"]["id"]

    response = client.post(
        f"/api/verification-sessions/{walk}/check",
        json={"tag_uid": _uid(1), "location_id": tag["location_id"], "carries_ndef": False},
    )
    assert response.json()["status"] == "match"
    # Untouched: still whatever the last device that actually looked reported.
    assert response.json()["ndef_state"] == "verified"


def test_confirming_a_container_at_the_bench_answers_wrong_usefully(
    client: TestClient,
) -> None:
    """The daily loop: "is the drawer in my hand the one I was sent for?"

    A pick from the drawer *next to* the right one is the error no amount of care
    prevents and no ledger row records, so the answer to a wrong scan has to name
    what was actually scanned. This is the server half of `ConfirmScan`.
    """
    cabinet = _cabinet(client, slots=3)
    tags = _provision_all(client, cabinet["id"], 2)
    wanted, other = tags[0], tags[1]

    right = client.post("/api/location-tags/resolve", json={"tag_uid": _uid(1)}).json()
    assert right["location"]["location_id"] == wanted["location_id"]

    wrong = client.post("/api/location-tags/resolve", json={"tag_uid": _uid(2)}).json()
    assert wrong["location"]["location_id"] == other["location_id"]
    # The label path is what the screen shows; it is derived, never read off the
    # tag, because a container that moves would make an encoded path a lie.
    assert wrong["location"]["label_path"].startswith("Cabinet room")

    # An unprovisioned drawer's tag is unknown, and that is a state rather than an
    # error: the answer on screen is "bind it", not a dead end.
    unknown = client.post("/api/location-tags/resolve", json={"tag_uid": _uid(99)}).json()
    assert unknown["status"] == "unknown"
    assert unknown["location"] is None


def test_a_disagreeing_tag_is_reported_rather_than_resolved(client: TestClient) -> None:
    """Payload says one drawer, UID is bound to another: a mis-bound tag.

    Silently preferring either carrier would hide the exact condition the
    verification walk exists to find.
    """
    cabinet = _cabinet(client, slots=3)
    tags = _provision_all(client, cabinet["id"], 2)

    resolved = client.post(
        "/api/location-tags/resolve",
        json={"tag_uid": _uid(2), "ndef_url": tags[0]["ndef_url"]},
    ).json()
    assert resolved["disagreement"] is True


def test_the_handoff_qr_encodes_a_path_and_refuses_anything_that_leaves_the_origin(
    client: TestClient,
) -> None:
    """Handing a walk to a phone is a URL, so the URL had better be ours.

    Encoding a caller-supplied absolute URL would make this an open redirect with
    a QR code on the front — and the phone scanning it has no way to tell a
    handoff from a hostile link.
    """
    ok = client.get("/api/handoff/qr.svg", params={"path": "/builds/12?tab=pick"})
    assert ok.status_code == 200, ok.text
    assert ok.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in ok.content

    for hostile in ("https://evil.example/x", "//evil.example/x", "builds/12"):
        refused = client.get("/api/handoff/qr.svg", params={"path": hostile})
        assert refused.status_code == 422, (hostile, refused.text)
        assert refused.json()["detail"]["reason"] == "unsafe_handoff_path"
