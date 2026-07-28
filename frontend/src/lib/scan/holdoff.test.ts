import { describe, expect, it } from "vitest";

import { DECODER_HOLD_OFF_MS, PayloadHoldOff, SCAN_DEBOUNCE_MS } from "./holdoff";

function clock(): { now: () => number; advance: (ms: number) => void } {
  let at = 1_000;
  return {
    now: () => at,
    advance: (ms) => {
      at += ms;
    },
  };
}

describe("the payload hold-off", () => {
  it("admits a payload it has not seen", () => {
    const time = clock();
    const holdOff = new PayloadHoldOff(SCAN_DEBOUNCE_MS, { now: time.now });
    expect(holdOff.admit("REEL-A")).toBe(true);
  });

  it("drops a duplicate inside the window", () => {
    const time = clock();
    const holdOff = new PayloadHoldOff(SCAN_DEBOUNCE_MS, { now: time.now });
    expect(holdOff.admit("REEL-A")).toBe(true);
    time.advance(1_999);
    expect(holdOff.admit("REEL-A")).toBe(false);
  });

  it("admits the same payload once the window has passed", () => {
    const time = clock();
    const holdOff = new PayloadHoldOff(SCAN_DEBOUNCE_MS, { now: time.now });
    expect(holdOff.admit("REEL-A")).toBe(true);
    time.advance(SCAN_DEBOUNCE_MS);
    expect(holdOff.admit("REEL-A")).toBe(true);
  });

  it("holds each payload off independently", () => {
    // A single most-recent slot would let two labels alternating in view fire on
    // every frame, which is exactly what the hold-off is for.
    const time = clock();
    const holdOff = new PayloadHoldOff(DECODER_HOLD_OFF_MS, { now: time.now });
    expect(holdOff.admit("A")).toBe(true);
    expect(holdOff.admit("B")).toBe(true);
    time.advance(100);
    expect(holdOff.admit("A")).toBe(false);
    expect(holdOff.admit("B")).toBe(false);
  });

  it("drops everything while blocked, which is the in-flight commit case", () => {
    const time = clock();
    const holdOff = new PayloadHoldOff(SCAN_DEBOUNCE_MS, { now: time.now });
    holdOff.block();
    expect(holdOff.blocked).toBe(true);
    expect(holdOff.admit("NEVER-SEEN")).toBe(false);
    holdOff.unblock();
    expect(holdOff.admit("NEVER-SEEN")).toBe(true);
  });

  describe("with refreshWhileSuppressed, the decoder's configuration", () => {
    it("fires once for a label held in front of the lens, not once per window", () => {
      const time = clock();
      const holdOff = new PayloadHoldOff(DECODER_HOLD_OFF_MS, {
        now: time.now,
        refreshWhileSuppressed: true,
      });

      expect(holdOff.admit("HELD")).toBe(true);
      // 15 seconds of frames at ~10 fps. With a fixed window this fires five times.
      let fired = 0;
      for (let frame = 0; frame < 150; frame += 1) {
        time.advance(100);
        if (holdOff.admit("HELD")) {
          fired += 1;
        }
      }
      expect(fired).toBe(0);
    });

    it("admits the label again once it has been out of frame for the window", () => {
      const time = clock();
      const holdOff = new PayloadHoldOff(DECODER_HOLD_OFF_MS, {
        now: time.now,
        refreshWhileSuppressed: true,
      });
      expect(holdOff.admit("HELD")).toBe(true);
      time.advance(DECODER_HOLD_OFF_MS);
      expect(holdOff.admit("HELD")).toBe(true);
    });

    it("remembers sightings made while blocked", () => {
      // Otherwise releasing the block re-fires the label the user is still holding
      // up while reading the result of the last commit.
      const time = clock();
      const holdOff = new PayloadHoldOff(DECODER_HOLD_OFF_MS, {
        now: time.now,
        refreshWhileSuppressed: true,
      });
      expect(holdOff.admit("HELD")).toBe(true);
      holdOff.block();
      time.advance(5_000);
      expect(holdOff.admit("HELD")).toBe(false);
      holdOff.unblock();
      expect(holdOff.admit("HELD")).toBe(false);
    });
  });

  it("forgets everything on request, e.g. a camera restart", () => {
    const time = clock();
    const holdOff = new PayloadHoldOff(SCAN_DEBOUNCE_MS, { now: time.now });
    expect(holdOff.admit("A")).toBe(true);
    holdOff.forget();
    expect(holdOff.admit("A")).toBe(true);
  });
});
