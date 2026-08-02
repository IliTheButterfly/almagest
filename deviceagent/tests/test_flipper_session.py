"""The Flipper path end to end, against a Flipper made of software.

`FlipperRpc` and `FlipperTagSource` run **unmodified** here — what is under test
is the production conversation, not a mock of it. The fake speaks real protobuf
frames and the real Antlia grammar against a real simulated tag, so a write in
this file goes through `agent.ndef`'s encoder, lands in user memory, and is read
back out through the parser.

What this cannot prove: that a real Flipper agrees. See ADR 0014's "unverified"
section. What it does prove is everything that is a decision rather than a fact
about hardware — the ack-then-answer ordering, the unprompted `HELLO`, the
capability handshake, and that every refusal reaches the caller with the same
`reason` a PN532 would have used.
"""

from __future__ import annotations

import pytest

from agent import tags
from agent.fake_tags import FakeWritableTagSource
from agent.flipper import antlia, proto, session
from agent.flipper.fake import DEFAULT_BANNER, FakeFlipperLink
from agent.flipper.session import FlipperRpc, FlipperTagSource
from agent.tags import TagRead, TagSource, TagSourceError, TagWriteRefused, WritableTagSource

URL = "https://almagest.lan/s/4K7T92M8"
OTHER_URL = "https://almagest.lan/s/9ZQR31VT"


def connect(link: FakeFlipperLink) -> FlipperTagSource:
    """What `session.open_serial` does, minus the serial port."""
    rpc = FlipperRpc(link)
    rpc.ping()
    hello = rpc.launch_antlia()
    return FlipperTagSource(rpc, hello)


class TestLaunch:
    def test_the_bridge_launches_antlia_into_rpc_mode(self) -> None:
        """The auto-launch. Nobody touches the Flipper's screen, and the argument
        is what selects bridge mode over keyboard-wedge mode (ADR 0014)."""
        link = FakeFlipperLink()
        connect(link)
        assert link.launched == [(proto.ANTLIA_FAP_PATH, proto.RPC_LAUNCH_ARGS)]
        assert link.app_running

    def test_capabilities_come_from_the_handshake_not_a_constant(self) -> None:
        writes = connect(FakeFlipperLink(can_write=True))
        reads = connect(FakeFlipperLink(can_write=False))
        assert writes.capabilities.writes_ndef
        assert not reads.capabilities.writes_ndef
        # Both read both carriers; only the write differs.
        assert reads.capabilities.reads_uid and reads.capabilities.reads_ndef

    def test_a_missing_fap_is_a_clear_error_not_a_silent_reader(self) -> None:
        with pytest.raises(TagSourceError, match="Is Antlia installed"):
            connect(FakeFlipperLink(antlia_installed=False))

    def test_an_app_that_starts_but_never_says_hello_is_refused(self) -> None:
        """The launch ack is not readiness. A bridge that treated it as readiness
        would send READ into a void."""
        link = FakeFlipperLink(answers_hello=False)
        rpc = FlipperRpc(link, timeout_s=0.01)
        rpc.ping()
        with pytest.raises(TagSourceError, match="never said HELLO"):
            rpc.launch_antlia(launch_timeout_s=0.01)

    def test_a_mismatched_protocol_version_is_refused_at_attach(self) -> None:
        """A stale `.fap` must fail loudly here rather than answer nothing later."""
        link = FakeFlipperLink(protocol_version=antlia.PROTOCOL_VERSION + 1)
        with pytest.raises(TagSourceError, match="protocol"):
            connect(link)

    def test_it_satisfies_the_tag_source_protocols(self) -> None:
        source = connect(FakeFlipperLink())
        assert isinstance(source, TagSource)
        assert isinstance(source, WritableTagSource)


class TestReading:
    def test_an_empty_field_reads_as_none(self) -> None:
        source = connect(FakeFlipperLink(FakeWritableTagSource(present=False)))
        assert source.poll() is None

    def test_a_written_tag_reports_both_carriers(self) -> None:
        tag = FakeWritableTagSource(uid="04A2B3C4D5E680", url=URL)
        source = connect(FakeFlipperLink(tag))
        assert source.poll() == TagRead(uid="04A2B3C4D5E680", ndef_url=URL)

    def test_a_blank_tag_reports_a_uid_and_no_url(self) -> None:
        """The normal state of a container before it is provisioned, and the one
        the walk offers to write. It must not look like an empty field."""
        source = connect(FakeFlipperLink(FakeWritableTagSource(url=None)))
        read = source.poll()
        assert read is not None
        assert read.uid is not None
        assert read.ndef_url is None

    def test_a_uid_only_tag_survives_the_absent_sentinel(self) -> None:
        source = connect(FakeFlipperLink(FakeWritableTagSource(uid=None, url=URL)))
        assert source.poll() == TagRead(uid=None, ndef_url=URL)


class TestWriting:
    def test_a_write_round_trips_through_real_ndef_bytes(self) -> None:
        tag = FakeWritableTagSource(url=None)
        source = connect(FakeFlipperLink(tag))
        assert source.write_uri(URL).read_back_url == URL
        assert tag.url == URL
        assert source.poll() == TagRead(uid=tag.uid, ndef_url=URL)

    def test_a_non_blank_tag_is_refused_with_the_shared_reason(self) -> None:
        """The same `reason` a PN532 produces, so the PWA has one table."""
        source = connect(FakeFlipperLink(FakeWritableTagSource(url=OTHER_URL)))
        with pytest.raises(TagWriteRefused) as raised:
            source.write_uri(URL)
        assert raised.value.reason == tags.NOT_BLANK

    def test_overwrite_is_a_different_verb_on_the_wire(self) -> None:
        """`WRITE!` rather than a trailing flag, so an Antlia that parsed only
        `WRITE` could never be tricked into overwriting by a token it ignored."""
        link = FakeFlipperLink(FakeWritableTagSource(url=OTHER_URL))
        source = connect(link)
        assert source.write_uri(URL, overwrite=True).read_back_url == URL
        assert link.commands[-1].startswith(antlia.WRITE_OVERWRITE + " ")

    def test_an_empty_field_refuses_with_no_tag(self) -> None:
        source = connect(FakeFlipperLink(FakeWritableTagSource(present=False)))
        with pytest.raises(TagWriteRefused) as raised:
            source.write_uri(URL)
        assert raised.value.reason == tags.NO_TAG

    def test_a_write_that_does_not_land_is_caught_by_the_read_back(self) -> None:
        """The whole reason `TagWrite` carries a URI instead of a boolean. The
        tag acknowledges the write and keeps its old contents; only reading it
        back through the same reader reveals that (ADR 0012)."""
        tag = FakeWritableTagSource(url=None, writes_land=False)
        source = connect(FakeFlipperLink(tag))
        with pytest.raises(TagWriteRefused) as raised:
            source.write_uri(URL)
        assert raised.value.reason == tags.READ_BACK_FAILED
        assert tag.writes == [URL], "the write was attempted, and did not take"

    def test_a_read_only_build_refuses_before_touching_the_wire(self) -> None:
        link = FakeFlipperLink(can_write=False)
        source = connect(link)
        before = list(link.commands)
        with pytest.raises(TagWriteRefused) as raised:
            source.write_uri(URL)
        assert raised.value.reason == tags.UNSUPPORTED
        assert link.commands == before, "a known-incapable reader is not asked"

    def test_a_payload_too_large_for_the_tag_is_refused(self) -> None:
        tag = FakeWritableTagSource(url=None, user_pages=4)
        source = connect(FakeFlipperLink(tag))
        with pytest.raises(TagWriteRefused) as raised:
            source.write_uri(URL)
        assert raised.value.reason == tags.TOO_LONG
        assert tag.writes == [], "nothing is written when it will not fit"


class TestConversation:
    def test_the_ack_is_not_mistaken_for_the_answer(self) -> None:
        """A data exchange is acknowledged by the *Flipper* and answered by the
        *app*, separately and out of band. This is the ordering a simpler mock
        would hide."""
        link = FakeFlipperLink()
        source = connect(link)
        assert source.poll() is not None
        assert link.commands == [antlia.READ]

    def test_a_silent_flipper_times_out_rather_than_hanging(self) -> None:
        link = FakeFlipperLink()
        source = connect(link)
        link.mute = True  # the app crashed, or the cable is half out
        source._rpc._timeout_s = 0.01
        with pytest.raises(TagSourceError, match="did not answer"):
            source.poll()

    def test_an_unknown_verb_from_antlia_is_loud(self) -> None:
        """Ignoring an unparseable line would leave the bridge waiting for a
        reply that already arrived in a form it discarded."""
        with pytest.raises(antlia.AntliaProtocolError):
            antlia.parse_reply("SPLINE 4K7T92M8")

    def test_an_err_reason_outside_the_vocabulary_is_refused(self) -> None:
        """The reason set is closed so the PWA can branch on it. A new one has to
        be added to `agent.tags` and here, deliberately."""
        with pytest.raises(antlia.AntliaProtocolError):
            antlia.parse_reply("ERR kaput something went wrong")

    def test_close_asks_the_app_to_exit(self) -> None:
        """A Flipper left in bridge mode with no host looks, on the device, like
        a frozen app whose only way out is the back button."""
        link = FakeFlipperLink()
        source = connect(link)
        source.close()
        assert link.exited
        assert link.closed


class TestBannerDrain:
    def test_the_cli_banner_does_not_desynchronise_the_decoder(self) -> None:
        """A fresh CDC session lands on the text CLI. Feeding its greeting to
        `FrameDecoder` would corrupt it for the life of the session."""
        link = FakeFlipperLink(banner=DEFAULT_BANNER)
        assert link.read(0.1), "the fake greets like a real Flipper"
        link.write(b"\r")
        session._drain(link, 0.01)
        link.write(session.START_RPC)
        session._drain(link, 0.01)

        rpc = FlipperRpc(link)
        rpc.ping()  # would raise if the decoder had eaten banner bytes
        hello = rpc.launch_antlia()
        assert hello.version == antlia.PROTOCOL_VERSION


class TestVocabularyStaysShared:
    def test_antlia_reasons_are_exactly_the_tags_module_s(self) -> None:
        """Two copies of a vocabulary is how they drift. This is the assertion
        that keeps a Flipper and a PN532 refusing a write with the same word."""
        assert {
            tags.NO_TAG,
            tags.NOT_BLANK,
            tags.TOO_LONG,
            tags.UNSUPPORTED,
            tags.READ_BACK_FAILED,
        } == antlia.REASONS
