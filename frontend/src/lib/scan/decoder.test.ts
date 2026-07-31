/**
 * The pass table, not the decode.
 *
 * Actually decoding needs the `zxing-wasm` module and a real image, which is a
 * `live`-shaped test this suite deliberately does not run. What *is* worth
 * pinning here is the shape of the ladder, because `useScanner`'s loop indexes
 * `DECODE_PASSES` and `ESCALATION_LEVELS` with the same number: if those two
 * ever stop lining up, the symptom is a scanner that runs the cheap pass at the
 * expensive pass's cadence — slow for no benefit, and invisible.
 */

import { describe, expect, it } from "vitest";

import {
  DECODE_PASSES,
  ESCALATED_INTERVAL_MS,
  ESCALATION_LEVELS,
  FRAME_INTERVAL_MS,
  MAX_SYMBOLS_PER_FRAME,
} from "./decoder";

describe("the decode ladder", () => {
  it("has one cadence entry per pass, in the same order", () => {
    expect(ESCALATION_LEVELS.map((level) => level.name)).toEqual(
      DECODE_PASSES.map((pass) => pass.name),
    );
  });

  it("starts on the centre crop and widens to the full frame", () => {
    // The first rung is what the previous single-pass decoder did, so a label
    // the user did centre still decodes exactly as fast as it used to.
    expect(DECODE_PASSES[0]?.roiFraction).toBeLessThan(1);
    for (const pass of DECODE_PASSES.slice(1)) {
      expect(pass.roiFraction).toBe(1);
    }
  });

  it("never narrows the crop as it escalates", () => {
    const fractions = DECODE_PASSES.map((pass) => pass.roiFraction);
    expect([...fractions].sort((a, b) => a - b)).toEqual(fractions);
  });

  it("spends `tryHarder` only on the top rung", () => {
    const harder = DECODE_PASSES.filter((pass) => pass.tryHarder);
    expect(harder).toHaveLength(1);
    expect(harder[0]).toBe(DECODE_PASSES.at(-1));
  });

  it("reaches every readable symbology, but only at the top", () => {
    // Enabling every format on every frame is what made the naive version slow;
    // enabling none of them is what made Aztec and PDF417 unreadable.
    expect(DECODE_PASSES.at(-1)?.formats).toContain("AllReadable");
    for (const pass of DECODE_PASSES.slice(0, -1)) {
      expect(pass.formats).not.toContain("AllReadable");
      expect(pass.formats).toContain("QRCode");
    }
  });

  it("gives the expensive pass a slower floor than the cheap ones", () => {
    const [cheap] = ESCALATION_LEVELS;
    const expensive = ESCALATION_LEVELS.at(-1);
    expect(cheap?.minIntervalMs).toBe(FRAME_INTERVAL_MS);
    expect(expensive?.minIntervalMs).toBe(ESCALATED_INTERVAL_MS);
    expect(expensive?.minIntervalMs).toBeGreaterThan(FRAME_INTERVAL_MS);
  });

  it("reads more than one symbol per frame", () => {
    // `maxNumberOfSymbols: 1` discarded the second barcode on a reel label.
    expect(MAX_SYMBOLS_PER_FRAME).toBeGreaterThan(1);
  });
});
