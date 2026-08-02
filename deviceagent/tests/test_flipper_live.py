"""The Flipper contract test. **Needs a real Flipper**, so it is skipped by default.

The sibling of `test_pn532_live.py` and `test_rc522_live.py`, and the one ADR 0014
implies but that did not exist: that ADR's "Unverified, and honest about it"
section says *"No Flipper has been driven by this code"* and that the RPC framing,
the app launch and the data-exchange round trip are tested only against a fake
that replays the bytes this codec produces — which proves the codec is
self-consistent, not that a Flipper agrees with it. This file is where that gets
settled, and like its siblings it exists as a runnable checklist rather than a
paragraph in a README: the day a Flipper is on the cable, this either passes or
names what is wrong.

    make agent-test-live          # or: uv run pytest -m live -k flipper

**Prerequisites, and each is a different failure if missing:**

* Antlia is installed at `/ext/apps/NFC/antlia.fap` and is a build with bridge
  mode. Absent, `launch_antlia` fails at open; stale, the `HELLO` version check
  below is what catches it.
* The operator can open the port. On a fresh Ubuntu the CDC node is
  `root:dialout 0660` and a new user is not in `dialout`, so this skips with a
  message saying exactly that rather than failing obscurely.
* A tag is on the antenna for the read and write tests, which say so when it is
  not — an empty field is a legitimate `poll()` answer and must not read as a
  broken reader.

**The write tests are opt-in a second time,** behind `--flipper-write`, because
unlike every other assertion here they *modify the tag in your hand*. A test
suite that silently rewrites a drawer's tag because someone ran the whole file is
the kind of thing that is discovered a week later at a bench.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent.devices import KIND_FLIPPER, DeviceRegistry, FlipperUsbBackend
from agent.identity import VIA_NDEF, VIA_UID, identify
from agent.tags import TagRead, TagSource, TagSourceError, TagWriteRefused, WritableTagSource

pytestmark = pytest.mark.live

#: Where the bridge looks, and the name it matches on. Both are
#: `FlipperUsbBackend`'s defaults, restated so a failure here reads as "the
#: node is not called what we expect" rather than as an attribute error.
BY_ID = "/dev/serial/by-id"
NAME_HINT = "Flipper"

#: A URI that is unmistakably a test write when found on a tag six months later.
#: Deliberately not a plausible short id: `idcodec` would accept a well-formed one
#: and a real drawer could then answer to it.
TEST_URI = "https://almagest.lan/s/LIVETEST"


def _nodes() -> list[str]:
    root = Path(BY_ID)
    if not root.is_dir():
        return []
    return [p.name for p in root.iterdir() if NAME_HINT in p.name]


@pytest.fixture(scope="module")
def port() -> str:
    """The Flipper's CDC node, or a skip that names which prerequisite is missing."""
    found = _nodes()
    if not found:
        pytest.skip(
            f"no {NAME_HINT} under {BY_ID}. Plug one in over USB; note that BLE is a "
            "separate transport and is not what this file tests."
        )
    path = str(Path(BY_ID) / found[0])
    if not os.access(path, os.R_OK | os.W_OK):
        pytest.skip(
            f"{path} exists but is not openable by this user. It is normally "
            "root:dialout 0660 — `sudo usermod -aG dialout $USER`, then log back in."
        )
    return path


@pytest.fixture(scope="module")
def reader(port: str) -> Iterator[TagSource]:
    """One session for the whole module.

    Module-scoped because opening is not cheap and is not free of side effects:
    it drains the CLI banner, switches the Flipper into RPC and launches an app
    on the device. Doing that per test would make the file slow and would make a
    failure in one test change the device state the next one sees.
    """
    from agent.flipper.session import open_serial

    try:
        source = open_serial(port)
    except TagSourceError as error:
        # A `TagSourceError` here is a *finding*, not a missing prerequisite:
        # `session.launch_antlia` raises it for a protocol mismatch, which is
        # the stale-`.fap` case this module's version test exists to catch. A
        # blanket `except Exception: skip` turned that into a green run.
        raise AssertionError(f"Antlia is on the device but unusable: {error}") from error
    except Exception as error:
        # Anything else is the device not being ready for us — no app installed,
        # the port grabbed by something else — which is a prerequisite.
        pytest.skip(f"could not bring up Antlia over RPC on {port}: {error!r}")
    yield source
    source.close()


# -- discovery -------------------------------------------------------------


def test_the_bridge_finds_the_flipper_by_its_stable_name(port: str) -> None:
    """The registry's own scan, not a hand-rolled listing.

    This is the step that decides whether a Flipper on a cable ever becomes a
    `device.attached` at all, and it is pure string matching against a udev name
    — which is exactly the sort of thing that is right in a fake and wrong on a
    device whose descriptors say something unexpected.
    """
    found = FlipperUsbBackend().scan()
    assert found, f"{BY_ID} has a {NAME_HINT} node but the backend matched nothing"
    device_id, label = next(iter(found.items()))
    assert device_id.startswith("flipper-usb:")
    # The label is what a person picks in the reader chooser, so it has to be
    # the Flipper's own name and not a 47-character udev path.
    assert NAME_HINT in label or label
    assert "/dev/serial/by-id" not in label


def test_the_registry_attaches_it_and_publishes_its_capabilities(port: str) -> None:
    """End to end through the object the bridge actually uses.

    Everything below tests `FlipperTagSource` directly; this tests that the
    registry can open one, which is a different code path and the one the running
    bridge takes.
    """
    registry = DeviceRegistry([FlipperUsbBackend()])
    try:
        events = registry.sweep()
        attached = [e for e in events if e.data.get("capabilities")]
        assert attached, f"sweep published no attachment: {[e.type for e in events]}"
        payload = attached[0].data
        assert payload["kind"] == KIND_FLIPPER
        caps = payload["capabilities"]
        # ADR 0014's table: a Flipper in bridge mode reads both carriers. Whether
        # it writes depends on the Antlia build and is asserted separately.
        assert caps["reads_uid"] and caps["reads_ndef"]
    finally:
        registry.close()


# -- the session -----------------------------------------------------------


def test_the_driver_satisfies_the_protocol_the_fake_stands_in_for(reader: TagSource) -> None:
    assert isinstance(reader, TagSource)


def test_antlia_announces_a_protocol_this_bridge_understands(reader: TagSource) -> None:
    """A stale `.fap` on the Flipper is the most likely field failure here.

    `antlia_rpc.c` sends its version in `HELLO` precisely so that a Flipper
    carrying last month's app is a clear refusal rather than a command that is
    silently misunderstood.
    """
    from agent.flipper import antlia

    version = getattr(reader, "protocol_version", None)
    assert version == antlia.PROTOCOL_VERSION, (
        f"Antlia announced protocol {version!r}; this bridge speaks {antlia.PROTOCOL_VERSION}"
    )


def test_the_build_on_the_device_says_it_can_write(reader: TagSource) -> None:
    """What `HELLO` reported, which is the only claim about this `.fap`.

    Not "capabilities are not a constant": `antlia_rpc.c:346` sends `rw`
    unconditionally today, so this reads a constant that travelled over a wire.
    It is still worth asserting, because the constant lives in the *firmware*
    and an older `.fap` is the thing being ruled out — but claiming more than
    that would be a test whose name is a lie.
    """
    caps = reader.capabilities
    assert caps.reads_uid and caps.reads_ndef
    assert caps.writes_ndef, "this Antlia build answered HELLO without `w`"


# -- reading ---------------------------------------------------------------


def test_a_tag_on_the_antenna_reads(reader: TagSource) -> None:
    """Hold a tag on the Flipper's antenna before running this."""
    read = reader.poll()
    if read is None:
        pytest.skip("no tag in the field — put one on the antenna and run again")
    assert isinstance(read, TagRead)
    assert read.uid or read.ndef_url, "something was in the field but neither carrier read"


def test_the_same_tag_reads_the_same_way_twice(reader: TagSource) -> None:
    """The property the whole station rests on, and the one a flaky reader breaks.

    `agent.presence` debounces on identity: if consecutive polls of one stationary
    tag returned different UIDs, every poll would look like a new container being
    set down, and the station would start a session per poll.
    """
    first = reader.poll()
    if first is None:
        pytest.skip("no tag in the field")
    second = reader.poll()
    assert second is not None, "the tag vanished between two consecutive polls"
    assert first.uid == second.uid


def test_the_carrier_a_tag_identifies_by_is_the_one_it_actually_has(reader: TagSource) -> None:
    """NDEF-first with a UID fallback, against a real tag rather than a fixture."""
    read = reader.poll()
    if read is None:
        pytest.skip("no tag in the field")
    carrier = identify(read)
    if read.ndef_url:
        assert carrier.via == VIA_NDEF
    else:
        assert carrier.via == VIA_UID, (
            "a tag with no NDEF must still identify by UID — that is what makes an "
            "unprovisioned tag bindable"
        )


# -- writing ---------------------------------------------------------------


def _require_write_opt_in(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--flipper-write", default=False):
        pytest.skip("writing modifies the tag in your hand; pass --flipper-write to allow it")


def test_a_blank_tag_takes_a_uri_and_reads_it_back(
    reader: TagSource, request: pytest.FixtureRequest
) -> None:
    """The claim ADR 0014 could not check: that a write survives a real tag.

    The read-back is done by the Flipper, because it is the device holding the
    tag — ADR 0012 refuses a write that reports its own success.
    """
    _require_write_opt_in(request)
    assert isinstance(reader, WritableTagSource)
    if reader.poll() is None:
        pytest.skip("no tag in the field")
    try:
        result = reader.write_uri(TEST_URI)
    except TagWriteRefused as refusal:
        if refusal.reason == "not_blank":
            pytest.skip(
                "the tag already carries a URI, and overwriting is refused by default — "
                "use a blank tag, or run the overwrite test deliberately"
            )
        raise
    assert result.read_back_url == TEST_URI, (
        f"wrote {TEST_URI!r} and the tag read back {result.read_back_url!r}"
    )


def test_a_written_tag_then_identifies_by_its_uri(
    reader: TagSource, request: pytest.FixtureRequest
) -> None:
    """The point of writing at all: the next tap resolves without the UID table."""
    _require_write_opt_in(request)
    read = reader.poll()
    if read is None:
        pytest.skip("no tag in the field")
    if read.ndef_url != TEST_URI:
        pytest.skip("this tag does not carry the test URI; run the write test first")
    assert identify(read).via == VIA_NDEF


def test_a_tag_that_already_has_a_uri_is_not_overwritten_by_accident(
    reader: TagSource, request: pytest.FixtureRequest
) -> None:
    """Refusing costs a toggle; overwriting costs a drawer that answers to another
    drawer's id. `agent.tags.NOT_BLANK` is the whole comment on why."""
    _require_write_opt_in(request)
    assert isinstance(reader, WritableTagSource)
    read = reader.poll()
    if read is None or not read.ndef_url:
        pytest.skip("need a tag that already carries a URI")
    with pytest.raises(TagWriteRefused) as refused:
        reader.write_uri("https://almagest.lan/s/OTHER001")
    assert refused.value.reason == "not_blank"


# -- the reader itself -----------------------------------------------------


def test_a_pulled_cable_is_a_reader_fault_not_an_unreadable_tag() -> None:
    """Cannot be automated — unplugging is the test — but the distinction is the
    one `TagSourceError` exists for, so it is written down where the checklist is.

    Pull the cable mid-run and the next `poll()` must raise `TagSourceError`,
    not return `None`. `None` would mean "the bench is clear" and the kiosk would
    show an idle screen for a reader that is gone.
    """
    pytest.skip("manual: pull the cable during a run and watch for TagSourceError")


def test_a_second_opener_is_refused_rather_than_corrupting_the_session(
    reader: TagSource, port: str
) -> None:
    """Two openers of one CDC node is silent corruption, so it must fail loudly.

    `SerialFlipperLink` passes `exclusive=True` for this: without it Linux hands
    out a second handle happily, and `open_serial` then writes `\r` and the
    start-RPC incantation into the port this module's session is already
    framing — which `session.open_serial`'s own docstring calls unrecoverable
    for the life of the session.

    Takes `reader` so the session is definitely open while this runs; that is
    the condition under test, not a side effect to be avoided.
    """
    from agent.flipper.link import FlipperLinkError, SerialFlipperLink

    with pytest.raises((FlipperLinkError, OSError)) as failure:
        SerialFlipperLink(port).close()
    # A message that does not name the device sends someone to the wrong cable
    # when two readers are attached.
    assert port in str(failure.value)
