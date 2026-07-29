import { describe, expect, it } from "vitest";

import { ALL_GLYPHS, GLYPH_LABELS, glyphLabel, glyphSymbol } from "./glyphs";

describe("glyphSymbol", () => {
  it("has one symbol per known glyph, with no duplicates", () => {
    const symbols = ALL_GLYPHS.map((glyph) => glyphSymbol(glyph));
    expect(symbols.every((symbol) => symbol !== null)).toBe(true);
    expect(new Set(symbols).size).toBe(symbols.length);
  });

  it("is null for 'no glyph chosen'", () => {
    expect(glyphSymbol(null)).toBeNull();
  });

  it("is null for a name this bundle has never heard of, rather than throwing", () => {
    // The no-CHECK promise: a row can hold a glyph a newer build invented.
    expect(() => glyphSymbol("isometric-hologram")).not.toThrow();
    expect(glyphSymbol("isometric-hologram")).toBeNull();
  });
});

describe("glyphLabel", () => {
  it("names every glyph a picker can offer", () => {
    for (const glyph of ALL_GLYPHS) {
      expect(glyphLabel(glyph)).toBe(GLYPH_LABELS[glyph]);
    }
  });

  it("is null for null and for an unrecognised name", () => {
    expect(glyphLabel(null)).toBeNull();
    expect(glyphLabel("hologram")).toBeNull();
  });
});
