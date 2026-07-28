import { describe, expect, it } from "vitest";

import { formatShortId, looksLikeShortId, normalizeShortId, shortIdFromPayload } from "./shortid";

describe("normalising a typed short id", () => {
  it("strips the cosmetic hyphen and upper-cases", () => {
    expect(normalizeShortId("4k7t-92m8")).toBe("4K7T92M8");
  });

  it("folds Crockford's confusable glyphs", () => {
    expect(normalizeShortId("O1LI2345")).toBe("01112345");
  });

  it("leaves U alone, because it is excluded rather than merged", () => {
    expect(normalizeShortId("U1234567")).toBe("U1234567");
    expect(looksLikeShortId("U1234567")).toBe(false);
  });

  it("drops a display prefix when the last token is a full code", () => {
    expect(normalizeShortId("BIN 4K7T-92M8")).toBe("4K7T92M8");
  });

  it("keeps the whole string when the space was the group separator", () => {
    expect(normalizeShortId("4K7T 92M8")).toBe("4K7T92M8");
  });

  it("tolerates surrounding whitespace and other separators", () => {
    expect(normalizeShortId("  4k7t_92m8  ")).toBe("4K7T92M8");
  });
});

describe("recognising the shape of a short id", () => {
  it("accepts eight symbols from the alphabet", () => {
    expect(looksLikeShortId("4K7T-92M8")).toBe(true);
  });

  it("rejects the wrong length", () => {
    expect(looksLikeShortId("4K7T92M")).toBe(false);
    expect(looksLikeShortId("4K7T92M89")).toBe(false);
    expect(looksLikeShortId("")).toBe(false);
  });

  it("rejects a part number, so manual entry can tell the two apart", () => {
    expect(looksLikeShortId("GRM188R61A106")).toBe(false);
  });

  it("does not verify the check symbol", () => {
    // Deliberate: the mod-37 check is implemented once, server-side. A second copy
    // would be a second thing to keep in step, and the docs' illustrative codes are
    // not check-valid — a client that enforced it would refuse the very examples the
    // design uses.
    expect(looksLikeShortId("4K7T92M8")).toBe(true);
    expect(looksLikeShortId("4K7T92M8")).toBe(true);
  });
});

describe("rendering", () => {
  it("groups four and four", () => {
    expect(formatShortId("4K7T92M8")).toBe("4K7T-92M8");
  });

  it("passes anything that is not a full code through untouched", () => {
    expect(formatShortId("odd")).toBe("odd");
    expect(formatShortId(null)).toBe("");
    expect(formatShortId(undefined)).toBe("");
  });
});

describe("pulling the code out of a tag payload", () => {
  it("reads the /s/ URL that both the NDEF record and the QR carry", () => {
    expect(shortIdFromPayload("https://almagest.lan/s/4K7T92M8")).toBe("4K7T92M8");
  });

  it("ignores a query string or fragment", () => {
    expect(shortIdFromPayload("https://almagest.lan/s/4K7T92M8?from=tag")).toBe("4K7T92M8");
  });

  it("passes a vendor payload straight through to the resolver chain", () => {
    const ecia = "[)>06PGRM188Q5000";
    expect(shortIdFromPayload(ecia)).toBe(ecia);
  });
});
