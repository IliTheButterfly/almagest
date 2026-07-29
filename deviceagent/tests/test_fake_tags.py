"""The fake reader itself. It stands in for hardware, so it has to be trusted."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.fake_tags import FakeTagSource, ScriptedPoll, load_script
from agent.tags import TagRead, TagSource, TagSourceError


def test_the_fake_satisfies_the_protocol_the_real_reader_implements() -> None:
    """Structural, so the fake cannot drift from `Pn532TagSource`'s contract while
    every test above it keeps passing."""
    assert isinstance(FakeTagSource(), TagSource)


def test_the_packaged_script_ships_inside_the_package() -> None:
    """`load_script()` with no argument must work from an installed wheel, because
    that is what `--fake` calls on the Pi."""
    assert len(load_script()) > 10


def test_the_script_admits_it_is_not_recorded() -> None:
    """Honesty enforced by a test: no PN532 has ever been attached to this code,
    and the fixture must keep saying so until one has been."""
    raw = json.loads(
        (Path(__file__).resolve().parents[1] / "agent/fixtures/scripted_session.json").read_text(
            encoding="utf-8"
        )
    )
    assert "HAND-WRITTEN, NOT RECORDED" in raw["description"]


def test_polls_are_replayed_in_order() -> None:
    script = (
        ScriptedPoll(read=None),
        ScriptedPoll(read=TagRead(uid="04AA", ndef_url=None)),
    )
    source = FakeTagSource(script)
    assert source.poll() is None
    assert source.poll() == TagRead(uid="04AA", ndef_url=None)


def test_a_scripted_fault_raises_the_readers_error(tmp_path: Path) -> None:
    document = {"description": "x", "polls": [{"error": "cable"}]}
    path = tmp_path / "s.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    source = FakeTagSource(load_script(path))
    with pytest.raises(TagSourceError, match="cable"):
        source.poll()


def test_an_exhausted_script_reads_as_an_empty_platform(tmp_path: Path) -> None:
    """Not an error. `--fake` is meant to be left running, and a station that
    stops answering looks broken to the person at the bench."""
    source = FakeTagSource((ScriptedPoll(read=None),))
    source.poll()
    assert source.exhausted
    for _ in range(5):
        assert source.poll() is None


def test_repeat_loops_the_script_for_ever() -> None:
    script = (ScriptedPoll(read=TagRead(uid="04AA", ndef_url=None)), ScriptedPoll(read=None))
    source = FakeTagSource(script, repeat=True)
    assert [source.poll() for _ in range(4)] == [
        TagRead(uid="04AA", ndef_url=None),
        None,
        TagRead(uid="04AA", ndef_url=None),
        None,
    ]


def test_an_empty_script_is_refused() -> None:
    with pytest.raises(ValueError, match="no polls"):
        FakeTagSource(())


def test_polling_after_close_is_a_reader_error() -> None:
    """The same class the real driver raises, so the poll loop's handling of a
    closed port is exercised by the fake."""
    source = FakeTagSource()
    source.close()
    with pytest.raises(TagSourceError):
        source.poll()
