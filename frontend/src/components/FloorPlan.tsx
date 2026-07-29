/**
 * A room, read rather than edited: the walls as drawn, the containers where they
 * stand, and the ones that have no place yet — said out loud instead of parked at
 * the origin.
 *
 * This is the payoff of ADR 0009 and the reason the coordinates exist at all: the
 * workshop finally looks like the workshop. It is *non-interactive as a plan* —
 * nothing here drags, rotates or saves — but every box is a real link, because a
 * drawn container you cannot tap is a picture of a container rather than a way to
 * reach one. That is the same rule the map view already lives by: in this renderer
 * the cell *is* the link, so anything not drawn is not merely invisible, it is
 * unreachable.
 *
 * Which is why the **tray is not optional**. A child with no placement is drawn
 * beside the plan under "not placed yet", never at (0, 0): ADR 0009 refuses to
 * default a coordinate precisely because every pre-existing container would then
 * stand in the same corner of every room and *look authored*. A tray says "nobody
 * has put this anywhere", which is the truth and is also the fix.
 *
 * Overlapping boxes are drawn overlapping. A box does sit on a bench, geometry
 * here is advisory exactly as capacity is elsewhere, and nothing in this system
 * refuses a fact because it is untidy.
 */

import { useMemo, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { PlanSurface, planBoxStyle } from "./PlanSurface";
import type { LocationNode, RoomPlanRead } from "../lib/api/client";
import {
  DEFAULT_GRID_MM,
  footprintOf,
  formatMm,
  frameOf,
  placementDraftsFrom,
  shapeDraftsFrom,
  type PlacementDraft,
} from "../lib/locations/roomPlan";

export interface FloorPlanProps {
  readonly plan: RoomPlanRead;
  /** The children of the container whose plan this is. */
  readonly nodes: readonly LocationNode[];
  /** Where a box links to — the same rule the map's cells use. */
  readonly hrefOf: (node: LocationNode) => string;
  /**
   * How the tray draws a child that has nowhere to stand.
   *
   * Passed in rather than written here so an unplaced container is drawn by the
   * *same* card as a placed one is in the flow view — being unplaced is a missing
   * coordinate, not a lesser kind of container.
   */
  readonly renderCard: (node: LocationNode) => ReactNode;
}

export function FloorPlan({ plan, nodes, hrefOf, renderCard }: FloorPlanProps) {
  const shapes = useMemo(() => {
    let next = 0;
    return shapeDraftsFrom(plan.shapes, () => `shape-${next++}`);
  }, [plan.shapes]);
  const placements = useMemo(() => placementDraftsFrom(plan.placements), [plan.placements]);
  const frame = useMemo(() => frameOf(shapes, placements, DEFAULT_GRID_MM), [shapes, placements]);

  const byId = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  const placedIds = new Set(placements.map((placement) => placement.locationId));
  // A placement whose child is not in this list is a child the tree fetch did not
  // return (a retired one, or a subtree boundary). Skipped rather than drawn as a
  // nameless box: a box with no name is not a container anybody can find.
  const boxes = placements.flatMap((placement) => {
    const node = byId.get(placement.locationId);
    return node === undefined ? [] : [{ placement, node }];
  });
  const unplaced = nodes.filter((node) => !placedIds.has(node.id));

  return (
    <div className="stack">
      <PlanSurface frame={frame} shapes={shapes} gridMm={DEFAULT_GRID_MM} label="Floor plan">
        {boxes.map(({ placement, node }) => (
          <Link
            key={node.id}
            className={boxClass(placement, node, "plan-box")}
            style={planBoxStyle(placement, frame)}
            to={hrefOf(node)}
            aria-label={boxLabel(placement, node)}
          >
            <span className="plan-box-name">{node.name}</span>
          </Link>
        ))}
      </PlanSurface>

      <p className="muted-note" style={{ margin: 0 }}>
        Drawn to scale: each box is where that container stands and how big it is.
        Containers may overlap — a box does sit on a bench — and a dashed box is one whose
        footprint nobody has measured, drawn at a nominal size. A plan is a floor: something
        bolted to the wall looks the same as something on the ground.
      </p>

      {unplaced.length > 0 && (
        <div className="plan-tray stack">
          <p className="muted-note" style={{ margin: 0 }}>
            <strong>Not placed yet ({unplaced.length}).</strong> These are inside this container
            but nowhere on the plan. Nothing is guessed at: edit this container to put them
            where they actually stand.
          </p>
          <div className="layout-plan">{unplaced.map((node) => renderCard(node))}</div>
        </div>
      )}
    </div>
  );
}

/**
 * The classes a box carries. Every one of them is a *shape* — dashed, thicker,
 * outlined — because a hue is never the only signal for anything here, and
 * "measured" versus "nominal" has to survive a monochrome screen.
 */
export function boxClass(
  placement: PlacementDraft,
  node: { readonly is_overfull: boolean },
  base: string,
): string {
  const classes = [base];
  if (footprintOf(placement).nominal) {
    classes.push("plan-box-nominal");
  }
  if (node.is_overfull) {
    classes.push("plan-box-over");
  }
  return classes.join(" ");
}

/**
 * What a screen reader hears, and what a keyboard user hears *change* as they
 * arrow a box around — which is the only feedback a non-sighted placement has, so
 * the coordinate is in the name rather than only in the picture.
 */
export function boxLabel(placement: PlacementDraft, node: { readonly name: string }): string {
  const box = footprintOf(placement);
  return (
    `${node.name}, at ${formatMm(placement.xMm)} across, ${formatMm(placement.yMm)} down` +
    `, ${formatMm(box.widthMm)} by ${formatMm(box.depthMm)}${box.nominal ? " (nominal)" : ""}` +
    (placement.rotationDeg === 0 ? "" : `, turned ${placement.rotationDeg} degrees`)
  );
}
