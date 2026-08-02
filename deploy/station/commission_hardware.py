#!/usr/bin/env python3
"""Commission a real drawer with a real tag, through the real Flipper.

Everything the docs describe, on hardware, in one pass:

  provisioning walk -> bind the tag the reader actually saw
                    -> the *bridge* writes the URI the server minted
                    -> read it back through the same reader
                    -> post the read-back so the server decides `verified`
  verification walk -> read the tag again and confirm the drawer

Two things this is careful about, both of them ADR 0012's rules:

* **The bridge never posts the write result.** It writes, reads back through the
  same reader, and publishes the string. The client holding the walk posts it,
  because only the client knows the `tag_id`.
* **Nothing computes `verified` here.** The read-back URI goes to the server and
  the server compares by short id, so a payload written against a different base
  URL is caught instead of agreeing with itself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlparse

import websockets

API = "http://127.0.0.1:8080"
BRIDGE = "ws://127.0.0.1:8765"
ORIGIN = "http://127.0.0.1:8080"

#: Everything the bridge can answer a `tag.write` with.
WRITE_EVENTS = {"tag.writing", "tag.written", "tag.write_refused", "tag.write_failed"}

failures: list[str] = []


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    # `--yes` rather than a prompt-by-default, because this is also run from a
    # terminal over ssh where a prompt would hang. Either way it is not the
    # default: the script picks the cabinet *and* the slot itself, and a tag
    # write is physical.
    parser.add_argument(
        "--yes",
        action="store_true",
        help="required: this binds and PHYSICALLY WRITES the tag on the antenna",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="let the write replace a URI already on the tag (refused otherwise)",
    )
    return parser.parse_args()


def _warn_if_unreachable(url: str) -> None:
    """A tag is about to carry this host. Say so if this machine cannot find it.

    Not fatal — the host is the *public* origin (ADR 0001) and need not resolve
    from the bench — but a sticker pointing at a name nothing resolves is a
    sticker that does nothing when tapped, and it is worth one line before it is
    burned rather than a puzzle a month later.
    """
    host = urlparse(url).hostname
    if host is None:
        return
    try:
        socket.getaddrinfo(host, None)
    except OSError:
        print(
            f"  !!  {host} does not resolve from this machine. The tag will still be\n"
            f"      written with it (ADR 0001 makes it the public origin), but nothing\n"
            f"      here will be able to follow the tag until DNS and the CA are in place."
        )


BRIDGE_HELP = (
    "Nothing is answering on the device bridge.\n"
    "  It is the unit that owns the readers:\n"
    "    systemctl --user start almagest-station-bridge\n"
    "    systemctl --user status almagest-station-bridge\n"
    "  On the bench Jetson its log goes to syslog, not the user journal:\n"
    "    tail -f /var/log/syslog | grep almagest"
)


async def main() -> int:
    args = _parse_args()
    if not args.yes:
        print(
            "Refusing to run without --yes.\n\n"
            "This picks a cabinet and a slot by itself, binds whatever tag is on the\n"
            "antenna to it, and PHYSICALLY WRITES that tag. A tag write cannot be\n"
            "undone by software. Run it against a station holding demo seed data and\n"
            "a tag you are willing to lose."
        )
        return 2

    try:
        connection = await websockets.connect(
            BRIDGE, additional_headers={"Origin": ORIGIN}
        )
    except OSError as error:
        # The likeliest first-run outcome, and 30 frames of asyncio name nothing
        # a person at a bench can act on.
        print(f"{BRIDGE}: {error}\n\n{BRIDGE_HELP}")
        return 1

    async with connection as ws:
        # --- the reader the bench actually has -------------------------------
        device_id = None
        tap = None
        deadline = asyncio.get_running_loop().time() + 25
        while asyncio.get_running_loop().time() < deadline and (
            device_id is None or tap is None
        ):
            event = json.loads(await asyncio.wait_for(ws.recv(), 20))
            if event["type"] == "device.attached":
                device_id = event["data"]["device_id"]
                caps = event["data"]["capabilities"]
                print(f"== reader: {event['data']['label']}  {caps}")
                check(
                    "the bridge offers a reader that can write",
                    caps["writes_ndef"],
                    caps,
                )
            elif event["type"] == "tag.seen" and device_id is not None:
                tap = event["data"]

        if device_id is None:
            # Which of the two is missing changes what you do next entirely, and
            # the script knows which.
            print(
                "No reader attached. The bridge is running but has found nothing.\n"
                "  A Flipper needs to be on USB and openable by this user:\n"
                "    ls -l /dev/serial/by-id/    # a *Flipper* node should be here\n"
                "    id -nG | tr ' ' '\\n' | grep dialout   # or `sudo usermod -aG dialout $USER`"
            )
            return 1
        if tap is None:
            print(
                "A reader is attached but no tag was read.\n"
                "  Put a tag on the antenna and run this again; an empty field is a\n"
                "  legitimate answer from a working reader, not a fault."
            )
            return 1

        uid = tap["tag_uid"]
        print(f"== tag on the antenna: uid={uid} url={tap['ndef_url']}")
        check("the reader produced a UID", bool(uid), uid)

        # --- a walk over a real cabinet --------------------------------------
        status, tree = call("GET", "/api/locations/tree")
        by_parent: dict[int, list[dict]] = {}
        for node in tree["nodes"]:
            if node.get("parent_id") is not None:
                by_parent.setdefault(node["parent_id"], []).append(node)
        root_id = max(by_parent.items(), key=lambda kv: len(kv[1]))[0]

        status, started = call(
            "POST",
            f"/api/locations/{root_id}/provisioning-sessions",
            {
                "device_kind": "flipper_rpc",
                "client_op_id": str(uuid.uuid4()),
                "device_id": "hw",
            },
        )
        check("a walk starts", status == 201, status)
        state = started["state"]
        sid = state["session"]["id"]
        slot = state["cursor"]["location_id"]
        slot_label = state["cursor"]["label_path"]
        print(f"== commissioning {slot_label}")

        # --- bind the tag the reader saw -------------------------------------
        status, bound = call(
            "POST",
            f"/api/provisioning-sessions/{sid}/bind",
            {"tag_uid": uid, "client_op_id": str(uuid.uuid4()), "device_id": "hw"},
        )
        check(
            "the real tag binds to the drawer",
            bound.get("status") == "bound",
            bound.get("status"),
        )
        if bound.get("status") != "bound":
            print(json.dumps(bound)[:400])
            return 1
        tag = bound["tag"]
        url = tag["ndef_url"]
        print(f"== the server minted {url}")
        _warn_if_unreachable(url)
        check(
            "the binding starts unverified",
            tag["ndef_state"] == "unverified",
            tag["ndef_state"],
        )

        # --- the Flipper writes it -------------------------------------------
        request_id = str(uuid.uuid4())
        await ws.send(
            json.dumps(
                {
                    "type": "tag.write",
                    "request_id": request_id,
                    "device_id": device_id,
                    "url": url,
                    "overwrite": args.overwrite,
                }
            )
        )
        read_back = None
        deadline = asyncio.get_running_loop().time() + 40
        while asyncio.get_running_loop().time() < deadline:
            event = json.loads(await asyncio.wait_for(ws.recv(), 30))
            # Exact names, not a prefix: `"tag.written".startswith("tag.write")`
            # is False — `writt` against `write` — so a prefix filter silently
            # drops the success event while still matching `tag.write_failed`,
            # and every run looks like a failed write on a correctly written tag.
            if event["type"] in WRITE_EVENTS:
                print(f"== {event['type']}: {json.dumps(event['data'])[:200]}")
                if event["type"] == "tag.written":
                    read_back = event["data"].get("read_back_url")
                    break
                if event["type"] in ("tag.write_refused", "tag.write_failed"):
                    break
                # `tag.writing` is progress, not an outcome: keep waiting.
        check("the Flipper wrote the tag and read it back", read_back == url, read_back)
        if read_back != url:
            # Stop rather than posting a null read-back. `check_write_result`
            # maps `None` to `degraded`, so carrying on would record a sticker
            # that may be perfectly good as permanently suspect — the exact
            # outcome the lock fix exists to prevent, reintroduced by the client.
            print(
                "\nthe write did not report success, so nothing is being posted:\n"
                "  a null read-back is recorded as `degraded` and that is a claim\n"
                "  about the sticker this run cannot honestly make."
            )
            return 1

        # --- the client posts the read-back; the server decides --------------
        status, result = call(
            "POST",
            f"/api/location-tags/{tag['id']}/write-result",
            {
                "read_back_url": read_back,
                "client_op_id": str(uuid.uuid4()),
                "device_id": "hw",
            },
        )
        check("the write-result is accepted", status == 200, status)
        check(
            "and the server calls the sticker verified",
            (result.get("tag") or {}).get("ndef_state") == "verified",
            (result.get("tag") or {}).get("ndef_state") or json.dumps(result)[:200],
        )

        # --- the verification walk, reading the tag again --------------------
        status, vstarted = call(
            "POST",
            f"/api/locations/{root_id}/verification-sessions",
            {
                "device_kind": "flipper_rpc",
                "client_op_id": str(uuid.uuid4()),
                "device_id": "hw",
            },
        )
        check("a verification walk starts", status == 201, status)
        vsid = vstarted["state"]["session"]["id"]

        # Re-read through the reader rather than reusing what we wrote: the point
        # of the walk is that the tag is asked again.
        fresh = None
        deadline = asyncio.get_running_loop().time() + 25
        while asyncio.get_running_loop().time() < deadline:
            event = json.loads(await asyncio.wait_for(ws.recv(), 20))
            if event["type"] == "tag.seen":
                fresh = event["data"]
                break
        check("the tag reads back through the reader", fresh is not None)
        if fresh is None:
            return 1
        print(f"== re-read: {fresh['ndef_url']} via {fresh['via']}")
        check(
            "and now identifies by its URI, not its UID",
            fresh["via"] == "ndef",
            fresh["via"],
        )

        status, checked = call(
            "POST",
            f"/api/verification-sessions/{vsid}/check",
            {
                "tag_uid": fresh["tag_uid"],
                "location_id": slot,
                "ndef_url": fresh["ndef_url"],
                "carries_ndef": True,
                "client_op_id": str(uuid.uuid4()),
                "device_id": "hw",
            },
        )
        check(
            "the drawer checks out",
            checked.get("status") == "match",
            checked.get("status"),
        )
        check(
            "with a verified sticker",
            checked.get("ndef_state") == "verified",
            checked.get("ndef_state"),
        )

    print()
    print(
        "FAILURES: " + ", ".join(failures)
        if failures
        else "commissioned on real hardware"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
