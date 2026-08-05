"""Configuration, and the one setting that is refused rather than documented."""

from __future__ import annotations

import pytest

from agent.config import AgentSettings


def default(field: str) -> object:
    """The declared default, not what this machine's environment resolves to.

    `make bootstrap` copies `.env.example` to `.env`, so an instantiated
    `AgentSettings()` here would be asserting against the developer's own file and
    would fail for someone who had legitimately retuned their station.
    """
    return AgentSettings.model_fields[field].default


def test_the_defaults_are_plan_mds_identify_budget() -> None:
    """Five tries at a 300 ms cadence bounds PLAN.md's "~5 tries / 1.5 s". The two
    numbers are only meaningful together, so a change to one that quietly changes
    the budget should have to change this line.

    A bound, not an equality: `tests/test_poll_loop.py` is where the arithmetic
    lives, and it is a bound because a poll that outlasts its interval makes the
    cadence the read — which no hardware has measured."""
    assert default("identify_polls") == 5
    assert default("poll_interval_ms") == 300
    assert AgentSettings(identify_polls=5, poll_interval_ms=300).poll_interval_s == 0.3


def test_the_default_bind_is_the_loopback() -> None:
    assert default("ws_host") == "127.0.0.1"


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.53", "::1", "localhost"])
def test_loopback_addresses_are_accepted(host: str) -> None:
    assert AgentSettings(ws_host=host).ws_host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "almagest.aether.lan", ""])
def test_a_non_loopback_bind_is_refused_at_startup(host: str) -> None:
    """Refused rather than documented, because the failure of the other choice is
    silent: bound wider, this unauthenticated socket narrates every container
    handled at the bench to anything on the LAN, and nothing looks different."""
    with pytest.raises(ValueError, match="loopback"):
        AgentSettings(ws_host=host)


@pytest.mark.parametrize(
    ("field", "value"), [("identify_polls", 0), ("absent_polls", 0), ("poll_interval_ms", 1)]
)
def test_degenerate_timings_are_refused(field: str, value: int) -> None:
    """A 1 ms poll interval is not "responsive", it is a UART saturated by a
    process that has nothing better to do; zero tries means deciding before
    looking."""
    with pytest.raises(ValueError):
        AgentSettings(**{field: value})


def test_the_command_debounce_is_plan_mds_four_hundred_milliseconds() -> None:
    """Reused, not invented: PLAN.md's provisioning walk debounces a tap at 400 ms
    and the PWA reuses it for decode feedback."""
    assert default("command_debounce_ms") == 400


def test_the_api_url_defaults_to_the_dev_loop_not_the_tag_origin() -> None:
    """`ALMAGEST_BASE_URL` (`https://almagest.aether.lan`, ADR 0001) is the public origin
    stamped into every tag and label. This one is the agent's route to the API, and
    conflating them would make a hostname change rewrite physical objects."""
    assert default("api_base_url") == "http://127.0.0.1:8000"


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8000", "https://almagest.aether.lan", "https://host.lan/almagest"],
)
def test_an_http_origin_is_accepted_path_prefix_included(url: str) -> None:
    """A reverse proxy may well mount the API under a path."""
    assert AgentSettings(api_base_url=url).api_base_url == url


@pytest.mark.parametrize(
    "url", ["almagest.aether.lan", "file:///etc/passwd", "ftp://host", "", "http://"]
)
def test_a_url_that_is_not_an_http_origin_is_refused_at_startup(url: str) -> None:
    """Checked at config time because the alternative is an `unknown url type` deep
    inside `urllib` on the first placement, at a bench, with a drawer in your hand —
    and because pinning the scheme is what stops config steering `urlopen` at a
    `file://` URL."""
    with pytest.raises(ValueError, match="http"):
        AgentSettings(api_base_url=url)


def test_a_trailing_slash_is_folded_away() -> None:
    """So `{base}{path}` never produces a double slash, which some proxies redirect
    and others 404."""
    assert AgentSettings(api_base_url="http://127.0.0.1:8000/").api_base_url == (
        "http://127.0.0.1:8000"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_timeout_s", 0),
        ("api_timeout_s", 61),
        ("command_debounce_ms", -1),
        ("device_id", "x" * 65),
    ],
)
def test_degenerate_api_settings_are_refused(field: str, value: object) -> None:
    """A zero timeout never completes a request; a 61 s one wedges the poll loop for
    a minute; `device_id` is `client_operations.device_id`, which is `String(64)`, so
    a longer one is an insert that fails at commit time."""
    with pytest.raises(ValueError):
        AgentSettings(**{field: value})
