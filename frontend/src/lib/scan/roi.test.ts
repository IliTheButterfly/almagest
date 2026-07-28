import { describe, expect, it } from "vitest";

import { centreRoi, ROI_FRACTION } from "./roi";

describe("the centre-ROI crop", () => {
  it("centres a crop of the configured fraction", () => {
    expect(centreRoi(1000, 500, 0.6)).toEqual({ x: 200, y: 100, width: 600, height: 300 });
  });

  it("keeps the frame's aspect ratio", () => {
    // A square crop is what makes a wide Code 128 strip on a reel label fail at a
    // comfortable working distance, so the crop stays as wide as the frame is.
    const rect = centreRoi(1280, 720);
    expect(rect.width / rect.height).toBeCloseTo(1280 / 720, 2);
  });

  it("leaves the crop fully inside the frame", () => {
    const rect = centreRoi(641, 481);
    expect(rect.x).toBeGreaterThanOrEqual(0);
    expect(rect.y).toBeGreaterThanOrEqual(0);
    expect(rect.x + rect.width).toBeLessThanOrEqual(641);
    expect(rect.y + rect.height).toBeLessThanOrEqual(481);
  });

  it("returns whole pixels", () => {
    const rect = centreRoi(1023, 767);
    for (const value of [rect.x, rect.y, rect.width, rect.height]) {
      expect(Number.isInteger(value)).toBe(true);
    }
  });

  it("returns an empty rect for a video that has not sized itself yet", () => {
    // A `<video>` reports 0x0 for the first frames after play(); the decode loop
    // skipping those is normal, not an error.
    expect(centreRoi(0, 0)).toEqual({ x: 0, y: 0, width: 0, height: 0 });
  });

  it("passes the whole frame through at a fraction of 1", () => {
    expect(centreRoi(100, 100, 1)).toEqual({ x: 0, y: 0, width: 100, height: 100 });
  });

  it("refuses a fraction outside (0, 1]", () => {
    expect(() => centreRoi(100, 100, 0)).toThrow(RangeError);
    expect(() => centreRoi(100, 100, 1.5)).toThrow(RangeError);
  });

  it("matches the fraction the viewfinder draws", () => {
    // `.viewfinder .roi { inset: 20% }` in styles.css leaves 60% of each axis.
    expect(ROI_FRACTION).toBe(0.6);
  });
});
