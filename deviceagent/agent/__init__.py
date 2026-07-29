"""The Almagest device agent — the small daemon on the bench-station Pi.

It exists for exactly what a browser sandbox cannot reach. Barcode decoding
happens *in the browser*, identically on a phone and on the kiosk, so it is not
here. What is here is the PN532 NFC reader on the Pi's UART, and later the CSI
camera; both are Pi-attached hardware the PWA can only see through this process.

The agent speaks one direction: it publishes a stream of events over a loopback
WebSocket that the kiosk PWA subscribes to. It never writes to the database and
never calls the ledger — a station action is committed by the PWA calling the
API, so the sole ledger writer stays `app/services/ledger.py`.

**Web Serial was evaluated as a way to delete this component and declined**
(docs/PLAN.md, "The station"): the agent has to exist for the camera and the
PN532 regardless, `navigator.serial` is Chromium-desktop-only so nothing about
it is reusable on a phone, and splitting hardware ownership between
browser-privileged and daemon-privileged code is more brittle than one coherent
event stream over a socket that already exists.

**No station hardware has ever been attached to this code.** Every decision
worth testing lives above `TagSource`, which is a protocol with a fake that
replays a scripted session; the one module that touches a real reader
(`agent.nfc_pn532`) is deliberately thin and its contract test is
`@pytest.mark.live`, skipped by default. See README.md for the list of things
that stay unverified until a reader exists.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Reported in `station.hello` so a mismatched PWA has something to name.
__version__ = "0.1.0"
