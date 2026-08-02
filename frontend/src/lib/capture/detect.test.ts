import { describe, expect, it } from "vitest";

import { withoutBarcodeShadows } from "./detect";
import { boxToQuad, type BarcodeRegion, type TextRegion } from "./types";

const CODE: BarcodeRegion = {
  kind: "barcode",
  text: "RC0805FR-0710KL",
  quad: boxToQuad(0, 0, 200, 80),
  symbology: "Code128",
};

function line(text: string, quad: TextRegion["quad"]): TextRegion {
  return { kind: "text", text, quad, confidence: 70 };
}

describe("withoutBarcodeShadows", () => {
  it("drops the digits Tesseract reads underneath a barcode", () => {
    // The reliable duplicate: a Code 128 strip prints its own value beneath the
    // bars, and the bars themselves are frequently "read" as characters.
    const kept = withoutBarcodeShadows([CODE], [line("RC0805FR-0710KL", boxToQuad(5, 55, 195, 78))]);
    expect(kept).toHaveLength(0);
  });

  it("keeps the same value printed somewhere else on the label", () => {
    // Not a duplicate — a second, independently checkable sighting, and the one
    // a human can actually verify against the part in their hand. Deduplicating
    // by text rather than geometry would have thrown this away.
    const elsewhere = line("RC0805FR-0710KL", boxToQuad(0, 300, 200, 340));
    expect(withoutBarcodeShadows([CODE], [elsewhere])).toEqual([elsewhere]);
  });

  it("keeps everything when there are no barcodes at all", () => {
    const lines = [line("Murata", boxToQuad(0, 0, 50, 20))];
    expect(withoutBarcodeShadows([], lines)).toEqual(lines);
  });
});
