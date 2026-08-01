"""Which readers exist right now, what each can do, and where a write goes.

ADR 0013 in one object. `agent.presence` answers "is a tag on the platform";
this answers the question underneath it — "is there a platform at all, and can
it write" — which nothing needed until a reader could be unplugged mid-walk.

Three decisions worth knowing before reading the code:

**Discovery is a sweep, not an event subscription.** No `udev`, no `pyudev`, no
D-Bus. A directory listing of `/dev/serial/by-id` every couple of seconds is
enough to notice a Flipper appearing, costs nothing measurable, and works
identically on a Pi, a laptop and in a container with a device passed in.
Subscribing to hotplug would be more elegant and would add a dependency, a
platform assumption, and a class of bug where the subscription dies quietly and
the bridge simply stops seeing new hardware.

**The station's own reader is registered but not polled here.** `poll_forever`
owns the station's cadence — the identify budget and the removal debounce are
both counted in *its* polls — and a second loop reading the same PN532 would
wedge the UART and silently corrupt both budgets. So `adopt` puts the station's
source in the roster, so it can be named in a write and announced with its
capabilities, and marks it as somebody else's to poll.

**Failures are announced once, not once per sweep.** A Flipper plugged in with
no Antlia installed fails to open every two seconds for as long as it is
connected. The same shape as `TagPresence.observe_fault` and
`poll_forever`'s overrun warning: say it on the edge, stay quiet after.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from agent import events, tags
from agent.events import Event
from agent.tags import (
    TagCapabilities,
    TagSource,
    TagSourceError,
    TagWrite,
    TagWriteRefused,
    WritableTagSource,
)

logger = logging.getLogger("almagest.deviceagent.devices")

#: `ProvisioningDevice` values, so a `kind` can be forwarded to the API verbatim
#: when a walk records who bound a tag. Not an enum here: this process must not
#: import `app.models`, and a string that matches is the whole contract.
KIND_STATION_PN532: Final = "station_pn532"
KIND_FLIPPER: Final = "flipper_rpc"

#: `device.detached` reasons.
UNPLUGGED: Final = "unplugged"
FAILED: Final = "failed"


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """One reader, as the wire describes it."""

    #: Stable across a detach and reattach of the same physical thing, because it
    #: is derived from a port or a Bluetooth address rather than from a counter.
    #: A PWA that had chosen a reader must not lose it when a cable is jiggled.
    device_id: str
    kind: str
    label: str
    capabilities: TagCapabilities

    def as_data(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "kind": self.kind,
            "label": self.label,
            "capabilities": self.capabilities.as_data(),
        }


class DeviceBackend(Protocol):
    """A family of readers that can be looked for and opened.

    Two methods rather than one because they cost wildly different amounts:
    `scan` is a directory listing or a BLE advertisement sweep and runs
    constantly, while `open` talks to the device — a PN532 handshake, a Flipper
    RPC session and a `.fap` launch — and runs once per attachment.
    """

    @property
    def kind(self) -> str:
        """The `ProvisioningDevice` value every device from this backend has."""
        ...

    def scan(self) -> Mapping[str, str]:
        """`{device_id: label}` for everything present right now.

        Cheap and side-effect free. Must not open anything: a scan that opened
        each candidate would re-launch Antlia on every sweep.
        """
        ...

    def open(self, device_id: str) -> TagSource:
        """Talk to it. Raises `TagSourceError` if it cannot be made to work."""
        ...


@dataclass
class Attached:
    """A reader the registry is holding open."""

    info: DeviceInfo
    source: TagSource
    #: True for the station's own reader, whose cadence `poll_forever` owns. The
    #: registry never polls it — two loops on one UART is a wedged reader and two
    #: silently wrong budgets.
    polled_elsewhere: bool = False


class DeviceRegistry:
    """The roster, and the only place a `TagSource` is opened or closed.

    Synchronous and event-returning, like `TagPresence.observe`: every decision
    is testable without an event loop, and `agent.main` is the one place that
    publishes. The blocking work (`scan`, `open`, `write`) runs in a worker
    thread, as `poll` already does.
    """

    def __init__(
        self,
        backends: list[DeviceBackend] | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._backends = list(backends or [])
        self._attached: dict[str, Attached] = {}
        #: Ids whose last `open` failed, so the failure is reported on the edge
        #: rather than on every sweep for as long as the thing stays plugged in.
        self._failed: set[str] = set()
        self._clock = clock

    # -- the roster --------------------------------------------------------

    def adopt(self, source: TagSource, *, device_id: str, kind: str, label: str) -> list[Event]:
        """Register a reader somebody else is polling. The station's own.

        Returns the `device.attached` event, so the station's PN532 appears in
        the roster with its capabilities and can be named by a `tag.write` — the
        gap ADR 0012 flagged, since binding from the bench previously left every
        tag `unverified`.
        """
        info = DeviceInfo(
            device_id=device_id, kind=kind, label=label, capabilities=source.capabilities
        )
        self._attached[device_id] = Attached(info=info, source=source, polled_elsewhere=True)
        return [events.device_attached(**_attached_payload(info))]

    @property
    def infos(self) -> tuple[DeviceInfo, ...]:
        return tuple(a.info for a in self._attached.values())

    def get(self, device_id: str) -> Attached | None:
        return self._attached.get(device_id)

    def pollable(self) -> tuple[Attached, ...]:
        """Everything the bridge loop is responsible for reading."""
        return tuple(a for a in self._attached.values() if not a.polled_elsewhere)

    # -- discovery ---------------------------------------------------------

    def sweep(self) -> list[Event]:
        """Look for readers; open the new ones, drop the vanished ones.

        Order matters: detachments are published before attachments so that a
        reader which reappears on a different node — a Flipper replugged into
        another port — never has two entries in a client's roster at once.
        """
        seen: dict[str, tuple[DeviceBackend, str]] = {}
        emitted: list[Event] = []

        for backend in self._backends:
            try:
                found = backend.scan()
            except Exception as error:
                logger.warning("scanning %s failed: %s", backend.kind, error)
                continue
            for device_id, label in found.items():
                seen[device_id] = (backend, label)

        for device_id in list(self._attached):
            attached = self._attached[device_id]
            if attached.polled_elsewhere or device_id in seen:
                continue
            self._close(device_id)
            emitted.append(events.device_detached(device_id=device_id, reason=UNPLUGGED))

        for device_id, (backend, label) in seen.items():
            if device_id in self._attached:
                continue
            try:
                source = backend.open(device_id)
            except TagSourceError as error:
                if device_id not in self._failed:
                    self._failed.add(device_id)
                    emitted.append(events.device_error(device_id=device_id, message=str(error)))
                    logger.warning("cannot open %s: %s", device_id, error)
                continue
            self._failed.discard(device_id)
            info = DeviceInfo(
                device_id=device_id,
                kind=backend.kind,
                label=label,
                capabilities=source.capabilities,
            )
            self._attached[device_id] = Attached(info=info, source=source)
            logger.info("attached %s (%s)", device_id, label)
            emitted.append(events.device_attached(**_attached_payload(info)))

        # A device that came back must be allowed to fail loudly again next time.
        self._failed &= set(seen)
        return emitted

    def fault(self, device_id: str, message: str) -> list[Event]:
        """A reader that faulted while attached. Dropped, not kept limping.

        Dropped because `TagSourceError` means the reader itself is broken — a
        pulled cable, a vanished port — and a source in that state produces the
        same error for ever. The next sweep re-opens it if the thing is really
        still there, which is also how a Flipper whose app was killed on the
        device recovers without anyone touching the bridge.
        """
        if device_id not in self._attached:
            return []
        self._close(device_id)
        return [
            events.device_error(device_id=device_id, message=message),
            events.device_detached(device_id=device_id, reason=FAILED),
        ]

    # -- writing -----------------------------------------------------------

    def write(self, device_id: str, url: str, *, overwrite: bool) -> TagWrite:
        """Route a write to a named device.

        Refuses rather than guessing when the device is unknown or cannot write.
        **There is deliberately no "pick whichever reader can write".** A bench
        may have a PN532 under the platform and a Flipper on a cable, and a write
        aimed at the wrong one either fails confusingly or — much worse —
        succeeds against whatever tag happened to be in that other reader's
        field. The client names the device because only the client knows which
        one the user is holding a tag against.
        """
        attached = self._attached.get(device_id)
        if attached is None:
            raise TagWriteRefused(f"no reader called {device_id!r}", reason=tags.UNSUPPORTED)
        if not attached.info.capabilities.writes_ndef:
            raise TagWriteRefused(
                f"{attached.info.label} cannot write", reason=tags.UNSUPPORTED
            )
        if not isinstance(attached.source, WritableTagSource):
            # Belt and braces: a source whose advertised capability and whose
            # actual methods disagree is a programming error, and finding it here
            # is better than an AttributeError halfway through a walk.
            raise TagWriteRefused(
                f"{attached.info.label} claims a write it does not implement",
                reason=tags.UNSUPPORTED,
            )
        return attached.source.write_uri(url, overwrite=overwrite)

    # -- lifecycle ---------------------------------------------------------

    def _close(self, device_id: str) -> None:
        attached = self._attached.pop(device_id, None)
        if attached is None or attached.polled_elsewhere:
            return
        try:
            attached.source.close()
        except Exception:
            logger.debug("closing %s raised", device_id, exc_info=True)

    def close(self) -> None:
        for device_id in list(self._attached):
            self._close(device_id)
        self._attached.clear()


def _attached_payload(info: DeviceInfo) -> dict[str, Any]:
    return {
        "device_id": info.device_id,
        "kind": info.kind,
        "label": info.label,
        "capabilities": info.capabilities.as_data(),
    }


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


@dataclass
class FlipperUsbBackend:
    """Flippers on USB cables, found by their CDC node's stable by-id name.

    `/dev/serial/by-id` rather than `/dev/ttyACM<n>` because the latter is
    assigned in plug order: unplug two Flippers and plug them back the other way
    round and every `device_id` has silently swapped, which for a *write* means
    the payload for one drawer going onto a tag held against the other. The by-id
    name contains the Flipper's serial number and does not move.
    """

    directory: str = "/dev/serial/by-id"
    name_hint: str = "Flipper"
    #: Injected so the tests do not need a `/dev`.
    lister: Callable[[str], list[str]] | None = None
    opener: Callable[[str], TagSource] | None = None

    @property
    def kind(self) -> str:
        return KIND_FLIPPER

    def scan(self) -> Mapping[str, str]:
        from pathlib import Path

        if self.lister is not None:
            names = self.lister(self.directory)
        else:
            root = Path(self.directory)
            if not root.is_dir():
                return {}
            names = sorted(entry.name for entry in root.iterdir())
        return {
            f"flipper-usb:{name}": _flipper_label(name)
            for name in names
            if self.name_hint.casefold() in name.casefold()
        }

    def open(self, device_id: str) -> TagSource:
        port = f"{self.directory}/{device_id.removeprefix('flipper-usb:')}"
        if self.opener is not None:
            return self.opener(port)
        from agent.flipper.session import open_serial

        return open_serial(port)


def _flipper_label(node_name: str) -> str:
    """`usb-Flipper_Devices_Inc._Flipper_Vyvern_flip_Vyvern-if00` → `Flipper Vyvern`.

    The device's own name is in there and is what the user calls it, which is
    the whole point of showing a label — "Flipper Vyvern" is answerable, and a
    47-character udev path is not.
    """
    parts = node_name.split("_")
    for index, part in enumerate(parts):
        if part.casefold() == "flipper" and index + 1 < len(parts):
            candidate = parts[index + 1]
            if candidate.casefold() not in ("devices", "inc.", "inc"):
                return f"Flipper {candidate}"
    return "Flipper Zero"


@dataclass
class StaticBackend:
    """A fixed roster. For `--fake`, and for tests.

    The bridge's equivalent of `FakeTagSource`: it makes the whole attach /
    detach / write path exercisable with no hardware, which is the only way the
    frontend gets developed at all — see `frontend/src/lib/tags/simulated.ts`,
    which does the same job on the other side of the socket.
    """

    sources: dict[str, TagSource] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    device_kind: str = KIND_FLIPPER
    #: Ids to report as present but refuse to open, so `device.error` has a path.
    broken: set[str] = field(default_factory=set)

    @property
    def kind(self) -> str:
        return self.device_kind

    def scan(self) -> Mapping[str, str]:
        found = {name: self.labels.get(name, name) for name in self.sources}
        found.update({name: self.labels.get(name, name) for name in self.broken})
        return found

    def open(self, device_id: str) -> TagSource:
        if device_id in self.broken:
            raise TagSourceError(f"{device_id} is wired up to fail")
        return self.sources[device_id]


def default_backends(*, flipper_usb: bool = True) -> list[DeviceBackend]:
    """What the bridge looks for unless told otherwise.

    BLE is **not** here. `BleFlipperLink` has never executed — the development
    machine has no Bluetooth stack — and a scan that spins up an adapter on every
    Pi boot to look for hardware nobody has confirmed works is a worse default
    than making it opt-in. `DEVICEAGENT_FLIPPER_BLE=1` turns it on; ADR 0013
    records why it is off.
    """
    backends: list[DeviceBackend] = []
    if flipper_usb:
        backends.append(FlipperUsbBackend())
    return backends
