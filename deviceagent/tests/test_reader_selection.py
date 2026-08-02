"""Which reader `build_source` builds, and what a wrong answer would cost.

There are two drivers now, and neither can be instantiated on this machine. What
*can* be checked is the dispatch: that the setting is honoured, that `--reader`
overrides it, that `--fake` still beats both, and that neither driver module is
imported until it is the one chosen — a station with an RC522 must not need the
CircuitPython stack installed, and the Pi image is small on purpose.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable

import pytest

from agent.config import AgentSettings
from agent.devices import DeviceRegistry
from agent.fake_tags import FakeTagSource, load_script
from agent.main import build_source, run
from agent.no_reader import NoTagSource
from agent.tags import READS_BOTH, TagCapabilities, TagSource
from tests.fake_api import FakeStationApi


class _Recorder:
    """Stands in for a driver class: records how it was constructed."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    @property
    def capabilities(self) -> TagCapabilities:
        """Present because `TagSource` requires it since ADR 0014, and the
        assertion below is an `isinstance` against that protocol — a stub
        missing it is not a `TagSource`, which is precisely what that check is
        for. The value is irrelevant here; what is under test is the dispatch."""
        return READS_BOTH

    def poll(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[_Recorder]]:
    """Both driver classes replaced, so a wrong dispatch is a visibly empty list
    rather than an exception about a missing serial port."""
    built: dict[str, list[_Recorder]] = {"pn532": [], "rc522": []}

    def make(kind: str) -> Callable[..., _Recorder]:
        def factory(*args: object, **kwargs: object) -> _Recorder:
            source = _Recorder(args=args, **kwargs)
            built[kind].append(source)
            return source

        return factory

    import agent.nfc_pn532
    import agent.nfc_rc522

    monkeypatch.setattr(agent.nfc_pn532, "Pn532TagSource", make("pn532"))
    monkeypatch.setattr(agent.nfc_rc522, "Rc522TagSource", make("rc522"))
    return built


def test_the_default_reader_is_the_one_plan_md_specifies() -> None:
    assert AgentSettings.model_fields["reader"].default == "pn532"


def test_an_unknown_reader_is_refused_at_config_time() -> None:
    """Rather than at the bench, as an import error naming a module nobody asked
    for."""
    with pytest.raises(ValueError, match="reader"):
        AgentSettings(reader="acr122u")  # type: ignore[arg-type]


def test_the_setting_chooses_the_rc522(recorded: dict[str, list[_Recorder]]) -> None:
    settings = AgentSettings(reader="rc522", rc522_spi_bus=0, rc522_spi_device=1)
    source = build_source(settings, fake=False, script=None)

    assert recorded["rc522"] and not recorded["pn532"]
    assert isinstance(source, TagSource)
    assert recorded["rc522"][0].kwargs["device"] == 1


def test_the_flag_overrides_the_setting(recorded: dict[str, list[_Recorder]]) -> None:
    """A cable swap at the bench should not need an `.env` edit."""
    settings = AgentSettings(reader="pn532")
    build_source(settings, fake=False, script=None, reader="rc522")
    assert recorded["rc522"] and not recorded["pn532"]


def test_fake_still_beats_both(recorded: dict[str, list[_Recorder]]) -> None:
    settings = AgentSettings(reader="rc522")
    source = build_source(settings, fake=True, script=None, reader="rc522")
    assert isinstance(source, FakeTagSource)
    assert not recorded["rc522"] and not recorded["pn532"]


def test_none_means_this_machine_has_no_platform(recorded: dict[str, list[_Recorder]]) -> None:
    """ADR 0014's other deployment: a browser on a machine whose only reader is a
    Flipper on USB. Neither driver may be constructed — opening a serial port
    that is not there exits before the bridge has looked for the one that is."""
    source = build_source(AgentSettings(reader="none"), fake=False, script=None)

    assert isinstance(source, NoTagSource)
    assert isinstance(source, TagSource)
    assert not recorded["pn532"] and not recorded["rc522"]


def test_an_absent_platform_reports_an_empty_field_not_an_unreadable_tag() -> None:
    """The distinction `agent.tags` is built around. `None` is "the bench is
    clear"; `TagRead(None, None)` is "something is here and its tag will not
    read", which would make the kiosk offer to provision a container that is not
    on the platform."""
    source = NoTagSource()
    assert source.poll() is None
    assert source.poll() is None


def test_an_absent_platform_claims_no_capabilities() -> None:
    """It is never adopted into the roster, so these are never published — but a
    reader whose capabilities are a lie where they are unread is how they come
    to be a lie where they are."""
    caps = NoTagSource().capabilities
    assert not caps.reads_uid and not caps.reads_ndef and not caps.writes_ndef


def test_closing_an_absent_platform_is_idempotent() -> None:
    source = NoTagSource()
    source.close()
    source.close()


def test_the_flag_can_select_no_platform_too(recorded: dict[str, list[_Recorder]]) -> None:
    """A PN532 station whose reader has been unplugged for the afternoon should
    be able to run the bridge without an `.env` edit."""
    source = build_source(AgentSettings(reader="pn532"), fake=False, script=None, reader="none")
    assert isinstance(source, NoTagSource)
    assert not recorded["pn532"]


class _SnapshottingRegistry(DeviceRegistry):
    """Records the roster at teardown.

    `run` closes the registry on the way out — which empties it — so reading
    `infos` afterwards reports nothing regardless of what was announced. The
    snapshot has to be taken from inside the lifetime it is describing.
    """

    def __init__(self) -> None:
        super().__init__([])
        self.roster_at_close: tuple[str, ...] = ()

    def close(self) -> None:
        self.roster_at_close = tuple(info.device_id for info in self.infos)
        super().close()


def _roster_after_a_run(source: TagSource) -> tuple[str, ...]:
    """Run the agent briefly against an empty registry and report what it announced."""
    registry = _SnapshottingRegistry()
    asyncio.run(
        asyncio.wait_for(
            run(
                source,
                AgentSettings(ws_port=0, poll_interval_ms=20),
                api=FakeStationApi(),
                max_polls=2,
                registry=registry,
                max_sweeps=1,
            ),
            10.0,
        )
    )
    return registry.roster_at_close


def test_a_station_with_a_reader_announces_it() -> None:
    """The control for the test below: adoption is what puts the platform reader
    in the roster, so a `tag.write` can name it."""
    assert _roster_after_a_run(FakeTagSource(load_script())) == ("station",)


def test_a_station_with_no_platform_announces_nothing() -> None:
    """Absence communicated by absence. A `device.attached` for a reader that can
    neither read nor write would put a dead entry in the PWA's reader chooser,
    and the user would be told to hold a tag against a platform that is not
    there."""
    assert _roster_after_a_run(NoTagSource()) == ()


def test_the_unused_driver_is_never_imported() -> None:
    """Both imports are inside `build_source` for this reason: the Pi installs one
    reader's library, not both, and an import at module scope would make the
    other's absence fatal."""
    for module in ("agent.nfc_pn532", "agent.nfc_rc522"):
        sys.modules.pop(module, None)

    build_source(AgentSettings(reader="rc522"), fake=True, script=None)

    assert "agent.nfc_pn532" not in sys.modules
    assert "agent.nfc_rc522" not in sys.modules
