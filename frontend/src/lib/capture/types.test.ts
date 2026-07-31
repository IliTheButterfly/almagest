import { describe, expect, it } from "vitest";

import { boxToQuad, bounds, overlaps } from "./types";

describe("quad geometry", () => {
  it("squares a box off into the corner order everything else speaks", () => {
    expect(boxToQuad(10, 20, 40, 50)).toEqual([
      { x: 10, y: 20 },
      { x: 40, y: 20 },
      { x: 40, y: 50 },
      { x: 10, y: 50 },
    ]);
  });

  it("bounds a rotated quad, which is what the overlay actually draws", () => {
    // A label read at an angle: `zxing-wasm` reports the true corners, and the
    // outline has to cover all of them rather than the first two.
    const rotated = [
      { x: 30, y: 10 },
      { x: 60, y: 30 },
      { x: 40, y: 60 },
      { x: 10, y: 40 },
    ] as const;
    expect(bounds(rotated)).toEqual({ x: 10, y: 10, width: 50, height: 50 });
  });
});

describe("overlaps", () => {
  it("is false for regions in different places", () => {
    // The case that must never be treated as a duplicate: the same MPN inside a
    // DataMatrix and printed in ink beside it. Two findings, and the ink is the
    // one a human can check.
    expect(overlaps(boxToQuad(0, 0, 50, 50), boxToQuad(200, 0, 250, 50))).toBe(false);
  });

  it("is true when an OCR line sits on top of a decoded symbol", () => {
    // Tesseract reliably reads the digits printed under a Code 128 strip, and
    // the bars themselves as characters.
    expect(overlaps(boxToQuad(0, 0, 100, 40), boxToQuad(5, 5, 95, 35))).toBe(true);
  });

  it("measures against the smaller region, not the union", () => {
    // A short line fully inside a large barcode is entirely shadowed by it even
    // though it covers a tiny fraction of it — comparing against the union would
    // score this near zero and let the duplicate through.
    expect(overlaps(boxToQuad(0, 0, 400, 400), boxToQuad(10, 10, 40, 40))).toBe(true);
  });

  it("is false for a mere clipped corner", () => {
    expect(overlaps(boxToQuad(0, 0, 100, 100), boxToQuad(90, 90, 190, 190))).toBe(false);
  });

  it("is false for zero-area regions rather than dividing by zero", () => {
    expect(overlaps(boxToQuad(10, 10, 10, 10), boxToQuad(10, 10, 10, 10))).toBe(false);
  });
});
