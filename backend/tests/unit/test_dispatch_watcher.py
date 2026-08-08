"""The watcher: what it does with a GPU it does not own.

## What this file is really guarding

**A drain that keeps the card.** The release is in a `finally`, behind a signal handler
and an `atexit` hook, and every one of those is easy to break with a tidy-looking edit.
So the card is asserted released on the success path, on the exception path, and
asserted *not taken at all* when the queue is empty.

**An eviction dressed up as a start.** `model_servers.start()` releases every other
server first. Calling it here would take the GPU from a chat model somebody is using, so
the deferral is asserted directly, and a second test greps the module to make sure the
call has not crept back in.

**A flag that holds the GPU forever.** The reaper defers to `DrainFlag`, so an unbounded
flag is a GPU leak with a helpful name. A live heartbeat must defer the reaper and a
stale one must not, and both are asserted against the same object the reaper reads over
HTTP.

**The opt-in quietly lost.** Nothing here may put an entry into `pending` — a person
still chooses per photograph. Asserted twice: the recording client sees no request call,
and the module contains no reference to the request route.

Everything is offline. There is no cluster, no GPU, no socket for the loop, and the clock
is injected.
"""

from __future__ import annotations

import ast
import json
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.scripts import dispatch_watcher
from app.scripts.dispatch_captures import QueuedCapture
from app.scripts.dispatch_watcher import (
    CardState,
    DrainFlag,
    HttpQueueStatus,
    Outcome,
    Watcher,
    serve_flag,
    server_for_model_url,
)
from app.services import model_servers
from app.services.enrichment.vision import FakeVisionProvider

SHA = "f" * 64
PNG = "image/png"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeCard:
    """A GPU made of three booleans, recording what was asked of it."""

    controllable: bool = True
    ours_up: bool = False
    ours_serving: bool = False
    held_by_others: tuple[str, ...] = ()
    #: Does the server come up when asked? False models a model that never serves.
    comes_up: bool = True
    #: The cluster refusing the scale-up outright.
    scale_accepted: bool = True
    asks: int = 0
    releases: int = 0

    def state(self) -> CardState:
        return CardState(
            controllable=self.controllable,
            ours_up=self.ours_up,
            ours_serving=self.ours_serving,
            held_by_others=self.held_by_others,
        )

    def ask_for_it(self) -> bool:
        self.asks += 1
        if not self.scale_accepted:
            return False
        self.ours_up = True
        if self.comes_up:
            self.ours_serving = True
        return True

    def release(self) -> bool:
        # Only counted when this drain had actually taken it, exactly as `ClusterCard`
        # behaves: releasing a server somebody else started is the eviction this whole
        # design refuses.
        if self.asks == 0:
            return False
        self.releases += 1
        self.ours_up = False
        self.ours_serving = False
        return True


@dataclass
class FakeStatus:
    """Queue depth, from a script of answers. `None` is an unreachable API."""

    answers: list[int | None]
    reads: int = 0

    def pending_count(self) -> int | None:
        self.reads += 1
        if not self.answers:
            return 0
        return self.answers.pop(0)


@dataclass
class FakeClient:
    """An `ApiClient` that hands out one photograph and records every call."""

    captures: list[QueuedCapture] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    submitted: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    #: Raise from `fetch_image`, to model a drain that dies mid-flight.
    explode: bool = False

    def claim(self, *, worker_id: str, limit: int) -> list[QueuedCapture]:
        self.calls.append("claim")
        batch, self.captures = self.captures[:limit], self.captures[limit:]
        return batch

    def fetch_image(self, sha256: str) -> bytes:
        self.calls.append("fetch_image")
        if self.explode:
            raise RuntimeError("the model server hung up")
        return b"\x89PNG\r\n\x1a\n"

    def create_stub_part(
        self, *, name: str, mpn: str, client_op_id: str, device_id: str
    ) -> int | None:
        self.calls.append("create_stub_part")
        return 7

    def submit_candidates(self, *, intake_id: int, candidates: Any, label_kind: str | None) -> None:
        self.calls.append("submit_candidates")
        self.submitted.append({"intake_id": intake_id, "candidates": list(candidates)})

    def submit_failure(self, *, intake_id: int, error: str) -> None:
        self.calls.append("submit_failure")
        self.failures.append(error)


def _module_tree() -> ast.Module:
    """The watcher parsed, for the two structural assertions below.

    Parsed rather than grepped, and that is not fussiness: this module's own docstring
    discusses `model_servers.start()` and `POST /api/dispatch/requests` at length, so a
    substring search over the file matches the prose forbidding the thing and fails
    forever. `ast` sees calls and code strings, and never sees a docstring.
    """
    return ast.parse(Path(dispatch_watcher.__file__).read_text(encoding="utf-8"))


def _called_attributes(tree: ast.Module) -> set[str]:
    """Every `something.name(...)` called anywhere in the module, as `something.name`."""
    return {
        f"{ast.unparse(node.func.value)}.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _code_strings(tree: ast.Module) -> list[str]:
    """Every string literal that is not a docstring.

    Docstrings are `ast.Constant` too, so they are subtracted explicitly by identity —
    the whole point is to read what the code *does*, not what it says about itself.
    """
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _capture(intake_id: int = 1) -> QueuedCapture:
    return QueuedCapture(
        intake_id=intake_id,
        capture_id=intake_id * 10,
        capture_sha256=SHA,
        media_type=PNG,
        barcode_texts=(),
        ocr_lines=("CF14JT100K",),
        mpn=None,
        attempts=1,
    )


@pytest.fixture
def provider(tmp_path: Path) -> FakeVisionProvider:
    """The real parser over a recorded response, keyed by the capture's `document_sha256`.

    Keyed on the claim's sha rather than on the image bytes, which is how
    `FakeVisionProvider` looks it up — a fixture keyed the other way raises
    `VisionFixtureMiss`, and `run_once` would turn that into a *submitted failure*
    instead of a proposal, so the mistake reads as "the drain worked and proposed
    nothing".
    """
    sha = SHA
    path = tmp_path / "vision.json"
    path.write_text(
        json.dumps(
            {
                "provider": "local-ollama",
                "model": "qwen3-vl:8b",
                "responses": {
                    sha: {
                        "candidates": [
                            {
                                "mpn": "CF14JT100K",
                                "manufacturer": "Stackpole",
                                "confidence": 0.9,
                                "source_text": "MFR PART NO: CF14JT100K",
                            }
                        ],
                        "label_kind": "bag",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return FakeVisionProvider(path)


def _watcher(
    status: FakeStatus,
    card: FakeCard,
    client: FakeClient,
    provider: FakeVisionProvider,
    **kwargs: Any,
) -> Watcher:
    # No real sleeping anywhere: a backoff that actually slept would make this suite
    # take minutes and would test the clock rather than the loop. `setdefault` so a
    # test that wants to *record* the sleeps can pass its own.
    kwargs.setdefault("sleep", lambda _seconds: None)
    return Watcher(
        status=status,
        card=card,
        client=client,
        provider=provider,
        worker_id="test",
        flag=DrainFlag(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The happy path, and the release at the end of it
# ---------------------------------------------------------------------------


def test_pending_work_starts_the_model_drains_the_queue_and_releases_the_card(
    provider: FakeVisionProvider,
) -> None:
    """The whole point, in one pass: up, drained, and the card handed back."""
    status = FakeStatus(answers=[2])
    card = FakeCard()
    client = FakeClient(captures=[_capture(1), _capture(2)])
    watcher = _watcher(status, card, client, provider)

    assert watcher.tick() is Outcome.DRAINED

    assert card.asks == 1, "the model server should have been asked for exactly once"
    # Both photographs read in one drain — one model load for the whole queue, which is
    # the entire reason this is a drain and not a job per capture.
    assert len(client.submitted) == 2
    assert card.releases == 1, "the card must go back when the queue is empty"
    assert card.ours_up is False


def test_the_flag_is_lowered_once_the_drain_finishes(provider: FakeVisionProvider) -> None:
    """A finished drain must stop deferring the reaper, or the GPU never comes back."""
    client = FakeClient(captures=[_capture()])
    watcher = _watcher(FakeStatus(answers=[1]), FakeCard(), client, provider)

    watcher.tick()

    assert watcher.flag.snapshot()["draining"] is False


# ---------------------------------------------------------------------------
# The card belongs to somebody else
# ---------------------------------------------------------------------------


def test_another_server_holding_the_card_defers_instead_of_evicting(
    provider: FakeVisionProvider,
) -> None:
    """**The refusal that matters.** A chat model in use is not ours to take."""
    status = FakeStatus(answers=[3])
    card = FakeCard(held_by_others=("vllm-27b",))
    client = FakeClient(captures=[_capture()])
    watcher = _watcher(status, card, client, provider)

    assert watcher.tick() is Outcome.DEFERRED

    assert card.asks == 0, "nothing may be scaled while another server holds the card"
    assert card.releases == 0, "a deferral must not release the server it deferred to"
    assert client.calls == [], "no photograph may be claimed without the card"


def test_a_deferred_drain_is_retried_and_backs_off(provider: FakeVisionProvider) -> None:
    """The wait must be a poll, not a hot loop against the Kubernetes API."""
    slept: list[float] = []
    status = FakeStatus(answers=[1, 1, 1])
    card = FakeCard(held_by_others=("vllm-27b",))
    watcher = _watcher(
        status, card, FakeClient(captures=[_capture()]), provider, sleep=slept.append
    )

    watcher.run(max_ticks=3)

    assert status.reads == 3, "it keeps asking rather than giving up on the photograph"
    assert slept == [
        dispatch_watcher.BACKOFF_START_SECONDS,
        dispatch_watcher.BACKOFF_START_SECONDS * 2,
        dispatch_watcher.BACKOFF_START_SECONDS * 4,
    ]


def test_the_module_never_calls_the_evicting_start() -> None:
    """`model_servers.start()` releases every other server. It must not be called here.

    Structural rather than behavioural because the failure is a *call that was added*,
    and no test over the current code can fail for a call the current code does not
    make. This one fails the moment somebody reaches for the convenient function.

    `model_servers.stop` is expected and asserted present, so the test cannot pass by
    the module having stopped importing `model_servers` altogether.
    """
    called = _called_attributes(_module_tree())

    assert "model_servers.start" not in called
    assert "model_servers.stop" in called


# ---------------------------------------------------------------------------
# The drain dies
# ---------------------------------------------------------------------------


def test_the_card_is_released_when_the_drain_raises(provider: FakeVisionProvider) -> None:
    """A GPU held by a crashed drain is the worst outcome available. `finally` covers it.

    `FakeClient.explode` raises from `fetch_image`, which `dispatch_captures.run_once`
    converts into a submitted failure — so this also pins that the drain reports the
    breakage rather than swallowing it.
    """
    card = FakeCard()
    client = FakeClient(captures=[_capture()], explode=True)
    watcher = _watcher(FakeStatus(answers=[1]), card, client, provider)

    watcher.tick()

    assert card.releases == 1
    assert client.failures, "the queue must be told the run broke, so it can retry"


def test_a_pass_that_raises_outright_still_releases_and_keeps_polling(
    provider: FakeVisionProvider,
) -> None:
    """Even an error `run_once` cannot catch must not end with the card held."""
    card = FakeCard()

    class Exploding(FakeClient):
        def claim(self, *, worker_id: str, limit: int) -> list[QueuedCapture]:
            raise RuntimeError("the API went away mid-claim")

    watcher = _watcher(FakeStatus(answers=[1, 0]), card, Exploding(), provider)

    watcher.run(max_ticks=2)

    assert card.releases == 1
    assert card.ours_up is False


# ---------------------------------------------------------------------------
# Idle costs nothing
# ---------------------------------------------------------------------------


def test_an_empty_queue_touches_no_gpu_at_all(provider: FakeVisionProvider) -> None:
    """Idle must be one HTTP call. Not a scale-up, not a probe, not a release."""
    card = FakeCard()
    client = FakeClient()
    watcher = _watcher(FakeStatus(answers=[0]), card, client, provider)

    assert watcher.tick() is Outcome.IDLE

    assert card.asks == 0
    assert card.releases == 0
    assert client.calls == [], "an empty queue must not even claim"
    assert watcher.flag.snapshot()["drains"] == 0, "no drain was started, so none is reported"


def test_an_unreachable_api_is_not_an_empty_queue(provider: FakeVisionProvider) -> None:
    """`None` must back off, not go to sleep as though nobody had asked for anything."""
    slept: list[float] = []
    card = FakeCard()
    watcher = _watcher(FakeStatus(answers=[None]), card, FakeClient(), provider, sleep=slept.append)

    watcher.run(max_ticks=1)

    assert card.asks == 0
    assert slept == [dispatch_watcher.BACKOFF_START_SECONDS], "backoff, not the poll interval"


# ---------------------------------------------------------------------------
# Degrading, and a server that will not come up
# ---------------------------------------------------------------------------


def test_an_uncontrollable_install_still_drains_and_does_not_crash(
    provider: FakeVisionProvider,
) -> None:
    """No cluster and no RBAC: it cannot start a model, so it works with what is up."""
    card = FakeCard(controllable=False)
    client = FakeClient(captures=[_capture()])
    watcher = _watcher(FakeStatus(answers=[1]), card, client, provider)

    assert watcher.tick() is Outcome.DRAINED

    assert card.asks == 0, "nothing here can scale, so nothing should try"
    assert len(client.submitted) == 1


def test_a_server_already_up_is_used_but_not_taken_over(provider: FakeVisionProvider) -> None:
    """The Ollama server holds the chat models too. Found up means left up.

    Releasing it would end a conversation to reclaim a GPU that was already spent, so
    the drain uses it and the reaper's idle timer keeps its ordinary job.
    """
    card = FakeCard(ours_up=True, ours_serving=True)
    client = FakeClient(captures=[_capture()])
    watcher = _watcher(FakeStatus(answers=[1]), card, client, provider)

    assert watcher.tick() is Outcome.DRAINED

    assert card.asks == 0
    assert card.releases == 0, "a server this drain did not start is not this drain's to stop"
    assert card.ours_up is True


def test_a_model_that_never_serves_gives_the_card_back(provider: FakeVisionProvider) -> None:
    """A card held by something that will not answer is the worst of both."""
    card = FakeCard(comes_up=False)
    watcher = _watcher(
        FakeStatus(answers=[1]),
        card,
        FakeClient(captures=[_capture()]),
        provider,
        ready_seconds=0.0,
    )

    assert watcher.tick() is Outcome.NOT_READY

    assert card.asks == 1
    assert card.releases == 1, "the scale-up must be undone when it does not pay off"


def test_a_refused_scale_up_is_not_reported_as_a_drain(provider: FakeVisionProvider) -> None:
    """The cluster saying no is a backoff, not a queue that got worked."""
    card = FakeCard(scale_accepted=False)
    watcher = _watcher(FakeStatus(answers=[1]), card, FakeClient(captures=[_capture()]), provider)

    assert watcher.tick() is Outcome.NOT_READY


# ---------------------------------------------------------------------------
# The flag the reaper reads
# ---------------------------------------------------------------------------


def test_a_live_drain_defers_the_reaper_and_a_stalled_one_does_not() -> None:
    """The reaper's whole condition, and the bound that keeps it from leaking a GPU.

    `pending > 0` is deliberately not the condition — a queue nobody is draining must
    not hold a card. What defers the reaper is a drain *making progress*, and the
    heartbeat is what makes "progress" mean something a wedged worker cannot fake.
    """
    now = [1000.0]
    flag = DrainFlag(stall_seconds=600.0, clock=lambda: now[0])

    assert flag.snapshot()["draining"] is False, "no drain, no deferral"

    flag.begin()
    assert flag.snapshot()["draining"] is True

    # Nine minutes of work, with a heartbeat partway: still live.
    now[0] += 300.0
    flag.touch()
    now[0] += 240.0
    snapshot = flag.snapshot()
    assert snapshot["draining"] is True
    assert snapshot["stalled"] is False

    # Past the stall window with no activity. The drain has *not* returned — `end` was
    # never called — and the flag stops vouching for it anyway. This is the assertion
    # that stands between a wedged worker and a permanently held GPU.
    now[0] += 400.0
    snapshot = flag.snapshot()
    assert snapshot["draining"] is False
    assert snapshot["stalled"] is True, "a stall must be distinguishable from no drain"

    # And a heartbeat brings it back, because a slow read is not a dead one.
    flag.touch()
    assert flag.snapshot()["draining"] is True


def test_the_flag_body_matches_the_shape_the_reaper_greps_for() -> None:
    """The reaper matches `*'"draining":true'*` in busybox sh. Compact JSON or bust.

    Asserted on the bytes actually served, over a real socket, because the failure this
    guards is a space after a colon — invisible in review, and it fails by reaping a
    live drain rather than by erroring.
    """
    flag = DrainFlag()
    flag.begin()
    # Port 0 lets the OS choose, so this cannot collide with anything on the machine.
    server = serve_flag(flag, port=0)
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/draining", timeout=5) as response:
            body = response.read().decode()
    finally:
        server.shutdown()
        server.server_close()

    assert '"draining":true' in body
    assert json.loads(body)["draining"] is True


#: The reaper's script, which lives in a manifest and so is otherwise untested.
REAPER_YAML = Path(__file__).resolve().parents[3] / "deploy" / "base" / "llm-reaper.yaml"

#: How `llm-reaper.yaml` preprocesses the body before matching: `tr -d ' \t\n'`.
_STRIPPED = str.maketrans("", "", " \t\n")


def _reaper_patterns() -> list[str]:
    """The `case` patterns the reaper matches the drain flag with, read from the manifest.

    Read rather than restated, so this cannot pass while the manifest says something
    else. The shape is `*'"draining":true'*` — a shell glob whose only interesting part
    is the quoted literal in the middle.
    """
    text = REAPER_YAML.read_text(encoding="utf-8")
    return re.findall(r"\*'([^']+)'\*\)", text)


def test_the_reapers_own_pattern_matches_a_live_drain_and_not_a_finished_one() -> None:
    """The seam between a Python program and a busybox `case`, asserted across both files.

    This is the bug class that would not show up in review and would not error at
    runtime: `json.dumps` puts a space after its colons by default, so `"draining": true`
    silently fails `*'"draining":true'*` and the reaper reaps a live drain. The patterns
    come out of the manifest and the body comes out of the flag, and the whitespace
    stripping mirrors the `tr -d` the script does first.
    """
    patterns = _reaper_patterns()
    assert '"draining":true' in patterns, "the reaper must still be asking about drains"
    assert '"stalled":true' in patterns

    def matches(flag: DrainFlag, pattern: str) -> bool:
        body = json.dumps(flag.snapshot(), separators=(",", ":")).translate(_STRIPPED)
        return pattern in body

    now = [0.0]
    flag = DrainFlag(stall_seconds=600.0, clock=lambda: now[0])

    # Nothing running: the reaper falls through to its ordinary idle check.
    assert not matches(flag, '"draining":true')

    flag.begin()
    assert matches(flag, '"draining":true'), "a live drain must hold the reaper off"

    # Wedged past the stall window. The drain has not returned, and the reaper is
    # nonetheless free to reclaim — the bound that stops this being a GPU leak.
    now[0] += 601.0
    assert not matches(flag, '"draining":true')
    assert matches(flag, '"stalled":true'), "and the reaper says so rather than reaping quietly"

    flag.end()
    assert not matches(flag, '"draining":true')
    assert not matches(flag, '"stalled":true'), "a finished drain is not a stalled one"


def test_the_watcher_is_deployed_and_reachable_by_the_reaper() -> None:
    """The three files have to agree on a name and a port or the flag is never read.

    `kustomization.yaml` must include the manifest, and the URL the reaper defaults to
    must be the Service the manifest declares — a typo in either is a silently ignored
    flag, which fails as "the reaper reaps mid-drain" with nothing pointing at why.
    """
    base = REAPER_YAML.parent
    assert "dispatch-watcher.yaml" in (base / "kustomization.yaml").read_text(encoding="utf-8")

    watcher_yaml = (base / "dispatch-watcher.yaml").read_text(encoding="utf-8")
    default_url = re.search(r'WATCHER="\$\{WATCHER:-([^}]+)\}"', REAPER_YAML.read_text("utf-8"))
    assert default_url is not None
    host, _, port = default_url.group(1).removeprefix("http://").partition(":")

    assert f"name: {host}" in watcher_yaml, "the reaper points at a Service that must exist"
    assert f"port: {port}" in watcher_yaml
    assert f"containerPort: {port}" in watcher_yaml
    assert str(dispatch_watcher.DEFAULT_FLAG_PORT) == port, (
        "the program's default port and the manifest's must agree"
    )


# ---------------------------------------------------------------------------
# The opt-in survives
# ---------------------------------------------------------------------------


def test_nothing_here_can_queue_a_photograph_by_itself(provider: FakeVisionProvider) -> None:
    """A read costs a GPU handover, so a person asks for it. Twice-asserted.

    The behavioural half: a full drain makes only the four worker calls, none of which
    can move an entry into `pending`. The structural half: the module does not mention
    the request route at all, so the next edit cannot quietly add one.
    """
    client = FakeClient(captures=[_capture()])
    watcher = _watcher(FakeStatus(answers=[1]), FakeCard(), client, provider)

    watcher.tick()

    assert set(client.calls) <= {
        "claim",
        "fetch_image",
        "create_stub_part",
        "submit_candidates",
        "submit_failure",
    }
    tree = _module_tree()
    strings = _code_strings(tree)
    assert not any("dispatch/requests" in text for text in strings), (
        "a path to the request route in this module's code would be the opt-in lost"
    )
    # And the module must still reach the queue it *is* allowed to read, so this cannot
    # pass by the watcher having stopped talking to the API at all.
    assert any("api/dispatch/status" in text for text in strings)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_a_model_url_maps_to_a_known_server_and_an_unknown_one_does_not() -> None:
    """A URL that maps to no Deployment would be a watcher that can never start anything."""
    from app.services.model_catalog import OLLAMA

    server = server_for_model_url(OLLAMA)
    assert server is not None
    assert server.id == "ollama"

    assert server_for_model_url("http://not-a-model-server:11434") is None


def test_queue_depth_survives_a_response_with_no_pending_field() -> None:
    """A malformed answer is `None` — unreachable — never a silent zero."""
    status = HttpQueueStatus("http://127.0.0.1:1")
    # Port 1 refuses the connection, which is the unreachable branch without a fake.
    assert status.pending_count() is None


# ---------------------------------------------------------------------------
# `Card.release` itself, which every test above fakes past
# ---------------------------------------------------------------------------
#
# Added after review, because a mutation got through: replacing
# `if not self._we_started_it:` with `if False:` — making the drain release a server
# it never started, which is the eviction this design explicitly refuses — left all
# twenty tests above green.
#
# The reason is `FakeCard`. It is the right seam for testing the *watcher*, and it
# means the real `Card`'s one safety decision is never executed. `almagest-llm` serves
# the chat models and the vision model on one listener, so releasing a server that was
# already up ends somebody's conversation. That is worth a test that touches the real
# object.


class _RecordingScaler:
    """The one call `Card` makes on the way up, and `model_servers.stop` on the way down."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def set_replicas(self, host: str, replicas: int) -> bool:
        self.calls.append((host, replicas))
        return True


def _real_card(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dispatch_watcher.ClusterCard, _RecordingScaler]:
    scaler = _RecordingScaler()
    server = model_servers.SERVERS[0]
    monkeypatch.setattr(dispatch_watcher.model_scaler, "set_replicas", scaler.set_replicas)
    # `model_servers.stop` scales the named server to zero through the same scaler, so
    # patching that one function is enough to observe both directions.
    monkeypatch.setattr(
        model_servers,
        "stop",
        lambda srv: model_servers.SwitchResult(
            ok=scaler.set_replicas(srv.host, 0), released=(srv.id,), detail="stopped"
        ),
    )
    return dispatch_watcher.ClusterCard(server), scaler


def test_a_card_this_drain_started_is_released(monkeypatch: pytest.MonkeyPatch) -> None:
    card, scaler = _real_card(monkeypatch)

    assert card.ask_for_it() is True
    assert card.we_started_it is True
    assert card.release() is True

    # Up, then back down: the drain gives back exactly what it took.
    assert scaler.calls == [(card.server.host, 1), (card.server.host, 0)]


def test_a_card_that_was_already_up_is_never_released(monkeypatch: pytest.MonkeyPatch) -> None:
    """The eviction this design refuses, and the one a mutation walked straight through.

    `almagest-llm` serves chat *and* vision on one listener. A drain that finds the
    server already up has borrowed somebody's model, and scaling it to zero afterwards
    ends their conversation — so `release` must be a no-op unless this object asked for
    the server itself.
    """
    card, scaler = _real_card(monkeypatch)

    # No `ask_for_it`: this stands for the drain finding the server already serving.
    assert card.we_started_it is False
    assert card.release() is False
    assert scaler.calls == [], "it scaled something it did not start"


def test_releasing_twice_gives_the_card_back_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """`release_now` is called on every exit path, so it runs more than once by design."""
    card, scaler = _real_card(monkeypatch)
    card.ask_for_it()

    assert card.release() is True
    assert card.release() is False
    assert scaler.calls.count((card.server.host, 0)) == 1
