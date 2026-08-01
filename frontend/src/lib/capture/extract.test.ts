/**
 * Field extraction, against two real photographs of DigiKey labels.
 *
 * `fixtures/digikey-label-26.json` and `-21.json` are exactly what the capture
 * pipeline produced from Iliana's phone — the same regions, geometry and OCR
 * damage. Synthetic fixtures were what made the previous round of this feature
 * look finished while it was broken, so the cases below assert against output
 * that includes real misreads (`Pan Number`, `Master Par ID`, `CFI4JT100K`)
 * rather than tidy text.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { extractSuggestions, matchHeading, type Suggestions } from "./extract";
import type { Region } from "./types";

const HERE = dirname(fileURLToPath(import.meta.url));

interface Fixture {
  width_px: number;
  height_px: number;
  regions: {
    kind: string;
    text: string;
    corners: { x: number; y: number }[];
    confidence: number | null;
    symbology: string | null;
  }[];
}

function load(name: string): Region[] {
  const raw = JSON.parse(readFileSync(join(HERE, "fixtures", name), "utf8")) as Fixture;
  return raw.regions.map((region) => {
    const quad = region.corners as unknown as Region["quad"];
    return region.kind === "barcode"
      ? { kind: "barcode", text: region.text, quad, symbology: region.symbology ?? "?" }
      : {
          kind: "text",
          text: region.text,
          quad,
          ...(region.confidence === null ? {} : { confidence: region.confidence }),
        };
  }) as Region[];
}

const values = (s: Suggestions, field: keyof Suggestions): string[] =>
  (s[field] ?? []).map((suggestion) => suggestion.value);

describe("matchHeading", () => {
  it("recognises headings the OCR pass damaged", () => {
    // All three are verbatim from the fixtures.
    expect(matchHeading("Pan Number")?.heading).toBe("part number");
    expect(matchHeading("Pant Description")?.heading).toBe("part description");
    expect(matchHeading("Master Par ID")?.heading).toBe("master part id");
  });

  it("does not turn a value into a heading", () => {
    expect(matchHeading("STACKPOLE ELECTRONICS INC")).toBeNull();
    expect(matchHeading("CF14JT100K")).toBeNull();
    expect(matchHeading("2247")).toBeNull();
  });
});

describe("a resistor label, text only", () => {
  const suggestions = extractSuggestions({ regions: load("digikey-label-26.json"), resolved: {} });

  it("pairs a heading with the value printed under it", () => {
    expect(values(suggestions, "manufacturer")).toContain("STACKPOLE ELECTRONICS INC");
    expect(values(suggestions, "quantity")).toContain("100");
  });

  it("offers nothing for a field whose value the OCR pass missed", () => {
    // `Date Code` is followed on this capture by the `Manufacturer` heading,
    // because the value between them was never read. Suggesting "Manufacturer"
    // as the date code is the failure this guards against.
    expect(values(suggestions, "date_code")).not.toContain("Manufacturer");
    expect(suggestions.date_code ?? []).toHaveLength(0);
  });

  it("keeps a value from a different column out of a heading's list", () => {
    // `Purchase Order` sits at x=443 and `Customer Reference` at x=90 on the
    // same rows; left-alignment is what separates them.
    expect(values(suggestions, "manufacturer")).not.toContain("81734485");
  });

  it("offers the row under Part Number as an MPN candidate, not as the answer", () => {
    // This capture's `Part Number` row is followed by `CFI4JT100K` — the OCR
    // pass dropped `CF14JT100KCT-ND` in between, so the pairing is confident and
    // wrong. It is offered because a person holding the label can see which it
    // is; it must never be applied on its own.
    expect(values(suggestions, "mpn")).toContain("CFI4JT100K");
  });
});

describe("a capacitor label with a readable Data Matrix", () => {
  const regions = load("digikey-label-21.json");
  // What `/api/scan/resolve` returns for that payload, as the ECIA handler
  // parses it: `1P` is the Vishay part, `P` is DigiKey's ordering code.
  const resolved = {
    [regions.findIndex((r) => r.kind === "barcode" && r.text.includes("1PK101K15C0GF53L2"))]: {
      parsed: {
        mpn: "BC5130-ND",
        supplier_part_number: "K101K15C0GF53L2",
        date_code: "2114",
        lot_code: "17W2112401",
        quantity_milli: 10_000,
        di_fields: {},
        warnings: [],
      },
    },
  } as never;

  const suggestions = extractSuggestions({ regions, resolved });

  it("puts the decoded part number ahead of the OCR'd one", () => {
    const mpn = suggestions.mpn ?? [];
    expect(mpn[0]?.source).toBe("barcode");
    expect(mpn[0]?.value).toBe("K101K15C0GF53L2");
    // The OCR pass read that same part as `K101K1SCOGF53L2` — a 5 as S and a 0
    // as O. It stays in the list, behind the decoded value, because the ranking
    // is the whole point rather than discarding the weaker read.
    expect(mpn.some((s) => s.source === "text")).toBe(true);
  });

  it("marks where each suggestion came from", () => {
    const mpn = suggestions.mpn ?? [];
    expect(mpn[0]?.via).toContain("1P");
    expect(mpn.find((s) => s.source === "text")?.via).toBe("part number");
  });

  it("reads the quantity off the code in whole units", () => {
    expect(values(suggestions, "quantity")[0]).toContain("10");
  });

  it("suggests the description as a name", () => {
    expect(values(suggestions, "name")).toContain("CAP CER 100PF SOV COG/NPO RADIAL");
  });
});
