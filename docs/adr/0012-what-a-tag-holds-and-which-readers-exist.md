# 0012 — What a tag holds, as distinct from what we bound; and which readers exist

Status: accepted, 2026-07-31

## Context

Tag provisioning had a complete API and no callers. The PWA could *read* a tag and
could not write one, so PLAN.md's "create the row → write the NDEF URI → **read
back to verify** → print the label card" existed only as far as the first step.
Three things followed from that, and they are the reason for this record.

**1. The record claimed a write that had not happened.** `bind` stamps
`written_at = now()`, which is honest about the *binding* and reads as a claim
about the *sticker*. The server never holds the tag, so it cannot know: only the
device with the tag against it can write user memory, and only that device can read
it back. A write that failed partway therefore looked identical to one that
worked — and `LocationTag`'s own docstring already promised that such a tag is
"flagged by the verify screen", with nothing anywhere to do the flagging.

**2. Capability probes lie about readers.** The screens gated tag affordances on
`typeof NDEFReader === "function"`. That probe answers exactly one question — does
*this browser* implement Web NFC — and it is not the question. A Flipper Zero
running [Antlia](../NAMING.md), a $25 USB barcode scanner and an ACR122U all read
tags perfectly well on a desktop Chromium that will never have `NDEFReader`; and on
Android the probe succeeds while the radio is switched off. Iliana, on seeing the
first draft: *"Since the scanners can be connected over usb (like the flipper) nfc
availability can lie."*

**3. The daily use of a tag was missing entirely.** Both walks answer "what should
I bind this to?", once per cabinet. What happens every day is the opposite
question, asked against an expectation:

> *I look at the list and go grab all the containers I need. I sit down at the desk,
> scan the first container. A confirmation or error message shows if I got the right
> container.*

## Decision

### `ndef_state` is a separate fact from the binding

`location_tags` gains `ndef_state` (`unverified` | `verified` | `degraded`) and
`ndef_checked_at`. `written_at` keeps its meaning — when the binding row was
written — and stops being the only thing a reader could mistake for a claim about
the sticker.

`POST /api/location-tags/{id}/write-result` takes the **read-back URI**, not a
boolean the client computed. The comparison is by short id, not by string, so a tag
written before a hostname change is still correct rather than one of three hundred
spurious rewrites; keeping that rule on the server means every client gets it.

Three states rather than two, because the third is the common one. Chrome does not
re-fire `reading` for a tag that never left the field, so "no read-back arrived"
happens constantly and means nothing about the tag. Only a read-back that *arrived
and disagreed* is evidence. Collapsing them would make the verify screen cry wolf,
and a verify screen nobody believes is worse than none.

A degraded tag is **never** an unbound tag. The UID lives in factory-locked pages
0–2, physically separate from user memory at page 4, so a failed write leaves the
drawer perfectly identifiable at the station and merely untappable by a phone. That
is a rewrite to offer, not a binding to drop.

The verification walk carries the same distinction: `check` accepts the URI the
same reading returned plus `carries_ndef`, so the walk catches a bad *write* as
well as a bad *sticker*. `carries_ndef` is load-bearing — a hand-typed UID has said
nothing about page 4, and without the flag every hand-verified drawer would be
marked for a rewrite it does not need.

### A reader is a capability set, never a supported/unsupported flag

`TagPresentation` carries three carriers — `uid`, `url`, `shortId` — and readers
differ in which they produce:

| Reader | UID | URI | Short id | Can write |
|---|---|---|---|---|
| Web NFC (Chrome/Android) | yes | yes | — | yes |
| Station PN532 | yes | yes | — | not yet |
| USB wedge (Antlia, barcode scanner) | — | sometimes | yes | no |
| Typed by hand | either | — | either | no |

This is what replaces the boolean. A wedge confirms a container all day and can
never bind one, because binding is a claim about a specific piece of silicon and a
wedge types what the tag *means*. Screens say that, rather than failing obscurely.

**A wedge is detected by its terminator, never by typing speed.** Antlia types at a
configurable 5–60 ms per key, squarely inside human range, so the obvious
>50 chars/sec heuristic would classify the only NFC reader a laptop has as "a person
typing" and never fire. A CR/LF-terminated line is a scan; inter-key timing may
inform an affordance and never the gate. Both payload forms are accepted, bare
`4K7T-92M8` and a full `/s/{short_id}` URL, since both already resolve at step 1 of
the resolver chain.

Consequently the wedge listener is **always installed**. It cannot be probed for,
and the honest UI says "this browser has no Web NFC — a plugged-in reader still
works" rather than "you have no reader".

### Confirming against an expectation is its own component

`ConfirmScan` takes the expected container and answers *right* / *wrong* /
*unbound* / *disagreement*. A wrong scan **names what was actually scanned** —
"that is Cabinet B / A2, you want Cabinet A / A1" — because "wrong drawer" is not
something a person can act on. It never blocks: CLAUDE.md's "a scan is never
rejected" applies with more force here than anywhere, since the person is holding
the drawer and the database is not. The take proceeds, marked unchecked.

### Handing a walk to a phone is a URL, and nothing else

> *it would be cool to be able to transfer a session to a phone by scanning a qr
> code.*

There is no session to transfer, which is what makes it cheap. A pick's progress is
in the ledger as each take is recorded and its position is derived from what
remains — the same principle as the provisioning cursor being `MIN(sort_order)`
among untagged slots rather than a stored number. So `GET /api/handoff/qr.svg?path=`
renders a QR of `{base_url}{path}`: no handoff table, no expiring token, and two
devices on one walk is two views of the same rows rather than a conflict. The path
is validated to be same-origin, because encoding a caller-supplied absolute URL
would be an open-redirect generator with a QR code on the front.

## Consequences

- Every existing binding lands on `unverified`, which is the honest answer for a row
  no device ever confirmed. It is not a backlog of failures.
- The station's PN532 still cannot write, so binding from the bench leaves tags
  `unverified` until a phone or the verify walk reads them. That is a real gap, and
  it is now visible instead of silently mislabelled as written.
- A simulated reader ships in the app behind `?sim=1`, with a standing warning,
  because there is no reader on this setup and "it compiles" is not evidence that a
  walk works. It binds real rows to invented UIDs, so it is a demo, never a
  fallback.
- `BuildScreen`'s tab moved into `?tab=`, so the handoff link opens the walk the
  desktop was on rather than the default view.

## Alternatives rejected

**A boolean `ndef_written`.** Cannot express "no read-back arrived", which is the
majority case, so it would either under-report real failures or manufacture false
ones.

**Renaming `written_at`.** The name is imprecise, not wrong — it records when the
binding was written. Renaming a column means a SQLite table rebuild, and
`location_tags` is referenced by every walk; the docstring carries the distinction
at no risk.

**Trusting a client-computed `verified: true`.** Puts the host-agnostic comparison
in every client that ever writes a tag, and they will drift.

**Feature-detecting the wedge.** There is nothing to detect. It is a keyboard.
