/**
 * The chip rules, which are where the "never auto-accept" invariant is actually
 * enforced. Everything else about a capture is presentation; this is policy.
 */

import { describe, expect, it } from "vitest";

import type { ScanResolveResponse } from "../api/client";
import { chipsForRegion } from "./chips";
import { boxToQuad, type BarcodeRegion, type TextRegion } from "./types";

const QUAD = boxToQuad(0, 0, 10, 10);

function barcode(text: string, symbology = "DataMatrix"): BarcodeRegion {
  return { kind: "barcode", text, quad: QUAD, symbology };
}

function line(text: string, confidence = 82): TextRegion {
  return { kind: "text", text, quad: QUAD, confidence };
}

function resolved(overrides: Partial<ScanResolveResponse> = {}): ScanResolveResponse {
  return {
    status: "unknown",
    decoded_kind: "ecia",
    normalized: "x",
    suggest_bind: false,
    latency_ms: 3,
    scan_event_id: 1,
    candidates: [],
    existing_lots: [],
    ...overrides,
  } as ScanResolveResponse;
}

describe("a text region", () => {
  it("offers exactly one chip, carrying its confidence", () => {
    const [chip, ...rest] = chipsForRegion(line("Murata Electronics", 74), 0);
    expect(rest).toHaveLength(0);
    expect(chip?.value).toBe("Murata Electronics");
    expect(chip?.kind).toBe("read");
    expect(chip?.confidence).toBe(74);
  });

  it("never carries a target field, however much it looks like a part number", () => {
    // The rule `CLAUDE.md` and `docs/PLAN.md` both state: an OCR'd part number is
    // never auto-accepted. A `field` here would let a form fill itself from a
    // guess, so the absence is the enforcement — the user has to point at the
    // field first, which is what supplies the human decision.
    for (const text of ["RC0805FR-0710KL", "STM32F103C8T6", "10uF 25V"]) {
      expect(chipsForRegion(line(text), 0)[0]?.field).toBeUndefined();
    }
  });
});

describe("a barcode region", () => {
  it("expands into the fields the resolver parsed, each with a target field", () => {
    const chips = chipsForRegion(
      barcode("[)>\x1e06\x1dP"),
      0,
      resolved({
        parsed: {
          mpn: "RC0805FR-0710KL",
          manufacturer: "Yageo",
          supplier_part_number: "311-10.0KCRCT-ND",
          quantity_milli: 5000_000,
          date_code: "2438",
          lot_code: "L77",
          di_fields: {},
          warnings: [],
        },
      } as Partial<ScanResolveResponse>),
    );
    const byLabel = Object.fromEntries(chips.map((chip) => [chip.label, chip]));

    expect(byLabel["MPN"]?.value).toBe("RC0805FR-0710KL");
    expect(byLabel["MPN"]?.field).toBe("mpn");
    expect(byLabel["Manufacturer"]?.field).toBe("manufacturer");
    expect(byLabel["Supplier PN"]?.field).toBe("supplier_part_number");
    expect(byLabel["Date code"]?.value).toBe("2438");
    // Whole units, not the milli integer the wire carries.
    expect(byLabel["Quantity"]?.value).toContain("5");
    expect(chips.every((chip) => chip.kind === "verified")).toBe(true);
  });

  it("still offers the raw payload before the resolver has answered", () => {
    // The outline is drawn before the round trip lands, so a region with no
    // resolution yet must not be a dead end.
    const chips = chipsForRegion(barcode("RC0805FR-0710KL"), 0);
    expect(chips).toHaveLength(1);
    expect(chips[0]?.value).toBe("RC0805FR-0710KL");
    // Labelled by symbology when it is the only thing on offer, so the chip says
    // what it is rather than "Whole payload" with nothing to contrast against.
    expect(chips[0]?.label).toBe("DataMatrix");
  });

  it("withholds the raw chip for a payload full of control characters", () => {
    // An ECIA envelope *is* its GS/RS/EOT separators and is stored verbatim, but
    // copying it pastes invisible junk into a text box.
    const chips = chipsForRegion(
      barcode("[)>\x1e06\x1dPRC0805\x1dQ5000\x1e\x04"),
      0,
      resolved({ parsed: { mpn: "RC0805", di_fields: {}, warnings: [] } } as Partial<ScanResolveResponse>),
    );
    expect(chips.some((chip) => chip.label === "Whole payload")).toBe(false);
    expect(chips.some((chip) => chip.label === "MPN")).toBe(true);
  });

  it("leads with somewhere to go when the payload is one of our own short IDs", () => {
    const chips = chipsForRegion(
      barcode("https://almagest.lan/s/4K7T92M8", "QRCode"),
      0,
      resolved({
        status: "resolved",
        decoded_kind: "short_id",
        target: {
          entity_type: "location",
          entity_pk: 12,
          label: "Drawer A3",
          label_path: "Bench / Cabinet 1 / Drawer A3",
          retired: false,
        },
      } as Partial<ScanResolveResponse>),
    );
    expect(chips[0]?.kind).toBe("resolved");
    expect(chips[0]?.href).toBe("/locations/12");
    expect(chips[0]?.value).toBe("Bench / Cabinet 1 / Drawer A3");
  });

  it("says so when the container it points at was removed", () => {
    const chips = chipsForRegion(
      barcode("https://almagest.lan/s/4K7T92M8", "QRCode"),
      0,
      resolved({
        status: "resolved",
        target: {
          entity_type: "location",
          entity_pk: 9,
          label: "Old drawer",
          retired: true,
        },
      } as Partial<ScanResolveResponse>),
    );
    // The tag is still on the drawer, so landing the user on something that
    // looks live would be the lie worth avoiding.
    expect(chips[0]?.label).toBe("Removed container");
  });
});
