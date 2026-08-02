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

#: Empty polls before a reader is said to have lost its tag.
#:
#: Two, not one. A tag resting at the edge of the antenna drops out of a single
#: poll and returns on the next, and at the 500 ms tap interval one missed read
#: is 500 ms of nothing — announcing a departure for that would turn a sticker
#: lying still into a stream of arrivals and departures. Two is 1 s, which is
#: shorter than the gesture of lifting a drawer and slower than the flicker.
#: `TagPresence` makes the same trade for the station with `absent_polls=3`.
GONE_AFTER_MISSED_POLLS: Final = 2

Publish = Callable[[Event], Awaitable[None]]


class _BadWrite(Exception):
    """A `tag.write` frame that cannot be acted on. Answered, never acted on."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class DeviceLocks:
    """One lock per reader, held by *everything* that talks to it.

    `FlipperRpc` says it in as many words — "not thread-safe by design: one link,
    one session, one caller" — and the bridge had two callers: the tap loop
    polling every 500 ms in a worker thread, and a `tag.write` running in
    another. Both go through `asyncio.to_thread`, so they genuinely overlap.

    **This was found on hardware and could not have been found without it.** The
    fake replays a byte sequence for a single caller, so it is perfectly happy;
    a real Flipper interleaves the two conversations on one CDC stream and the
    write's reply gets consumed by the poller. The visible symptom is the worst
    kind: `tag.write_failed` saying the Flipper "answered a WRITE with" a read's
    answer — *while the tag is written correctly*. A client that believes that
    event posts a null read-back, and a perfectly good sticker is recorded
    `degraded` for ever.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def for_device(self, device_id: str) -> asyncio.Lock:
        return self._locks.setdefault(device_id, asyncio.Lock())


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
        busy: DeviceLocks | None = None,
    ) -> None:
        self._registry = registry
        self._timeout_s = timeout_s
        self._busy = busy if busy is not None else DeviceLocks()

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

        lock = self._busy.for_device(device_id)
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
    busy: DeviceLocks | None = None,
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
    #: What each reader currently has in its field, by identity key. The edges of
    #: this map are the whole `tag.seen` / `tag.gone` vocabulary.
    present: dict[str, str | None] = {}
    #: Consecutive empty polls per reader, so a tag flickering at the edge of the
    #: antenna is not reported as leaving and arriving.
    misses: dict[str, int] = {}
    # Shared with the `TagWriter` by `main.run`; a private one here would lock
    # against nothing, which is exactly the bug this is fixing.
    busy = busy if busy is not None else DeviceLocks()
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
            lock = busy.for_device(device_id)
            if lock.locked():
                # A write is in flight on this reader. Skipping the poll rather
                # than queueing behind it: by the time the write finishes this
                # tick is stale, and the next one is 500 ms away.
                continue
            try:
                async with lock:
                    read = await asyncio.to_thread(attached.source.poll)
            except TagSourceError as error:
                logger.warning("%s faulted: %s", device_id, error)
                for event in registry.fault(device_id, str(error)):
                    await publish(event)
                continue

            if read is None:
                # Empty field. Not "gone" yet: a tag at the edge of the antenna
                # drops out of a poll and comes back on the next one, and
                # announcing a departure per missed read would make one sticker
                # sitting still look like somebody tapping it repeatedly.
                if present.get(device_id) is None:
                    continue
                misses[device_id] = misses.get(device_id, 0) + 1
                if misses[device_id] < GONE_AFTER_MISSED_POLLS:
                    continue
                departed = present.pop(device_id, None)
                misses.pop(device_id, None)
                # The hold-off for **that tag** goes with it, so putting the same
                # one back is a fresh arrival rather than a silence.
                #
                # This used to forget `_key(device_id, None)` — the literal key
                # `"<device>|unknown"` — which is the key an *unreadable* tag
                # debounces under and never the key of the tag that just left.
                # So the comment claiming lift-and-replace was two taps was
                # false: inside the window it was one tap and then nothing, and
                # nothing said why.
                holdoff.forget(_key(device_id, departed))
                await publish(events.tag_gone(device_id=device_id))
                continue

            found = identity.identify(read)
            misses.pop(device_id, None)
            if present.get(device_id) == found.key:
                # **The same tag, still there.** Silence is the message: this
                # loop used to re-publish `tag.seen` every hold-off window for as
                # long as a tag lay in the field, so a client wanting to know
                # what *changed* had to dedupe a drumbeat, and one that recorded
                # every sighting recorded the same drawer thirty times. Presence
                # is now stated by its edges — one arrival, one departure.
                continue
            key = _key(device_id, found.key)
            if not holdoff.admit(key):
                continue
            present[device_id] = found.key
            await publish(events.tag_seen(device_id=device_id, identity=found))

        elapsed = clock() - started
        since_sweep += max(elapsed, tap_interval_s)
        await sleep(max(0.0, tap_interval_s - elapsed))


def _key(device_id: str, tag_key: str | None) -> str:
    """A tag that answered but identified as nothing still debounces — otherwise
    an unreadable tag left in a field would re-fire at the tap rate."""
    return f"{device_id}|{tag_key or 'unknown'}"
