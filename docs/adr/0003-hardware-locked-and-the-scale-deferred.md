# ADR 0003 — Locked hardware, and the scale deferred

**Status:** accepted
**Date:** 2026-07-28

## Context

`docs/PLAN.md` specified the station's hardware as a shopping list, deliberately
leaving choices open. Enough is now bought or ordered to close several of them,
and one specified component — the load cell — is being **deferred rather than
bought**. This ADR records what is settled and, more importantly, what each
choice *costs*, because two of them invalidate reasoning PLAN.md relied on.

## Decision

### What is owned

| Item | Detail |
|---|---|
| Compute | **Raspberry Pi 4 B, 4 GB** |
| Camera | **Pi HQ Camera** (Sony IMX477, 4056 × 3040, 6.287 × 4.712 mm active area) |
| Lenses | **16 mm and 25 mm** C/CS-mount |
| Lighting | **5 V addressable strip, 100 LED/m** (10 mm pitch) — on order |
| Later, unowned | a standard USB webcam, purpose not yet fixed |

### What is deferred

**The load cell and its ADC are not being bought.** PLAN.md made differential
weighing the *primary* counting method; that is now deferred as not obviously
worth the build cost for most day-to-day situations.

### Models

Agent and extraction work targets the **Qwen** family, which is already running
on the GPU host this project will use. Vision-capable Qwen covers the two jobs
PLAN.md wanted a model for — datasheet table extraction and the IC-marking
reader's fuzzy front door. Cluster specifics stay in `CLAUDE.local.md`, which is
gitignored; nothing about the host belongs in a committed file.

## Consequences

### Pi 4, not Pi 5 — the LED locator gets simpler

PLAN.md ruled out driving addressable LEDs from the Pi because "`rpi_ws281x` is
broken on Pi 5 since the SoC dropped the legacy PWM/DMA it needed", and routed
the locator through an ESP32 running WLED instead.

**That constraint does not apply to a Pi 4**, whose PWM and DMA are intact, so
direct GPIO drive is available. Two caveats survive and are the reason WLED
remains a live option rather than a dead one:

- PWM drive needs root and contends with onboard audio. The SPI (MOSI) path
  avoids both and is the one to try first.
- WS2812B wants a logic high near 0.7 × V<sub>DD</sub> ≈ 3.5 V and the Pi's GPIO
  is 3.3 V. It usually works and is out of spec; a 74AHCT125 is the standard fix
  and costs under a pound. "Usually works" is not a thing to build a locator on.

### The strip cannot be powered from the Pi

100 LED/m at ~60 mA/LED full white is **~6 A/m**. A one-metre strip at full
brightness needs its own 5 V supply with a common ground back to the Pi; the
Pi's own 5 V rail is nowhere near it. This is a wiring fact, not a preference —
attempting it browns out the Pi mid-session and corrupts an SD card eventually.

For the counting tray the strip is a *backlight behind a diffuser*, where 10 mm
pitch is dense enough to avoid visible banding. For a drawer locator it is a run
along a shelf edge. Both want current limiting well below full white anyway.

### The lenses are telephoto, and that sets the gantry height

Field of view is approximately `sensor_dimension × distance / focal_length`. With
the HQ camera's 6.287 mm sensor width:

| Lens | 200 mm | 300 mm | 500 mm | 600 mm |
|---|---|---|---|---|
| 16 mm | ~79 mm | ~118 mm | ~196 mm | ~236 mm |
| 25 mm | ~50 mm | ~75 mm | ~126 mm | ~151 mm |

So a ~150 mm tray needs roughly **380 mm of standoff on the 16 mm lens, or
600 mm on the 25 mm**. That is a tall gantry, and it is a real constraint on the
fixture rather than a detail — PLAN.md's ~220 × 220 mm plinth does not imply a
camera mount of that height.

Read the other way, this is *excellent* for the jobs that need magnification. At
300 mm the 16 mm lens gives ~29 µm/px, so an 0603 part (1.6 mm) spans ~55 px —
ample for counting — and the 25 mm at 200 mm gives ~12 µm/px, which is the
regime the IC-marking reader and the resistor colour-band checker want.

**Neither lens is a wide-angle.** The 6 mm lens from the usual Pi kit is not
owned, so the "whole tray in one frame at short standoff" case has no glass. The
options are to mount high, to accept a smaller tray, or to buy a 6–8 mm lens
later. Also unverified: both lenses' **minimum object distance**, which sets the
close end of every number above and must be measured against the actual lenses
before the fixture is cut.

### Deferring the scale costs three things, and they are not small

This is the consequence worth being honest about, because PLAN.md's counting
design leaned on the scale in ways that are easy to miss.

**1. The station loses its trigger.** PLAN.md's state machine begins
`IDLE → CONTAINER_DETECTED (weight jump > ~200 mg) → IDENTIFYING`. With no scale
there is no weight jump, so "set a container down and it identifies itself" —
the station's whole reason for existing — needs a different first edge:
continuous PN532 polling, a cheap IR/proximity sensor, or a button. Continuous
polling is the only one that preserves the no-gesture property and is the
default to try.

**2. Vision counting loses one of its three anti-stacking detectors.** PLAN.md is
emphatic that "stacking must never fail silently", because a 2D silhouette loses
occluded area with no bounded correction, and it names three independent
detectors that each force a hard "spread these out and retry": a shallow tray, a
**scale cross-check**, and raking light. Deferring the scale removes the middle
one. The remaining two must now carry the guarantee alone, which is an accepted
weakening and must be stated in the UI rather than papered over.

**3. There is nothing left to fuse against, so `|z| > 3` stops existing.**
PLAN.md fuses vision and mass by inverse-variance weighting and flags
`|z| > 3` as "do not fuse, flag" — that flag being the detector for a **mixed
bin, a wrong `unit_mass_mg`, or hidden overlap**. A vision-only count has no
second estimate, so a mixed bin is no longer caught by disagreement.

Together these mean vision counting must be **more** conservative than PLAN.md
assumed, and "count the handful removed, not the bin" moves from a preference to
the only defensible mode.

Nothing in the schema needs changing. `locations.tare_mg` already exists and
simply stays NULL; the `weighings` and `unit_mass_samples` tables PLAN.md
specifies are additive and go unbuilt. Every by-weight affordance is already
specified to disappear when no scale is present — "scale absent → no `weight.*`
ever emitted → the PWA hides every by-weight affordance. No special-casing" — so
the deferral needs no feature flag. That property was designed in, and this is
the first time it pays.

**Reversible.** The cell and ADC are ~$25 together, so this is a decision to not
spend build time now, not a decision that anything be designed to exclude a
scale later.

### A USB webcam is additive

The Pi 4 B has one CSI port, taken by the HQ camera, so a second camera arrives
over USB. Nothing in the design assumes exactly one camera; `devices` is already
a table with a `kind`. Its purpose is undecided, so nothing is built for it.

## Supersedes

- PLAN.md's Pi-5 justification for the ESP32/WLED locator route — the constraint
  is real but does not apply to this hardware.
- PLAN.md's Phase 2 line, in part: the station's **scale** half is deferred,
  while `deviceagent`, PN532 auto-identify and the station state machine remain.
- PLAN.md's "differential weighing is primary, absolute recount secondary" — for
  now there is no weighing at all, and vision is the only counting method.
