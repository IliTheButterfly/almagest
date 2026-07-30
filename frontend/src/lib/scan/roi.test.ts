import { describe, expect, it } from "vitest";

import { centreRoi, ROI_FRACTION, roiOverlayInset, VIEWFINDER_ASPECT } from "./roi";

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

  it("matches the fraction the viewfinder falls back to", () => {
    // `.viewfinder .roi { inset: 20% }` in styles.css leaves 60% of each axis,
    // and is the value used until the granted resolution is known.
    expect(ROI_FRACTION).toBe(0.6);
  });
});

describe("placing the overlay over a cover-cropped preview", () => {
  it("insets less horizontally than vertically for a 16:9 camera in a 4:3 box", () => {
    // 1080 × 4/3 = 1440 frame pixels wide are visible; the crop is 1152 wide, so
    // 10% in from each side. Vertically the whole 1080 is visible against a
    // 648-pixel crop, so 20%. A fixed 20% inset — what the CSS used to do alone —
    // marks a region narrower than the one being read, and the box is the only
    // aiming instruction the user has.
    const inset = roiOverlayInset(1920, 1080, 0.6, 4 / 3);
    expect(inset.x).toBeCloseTo(0.1, 5);
    expect(inset.y).toBeCloseTo(0.2, 5);
  });

  it("is symmetric when the camera agrees with the box", () => {
    const inset = roiOverlayInset(1440, 1080, 0.6, 4 / 3);
    expect(inset.x).toBeCloseTo(0.2, 5);
    expect(inset.y).toBeCloseTo(0.2, 5);
  });

  it("crops the other axis for a frame taller than the box", () => {
    // A portrait-locked camera: now the top and bottom are the parts cut away.
    const inset = roiOverlayInset(1080, 1920, 0.6, 4 / 3);
    expect(inset.y).toBeLessThan(inset.x);
  });

  it("never draws the box outside the preview", () => {
    // A crop wider than the visible picture would give a negative inset, which
    // would put the border off the edge of the panel.
    const inset = roiOverlayInset(4000, 1000, 1, 4 / 3);
    expect(inset.x).toBe(0);
    expect(inset.y).toBeGreaterThanOrEqual(0);
  });

  it("falls back to the naive inset before the resolution is known", () => {
    // `useScanner` reports a null resolution until `getSettings()` answers, and
    // the box has to be drawn somewhere in the meantime.
    for (const inset of [roiOverlayInset(0, 0), roiOverlayInset(1920, 0), roiOverlayInset(0, 1080)]) {
      expect(inset).toEqual({ x: 0.2, y: 0.2 });
    }
  });

  it("defaults to the fraction and box the app actually uses", () => {
    expect(roiOverlayInset(1920, 1080)).toEqual(roiOverlayInset(1920, 1080, ROI_FRACTION, 4 / 3));
    expect(VIEWFINDER_ASPECT).toBeCloseTo(4 / 3, 5);
  });
});
