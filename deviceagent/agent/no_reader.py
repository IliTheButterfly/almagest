"""`NoTagSource` — the station half turned off, for a bench that has no platform.

ADR 0014 widened where this process runs: it was *"a Pi-side daemon next to
kiosk Chromium"* and is now *"whatever machine the browser is on — the Pi at the
bench, or a laptop with a Flipper on the end of a cable"*. That second shape had
no way to start. `build_source` offered `pn532`, `rc522`, or `--fake`, and all
three are wrong for a machine whose only reader is on USB:

* `pn532`/`rc522` open a port that is not there and exit 2 before the bridge —
  the part that would have found the Flipper — has run at all.
* `--fake` starts, and is worse. `FakeTagSource(..., repeat=True)` replays a
  scripted placement **forever**, so a bench with nothing on it narrates a
  container being set down every few seconds, tries to commit each one, and logs
  `station.failed` when the API has never heard of the demo tag. A kiosk showing
  a drawer that is not there is not a degraded station; it is a lying one.

So the third answer is: **there is no platform reader here, and that is a
statement, not a failure.** The station loop runs against a source that always
reports an empty field, presence stays `IDLE`, no session ever starts, and the
bridge loop does the actual work.

**It is not adopted into the device roster**, and that is the point of it being
a distinct class rather than a fake with an empty script. `devices.adopt`
publishes `device.attached` with a capability set, and the PWA offers a reader
to write with on the strength of that message. Announcing a reader that cannot
read a tag or write one would put a dead entry in the chooser — exactly the
"supported/unsupported flag" ADR 0012 refuses, with the sign flipped. The
absence of a device is communicated by the absence of a `device.attached`, which
is the same rule ADR 0003 applies to the missing scale.
"""

from __future__ import annotations

from typing import Final

from agent.tags import TagCapabilities, TagRead

#: Reads nothing, writes nothing. Constructed for completeness — the registry
#: never sees it, because a `NoTagSource` is never adopted — but `TagSource`
#: requires the property and a reader whose capabilities were a lie in the one
#: place they are unused is how they become a lie somewhere they are not.
READS_NOTHING: Final = TagCapabilities(reads_uid=False, reads_ndef=False, writes_ndef=False)


class NoTagSource:
    """A platform reader that is definitively absent.

    `poll()` returns `None`, which `agent.tags` defines precisely as *"the field
    is empty. Nothing is on the platform."* That is true, permanently, and it is
    a different statement from `TagRead(None, None)` — *"something is in the
    field but neither carrier came back"* — which would make the kiosk offer to
    provision a tag that does not exist.
    """

    @property
    def capabilities(self) -> TagCapabilities:
        return READS_NOTHING

    def poll(self) -> TagRead | None:
        return None

    def close(self) -> None:
        """Nothing to release. Idempotent by being empty."""
