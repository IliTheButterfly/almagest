import { describe, expect, it } from "vitest";

import { formatDelta, formatQty, fromMilli, parseQtyToMilli, toMilli } from "./format";

describe("integer-scaled quantities", () => {
  it("round-trips whole units", () => {
    expect(toMilli(1200)).toBe(1_200_000);
    expect(fromMilli(1_200_000)).toBe(1200);
  });

  it("rounds on the way in, because thousandths are the storage unit", () => {
    expect(toMilli(2.0005)).toBe(2001);
    expect(toMilli(2.0004)).toBe(2000);
  });

  it("hides a fractional part that is not there", () => {
    expect(formatQty(5_000)).toBe("5");
  });

  it("shows one when there is", () => {
    expect(formatQty(2_500)).toBe("2.5");
  });

  it("signs a delta, since the direction is the point in a ledger", () => {
    expect(formatDelta(5_000)).toBe("+5");
    expect(formatDelta(-5_000)).toBe("-5");
    expect(formatDelta(0)).toBe("0");
  });
});

describe("parsing keypad text", () => {
  it("accepts digits and a decimal point", () => {
    expect(parseQtyToMilli("250")).toBe(250_000);
    expect(parseQtyToMilli("2.5")).toBe(2_500);
    expect(parseQtyToMilli(" 7 ")).toBe(7_000);
  });

  it("rejects anything else instead of snapping to zero mid-typing", () => {
    expect(parseQtyToMilli("")).toBeNull();
    expect(parseQtyToMilli("-1")).toBeNull();
    expect(parseQtyToMilli("1e6")).toBeNull();
    expect(parseQtyToMilli("abc")).toBeNull();
  });
});
