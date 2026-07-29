/**
 * The arithmetic behind a drawn room.
 *
 * `roomPlan.ts` is pure on purpose — three coordinate systems, no geometry
 * library, no React — so this is where the room's behaviour is actually pinned.
 * The two things worth stating about what is asserted here:
 *
 * 1. **"Nowhere" is a state, not a coordinate.** `placementDiff` has to name a
 *    child in `unplaced` when it leaves the plan, and must *not* invent (0, 0) for
 *    it. ADR 0009 refuses a default coordinate precisely because it would put every
 *    pre-existing container in the same corner of every room and look authored.
 * 2. **Nothing here validates.** Overlaps, self-intersecting outlines and boxes
 *    outside the walls are all drawn as given — capacity is advisory everywhere in
 *    this system and a drawing is a weaker claim than capacity, not a stronger one.
 */

import { describe, expect, it } from "vitest";

import {
  clampCoord,
  DEFAULT_GRID_MM,
  EMPTY_ROOM_MM,
  footprintOf,
  formatMm,
  frameCentre,
  frameOf,
  MAX_COORD_MM,
  NOMINAL_FOOTPRINT_MM,
  normalizeRotation,
  placementDiff,
  placementDraftsFrom,
  placementPct,
  placementsToRequest,
  pointFromSurface,
  ruleLines,
  ruledStepMm,
  scaleBarMm,
  sendableShapes,
  shapeDraftsFrom,
  shapesDirty,
  shapesToRequest,
  snapMm,
  type PlacementDraft,
  type PlanShapeDraft,
} from "./roomPlan";

function placement(overrides: Partial<PlacementDraft> = {}): PlacementDraft {
  return {
    locationId: 12,
    xMm: 1000,
    yMm: 1000,
    rotationDeg: 0,
    widthMm: 600,
    depthMm: 400,
    ...overrides,
  };
}

function shape(overrides: Partial<PlanShapeDraft> = {}): PlanShapeDraft {
  return {
    id: "a",
    kind: "outline",
    label: null,
    isClosed: true,
    thicknessMm: null,
    points: [
      { xMm: 0, yMm: 0 },
      { xMm: 4000, yMm: 0 },
      { xMm: 4000, yMm: 3000 },
      { xMm: 0, yMm: 3000 },
    ],
    ...overrides,
  };
}

describe("the grid", () => {
  it("snaps to the step, and treats a nonsense step as a millimetre", () => {
    expect(snapMm(1249, 100)).toBe(1200);
    expect(snapMm(1251, 100)).toBe(1300);
    expect(snapMm(-1251, 100)).toBe(-1300);
    expect(snapMm(1234.7, 0)).toBe(1235);
  });

  it("clamps a fat-fingered coordinate rather than letting the server 422 it", () => {
    expect(clampCoord(9_999_999)).toBe(MAX_COORD_MM);
    expect(clampCoord(-9_999_999)).toBe(-MAX_COORD_MM);
    expect(clampCoord(12.6)).toBe(13);
  });

  it("wraps a rotation instead of failing on one", () => {
    expect(normalizeRotation(-90)).toBe(270);
    expect(normalizeRotation(450)).toBe(90);
    expect(normalizeRotation(360)).toBe(0);
  });
});

describe("the frame", () => {
  it("gives an undrawn room an honest blank surface at the origin", () => {
    // Not a wall, and never sent: the server reports `extent: null` for an empty
    // room precisely so a client does not mistake a canvas for a room.
    expect(frameOf([], [], DEFAULT_GRID_MM)).toEqual({
      minXMm: 0,
      minYMm: 0,
      widthMm: EMPTY_ROOM_MM,
      depthMm: EMPTY_ROOM_MM,
    });
  });

  it("pads what is drawn so a wall is not flush against the edge of the picture", () => {
    const frame = frameOf([shape()], [], 100);
    expect(frame.minXMm).toBe(-200);
    expect(frame.minYMm).toBe(-200);
    expect(frame.widthMm).toBe(4400);
    expect(frame.depthMm).toBe(3400);
  });

  it("counts a box's whole footprint, not just its corner", () => {
    const frame = frameOf([], [placement({ xMm: 0, yMm: 0, widthMm: 2000, depthMm: 1000 })], 100);
    expect(frame.minXMm).toBe(-200);
    expect(frame.widthMm).toBe(2400);
  });

  it("maps millimetres to percentages of itself, which is all the DOM ever sees", () => {
    const frame = { minXMm: 0, minYMm: 0, widthMm: 1000, depthMm: 1000 };
    expect(placementPct(placement({ xMm: 100, yMm: 200, widthMm: 500, depthMm: 250 }), frame)).toEqual({
      leftPct: 10,
      topPct: 20,
      widthPct: 50,
      heightPct: 25,
    });
  });

  it("maps a pointer back into millimetres, snapped, with no hit testing", () => {
    const frame = { minXMm: 0, minYMm: 0, widthMm: 1000, depthMm: 1000 };
    const rect = { left: 0, top: 0, width: 100, height: 100 };
    expect(pointFromSurface(frame, rect, 50, 25, 10)).toEqual({ xMm: 500, yMm: 250 });
    // A zero-size surface (jsdom, or a hidden panel) is answered, not divided by.
    expect(pointFromSurface(frame, { left: 0, top: 0, width: 0, height: 0 }, 9, 9, 10)).toEqual({
      xMm: 0,
      yMm: 0,
    });
  });

  it("puts a newly placed box in the middle of what is being looked at", () => {
    expect(frameCentre({ minXMm: -1000, minYMm: 0, widthMm: 2000, depthMm: 1000 }, 100)).toEqual({
      xMm: 0,
      yMm: 500,
    });
  });
});

describe("scale and ruling", () => {
  it("coarsens the ruled step until the grid is a grid rather than a grey rectangle", () => {
    const yard = { minXMm: 0, minYMm: 0, widthMm: 40_000, depthMm: 40_000 };
    const ruled = ruledStepMm(yard, 10);
    expect(ruled).toBeGreaterThan(10);
    expect(yard.widthMm / ruled).toBeLessThanOrEqual(40);
    // A step that already rules legibly is left exactly as chosen.
    expect(ruledStepMm({ minXMm: 0, minYMm: 0, widthMm: 4000, depthMm: 4000 }, 100)).toBe(100);
  });

  it("rules from the first multiple inside the frame, including through the origin", () => {
    expect(ruleLines(-250, 500, 100)).toEqual([-200, -100, 0, 100, 200]);
  });

  it("draws a round scale bar, because a plan with no scale is a doodle", () => {
    expect(scaleBarMm({ minXMm: 0, minYMm: 0, widthMm: 4000, depthMm: 3000 })).toBe(1000);
    expect(scaleBarMm({ minXMm: 0, minYMm: 0, widthMm: 800, depthMm: 800 })).toBe(200);
  });

  it("says metres above a metre, because a room is metres", () => {
    expect(formatMm(250)).toBe("250 mm");
    expect(formatMm(1000)).toBe("1 m");
    expect(formatMm(1500)).toBe("1.5 m");
  });
});

describe("a footprint nobody measured", () => {
  it("is drawn at a nominal size and says so", () => {
    expect(footprintOf({ widthMm: null, depthMm: null })).toEqual({
      widthMm: NOMINAL_FOOTPRINT_MM,
      depthMm: NOMINAL_FOOTPRINT_MM,
      nominal: true,
    });
    // Half-measured is still nominal: the picture must not imply a precision that
    // only one of the two dimensions has.
    expect(footprintOf({ widthMm: 300, depthMm: null }).nominal).toBe(true);
    expect(footprintOf({ widthMm: 300, depthMm: 200 })).toEqual({
      widthMm: 300,
      depthMm: 200,
      nominal: false,
    });
    // Zero is not a size. A zero-size box is an invisible container.
    expect(footprintOf({ widthMm: 0, depthMm: 0 }).widthMm).toBe(NOMINAL_FOOTPRINT_MM);
  });
});

describe("what reaches the wire", () => {
  it("draws a shape kind it has never heard of rather than dropping the line", () => {
    // `kind` carries no CHECK, so a newer build can write a kind this bundle does
    // not know. A line somebody drew must not vanish because the client is older.
    const drafts = shapeDraftsFrom(
      [
        {
          id: 3,
          kind: "trapdoor",
          label: " lounge ",
          is_closed: false,
          thickness_mm: 40,
          sort_order: 0,
          points: [
            { x_mm: -100, y_mm: 0 },
            { x_mm: 100, y_mm: 0 },
          ],
        },
      ],
      () => "local",
    );
    expect(drafts[0]?.kind).toBe("wall");
    expect(drafts[0]?.id).toBe("local");
  });

  it("drops an unfinished line instead of turning it into a 422", () => {
    const half = shape({ id: "half", points: [{ xMm: 0, yMm: 0 }] });
    expect(sendableShapes([shape(), half])).toHaveLength(1);
    const request = shapesToRequest([shape({ label: "  Workshop  " }), half]);
    expect(request).toHaveLength(1);
    expect(request[0]?.label).toBe("Workshop");
    expect(request[0]?.points).toHaveLength(4);
    // An empty label is null, not "", so the server keeps saying "unnamed".
    expect(shapesToRequest([shape({ label: "   " })])[0]?.label).toBeNull();
  });

  it("normalises a rotation and clamps a coordinate on the way out", () => {
    const [sent] = placementsToRequest([
      placement({ xMm: 9_999_999, rotationDeg: -90, widthMm: null }),
    ]);
    expect(sent?.x_mm).toBe(MAX_COORD_MM);
    expect(sent?.rotation_deg).toBe(270);
    // Null travels as null: it means "use the container type's size", and a
    // nominal drawing width is not a measurement to write back.
    expect(sent?.width_mm).toBeNull();
  });

  it("reads placements back off the wire", () => {
    expect(
      placementDraftsFrom([
        {
          location_id: 12,
          parent_id: 11,
          x_mm: 100,
          y_mm: 200,
          rotation_deg: 90,
          width_mm: null,
          depth_mm: 300,
        },
      ]),
    ).toEqual([
      { locationId: 12, xMm: 100, yMm: 200, rotationDeg: 90, widthMm: null, depthMm: 300 },
    ]);
  });
});

describe("what a batched save has to carry", () => {
  it("sends only what moved", () => {
    const baseline = [placement({ locationId: 12 }), placement({ locationId: 13, xMm: 50 })];
    const draft = [placement({ locationId: 12, xMm: 1500 }), placement({ locationId: 13, xMm: 50 })];
    const diff = placementDiff(baseline, draft);
    expect(diff.changed.map((item) => item.locationId)).toEqual([12]);
    expect(diff.unplaced).toEqual([]);
  });

  it("counts a rotation, a resize and a brand-new placement as changes", () => {
    const baseline = [placement({ locationId: 12 })];
    expect(placementDiff(baseline, [placement({ locationId: 12, rotationDeg: 90 })]).changed).toHaveLength(1);
    expect(placementDiff(baseline, [placement({ locationId: 12, widthMm: null })]).changed).toHaveLength(1);
    expect(placementDiff([], [placement()]).changed).toHaveLength(1);
    // 360 and 0 are the same angle, so this is not a change and must not be sent.
    expect(placementDiff(baseline, [placement({ rotationDeg: 360 })]).changed).toHaveLength(0);
  });

  it("names a child that went back to the tray, and never invents a coordinate for it", () => {
    const diff = placementDiff([placement({ locationId: 12 })], []);
    expect(diff.unplaced).toEqual([12]);
    expect(diff.changed).toEqual([]);
    // And one that was never placed is not named at all — it has no coordinate to
    // clear, and asking the server to unplace it would be a lie about history.
    expect(placementDiff([], []).unplaced).toEqual([]);
  });

  it("knows when the drawing itself changed, ignoring the client-local ids", () => {
    const baseline = [shape({ id: "server-1" })];
    expect(shapesDirty(baseline, [shape({ id: "draft-9" })])).toBe(false);
    expect(shapesDirty(baseline, [shape({ isClosed: false })])).toBe(true);
    expect(shapesDirty(baseline, [shape({ label: "Workshop" })])).toBe(true);
    expect(
      shapesDirty(baseline, [shape({ points: [...shape().points, { xMm: 10, yMm: 10 }] })]),
    ).toBe(true);
    expect(shapesDirty(baseline, [])).toBe(true);
    // Starting a line is not an edit until it is a line: a one-point draft is not
    // sendable, so it must not make the panel claim unsaved work either.
    expect(shapesDirty(baseline, [shape({ id: "server-1" }), shape({ id: "new", points: [{ xMm: 0, yMm: 0 }] })])).toBe(
      false,
    );
  });
});
