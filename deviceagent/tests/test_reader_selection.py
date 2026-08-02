"""Which reader `build_source` builds, and what a wrong answer would cost.

There are two drivers now, and neither can be instantiated on this machine. What
*can* be checked is the dispatch: that the setting is honoured, that `--reader`
overrides it, that `--fake` still beats both, and that neither driver module is
imported until it is the one chosen — a station with an RC522 must not need the
CircuitPython stack installed, and the Pi image is small on purpose.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

import pytest

from agent.config import AgentSettings
from agent.fake_tags import FakeTagSource
from agent.main import build_source
from agent.tags import READS_BOTH, TagCapabilities, TagSource


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


def test_the_unused_driver_is_never_imported() -> None:
    """Both imports are inside `build_source` for this reason: the Pi installs one
    reader's library, not both, and an import at module scope would make the
    other's absence fatal."""
    for module in ("agent.nfc_pn532", "agent.nfc_rc522"):
        sys.modules.pop(module, None)

    build_source(AgentSettings(reader="rc522"), fake=True, script=None)

    assert "agent.nfc_pn532" not in sys.modules
    assert "agent.nfc_rc522" not in sys.modules
