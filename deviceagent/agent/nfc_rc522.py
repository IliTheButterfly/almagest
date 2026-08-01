"""`Rc522TagSource` — an MFRC522 over SPI. **Never executed in CI.**

The sibling of `agent.nfc_pn532`, and thin for the same reason: no reader has
ever been attached to this code, so everything that could be a decision has been
moved somewhere a test can reach it. What is left here is register access, the
FIFO transaction, and the block cache — the parts that are inherently about a
chip. The protocol above it is `agent.iso14443a`; the bytes above *that* are
`agent.ndef`; presence and identity are unchanged and shared with the PN532.

**Why this exists at all**, given `docs/PLAN.md` rejects the MFRC522: an RC522 is
already on the shelf and a PN532 is not, and PLAN.md's reasoning was about the
*ecosystem*, not the silicon — "Python ports are UID-focused with hand-rolled NDEF
across several unmaintained forks". That objection is answered by not using one.
The NDEF is `agent.ndef`, which predates this file and is tested; the UID handling
is `agent.iso14443a`, which handles the 7-byte cascade NTAG213 needs and is tested
against both cascade lengths. ADR 0013 records the trade in full, including what is
worse about it: less range margin, and a driver this repo now owns.

Three things about the chip shape this file:

* **SPI, not UART.** The upside over the PN532 path is real and worth naming: no
  `enable_uart=1`, no evicting the Bluetooth modem off the primary UART. Enable
  SPI in `raspi-config` and wire six pins. **RST must be tied to 3V3** — this
  driver does not drive a reset line, it uses the chip's SoftReset command, and a
  floating RST is the usual reason an RC522 reads as absent.
* **An empty field costs ~25 ms, not 250 ms.** The PN532's `read_passive_target`
  blocks for its whole timeout before admitting the field is empty, and README.md
  item 2 is largely about what that does to budgets made of empty polls. Here the
  bound is the chip's own timer (`TIMER_RELOAD` below), so the removal debounce
  and the identify budget have room inside a 300 ms interval that they did not
  obviously have before. Still unmeasured, like everything else in this file.
* **One READ returns four pages.** `agent.ndef` asks for one page at a time
  because that is what the PN532 library offers; here the same walk is served
  from a 16-byte block cache, so a typical NDEF read is about two transactions
  rather than nine.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any, Final

from agent import iso14443a, ndef
from agent.tags import TagRead, TagSourceError, format_uid

# --- registers, from the MFRC522 datasheet §9.2 ------------------------------
# Only the ones this driver touches. Named as the datasheet names them so a
# reader with the PDF open can follow along; that is worth more here than
# conforming to local naming, because nothing else in this repo can check them.
_COMMAND_REG: Final = 0x01
_COM_IRQ_REG: Final = 0x04
_ERROR_REG: Final = 0x06
_FIFO_DATA_REG: Final = 0x09
_FIFO_LEVEL_REG: Final = 0x0A
_CONTROL_REG: Final = 0x0C
_BIT_FRAMING_REG: Final = 0x0D
_COLL_REG: Final = 0x0E
_MODE_REG: Final = 0x11
_TX_CONTROL_REG: Final = 0x14
_TX_ASK_REG: Final = 0x15
_RF_CFG_REG: Final = 0x26
_T_MODE_REG: Final = 0x2A
_T_PRESCALER_REG: Final = 0x2B
_T_RELOAD_REG_H: Final = 0x2C
_T_RELOAD_REG_L: Final = 0x2D
_VERSION_REG: Final = 0x37

_CMD_IDLE: Final = 0x00
_CMD_TRANSCEIVE: Final = 0x0C
_CMD_SOFT_RESET: Final = 0x0F

#: ComIrqReg: RxIRq | IdleIRq — the transaction finished. TimerIRq says it did not.
_IRQ_RX_OR_IDLE: Final = 0x30
_IRQ_TIMER: Final = 0x01

#: ErrorReg bits worth failing on: buffer overflow, parity, protocol, CRC. The
#: collision bit is deliberately *not* here — a collision produces a frame whose
#: BCC then fails in `agent.iso14443a`, which is where that decision belongs.
_ERROR_MASK: Final = 0x13

#: BitFramingReg: StartSend.
_START_SEND: Final = 0x80

#: The chip's own timeout for one transaction, as prescaler + reload. 25 ms is
#: several times the ~5 ms a Type A exchange needs and short enough that an empty
#: field is cheap — which, per the module docstring, is the whole latency
#: argument for this reader over the PN532.
_TIMER_PRESCALER: Final = 0x0D3E  # 40 kHz timer tick
_TIMER_RELOAD: Final = 25  # ticks of 1 ms

#: RFCfgReg receiver gain, bits 6:4. 0x07 is the maximum, 48 dB. Maximum by
#: default and not tunable down through config on purpose: the one thing PLAN.md
#: calls the design's biggest unknown is whether a bottom-pocket tag reads through
#: ~8-12 mm of PETG, this reader has less margin there than a PN532, and no
#: situation in a bench station is improved by hearing tags less well.
_MAX_RX_GAIN: Final = 0x07

#: Python-side backstop on waiting for the chip. Only reached if the MFRC522's own
#: timer never fires, which means the chip has wedged or the wiring is wrong —
#: without it a poll would block for ever and take the whole station with it.
DEFAULT_TRANSACTION_TIMEOUT_S: Final = 0.1

DEFAULT_SPI_BUS: Final = 0
DEFAULT_SPI_DEVICE: Final = 0

#: The chip tops out at 10 MHz. 1 MHz is plenty for 18-byte frames and is kind to
#: the dupont wiring an RC522 is invariably attached with.
DEFAULT_SPI_HZ: Final = 1_000_000

#: VersionReg values seen in the wild: 0x91/0x92 are the datasheet's v1.0/v2.0,
#: and clones report various others. Only all-zeroes and all-ones are treated as
#: proof of absence — they are what a floating MISO reads as, which is the actual
#: symptom of "SPI not enabled" or "RST left floating".
_ABSENT_VERSIONS: Final = frozenset({0x00, 0xFF})


class Rc522TagSource:
    """An MFRC522 on an SPI bus, polled for one tag at a time.

    Imports `spidev` inside `__init__`, exactly as `Pn532TagSource` imports its
    stack, so a development machine can import `agent.main`, run `--fake` and be
    type-checked without the `pi` extra installed.
    """

    def __init__(
        self,
        *,
        bus: int = DEFAULT_SPI_BUS,
        device: int = DEFAULT_SPI_DEVICE,
        speed_hz: int = DEFAULT_SPI_HZ,
        transaction_timeout_s: float = DEFAULT_TRANSACTION_TIMEOUT_S,
        max_ndef_pages: int = ndef.DEFAULT_MAX_PAGES,
    ) -> None:
        try:
            import spidev
        except ImportError as error:  # pragma: no cover — needs the `pi` extra absent
            raise TagSourceError(
                "the RC522 driver needs the `pi` extra: uv sync --extra pi"
            ) from error

        self._timeout_s = transaction_timeout_s
        self._max_ndef_pages = max_ndef_pages
        self._blocks: dict[int, bytes] = {}
        try:
            self._spi: Any = spidev.SpiDev()
            self._spi.open(bus, device)
            self._spi.max_speed_hz = speed_hz
            self._spi.mode = 0
            self._reset()
            #: The cheapest proof the chip is there, and the counterpart of
            #: `Pn532TagSource.firmware_version`: without it a wrong bus, SPI left
            #: disabled or a floating RST all present as "no tags, ever", which is
            #: the worst diagnostic a bench can give you.
            self.version: int = self._read(_VERSION_REG)
            if self.version in _ABSENT_VERSIONS:
                raise TagSourceError(
                    f"no MFRC522 answering on SPI {bus}.{device} (VersionReg=0x{self.version:02X})"
                    ": check that SPI is enabled and that RST is tied to 3V3"
                )
            self._configure()
        except TagSourceError:
            self.close()
            raise
        except Exception as error:  # pragma: no cover — needs hardware
            self.close()
            raise TagSourceError(f"cannot open the RC522 on SPI {bus}.{device}: {error}") from error

    # --- the TagSource protocol ----------------------------------------------

    def poll(self) -> TagRead | None:
        """One anticollision attempt, then user memory if a tag selected."""
        self._blocks.clear()
        try:
            selection = iso14443a.select_unique(self._transceive)
            if not selection.present:
                return None
            if selection.uid is None:
                # Present but unreadable — a damaged tag, a tag half off the
                # antenna, or two containers stacked. `agent.presence` renders
                # that differently from an empty platform, which is why the
                # protocol is three-valued.
                return TagRead(uid=None, ndef_url=None)
            return TagRead(
                uid=format_uid(selection.uid) or None,
                ndef_url=self._read_ndef_url(),
            )
        except TagSourceError:
            raise
        except Exception as error:  # pragma: no cover — needs hardware
            # An SPI transfer raising is the *reader* failing, which the station
            # reacts to differently from an unreadable tag: one is fixed by
            # re-seating a drawer and the other is not.
            raise TagSourceError(f"RC522 read failed: {error}") from error
        finally:
            # Unconditional: a tag left ACTIVE answers no WUPA, so skipping this
            # on the error paths would make the *next* poll report an empty
            # platform under a container that never moved.
            with contextlib.suppress(Exception):
                iso14443a.halt(self._transceive)

    def close(self) -> None:
        # Idempotent and never raising: this runs on shutdown paths that are
        # already handling an error, including one inside `__init__` before
        # `_spi` exists. Turning the antenna off first is politeness to the
        # 13.56 MHz band, not a requirement.
        spi = getattr(self, "_spi", None)
        if spi is None:
            return
        with contextlib.suppress(Exception):
            self._clear_bits(_TX_CONTROL_REG, 0x03)
        with contextlib.suppress(Exception):
            spi.close()

    # --- NDEF ------------------------------------------------------------------

    def _read_ndef_url(self) -> str | None:
        """Best-effort user-memory read. A failure here is not a failed poll.

        A tag that selects but whose user memory does not read is PLAN.md's
        degraded case: the UID lives in factory-locked pages 0-2, so an
        interrupted write leaves a UID-only tag rather than a dead one, and the
        station resolves that against `location_tags`.
        """
        try:
            data = ndef.collect_ndef_bytes(self._read_page, max_pages=self._max_ndef_pages)
        except Exception:  # pragma: no cover — needs hardware
            return None
        return ndef.parse_uri_record(data)

    def _read_page(self, page: int) -> bytes | None:
        """One 4-byte page, served from the block a previous page read fetched.

        The cache is per poll (`poll` clears it), so it can never serve a page
        from the container before last — which would be the one way a cache here
        could produce a wrong answer rather than merely a slow one.
        """
        cached = self._blocks.get(page)
        if cached is not None:
            return cached
        block = iso14443a.read_block(self._transceive, page)
        if block is None:
            return None
        for offset in range(4):
            self._blocks[page + offset] = block[offset * 4 : offset * 4 + 4]
        return block[:4]

    # --- the chip ---------------------------------------------------------------

    def _transceive(self, frame: bytes, *, tx_last_bits: int = 0) -> bytes | None:
        """One FIFO transaction. `None` is the chip's timer expiring: no answer.

        Datasheet §10.3.1.8. Note that `_COLL_REG`'s ValuesAfterColl bit is
        cleared in `_configure` and never touched here — this driver does not do
        bit-oriented collision resolution, and `agent.iso14443a` says why.
        """
        self._write(_COMMAND_REG, _CMD_IDLE)
        self._write(_COM_IRQ_REG, 0x7F)  # clear every interrupt request bit
        self._set_bits(_FIFO_LEVEL_REG, 0x80)  # flush the FIFO
        for byte in frame:
            self._write(_FIFO_DATA_REG, byte)
        self._write(_BIT_FRAMING_REG, tx_last_bits & 0x07)
        self._write(_COMMAND_REG, _CMD_TRANSCEIVE)
        self._set_bits(_BIT_FRAMING_REG, _START_SEND)

        deadline = time.monotonic() + self._timeout_s
        while True:
            irq = self._read(_COM_IRQ_REG)
            if irq & _IRQ_RX_OR_IDLE:
                break
            if irq & _IRQ_TIMER:
                self._clear_bits(_BIT_FRAMING_REG, _START_SEND)
                return None
            if time.monotonic() > deadline:  # pragma: no cover — needs hardware
                self._clear_bits(_BIT_FRAMING_REG, _START_SEND)
                raise TagSourceError(
                    "the MFRC522 did not finish a transaction and its own timer never fired; "
                    "the chip has stopped responding"
                )
        self._clear_bits(_BIT_FRAMING_REG, _START_SEND)

        if self._read(_ERROR_REG) & _ERROR_MASK:
            return None

        length = self._read(_FIFO_LEVEL_REG)
        if length == 0:
            return None
        answer = bytes(self._read(_FIFO_DATA_REG) for _ in range(length))
        # A partial last byte is a NAK or a collision fragment, never a frame this
        # driver can use. Returning it whole would let a 4-bit NAK be mistaken for
        # a one-byte payload.
        rx_last_bits = self._read(_CONTROL_REG) & 0x07
        if rx_last_bits and len(answer) == 1:
            return None
        return answer

    def _reset(self) -> None:
        self._write(_COMMAND_REG, _CMD_SOFT_RESET)
        # The datasheet gives no completion flag for SoftReset; the oscillator
        # start-up bound is ~37.74 µs and every implementation waits a little
        # longer. This one is a poll of the command register rather than a flat
        # sleep, so a chip that is not there fails in `_read` instead of here.
        deadline = time.monotonic() + 0.05
        while self._read(_COMMAND_REG) & (1 << 4):  # PowerDown clears when ready
            if time.monotonic() > deadline:  # pragma: no cover — needs hardware
                break
            time.sleep(0.001)

    def _configure(self) -> None:
        # Timer: auto-start at the end of transmission, prescaler + reload as
        # above, so every transaction is bounded by the chip rather than by us.
        self._write(_T_MODE_REG, 0x80 | ((_TIMER_PRESCALER >> 8) & 0x0F))
        self._write(_T_PRESCALER_REG, _TIMER_PRESCALER & 0xFF)
        self._write(_T_RELOAD_REG_H, (_TIMER_RELOAD >> 8) & 0xFF)
        self._write(_T_RELOAD_REG_L, _TIMER_RELOAD & 0xFF)
        self._write(_TX_ASK_REG, 0x40)  # 100 % ASK, which Type A requires
        self._write(_MODE_REG, 0x3D)  # CRC coprocessor preset 0x6363, unused here
        self._write(_COLL_REG, self._read(_COLL_REG) & 0x7F)  # ValuesAfterColl clear
        self._write(_RF_CFG_REG, _MAX_RX_GAIN << 4)
        self._set_bits(_TX_CONTROL_REG, 0x03)  # antenna drivers on

    def _read(self, register: int) -> int:
        # Address byte: bit 7 set = read, bits 6:1 = address, bit 0 always 0.
        answer = self._spi.xfer2([((register << 1) & 0x7E) | 0x80, 0x00])
        return int(answer[1])

    def _write(self, register: int, value: int) -> None:
        self._spi.xfer2([(register << 1) & 0x7E, value & 0xFF])

    def _set_bits(self, register: int, mask: int) -> None:
        self._write(register, self._read(register) | mask)

    def _clear_bits(self, register: int, mask: int) -> None:
        self._write(register, self._read(register) & (~mask & 0xFF))
