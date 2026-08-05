# ADR 0019 — The hostname moves to `almagest.aether.lan`

**Status:** accepted
**Date:** 2026-08-04

## Context

[ADR 0001](0001-base-url-and-tls.md) settled the origin as `https://almagest.lan`.
That name has never resolved — the DNS record was always a prerequisite for
provisioning rather than something already in place — so nothing physical has been
written with it yet. **No tag, no printed label, and no QR code carries the old
name**, which is the only reason this change is cheap.

The node the cluster runs on answers to `aether.lan`, and the app belongs under it
rather than beside it. `almagest.aether.lan` says whose it is.

## Decision

**The origin is `https://almagest.aether.lan`.** Everything else in ADR 0001 is
unchanged and is not reopened here:

- **`https` is not optional.** Web NFC and `getUserMedia` are gated behind a
  browser secure context, and plain http silently removes tag writing and camera
  scanning from the PWA rather than failing loudly.
- **A private CA, installed and trusted on every provisioning phone**, because a
  `.lan` name cannot get a publicly-trusted certificate. Still a hard prerequisite,
  now for a longer name.
- **The payload stays portless.** `https://almagest.aether.lan/s/{short_id}` is
  what goes on a tag and under a QR. ADR 0009's 443 problem is untouched: the
  cluster cannot serve 443, so something must terminate it and forward to the
  node's `30443`.
- **A tag scanned off-LAN still resolves to nothing.** Accepted then, accepted now.

### The QR budget shrank, and the old number was wrong anyway

ADR 0001 says the payload is "33 characters". It is not, and was not:

| | characters | NDEF URI payload |
|---|---|---|
| `https://almagest.lan/s/4K7T92M8` | 31 | 24 bytes |
| `https://almagest.aether.lan/s/4K7T92M8` | **38** | **31 bytes** |

The NDEF figure is the `0x04` (`https://`) abbreviation byte plus the remainder,
which is the whole reason that abbreviation is used. At 31 bytes inside an
NTAG213's 144 bytes of user memory there is no pressure at all, and the tag side
of this needs no thought.

**The QR side does.** A URL contains lowercase, so it encodes in byte mode, not
alphanumeric. Version 3 at ECC-M holds 42 bytes. The old payload used 24 of them;
the new one uses 38 — still version 3, still about 13 mm square at a 0.4 mm
module, but the headroom fell from 18 bytes to 4.

That matters for one specific future change and should be said plainly now:
**adding anything to the payload will push it to QR version 4.** A hyphenated
short ID (`4K7T-92M8`, 39 bytes) still fits; a query parameter, a longer
sub-domain, or a nine-symbol short ID does not. Version 4 is not a catastrophe —
it is a denser code and a slightly worse scan off a curved or scuffed label — but
it should be a decision somebody makes rather than a thing that happens.

## Consequences

- One string, changed in 54 tracked files: config, deploy manifests, the CI
  certificate's CN, the device agent's allowed-origin default, user-facing
  capability copy, and a large number of test fixtures where the hostname is
  arbitrary but consistency is worth more than the diff is worth avoiding.
- **`antlia/` is deliberately not changed.** Its `tools/gen_vectors.py` builds the
  C codec's test vectors from sample URLs, and those are parser inputs — the short
  ID codec does not know or care what host precedes `/s/`. Changing them would mean
  regenerating `tests/vectors.h`, committing inside the submodule and bumping the
  pointer, for no behavioural difference. The one thing that goes stale is a
  comment there calling `almagest.lan` "the real one"; worth fixing next time that
  file is touched for a real reason.
- **Ports, still to be opened** — none of this works until they are:
  - inbound **443/tcp** to whatever answers for `almagest.aether.lan`, because the
    payload is portless;
  - inbound **30443/tcp** on the node, which is filtered today (verified
    2026-08-04);
  - outbound **443/tcp** from cluster pods, for ADR 0017's datasheet research.
    Absent it the pipeline still runs, against offline sources only.
- **DNS is still a prerequisite and still absent.** `almagest.aether.lan` resolves
  to nothing as of this ADR, exactly as `almagest.lan` never did.

## Supersedes

The hostname in [ADR 0001](0001-base-url-and-tls.md), and nothing else in it.
