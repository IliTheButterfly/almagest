/**
 * A container drawn as the thing it is: a cabinet of drawers, a baseplate of bins.
 *
 * One component, called on itself. The containment chain in this system is
 * `room → cabinet → drawer → baseplate → bin → divider`, every arrow of it "a
 * thing presenting a grid" holding "a thing occupying part of that grid", with no
 * depth limit and no named levels (ADR 0002). A renderer with a special case per
 * level would contradict the schema, so this one renders *any* container's
 * children and drills into any of them — and calls itself once more, in `mini`
 * form, to show what is inside a cell without leaving the screen.
 *
 * ---------------------------------------------------------------------------
 * KNOWN LIMITATION — the geometry here is inferred, not authored.
 *
 * `locations.row_idx`/`col_idx` and `container_types.grid_rows`/`grid_cols` all
 * exist in the database, and **none of them are exposed by the merged API**:
 * `LocationNode` carries `slot_label` and nothing else positional. So the layout
 * is read back out of the label text by `lib/locations/slots.ts`.
 *
 * `GET /api/locations/{id}/layout` is being built on another branch and will
 * supersede that inference. When it lands, the swap is: fetch the layout, hand
 * the resulting `Layout` to this component, delete the `inferLayout` call. This
 * component takes a `Layout` as data and asks no questions about where it came
 * from, and `slots.ts` is the only module that knows about label parsing at all.
 * ---------------------------------------------------------------------------
 */

import { useMemo } from "react";
import { Link } from "react-router-dom";

import type { LocationNode } from "../lib/api/client";
import { formatQty } from "../lib/format";
import { inferLayout, type FallbackReason, type Layout } from "../lib/locations/slots";
import { childrenOf, type TreeIndex } from "../lib/locations/tree";
import { FillMeter } from "./FillMeter";

/** Beyond this many children, a nested preview is noise rather than a picture. */
const MAX_PREVIEW_CELLS = 36;

const FALLBACK_NOTE: Readonly<Record<FallbackReason, string>> = {
  unlabelled:
    "No slot labels here, so there is no layout to draw — listed in the order the " +
    "containers were created.",
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

export interface ContainerLayoutProps {
  readonly index: TreeIndex;
  /** The container whose children are drawn. `null` draws the roots. */
  readonly parentId: number | null;
  /** Where a cell with children of its own links to. */
  readonly drillTo: (node: LocationNode) => string;
  readonly variant?: "full" | "mini" | undefined;
  /** How many levels of nested preview are still allowed. */
  readonly previewDepth?: number | undefined;
}

export function ContainerLayout(props: ContainerLayoutProps) {
  const { index, parentId, variant = "full" } = props;
  const children = childrenOf(index, parentId);
  const layout = useMemo(() => inferLayout(children, (node) => node.slot_label), [children]);

  if (variant === "mini") {
    return <MiniLayout layout={layout} index={index} />;
  }
  return <FullLayout {...props} layout={layout} nodes={children} />;
}

function FullLayout({
  index,
  layout,
  nodes,
  drillTo,
  previewDepth = 1,
}: ContainerLayoutProps & {
  readonly layout: Layout<LocationNode>;
  readonly nodes: readonly LocationNode[];
}) {
  if (nodes.length === 0) {
    return (
      <p className="dim">
        Nothing is recorded inside this container. Put something away and it appears here.
      </p>
    );
  }

  const cell = (node: LocationNode) => (
    <Cell key={node.id} node={node} index={index} drillTo={drillTo} previewDepth={previewDepth} />
  );

  if (layout.kind === "grid") {
    return (
      <div className="stack">
        <div className="layout-scroll">
          {/*
            `role="group"` rather than `role="grid"`: an ARIA grid owes a row
            structure and arrow-key navigation, and claiming one without them is
            worse for a screen reader than the plain group this actually is. The
            spatial information is carried by each cell's own label.
          */}
          <div
            className="layout-grid"
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
                  <Cell
                    node={laidOut.node}
                    index={index}
                    drillTo={drillTo}
                    previewDepth={previewDepth}
                    slotLabel={laidOut.slotLabel}
                  />
                )}
              </div>
            ))}
          </div>
        </div>
        <p className="muted-note">
          {layout.rows} × {layout.cols}, read from the slot labels. Positions are inferred until
          the layout endpoint lands.
        </p>
        {layout.unplaced.length > 0 && (
          <div className="layout-fallback stack">
            <p className="muted-note" style={{ margin: 0 }}>
              {layout.unplaced.length} container(s) here have a label that is not a grid position,
              so they are listed rather than placed.
            </p>
            <div className="layout-flow">{layout.unplaced.map(cell)}</div>
          </div>
        )}
      </div>
    );
  }

  // Sequence and flow both fall back to a wrapping list. Only the genuinely
  // ambiguous cases are *framed* as a fallback: an unlabelled room of cabinets
  // is not a broken grid, and dressing it up as one would cry wolf.
  const framed = layout.reason !== null && layout.reason !== "unlabelled";
  return (
    <div className="stack">
      <div className={framed ? "layout-fallback" : undefined}>
        <div className="layout-flow">
          {layout.cells.map((laidOut) =>
            laidOut.node === null ? null : (
              <Cell
                key={laidOut.node.id}
                node={laidOut.node}
                index={index}
                drillTo={drillTo}
                previewDepth={previewDepth}
                slotLabel={laidOut.slotLabel}
              />
            ),
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

function Cell({
  node,
  index,
  drillTo,
  previewDepth,
  slotLabel,
}: {
  node: LocationNode;
  index: TreeIndex;
  drillTo: (node: LocationNode) => string;
  previewDepth: number;
  slotLabel?: string | undefined;
}) {
  const inside = childrenOf(index, node.id);
  // A container with children drills one level down in the map; a leaf goes
  // straight to its own screen, which is where taking and returning happens.
  const href = inside.length > 0 ? drillTo(node) : `/locations/${node.id}`;
  const label = slotLabel ?? node.slot_label ?? "";

  const classes = ["cell"];
  if (node.is_overfull) {
    classes.push("cell-over");
  }
  if (node.is_staging) {
    classes.push("cell-staging");
  }

  return (
    <Link
      className={classes.join(" ")}
      to={href}
      aria-label={
        `${node.name}${label === "" ? "" : `, slot ${label}`}, ` +
        `${node.lot_count} lot(s)` +
        (node.is_overfull ? ", over capacity" : "") +
        (node.is_staging ? ", staging inbox" : "") +
        (inside.length > 0 ? `, ${inside.length} container(s) inside` : "")
      }
    >
      <span className="row-tight">
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
              drillTo={drillTo}
              variant="mini"
              previewDepth={previewDepth - 1}
            />
          )}
        </>
      )}
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
 * baseplate at a glance. Decorative: the cell it sits in already carries the
 * count in text, and this is `aria-hidden` because dots are not information a
 * screen reader can use.
 */
function MiniLayout({
  layout,
  index,
}: {
  layout: Layout<LocationNode>;
  index: TreeIndex;
}) {
  const cells = layout.cells.slice(0, MAX_PREVIEW_CELLS);
  if (cells.length === 0) {
    return null;
  }
  const columns = layout.kind === "grid" ? layout.cols : Math.min(cells.length, 6);

  return (
    <span
      className="mini-grid"
      aria-hidden="true"
      style={{ gridTemplateColumns: `repeat(${columns}, 1fr)` }}
    >
      {cells.map((laidOut) => {
        const node = laidOut.node;
        const classes = ["mini-cell"];
        if (node === null) {
          classes.push("blank");
        } else if (node.is_overfull) {
          classes.push("over");
        } else if (node.lot_count > 0 || childrenOf(index, node.id).length > 0) {
          classes.push("has-stock");
        }
        return <span key={`${laidOut.row}:${laidOut.col}`} className={classes.join(" ")} />;
      })}
    </span>
  );
}
