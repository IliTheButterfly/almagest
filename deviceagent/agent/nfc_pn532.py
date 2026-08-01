"""`Pn532TagSource` — the real reader, over UART. **Never executed in CI.**

No PN532 has been attached to this code, so nothing in this module is verified.
That is why it is the thinnest file in the package: it opens a port, asks the
library for a UID, hands page reads to `agent.ndef`, and formats bytes as hex.
Every branch worth arguing about — what counts as presence, when a tag is the
same tag, what to do with an unreadable one — is in `agent.presence` and
`agent.identity`, against a fake. The one test that touches this class is
`@pytest.mark.live` and skipped by default.

**UART, not SPI.** PLAN.md picks a genuine Adafruit PN532 with
`adafruit-circuitpython-pn532` over a $3 MFRC522 on purpose: the cheap module's
Python ports are UID-focused with hand-rolled NDEF across several unmaintained
forks, and clone PN532s have documented flaky SPI/firmware. On a Pi the UART also
needs `enable_uart=1` and the Bluetooth modem off the primary port, which is
setup this code cannot do for you — see README.md.

That rejection stands for a reader anyone still has to buy, and `agent.nfc_rc522`
is not a reversal of it: it exists because an MFRC522 was already on the shelf and
an unrun driver is worth less than a run one. It answers the "unmaintained forks"
half by having no fork in it — the anticollision and the framing are
`agent.iso14443a`, which is this repo's and is unit-tested. See ADR 0013.

Known-unverified, in the order they are likely to bite:

* **Read range through the platform.** The tag sits ~8-12 mm above the antenna
  through printed PETG. PLAN.md calls antenna centring the design's biggest
  unknown, and the load cell that was to be mounted beside it is deferred
  (ADR 0003), so even the geometry that will be tested is not final.
* **`ntag2xx_read_block` page-by-page latency** at the station's poll cadence.
  If a full NDEF read cannot finish inside one poll interval, the fix is a
  UID-first fast path that only reads user memory once per placement — the state
  machine already tolerates a poll that returns UID-only, which is exactly why
  it was built that way.
* **Which tags actually answer.** NTAG213/215/216 are assumed. Anything else in a
  drawer bottom is a UID-only tag as far as this module is concerned.
"""

from __future__ import annotations

import contextlib
from typing import Any

from agent import ndef
from agent.tags import TagRead, TagSourceError, format_uid

#: The PN532's own timeout for one anticollision attempt. Short on purpose: the
#: station polls continuously, so a long block here does not find more tags.
#:
#: **This is spent on every poll that finds nothing** — `read_passive_target`
#: blocks for the full timeout before reporting an empty field — which is every
#: poll of a removal debounce and every poll of an unreadable tag's identify
#: budget. It is therefore 250 ms of the default 300 ms poll interval, and if it
#: ever exceeds the interval the cadence becomes this number instead:
#: `agent.main.poll_forever` paces to a fixed period and logs the overrun rather
#: than letting both budgets quietly stretch. Untested against hardware, like
#: everything else in this file.
DEFAULT_TARGET_TIMEOUT_S = 0.25

#: PN532 UART default. The chip also accepts 9600-1.2M after a handshake; there is
#: no reason to change it.
DEFAULT_BAUDRATE = 115200


class Pn532TagSource:
    """A PN532 on a serial port, polled for one tag at a time.

    The imports are inside `__init__` rather than at module scope so that a
    development machine — which has neither the CircuitPython stack nor a reader —
    can import `agent.main`, run `--fake`, and be type-checked, without the
    optional `pi` extra installed.
    """

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = DEFAULT_BAUDRATE,
        target_timeout_s: float = DEFAULT_TARGET_TIMEOUT_S,
        max_ndef_pages: int = ndef.DEFAULT_MAX_PAGES,
    ) -> None:
        try:
            import serial
            from adafruit_pn532.uart import PN532_UART
        except ImportError as error:  # pragma: no cover — needs the `pi` extra absent
            raise TagSourceError(
                "the PN532 driver needs the `pi` extra: uv sync --extra pi"
            ) from error

        self._target_timeout_s = target_timeout_s
        self._max_ndef_pages = max_ndef_pages
        try:
            # `timeout` is the pyserial read timeout and must exceed the PN532's
            # own; a serial timeout shorter than the chip's response window turns
            # every read into a spurious failure.
            self._uart: Any = serial.Serial(port, baudrate=baudrate, timeout=1)
            self._reader: Any = PN532_UART(self._uart, debug=False)
            # Reading the firmware version is the cheapest proof the chip is
            # actually there — without it, a wrong port fails later and looks
            # like "no tags ever", which is a much worse diagnostic.
            self.firmware_version: tuple[int, ...] = tuple(self._reader.firmware_version)
            self._reader.SAM_configuration()
        except Exception as error:  # pragma: no cover — needs hardware
            raise TagSourceError(f"cannot open PN532 on {port}: {error}") from error

    def poll(self) -> TagRead | None:
        """One anticollision attempt, then user memory if a tag answered."""
        try:
            uid = self._reader.read_passive_target(timeout=self._target_timeout_s)
        except Exception as error:  # pragma: no cover — needs hardware
            # A raise from the transport is the reader failing, which is a
            # different fact from an empty field, and the station reacts
            # differently to each.
            raise TagSourceError(f"PN532 read failed: {error}") from error

        if uid is None:
            return None

        return TagRead(uid=format_uid(uid) or None, ndef_url=self._read_ndef_url())

    def _read_ndef_url(self) -> str | None:
        """Best-effort user-memory read. A failure here is not a failed poll.

        A tag that answers anticollision but whose user memory does not read is
        exactly PLAN.md's degraded case: the UID lives in factory-locked pages
        0-2, so an interrupted write leaves a UID-only tag rather than a dead one,
        and the station resolves that against `location_tags`.
        """
        try:
            data = ndef.collect_ndef_bytes(self._read_page, max_pages=self._max_ndef_pages)
        except Exception:  # pragma: no cover — needs hardware
            return None
        return ndef.parse_uri_record(data)

    def _read_page(self, page: int) -> bytes | None:
        try:
            block = self._reader.ntag2xx_read_block(page)
        except Exception:  # pragma: no cover — needs hardware
            # Reading past the end of a smaller tag lands here. It is the normal
            # way a read of an NTAG213 stops, not an error.
            return None
        return None if block is None else bytes(block)

    def close(self) -> None:
        # Idempotent and never raising: this runs on the shutdown path that is
        # already handling an error, and masking that error with a close()
        # failure loses the reason the agent is stopping. `getattr` because
        # __init__ can fail before `_uart` exists.
        uart = getattr(self, "_uart", None)
        if uart is not None:
            with contextlib.suppress(Exception):
                uart.close()
