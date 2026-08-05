# Architecture decision records

Each file records one decision, why it was taken, and what it costs.
[PLAN.md](../PLAN.md) is the original design; an ADR is how that design gets
amended, so **where an ADR and PLAN.md disagree, the ADR wins.**

## Cite ADRs by slug, not by number

**Five numbers are used twice** — 0007, 0009, 0011, 0012 and 0013. Two work
streams (storage/authoring on one side, deployment and readers on the other)
numbered independently and both landed.

The numbers are **not being reassigned**, because roughly four hundred citations
of the form `ADR 00NN` already exist in code comments, migrations and the
submodules, and a renumbering would repoint some fraction of them at the wrong
decision without any test noticing. The collision is cheap to live with and
expensive to undo.

So a bare "ADR 0013" is ambiguous, and this index is how you resolve one. In a
*new* citation, name the slug — `ADR 0013 (rc522)`, or the path
`docs/adr/0013-the-rc522-as-a-second-reader.md` — never the number alone. The
next free number is **0020**.

## The records

| # | Title | What it settles |
|---|---|---|
| [0001](0001-base-url-and-tls.md) | Base URL and TLS — **hostname superseded by [0019](0019-the-hostname-moves-under-aether.md)** | The origin is written physically into every tag and QR, so it must be settled before provisioning. `https` is not optional: Web NFC and `getUserMedia` need a secure context |
| [0002](0002-recursive-container-types.md) | Container types are recursive; Gridfinity is the reference case | A container kind is a row, not a migration |
| [0003](0003-hardware-locked-and-the-scale-deferred.md) | Locked hardware, and the scale deferred | The load cell and NAU7802 are **not bought**. Supersedes PLAN.md's weight-triggered station machine: PN532 polling is the trigger, and `CONTAINER_DETECTED`/`WEIGHED` are gone rather than stubbed |
| [0004](0004-staged-parts-and-project-iterations.md) | Staged parts live at a project location; iterations are builds | Build one, revise, build again, reuse parts from the last iteration |
| [0005](0005-extraction-runs-outside-the-api.md) | Datasheet extraction runs outside the API process | Docling's multi-GB weights never enter the API image; a worker claims and submits over HTTP. Amended once, for capture OCR only — see 0015 |
| [0006](0006-per-layer-child-view.md) | Each layer of the storage tree carries its own view type | Extends 0002 |
| [0007](0007-container-pictures-glyph-and-photo.md) | A container's picture is two things, not one | A glyph and a photo. Extends 0002 and 0006 |
| [0007](0007-the-cart-and-two-ways-to-choose-parts.md) | The cart, and the two ways of choosing parts | Supersedes the UI study's project-as-a-mode. Its *checkout* model is in turn superseded by 0010 |
| [0008](0008-two-ways-to-create-a-container.md) | Creating a container is two routes, and the UI must offer both | Extends 0002; no schema change |
| [0009](0009-cluster-deployment-and-the-443-problem.md) | The cluster deployment: one origin on NodePort 443 | Closes the "ingress class is unknown" question by probing: no ingress, no LoadBalancer, `hostPort` forbidden, so one NodePort on 30443 behind our own nginx. **No tag may be provisioned** until something outside the cluster answers on 443 |
| [0009](0009-drawn-rooms-and-placed-containers.md) | Draw the room; place the containers in it | Closes the gap 0006 named and refused to guess at |
| [0010](0010-the-active-project-and-the-cart-as-a-running-record.md) | Open projects as tabs, and the cart as a running record | Supersedes 0007's (cart) checkout model; amends what the cart is *for* |
| [0011](0011-a-take-is-a-withdrawal-and-belongs-to-an-iteration.md) | A take is a withdrawal, and it belongs to an iteration | Amends 0010: the tab strip is unchanged, what *committing* does is not |
| [0011](0011-authoring-part-types-and-filterable-fields.md) | Authoring part types, and what happens when two of them want the same field name | One column, `parameter_template.is_seed` |
| [0012](0012-the-mcp-server-and-a-forced-coverage-decision.md) | The MCP server, and why every route must be decided about | 26 curated tools, and `coverage.py` forcing a disposition for **every** route so the tool surface cannot silently go stale |
| [0012](0012-what-a-tag-holds-and-which-readers-exist.md) | What a tag holds, as distinct from what we bound; and which readers exist | Tag provisioning had a complete API and no callers; this is the write half |
| [0013](0013-the-nightly-pass-repairs-staleness-and-only-reports-drift.md) | The nightly pass repairs staleness and only reports drift | A scheduled rebuild would erase the write-path bug it is evidence of; the repair is an explicit route |
| [0013](0013-the-rc522-as-a-second-reader.md) | The RC522 as a second reader, and what that costs | Supersedes PLAN.md's rejection of the MFRC522, which rested on library quality that no longer applies now that `agent/iso14443a.py` is ours and unit-tested |
| [0014](0014-the-device-bridge-and-how-a-reader-is-found.md) | The device bridge, and how a reader is found | Extends 0012 (tags/readers) with the half it left as a gap; takes a narrow position on 0003's no-feature-flag rule |
| [0015](0015-the-capture-and-where-text-is-read.md) | The capture, and where text gets read | Amends 0005 without replacing it — the split still holds for every PDF |
| [0016](0016-local-models-and-where-they-run.md) | Local models, which ones, and where they run | `nvidia.com/gpu` is capacity 1 and exclusive on this node (measured), so freeing VRAM does not free the device. One pod holds it and sleeps; yielding is an explicit scale-to-zero. Departs from `CLAUDE.local.md`'s Job-that-releases rule and says why |
| [0017](0017-the-researcher-proposes-and-never-asserts.md) | The researcher proposes and never asserts a URL | Every proposed URL is fetched and validated — magic bytes, it parses, and the normalised MPN is in the text — before it becomes a document. Deterministic sources before any model |
| [0018](0018-chat-threads-writeups-and-export.md) | Two chat surfaces, writeups, and export | Separate histories per kind; the agent loop lives outside the API because its tools call back into the single SQLite writer; chat proposes and never commits |
| [0019](0019-the-hostname-moves-under-aether.md) | The hostname moves to `almagest.aether.lan` | Supersedes 0001's hostname and nothing else. Recomputes the QR budget: 38 bytes leaves 4 bytes of headroom at version 3, so anything added to the payload pushes it to version 4 |

## Where these override PLAN.md

The places PLAN.md describes a design that was later *changed*, as opposed to one
that is merely unbuilt:

| PLAN.md says | Actually |
|---|---|
| The station is triggered by a weight jump, and weighs before it is READY | 0003 — no scale exists; PN532 polling is the trigger |
| One PN532 over UART, and the MFRC522 is rejected | 0013 (rc522) and 0014 — three drivers and a bridge |
| Put the devices on a Tailscale tailnet | 0001 — `https://almagest.aether.lan` with a private CA |
| Extraction is a stage inside the pipeline | 0005 — a separate worker over HTTP, amended by 0015 for capture OCR |
| Ingress class and hostname are open questions | 0009 (cluster) — probed and settled |
