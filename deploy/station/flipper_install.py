"""Upload a .fap to a Flipper over its text CLI.

`ufbt install` is the normal route and cannot be used here: the fbt toolchain
ships x86_64 binaries only, and the bench machine is aarch64. The CLI's
`storage write_chunk <path> <size>` takes an exact byte count, so a binary
upload needs nothing but pyserial.

    python flipper_install.py /dev/serial/by-id/usb-Flipper... /tmp/antlia.fap \
        /ext/apps/NFC/antlia.fap
"""

from __future__ import annotations

import sys
import time

import serial

PROMPT = b">: "


def drain(port: serial.Serial, quiet_s: float = 0.4, limit_s: float = 5.0) -> bytes:
    """Read until the Flipper stops talking for `quiet_s`."""
    seen = bytearray()
    last = time.monotonic()
    started = last
    while time.monotonic() - started < limit_s:
        chunk = port.read(port.in_waiting or 1)
        if chunk:
            seen += chunk
            last = time.monotonic()
        elif time.monotonic() - last >= quiet_s:
            break
    return bytes(seen)


def command(port: serial.Serial, line: str, quiet_s: float = 0.5) -> str:
    port.write(line.encode() + b"\r")
    port.flush()
    return drain(port, quiet_s).decode("utf-8", "replace")


def main() -> int:
    node, local, remote = sys.argv[1], sys.argv[2], sys.argv[3]
    payload = open(local, "rb").read()
    print(f"uploading {len(payload)} bytes to {remote}")

    port = serial.Serial(node, baudrate=115200, timeout=0.2, exclusive=True)
    try:
        # Wake the CLI and swallow the banner.
        port.write(b"\r")
        drain(port)

        # The directory may or may not exist; a failure here is not fatal.
        print(command(port, "storage mkdir /ext/apps").strip()[:120])
        print(command(port, "storage mkdir /ext/apps/NFC").strip()[:120])
        # Remove any previous copy so the write is not appending into a stale file.
        print(command(port, f"storage remove {remote}").strip()[:120])

        # `\r` only, never `\r\n`. The CLI terminates a line on `\r` and leaves the
        # `\n` in the stream — which then becomes the *first byte the payload
        # reader sees*, shifting the whole file by one and dropping its last
        # byte. The file lands with exactly the right size and the wrong md5,
        # which is as quiet as corruption gets.
        port.write(f"storage write_chunk {remote} {len(payload)}\r".encode())
        port.flush()
        # The firmware answers "Ready" before it will accept the bytes; sending
        # early is how the first chunk gets eaten by the echo.
        ready = drain(port, quiet_s=0.4, limit_s=5.0).decode("utf-8", "replace")
        print("ready:", ready.strip()[:120])

        sent = 0
        while sent < len(payload):
            block = payload[sent : sent + 512]
            port.write(block)
            port.flush()
            sent += len(block)
        print(f"sent {sent} bytes")

        print("after:", drain(port, quiet_s=1.0, limit_s=15.0).decode("utf-8", "replace").strip()[:200])
        print("stat:", command(port, f"storage stat {remote}", quiet_s=1.0).strip()[:200])
    finally:
        port.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
