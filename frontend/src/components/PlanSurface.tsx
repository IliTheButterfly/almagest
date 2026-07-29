/**
 * The drawing surface a room plan is drawn on — the one picture, shared by the
 * page that reads it and the panel that edits it.
 *
 * ADR 0009 settled the data; this settles the *drawing*, and it is deliberately
 * one component so that read mode and edit mode cannot drift into two different
 * rooms. What is inside it is the caller's business: this draws the grid, the
 * walls and the scale, and absolutely positions whatever boxes it is given.
 *
 * **Inline SVG plus absolutely-positioned DOM, not a canvas.** A canvas would mean
 * hand-rolled hit testing, a hand-rolled focus order and a hand-rolled name for
 * every box — three things the DOM already does, in a project whose stated
 * dominant risk is a solo maintainer drowning in an over-engineered stack. So a
 * placed container is a real `<a>` or `<button>`, it is tabbable because it is a
 * button, and a screen reader reads its name because it has one.
 *
 * **Millimetres reach the SVG directly**, through a `viewBox` in the room's own
 * frame, so a 100 mm stud wall is drawn 100 mm thick and no scaling arithmetic is
 * written here at all. The boxes on top are positioned in *percentages* of the
 * same frame (`roomPlan.placementPct`), which is what makes the surface fluid: the
 * same plan is read on a phone at the shelf and on a desktop, and neither needs a
 * pixel size. `preserveAspectRatio="none"` is what keeps those two agreeing if CSS
 * ever gives the surface a shape other than the frame's own.
 *
 * **A plan with no scale is a doodle**, so the scale bar and the ruled grid step
 * are part of the surface rather than an optional extra a caller might forget.
 */

import type { CSSProperties, PointerEvent as ReactPointerEvent, ReactNode, RefObject } from "react";

import {
  formatMm,
  NOMINAL_THICKNESS_MM,
  normalizeRotation,
  placementPct,
  ruleLines,
  ruledStepMm,
  scaleBarMm,
  type PlacementDraft,
  type PlanFrame,
  type PlanShapeDraft,
} from "../lib/locations/roomPlan";

/**
 * Where one box sits on the surface, as a `style` object.
 *
 * Percentages of the frame, and rotation as a CSS transform about the box's own
 * centre — ADR 0009 is explicit that rotation is *drawn* and never applied to the
 * footprint, so this is a picture and not a collision surface. Doing it in CSS is
 * what keeps the first geometry function out of the codebase.
 */
export function planBoxStyle(placement: PlacementDraft, frame: PlanFrame): CSSProperties {
  const pct = placementPct(placement, frame);
  const rotation = normalizeRotation(placement.rotationDeg);
  return {
    left: `${pct.leftPct}%`,
    top: `${pct.topPct}%`,
    width: `${pct.widthPct}%`,
    height: `${pct.heightPct}%`,
    ...(rotation === 0 ? {} : { transform: `rotate(${rotation}deg)` }),
  };
}

function pointsAttr(shape: PlanShapeDraft): string {
  return shape.points.map((point) => `${point.xMm},${point.yMm}`).join(" ");
}

export interface PlanSurfaceProps {
  readonly frame: PlanFrame;
  readonly shapes: readonly PlanShapeDraft[];
  /** The snap step. Only ever *coarsened* for ruling — see `ruledStepMm`. */
  readonly gridMm: number;
  /** What a screen reader calls the surface. */
  readonly label: string;
  /** The placed boxes, positioned by the caller with `planBoxStyle`. */
  readonly children?: ReactNode | undefined;
  /** Needed by the editor to map a pointer back into millimetres. */
  readonly surfaceRef?: RefObject<HTMLDivElement | null> | undefined;
  readonly onPointerDown?: ((event: ReactPointerEvent<HTMLDivElement>) => void) | undefined;
  /** Set while a line is being drawn: stops a touch from scrolling the page. */
  readonly capturesTouch?: boolean | undefined;
  /**
   * Dot every drawn corner.
   *
   * On by default only for the editor, which is where a corner is a thing you
   * placed and might want to undo. On a plan you are merely reading they are
   * artefacts on the walls — the wall is the information, not its vertices.
   */
  readonly showVertices?: boolean | undefined;
}

export function PlanSurface({
  frame,
  shapes,
  gridMm,
  label,
  children,
  surfaceRef,
  onPointerDown,
  capturesTouch = false,
  showVertices = false,
}: PlanSurfaceProps) {
  const ruled = ruledStepMm(frame, gridMm);
  const verticals = ruleLines(frame.minXMm, frame.widthMm, ruled);
  const horizontals = ruleLines(frame.minYMm, frame.depthMm, ruled);
  const bar = scaleBarMm(frame);
  // A vertex dot has to be visible at any room size, and the viewBox is in
  // millimetres — so its radius is a fraction of the frame rather than a constant
  // that would be a boulder in a drawer and invisible in a yard.
  const vertexR = Math.max(frame.widthMm, frame.depthMm) / 150;

  return (
    <div className="plan-frame">
      <div
        className={capturesTouch ? "plan-surface plan-surface-drawing" : "plan-surface"}
        ref={surfaceRef}
        role="group"
        aria-label={label}
        style={{ aspectRatio: `${frame.widthMm} / ${frame.depthMm}` }}
        onPointerDown={onPointerDown}
      >
        {/*
          Decorative: every fact the ink carries is also carried in words — the
          scale under it, the shape list beside it, and each box's own label. A
          screen reader gets nothing from a `<line>`.
        */}
        <svg
          className="plan-ink"
          viewBox={`${frame.minXMm} ${frame.minYMm} ${frame.widthMm} ${frame.depthMm}`}
          preserveAspectRatio="none"
          aria-hidden="true"
          focusable="false"
        >
          <g className="plan-grid">
            {verticals.map((at) => (
              <line
                key={`v${at}`}
                className={at === 0 ? "plan-axis" : undefined}
                x1={at}
                y1={frame.minYMm}
                x2={at}
                y2={frame.minYMm + frame.depthMm}
                vectorEffect="non-scaling-stroke"
              />
            ))}
            {horizontals.map((at) => (
              <line
                key={`h${at}`}
                className={at === 0 ? "plan-axis" : undefined}
                x1={frame.minXMm}
                y1={at}
                x2={frame.minXMm + frame.widthMm}
                y2={at}
                vectorEffect="non-scaling-stroke"
              />
            ))}
          </g>
          {shapes.map((shape) =>
            shape.points.length === 0 ? null : shape.isClosed ? (
              <polygon
                key={shape.id}
                className={`plan-shape plan-shape-${shape.kind}`}
                points={pointsAttr(shape)}
                strokeWidth={shape.thicknessMm ?? NOMINAL_THICKNESS_MM[shape.kind]}
              />
            ) : (
              <polyline
                key={shape.id}
                className={`plan-shape plan-shape-${shape.kind}`}
                points={pointsAttr(shape)}
                strokeWidth={shape.thicknessMm ?? NOMINAL_THICKNESS_MM[shape.kind]}
              />
            ),
          )}
          {(showVertices ? shapes : []).map((shape) =>
            shape.points.map((point, at) => (
              <circle
                key={`${shape.id}:${at}`}
                className="plan-vertex"
                cx={point.xMm}
                cy={point.yMm}
                r={vertexR}
                vectorEffect="non-scaling-stroke"
              />
            )),
          )}
        </svg>
        {children}
      </div>
      {/*
        The scale, and the step the grid is actually ruled at — which is not always
        the snap step, because a 40 m yard on a 10 mm grid is 4000 lines and a grey
        rectangle. Saying both is what stops that being a silent lie.
      */}
      <p className="plan-scale muted-note" style={{ margin: 0 }}>
        <span
          className="plan-scale-bar"
          style={{ width: `${(bar / frame.widthMm) * 100}%` }}
          aria-hidden="true"
        />
        <span>
          {formatMm(bar)} · grid {formatMm(ruled)}
          {ruled === gridMm ? "" : ` (snapping to ${formatMm(gridMm)})`}
        </span>
      </p>
    </div>
  );
}
