"""Finding Flippers over Bluetooth. **Never executed.**

Split from `agent.devices` — which holds the USB backend — for one reason: this
module imports `bleak` and spins up a Bluetooth adapter, and `agent.devices` is
imported by everything. Keeping the import behind a function that is only called
when `DEVICEAGENT_FLIPPER_BLE` is set means the ordinary case (a Pi with a
PN532, a laptop with a cable) never touches a radio.

The honesty warning from `agent.flipper.link` applies doubly here: the machine
this was written on has no Bluetooth stack at all, so neither the scan nor the
connect has run even once. ADR 0014 lists it as the least-verified code in the
repository. What is written here is what `bleak`'s API says; whether a Flipper
answers it is unknown.

**Scanning is not free and this is why it is opt-in.** A BLE discovery sweep
holds the adapter for its whole duration, so running one every two seconds
alongside the USB sweep would make the adapter useless for anything else on the
machine. The scan interval is therefore its own, much longer, number.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from agent.devices import KIND_FLIPPER
from agent.flipper.link import BLE_NAME_HINT
from agent.tags import TagSource, TagSourceError

logger = logging.getLogger("almagest.deviceagent.flipper.discovery")

#: How long each discovery sweep listens. Below ~3 s a Flipper advertising at its
#: default interval can be missed entirely, which presents as a device that
#: "sometimes" appears — the worst kind of bug to chase at a bench.
DEFAULT_SCAN_SECONDS: Final = 4.0

#: A BLE sweep is far more expensive than a directory listing, so the registry's
#: 2 s cadence is wrong for it. This backend caches its last result and only
#: re-scans this often.
DEFAULT_RESCAN_SECONDS: Final = 30.0


@dataclass
class FlipperBleBackend:
    """Flippers advertising over BLE, matched by name.

    Matched by name because that is all an advertisement offers without
    connecting, and connecting to everything in range to ask what it is would be
    both slow and rude. The consequence — a device the user renamed to something
    without "Flipper" in it is invisible — is real, and the answer is
    `DEVICEAGENT_FLIPPER_BLE_NAME`, not a heuristic.
    """

    name_hint: str = BLE_NAME_HINT
    scan_seconds: float = DEFAULT_SCAN_SECONDS
    rescan_seconds: float = DEFAULT_RESCAN_SECONDS
    _cache: dict[str, str] = field(default_factory=dict)
    _last_scan: float | None = None

    @property
    def kind(self) -> str:
        return KIND_FLIPPER

    def scan(self) -> Mapping[str, str]:
        """Advertised Flippers as `{device_id: label}`, cached between sweeps.

        The registry sweeps every couple of seconds and a BLE scan takes several,
        so an uncached implementation would hold the adapter permanently and
        still be behind. Returning the cache between scans is what makes this
        backend cohabit with the registry's cadence rather than fight it.
        """
        import time

        now = time.monotonic()
        if self._last_scan is not None and now - self._last_scan < self.rescan_seconds:
            return dict(self._cache)

        self._last_scan = now
        try:
            self._cache = self._discover()
        except Exception as error:
            # No adapter, no permission, `bleak` absent. All operator problems
            # with operator answers, none of which are "the bridge crashed".
            logger.warning("BLE discovery failed: %s", error)
            self._cache = {}
        return dict(self._cache)

    def _discover(self) -> dict[str, str]:  # pragma: no cover — needs a Bluetooth stack
        import asyncio

        from bleak import BleakScanner

        async def sweep() -> dict[str, str]:
            found: dict[str, str] = {}
            for device in await BleakScanner.discover(timeout=self.scan_seconds):
                name = device.name or ""
                if self.name_hint.casefold() not in name.casefold():
                    continue
                found[f"flipper-ble:{device.address}"] = name or "Flipper Zero"
            return found

        # Its own loop: the registry calls `scan` from a worker thread
        # (`asyncio.to_thread`), where there is no running loop to borrow.
        return asyncio.run(sweep())

    def open(self, device_id: str) -> TagSource:  # pragma: no cover — needs hardware
        from agent.flipper.link import BleFlipperLink, FlipperLinkError
        from agent.flipper.session import FlipperRpc, FlipperTagSource

        address = device_id.removeprefix("flipper-ble:")
        try:
            link = BleFlipperLink(address, name=self._cache.get(device_id))
        except FlipperLinkError as error:
            raise TagSourceError(str(error)) from error

        try:
            rpc = FlipperRpc(link)
            rpc.ping()
            hello = rpc.launch_antlia()
        except Exception:
            link.close()
            raise
        return FlipperTagSource(rpc, hello)
