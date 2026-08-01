# Naming

The project is named after the sky, and each repo after a constellation. The
conceit is not decorative: an inventory system exists to make thousands of tiny,
individually indistinguishable objects findable, which is the same problem the
constellations solved.

Most of the names come from Lacaille's 1756 southern catalogue, whose
constellations are all **scientific instruments** — a workbench project gets to
name each repo after the instrument it actually is.

## The repos

| Name | Repo | What it is |
|---|---|---|
| **Almagest** | `almagest` (master) | The web platform: `backend/`, `frontend/`, `deviceagent/`, `idcodec/`, the SQLite database, deployment manifests, docs |
| **Mensa** | `mensa` (submodule) | ESP-IDF firmware for the bench station — NAU7802 scale, WS2812B, USB-serial line protocol |
| **Circinus** | `circinus` (submodule) | OpenSCAD/CAD — Gridfinity trays, label cards, station plinth/gantry/platform |
| **Antlia** | `antlia` (submodule) | Flipper Zero app — reads a container tag, types its short ID as a USB keyboard |
| — | `ecia-barcode` (submodule) | MH10.8.2 / EIGP-114 barcode parser. **Descriptive on purpose** |
| — | `elec-value-parser` (submodule) | `4k7` / `0R22` electronics shorthand grammar. **Descriptive on purpose** |

**Almagest** is Ptolemy's star catalogue, the source of the classical
constellations — a catalogue of thousands of faint points, made findable. That
is the entire product.

**Mensa** is the constellation "the Table". The station is a table.

**Circinus** is the drafting compasses. It holds the CAD.

**Antlia** is the air pump — the instrument whose whole job is moving the contents
of one vessel into another, which is precisely and only what that app does: it
takes the ID off a tag and puts it into a computer. It is a submodule for the same
reason Mensa is: a separate toolchain (`ufbt` and the Flipper SDK) with no
coupling to the API contract.

## The machines

A physical machine gets an instrument name too, on the same terms as a repo: it
is a thing you address by name, over and over, from outside itself. There is
exactly one so far.

| Name | Machine | What it is |
|---|---|---|
| **Norma** | `norma` | The bench station's Raspberry Pi 4 — runs `deviceagent/`, owns the PN532 on the UART and later the CSI camera |

**Norma** is the set square: the reference standard you lay against a piece of
work to check that it is true. That is the Pi's whole job — a container lands on
the platform and the Pi checks the thing in front of it against the record. The
Latin sense of *norma*, the standard itself, is the same word doing the same
work. It pairs with Mensa physically as well as thematically: Mensa is the table,
and the square lies on the table.

Note the split it makes visible. **Mensa is the firmware, Norma is the host** —
two names for one bench, because they are two artifacts with two toolchains that
fail independently. "The station" remains the right word for the assembly of
both.

## The two libraries stay descriptive

They are the only artifacts aimed at strangers. `pip install ecia-barcode` is
discoverable in a way that no constellation name will ever be, and a repo name
that disagrees with its PyPI distribution name is pure friction for the one
audience that is not us. Do not rename them.

## What is *not* named

Only repos and machines get constellation names. Everything *inside* one keeps
its plain descriptive name: `backend/`, `frontend/`, `deviceagent/`, `idcodec/`,
`services/search/`, `counting/`, and so on. `idcodec/` is a *distribution* —
`almagest-idcodec` — and still not a repo, so it stays descriptive too: it is the
identity codec, and that is what it is called. Subsystems — the scale, the vision counter, the colour-
band checker, the enrichment pipeline — are called what they are.

This boundary is the whole point. A codename is worth learning once per repo,
because a repo is a thing you clone, pin and publish — and once per machine,
because a machine is a thing you ssh into and point a config at. A codename for a
module is a second vocabulary to hold in your head for no navigational gain, and
in a solo-maintained project that is a cost with no payer.

## Consequences of the name

- **Environment variables are prefixed `ALMAGEST_`.** See `.env.example`.
- **The database file is `data/almagest.db`.**
- **`ALMAGEST_BASE_URL` is physically permanent.** Its host is written into every
  NFC tag's NDEF URI record and every printed QR, as `https://<host>/s/{short_id}`.
  Settle the hostname before provisioning tags, or every tag needs rewriting.
  Prefer a short host: the payload length drives QR module density, and a denser
  QR is a worse scan off a curved or scuffed label.
- **Deployed resources are prefixed `almagest-`** so it is unambiguous which
  workloads belong to this project.

## Collisions checked

- **Argo** was rejected for the master repo despite the Argo Navis metaphor
  (Carina/Vela/Puppis for backend/frontend/deviceagent) — it collides badly with
  Argo CD and Argo Workflows on the deployment target.
- **Pyxis** — apt (Greek for "box") but a BD trademark for medication dispensing
  cabinets, which is close enough to inventory to avoid.
- **Reticulum** — also a networking stack.
- **Mensa** — also the IQ society. Unrelated domain, no practical conflict.
- **Antlia** — an asteroid family and a defunct telescope project; nothing in
  software, and nothing in inventory or embedded tooling.
- **Norma** — a dead Visual Studio ORM plug-in, and a common given name. Both are
  further away than the Mensa/IQ-society overlap already accepted above.
- **Pictor** was rejected for the station Pi despite being the better fit for a
  machine that will hold a camera — it collides twice, and both times in an
  adjacent domain: Pictorus is a model-based-design platform that deploys
  generated code to connected embedded devices, and PICTOR is an open-source
  radio telescope. An astronomy-named project colliding with an astronomy
  instrument is the worst available case.
- **Microscopium** — no collision, and the strongest metaphor of any candidate
  (an instrument for examining objects too small to tell apart by eye is the
  product thesis). Rejected as a hostname only: twelve characters with no clean
  abbreviation, typed at every ssh.
