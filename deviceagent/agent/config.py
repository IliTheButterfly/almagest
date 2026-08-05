"""Runtime configuration, read from the environment.

Same shape as `backend/app/config.py` — pydantic-settings, explicit env aliases,
the repo-root `.env` — because these are two components of one system and every
key is documented in the same `.env.example`. Keys are prefixed `DEVICEAGENT_`
rather than `ALMAGEST_`: this process runs on the Pi, not in the cluster, and its
settings are physically about that one machine's ports.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Hostnames accepted in addition to anything `ipaddress` calls loopback.
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        # Both, so `cd deviceagent && almagest-deviceagent` and a repo-root
        # invocation read the same file.
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    ws_host: str = Field(default="127.0.0.1", alias="DEVICEAGENT_WS_HOST")
    ws_port: int = Field(default=8765, alias="DEVICEAGENT_WS_PORT")

    #: Five tries at a 300 ms cadence is where both numbers come from: it bounds
    #: PLAN.md's "~5 tries / 1.5 s" identify budget. **A bound, not an equality,
    #: and only while one poll's read fits inside one interval.** `poll_forever`
    #: paces to a fixed period so the multiplication is at least the right shape,
    #: but `Pn532TagSource.DEFAULT_TARGET_TIMEOUT_S` is 250 ms of that 300 ms and
    #: an NDEF read is one UART round trip per 4-byte page on top; nothing has
    #: measured a real reader, so whether the budget really lands near 1.5 s is
    #: unverified (README.md, "Unverifiable without hardware"). An overrunning
    #: poll is logged rather than silently stretching the cadence.
    #:
    #: Faster polling does not identify anything sooner — a tag either answers or
    #: it does not — it just burns the UART and the Pi's idle power.
    poll_interval_ms: int = Field(default=300, alias="DEVICEAGENT_POLL_INTERVAL_MS", ge=20)
    identify_polls: int = Field(default=5, alias="DEVICEAGENT_IDENTIFY_POLLS", ge=1)

    #: Consecutive empty polls before a removal is believed. Raise it if drawers
    #: report spurious removals; lower it only if you never move two containers
    #: in quick succession.
    absent_polls: int = Field(default=3, alias="DEVICEAGENT_ABSENT_POLLS", ge=1)

    #: Which reader is wired to this station. Two exist and they are not
    #: equivalent: the PN532 is what PLAN.md specifies and `docs/adr/0013` is why
    #: the RC522 is here anyway. Defaulting to `pn532` keeps the specified
    #: hardware the one you get by not deciding.
    #:
    #: A setting rather than probing for whichever answers. Probing would open a
    #: serial port and an SPI bus on every start, and — worse — a station whose
    #: reader had come unplugged would silently fall through to the other one and
    #: report an empty platform instead of a broken reader, which is the one
    #: distinction `TagSourceError` exists to preserve.
    #:
    #: `none` is the third answer, and it is not a disabled station — it is a
    #: machine with no platform under it at all: the laptop-with-a-Flipper shape
    #: ADR 0014 widened this process to cover. The station loop then reports an
    #: empty field forever and the bridge does the work. See `agent/no_reader.py`
    #: for why that is a `NoTagSource` rather than `--fake` with an empty script.
    reader: Literal["pn532", "rc522", "none"] = Field(default="pn532", alias="DEVICEAGENT_READER")

    pn532_port: str = Field(default="/dev/ttyAMA0", alias="DEVICEAGENT_PN532_PORT")

    #: `/dev/spidev<bus>.<device>`; CE0 on a Pi's primary SPI bus is `0.0`. Only
    #: read when `reader` is `rc522`.
    rc522_spi_bus: int = Field(default=0, alias="DEVICEAGENT_RC522_SPI_BUS", ge=0)
    rc522_spi_device: int = Field(default=0, alias="DEVICEAGENT_RC522_SPI_DEVICE", ge=0)

    #: The chip's ceiling is 10 MHz. There is no reason to approach it for 18-byte
    #: frames, and dupont wire to a $3 module is not a transmission line.
    rc522_spi_hz: int = Field(
        default=1_000_000, alias="DEVICEAGENT_RC522_SPI_HZ", ge=10_000, le=10_000_000
    )

    #: The API **as reachable from the Pi**. Deliberately not `ALMAGEST_BASE_URL`:
    #: that is the public origin written into every tag and printed label
    #: (`https://almagest.aether.lan`, ADR 0001) and it must stay stamped on physical
    #: objects whatever route this daemon takes to the server. The default is the
    #: dev loop's `make run`, because there is no correct default for a Pi.
    api_base_url: str = Field(default="http://127.0.0.1:8000", alias="DEVICEAGENT_API_BASE_URL")

    #: Bounds how long a placement or a commit can hold up the poll loop, which
    #: awaits the session's round trip inline. Long enough for a cold SQLite page
    #: cache, short enough that a lifted container is noticed promptly.
    api_timeout_s: float = Field(default=5.0, alias="DEVICEAGENT_API_TIMEOUT_S", gt=0, le=60)

    #: Recorded on every movement this station commits, in
    #: `client_operations.device_id` (`String(64)`) — which is what lets "who moved
    #: this" be answered for a bench with two stations on it.
    device_id: str = Field(default="station", alias="DEVICEAGENT_DEVICE_ID", max_length=64)

    #: PLAN.md's provisioning walk debounces a tap at 400 ms and the PWA reuses
    #: that number for decode feedback; the station reuses it a third time so a
    #: double-tapped Commit is one commit. Zero disables it, which is for tests.
    command_debounce_ms: int = Field(
        default=400, alias="DEVICEAGENT_COMMAND_DEBOUNCE_MS", ge=0, le=5000
    )

    # -- the bridge (ADR 0014) ------------------------------------------------

    #: Look for Flippers on USB. On by default: a directory listing of
    #: `/dev/serial/by-id` costs nothing and finds nothing when nothing is
    #: plugged in, so the failure mode of leaving it on is zero.
    flipper_usb: bool = Field(default=True, alias="DEVICEAGENT_FLIPPER_USB")

    #: Look for Flippers over Bluetooth. **Off by default, and that is a
    #: statement about verification rather than about preference.**
    #: `BleFlipperLink` has never executed — the machine it was written on has no
    #: Bluetooth stack — so switching it on spins up an adapter to look for
    #: hardware nobody has confirmed the code can talk to. It also needs the
    #: `flipper` extra for `bleak`. Turn it on when there is a Flipper to test
    #: against; see ADR 0014's "unverified" section first.
    flipper_ble: bool = Field(default=False, alias="DEVICEAGENT_FLIPPER_BLE")

    #: How often to look for readers appearing or vanishing.
    sweep_interval_ms: int = Field(default=2000, alias="DEVICEAGENT_SWEEP_INTERVAL_MS", ge=200)

    #: How often each attached bridge reader is asked for a tag. Deliberately
    #: slower than `poll_interval_ms`: that one paces the station's *budgets*,
    #: while this one paces a hand holding a tag against a Flipper.
    tap_interval_ms: int = Field(default=500, alias="DEVICEAGENT_TAP_INTERVAL_MS", ge=50)

    #: The origin the PWA is served from, allowed through the WebSocket
    #: handshake's CORS and Private Network Access checks.
    #:
    #: **Why this exists at all.** The PWA is on `https://almagest.aether.lan` (ADR
    #: 0001) and opens `ws://127.0.0.1:8765`. Loopback is "potentially
    #: trustworthy" per the secure-context spec so this is *specified* to work,
    #: but Chrome's Private Network Access rollout adds a preflight for
    #: public→local requests and browsers have differed here before. Answering
    #: explicitly is cheap; discovering the default was wrong means a bridge that
    #: is running, reachable by curl, and invisible to the page.
    allowed_origin: str = Field(
        default="https://almagest.aether.lan", alias="DEVICEAGENT_ALLOWED_ORIGIN"
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("ws_host")
    @classmethod
    def _refuse_a_non_loopback_bind(cls, value: str) -> str:
        """The event socket is unauthenticated, so it stays on the loopback.

        Refused at config time rather than documented, because the failure of the
        other choice is silent: bound to 0.0.0.0 this port narrates every
        container handled at the bench to anything on the LAN, and nothing about
        the agent's behaviour would look different. If a remote kiosk is ever
        wanted, the answer is to reverse-proxy it behind the PWA's own TLS origin,
        not to widen this bind.
        """
        host = value.strip()
        if host.casefold() in _LOOPBACK_NAMES:
            return host
        try:
            if ipaddress.ip_address(host).is_loopback:
                return host
        except ValueError as error:
            message = f"DEVICEAGENT_WS_HOST must be a loopback address, got {value!r}"
            raise ValueError(message) from error
        raise ValueError(
            f"DEVICEAGENT_WS_HOST must be a loopback address, got {value!r}: this socket has no "
            "authentication and reports every container handled at the bench"
        )

    @field_validator("api_base_url")
    @classmethod
    def _require_an_http_url(cls, value: str) -> str:
        """Refuse anything that is not an http(s) origin.

        Checked at config time because the alternative is discovering it as an
        `unknown url type` deep inside `urllib` on the first placement, at a bench,
        with a drawer in your hand. Plain `http` is accepted rather than forbidden:
        there is no credential here to leak, and requiring TLS before the private
        CA of ADR 0001 exists would make the agent unusable for the phase it was
        built in. A path prefix is allowed — a reverse proxy may well add one.
        """
        url = value.strip().rstrip("/")
        scheme = next((s for s in ("http://", "https://") if url.startswith(s)), None)
        if scheme is None or not url[len(scheme) :]:
            raise ValueError(
                f"DEVICEAGENT_API_BASE_URL must be an http:// or https:// origin, got {value!r}"
            )
        return url

    @property
    def poll_interval_s(self) -> float:
        return self.poll_interval_ms / 1000.0

    @property
    def sweep_interval_s(self) -> float:
        return self.sweep_interval_ms / 1000.0

    @property
    def tap_interval_s(self) -> float:
        return self.tap_interval_ms / 1000.0


@lru_cache
def get_settings() -> AgentSettings:
    return AgentSettings()
