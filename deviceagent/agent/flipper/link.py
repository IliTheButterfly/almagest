"""Byte transports to a Flipper. Two of them, one protocol above.

The firmware exports `ble_profile_serial_set_rpc_active`, which is the fact this
module is built on: **BLE carries the same RPC stream as USB CDC.** So there is
one codec (`agent.flipper.proto`), one session (`agent.flipper.session`), and the
only thing that differs between a Flipper on a cable and a Flipper across the
bench is where the bytes come from. Had the two transports spoken different
protocols this would have been two integrations and the second would have rotted.

**Synchronous, like `TagSource`.** The agent already runs blocking reads in
`asyncio.to_thread`; adding a second concurrency model for one peripheral would
mean two ways to be slow. `BleFlipperLink` therefore hides `bleak`'s event loop
inside a thread it owns and presents the same three methods, rather than leaking
`async` upward into the session and the registry.

**Neither has run against a Flipper.** USB is expected to work first: `pyserial`
against a CDC ACM node is well-trodden and the framing is asserted byte-for-byte
in `tests/test_flipper_proto.py`. BLE is the least-verified thing in this repo —
the bench machine has no Bluetooth stack installed at all, so even the discovery
call has never executed. Its characteristic UUIDs are recorded below with the
firmware source that defines them, because they are the part most likely to be
wrong and the hardest to notice.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
from typing import Any, Final, Protocol, runtime_checkable

logger = logging.getLogger("almagest.deviceagent.flipper.link")


class FlipperLinkError(RuntimeError):
    """The transport failed. The twin of `TagSourceError`: the *link* is broken,
    as distinct from the Flipper refusing something, which is a reply."""


@runtime_checkable
class FlipperLink(Protocol):
    """A bidirectional byte pipe to one Flipper.

    Deliberately dumber than a socket: no framing, no reconnection, no notion of
    a message. `agent.flipper.proto.FrameDecoder` owns framing and
    `agent.flipper.session` owns the conversation, so a transport is only ever
    responsible for moving bytes and saying when it cannot.
    """

    @property
    def description(self) -> str:
        """Operator-facing, and stable for the life of the link — it ends up in
        `device.attached`'s label and in every log line about this Flipper."""
        ...

    def read(self, timeout_s: float) -> bytes:
        """Whatever has arrived, up to `timeout_s`. `b""` on timeout, never None.

        A short read is normal and not an error: USB hands back an endpoint
        buffer and BLE a notification, and both are smaller than most frames.
        """
        ...

    def write(self, data: bytes) -> None: ...

    def close(self) -> None:
        """Idempotent, and must not raise — it runs on the path already handling
        an error, and a close failure would mask the reason we are shutting down."""
        ...


# ---------------------------------------------------------------------------
# USB CDC
# ---------------------------------------------------------------------------

#: The Flipper's CDC node appears here with its name in the path, which is how
#: discovery finds it without a vendor/product table. Stable across reboots and
#: across several Flippers on one machine, unlike `/dev/ttyACM<n>`.
SERIAL_BY_ID: Final = "/dev/serial/by-id"
SERIAL_NAME_HINT: Final = "Flipper"

#: The CDC endpoint ignores the line rate, but pyserial insists on one.
SERIAL_BAUDRATE: Final = 115200


class SerialFlipperLink:
    """A Flipper on a USB cable, over its CDC ACM node.

    The import is inside `__init__` — as in `Pn532TagSource` — so a machine with
    neither `pyserial` nor a Flipper can still import the bridge, type-check it
    and run every test.

    **The CLI banner is drained before anything else.** A fresh CDC session opens
    onto the Flipper's text CLI, which greets it with a prompt; those bytes are
    not protobuf and would desynchronise `FrameDecoder` on the first frame. The
    session sends `start_rpc_session\\r` and swallows everything up to the point
    the stream turns binary — see `agent.flipper.session.open_serial`.
    """

    def __init__(self, port: str, *, baudrate: int = SERIAL_BAUDRATE) -> None:
        try:
            import serial
        except ImportError as error:  # pragma: no cover — needs the extra absent
            raise FlipperLinkError(
                "talking to a Flipper over USB needs the `flipper` extra: uv sync --extra flipper"
            ) from error
        self._port = port
        try:
            # `timeout=0` makes every read non-blocking; the caller's timeout is
            # applied per call in `read` instead, so one link cannot pin a
            # thread for longer than the caller asked for.
            # `exclusive=True` because a CDC node is openable twice on Linux and
            # the second opener does real damage: `open_serial` writes `\r` and
            # the start-RPC incantation into the port, which desynchronises the
            # `FrameDecoder` of the session already using it — the exact failure
            # `session.open_serial`'s docstring calls unrecoverable for the life
            # of the session, and one that presents as "the Flipper stopped
            # answering" rather than as a second process. Better to fail at open.
            #
            # It is pyserial's advisory lock, so it stops *another instance of
            # this bridge* — the realistic case, since a station may be started
            # twice — and does not stop `cat /dev/ttyACM0` or `screen`. There is
            # no portable way to stop those, and a human who runs one is not the
            # failure mode this guards.
            self._serial: Any = serial.Serial(port, baudrate=baudrate, timeout=0, exclusive=True)
        except Exception as error:  # pragma: no cover — needs hardware
            raise FlipperLinkError(f"cannot open a Flipper on {port}: {error}") from error

    @property
    def description(self) -> str:
        return f"Flipper Zero on {self._port}"

    @property
    def port(self) -> str:
        return self._port

    def read(self, timeout_s: float) -> bytes:
        try:
            self._serial.timeout = timeout_s
            # `read(1)` blocks up to the timeout for the first byte, then
            # `in_waiting` takes whatever else arrived with it. Reading a fixed
            # large size instead would wait for the whole buffer to fill.
            first = self._serial.read(1)
            if not first:
                return b""
            rest: bytes = self._serial.read(self._serial.in_waiting or 0)
            return bytes(first) + rest
        except Exception as error:  # pragma: no cover — needs hardware
            raise FlipperLinkError(f"read from {self._port} failed: {error}") from error

    def write(self, data: bytes) -> None:
        try:
            self._serial.write(data)
            self._serial.flush()
        except Exception as error:  # pragma: no cover — needs hardware
            raise FlipperLinkError(f"write to {self._port} failed: {error}") from error

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._serial.close()


# ---------------------------------------------------------------------------
# BLE
# ---------------------------------------------------------------------------

#: The Flipper's BLE serial service, from the firmware's
#: `targets/f7/ble_glue/services/serial_service.c`. **Unverified from this
#: machine** — there is no Bluetooth stack here, so nothing has ever connected.
#: If a Flipper advertises but every read times out, these are the first thing to
#: check, with `bleakscan` or `nRF Connect` against a real device.
BLE_SERVICE_UUID: Final = "8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000"
BLE_RX_CHAR_UUID: Final = "19ed82ae-ed21-4c9d-4145-228e61fe0000"  # host writes here
BLE_TX_CHAR_UUID: Final = "19ed82ae-ed21-4c9d-4145-228e62fe0000"  # host subscribes here

#: Flippers advertise under their own name, which the user set. The prefix is
#: the factory default and the only thing discovery can match on without pairing
#: to everything in range first.
BLE_NAME_HINT: Final = "Flipper"


class BleFlipperLink:
    """A Flipper over Bluetooth LE, with `bleak`'s event loop hidden in a thread.

    `bleak` is async and everything above this is not, so this class owns a
    thread, runs a loop in it, and marshals across with a queue. The alternative
    — making `FlipperLink` async — would have pushed `asyncio` into the device
    registry and into `TagSource`, for one optional peripheral on one transport.

    **Nothing here has ever executed.** No Bluetooth stack is installed on the
    development machine, so this is written from the API and the firmware source
    and is the least-verified code in the repository. `tests/test_flipper_ble.py`
    is `live`-marked in its entirety.
    """

    def __init__(
        self,
        address: str,
        *,
        name: str | None = None,
        connect_timeout_s: float = 20.0,
    ) -> None:
        try:
            import bleak
        except ImportError as error:  # pragma: no cover — needs the extra absent
            raise FlipperLinkError(
                "talking to a Flipper over BLE needs the `flipper` extra: uv sync --extra flipper"
            ) from error
        self._bleak = bleak
        self._address = address
        self._name = name or address
        self._inbox: queue.Queue[bytes] = queue.Queue()
        self._loop: Any = None
        self._client: Any = None
        self._closed = threading.Event()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, name=f"flipper-ble-{address}", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(connect_timeout_s):
            self.close()
            raise FlipperLinkError(f"timed out connecting to {self._name} over BLE")
        if self._error is not None:
            self.close()
            raise FlipperLinkError(f"cannot connect to {self._name}: {self._error}")

    # -- the loop thread ---------------------------------------------------

    def _run(self) -> None:  # pragma: no cover — needs a Bluetooth stack
        import asyncio

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except BaseException as error:
            self._error = error
        finally:
            self._ready.set()
            with contextlib.suppress(Exception):
                self._loop.close()

    async def _serve(self) -> None:  # pragma: no cover — needs a Bluetooth stack
        import asyncio

        def on_notify(_: Any, data: bytearray) -> None:
            self._inbox.put(bytes(data))

        async with self._bleak.BleakClient(self._address) as client:
            self._client = client
            await client.start_notify(BLE_TX_CHAR_UUID, on_notify)
            self._ready.set()
            while not self._closed.is_set():
                await asyncio.sleep(0.05)
            with contextlib.suppress(Exception):
                await client.stop_notify(BLE_TX_CHAR_UUID)

    # -- the FlipperLink surface -------------------------------------------

    @property
    def description(self) -> str:
        return f"{self._name} over Bluetooth"

    @property
    def address(self) -> str:
        return self._address

    def read(self, timeout_s: float) -> bytes:
        """Drain the notification queue, waiting up to `timeout_s` for the first.

        Concatenating whatever else is already queued matters more here than over
        USB: a BLE MTU is 20-244 bytes, so a single frame routinely arrives as
        several notifications and returning them one at a time would multiply the
        round trips.
        """
        try:
            first = self._inbox.get(timeout=timeout_s)
        except queue.Empty:
            return b""
        chunks = [first]
        while True:
            try:
                chunks.append(self._inbox.get_nowait())
            except queue.Empty:
                return b"".join(chunks)

    def write(self, data: bytes) -> None:  # pragma: no cover — needs a Bluetooth stack
        import asyncio

        if self._client is None or self._loop is None:
            raise FlipperLinkError("not connected")
        future = asyncio.run_coroutine_threadsafe(
            self._client.write_gatt_char(BLE_RX_CHAR_UUID, data, response=False), self._loop
        )
        try:
            future.result(timeout=10.0)
        except Exception as error:
            raise FlipperLinkError(f"BLE write to {self._name} failed: {error}") from error

    def close(self) -> None:
        self._closed.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
