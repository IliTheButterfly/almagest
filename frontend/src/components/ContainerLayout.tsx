/**
 * A container drawn as the thing it is: a workshop of cabinets, a cabinet of
 * drawers, a baseplate of bins.
 *
 * One component, called on itself. The containment chain in this system is
 * `room → cabinet → drawer → baseplate → bin → divider`, every arrow of it "a
 * thing presenting a grid" holding "a thing occupying part of that grid", with no
 * depth limit and no named levels (ADR 0002). A renderer with a special case per
 * level would contradict the schema, so this one renders *any* container's
 * children and drills into any of them — and calls itself, in `mini` form, to
 * show what is inside a cell without leaving the screen.
 *
 * **What picture to draw is the level's own answer, at every depth** (ADR 0006).
 * `childViewOf(index, parentId)` is the *only* thing that decides, it is called
 * with the id of whichever container is being drawn, and `parentId === null` —
 * the roots — goes through the identical call rather than a hardcoded default. So
 * a workshop, a cabinet and a baseplate render differently in the same pass
 * because they carry different `effective_child_view`s, not because anything here
 * knows what a cabinet is. `ContainerLayout.recursion.test.tsx` asserts exactly
 * that, at three nested depths in one render.
 *
 * ---------------------------------------------------------------------------
 * KNOWN LIMITATION — the *positions* here are inferred, not authored.
 *
 * The view kind now comes from the API. The row/column each child sits at still
 * does not: `locations.row_idx`/`col_idx` exist in the database and
 * `LocationNode` carries neither, so for the two slotted views the geometry is
 * read back out of the label text by `lib/locations/slots.ts`.
 *
 * The **floor plan is the exception, and now the counter-example**: when a caller
 * hands this component a `plan` (ADR 0009), the boxes it draws are *authored*
 * millimetre coordinates from the server and nothing is inferred from a label. A
 * container in that plan with no coordinate is drawn in a "not placed yet" tray
 * rather than at the origin, for the same reason the two slotted views refuse to
 * guess a grid: a guessed position puts a drawer somewhere it is not.
 *
 * `GET /api/locations/{id}/layout` does return the authored geometry, per
 * location. When the tree screen starts fetching it, the swap is: hand the
 * resulting `Layout` to this component and delete the `inferLayout` call. This
 * component takes a `Layout` as data and asks no questions about where it came
 * from, and `slots.ts` is the only module that knows about label parsing at all.
 * ---------------------------------------------------------------------------
 */

import { useMemo } from "react";
import { Link } from "react-router-dom";

import type { LocationNode, RoomPlanRead } from "../lib/api/client";
import { formatQty } from "../lib/format";
import { inferLayout, type FallbackReason, type Layout } from "../lib/locations/slots";
import { childrenOf, type TreeIndex } from "../lib/locations/tree";
import {
  childCanvasOf,
  childViewOf,
  isSlotted,
  VIEW_NOTES,
  type ChildView,
} from "../lib/locations/views";
import { ContainerPhoto } from "./ContainerPhoto";
import { FillMeter } from "./FillMeter";
import { FloorPlan } from "./FloorPlan";

/** Beyond this many children, a nested preview is noise rather than a picture. */
const MAX_PREVIEW_CELLS = 36;

/**
 * …and beyond this many, a preview *of a preview* is. The first level of nesting
 * earns its keep — it is what makes a baseplate read as a baseplate from the
 * cabinet above it — but a 36-cell plate inside each of 36 drawers is 1296 dots
 * and no information.
 */
const MAX_NESTED_PREVIEW_CELLS = 12;

/**
 * How many levels of nested preview the map draws by default.
 *
 * Two rather than one: the whole point of the view axis is that three levels of
 * the same tree look like three different things, and you cannot see that in a
 * screenshot of two.
 */
const DEFAULT_PREVIEW_DEPTH = 2;

/** One shared empty array, so "nothing is excluded" is a stable dependency. */
const NO_EXCLUSIONS: readonly number[] = [];

const FALLBACK_NOTE: Readonly<Record<FallbackReason, string>> = {
  unlabelled:
    "This is drawn as a grid, but nothing in it carries a slot label — so the positions " +
    "are not guessed at, and the containers are listed in the order they were created.",
  unparsed:
    "These labels do not fit the row-and-column scheme, so the positions are not " +
    "guessed at. A guessed grid would put a drawer somewhere it is not.",
  collision:
    "Two labels here read as the same cell, so the reading must be wrong and the " +
    "layout is not drawn.",
  implausible:
    "The labels imply a grid far larger than the number of containers, so the " +
    "reading is probably wrong and the layout is not drawn.",
};

/**
 * What pressing a cell does, when the map is being used to *choose* a container
 * rather than to walk around storage.
 *
 * The map is the only honest picture of where things are — it draws the empty
 * positions too, so "drawer B3 has nothing in it" is visible — and Iliana asked
 * for "the same UI as the storage tab, being able to select the containers as
 * they appear there" wherever a destination is chosen. That has to be a callback
 * and not a link: a picker runs inside a form on the way to a commit, and routing
 * away would discard the quantity already typed. Issue #43.
 *
 * `onPick` is the cell body. `onDrill` is a separate control on a cell that has
 * children, because in a picker "choose this drawer" and "look inside this
 * cabinet" are both wanted and a single press cannot mean both — a shelf holds
 * stock as well as bins.
 */
export interface CellPicking {
  readonly onPick: (node: LocationNode) => void;
  readonly onDrill: (node: LocationNode) => void;
  /** Already chosen, so the cell can say so. */
  readonly pickedId?: number | null | undefined;
  /** Containers that cannot be chosen — the bin being emptied. */
  readonly excludeIds?: readonly number[] | undefined;
  /** Verb on the cell's accessible name. "Choose" reads oddly for a move. */
  readonly actionLabel?: string | undefined;
}

interface ContainerLayoutBase {
  readonly index: TreeIndex;
  /** The container whose children are drawn. `null` draws the roots. */
  readonly parentId: number | null;
  readonly variant?: "full" | "mini" | undefined;
  /**
   * This container's drawn room — ADR 0009 — when the caller has fetched it.
   *
   * Only ever *used* by the `floor_plan` branch, and passing it is optional
   * because the map view draws many levels in one render and one plan fetch per
   * level would be N requests for a picture nobody has zoomed into yet. A level
   * with no plan handed to it draws the flow of cards it always did, which is
   * honest rather than empty: no plan means nobody has drawn one.
   */
  readonly plan?: RoomPlanRead | null | undefined;
  /** How many levels of nested preview are still allowed. */
  readonly previewDepth?: number | undefined;
}

/**
 * Exactly one of the two behaviours, enforced at every call site.
 *
 * A union rather than two optional props, so there is no fourth state where a
 * cell has neither a destination nor a callback and silently becomes furniture
 * you cannot press.
 */
export type ContainerLayoutProps = ContainerLayoutBase &
  (
    | {
        /** Where a cell with children of its own links to. */
        readonly drillTo: (node: LocationNode) => string;
        readonly pick?: undefined;
      }
    | { readonly pick: CellPicking; readonly drillTo?: undefined }
  );

/**
 * The behaviour half of the props, as one object the internals pass around.
 *
 * The public type guarantees one of the two is present; below this line both are
 * optional and every branch asks which it has.
 */
function behaviourOf(props: ContainerLayoutProps): {
  drillTo?: ((node: LocationNode) => string) | undefined;
  pick?: CellPicking | undefined;
} {
  return props.pick === undefined ? { drillTo: props.drillTo } : { pick: props.pick };
}

export function ContainerLayout(props: ContainerLayoutProps) {
  const { index, parentId, variant = "full" } = props;
  const children = childrenOf(index, parentId);
  // The one place a view is decided, for every level and every depth — including
  // the roots, which reach it through the same call with `parentId === null`.
  const view = childViewOf(index, parentId);
  // The one piece of *authored* geometry available here, and the only reason a
  // level whose slot labels are a plain sequence can be drawn as the cabinet face
  // the server derived for it. Memoised so it is a stable dependency below.
  const canvas = useMemo(() => childCanvasOf(index, parentId), [index, parentId]);
  const layout = useMemo(
    () => inferLayout(children, (node) => node.slot_label, canvas),
    [children, canvas],
  );

  if (variant === "mini") {
    return (
      <MiniLayout
        layout={layout}
        index={index}
        nodes={children}
        view={view}
        {...behaviourOf(props)}
        previewDepth={props.previewDepth ?? 0}
      />
    );
  }
  return <FullLayout {...props} layout={layout} nodes={children} view={view} />;
}

function FullLayout({
  index,
  layout,
  nodes,
  view,
  drillTo,
  pick,
  plan = null,
  previewDepth = DEFAULT_PREVIEW_DEPTH,
}: ContainerLayoutProps & {
  readonly layout: Layout<LocationNode>;
  readonly nodes: readonly LocationNode[];
  readonly view: ChildView;
}) {
  const cell = (node: LocationNode, slotLabel?: string) => (
    <Cell
      key={node.id}
      node={node}
      index={index}
      previewDepth={previewDepth}
      view={view}
      {...(drillTo === undefined ? {} : { drillTo })}
      {...(pick === undefined ? {} : { pick })}
      {...(slotLabel === undefined ? {} : { slotLabel })}
    />
  );

  // A room somebody has actually drawn — walls, and a coordinate per container
  // (ADR 0009). This is the one branch that draws *authored* positions rather than
  // positions read back out of a label, and it comes first because a drawn plan is
  // the strongest thing this renderer can say about where furniture is. An
  // undrawn one falls through to the flow below, which is the honest picture of
  // "nobody has said where these are".
  if (view === "floor_plan" && plan != null && (plan.shapes.length > 0 || plan.placements.length > 0)) {
    return (
      <div className="stack" data-view={view} data-drawn="plan">
        <FloorPlan
          plan={plan}
          nodes={nodes}
          {...(drillTo === undefined
            ? {}
            : { hrefOf: (node: LocationNode) => cellHref(index, node, drillTo) })}
          {...(pick === undefined
            ? {}
            : {
                onSelect: pick.onPick,
                pickedId: pick.pickedId ?? null,
                excludeIds: pick.excludeIds ?? NO_EXCLUSIONS,
                actionLabel: pick.actionLabel ?? "Choose",
              })}
          /* The unplaced tray, and the only way to reach a container nobody has
             put on the plan — so these are the ordinary pressable cells, exactly
             as they are in the flow view. */
          renderCard={(node) => cell(node)}
        />
      </div>
    );
  }

  if (nodes.length === 0) {
    return (
      <p className="dim">
        Nothing is recorded inside this container. Put something away and it appears here.
      </p>
    );
  }

  /**
   * The children a positional view could not place, drawn anyway.
   *
   * **Every branch that lays children out by position owes this one.** In this
   * renderer the cell *is* the link, so a child that is not drawn is not merely
   * invisible — it is unreachable from the map, silently. Shared between the two
   * branches below rather than written twice because writing it twice is exactly
   * how the shelf run came to be missing it.
   */
  const unplaced =
    layout.unplaced.length > 0 ? (
      <div className="layout-fallback stack">
        <p className="muted-note" style={{ margin: 0 }}>
          {layout.unplaced.length} container(s) here have a label that is not a grid position, so
          they are listed rather than placed.
        </p>
        <div className="layout-flow">{layout.unplaced.map((node) => cell(node))}</div>
      </div>
    ) : null;

  // A slotted view draws real positions, and draws the empty ones, because
  // "drawer B3 has nothing in it" is a fact about the furniture. It can only do
  // that when the labels say where things go; when they do not, the fallback
  // below says so rather than inventing a grid.
  if (isSlotted(view) && layout.kind === "grid") {
    return (
      <div className="stack" data-view={view}>
        <div className="layout-scroll">
          {/*
            `role="group"` rather than `role="grid"`: an ARIA grid owes a row
            structure and arrow-key navigation, and claiming one without them is
            worse for a screen reader than the plain group this actually is. The
            spatial information is carried by each cell's own label.
          */}
          <div
            className={`layout-grid view-${view}`}
            role="group"
            aria-label={`${layout.rows} by ${layout.cols} layout`}
            style={{
              gridTemplateColumns: `repeat(${layout.cols}, minmax(var(--cell-min), 1fr))`,
              gridTemplateRows: `repeat(${layout.rows}, auto)`,
            }}
          >
            {layout.cells.map((laidOut) => (
              <div
                key={`${laidOut.row}:${laidOut.col}`}
                style={{ gridRow: laidOut.row + 1, gridColumn: laidOut.col + 1, display: "flex" }}
              >
                {laidOut.node === null ? (
                  <EmptySlot label={laidOut.slotLabel} />
                ) : (
                  cell(laidOut.node, laidOut.slotLabel)
                )}
              </div>
            ))}
          </div>
        </div>
        <p className="muted-note">
          {VIEW_NOTES[view]} {layout.rows} × {layout.cols}, read from the slot labels — positions
          are inferred until the tree screen fetches the authored layout.
        </p>
        {unplaced}
      </div>
    );
  }

  // A shelving unit: rows are authored, columns are not — how many boxes stand on
  // a shelf is a fact about the boxes. So each level is one horizontal run that
  // scrolls, never a cell in a grid that would imply a fixed capacity per shelf.
  if (view === "shelf_run") {
    // `groupByRow` reads `layout.cells`, which holds only what a row/column label
    // placed — so the boxes it could not place reach the screen through
    // `unplaced` below, exactly as they do for a cabinet face. Dropping them was
    // the whole defect: three of four boxes drawn, the fourth unreachable.
    const levels = layout.kind === "grid" ? groupByRow(layout) : [nodes];
    return (
      <div className="stack" data-view={view}>
        {levels.map((level, rowIndex) => (
          <div className="layout-shelf" key={rowIndex}>
            <span className="shelf-index mono" aria-hidden="true">
              {rowIndex + 1}
            </span>
            <div className="layout-run" role="group" aria-label={`shelf ${rowIndex + 1}`}>
              {level.map((node) => cell(node))}
            </div>
          </div>
        ))}
        <p className="muted-note">{VIEW_NOTES[view]}</p>
        {unplaced}
      </div>
    );
  }

  // Floor plan and list are both "placed, not slotted", and neither draws an
  // empty position, because neither has one. They differ only in shape, so they
  // share this branch and part on a class.
  if (view === "floor_plan" || view === "list") {
    return (
      <div className="stack" data-view={view}>
        <div className={view === "list" ? "layout-rows" : "layout-plan"}>
          {nodes.map((node) => cell(node))}
        </div>
        <p className="muted-note">{VIEW_NOTES[view]}</p>
      </div>
    );
  }

  // A slotted view whose labels do not say where anything goes. *Framed* as a
  // fallback, because unlike the two views above this one really did want a grid
  // and could not draw one.
  return (
    <div className="stack" data-view={view}>
      <div className="layout-fallback">
        <div className="layout-flow">
          {layout.cells.map((laidOut) =>
            laidOut.node === null ? null : cell(laidOut.node, laidOut.slotLabel),
          )}
        </div>
      </div>
      <p className="muted-note">
        {layout.kind === "sequence"
          ? "Numbered slots: an order, but no rows and columns to place them in."
          : FALLBACK_NOTE[layout.reason ?? "unlabelled"]}
      </p>
    </div>
  );
}

function miniColumns(view: ChildView, layout: Layout<LocationNode>, count: number): number {
  if (isSlotted(view) && layout.kind === "grid") {
    return layout.cols;
  }
  if (view === "shelf_run" || view === "list") {
    return 1;
  }
  return Math.min(count, 6);
}

/**
 * What a preview draws, in the order the full renderer would draw it — `null`
 * for a position that is genuinely empty.
 *
 * This exists because `layout.cells` is the wrong answer for four of the five
 * views. It holds the *empty* positions of an inferred grid, and `isSlotted` is
 * documented as the one behavioural consequence of the whole view axis: only a
 * cabinet face and a grid of cells draw a position that is empty, because "a room
 * has no position to be empty, so inventing one there would be inventing
 * furniture". A preview built from `cells` regardless drew a workshop of two sheds
 * as four dots, two of them blank — the map contradicting its own full drawing of
 * the same level one click away.
 *
 * So each branch here mirrors the branch of `FullLayout` that would draw this
 * level, including the unplaced children a slotted grid lists separately.
 */
function previewNodes(
  view: ChildView,
  layout: Layout<LocationNode>,
  nodes: readonly LocationNode[],
): readonly (LocationNode | null)[] {
  if (isSlotted(view)) {
    return layout.kind === "grid"
      ? [...layout.cells.map((laidOut) => laidOut.node), ...layout.unplaced]
      : // The fallback flow: real children only, never a gap.
        layout.cells.map((laidOut) => laidOut.node);
  }
  // A floor plan, a shelf run and a list all draw the children themselves, in
  // the order the API returned them, and none of them has an empty position.
  return nodes;
}

/** A grid layout's cells split into rows, in column order, gaps dropped. */
function groupByRow(layout: Layout<LocationNode>): LocationNode[][] {
  const rows: LocationNode[][] = Array.from({ length: layout.rows }, () => []);
  for (const laidOut of layout.cells) {
    if (laidOut.node !== null) {
      rows[laidOut.row]?.push(laidOut.node);
    }
  }
  return rows;
}

/**
 * Where a cell or a plan box links to: one level down in the map if there is
 * anything inside, else straight to the container's own screen, which is where
 * taking and returning happens. Shared so a drawn box and a flowed card cannot
 * disagree about where the same container lives.
 */
function cellHref(
  index: TreeIndex,
  node: LocationNode,
  drillTo: (node: LocationNode) => string,
): string {
  return childrenOf(index, node.id).length > 0 ? drillTo(node) : `/locations/${node.id}`;
}

function Cell({
  node,
  index,
  drillTo,
  pick,
  previewDepth,
  view,
  slotLabel,
}: {
  node: LocationNode;
  index: TreeIndex;
  drillTo?: ((node: LocationNode) => string) | undefined;
  pick?: CellPicking | undefined;
  previewDepth: number;
  view: ChildView;
  slotLabel?: string | undefined;
}) {
  const inside = childrenOf(index, node.id);
  const label = slotLabel ?? node.slot_label ?? "";
  const excluded = (pick?.excludeIds ?? NO_EXCLUSIONS).includes(node.id);
  const chosen = pick !== undefined && pick.pickedId === node.id;

  const classes = ["cell", `cell-${view}`];
  if (node.is_overfull) {
    classes.push("cell-over");
  }
  if (node.is_staging) {
    classes.push("cell-staging");
  }
  if (pick !== undefined) {
    classes.push("cell-pickable");
  }
  if (chosen) {
    classes.push("cell-current");
  }

  const describe =
    `${node.name}${label === "" ? "" : `, slot ${label}`}, ` +
    `${node.lot_count} lot(s)` +
    (node.is_overfull ? ", over capacity" : "") +
    (node.is_staging ? ", staging inbox" : "") +
    (inside.length > 0 ? `, ${inside.length} container(s) inside` : "");

  const body = (
    <>
      <span className="row-tight">
        {/* The cheap picture — see `ContainerPhoto`'s docstring for why this is
            always the glyph and never a photo here: a slotted view can lay out
            dozens of these in one screen, and loading a real image per cell to
            draw something a few dozen pixels wide is the exact waste the
            glyph/photo split exists to avoid. */}
        <ContainerPhoto glyph={node.effective_glyph} alt="" />
        {label !== "" && <span className="cell-slot mono">{label}</span>}
        <span className="spacer" />
        {node.is_staging && <span className="badge badge-accent">inbox</span>}
        {/* A warning, not an error: an over-capacity put-away was accepted on
            purpose. The badge carries a "!" glyph and a 2px border too. */}
        {node.is_overfull && <span className="badge badge-warn">over</span>}
      </span>
      <span className="cell-name">{node.name}</span>
      <FillMeter ratio={node.fill_ratio} overfull={node.is_overfull} />
      <span className="cell-sub">
        {node.lot_count === 0 ? "no lots" : `${node.lot_count} lot(s) · ${formatQty(node.qty_milli)}`}
      </span>
      {inside.length > 0 && (
        <>
          <span className="cell-sub">{inside.length} inside →</span>
          {previewDepth > 0 && inside.length <= MAX_PREVIEW_CELLS && (
            <ContainerLayout
              index={index}
              parentId={node.id}
              variant="mini"
              previewDepth={previewDepth - 1}
              {...(pick === undefined
                ? { drillTo: drillTo as (node: LocationNode) => string }
                : { pick })}
            />
          )}
        </>
      )}
    </>
  );

  // Choosing a destination. The cell body picks; a cell with children of its own
  // also gets a small control to look inside, since a picker wants both and one
  // press cannot mean two things.
  if (pick !== undefined) {
    return (
      <span className="cell-pick">
        <button
          type="button"
          className={classes.join(" ")}
          disabled={excluded}
          aria-pressed={chosen}
          aria-label={
            excluded
              ? `${node.name}: where the stock is now`
              : `${chosen ? "Chosen" : (pick.actionLabel ?? "Choose")}: ${describe}`
          }
          onClick={() => pick.onPick(node)}
        >
          {body}
          {excluded && <span className="cell-sub">where it is now</span>}
          {chosen && <span className="cell-sub">chosen</span>}
        </button>
        {inside.length > 0 && (
          <button
            type="button"
            className="cell-open"
            aria-label={`Look inside ${node.name}`}
            onClick={() => pick.onDrill(node)}
          >
            <span aria-hidden="true">&rsaquo;</span>
          </button>
        )}
      </span>
    );
  }

  return (
    <Link
      className={classes.join(" ")}
      to={cellHref(index, node, drillTo as (node: LocationNode) => string)}
      aria-label={describe}
    >
      {body}
    </Link>
  );
}

function EmptySlot({ label }: { label: string }) {
  return (
    <span
      className="cell cell-empty"
      title="No container is recorded at this position."
      aria-label={`slot ${label}, no container`}
    >
      <span className="cell-slot mono">{label}</span>
      <span className="cell-sub">no bin</span>
    </span>
  );
}

/**
 * The same layout one level down, as inert dots — so a baseplate reads as a
 * baseplate at a glance, and a room of cabinets does not read as a baseplate.
 *
 * **The view kind reaches this too**, which is what makes the recursion whole
 * rather than "the top level is drawn properly and everything below it is dots":
 * the arrangement of the dots comes from the same `childViewOf` call, and a cell
 * whose own children are worth previewing gets one nested inside it. It reaches
 * *what* is drawn as well as how many columns it is drawn in — see
 * `previewNodes`, which is the half that used to be missing.
 *
 * Decorative: the cell it sits in already carries the count in text, so this is
 * `aria-hidden`, and the `data-view` attribute is what a test reads instead.
 */
function MiniLayout({
  layout,
  index,
  nodes,
  view,
  drillTo,
  pick,
  previewDepth,
}: {
  layout: Layout<LocationNode>;
  index: TreeIndex;
  nodes: readonly LocationNode[];
  view: ChildView;
  drillTo?: ((node: LocationNode) => string) | undefined;
  pick?: CellPicking | undefined;
  previewDepth: number;
}) {
  const cells = previewNodes(view, layout, nodes).slice(0, MAX_PREVIEW_CELLS);
  if (cells.length === 0) {
    return null;
  }
  // The same three shapes the full renderer draws, one order of magnitude down:
  // a slotted level keeps its real column count, a shelf and a list stack, and a
  // floor plan flows. Computed here rather than overridden in CSS so there is one
  // place the arrangement comes from.
  const columns = miniColumns(view, layout, cells.length);

  return (
    <span
      className={`mini-grid mini-${view}`}
      data-view={view}
      aria-hidden="true"
      style={{ gridTemplateColumns: `repeat(${columns}, 1fr)` }}
    >
      {cells.map((node, position) => {
        const classes = ["mini-cell"];
        const inside = node === null ? [] : childrenOf(index, node.id);
        if (node === null) {
          classes.push("blank");
        } else if (node.is_overfull) {
          classes.push("over");
        } else if (node.lot_count > 0 || inside.length > 0) {
          classes.push("has-stock");
        }
        const nest =
          node !== null &&
          previewDepth > 0 &&
          inside.length > 0 &&
          inside.length <= MAX_NESTED_PREVIEW_CELLS;
        if (nest) {
          // A dot with a picture inside it is no longer 6px tall.
          classes.push("nested");
        }
        return (
          <span
            key={node === null ? `empty-${position}` : node.id}
            className={classes.join(" ")}
          >
            {nest && node !== null && (
              <ContainerLayout
                index={index}
                parentId={node.id}
                variant="mini"
                previewDepth={previewDepth - 1}
                {...(pick === undefined
                  ? { drillTo: drillTo as (node: LocationNode) => string }
                  : { pick })}
              />
            )}
          </span>
        );
      })}
    </span>
  );
}
