/**
 * "Where is it?" answered with the drawers, not with a filing path.
 *
 * The part screen has always been able to say *Workshop / Cabinet A / Drawer B3 /
 * Bin 7* and never able to say **which drawer front to pull**. Those are different
 * questions, and only the first one has ever had an answer here: a path is what a
 * filing system knows, and standing in front of a cabinet of forty identical
 * fronts it is a string you have to translate before it helps. The map screen has
 * drawn that translation since ADR 0002, one level at a time — nothing had ever
 * pointed it at a part.
 *
 * So this walks the tree the map already loads and draws each level the map
 * already draws, with one cell lit up. Three properties are the whole design, and
 * each of them is a thing that would otherwise have been tempting:
 *
 * - **It reuses `ContainerLayout`, it does not draw storage itself.** A second
 *   renderer would be a second opinion about what the workshop looks like, and the
 *   two would drift the first time somebody changed a view kind. Every level here
 *   is the identical component `/tree` uses, at the identical `parent_id`, so a
 *   cabinet face looks like that cabinet face — including its *empty* slots, which
 *   is not a detail: "the third of four" is only legible against the other three.
 * - **A level with nothing to choose between is a sentence, not a picture.** A map
 *   of one shed teaches nobody anything and pushes the useful step off the screen.
 *   `siblingCount` decides, so the rule is about the furniture rather than about
 *   depth.
 * - **It never invents the walk.** A destination the tree does not hold falls back
 *   to the plain path the caller already had, and says why. Half a route starting
 *   in mid-air is worse than the string — it looks authoritative and points at the
 *   wrong cabinet.
 *
 * One tree fetch, and room plans only for the levels drawn as rooms (ADR 0009) —
 * a `floor_plan` level with no plan fetched falls through to the flow of cards,
 * which is honest: nobody has drawn that room.
 */

import { useMemo } from "react";
import { Link } from "react-router-dom";

import { ContainerLayout } from "./ContainerLayout";
import { ErrorBanner, Loading } from "./Feedback";
import {
  getLocationPlan,
  getLocationTree,
  type LocationNode,
  type LocationTree,
  type RoomPlanRead,
} from "../lib/api/client";
import {
  directionsSentence,
  directionsTo,
  siblingCount,
  type Direction,
} from "../lib/locations/directions";
import { indexTree, type TreeIndex } from "../lib/locations/tree";
import { childViewOf, FLOOR_PLAN } from "../lib/locations/views";
import { useAsync } from "../lib/hooks/useAsync";

export interface WhereIsItProps {
  readonly locationId: number;
  /**
   * The path the caller already has on the wire, shown while the tree loads and
   * kept as the answer if the walk cannot be built.
   *
   * Required in spirit if not in types: this component's fallback *is* the old
   * behaviour, and a caller that passes nothing degrades to a bare id. Every
   * payload that carries a `location_id` carries a `label_path` beside it.
   */
  readonly labelPath?: string | null | undefined;
}

export function WhereIsIt({ locationId, labelPath = null }: WhereIsItProps) {
  const tree = useAsync<LocationTree>(() => getLocationTree(), []);
  const index = useMemo(
    () => (tree.data === null ? null : indexTree(tree.data.nodes)),
    [tree.data],
  );
  const directions = useMemo(
    () => (index === null ? [] : directionsTo(index, locationId)),
    [index, locationId],
  );

  if (tree.error !== null) {
    return (
      <div className="stack">
        <PlainPath labelPath={labelPath} locationId={locationId} />
        <ErrorBanner error={tree.error} fallback="The map of storage could not be loaded." />
      </div>
    );
  }
  if (index === null) {
    return (
      <div className="stack">
        <PlainPath labelPath={labelPath} locationId={locationId} />
        <Loading what="the walk there" />
      </div>
    );
  }
  if (directions.length === 0) {
    // A retired container, or one outside the fetched subtree. Said out loud:
    // silently showing the string would look like the feature simply chose not
    // to draw anything this time.
    return (
      <div className="stack">
        <PlainPath labelPath={labelPath} locationId={locationId} />
        <p className="muted-note" style={{ margin: 0 }}>
          This container is not in the current map of storage — it may have been
          retired — so the walk to it cannot be drawn. The path above is still where
          it was put.
        </p>
      </div>
    );
  }

  const last = directions[directions.length - 1] as Direction;

  return (
    <div className="stack where-is-it">
      {/* The whole answer in one line, before any picture. This is what a screen
          reader gets instead of the drawings, and what somebody who already knows
          the workshop reads and then stops reading. */}
      <p className="where-sentence">{directionsSentence(directions)}</p>

      <ol className="where-steps">
        {directions.map((step, position) => (
          <WalkStep
            key={step.to.id}
            index={index}
            step={step}
            position={position + 1}
            {...(step.at === null ? {} : { atId: step.at.id })}
          />
        ))}
      </ol>

      <p className="row-tight">
        <Link to={`/locations/${last.to.id}`}>Open {last.to.name} &rarr;</Link>
        <span className="spacer" />
        {/* The same two ids `/tree` already reads out of the URL, so "show me this
            in the big map" is a link rather than a feature. */}
        <Link
          to={`/tree?at=${last.at === null ? "" : last.at.id}&sel=${last.to.id}`}
          className="dim"
        >
          Show on the storage map &rarr;
        </Link>
      </p>
    </div>
  );
}

/** The old answer, kept as the floor under the new one. */
function PlainPath({
  labelPath,
  locationId,
}: {
  labelPath: string | null | undefined;
  locationId: number;
}) {
  return (
    <p className="where-sentence" style={{ margin: 0 }}>
      {labelPath ?? `location ${locationId}`}
    </p>
  );
}

/**
 * One turn, drawn.
 *
 * The room plan is fetched *here* rather than by the panel, so a walk through
 * four levels does not wait on four requests before drawing any of them — each
 * step paints as soon as it can, and a level that is not a room never asks.
 */
function WalkStep({
  index,
  step,
  position,
  atId,
}: {
  index: TreeIndex;
  step: Direction;
  position: number;
  atId?: number | undefined;
}) {
  const view = childViewOf(index, atId ?? null);
  const wantsPlan = view === FLOOR_PLAN && atId !== undefined;
  const plan = useAsync<RoomPlanRead | null>(
    () => (wantsPlan ? getLocationPlan(atId) : Promise.resolve(null)),
    [atId, wantsPlan],
  );

  const siblings = siblingCount(index, step);
  const where = step.at === null ? "storage" : step.at.name;
  const slot = step.to.slot_label;

  return (
    <li className="where-step">
      <p className="where-step-head">
        <span className="where-step-n mono" aria-hidden="true">
          {position}
        </span>
        <span>
          {step.at === null ? "Start at " : `In ${where}, go to `}
          <strong>{step.to.name}</strong>
          {slot !== null && slot !== "" && (
            <>
              {" "}
              <span className="mono">({slot})</span>
            </>
          )}
          {/* The count is the reason the picture below is worth looking at. Said
              in words as well as drawn, because the drawing is decorative to a
              screen reader and this sentence is not. */}
          {siblings > 1 && (
            <span className="dim"> — one of {siblings} here</span>
          )}
        </span>
      </p>

      {/* Nothing to choose between: a map of one box is furniture, not help. */}
      {siblings > 1 && (
        <div className="where-step-map">
          <ContainerLayout
            index={index}
            parentId={atId ?? null}
            highlightId={step.to.id}
            plan={plan.data}
            /* No nested previews. The map screen previews children to show what
               kind of thing a cell is; here the next step draws that level
               properly one card down, so a preview would be the same information
               twice and a much taller page on a phone. */
            previewDepth={0}
            /* Directions, not a tutorial on the renderer — see `quiet`. */
            quiet
            drillTo={(node: LocationNode) => `/locations/${node.id}`}
          />
        </div>
      )}
    </li>
  );
}
