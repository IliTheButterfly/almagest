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

# On systemd >= 240 (a Pi, a laptop) the user journal has it:
journalctl --user -u almagest-station-bridge -f
# On the bench Jetson (systemd 237) user units forward to syslog instead, and
# the journalctl form above answers "No journal files were found":
tail -f /var/log/syslog | grep almagest
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

## Checking a station actually commissions containers

```bash
uv run --no-project --python 3.12 -- python deploy/station/commission_smoke.py
```

Drives a whole provisioning walk and then a verification walk over HTTP against
the running station — no imports from `app`, so it exercises the station exactly
the way the kiosk does and anything it catches is something a person at the bench
would hit. It asserts the four conflict shapes, which are four different
sentences to someone holding a tag:

| | what happened | what the UI should say |
|---|---|---|
| `already_bound_here` | the same tag twice | nothing, and no undo step is spent |
| `already_bound_elsewhere` | this tag means another drawer | names it — Move here? / Cancel |
| `slot_already_bound` | this drawer already has a tag | names it — Move here? / Cancel |
| `two_conflicts` | both at once | **409, refused outright** — resolving it is two displacements and there is one undo slot |

and then the thing the verification walk exists for: a tag stuck on the wrong
drawer is reported with the reverse lookup ("this tag belongs to 02"), left
`resolved_at: null`, and **never auto-fixed** — no software can stop someone
sticking a tag on the wrong drawer, only detect it.

It writes real bindings, so run it against a station whose database is demo seed
data, not one holding a real cabinet.

## Commissioning with the Flipper, on hardware

```bash
# once per Flipper: put the bridge-mode app on it. `ufbt install` cannot be used
# here — the fbt toolchain is x86_64 and the bench is aarch64.
#
# `--no-project --with`, like the smoke command above: there is no root
# `pyproject.toml`, so a bare `uv run` at the repo root resolves no dependencies
# at all and these fail on `No module named 'serial'`.
uv run --no-project --python 3.12 --with pyserial -- \
    python deploy/station/flipper_install.py \
    /dev/serial/by-id/usb-Flipper_* antlia/dist/antlia.fap /ext/apps/NFC/antlia.fap

# the whole workflow against a real tag on the antenna. `--yes` is required: this
# picks a cabinet and a slot itself and then PHYSICALLY WRITES the tag, which no
# software undoes. Add `--overwrite` for a tag that already carries a URI.
uv run --no-project --python 3.12 --with websockets -- \
    python deploy/station/commission_hardware.py --yes
```

`commission_hardware.py` is `commission_smoke.py`'s hardware sibling and drives
the path the docs describe end to end: a provisioning walk binds **the tag the
reader actually saw**, the *bridge* writes the URI the server minted, reads it
back through the same reader, and the client posts that read-back so the
**server** decides `verified` — never the client, per ADR 0012. Then a
verification walk reads the tag again and confirms the drawer.

Two things it will teach you if you skip them:

- **`\r`, never `\r\n`, in `flipper_install.py`.** The CLI ends a line on `\r`
  and leaves the `\n` in the stream, where it becomes the first byte the payload
  reader sees — so the file lands with exactly the right *size* and the wrong
  md5, and the loader answers `ERROR_APP_CANT_START` while `storage stat` looks
  healthy. Always check the md5 against the local file.
- **Close whatever is running on the Flipper first.** `app_start_request` cannot
  launch Antlia while an app — including Antlia in wedge mode — is in the
  foreground; the CLI says `Loader is locked`. `loader close` fixes it.

## What this machine cannot do, and what to do instead

**It cannot build the PWA.** Node 22 requires glibc 2.28; Ubuntu 18.04 ships
2.27. This is not slowness, it is a hard incompatibility. Build on a workstation
and copy the bundle:

```bash
# on the workstation, from the repo root
cd frontend && pnpm install && pnpm build
rsync -a --delete frontend/dist/ jetson:prog/almagest/frontend/dist/
```

**Do not let it write tags with the wrong base URL.** The tag payload is
`{ALMAGEST_BASE_URL}/s/{short_id}`, and `.env.example` ships
`http://localhost:8000` — a URL that means "this machine" to every phone that
reads it, and therefore resolves to nothing. `almagest-station-api.service` sets
`ALMAGEST_BASE_URL=https://almagest.lan` (ADR 0001) for exactly this reason; if
you override it, override it to the public origin and not to the station's own
address. **A tag write is physical and no software undoes it.** The same value
decides what `location_tags.ndef_url` records at bind time, so getting it wrong
also makes every later verification read `degraded`.

Note the standing constraint in `CLAUDE.md`: **provision no tags until a reverse
proxy fronts 443**, because the cluster answers on `:30443` and a tag must carry
a portless URL. Commissioning *bindings* at this station is fine and is what
`commission_smoke.py` exercises; burning stickers is not, yet.

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

**It has never been looked at on a real inverted camera.** The webcam was
disconnected from the bench before that could be done, and every test for it runs
in jsdom — which asserts a class name and a stored value, not a picture. The
reasoning below is an argument, not a photograph.

The *preview* turns and the decode path is deliberately left alone, for reasons
that module sets out at length: the decoder reads a *centred* crop, which a half turn
maps onto itself, and `decodeImageData` already passes `tryRotate: true` for the
symbologies that care. What an inverted mount actually breaks is a person trying
to aim, so that is what is fixed. **The still-capture path is a different
story** — see the note in that file before wiring the camera into anything that
OCRs a frame.
