"""The bridge loop: find readers, publish their taps, carry out writes.

The station half of this daemon (`agent.presence`, `agent.session`,
`poll_forever`) is untouched by everything here, and that separation is
deliberate rather than incidental:

* **The station has one reader and a state machine.** Its cadence is a contract —
  the identify budget and the removal debounce are counted in its polls — and its
  vocabulary is workflow 5's. Nothing in this module may perturb it.
* **The bridge has zero or more readers and no state machine.** A provisioning
  walk wants "a tag was held against this reader", debounced, and the ability to
  write. That is `tag.seen` and `tag.write`, and it is the whole surface.

They share the hub, the identity rules and the registry, and nothing else. A
reader can be in both roles — the station's PN532 is adopted into the roster so
it can be *written to* — but it is only ever **polled by one of them**, because
two loops on one UART is a wedged reader and two silently wrong budgets.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

from agent import events, identity, tags
from agent.devices import DeviceRegistry
from agent.events import Event
from agent.holdoff import DEFAULT_WINDOW_MS, HoldOff
from agent.tags import TagSourceError, TagWriteRefused

logger = logging.getLogger("almagest.deviceagent.bridge")

#: How often to look for readers appearing or vanishing. Two seconds is well
#: below the time it takes a person to plug something in and look at the screen,
#: and a directory listing at that rate is unmeasurable.
DEFAULT_SWEEP_INTERVAL_S: Final = 2.0

#: How often each attached bridge reader is asked for a tag. Slower than the
#: station's 300 ms because a Flipper's answer costs a whole RPC round trip over
#: USB and it is being held in someone's hand, not sitting under a platform.
DEFAULT_TAP_INTERVAL_S: Final = 0.5

#: The longest a single write may take before the bridge stops waiting. Generous:
#: a Type 2 Tag write is eight page writes plus a full read-back, and over a
#: Flipper each of those is an RPC round trip.
DEFAULT_WRITE_TIMEOUT_S: Final = 20.0

Publish = Callable[[Event], Awaitable[None]]


class _BadWrite(Exception):
    """A `tag.write` frame that cannot be acted on. Answered, never acted on."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class TagWriter:
    """Carries out `tag.write`, one at a time per device.

    **Serialised per device, not globally.** A bench with a PN532 and a Flipper
    should be able to write both at once; the same reader being asked twice at
    once is a different matter — a Type 2 Tag write is eight non-atomic page
    writes, and interleaving two of them produces a tag holding half of each.
    That is not a hypothetical: a double-tapped button in a PWA is the ordinary
    way it would happen.
    """

    def __init__(
        self,
        registry: DeviceRegistry,
        *,
        timeout_s: float = DEFAULT_WRITE_TIMEOUT_S,
    ) -> None:
        self._registry = registry
        self._timeout_s = timeout_s
        self._busy: dict[str, asyncio.Lock] = {}

    async def handle(self, frame: dict[str, Any]) -> list[Event]:
        """One `tag.write` command into its events. Never raises.

        Every refusal is an *event*, not an exception, because the client that
        sent the command is not the only one watching: two kiosk tabs open on one
        bench must not disagree about whether a tag was written. That is the same
        reason `agent.ws` publishes command consequences to everyone rather than
        answering the sender privately.
        """
        try:
            request_id, device_id, url, overwrite = _parse(frame)
        except _BadWrite as bad:
            # No device id to blame and possibly no request id either, so this is
            # the one case that cannot name what it refused. Logged rather than
            # published: a frame this malformed did not come from our own PWA.
            logger.warning("dropping a malformed tag.write: %s", bad)
            return []

        lock = self._busy.setdefault(device_id, asyncio.Lock())
        if lock.locked():
            return [
                events.tag_write_refused(
                    request_id=request_id,
                    device_id=device_id,
                    reason=tags.UNSUPPORTED,
                    message="a write is already in progress on this reader",
                )
            ]

        async with lock:
            emitted: list[Event] = [
                events.tag_writing(request_id=request_id, device_id=device_id, url=url)
            ]
            try:
                written = await asyncio.wait_for(
                    asyncio.to_thread(self._registry.write, device_id, url, overwrite=overwrite),
                    timeout=self._timeout_s,
                )
            except TagWriteRefused as refusal:
                emitted.append(
                    events.tag_write_refused(
                        request_id=request_id,
                        device_id=device_id,
                        reason=refusal.reason,
                        message=str(refusal),
                    )
                )
            except ValueError as error:
                # `ndef.pages_for_uri` refuses a payload that will not fit, before
                # a byte is written. A `ValueError` rather than a refusal because
                # it is a fact about the *payload*, not about the tag.
                emitted.append(
                    events.tag_write_refused(
                        request_id=request_id,
                        device_id=device_id,
                        reason=tags.TOO_LONG,
                        message=str(error),
                    )
                )
            except (TagSourceError, TimeoutError) as error:
                emitted.append(
                    events.tag_write_failed(
                        request_id=request_id,
                        device_id=device_id,
                        message=str(error) or "the reader stopped answering",
                    )
                )
                emitted.extend(self._registry.fault(device_id, str(error)))
            else:
                emitted.append(
                    events.tag_written(
                        request_id=request_id,
                        device_id=device_id,
                        url=url,
                        read_back_url=written.read_back_url,
                    )
                )
        return emitted


def _parse(frame: dict[str, Any]) -> tuple[str, str, str, bool]:
    request_id = frame.get("request_id")
    device_id = frame.get("device_id")
    url = frame.get("url")
    overwrite = frame.get("overwrite", False)
    if not isinstance(request_id, str) or not request_id:
        raise _BadWrite("request_id must be a non-empty string", reason=tags.UNSUPPORTED)
    if not isinstance(device_id, str) or not device_id:
        raise _BadWrite("device_id must be a non-empty string", reason=tags.UNSUPPORTED)
    if not isinstance(url, str) or not url:
        raise _BadWrite("url must be a non-empty string", reason=tags.UNSUPPORTED)
    if not isinstance(overwrite, bool):
        raise _BadWrite("overwrite must be a boolean", reason=tags.UNSUPPORTED)
    return request_id, device_id, url, overwrite


async def bridge_forever(
    registry: DeviceRegistry,
    publish: Publish,
    *,
    sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
    tap_interval_s: float = DEFAULT_TAP_INTERVAL_S,
    debounce_ms: int = DEFAULT_WINDOW_MS,
    max_sweeps: int | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Sweep for readers, poll the ones found, publish what they see.

    Paced to a fixed period like `poll_forever`, and for a weaker version of the
    same reason: the work here is bounded by however many readers are attached
    and how slow each one is, so sleeping the interval *after* the work would let
    a second Flipper halve the tap rate of the first.

    **A tap is debounced per device and per payload.** `HoldOff` keyed by
    `device_id + short id` rather than by payload alone, because two readers
    seeing the same tag are two facts — you can move one drawer between two
    stations — and one reader seeing it twice in 400 ms is one.

    `max_sweeps` exists so a test can drive the real loop rather than reimplement
    it; production passes `None`.
    """
    holdoff = HoldOff(debounce_ms, clock=clock)
    sweeps = 0
    since_sweep = sweep_interval_s  # sweep immediately on the first pass

    while max_sweeps is None or sweeps < max_sweeps:
        started = clock()

        if since_sweep >= sweep_interval_s:
            since_sweep = 0.0
            sweeps += 1
            for event in await asyncio.to_thread(registry.sweep):
                await publish(event)

        for attached in registry.pollable():
            device_id = attached.info.device_id
            try:
                read = await asyncio.to_thread(attached.source.poll)
            except TagSourceError as error:
                logger.warning("%s faulted: %s", device_id, error)
                for event in registry.fault(device_id, str(error)):
                    await publish(event)
                continue

            if read is None:
                # An empty field clears the hold-off, so lifting a tag and
                # putting it back is two taps rather than one and a silence.
                holdoff.forget(_key(device_id, None))
                continue

            found = identity.identify(read)
            key = _key(device_id, found.key)
            if not holdoff.admit(key):
                continue
            await publish(events.tag_seen(device_id=device_id, identity=found))

        elapsed = clock() - started
        since_sweep += max(elapsed, tap_interval_s)
        await sleep(max(0.0, tap_interval_s - elapsed))


def _key(device_id: str, tag_key: str | None) -> str:
    """A tag that answered but identified as nothing still debounces — otherwise
    an unreadable tag left in a field would re-fire at the tap rate."""
    return f"{device_id}|{tag_key or 'unknown'}"
