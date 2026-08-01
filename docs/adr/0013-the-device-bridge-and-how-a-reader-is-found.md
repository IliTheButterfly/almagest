# ADR 0013 — The device bridge, and how a reader is found

Status: accepted, 2026-08-01

Supersedes nothing. Extends **ADR 0012** (what a tag holds, and which readers
exist) with the half it left as a gap, and takes a position on **ADR 0003**'s
no-feature-flag rule that is narrower than it first appears.

## Context

ADR 0012 settled what a tag holds and drew the reader table:

| Reader | UID | URI | Short id | Can write |
|---|---|---|---|---|
| Web NFC (Chrome/Android) | yes | yes | — | yes |
| Station PN532 | yes | yes | — | **not yet** |
| USB wedge (Antlia, barcode scanner) | — | sometimes | yes | no |
| Typed by hand | either | — | either | no |

Two facts in that table are the whole reason for this ADR.

**Only one row can write, and it is the row that needs a phone.** Everything
Almagest knows about writing a tag is `frontend/src/lib/scan/nfc.ts`, which is
Web NFC, which is Chromium-on-Android and nothing else. A desktop cannot
provision. An iPhone cannot provision. The Pi kiosk — the machine bolted to the
bench next to the drawers, the one holding the only reader in the building —
cannot provision, because kiosk Chromium has no `NDEFReader`. ADR 0012 recorded
this honestly ("that is a real gap, and it is now visible instead of silently
mislabelled as written") and left it open.

**The row that could close it has no client.** `deviceagent` already runs a
loopback WebSocket carrying `tag.identified` with both carriers, and
`frontend/src/lib/tags/source.ts` already declares `station_pn532` as a
`TagDeviceKind`. Nothing implements it. Grep the frontend for `8765`, `ws://` or
`WebSocket` and the only hit is a doc comment. The socket has been talking to
nobody since it was built.

Meanwhile a third reader exists and is already in the repo. **Antlia** — the
Flipper Zero app — reads a container tag and types its short id as a USB
keyboard. It is, in PLAN.md's words, "the laptop's only NFC reader". It is also
deliberately crippled: it cannot write, because *"a tag written by a device that
cannot check the ID against the inventory is a tag that might be a duplicate."*
That objection is entirely correct about a Flipper acting alone, and entirely
void about a Flipper acting as a peripheral of something that *is* talking to
the inventory.

So the shape of the problem is: there is a process on the bench that can reach
hardware a browser cannot, there is hardware in a drawer that can read and write
tags, and there is no wire between them.

## Decision

### `deviceagent` becomes the device bridge. No new repo.

Adding a second process that also owns `/dev/ttyAMA0` would be a bug, not an
architecture — two pollers on one UART is a wedged reader. `deviceagent` already
holds the reader, already owns the loopback socket, already speaks the tag
vocabulary, and already runs on the machine the browser runs on. It grows a
device registry and a write path; it does not get a sibling.

The name stays. It is not a repo, so it does not get a constellation name
(NAMING.md), and "device agent" was always the accurate description of a process
whose job is to be the part of the browser that can reach a device.

What changes is its scope of deployment. It was specified as a Pi-side daemon
next to kiosk Chromium. It is now **whatever machine the browser is on** — the
Pi at the bench, or a laptop with a Flipper on the end of a cable. The loopback
bind (`agent.config._refuse_a_non_loopback_bind`) is what makes that safe and is
unchanged: the bridge is always same-machine, never a network service.

### A device is announced as a capability set, and this does not contradict ADR 0003

ADR 0003 is emphatic, and `agent/events.py` repeats it: `station.hello`
deliberately does not enumerate which devices exist, because that is the feature
flag ADR 0003 says not to build. *"Scale absent → no `weight.*` ever emitted →
the PWA hides every by-weight affordance. No special-casing."*

The bridge nonetheless publishes `device.attached` carrying a capability set.
The two are consistent, and the reason is worth stating precisely rather than
leaving to be re-derived in an argument later.

**The scale rule is about affordances derived from a stream. Writing is not
derived from a stream — it is initiated against a named device.** A weight
readout is drawn because a `weight.reading` arrived; nothing has to be known in
advance. A *write* is the client saying "put this URI on the tag that is in the
field of that reader". That sentence cannot be constructed from a history of
read events. It needs two things no event stream can supply:

- **Whether anything present can write at all.** A PN532 and a Flipper both emit
  identical `tag.identified` events, and one of them can write. There is no
  sequence of reads that distinguishes them.
- **Which one.** A bench with a PN532 under the platform and a Flipper on a USB
  cable has two readers in two physical places, and "write this tag" is
  meaningless until the user has been told which field to hold the tag in.

So the line is: **capability sets exist for devices the user chooses between and
issues commands to; they do not exist for sensors whose absence is simply
silence.** `station.hello` still enumerates nothing. ADR 0012 already committed
to this for readers in as many words — *"a reader is a capability set, never a
supported/unsupported flag"* — and its table is a capability matrix. This ADR is
that sentence made into a wire message.

Concretely, `device.attached` carries:

```json
{"device_id": "pn532:/dev/ttyAMA0", "kind": "station_pn532",
 "label": "PN532 on /dev/ttyAMA0",
 "capabilities": {"reads_uid": true, "reads_ndef": true, "writes_ndef": true}}
```

`device_id` is stable across a detach/reattach of the same physical thing, so a
PWA that had chosen a reader does not lose it when a cable is jiggled. It is
derived from the port or the Bluetooth address, never from a counter.

### A write returns a read-back URI, and the bridge does not report it

ADR 0012 refuses a client-computed `verified: true` and makes
`POST /api/location-tags/{id}/write-result` take the **read-back URI**, compared
server-side by short id. The bridge is a client. So the bridge writes, reads the
tag back through the same reader, and publishes what it read — a string, or null
if nothing came back. It never posts that anywhere.

The PWA posts it, because the PWA is the thing holding the provisioning session
and therefore the only thing that knows the `tag_id`. This keeps the bridge free
of any knowledge of provisioning walks: it is a device bridge, not a second
client of the API. (It does still commit stock movements over
`/api/stock/...` — that is the station session, an existing and separate
concern.)

A tag whose read-back is null is exactly ADR 0012's `degraded`, and the UID in
factory-locked pages 0-2 means it is still a perfectly identifiable tag. The
bridge reports the fact and takes no view.

### The Flipper is reached over its own RPC, on both transports

The Flipper's RPC is protobuf-over-a-byte-stream, and the firmware exports
`ble_profile_serial_set_rpc_active` — **BLE carries the same stream as USB CDC**.
So there is one protocol and two transports (`pyserial`, `bleak`), not two
integrations. This was checked against the Momentum `mntm-012` / API 87.1 symbol
table on the bench machine, not assumed.

The sequence is:

1. Discover a Flipper — a `/dev/serial/by-id/*Flipper*` node, or a BLE
   advertisement — and open an RPC session.
2. `app_start_request{name: "/ext/apps/NFC/antlia.fap", args: "RPC"}`. The app
   launches itself; nobody touches the Flipper's screen.
3. Everything Almagest-specific rides inside `app_data_exchange_request` as a
   **line protocol** — `READ`, `WRITE <url>`, and one-line answers. The firmware
   documents this exact use: *"bi-directional exchange of arbitrary raw data.
   Useful for implementing higher-level protocols while using the RPC as a
   transport layer."*

Point 3 is the load-bearing one. It means **the bridge models six protobuf
messages and no more**, and every Almagest concept stays in a text protocol that
can be read in a log and tested against a fake. Modelling the Flipper's full RPC
surface to send `WRITE` would have been a large dependency for no gain.

The six are hand-encoded with a stdlib varint writer, and their field numbers
are pinned from upstream `flipperzero-protobuf` in one table with a test that
asserts the encoding byte-for-byte. Rationale is the same as
`deviceagent/pyproject.toml`'s: this process runs on a Pi and every dependency is
one more thing to apt-pin. The transports themselves are behind an optional
`flipper` extra, so the Pi installs neither.

### Antlia must not claim USB HID in bridge mode

Claiming USB HID **replaces the CDC interface** — that is already why Antlia's
claim is scoped to its scan view, and it is already known to strand the Flipper
CLI until someone physically presses a button. Under RPC the CDC interface *is*
the session giving the orders, so claiming HID would sever the connection
mid-command. It is not a trade-off; it is a self-inflicted disconnect.

This costs nothing, because it is redundant. HID exists to get the short id into
a computer that has no other channel. In bridge mode there is a channel, and it
is better: the bridge gets both carriers and can write, where the keyboard wedge
only ever got a short id.

Antlia therefore has two disjoint modes — **wedge** (today's behaviour, HID
claimed, no host) and **bridge** (RPC session, HID untouched) — and it enters
bridge mode only when launched with `args == "RPC"`.

### `ProvisioningDevice` gains `flipper_rpc`

Purely additive, no migration, no DDL. This is the payoff of the never-use-a-
`CHECK`-enum rule in CLAUDE.md, and it is the first time the rule has actually
been cashed in: a new kind of reader is three lines in `app/models/enums.py` and
a string in a TypeScript union.

## Consequences

**The PN532 can write, so the station can provision.** ADR 0012's gap closes for
the bench: binding from the station no longer necessarily leaves a tag
`unverified`, because the same reader that bound it can write it and read it
back. Whether it *does* is a hardware question nobody has answered yet — see
below.

**A laptop becomes a provisioning station.** With a Flipper on a cable and the
bridge running, a desktop Chromium that will never have `NDEFReader` can run a
full provisioning walk. This is the largest practical change: provisioning was
previously gated on owning an Android phone and trusting the private CA on it.

**The bridge is now optional-but-detectable, which is a new failure mode.** The
PWA tries `ws://127.0.0.1:8765` and must degrade in silence when nothing answers
— the overwhelmingly common case, since most page loads are on a phone with no
bridge anywhere. It must not warn, must not retry aggressively, and must not
delay first paint. A bridge that is not running is not an error; it is Tuesday.

**Mixed content and Private Network Access are a real risk and are handled, not
assumed.** The PWA is served from `https://almagest.lan` (ADR 0001) and opens
`ws://127.0.0.1:8765`. Loopback is "potentially trustworthy" per the secure-
context spec, so this is *specified* to work, but Chrome's Private Network
Access rollout adds a preflight for public→local subresource requests, and
browsers have historically differed here. The bridge therefore answers the
WebSocket handshake with explicit CORS and
`Access-Control-Allow-Private-Network: true` headers rather than relying on the
default. It remains possible that a future browser closes this path entirely, in
which case the answer is a `wss://` listener using the same private CA as ADR
0001 — noted, not built, because it needs a certificate story for a name that
resolves to 127.0.0.1 and that is a larger decision than this ADR.

**`PROTOCOL_VERSION` goes to 2.** A version-1 client sees `device.*` and
`tag.write.*` events it does not know and ignores them, which is the designed
behaviour of the envelope; but a version-1 client also cannot write, and telling
it apart from a version-2 client matters for the UI. The bump is cheap and the
alternative is guessing.

## Unverified, and honest about it

Nothing here has touched hardware, and the list is longer than usual:

- **No PN532 exists.** The write path is `ntag2xx_write_block` in a loop and has
  never run, exactly like the read path it sits beside. Its contract test is
  `live`-marked.
- **No Flipper has been driven by this code.** The RPC framing, the app launch,
  and the data-exchange round trip are tested against a fake that replays the
  byte sequences this codec produces. That proves the codec is self-consistent,
  not that a Flipper agrees with it. The field numbers come from upstream and the
  encoding is asserted byte-for-byte, which is the strongest check available
  without the device.
- **BLE is the least-verified leg.** `bleak` scanning, the GATT characteristics
  the serial profile exposes, and pairing are all untested; the bench machine has
  no Bluetooth stack installed at all. USB is expected to work first.
- **Antlia's write path does not exist yet.** `mf_ultralight_poller_sync_write_page`
  is exported by API 87.1, so it is possible; it is a separate change in a
  separate repo, and until it lands a Flipper in bridge mode reads only.
- **Whether a write survives the platform.** PLAN.md already calls antenna
  centring at 8-12 mm through PETG the design's biggest unknown, and a write is
  strictly more demanding of the coupling than a read.

## Alternatives rejected

**A new process, separate from `deviceagent`.** Two owners of one UART. The
station and the bridge also want the same event stream and the same identity
rules, and splitting them would mean two implementations of tag folding — the
precise failure `idcodec` exists to prevent.

**Web Serial / WebUSB, removing the bridge entirely.** PLAN.md already evaluated
and declined this for the station. It fails harder here: Web Serial is
Chromium-desktop-only, so it is absent on exactly the two clients that need a
reader most (the Pi kiosk and any phone), and it cannot see a BLE Flipper at all.

**Driving the Flipper by its text CLI instead of RPC.** Tempting — no protobuf
at all. Rejected because the CLI is bound to the USB CDC session and does not
exist over BLE, so it would buy simplicity on one transport at the price of the
other; and because a FAP driven over the CLI cannot be launched by the host,
which was the requirement.

**Screen-scraping the Flipper's stock NFC app over `gui_screen_frame`.** Reads
pixels, invents no protocol, requires no Antlia change, and is exactly the kind
of confidently-wrong reading CLAUDE.md forbids for OCR'd part numbers. A
misread short id that passes its check symbol is a container bound to the wrong
drawer.

**Having the bridge post `write-result` itself.** It would need the `tag_id`,
which means it would need the provisioning session, which means the bridge
becomes a second client of the walk API with its own idea of where the cursor is.
The PWA already holds that state correctly.
