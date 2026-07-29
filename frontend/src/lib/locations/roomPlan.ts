/**
 * The arithmetic behind a drawn room — ADR 0009, client side.
 *
 * Everything here is pure. It converts between the three coordinate systems a
 * floor plan unavoidably has, and it holds the drafts an editing session
 * accumulates before one batched save:
 *
 * 1. **Millimetres**, signed, in the room's own frame. This is what the database
 *    stores and the only thing ever sent. The origin is wherever the person
 *    drawing put it (ADR 0009: "requiring it to be a corner of the room would
 *    make the first wall they drew the wrong one"), which is why nothing here
 *    assumes it is a corner or that coordinates are positive.
 * 2. **A `PlanFrame`** — the millimetre window actually being drawn, padded out
 *    from the content's bounding box and snapped to the grid. Derived on every
 *    render, never stored, for the same reason the server derives `extent`.
 * 3. **Percentages of that frame**, which is what reaches the DOM. Percentages
 *    rather than pixels so the surface is fluid: the same plan is read on a phone
 *    at the shelf and on a desktop, and nothing here needs to know which.
 *
 * **No geometry library, and no rotation maths.** A placed box's drawn extent is
 * its axis-aligned width × depth, exactly as the server reports it — ADR 0009
 * states that limit rather than correcting it, and a rotated bounding box would
 * be the first geometry function in the codebase. Rotation is drawn by CSS on the
 * box itself, which is honest about being a picture and not a collision surface.
 *
 * **Nothing here validates.** Overlapping containers are allowed, a
 * self-intersecting outline is drawn as given, and a box outside the walls is
 * simply outside them — capacity is advisory everywhere else in this system and a
 * drawing is a weaker claim than capacity, not a stronger one.
 */

import type { PlanShapeKind, PlanShapeRead, PlacementRead } from "../api/client";

// ---------------------------------------------------------------- drafts ----

export interface PlanPointDraft {
  readonly xMm: number;
  readonly yMm: number;
}

/**
 * One drawn polyline while it is being edited.
 *
 * `id` is client-local and deliberately never sent: the whole plan is replaced on
 * every save, so the server's shape ids change each time and a client that held
 * one would be holding a stale key. It exists only as a React list key.
 */
export interface PlanShapeDraft {
  readonly id: string;
  readonly kind: PlanShapeKind;
  readonly label: string | null;
  readonly isClosed: boolean;
  readonly thicknessMm: number | null;
  readonly points: readonly PlanPointDraft[];
}

/** Where one child stands, as the editor holds it before a save. */
export interface PlacementDraft {
  readonly locationId: number;
  readonly xMm: number;
  readonly yMm: number;
  readonly rotationDeg: number;
  readonly widthMm: number | null;
  readonly depthMm: number | null;
}

// ----------------------------------------------------------------- kinds ----

export const SHAPE_KINDS: readonly PlanShapeKind[] = [
  "outline",
  "wall",
  "door",
  "window",
  "fixture",
  "zone",
];

export const SHAPE_LABELS: Readonly<Record<PlanShapeKind, string>> = {
  outline: "Outline — the room's own walls",
  wall: "Wall — a run inside the room",
  door: "Door",
  window: "Window",
  fixture: "Fixture — a sink, a pillar, a bench that holds nothing",
  zone: "Zone — an area you want named",
};

/**
 * What to draw a line as when nobody measured its thickness.
 *
 * Nominal, and the field stays null so the server keeps saying "unmeasured"
 * rather than recording a number the user never gave. ADR 0009's own words:
 * "nobody measures the thickness of a door swing".
 */
export const NOMINAL_THICKNESS_MM: Readonly<Record<PlanShapeKind, number>> = {
  outline: 100,
  wall: 100,
  door: 40,
  window: 40,
  fixture: 30,
  zone: 20,
};

/** Whether a freshly started shape of this kind is a loop or a run. */
export const CLOSED_BY_DEFAULT: Readonly<Record<PlanShapeKind, boolean>> = {
  outline: true,
  wall: false,
  door: false,
  window: false,
  fixture: true,
  zone: true,
};

export function isShapeKind(value: string): value is PlanShapeKind {
  return (SHAPE_KINDS as readonly string[]).includes(value);
}

// ------------------------------------------------------------------ grid ----

/** Grid steps offered, in millimetres. 100 mm is a plan you can draw with. */
export const GRID_CHOICES: readonly number[] = [10, 50, 100, 250, 500, 1000];

export const DEFAULT_GRID_MM = 100;

/**
 * A room with nothing in it yet, in millimetres square.
 *
 * Only ever used as the *drawing surface* for an undrawn room — it is never sent,
 * never stored, and never becomes a shape. The server reports `extent: null` for
 * an empty room precisely so a client does not mistake a canvas for a wall.
 */
export const EMPTY_ROOM_MM = 4000;

export function snapMm(valueMm: number, gridMm: number): number {
  const step = gridMm > 0 ? gridMm : 1;
  return Math.round(valueMm / step) * step;
}

/** The server's own bound on a coordinate (`PlanCoordMm`), enforced here too so
 * a fat-fingered numeric field is corrected rather than 422'd. */
export const MAX_COORD_MM = 1_000_000;

export function clampCoord(valueMm: number): number {
  return Math.max(-MAX_COORD_MM, Math.min(MAX_COORD_MM, Math.round(valueMm)));
}

/** 0–359, the server's `PlanRotationDeg`. Negative input wraps rather than failing. */
export function normalizeRotation(deg: number): number {
  const whole = Math.round(deg);
  return ((whole % 360) + 360) % 360;
}

// ---------------------------------------------------------------- extent ----

/**
 * How big to draw a box whose footprint nobody knows.
 *
 * A placement's width/depth are null when neither the placement nor the container
 * type carries a size, and `room_plan.Placement` documents that as "draw a
 * nominal box, not zero size". A nominal box is drawn dashed by the stylesheet, so
 * an unmeasured footprint never passes for a measured one.
 */
export const NOMINAL_FOOTPRINT_MM = 400;

export interface Footprint {
  readonly widthMm: number;
  readonly depthMm: number;
  /** True when either dimension was invented — the picture says so too. */
  readonly nominal: boolean;
}

export function footprintOf(placement: {
  readonly widthMm: number | null;
  readonly depthMm: number | null;
}): Footprint {
  const width = placement.widthMm;
  const depth = placement.depthMm;
  return {
    widthMm: width !== null && width > 0 ? width : NOMINAL_FOOTPRINT_MM,
    depthMm: depth !== null && depth > 0 ? depth : NOMINAL_FOOTPRINT_MM,
    nominal: !(width !== null && width > 0) || !(depth !== null && depth > 0),
  };
}

export interface PlanFrame {
  readonly minXMm: number;
  readonly minYMm: number;
  readonly widthMm: number;
  readonly depthMm: number;
}

/** The smallest span a frame is ever drawn at, so one point is not a zero-size box. */
const MIN_SPAN_STEPS = 8;

/**
 * The millimetre window to draw, given what is in the room.
 *
 * Padded by two grid steps on every side and snapped outward, so a wall on the
 * edge of the drawing is not flush against the edge of the picture and a box can
 * be dragged past the last thing drawn. Derived on every render: a stored canvas
 * size would be a second fact to keep in step with the drawing, and the first
 * thing drawn outside it would be invisible rather than obviously outside.
 */
export function frameOf(
  shapes: readonly PlanShapeDraft[],
  placements: readonly PlacementDraft[],
  gridMm: number = DEFAULT_GRID_MM,
): PlanFrame {
  const step = gridMm > 0 ? gridMm : 1;
  const xs: number[] = [];
  const ys: number[] = [];
  for (const shape of shapes) {
    for (const point of shape.points) {
      xs.push(point.xMm);
      ys.push(point.yMm);
    }
  }
  for (const placement of placements) {
    const box = footprintOf(placement);
    xs.push(placement.xMm, placement.xMm + box.widthMm);
    ys.push(placement.yMm, placement.yMm + box.depthMm);
  }
  if (xs.length === 0 || ys.length === 0) {
    // Nothing drawn and nothing placed: an honest blank surface to draw the first
    // wall on, at the origin, so the numbers a user types match what they see.
    return { minXMm: 0, minYMm: 0, widthMm: EMPTY_ROOM_MM, depthMm: EMPTY_ROOM_MM };
  }
  const pad = step * 2;
  const minX = Math.floor((Math.min(...xs) - pad) / step) * step;
  const minY = Math.floor((Math.min(...ys) - pad) / step) * step;
  const maxX = Math.ceil((Math.max(...xs) + pad) / step) * step;
  const maxY = Math.ceil((Math.max(...ys) + pad) / step) * step;
  const floor = step * MIN_SPAN_STEPS;
  return {
    minXMm: minX,
    minYMm: minY,
    widthMm: Math.max(maxX - minX, floor),
    depthMm: Math.max(maxY - minY, floor),
  };
}

/** A percentage of the frame — what actually reaches a `style` attribute. */
export interface FramePct {
  readonly leftPct: number;
  readonly topPct: number;
  readonly widthPct: number;
  readonly heightPct: number;
}

export function placementPct(placement: PlacementDraft, frame: PlanFrame): FramePct {
  const box = footprintOf(placement);
  return {
    leftPct: ((placement.xMm - frame.minXMm) / frame.widthMm) * 100,
    topPct: ((placement.yMm - frame.minYMm) / frame.depthMm) * 100,
    widthPct: (box.widthMm / frame.widthMm) * 100,
    heightPct: (box.depthMm / frame.depthMm) * 100,
  };
}

/**
 * A point on the surface, in millimetres, snapped.
 *
 * `rect` is the surface's own bounding box, so this is a linear map and not hit
 * testing: which element was hit is answered by the DOM, as it is everywhere else
 * in this app.
 */
export function pointFromSurface(
  frame: PlanFrame,
  rect: { readonly left: number; readonly top: number; readonly width: number; readonly height: number },
  clientX: number,
  clientY: number,
  gridMm: number,
): PlanPointDraft {
  const fx = rect.width > 0 ? (clientX - rect.left) / rect.width : 0;
  const fy = rect.height > 0 ? (clientY - rect.top) / rect.height : 0;
  return {
    xMm: clampCoord(snapMm(frame.minXMm + fx * frame.widthMm, gridMm)),
    yMm: clampCoord(snapMm(frame.minYMm + fy * frame.depthMm, gridMm)),
  };
}

/** The centre of the frame, snapped — where a container lands when it is first placed. */
export function frameCentre(frame: PlanFrame, gridMm: number): PlanPointDraft {
  return {
    xMm: clampCoord(snapMm(frame.minXMm + frame.widthMm / 2, gridMm)),
    yMm: clampCoord(snapMm(frame.minYMm + frame.depthMm / 2, gridMm)),
  };
}

// ------------------------------------------------------- grid and scale -----

/**
 * How many ruled lines is still a grid rather than a grey rectangle.
 *
 * 64 rather than 40 so that an ordinary room rules at the step it snaps to: a
 * 5 m workshop on the default 100 mm grid is 56 lines, which is legible, and
 * ruling it at 200 mm while snapping at 100 mm was a difference the reader had to
 * be told about for no benefit. A yard still coarsens.
 */
export const MAX_RULED_LINES = 64;

/**
 * The grid step to actually rule, and how many lines that is.
 *
 * The chosen step is coarsened by whole multiples until the ruling is legible —
 * a 40 m yard on a 10 mm grid is 4000 lines and a grey rectangle. It coarsens
 * rather than hiding the grid, because the snap step and the drawn step being
 * different is a smaller lie than a plan with no scale at all.
 */
export function ruledStepMm(frame: PlanFrame, gridMm: number, maxLines = MAX_RULED_LINES): number {
  const step = gridMm > 0 ? gridMm : 1;
  const span = Math.max(frame.widthMm, frame.depthMm);
  let ruled = step;
  while (span / ruled > maxLines) {
    ruled *= ruled * 2 <= span ? 2 : 10;
    if (ruled >= span) {
      break;
    }
  }
  return ruled;
}

/** Ruling positions in millimetres, from the first multiple inside the frame. */
export function ruleLines(fromMm: number, spanMm: number, stepMm: number): number[] {
  const step = stepMm > 0 ? stepMm : 1;
  const lines: number[] = [];
  let at = Math.ceil(fromMm / step) * step;
  while (at <= fromMm + spanMm) {
    lines.push(at);
    at += step;
  }
  return lines;
}

/**
 * A round distance to draw a scale bar for — the largest 1/2/5 × 10ⁿ that fits in
 * a third of the frame's width. A plan with no scale is a doodle.
 */
export function scaleBarMm(frame: PlanFrame): number {
  const target = frame.widthMm / 3;
  const candidates: number[] = [];
  for (let magnitude = 1; magnitude <= 100_000; magnitude *= 10) {
    candidates.push(magnitude, magnitude * 2, magnitude * 5);
  }
  let best = candidates[0] ?? 1;
  for (const candidate of candidates) {
    if (candidate <= target) {
      best = candidate;
    }
  }
  return best;
}

/** Millimetres as a human distance. Metres above a metre, because a room is metres. */
export function formatMm(valueMm: number): string {
  const abs = Math.abs(valueMm);
  if (abs >= 1000) {
    const metres = valueMm / 1000;
    const text = Number.isInteger(metres) ? String(metres) : metres.toFixed(2).replace(/0+$/, "");
    return `${text} m`;
  }
  return `${Math.round(valueMm)} mm`;
}

// ------------------------------------------------ to and from the server ----

export function shapeDraftsFrom(
  shapes: readonly PlanShapeRead[],
  makeId: () => string,
): PlanShapeDraft[] {
  return shapes.map((shape) => ({
    id: makeId(),
    // `kind` is a plain string on the wire — the column carries no CHECK, so a
    // row written by a newer build can name a kind this bundle never heard of.
    // Drawn as a wall rather than dropped: a line somebody drew must not vanish
    // because the client is older than the drawing.
    kind: isShapeKind(shape.kind) ? shape.kind : "wall",
    label: shape.label,
    isClosed: shape.is_closed,
    thicknessMm: shape.thickness_mm,
    points: shape.points.map((point) => ({ xMm: point.x_mm, yMm: point.y_mm })),
  }));
}

export function placementDraftsFrom(placements: readonly PlacementRead[]): PlacementDraft[] {
  return placements.map((placement) => ({
    locationId: placement.location_id,
    xMm: placement.x_mm,
    yMm: placement.y_mm,
    rotationDeg: placement.rotation_deg,
    widthMm: placement.width_mm,
    depthMm: placement.depth_mm,
  }));
}

/** The server refuses a one-point line (`PLAN_MIN_POINTS`), so an unfinished one
 * is dropped on the way out rather than turned into a 422 the user cannot read. */
export const MIN_SHAPE_POINTS = 2;

export function sendableShapes(shapes: readonly PlanShapeDraft[]): PlanShapeDraft[] {
  return shapes.filter((shape) => shape.points.length >= MIN_SHAPE_POINTS);
}

export function shapesToRequest(
  shapes: readonly PlanShapeDraft[],
): {
  kind: PlanShapeKind;
  label: string | null;
  is_closed: boolean;
  thickness_mm: number | null;
  points: { x_mm: number; y_mm: number }[];
}[] {
  return sendableShapes(shapes).map((shape) => ({
    kind: shape.kind,
    label: shape.label === null || shape.label.trim() === "" ? null : shape.label.trim(),
    is_closed: shape.isClosed,
    thickness_mm: shape.thicknessMm,
    points: shape.points.map((point) => ({
      x_mm: clampCoord(point.xMm),
      y_mm: clampCoord(point.yMm),
    })),
  }));
}

export function placementsToRequest(
  placements: readonly PlacementDraft[],
): {
  location_id: number;
  x_mm: number;
  y_mm: number;
  rotation_deg: number;
  width_mm: number | null;
  depth_mm: number | null;
}[] {
  return placements.map((placement) => ({
    location_id: placement.locationId,
    x_mm: clampCoord(placement.xMm),
    y_mm: clampCoord(placement.yMm),
    rotation_deg: normalizeRotation(placement.rotationDeg),
    width_mm: placement.widthMm,
    depth_mm: placement.depthMm,
  }));
}

// ----------------------------------------------------------------- dirty ----

function samePoints(a: readonly PlanPointDraft[], b: readonly PlanPointDraft[]): boolean {
  return (
    a.length === b.length &&
    a.every((point, at) => point.xMm === b[at]?.xMm && point.yMm === b[at]?.yMm)
  );
}

/** Whether the drawing differs from what the server last returned. Ids are not
 * compared: they are client-local and change on every load. */
export function shapesDirty(
  baseline: readonly PlanShapeDraft[],
  draft: readonly PlanShapeDraft[],
): boolean {
  const sendable = sendableShapes(draft);
  if (sendable.length !== baseline.length) {
    return true;
  }
  return sendable.some((shape, at) => {
    const was = baseline[at];
    return (
      was === undefined ||
      shape.kind !== was.kind ||
      (shape.label ?? "") !== (was.label ?? "") ||
      shape.isClosed !== was.isClosed ||
      shape.thicknessMm !== was.thicknessMm ||
      !samePoints(shape.points, was.points)
    );
  });
}

export function samePlacement(a: PlacementDraft, b: PlacementDraft): boolean {
  return (
    a.locationId === b.locationId &&
    a.xMm === b.xMm &&
    a.yMm === b.yMm &&
    normalizeRotation(a.rotationDeg) === normalizeRotation(b.rotationDeg) &&
    a.widthMm === b.widthMm &&
    a.depthMm === b.depthMm
  );
}

/**
 * What a batched save has to carry: every placement that moved or appeared, and
 * every child sent back to the tray.
 *
 * "Nowhere" is its own field rather than a sentinel coordinate — ADR 0009 — so a
 * child that was placed and is now unplaced has to be named in
 * `unplace_location_ids`, and one that never had a coordinate must not be.
 */
export function placementDiff(
  baseline: readonly PlacementDraft[],
  draft: readonly PlacementDraft[],
): { readonly changed: PlacementDraft[]; readonly unplaced: number[] } {
  const was = new Map(baseline.map((placement) => [placement.locationId, placement]));
  const now = new Map(draft.map((placement) => [placement.locationId, placement]));
  const changed = draft.filter((placement) => {
    const before = was.get(placement.locationId);
    return before === undefined || !samePlacement(before, placement);
  });
  const unplaced = [...was.keys()].filter((id) => !now.has(id));
  return { changed, unplaced };
}
