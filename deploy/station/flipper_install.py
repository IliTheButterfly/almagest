#!/usr/bin/env python3
"""Upload a .fap to a Flipper over its text CLI.

`ufbt install` is the normal route and cannot be used here: the fbt toolchain
ships x86_64 binaries only, and the bench machine is aarch64. The CLI's
`storage write_chunk <path> <size>` takes an exact byte count, so a binary
upload needs nothing but pyserial.

    python flipper_install.py /dev/serial/by-id/usb-Flipper... /tmp/antlia.fap \
        /ext/apps/NFC/antlia.fap
"""

from __future__ import annotations

import hashlib
import posixpath
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
    if len(sys.argv) != 4:
        print(__doc__)
        print("usage: flipper_install.py <by-id node> <local file> <remote path>")
        return 2
    node, local, remote = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(local, "rb") as handle:
        payload = handle.read()
    want = hashlib.md5(payload).hexdigest()
    print(f"uploading {len(payload)} bytes to {remote} (md5 {want})")

    # Upload beside the target and only move it into place once the md5 checks
    # out. The previous version removed the working `.fap` first, so a short or
    # corrupt write left the Flipper with no app at all — and said nothing,
    # because it returned 0 regardless.
    staging = posixpath.join(
        posixpath.dirname(remote) or "/ext", ".almagest-upload.tmp"
    )

    port = serial.Serial(node, baudrate=115200, timeout=0.2, exclusive=True)
    try:
        # Wake the CLI and swallow the banner.
        port.write(b"\r")
        drain(port)

        # Make the target's directory, following `remote` rather than a
        # hardcoded path. Already-exists is the normal answer and not fatal.
        parts = posixpath.dirname(remote).strip("/").split("/")
        for depth in range(1, len(parts) + 1):
            command(port, "storage mkdir /" + "/".join(parts[:depth]))
        command(port, f"storage remove {staging}")

        # `\r` only, never `\r\n`. The CLI terminates a line on `\r` and leaves the
        # `\n` in the stream — which then becomes the *first byte the payload
        # reader sees*, shifting the whole file by one and dropping its last
        # byte. The file lands with exactly the right size and the wrong md5,
        # which is as quiet as corruption gets.
        port.write(f"storage write_chunk {staging} {len(payload)}\r".encode())
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

        drain(port, quiet_s=1.0, limit_s=15.0)

        # Verify before replacing anything. The corruption this file exists to
        # avoid produces a file of exactly the right *size*, so a `stat` is not
        # evidence — only the digest is.
        got = command(port, f"storage md5 {staging}", quiet_s=1.5)
        if want not in got:
            print(f"md5 MISMATCH: wanted {want}, device said {got.strip()[:120]}")
            print("leaving the previous file in place and removing the upload")
            command(port, f"storage remove {staging}")
            return 1

        command(port, f"storage remove {remote}")
        moved = command(port, f"storage rename {staging} {remote}", quiet_s=1.0)
        stat = command(port, f"storage stat {remote}", quiet_s=1.0)
        if str(len(payload)) not in stat:
            print(
                f"the file did not land: {moved.strip()[:120]} / {stat.strip()[:160]}"
            )
            return 1
        print(f"installed {remote}: md5 verified, {len(payload)} bytes")
    finally:
        port.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
