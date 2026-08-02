"""The bridge: discovery, the roster, writes, and the loop that ties them together.

Everything here runs the real `DeviceRegistry`, the real `TagWriter` and the real
`bridge_forever` against `StaticBackend` and `FakeWritableTagSource`. No sockets,
no hardware, no sleeping — the loop's clock and sleep are injected for the same
reason `poll_forever`'s are: what is being asserted is the arithmetic, and a test
that measured it with a stopwatch would be slow and flaky.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from agent import events, tags
from agent.bridge import TagWriter, bridge_forever
from agent.devices import (
    FAILED,
    KIND_FLIPPER,
    KIND_STATION_PN532,
    UNPLUGGED,
    DeviceRegistry,
    StaticBackend,
    _flipper_label,
)
from agent.events import Event
from agent.fake_tags import FakeTagSource, FakeWritableTagSource
from agent.hub import EventHub
from agent.tags import READS_BOTH_AND_WRITES, TagSourceError

#: `tests/test_ws_loopback.py` runs its async scenarios with
#: `asyncio.run(asyncio.wait_for(...))` rather than pulling in `pytest-asyncio`.
#: This is that convention as a decorator, so the tests below read as ordinary
#: async functions and the deviceagent's dependency list stays as short as
#: `pyproject.toml` insists it should be.
TIMEOUT_S = 5.0


def sync[T](
    test: Callable[..., Coroutine[Any, Any, T]],
) -> Callable[..., T]:
    @functools.wraps(test)
    def wrapper(*args: object, **kwargs: object) -> T:
        return asyncio.run(asyncio.wait_for(test(*args, **kwargs), TIMEOUT_S))

    return wrapper


URL = "https://almagest.lan/s/4K7T92M8"
OTHER_URL = "https://almagest.lan/s/9ZQR31VT"


def registry_with(**sources: FakeWritableTagSource) -> tuple[DeviceRegistry, StaticBackend]:
    backend = StaticBackend(sources=dict(sources))
    return DeviceRegistry([backend]), backend


class TestDiscovery:
    def test_a_new_reader_is_announced_with_its_capabilities(self) -> None:
        registry, _ = registry_with(**{"flipper-usb:a": FakeWritableTagSource()})
        [event] = registry.sweep()
        assert event.type == events.DEVICE_ATTACHED
        assert event.data["device_id"] == "flipper-usb:a"
        assert event.data["kind"] == KIND_FLIPPER
        assert event.data["capabilities"] == READS_BOTH_AND_WRITES.as_data()

    def test_a_second_sweep_is_silent(self) -> None:
        """Discovery runs every couple of seconds for the life of the process. It
        must announce edges, not state."""
        registry, _ = registry_with(**{"flipper-usb:a": FakeWritableTagSource()})
        assert len(registry.sweep()) == 1
        assert registry.sweep() == []

    def test_unplugging_detaches(self) -> None:
        registry, backend = registry_with(**{"flipper-usb:a": FakeWritableTagSource()})
        registry.sweep()
        backend.sources.clear()
        [event] = registry.sweep()
        assert event.type == events.DEVICE_DETACHED
        assert event.data == {"device_id": "flipper-usb:a", "reason": UNPLUGGED}

    def test_a_device_id_survives_a_replug(self) -> None:
        """The whole reason ids come from a port and not a counter: a PWA that had
        chosen a reader must not lose it when a cable is jiggled."""
        source = FakeWritableTagSource()
        registry, backend = registry_with(**{"flipper-usb:a": source})
        registry.sweep()
        backend.sources.clear()
        registry.sweep()
        backend.sources["flipper-usb:a"] = source
        [event] = registry.sweep()
        assert event.data["device_id"] == "flipper-usb:a"

    def test_a_reader_that_cannot_be_opened_is_reported_once(self) -> None:
        """A Flipper plugged in with no Antlia installed fails on every sweep for
        as long as it stays connected. One message, on the edge."""
        registry = DeviceRegistry([StaticBackend(broken={"flipper-usb:bad"})])
        [event] = registry.sweep()
        assert event.type == events.DEVICE_ERROR
        assert registry.sweep() == []
        assert registry.sweep() == []

    def test_a_reader_that_recovers_can_fail_loudly_again(self) -> None:
        backend = StaticBackend(broken={"flipper-usb:x"})
        registry = DeviceRegistry([backend])
        assert len(registry.sweep()) == 1
        backend.broken.clear()
        backend.sources["flipper-usb:x"] = FakeWritableTagSource()
        assert [e.type for e in registry.sweep()] == [events.DEVICE_ATTACHED]

    def test_one_broken_backend_does_not_stop_the_others(self) -> None:
        class Exploding:
            kind = KIND_FLIPPER

            def scan(self) -> dict[str, str]:
                raise RuntimeError("no /dev today")

            def open(self, device_id: str) -> FakeWritableTagSource:
                raise AssertionError("never reached")

        good = StaticBackend(sources={"flipper-usb:a": FakeWritableTagSource()})
        registry = DeviceRegistry([Exploding(), good])  # type: ignore[list-item]
        assert [e.data["device_id"] for e in registry.sweep()] == ["flipper-usb:a"]

    def test_a_fault_drops_the_reader_so_the_next_sweep_reopens_it(self) -> None:
        """A `TagSourceError` means the reader itself is broken and will produce
        the same error for ever. Dropping it is also how a Flipper whose app was
        killed on the device recovers without anyone touching the bridge."""
        registry, _ = registry_with(**{"flipper-usb:a": FakeWritableTagSource()})
        registry.sweep()
        emitted = registry.fault("flipper-usb:a", "cable pulled")
        assert [e.type for e in emitted] == [events.DEVICE_ERROR, events.DEVICE_DETACHED]
        assert emitted[1].data["reason"] == FAILED
        assert [e.type for e in registry.sweep()] == [events.DEVICE_ATTACHED]


class TestTheStationIsAdoptedNotDiscovered:
    def test_it_joins_the_roster_and_is_never_polled_here(self) -> None:
        """Two loops on one UART is a wedged reader and two silently wrong
        budgets. `poll_forever` owns the station's cadence."""
        registry = DeviceRegistry([])
        station = FakeWritableTagSource()
        [event] = registry.adopt(
            station, device_id="station", kind=KIND_STATION_PN532, label="Station PN532"
        )
        assert event.data["kind"] == KIND_STATION_PN532
        assert registry.pollable() == ()
        assert registry.get("station") is not None

    def test_it_can_still_be_written_to(self) -> None:
        """The gap ADR 0012 recorded: binding from the bench left every tag
        `unverified` because the station's reader could not write."""
        registry = DeviceRegistry([])
        tag = FakeWritableTagSource(url=None)
        registry.adopt(tag, device_id="station", kind=KIND_STATION_PN532, label="Station")
        assert registry.write("station", URL, overwrite=False).read_back_url == URL

    def test_a_sweep_never_detaches_it(self) -> None:
        registry, _ = registry_with()
        registry.adopt(
            FakeWritableTagSource(), device_id="station", kind=KIND_STATION_PN532, label="S"
        )
        assert registry.sweep() == []
        assert registry.get("station") is not None


class TestWriteRouting:
    def test_an_unknown_device_is_refused_not_guessed(self) -> None:
        registry, _ = registry_with()
        with pytest.raises(tags.TagWriteRefused) as raised:
            registry.write("flipper-usb:ghost", URL, overwrite=False)
        assert raised.value.reason == tags.UNSUPPORTED

    def test_a_reader_that_cannot_write_is_refused(self) -> None:
        registry = DeviceRegistry([StaticBackend(sources={"scripted": FakeTagSource()})])
        registry.sweep()
        with pytest.raises(tags.TagWriteRefused) as raised:
            registry.write("scripted", URL, overwrite=False)
        assert raised.value.reason == tags.UNSUPPORTED

    def test_there_is_no_pick_whichever_reader_can_write(self) -> None:
        """A bench may have a PN532 under the platform and a Flipper on a cable.
        A write aimed at the wrong one either fails confusingly or — much worse —
        succeeds against whatever tag is in that other reader's field."""
        one = FakeWritableTagSource(url=None)
        two = FakeWritableTagSource(url=None)
        registry, _ = registry_with(**{"flipper-usb:one": one, "flipper-usb:two": two})
        registry.sweep()
        registry.write("flipper-usb:two", URL, overwrite=False)
        assert two.url == URL
        assert one.url is None, "the other reader was not touched"


class TestTagWriter:
    async def _events(self, writer: TagWriter, **frame: object) -> list[Event]:
        return await writer.handle({"type": events.TAG_WRITE, **frame})

    @sync
    async def test_a_write_publishes_writing_then_written(self) -> None:
        tag = FakeWritableTagSource(url=None)
        registry, _ = registry_with(**{"flipper-usb:a": tag})
        registry.sweep()
        emitted = await self._events(
            TagWriter(registry), request_id="r1", device_id="flipper-usb:a", url=URL
        )
        assert [e.type for e in emitted] == [events.TAG_WRITING, events.TAG_WRITTEN]
        assert emitted[1].data["read_back_url"] == URL
        assert "verified" not in emitted[1].data, "ADR 0012: no client-computed boolean"

    @sync
    async def test_a_refusal_names_a_reason_from_the_closed_vocabulary(self) -> None:
        registry, _ = registry_with(**{"flipper-usb:a": FakeWritableTagSource(url=OTHER_URL)})
        registry.sweep()
        emitted = await self._events(
            TagWriter(registry), request_id="r1", device_id="flipper-usb:a", url=URL
        )
        assert emitted[-1].type == events.TAG_WRITE_REFUSED
        assert emitted[-1].data["reason"] == tags.NOT_BLANK

    @sync
    async def test_a_payload_that_will_not_fit_is_refused_before_the_write(self) -> None:
        tag = FakeWritableTagSource(url=None, user_pages=2)
        registry, _ = registry_with(**{"flipper-usb:a": tag})
        registry.sweep()
        emitted = await self._events(
            TagWriter(registry), request_id="r1", device_id="flipper-usb:a", url=URL
        )
        assert emitted[-1].data["reason"] == tags.TOO_LONG
        assert tag.writes == []

    @sync
    async def test_a_broken_reader_fails_and_is_detached(self) -> None:
        """A refusal has a user-facing answer; a failure does not, and telling
        someone to re-seat a drawer when the reader is unplugged is a lie."""

        class Broken(FakeWritableTagSource):
            def write_uri(self, url: str, *, overwrite: bool = False) -> object:
                raise TagSourceError("the port vanished")

        registry, _ = registry_with(**{"flipper-usb:a": Broken()})
        registry.sweep()
        emitted = await self._events(
            TagWriter(registry), request_id="r1", device_id="flipper-usb:a", url=URL
        )
        assert [e.type for e in emitted] == [
            events.TAG_WRITING,
            events.TAG_WRITE_FAILED,
            events.DEVICE_ERROR,
            events.DEVICE_DETACHED,
        ]

    @sync
    async def test_a_malformed_command_is_dropped_rather_than_answered(self) -> None:
        """A frame this broken did not come from our own PWA, and there is no
        device to blame it on."""
        registry, _ = registry_with()
        writer = TagWriter(registry)
        assert await writer.handle({"type": events.TAG_WRITE}) == []
        assert await self._events(writer, request_id="r", device_id="d", url="") == []
        assert (
            await self._events(writer, request_id="r", device_id="d", url=URL, overwrite="yes")
            == []
        )

    @sync
    async def test_two_writes_to_one_reader_do_not_interleave(self) -> None:
        """A Type 2 Tag write is eight non-atomic page writes. Interleaving two
        produces a tag holding half of each, and a double-tapped PWA button is
        the ordinary way that happens."""
        registry, _ = registry_with(**{"flipper-usb:a": _SlowTag()})
        registry.sweep()
        writer = TagWriter(registry)
        first, second = await asyncio.gather(
            self._events(writer, request_id="r1", device_id="flipper-usb:a", url=URL),
            self._events(writer, request_id="r2", device_id="flipper-usb:a", url=OTHER_URL),
        )
        outcomes = {e.type for e in first + second}
        assert events.TAG_WRITE_REFUSED in outcomes, "the second was turned away"
        refused = [e for e in first + second if e.type == events.TAG_WRITE_REFUSED]
        assert "already in progress" in refused[0].data["message"]


class _SlowTag(FakeWritableTagSource):
    """Long enough for a second request to arrive while the first is in flight."""

    def __init__(self) -> None:
        super().__init__(url=None)

    def write_uri(self, url: str, *, overwrite: bool = False):  # type: ignore[no-untyped-def]
        import time

        time.sleep(0.05)
        return super().write_uri(url, overwrite=overwrite)


class TestBridgeLoop:
    @sync
    async def test_a_tap_is_published_once_not_once_per_poll(self) -> None:
        """A tag held against a reader is seen on every poll. `HoldOff` is what
        turns that into one event, the same 400 ms the PWA and the station use."""
        registry, _ = registry_with(
            **{"flipper-usb:a": FakeWritableTagSource(uid="04A2B3C4D5E680", url=URL)}
        )
        published = await _run_loop(registry, sweeps=4)
        seen = [e for e in published if e.type == events.TAG_SEEN]
        assert len(seen) == 1
        assert seen[0].data["device_id"] == "flipper-usb:a"
        assert seen[0].data["ndef_url"] == URL
        assert seen[0].data["short_id"] == "4K7T92M8"

    @sync
    async def test_two_readers_seeing_one_tag_are_two_facts(self) -> None:
        """You can move one drawer between two stations, and both sightings are
        real. The hold-off is keyed per device for exactly this."""
        registry, _ = registry_with(
            **{
                "flipper-usb:a": FakeWritableTagSource(url=URL),
                "flipper-usb:b": FakeWritableTagSource(url=URL),
            }
        )
        published = await _run_loop(registry, sweeps=3)
        seen = [e for e in published if e.type == events.TAG_SEEN]
        assert {e.data["device_id"] for e in seen} == {"flipper-usb:a", "flipper-usb:b"}

    @sync
    async def test_an_empty_field_re_arms_the_tap(self) -> None:
        """Lifting a tag and putting it back is two taps, not one and a silence."""
        tag = FakeWritableTagSource(url=URL)
        registry, _ = registry_with(**{"flipper-usb:a": tag})
        registry.sweep()

        published: list[Event] = []

        async def publish(event: Event) -> None:
            published.append(event)

        # Poll once with the tag present, once with it gone, once with it back.
        for present in (True, False, True):
            tag.present = present
            await bridge_forever(
                registry,
                publish,
                sweep_interval_s=1_000,  # no re-sweep; the reader is already attached
                tap_interval_s=0,
                max_sweeps=1,
                clock=_FrozenClock(),
                sleep=_no_sleep,
            )
        assert len([e for e in published if e.type == events.TAG_SEEN]) == 2

    @sync
    async def test_a_reader_that_faults_mid_poll_is_dropped(self) -> None:
        class Faulty(FakeWritableTagSource):
            def poll(self) -> object:
                raise TagSourceError("gone")

        registry, _ = registry_with(**{"flipper-usb:a": Faulty()})
        published = await _run_loop(registry, sweeps=1)
        assert events.DEVICE_DETACHED in {e.type for e in published}


class _FrozenClock:
    """Advances only when asked, so the hold-off window never elapses by accident."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _no_sleep(_: float) -> None:
    return None


async def _run_loop(registry: DeviceRegistry, *, sweeps: int) -> list[Event]:
    published: list[Event] = []

    async def publish(event: Event) -> None:
        published.append(event)

    await bridge_forever(
        registry,
        publish,
        sweep_interval_s=0,
        tap_interval_s=0,
        max_sweeps=sweeps,
        clock=_FrozenClock(),
        sleep=_no_sleep,
    )
    return published


class TestLabels:
    @pytest.mark.parametrize(
        ("node", "expected"),
        [
            ("usb-Flipper_Devices_Inc._Flipper_Vyvern_flip_Vyvern-if00", "Flipper Vyvern"),
            ("usb-Flipper_Zero_serial-if00", "Flipper Zero"),
        ],
    )
    def test_the_device_s_own_name_reaches_the_label(self, node: str, expected: str) -> None:
        """ "Flipper Vyvern" is answerable at a bench. A 47-character udev path is
        not, and the label exists only to be answerable."""
        assert _flipper_label(node) == expected


class TestHubRoster:
    @sync
    async def test_a_reconnecting_client_hears_about_every_reader(self) -> None:
        """The roster is a *set*, so it cannot use the single sticky slot: a kiosk
        reloading with two readers attached must hear about both."""
        hub = EventHub()
        for device_id in ("station", "flipper-usb:a"):
            await hub.publish(
                events.device_attached(
                    device_id=device_id,
                    kind=KIND_FLIPPER,
                    label=device_id,
                    capabilities=READS_BOTH_AND_WRITES.as_data(),
                )
            )
        sink = _ListSink()
        await hub.attach(sink)
        types = [m["type"] for m in sink.messages]
        assert types == [events.STATION_HELLO, events.DEVICE_ATTACHED, events.DEVICE_ATTACHED]
        assert [m["data"]["device_id"] for m in sink.messages[1:]] == [
            "station",
            "flipper-usb:a",
        ]

    @sync
    async def test_a_detached_reader_is_not_replayed(self) -> None:
        hub = EventHub()
        await hub.publish(
            events.device_attached(
                device_id="flipper-usb:a",
                kind=KIND_FLIPPER,
                label="a",
                capabilities=READS_BOTH_AND_WRITES.as_data(),
            )
        )
        await hub.publish(events.device_detached(device_id="flipper-usb:a", reason=UNPLUGGED))
        sink = _ListSink()
        await hub.attach(sink)
        assert [m["type"] for m in sink.messages] == [events.STATION_HELLO]

    @sync
    async def test_the_roster_keeps_its_original_seq(self) -> None:
        """Same dedupe mechanism as the sticky slot: a client that has already
        processed seq 3 ignores it when it is replayed."""
        hub = EventHub()
        message = await hub.publish(
            events.device_attached(device_id="a", kind=KIND_FLIPPER, label="a", capabilities={})
        )
        await hub.publish(events.tag_removed(missed_polls=3))
        sink = _ListSink()
        await hub.attach(sink)
        assert sink.messages[1]["seq"] == message["seq"]


class _ListSink:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send(self, message: str) -> None:
        import json

        self.messages.append(json.loads(message))
