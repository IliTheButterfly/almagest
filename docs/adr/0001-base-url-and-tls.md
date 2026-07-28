# ADR 0001 — Base URL is `https://almagest.lan`, served with a private CA

**Status:** accepted
**Date:** 2026-07-27

## Context

`ALMAGEST_BASE_URL` is not an ordinary setting. Its host is written **physically**
into every NTAG213's NDEF URI record and printed into every QR code, as
`{base_url}/s/{short_id}`. Changing it after tags exist means re-writing every
tag by hand. It has to be settled before provisioning starts, not after.

Two browser capabilities that Phase 1 depends on are gated behind a **secure
context** — `https://`, or the `localhost`/`127.0.0.1` exemption:

- **Web NFC** (`NDEFReader.scan()` / `.write()`), which is how the phone PWA
  provisions and verifies tags. Chrome for Android only.
- **`getUserMedia`**, which is how the phone scans distributor barcodes at intake.

Over plain HTTP both are simply absent — no error dialog, the feature just does
not exist. That would leave Phase 1 with no working tag-provisioning path at
all, since the station's PN532 reader is Phase 2 hardware.

`.lan` is not a public TLD, so no publicly-trusted CA will issue a certificate
for it.

## Decision

Serve the PWA at **`https://almagest.lan`** using a **privately-issued
certificate**, with the issuing CA installed on every device that provisions
tags or scans barcodes.

## Consequences

**Operational prerequisite — Phase 1.5 cannot start without this.** Before any
tag is provisioned:

- The CA certificate must be installed and trusted on each phone.
  - *Android*: Settings → Security → Encryption & credentials → Install a
    certificate → CA certificate. Chrome honours the user CA store for browsing.
  - *iOS*: install the profile, then **Settings → General → About → Certificate
    Trust Settings → enable full trust**. The second step is separate and is the
    one that is usually forgotten.
- `almagest.lan` must resolve for those devices — a local DNS record, or the
  VPN's DNS when off-LAN.
- The certificate needs a renewal plan. An expired cert breaks NFC provisioning
  and barcode scanning in the same silent way plain HTTP does.

**Accepted limitation: a tag scanned from outside the LAN/VPN resolves to
nothing.** `almagest.lan` is not answerable by public DNS. Tapping a drawer's
tag on a phone that is off the network opens a browser to a name that does not
exist. This was weighed against a Tailscale `*.ts.net` hostname, which gets a
real Let's Encrypt certificate and resolves over cellular; the shorter host was
preferred, and off-network scanning is not a workflow that matters here.

Mitigation already in the design: every printed label carries the bare MPN as
text under the QR, so a manual manufacturer-site search always works with no
infrastructure at all.

**Payload length.** `https://almagest.lan/s/4K7T92M8` is 33 characters — QR
version 3 at ECC-M, about 13 mm square at a 0.4 mm module including the quiet
zone. Comfortably inside the budget, and shorter than the `*.ts.net`
alternative would have been, which means a less dense QR and better scans off
curved or scuffed labels.

**Development is unaffected.** `http://localhost:8000` is a secure context by
specification, so the whole feature set works locally with no certificate. The
default in `app/config.py` therefore stays `http://localhost:8000`; only
deployed instances set `ALMAGEST_BASE_URL`.
