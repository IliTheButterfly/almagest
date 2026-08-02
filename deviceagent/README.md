# `deviceagent` — the bench-station device agent

A small daemon on the station's Raspberry Pi. It exists for exactly what a
browser sandbox cannot reach: the **PN532 NFC reader** on the Pi's UART, and
later the CSI camera. Barcode decoding is *not* here — that happens in the
browser, identically on a phone and on the kiosk.

It publishes an event stream over a **loopback WebSocket** that the kiosk PWA
subscribes to, and it owns the station session: PLAN.md's workflow 5, from a
container landing on the platform through take/add/recount, confirm and commit.

It never touches the database and never writes the ledger. Every commit is an HTTP
call to the existing `/api/stock/...` routes, so `app/services/ledger.py` stays the
sole ledger writer and a station movement is indistinguishable from a hand-entered
one except for its `source` and `device_id`.

```bash
uv sync                          # from deviceagent/, or `make agent-sync` from the root
uv run almagest-deviceagent --fake   # no reader needed: replays a scripted session
uv run almagest-deviceagent          # the real PN532 on DEVICEAGENT_PN532_PORT
```

`--fake` fakes the *reader*, not the API: point `DEVICEAGENT_API_BASE_URL` at a real
backend (`make run`) or every placement reports `station.failed`. A fake API would
mean a demo stream naming containers that do not exist, which is worse than a
visible failure.

## What is built, and what is deliberately not

Built: the `TagSource` protocol, a fake that replays a scripted session, the real
PN532 driver (unrun), NDEF decoding, NDEF-first-with-UID-fallback resolution, tag
presence, the station session, the API client, and the WebSocket stream.

**Nothing weight-related, per [ADR 0003](../docs/adr/0003-hardware-locked-and-the-scale-deferred.md).**
The load cell and its ADC are deferred, so there is no `WeightSource`, no
`weight.*` event, and no differential counting. PLAN.md's state machine opens
`IDLE → CONTAINER_DETECTED (weight jump > ~200 mg)`; with no scale there is no
weight jump and therefore no trigger, so **continuous PN532 polling** is the
first edge instead — the only replacement that keeps the property the station
exists for, that you set a container down and it identifies itself with no
gesture at all.

There is no feature flag for the missing scale and there must not be one. The
contract is: *scale absent → no `weight.*` ever emitted → the PWA hides every
by-weight affordance.* An affordance is drawn because an event arrived, not
because a capability list permitted it — which is why `station.hello`
deliberately does not enumerate which devices exist.

## The state machine, and what changed from PLAN.md

PLAN.md's workflow 5 is
`IDLE → CONTAINER_DETECTED → IDENTIFYING → IDENTIFIED | UNIDENTIFIED → WEIGHED →
READY → ACTION → CONFIRM → COMMIT`. ADR 0003 makes two of those impossible, so
they are **gone rather than stubbed** — a state that is always skipped is a lie in
a diagram someone will read later. What is built:

```
IDLE
 └─ a tag appears ──▶ IDENTIFYING       (PLAN.md: CONTAINER_DETECTED + IDENTIFYING)
      ├─ a carrier reads ──▶ RESOLVING                    (PLAN.md: IDENTIFIED)
      │    ├─ resolved ──▶ READY                 (PLAN.md: READY, no weight count)
      │    │                └─ propose ──▶ PROPOSED       (PLAN.md: ACTION + CONFIRM)
      │    │                     ├─ cancel ──▶ READY
      │    │                     └─ confirm ──▶ COMMITTING (PLAN.md: COMMIT)
      │    │                          ├─ committed ──▶ READY     ← the loop back to ACTION
      │    │                          └─ failed ──▶ PROPOSED     ← the same key retries
      │    └─ unknown tag ──▶ UNIDENTIFIED
      │         └─ provision it, then `station.refresh` ──▶ RESOLVING
      └─ budget spent (~5 tries) ──▶ UNIDENTIFIED
             └─ manual search only. `station.refresh` is *refused* here

from any state:  the container is removed ──▶ IDLE, `station.aborted`, nothing written
                 (PLAN.md: WEIGHED is gone — there is no scale)
```

**The two routes into `UNIDENTIFIED` differ, and `refresh` is the whole
difference.** `refresh` re-asks the server about a carrier the session already
read. The `unknown tag` route has one — a tag that read cleanly and that the
server has no binding for — so provisioning it in the PWA and refreshing reaches
`READY` without lifting the container. The `budget spent` route has none: no
carrier was ever read, so `refresh` is refused `nothing_to_resolve`, and a
container provisioned after a timeout comes back by being **set down again** —
presence takes its `UNIDENTIFIED → IDENTIFIED` re-seat edge with no teardown and
that is a fresh placement with a fresh session id. Drawing the `refresh` edge
under both would draw a transition nothing can take, which is exactly the mistake
that removing `CONTAINER_DETECTED` avoided.

| PLAN.md | here | why |
|---|---|---|
| `CONTAINER_DETECTED` | folded into `IDENTIFYING` | It named the weight jump. Under polling there is no observable moment between "something is there" and "we are trying to read it" — the same poll does both. |
| `IDENTIFYING` | `IDENTIFYING` | Unchanged: ~5 tries. The budget is a count of polls; "1.5 s" is that count times the interval and holds as a bound — see "The budgets are counts of polls". |
| `IDENTIFIED` | renamed `RESOLVING` | What happens between a carrier being read and `READY` is one round trip to `POST /api/location-tags/resolve`, which can fail or answer `unknown`. The interval needs a name. |
| `UNIDENTIFIED` | `UNIDENTIFIED` | Reached by a spent read budget *or* by a tag the server has no binding for. Both dead-end for the same user, and neither is an error. |
| `WEIGHED` | **removed** | No load cell (ADR 0003). Not stubbed. |
| `READY` | `READY` | Name, derived path, short id, ledger balance. The weight-derived count is absent, because no `weight.*` event ever arrives. |
| `ACTION` + `CONFIRM` | one state, `PROPOSED` | The agent hears about an action only when it is complete, so "choosing" and "reviewing" are indistinguishable from here; a confirm screen renders the pending action. |
| `COMMIT` | `COMMITTING` | An interval, not an instant — it is exactly where the abort guarantee changes hands. |

### Removing the container before COMMIT writes nothing

Three mechanisms, because this is the guarantee that matters — a half-finished
session that commits is stock that moved without anyone saying so.

1. the only path to a write needs a pending action **and** `state is PROPOSED`.
   `tag.removed` clears both before anything else runs;
2. every command carries the `session_id` minted when the container was
   identified. A confirm that races the lift arrives holding the previous
   session's id and is refused (`no_session` on an empty platform,
   `stale_session` once the next container has landed), so the user's last tap
   cannot land against the next bin either;
3. one lock serialises presence and commands, so a removal cannot interleave with
   a commit. It either precedes it (nothing is written) or follows it (the write
   was already confirmed, and the eight-second undo — not the tag — reverses it).

## The protocol

`ws://127.0.0.1:8765`, JSON text frames. Events flow agent → client; **four
commands** flow the other way, and they are the entire inbound surface. Anything
else a client sends is dropped unread.

The `tag.*` half started one-directional and stayed that way. The `station.*` half
is not, and workflow 5 is why: the process that must refuse a commit the instant
the container is lifted has to be the one holding the reader. A PWA that owned the
pending action could not be stopped by a removal it has not heard about yet.
Grammar keeps the directions apart — **commands are imperative, events are past
tense**: `station.propose` is something you ask for, `station.proposed` is
something that happened.

```json
{"type":"tag.identified","seq":12,"at":"2026-07-29T01:02:03.456000Z","data":{ … }}
```

| field | meaning |
|---|---|
| `type` | `<device>.<verb>`. The device half is what makes the scale additive. |
| `seq` | Monotonic for the agent's lifetime. `0` for `station.hello`, which is per-connection rather than part of the ordered stream. |
| `at` | ISO-8601 UTC with `Z`, the same format every API timestamp uses. |
| `data` | Per-type body. |

### Events

| type | when | `data` |
|---|---|---|
| `station.hello` | on connect, first | `protocol`, `agent`, `last_seq` |
| `tag.reading` | a tag is in the field and has not resolved yet | `poll`, `of` |
| `tag.identified` | the settled answer for this placement | `short_id`, `tag_uid`, `ndef_url`, `via` |
| `tag.timeout` | present, unreadable, budget spent | `polls` |
| `tag.error` | the reader faulted; once per run of faults | `message` |
| `tag.removed` | the field is confirmed empty, or the container was swapped | `missed_polls` |
| `station.ready` | the container is known: PLAN.md's `READY`, and the loop back to it after every commit or cancel | `session_id`, `client_op_id`, `location_id`, `name`, `label_path`, `short_id`, `matched_by`, `disagreement`, `total_qty_milli`, `lots[]` |
| `station.proposed` | an action exists and **nothing is written** | `session_id`, `client_op_id`, `action`, `projected_qty_milli` |
| `station.unidentified` | no readable tag, or one the server does not know | `session_id`, `reason`, the carriers, `offers` |
| `station.committed` | the movement is in the ledger | `session_id`, `client_op_id`, `action`, `seqs`, `lot`, `replayed` |
| `station.aborted` | the session ended without committing | `session_id`, `reason`, `discarded` |
| `station.rejected` | the **station** refused a command | `session_id`, `reason`, `message` |
| `station.failed` | the **API** refused, or did not answer | `session_id`, `reason`, `message`, `action` |

Every `station.*` session event also carries `state`, so a kiosk renders off the
last frame it received rather than keeping a second copy of the state machine.

### Commands

| type | payload | meaning |
|---|---|---|
| `station.propose` | `session_id`, `action: {kind, lot_id, qty_milli}` | Enter or replace the pending action. `kind` is `take`, `add` or `recount`; there are three because three stock routes exist. |
| `station.confirm` | `session_id` | Commit the pending action. The only path in this process to a write. |
| `station.cancel` | `session_id` | Discard the pending action, keep the session. |
| `station.refresh` | `session_id` | Re-ask the server about the carrier this session already read. Never re-identifies, never writes. This is how a container whose tag **read** but was not yet bound reaches `READY` after being provisioned, without being lifted. A placement whose identify budget was spent read no carrier and is refused `nothing_to_resolve`: set that container down again instead. |

Two properties a command cannot violate: **quantities are absolute, never deltas**
(which is what makes a repeated proposal recognisable as a duplicate rather than a
second increment), and **a command can only name a lot the agent already
announced** — the vocabulary is "the lot you told me about", not "lot 4173", which
is what keeps a loopback socket from being a way to move any stock in the system.

`PLAN.md`'s fourth station action, "pour into the counting tray", needs vision
counting and is a later phase. Intake (`receive`) is workflow 1's: it needs a part
and a destination the station has no way to know.

Named to line up verb-for-verb with the scale vocabulary PLAN.md already fixes,
so adding the load cell later is new *types*, never a new protocol:

    weight.reading  ↔ tag.reading      a sample arrived; nothing decided yet
    weight.stable   ↔ tag.identified   the settling rule says this is the answer
    weight.timeout  ↔ tag.timeout      it never settled inside the budget
    weight.error    ↔ tag.error        the device faulted
    weight.zeroed   ↔ —                a tare has no tag analogue
    —               ↔ tag.removed      a scale never leaves the bench; a container does

`tests/test_events.py` asserts that parity, so drift is a test failure rather
than something a review has to notice.

### Properties a client can rely on

**A container that stays put produces silence.** The station loops in `PROPOSED`
and `READY` while the tag remains, so exactly one `tag.identified` and one
`station.ready` are emitted per placement. A poller that re-fired every read would
hand the PWA a fresh identification several times a second, and a take confirmed
against each is a stack of spurious ledger rows against a real bin.

**The loop back to `ACTION` neither re-identifies nor re-commits.** A second take in
one placement re-uses the resolved container and a **fresh** idempotency key: a
second commit under the first key would replay the first movement and silently move
nothing.

**A double-tap costs nothing.** A repeated identical proposal inside 400 ms is
silence, a second Commit tap after a commit is `nothing_pending`, and impatient taps
against a failing API send one request rather than four. The window is PLAN.md's
provisioning-walk debounce, reused a third time (`frontend/src/lib/scan/holdoff.ts`
is the same mechanism in the browser).

**A removal is debounced, a swap is not.** Three consecutive empty polls — at
most ~0.9 s at the default cadence, see the budgets note below — confirm a
departure, because a PN532 misses reads and removing the container
before `COMMIT` aborts and writes nothing — a single dropped read must not
discard the user's work. Two consecutive *different* tags need no debounce and
emit `tag.removed` (with `missed_polls: 0`, which is how you tell a swap from a
departure) before the new `tag.identified`.

### The budgets are counts of polls; the seconds are a bound

`identify_polls` (5) and `absent_polls` (3) are **counts**, and nothing in
`agent/presence.py` reads a clock — which is why the tests are exact rather than
sleepy. The durations quoted throughout this file and PLAN.md are those counts
times `DEVICEAGENT_POLL_INTERVAL_MS`, and they hold as an **upper bound**, not an
equality, and only while one poll's read fits inside one interval:

- `poll_forever` paces to a fixed period (`sleep(interval − elapsed)`). Sleeping
  the interval flat *after* the read would make the period `read + interval`, and
  the read is not free in exactly the cases these budgets govern: the PN532's
  anticollision attempt blocks for its full `DEFAULT_TARGET_TIMEOUT_S` (250 ms)
  before reporting an empty field, which is every empty-platform poll and every
  poll of an unreadable tag. Unpaced, five 300 ms polls against a reader that
  blocked 250 ms took 2.75 s, not 1.5 s. `tests/test_poll_loop.py` pins the
  arithmetic.
- 250 ms of a 300 ms interval leaves 50 ms for an NDEF read that is one UART round
  trip per 4-byte page, so **whether a real poll fits at all is unmeasured** —
  item 2 below. If it does not, the cadence becomes the read and both budgets
  stretch; the agent logs that once per run of overruns rather than letting it
  pass silently, so the claim checks itself the day a reader exists.

### Reconnecting

`station.hello` carries `last_seq`; if the client's own high-water mark is lower,
it missed events. Immediately after hello the agent replays the last
state-defining event **with its original `seq`** if a container is currently on the
platform — `tag.identified`, `tag.timeout`, `station.ready`, `station.proposed` or
`station.unidentified`, whichever came last. A client that already processed that
seq ignores it; a freshly-opened tab renders it. That is the whole dedupe
mechanism, and it is why a kiosk reload does not require lifting the container and
putting it back down. A mid-session reload therefore comes back to the *confirm*
screen with the pending action and its idempotency key intact.

`tag.removed` and `station.aborted` empty the slot: the platform is clear, and a
replay hours later would render a container that is back in its cabinet.

`tag.reading` is never replayed — it is stale by definition, and replaying it
would show a spinner that never resolves. Neither is `station.committed`: a
reconnecting client would re-render a movement as if it had just happened, and the
`station.ready` that follows every commit carries the part that is still true.

### Resolution is NDEF-first, and the agent does not decide

The agent reads the NDEF URI record, extracts the short id (mod-37 check symbol
verified), and falls back to the recorded UID when the NDEF is absent or
unreadable — the degraded case that matters, because the UID lives in
factory-locked pages 0-2 while NDEF lives in user memory at page 4, so an
interrupted write leaves a UID-only tag rather than a dead one.

Both rules are imported from `idcodec.tagpayload` (`parse_ndef_url`,
`normalize_tag_uid`) and **never reimplemented**: a UID folded by a different rule
is invisible to the `location_tags` binding it should match while looking
perfectly correct in the event payload.

`idcodec` is the same code the API runs — `app.services.provisioning` re-exports
`parse_ndef_url` verbatim and wraps `normalize_tag_uid` only to translate its
`InvalidTagUid` into the `ProvisioningError` four routes turn into a 422, so the
folding rule is the same one this process runs — and it declares no dependencies
at all, so sharing
them costs this process nothing. It used to: the agent depended on
`almagest-backend` for exactly these functions and got fastapi, sqlalchemy,
alembic and pint onto the Pi along with them. `almagest-backend` is now a
**test-only** dependency, for `tests/test_session_ledger.py` alone.

`tag.identified` therefore carries **both carriers, verbatim**. The authoritative
answer comes from `POST /api/location-tags/resolve`, which is given both and
reports `disagreement: true` when a tag's payload names one slot and its UID is
bound to another. Preferring either carrier in the agent would hide exactly the
mis-binding the verification walk exists to find, so `via` says which carrier
produced the local short id and claims nothing more.

## The bridge: readers that are not the station's

Since ADR 0013 this daemon is two things at once, and keeping them apart is the
whole design.

**The station half** is everything above: one reader, a presence machine, a
session, workflow 5. Its cadence is a contract — the identify budget and the
removal debounce are both counted in *its* polls — and nothing else may perturb
it.

**The bridge half** discovers whatever readers exist, announces what each can
do, and carries out writes. It exists because ADR 0012 left a gap: the only
reader that could write a tag was Web NFC, which is Chromium-on-Android. A
desktop could not provision. An iPhone could not provision. The Pi kiosk bolted
to the bench next to the only reader in the building could not provision.

The two run concurrently over one hub and share nothing but the identity rules
and the registry. **A reader can be in both roles but is polled by exactly one
of them**: the station's PN532 is `adopt`ed into the roster so it can be
*written to*, and the bridge loop never touches it. Two loops on one UART is a
wedged reader and two silently wrong budgets.

### Which readers

| Reader | UID | URI | Can write | Discovered by |
|---|---|---|---|---|
| Station PN532 | yes | yes | yes (unrun) | adopted at startup |
| Flipper Zero over USB | yes | yes | yes¹ | `/dev/serial/by-id` sweep |
| Flipper Zero over BLE | yes | yes | yes¹ | `bleak` scan, **opt-in** |

¹ once Antlia's bridge mode is installed on the device; it announces its own
capabilities in `HELLO` and a build without a write path answers `r`.

A Flipper is launched into bridge mode automatically — the host sends
`app_start_request{args: "RPC"}` and nobody touches the device's screen — and
everything Almagest-specific travels as a text line protocol inside
`app_data_exchange_request`. That keeps the modelled protobuf surface at six
messages; see `agent/flipper/proto.py`, whose field numbers come from upstream
and are asserted byte for byte.

**BLE and USB are one protocol, not two.** The firmware exports
`ble_profile_serial_set_rpc_active`, so Bluetooth carries the same RPC stream as
the CDC endpoint. One codec, one session, two byte transports.

### The bridge's events

| Event | Meaning |
|---|---|
| `device.attached` | a reader appeared, with its capability set |
| `device.detached` | it went away (`unplugged` or `failed`) |
| `device.error` | it could not be opened, said once per run of failures |
| `tag.seen` | one debounced tap, with `device_id` |
| `tag.writing` | a write started; nothing decided |
| `tag.written` | it finished — carries the **read-back URI** |
| `tag.write_refused` | the tag said no; **nothing was written** |
| `tag.write_failed` | the reader broke; whether anything was written is unknown |

and one command, `tag.write {request_id, device_id, url, overwrite}`.

Three things about that table are load-bearing:

**`device.attached` carries capabilities, and `station.hello` still enumerates
nothing.** ADR 0003's rule — no feature flags, an affordance is drawn because an
event arrived — is about sensors whose absence is silence. A write is not drawn
from a stream: it is a command issued against a *named* device, and no history
of `tag.identified` distinguishes a PN532 that writes from a Flipper that does
not, nor says which of two attached readers to hold the tag against.

**`tag.seen` is not `tag.identified`.** The latter is the output of the presence
machine. A provisioning walk wants a debounced tap, which is what the browser's
`TagPresentation` models. Overloading one on the other would force one of them
to pretend.

**`tag.written` carries a URI and no boolean.** ADR 0012 refuses a
client-computed `verified: true` and makes `POST /api/location-tags/{id}/write-result`
take the read-back URI, compared server-side by short id. The bridge is a
client: it writes, reads back through the same reader, and reports the string.
It never posts it — the PWA does, because the PWA holds the provisioning session
and is the only thing that knows the `tag_id`.

A write that *fails* leaves it unknown whether anything landed, which is exactly
ADR 0012's `degraded`: the UID is in factory-locked pages 0-2, so the tag still
identifies itself and the honest next step is to read it back.

### The browser has to be allowed to connect

The PWA is served from `https://almagest.lan` (ADR 0001) and opens
`ws://127.0.0.1:8765`. Loopback is "potentially trustworthy" per the
secure-context spec, so this is *specified* to work — but Chrome's Private
Network Access rollout adds a preflight for public→local requests, so the
handshake answers with explicit CORS and
`Access-Control-Allow-Private-Network` headers rather than relying on the
default. Getting this wrong costs a bridge that is running, answers `curl`, and
is invisible to the page.

That is a compatibility shim and **not** authentication. The socket's security
is that it is bound to the loopback and refuses to be bound anywhere else.

## Testing without hardware

**No PN532 has ever been attached to this code.** Structure follows from that:

- `FakeTagSource` replays `agent/fixtures/scripted_session.json` — no tag, a tag
  arriving, the same tag still present, a dropped read, the tag leaving, an
  unreadable tag, a reader fault, a UID-only tag, a swap, a foreign card. It is
  shipped in the package (not in `tests/`) so `--fake` and the tests replay the
  same bytes, and so the PWA's station screen can be developed with nothing on
  the desk.
- That fixture is **hand-written, not recorded**, and says so in its own
  `description` field — with a test asserting it still says so. Re-record it from
  real polls the day a reader exists; the tests assert the *situations*, so a
  re-recorded file containing them keeps every one as a real regression test.
- `Pn532TagSource` is the thinnest module here on purpose. Its contract test is
  `tests/test_pn532_live.py`, marked `live` and skipped by default, and it is the
  checklist for the day the hardware arrives.
- `tests/test_poll_loop.py` drives the real loop against a reader that **charges a
  clock the test owns** instead of blocking. Every fake here is instant, which is
  how a poll period of `read + interval` looked correct for as long as it did; a
  reader with a cost, and no sleeping, is the only way to hold the loop to the
  arithmetic four other files quote.
- The **session** is tested twice. `tests/test_session.py` drives every edge off
  `FakeTagSource` and a fake API, with the 400 ms hold-off reading a clock the test
  steps by hand — nothing sleeps, because the identify budget and the removal
  debounce are counts of polls. `tests/test_session_ledger.py` then runs the same
  flows against a **migrated temp database and the real routes over a real
  loopback socket**, so "removing the container writes nothing" is a
  `SELECT count(*)` over `stock_ledger` rather than a fake's empty list.
- `agent/api.py` is hand-written where `CLAUDE.md` says API clients are generated.
  `tests/test_api_contract.py` is the compensating control: it reads the committed
  `openapi.json` and asserts every path, request field and response field this
  client uses still exists. A renamed route is a red `make check`, not a bench
  surprise. Past a handful of endpoints, generate it.

### Unverifiable without hardware

1. **Read range through the platform.** A bottom-pocket tag ~8-12 mm above the
   antenna through printed PETG. PLAN.md calls antenna centring the design's
   biggest unknown; ADR 0003 removed the load cell that was to be mounted beside
   it, so even the geometry to be tested is not final.
2. **How long one poll actually costs, and therefore what the budgets are in
   seconds.** Two parts, and both are unmeasured. The anticollision attempt is
   charged on *every* poll — `read_passive_target` blocks for its full 250 ms
   timeout before reporting an empty field, so the empty-platform and
   unreadable-tag polls that the removal debounce and the identify budget are made
   of are the expensive ones. On top of that, `ntag2xx_read_block` is one UART
   round trip per 4-byte page, so an NDEF read has 50 ms of the default interval
   left. Consequences if a poll does not fit: the fixed-period pacing degrades to
   the read time and both budgets stretch (logged, not silent — see "The budgets
   are counts of polls"), and the fix for the NDEF half is a UID-first fast path
   that reads user memory once per placement, which the state machine already
   tolerates because it accepts a UID-only poll. **`5 × 300 ms = 1.5 s` is
   arithmetic this machine can check; that a real reader honours it is not.**
3. **Whether the chosen debounce and identify budgets feel right.** 5 polls and 3
   empty polls come from PLAN.md and from reasoning about the failure modes, not
   from watching anyone use a bench. Separate question from item 2: that one is
   whether the seconds are what we say, this one is whether the seconds are what
   anyone wants.
4. **Which tags answer.** NTAG213/215/216 assumed; anything else is a UID-only
   tag as far as this code is concerned.
5. **Pi UART setup.** `enable_uart=1`, and the Bluetooth modem moved off the
   primary UART, are host configuration this package cannot do and has not been
   able to try.
6. **Whether awaiting the API inline is the right trade.** While a placement or a
   commit is outstanding the reader is not polled, deliberately: interleaving a
   commit with a container swap is a ledger row against the wrong bin, whereas a
   late-noticed removal costs a fraction of a second. Nobody has yet stood at a
   bench with a slow API to say whether that feels wrong.
7. **What a disagreeing tag should do.** When a tag's payload names one slot and
   its UID is bound to another, the station reports `disagreement: true` and
   carries on rather than blocking — the server chose NDEF, the movement is
   undoable, and the verification walk is the designed repair. Whether a user
   *notices* the warning before committing is a UI question no test here can
   answer.

## Configuration

Every key is documented in the repo-root `.env.example`. Prefixed
`DEVICEAGENT_` rather than `ALMAGEST_` because these settings are about one
physical machine's ports, not about the deployment.

| key | default | note |
|---|---|---|
| `DEVICEAGENT_WS_HOST` | `127.0.0.1` | **Refused if not a loopback address.** The socket is unauthenticated and narrates every container handled at the bench. |
| `DEVICEAGENT_WS_PORT` | `8765` | |
| `DEVICEAGENT_POLL_INTERVAL_MS` | `300` | The poll *period*: the loop sleeps `interval − elapsed`, not the interval flat. × `IDENTIFY_POLLS` **bounds** PLAN.md's ~1.5 s identify budget, and only while one read fits inside one interval — unmeasured, see item 2 above. |
| `DEVICEAGENT_IDENTIFY_POLLS` | `5` | A count of polls, which is what the code honours. |
| `DEVICEAGENT_ABSENT_POLLS` | `3` | Empty polls before a removal is believed; ≤ ~0.9 s at the default cadence, same caveat. |
| `DEVICEAGENT_PN532_PORT` | `/dev/ttyAMA0` | |
| `DEVICEAGENT_API_BASE_URL` | `http://127.0.0.1:8000` | The API **as reachable from the Pi** — not `ALMAGEST_BASE_URL`, which is the public origin stamped into tags and labels (ADR 0001) and must stay put. `https` needs that ADR's private CA in the Pi's trust store; there is deliberately no switch to skip verification. |
| `DEVICEAGENT_API_TIMEOUT_S` | `5` | Bounds how long one round trip holds up the poll loop. |
| `DEVICEAGENT_DEVICE_ID` | `station` | Recorded on every movement (`client_operations.device_id`). |
| `DEVICEAGENT_COMMAND_DEBOUNCE_MS` | `400` | PLAN.md's provisioning-walk debounce, reused. `0` disables it. |

There is no Dockerfile: this process runs on the Pi, beside kiosk Chromium, not
in the cluster.
