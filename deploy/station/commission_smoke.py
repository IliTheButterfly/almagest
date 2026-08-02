"""Drive a full commissioning walk against a running station, over HTTP only.

Deliberately no imports from `app`: this exercises the station the way the PWA
does — through the one origin the kiosk talks to — so anything it catches is a
thing a person at the bench would hit.

Covers the provisioning walk and the verification walk PLAN.md describes at line
572, including the case that whole feature exists for: a tag stuck on the wrong
drawer, which must be *detected and named*, never auto-fixed.

The four conflict shapes are the interesting part, because they are four
different sentences to a person holding a tag:

  already_bound_here       the same tag twice — nothing to do, no undo step
  already_bound_elsewhere  this tag means another drawer      -> Move here?
  slot_already_bound       this drawer already has a tag      -> Move here?
  two_conflicts            both at once — refused outright, 409, because
                           resolving it is two displacements and there is one
                           undo slot
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"

failures: list[str] = []


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode("utf-8", "replace")}


def check(label: str, ok: bool, detail: object = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def cursor_of(state: dict) -> int | None:
    cur = state.get("cursor")
    return cur.get("location_id") if cur else None


# A tag UID is hex, and the API enforces it — a non-hex character here is a 422
# about the fixture rather than a finding about the app.
def uid(n: int) -> str:
    return f"04{n:02X}AABBCCDDEE"


print("== finding a subtree to commission")
status, tree = call("GET", "/api/locations/tree")
if status != 200:
    print(f"cannot read the tree: {status} {tree}")
    raise SystemExit(1)
by_parent: dict[int, list[dict]] = {}
for node in tree["nodes"]:
    if node.get("parent_id") is not None:
        by_parent.setdefault(node["parent_id"], []).append(node)
root_id, children = max(by_parent.items(), key=lambda kv: len(kv[1]))
print(f"   root={root_id} with {len(children)} children")

print("== a walk starts on the first unbound slot")
status, started = call(
    "POST",
    f"/api/locations/{root_id}/provisioning-sessions",
    {"device_kind": "flipper_rpc", "client_op_id": "e2e-start", "device_id": "e2e"},
)
check("a walk starts", status == 201, status)
if status != 201:
    raise SystemExit(1)
state = started["state"]
sid = state["session"]["id"]
check("the cursor starts on a slot", cursor_of(state) is not None, cursor_of(state))

print("== walking the cabinet, one tag per drawer")
bound: list[tuple[int, str]] = []
for n in range(3):
    here = cursor_of(state)
    if here is None:
        break
    tag = uid(n)
    status, resp = call(
        "POST",
        f"/api/provisioning-sessions/{sid}/bind",
        {"tag_uid": tag, "client_op_id": f"e2e-bind-{n}", "device_id": "e2e"},
    )
    ok = status == 200 and resp.get("status") == "bound"
    check(f"drawer {here} takes a tag", ok, resp.get("status"))
    if ok:
        bound.append((here, tag))
        state = resp["state"]
        check(f"the cursor moved off {here}", cursor_of(state) != here, cursor_of(state))

if len(bound) < 2:
    print("not enough slots bound to exercise the conflicts")
    raise SystemExit(1)

slot_a, tag_a = bound[0]
slot_b, tag_b = bound[1]
free_slot = cursor_of(state)

print("== the same tag tapped twice costs nothing")
status, resp = call(
    "POST",
    f"/api/provisioning-sessions/{sid}/bind",
    {"tag_uid": tag_a, "location_id": slot_a, "client_op_id": "e2e-dup", "device_id": "e2e"},
)
check("a re-tap is already_bound_here", resp.get("status") == "already_bound_here", resp.get("status"))
check("and adds no undo step", resp["state"]["undo_depth"] == started["state"]["undo_depth"] + 3, resp["state"]["undo_depth"])

print("== this tag already means another drawer")
status, resp = call(
    "POST",
    f"/api/provisioning-sessions/{sid}/bind",
    {"tag_uid": tag_a, "location_id": free_slot, "client_op_id": "e2e-elsewhere", "device_id": "e2e"},
)
check("refused as already_bound_elsewhere", resp.get("status") == "already_bound_elsewhere", resp.get("status"))
conflict = resp.get("conflict") or {}
check("and names the drawer it means", conflict.get("location_id") == slot_a, conflict.get("label_path"))

print("== this drawer already has a tag")
fresh = uid(90)
status, resp = call(
    "POST",
    f"/api/provisioning-sessions/{sid}/bind",
    {"tag_uid": fresh, "location_id": slot_a, "client_op_id": "e2e-slotbound", "device_id": "e2e"},
)
check("refused as slot_already_bound", resp.get("status") == "slot_already_bound", resp.get("status"))
conflict = resp.get("conflict") or {}
check("and names the tag in the way", conflict.get("tag_uid") == tag_a, conflict.get("tag_uid"))

print("== both at once is refused outright, not half-done")
status, resp = call(
    "POST",
    f"/api/provisioning-sessions/{sid}/bind",
    {"tag_uid": tag_b, "location_id": slot_a, "client_op_id": "e2e-two", "device_id": "e2e"},
)
check("two conflicts is a 409", status == 409, status)
check(
    "and says which rule refused it",
    (resp.get("detail") or {}).get("reason") == "two_conflicts",
    (resp.get("detail") or {}).get("reason"),
)

print("== 'Move here' is what resolves a single conflict")
status, resp = call(
    "POST",
    f"/api/provisioning-sessions/{sid}/bind",
    {
        "tag_uid": tag_a,
        "location_id": free_slot,
        "move": True,
        "client_op_id": "e2e-move",
        "device_id": "e2e",
    },
)
check("a confirmed move is accepted", status == 200 and resp.get("status") in {"moved", "rebound"}, resp.get("status"))
moved_state = resp.get("state", {})

print("== and the move is undoable, because a person can be wrong")
status, resp = call(
    "POST",
    f"/api/provisioning-sessions/{sid}/undo",
    {"client_op_id": "e2e-undo-move", "device_id": "e2e"},
)
check("the move can be undone", status == 200, status)
undone = resp.get("undone") or {}
check("the undo names what it put back", undone.get("action_kind") in {"move", "rebind"}, undone.get("action_kind"))

print("== the verification walk: the same cabinet, re-read")
status, vstarted = call(
    "POST",
    f"/api/locations/{root_id}/verification-sessions",
    {"device_kind": "flipper_rpc", "client_op_id": "e2e-vstart", "device_id": "e2e"},
)
check("a verification walk starts", status == 201, status)
vsid = vstarted["state"]["session"]["id"]

status, resp = call(
    "POST",
    f"/api/verification-sessions/{vsid}/check",
    {
        "tag_uid": tag_a,
        "location_id": slot_a,
        "ndef_url": "https://almagest.lan/s/4K7T92M8",
        "carries_ndef": True,
        "client_op_id": "e2e-vok",
        "device_id": "e2e",
    },
)
check("the right tag on the right drawer matches", resp.get("status") == "match", resp.get("status"))

print("== a tag on the wrong drawer — the reason this walk exists")
status, resp = call(
    "POST",
    f"/api/verification-sessions/{vsid}/check",
    {
        "tag_uid": tag_b,
        "location_id": slot_a,
        "carries_ndef": True,
        "client_op_id": "e2e-vbad",
        "device_id": "e2e",
    },
)
check("a swapped tag is a mismatch", resp.get("status") == "mismatch", resp.get("status"))
mismatch = resp.get("mismatch") or {}
check(
    "it records what was expected and what was read",
    mismatch.get("expected_tag_uid") == tag_a and mismatch.get("scanned_tag_uid") == tag_b,
    (mismatch.get("expected_tag_uid"), mismatch.get("scanned_tag_uid")),
)
check(
    "and does the reverse lookup: 'this tag belongs to ...'",
    mismatch.get("scanned_resolved_location_id") == slot_b,
    mismatch.get("scanned_resolved_label_path"),
)
check(
    "and does not auto-fix it",
    mismatch.get("resolved_at") is None,
    mismatch.get("resolved_at"),
)

print("== a readable tag whose URI never got written is degraded, not a mismatch")
status, resp = call(
    "POST",
    f"/api/verification-sessions/{vsid}/check",
    {
        "tag_uid": tag_b,
        "location_id": slot_b,
        "carries_ndef": True,
        "client_op_id": "e2e-vdegraded",
        "device_id": "e2e",
    },
)
check("the right tag still matches", resp.get("status") == "match", resp.get("status"))
check(
    "but the missing URI is recorded as degraded",
    resp.get("ndef_state") == "degraded",
    resp.get("ndef_state"),
)

print()
print("FAILURES: " + ", ".join(failures) if failures else "all commissioning checks passed")
raise SystemExit(1 if failures else 0)
