# The bench station, as configured on a machine

The cluster deployment is [`deploy/README.md`](../README.md). This is the other
one: **the machine at the bench**, running the API, the PWA, the device bridge
and a kiosk browser, all on loopback, all as one user with no root.

It is written against the Jetson Nano currently on the bench (`ssh jetson`), but
nothing here is Jetson-specific except the two constraints in "What this machine
cannot do" — the same four units run on a Pi or a laptop.

## Why everything is on loopback, and why that is the point

ADR 0001 settles the public origin as `https://almagest.lan`, behind a private CA
that has to be installed and trusted on every device that provisions a tag. That
CA is a hard prerequisite for a phone. **It is not one here**, because
`http://127.0.0.1` is already a *potentially trustworthy origin* per the
secure-context spec: `getUserMedia` exists on it with no certificate at all.

So the station gets its camera for free, and the one thing it still does not get
is `NDEFReader` — kiosk Chromium has no Web NFC on any origin, which is precisely
the gap [ADR 0014](../../docs/adr/0014-the-device-bridge-and-how-a-reader-is-found.md)
opened the device bridge to fill. A Flipper on the end of a USB cable is this
machine's NFC reader.

One further consequence, and it is the one that bites: **the PWA and the API must
be the same origin.** `frontend/src/lib/api/client.ts` builds its client with
`baseUrl: currentOrigin()` and there is deliberately no configurable API host,
and `/s/{short_id}` is answered by the *backend* with a **relative** redirect
that the browser resolves against whoever served it. Serve the two on different
ports and a tag tap lands nowhere. `station_web.py` is what makes them one — it
is `frontend/nginx.conf` reduced to what loopback needs, in the standard library,
because there is no nginx and no root to install one.

## The four units

All `systemd --user`, all `WantedBy=default.target`, so they come up with the
logged-in session on the bench display. There is no `enable-linger` — that needs
root, and a station whose display is logged out is a station nobody is standing
at.

| Unit | What it is |
|---|---|
| `almagest-station-api` | `uvicorn` on 127.0.0.1:8000. One replica, always: SQLite tolerates one writer. |
| `almagest-station-web` | `station_web.py` on 127.0.0.1:8080 — the built PWA plus a pass-through to the API. |
| `almagest-station-bridge` | `almagest-deviceagent --reader none`. The WebSocket on 127.0.0.1:8765 and whatever USB readers it finds. |
| `almagest-station-kiosk` | Chromium, fullscreen, at `http://127.0.0.1:8080/scan`. |

```bash
./deploy/station/install.sh
systemctl --user start almagest-station-{api,web,bridge,kiosk}
systemctl --user status almagest-station-bridge
journalctl --user -u almagest-station-bridge -f
```

### `--reader none` is a statement, not a disabled feature

This bench has no PN532 under a platform; its only reader is on USB. That is
ADR 0014's second deployment shape — *"a laptop with a Flipper on the end of a
cable"* — and until now it had no way to start: `pn532` and `rc522` both exit 2
opening a port that is not there, before the bridge has looked for the reader
that is.

**Do not reach for `--fake` to get past that.** `FakeTagSource(..., repeat=True)`
replays a scripted placement forever, so an empty bench narrates a container
being set down every few seconds and logs `station.failed` on each one. A kiosk
showing a drawer that is not there is worse than one showing nothing.
`agent/no_reader.py` is the honest answer, and it is deliberately **not adopted
into the device roster**: the absence of a platform reader is communicated by the
absence of a `device.attached`, the same way ADR 0003 communicates the absence of
the scale.

## What this machine cannot do, and what to do instead

**It cannot build the PWA.** Node 22 requires glibc 2.28; Ubuntu 18.04 ships
2.27. This is not slowness, it is a hard incompatibility. Build on a workstation
and copy the bundle:

```bash
# on the workstation, from the repo root
cd frontend && pnpm install && pnpm build
rsync -a --delete frontend/dist/ jetson:prog/almagest/frontend/dist/
```

**It cannot open a USB reader until the operator is in `dialout`.** `/dev/ttyACM0`
is `root:dialout 0660` and a fresh Ubuntu user is not a member, so the bridge's
discovery sweep finds a Flipper it cannot open. This is the one step that needs
root and it is deliberately not in `install.sh`, because a script that asks for a
password is a script people run without reading:

```bash
sudo usermod -aG dialout "$USER"    # then log out and back in, or reboot
```

## The camera is mounted upside down

The bench webcam is on a bracket that holds it head-down, and nothing in
`getUserMedia` reports that — the frame is what the sensor saw, and only the
fixture knows which way up it is. The webcam here (a NexiGo UVC) exposes no
`vflip`/`hflip` control either; `v4l2-ctl -d /dev/video0 -l` lists pan, tilt,
zoom and focus and no rotation, so there is nothing to set in the driver.

It is therefore a **display setting in the PWA**, remembered per device:
`frontend/src/lib/scan/orientation.ts`, toggled from the button beside the
resolution readout under the viewfinder. Set it once on this machine and it
sticks in `localStorage`.

Only the picture turns, and that module explains at length why that is enough
rather than a shortcut: the decoder reads a *centred* crop, which a half turn
maps onto itself, and `decodeImageData` already passes `tryRotate: true` for the
symbologies that care. What an inverted mount actually breaks is a person trying
to aim, so that is what is fixed. **The still-capture path is a different
story** — see the note in that file before wiring the camera into anything that
OCRs a frame.
