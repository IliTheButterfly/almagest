# 0013 — The RC522 as a second reader, and what that costs

Status: accepted, 2026-08-01

## Context

`docs/PLAN.md` picks the station's NFC reader explicitly, and rejects this one by
name:

> **Station reader: genuine Adafruit PN532 over UART (~$40)** with
> `adafruit-circuitpython-pn532`, which is maintained and reads NDEF natively.
> MFRC522 is rejected despite costing ~$3: its Python ports are UID-focused with
> hand-rolled NDEF and several unmaintained forks — the $37 premium buys a
> maintained NDEF-native library.

Two facts have changed since, and neither is about price.

**An RC522 is on the shelf and a PN532 is not.** ADR 0003 locked the hardware and
listed what was committed to; no reader was bought. So `Pn532TagSource` has never
run — `deviceagent/README.md` says so in three places, and its contract test is
`live`-marked and skipped on every CI run. The station's whole state machine, the
presence filter, the NDEF-first resolution and the ledger guarantees sit behind a
driver nobody has ever seen answer.

**The rejection was about an ecosystem, not about silicon.** The MFRC522 is a
13.56 MHz ISO/IEC 14443-3 Type A reader, which is exactly and only what NTAG213
needs: anticollision for the UID, `READ` for user memory. Everything PLAN.md
objects to is downstream of the *libraries* — and this repo does not need one. The
NDEF half was already written and unit-tested (`agent/ndef.py`) against a
`read_page` callable, because it was factored that way for the PN532. What was
missing was the ISO 14443-3 half.

## Decision

**`Rc522TagSource` is added as a second `TagSource`, and `pn532` stays the
default.** `DEVICEAGENT_READER` selects, `--reader` overrides it for one run, and
`build_source` remains the one place a reader is chosen. Both drivers import their
transport inside their constructor, so a station installs one library, not two.

**The ISO 14443-3 layer is ours, in `agent/iso14443a.py`, and is unit-tested.**
That is the whole answer to "several unmaintained forks": there is no fork. The
module takes a `transceive` callable and does anticollision, the cascade, CRC_A
and the `READ` framing as pure functions, in the same shape and for the same
reason `agent/ndef.py` takes a `read_page` callable — a decision that can be
tested without hardware should be.

**The specific bug being designed against is the 7-byte UID.** NTAG213's UID
arrives in two cascade levels with a `0x88` cascade tag in front of the first
three bytes. The recurring failure in MFRC522 libraries written for MIFARE
Classic is to keep that `0x88`, or to stop after level 1 and return four bytes of
seven. In this system that does not fail loudly: `deviceagent/pyproject.toml`
already records why `almagest-idcodec` is a shared distribution rather than a
copy — a UID folded by a different rule "is invisible to the binding it should
match while looking perfectly correct in both places", and a verification walk
then reports a whole cabinet as swapped. So both cascade lengths are driven from
scripted frames in `tests/test_iso14443a.py`, every malformed answer is asserted
to come back as *present but unreadable* rather than as a UID, and the live test
asserts the UID is 14 hex characters with a message saying what a shorter one
means.

**Single-tag anticollision only.** The bit-oriented collision-resolution loop is
not implemented. Two tags in the field corrupt the anticollision answer, its BCC
fails, and the poll reports present-but-unreadable — which the station already
renders as "this container's tag will not read". Guessing which of two tags was
meant would be the wrong answer to give a bench.

## Consequences

- **The station path can finally be run.** That is the point. An unrun driver is
  worth less than a run one, and every unverified claim in `deviceagent/README.md`
  — read range through the platform, what a poll actually costs, whether the
  budgets feel right — becomes measurable with hardware that already exists.
- **Less range margin, and that is the real cost.** PLAN.md calls antenna centring
  the design's biggest unknown and quotes a PN532's 30–50 mm open-air NTAG213
  range against a tag sitting 8–12 mm above the antenna through PETG. A stock
  RC522's small, often poorly-tuned antenna gets appreciably less. The driver
  therefore sets the receiver gain to its 48 dB maximum and does not expose a way
  to lower it. If the platform does not read, this is the first suspect and the
  PN532 is the fix.
- **We now own an ISO 14443-3 implementation.** Roughly 200 lines, tested against
  frames written by the same person who wrote the parser — which is exactly the
  weakness the ECIA fixture set has, and it is mitigated the same way: the one
  externally-fixed vector available without a reader (HLTA is `50 00 57 CD`)
  anchors the CRC, and the live test is the checklist for everything else.
- **Empty polls get much cheaper, which unblocks a documented worry.**
  `deviceagent/README.md` item 2 is largely about the PN532 spending its full
  250 ms timeout before admitting the field is empty, on every poll the removal
  debounce and the identify budget are made of. The RC522 path is bounded by the
  chip's own 25 ms timer, so `5 × 300 ms` has room it did not obviously have. The
  live test measures it rather than assuming it.
- **Writing tags is now possible but is not done here.** ADR 0012 records that the
  station cannot write, so bench-bound tags stay `unverified` until a phone or the
  verify walk reads them. An MFRC522 issues `WRITE` as readily as `READ`, so that
  gap is now a decision rather than a hardware limit — but it stays open, because
  writing means the write-result round trip of ADR 0012 and a provisioning UI at
  the station, not just a command byte.
- **PLAN.md's paragraph stands as written.** It is the right call for a reader
  someone still has to buy, and `pn532` remains the default for exactly that
  reason. This ADR is the exception and its cost, not a reversal.

## Alternatives rejected

**Using an existing MFRC522 Python library.** `mfrc522` and `pi-rc522` are the two
obvious candidates and both are MIFARE-Classic-shaped: UID-focused, patchy on
7-byte UIDs, and with no Type 2 `READ` path that returns pages to a caller like
`agent/ndef.py`. Adopting one would import precisely the risk PLAN.md named, and
in the one place where a wrong answer is silent.

**Replacing the PN532 driver.** It is written, it is thin, and it is what the plan
specifies. Deleting an unrun driver in favour of a differently-unrun one trades a
known-good specification for whatever is on the shelf this month.

**Probing for whichever reader answers.** Tempting, and wrong: a station whose
reader had come unplugged would fall through to the other and report an empty
platform rather than a broken reader. `TagSourceError` exists to keep exactly that
distinction, because one of them is fixed by re-seating a drawer and the other is
not.

**Waiting for the load cell.** Unrelated. ADR 0003 defers the scale, and continuous
polling is what replaced the weight trigger — so the reader is the *only* sensor
the station has, which is an argument for having one that works, not for waiting.
