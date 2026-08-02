"""The poll loop, and the entry point. `almagest-deviceagent [--fake]`.

The loop is a dozen lines of logic and three deliberate choices.

**The blocking read runs in a worker thread** (`asyncio.to_thread`) so a PN532
that takes 250 ms to answer does not stall the WebSocket server sharing this
event loop. A stalled server means a kiosk that stops receiving events while a tag
is being read, which is precisely when it must not.

**Each presence event is published before the session sees it.** The kiosk should
render "reading 4K7T-92M8…" from the local parse without waiting for the API round
trip that turns it into `station.ready`; publishing first is what makes that
ordering guaranteed rather than incidental.

**The loop paces to a fixed period**, sleeping `interval − elapsed` rather than
sleeping the interval flat after the read. This is not a micro-optimisation, it is
what makes the only two numbers this daemon publishes in seconds mean anything.
Both budgets are counts of polls — `identify_polls` (~5 tries) and `absent_polls`
(3 empty polls) — and every doc quotes them as a duration by multiplying by the
interval. Sleeping the interval *after* the read makes the true period
`read + interval`, and the read is not free in exactly the cases those budgets
govern: `Pn532TagSource` spends its `DEFAULT_TARGET_TIMEOUT_S` (250 ms) on every
poll that finds no answer, which is every empty-platform poll of a removal
debounce and every poll of an unreadable tag's identify budget. Unpaced, five
300 ms polls took 2.75 s against a reader that blocked 250 ms; paced, they take
1.5 s. `tests/test_poll_loop.py` pins the arithmetic.

The bound is `polls × interval` only while one poll's read fits *inside* one
interval, and **nothing has measured whether it does** — 250 ms of anticollision
timeout leaves 50 ms of the default interval for an NDEF read that is one UART
round trip per 4-byte page. A read that overruns is logged once per run of
overruns rather than silently stretching the cadence, so the day a reader exists
the number checks itself. See README.md, "Unverifiable without hardware".

While a session round trip is outstanding the reader is not polled — the session's
work is awaited inline. That is a deliberate trade: interleaving a commit with a
container swap is a ledger row against the wrong bin, whereas a late-noticed
removal costs a fraction of a second. `DEVICEAGENT_API_TIMEOUT_S` bounds it. It
counts against the period too, so a slow commit is followed by an immediate poll
rather than by a further interval of not looking.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

from agent import __version__, events
from agent.api import HttpStationApi, StationApi
from agent.bridge import DeviceLocks, TagWriter, bridge_forever
from agent.config import AgentSettings, get_settings
from agent.devices import (
    KIND_STATION_PN532,
    KIND_STATION_RC522,
    DeviceBackend,
    DeviceRegistry,
    FlipperUsbBackend,
)
from agent.events import Event
from agent.fake_tags import FakeTagSource, load_script
from agent.hub import EventHub
from agent.no_reader import NoTagSource
from agent.presence import TagPresence
from agent.session import StationSession, parse_frame
from agent.tags import TagSource, TagSourceError
from agent.ws import serve_events

logger = logging.getLogger("almagest.deviceagent")


async def poll_forever(
    source: TagSource,
    presence: TagPresence,
    session: StationSession,
    hub: EventHub,
    *,
    interval_s: float,
    max_polls: int | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Poll, fold, publish, let the session react, then sleep out the rest of the period.

    `max_polls` exists so a test can drive the real loop to completion instead of
    reimplementing it; production passes `None`. `clock` and `sleep` are injected
    for the same reason `StationSession` takes a `clock`: the pacing arithmetic is
    the thing being asserted, and a test that measured it with a stopwatch would
    be both slow and flaky. Production takes the defaults.
    """
    polls = 0
    overrunning = False
    while max_polls is None or polls < max_polls:
        polls += 1
        started = clock()
        try:
            read = await asyncio.to_thread(source.poll)
        except TagSourceError as error:
            emitted = presence.observe_fault(str(error))
            if emitted:
                logger.warning("reader fault: %s", error)
        else:
            emitted = presence.observe(read)
        read_s = clock() - started

        for event in emitted:
            await _publish(hub, event)
            for consequence in await session.on_presence(event):
                await _publish(hub, consequence)

        # A read that fills the whole interval means the cadence is the read, not
        # the interval — and so both budgets are slower than every doc says. Said
        # once per run of overruns, the same shape as `TagPresence.observe_fault`,
        # because at 3 polls/second a line per poll would bury itself. An interval
        # of 0 is the tests and `--max-polls`, where "overrun" is meaningless.
        if read_s >= interval_s > 0.0:
            if not overrunning:
                overrunning = True
                logger.warning(
                    "a poll took %.0f ms, at or over the %.0f ms interval: the identify budget "
                    "and the removal debounce are both slower than DEVICEAGENT_POLL_INTERVAL_MS "
                    "implies. Raise the interval, or shorten the reader's target timeout.",
                    read_s * 1000.0,
                    interval_s * 1000.0,
                )
        else:
            overrunning = False

        # `interval` minus `elapsed`, not `interval`: see the module docstring. Never
        # negative, so a slow poll catches up on the next one instead of the loop
        # drifting further behind with every iteration.
        await sleep(max(0.0, interval_s - (clock() - started)))


async def _publish(hub: EventHub, event: Event) -> None:
    if event.type == events.STATION_FAILED:
        # The one event an operator needs to see without a browser attached: the
        # API is unreachable or refusing, and on a fresh Pi the kiosk may not be
        # running yet — in which case the event goes to nobody at all.
        logger.warning("api %s: %s", event.data.get("reason"), event.data.get("message"))
    message = await hub.publish(event)
    logger.debug("published %s seq=%s", message["type"], message["seq"])


def build_source(
    settings: AgentSettings,
    *,
    fake: bool,
    script: Path | None,
    reader: str | None = None,
) -> TagSource:
    """The one place the choice of reader is made.

    A `--script` implies `--fake`: naming a script and then talking to real
    hardware is never what was meant, and silently ignoring the flag is how you
    spend an afternoon wondering why the fixture changed nothing.

    `reader` overrides `DEVICEAGENT_READER` for one run, which is what a bench
    swap between the two modules wants — the alternative is editing `.env` to try
    a cable. Both driver modules are imported lazily, so a station with one reader
    never needs the other's library present.
    """
    if fake or script is not None:
        return FakeTagSource(load_script(script), repeat=True)

    if (reader or settings.reader) == "none":
        # A machine with no platform under it: ADR 0014's laptop-with-a-Flipper.
        # Deliberately not `--fake`, which would replay a placement forever and
        # narrate a container that is not on the bench. See `agent/no_reader.py`.
        # Imported at module scope, unlike the two driver modules: it has no
        # transport library behind it to keep off a machine that lacks one.
        return NoTagSource()

    if (reader or settings.reader) == "rc522":
        from agent.nfc_rc522 import Rc522TagSource

        return Rc522TagSource(
            bus=settings.rc522_spi_bus,
            device=settings.rc522_spi_device,
            speed_hz=settings.rc522_spi_hz,
        )

    from agent.nfc_pn532 import Pn532TagSource

    return Pn532TagSource(settings.pn532_port)


def build_api(settings: AgentSettings) -> StationApi:
    """The one place the API client is constructed.

    `api_base_url` is the API as reachable *from the Pi*, which is not
    `ALMAGEST_BASE_URL`: that one is the public origin stamped into every tag and
    printed label (ADR 0001), and it must not follow the agent's route to the
    server around.
    """
    return HttpStationApi(
        settings.api_base_url,
        device_id=settings.device_id,
        timeout_s=settings.api_timeout_s,
    )


def build_registry(settings: AgentSettings) -> DeviceRegistry:
    """The one place the bridge's discovery backends are chosen.

    BLE is opt-in — see `AgentSettings.flipper_ble` and ADR 0014. It is off not
    because it is undesirable but because nothing has ever run it.
    """
    backends: list[DeviceBackend] = []
    if settings.flipper_usb:
        backends.append(FlipperUsbBackend())
    if settings.flipper_ble:
        from agent.flipper.discovery import FlipperBleBackend

        backends.append(FlipperBleBackend())
    return DeviceRegistry(backends)


async def run(
    source: TagSource,
    settings: AgentSettings,
    *,
    api: StationApi | None = None,
    max_polls: int | None = None,
    registry: DeviceRegistry | None = None,
    max_sweeps: int | None = None,
) -> None:
    """Wire everything and run until cancelled.

    `api` is injectable so a test can drive the real loop, the real socket and the
    real state machines against a fake server — the same reason `source` is.

    **Two loops, one hub.** The station loop owns `source` and its cadence; the
    bridge loop owns everything the registry discovered. They run concurrently
    and neither can stall the other, which matters because a Flipper doing an
    NFC read takes an RPC round trip and the station's budgets are counted in
    300 ms polls.

    The station's own reader is `adopt`ed into the roster rather than discovered:
    that makes it nameable by a `tag.write` — closing the gap ADR 0012 recorded,
    where binding from the bench left every tag `unverified` — while leaving it
    polled by exactly one loop.
    """
    hub = EventHub()
    presence = TagPresence(
        identify_polls=settings.identify_polls,
        absent_polls=settings.absent_polls,
    )
    session = StationSession(
        api if api is not None else build_api(settings),
        debounce_ms=settings.command_debounce_ms,
    )
    devices = registry if registry is not None else build_registry(settings)
    # One set of per-device locks for both users of a reader — the tap loop
    # and the write path. Two readers of one RPC session interleave on real
    # hardware; see `DeviceLocks`.
    busy = DeviceLocks()
    writer = TagWriter(devices, busy=busy)

    async def on_frame(raw: str) -> None:
        frame = parse_frame(raw)
        if frame is not None and frame.get("type") == events.TAG_WRITE:
            for event in await writer.handle(frame):
                await _publish(hub, event)
            return
        for event in await session.handle_frame(raw):
            await _publish(hub, event)

    async def publish(event: Event) -> None:
        await _publish(hub, event)

    async with serve_events(
        hub,
        host=settings.ws_host,
        port=settings.ws_port,
        on_frame=on_frame,
        allowed_origin=settings.allowed_origin,
    ) as port:
        logger.info("event stream on ws://%s:%s", settings.ws_host, port)
        logger.info("committing through %s as device %r", settings.api_base_url, settings.device_id)

        # A station with no platform reader announces no platform reader. The
        # roster is what the PWA offers as a thing to hold a tag against, so an
        # entry that can neither read nor write is a dead choice in a chooser —
        # the "supported/unsupported flag" ADR 0012 refuses, sign flipped.
        # Absence is communicated by absence, exactly as ADR 0003 does for the
        # scale.
        if not isinstance(source, NoTagSource):
            for event in devices.adopt(
                source,
                device_id=STATION_DEVICE_ID,
                kind=_station_kind(source, settings),
                label=_station_label(source, settings),
            ):
                await publish(event)

        bridge = asyncio.create_task(
            bridge_forever(
                devices,
                publish,
                sweep_interval_s=settings.sweep_interval_s,
                tap_interval_s=settings.tap_interval_s,
                debounce_ms=settings.command_debounce_ms,
                max_sweeps=max_sweeps,
                busy=busy,
            )
        )
        try:
            await poll_forever(
                source,
                presence,
                session,
                hub,
                interval_s=settings.poll_interval_s,
                max_polls=max_polls,
            )
        finally:
            bridge.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bridge
            devices.close()
            source.close()


#: The station's reader always has this id. Fixed rather than derived from a port
#: because there is exactly one station per bridge and a PWA that has been told
#: to use "the station" must not have to re-learn its name when
#: `DEVICEAGENT_PN532_PORT` changes.
STATION_DEVICE_ID: Final = "station"


def _station_kind(source: TagSource, settings: AgentSettings) -> str:
    """Which `ProvisioningDevice` the station's own reader records itself as.

    Not folded into one "station" value: ADR 0013 makes the RC522 the module with
    less antenna and less margin, so "which one read this tag" is the first
    question worth asking about a drawer that binds intermittently. A fake
    reports as a PN532 because that is what it is standing in for.
    """
    if not isinstance(source, FakeTagSource) and settings.reader == "rc522":
        return KIND_STATION_RC522
    return KIND_STATION_PN532


def _station_label(source: TagSource, settings: AgentSettings) -> str:
    if isinstance(source, FakeTagSource):
        return "Simulated station reader"
    if settings.reader == "rc522":
        return f"Station RC522 on SPI {settings.rc522_spi_bus}.{settings.rc522_spi_device}"
    return f"Station PN532 on {settings.pn532_port}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="almagest-deviceagent",
        description="Almagest bench-station device agent: NFC tag reader and event stream.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--fake",
        action="store_true",
        help="replay the packaged scripted session instead of talking to a PN532; "
        "this is how the kiosk PWA is developed with no hardware on the desk",
    )
    parser.add_argument(
        "--script",
        type=Path,
        default=None,
        help="a session script to replay (implies --fake)",
    )
    parser.add_argument(
        "--reader",
        choices=("pn532", "rc522", "none"),
        default=None,
        help="override DEVICEAGENT_READER for this run; for trying the other module "
        "at the bench without editing .env. `none` means this machine has no "
        "platform reader and only the bridge's readers (a Flipper on USB) matter",
    )
    parser.add_argument(
        "--max-polls",
        type=int,
        default=None,
        help="stop after N polls; for smoke-testing a wiring change",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(levelname)s %(name)s %(message)s"
    )
    try:
        source = build_source(settings, fake=args.fake, script=args.script, reader=args.reader)
    except TagSourceError as error:
        # A missing reader is an operator problem with an operator answer, not a
        # traceback: the causes are a wrong port or SPI address, the `pi` extra
        # not being installed, and — on an RC522 — SPI disabled or RST floating.
        # Every one of them is named in the message it carries.
        logger.error("%s", error)
        return 2
    try:
        asyncio.run(run(source, settings, max_polls=args.max_polls))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
