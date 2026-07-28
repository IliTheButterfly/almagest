/**
 * Turning `slot_label` back into a row and a column.
 *
 * **This is inference, and it is temporary.** `locations` really does store
 * `row_idx`/`col_idx`, and `container_types` really does declare
 * `grid_rows`/`grid_cols` — but `LocationNode`, the only shape
 * `GET /api/locations/tree` returns, carries none of them. `GET
 * /api/locations/{id}/layout` is being built on another branch and will hand
 * over the authored geometry directly.
 *
 * So everything here reads the label and nothing else, and it is deliberately
 * isolated in one module with one entry point (`inferLayout`) for exactly that
 * reason: when the layout endpoint lands, the call site swaps the inferred
 * `Layout` for a fetched one and no component changes. The renderer already
 * treats a `Layout` as data.
 *
 * The default scheme is `row_alpha_col_num`, and the backend's own generator
 * (`services/assignment._next_grid_slot`) emits `f"{chr(ord('A') + row)}{col + 1}"`
 * — so `A1` is row 0, column 0. That is the form parsed here, plus the hand-typed
 * variants a human writes on a label maker: lower case, a separator, two letters
 * once a cabinet passes twenty-six rows.
 *
 * Where the labels do not say, this **says so** rather than guessing: a flow
 * layout with a reason, which the UI draws as an obvious fallback. A grid that
 * is silently wrong about where a drawer is would be worse than no grid, because
 * the whole point of the spatial view is that the screen matches the furniture.
 */

/** Zero-based, matching `locations.row_idx` / `col_idx`. */
export interface SlotPosition {
  readonly row: number;
  readonly col: number;
}

export type LayoutKind =
  /** Real row/column positions, recovered from every label that parsed. */
  | "grid"
  /** Plain `1`, `2`, `3` — an order but no geometry, so it flows. */
  | "sequence"
  /** No geometry recoverable. Drawn as a marked fallback. */
  | "flow";

export type FallbackReason =
  /** Nothing carries a slot label — a room full of cabinets, typically. */
  | "unlabelled"
  /** Labels exist and do not fit any scheme this knows. */
  | "unparsed"
  /** Two children resolved to the same cell, so the reading must be wrong. */
  | "collision"
  /** The implied grid is far larger than the children could fill. */
  | "implausible";

export interface LaidOutCell<T> {
  readonly row: number;
  readonly col: number;
  /** What to print. The node's own label where it has one. */
  readonly slotLabel: string;
  /** `null` for a position in the grid with no container in it. */
  readonly node: T | null;
  /** True when `slotLabel` was computed rather than read off the node. */
  readonly inferredLabel: boolean;
}

export interface Layout<T> {
  readonly kind: LayoutKind;
  /** Only meaningful for `kind === "grid"`; zero otherwise. */
  readonly rows: number;
  readonly cols: number;
  /** Row-major for a grid, including the empty positions. Source order
   * otherwise. */
  readonly cells: readonly LaidOutCell<T>[];
  /** Children a grid could not place — rendered separately and labelled. */
  readonly unplaced: readonly T[];
  readonly reason: FallbackReason | null;
}

const ALPHA_NUM = /^([A-Z]{1,2})[\s._-]?(\d{1,3})$/;
const NUMERIC = /^(\d{1,4})$/;

/**
 * `A1` → row 0, col 0. `null` when the label is not of that form.
 *
 * Letters are bijective base-26, so a 30-row cabinet's `AD7` lands on row 29 —
 * spreadsheet columns, which is what anybody labelling by hand will assume.
 */
export function parseSlotLabel(label: string | null | undefined): SlotPosition | null {
  if (label === null || label === undefined) {
    return null;
  }
  const match = ALPHA_NUM.exec(label.trim().toUpperCase());
  if (match === null) {
    return null;
  }
  const [, letters = "", digits = ""] = match;
  const column = Number.parseInt(digits, 10);
  // The generator emits 1-based columns, so `A0` is not a slot this scheme
  // produces. Refusing it keeps a typo out of column −1.
  if (column < 1) {
    return null;
  }
  let row = 0;
  for (const character of letters) {
    row = row * 26 + (character.charCodeAt(0) - 64);
  }
  return { row: row - 1, col: column - 1 };
}

/** The inverse, for labelling an empty position the grid implies. */
export function slotLabelFor({ row, col }: SlotPosition): string {
  let letters = "";
  let remaining = row + 1;
  while (remaining > 0) {
    const digit = (remaining - 1) % 26;
    letters = String.fromCharCode(65 + digit) + letters;
    remaining = Math.floor((remaining - 1) / 26);
  }
  return `${letters}${col + 1}`;
}

/** `7` → index 6, for the plain-numbered scheme. `null` otherwise. */
export function parseSequenceLabel(label: string | null | undefined): number | null {
  if (label === null || label === undefined) {
    return null;
  }
  const match = NUMERIC.exec(label.trim());
  if (match === null) {
    return null;
  }
  const index = Number.parseInt(match[1] ?? "", 10);
  return index >= 1 ? index - 1 : null;
}

/**
 * How big an implied grid may get before the reading is not believable.
 *
 * One stray `Z9` in a drawer of four bins implies 26×9 = 234 cells, 230 of them
 * empty. Rendering that is not a cabinet, it is a bug with a scrollbar.
 */
function plausible(rows: number, cols: number, placed: number): boolean {
  return rows * cols <= Math.max(64, placed * 6);
}

function flow<T>(children: readonly T[], reason: FallbackReason): Layout<T> {
  return {
    kind: "flow",
    rows: 0,
    cols: 0,
    cells: children.map((node, index) => ({
      row: index,
      col: 0,
      slotLabel: "",
      node,
      inferredLabel: false,
    })),
    unplaced: [],
    reason,
  };
}

/**
 * Lay children out spatially if their labels allow it, and say so if they don't.
 *
 * Children arrive in the order the API returned them, which for
 * `/api/locations/tree` is `id_path` order — creation order within a parent.
 * `locations.sort_order` exists in the schema but is **not** exposed on
 * `LocationNode`, so that is the closest thing to the authored order available
 * here, and it is what both fallback kinds preserve.
 */
export function inferLayout<T>(
  children: readonly T[],
  slotLabelOf: (child: T) => string | null,
): Layout<T> {
  if (children.length === 0) {
    return { kind: "flow", rows: 0, cols: 0, cells: [], unplaced: [], reason: "unlabelled" };
  }

  const labels = children.map(slotLabelOf);
  if (labels.every((label) => label === null || label.trim() === "")) {
    return flow(children, "unlabelled");
  }

  const placed = new Map<string, { node: T; position: SlotPosition; label: string }>();
  const unplaced: T[] = [];
  let collision = false;

  children.forEach((node, index) => {
    const label = labels[index] ?? null;
    const position = parseSlotLabel(label);
    if (position === null) {
      unplaced.push(node);
      return;
    }
    const key = `${position.row}:${position.col}`;
    if (placed.has(key)) {
      // `UNIQUE(parent_id, slot_label)` makes exact duplicates impossible, so
      // this means two *different* labels read as the same cell — "A1" and
      // "a-1", say. The parse is ambiguous, so it is not used at all.
      collision = true;
      return;
    }
    placed.set(key, { node, position, label: label ?? "" });
  });

  if (collision) {
    return flow(children, "collision");
  }

  if (placed.size === 0) {
    // Every label present, none of them a grid position. A run of plain numbers
    // is an order without a geometry; anything else is not understood at all.
    const sequence = children
      .map((node, index) => ({ node, index: parseSequenceLabel(labels[index] ?? null) }))
      .filter((entry): entry is { node: T; index: number } => entry.index !== null);

    if (sequence.length === children.length) {
      const ordered = [...sequence].sort((a, b) => a.index - b.index);
      return {
        kind: "sequence",
        rows: 0,
        cols: 0,
        cells: ordered.map((entry) => ({
          row: entry.index,
          col: 0,
          slotLabel: String(entry.index + 1),
          node: entry.node,
          inferredLabel: false,
        })),
        unplaced: [],
        reason: null,
      };
    }
    return flow(children, "unparsed");
  }

  const positions = [...placed.values()].map((entry) => entry.position);
  const rows = Math.max(...positions.map((position) => position.row)) + 1;
  const cols = Math.max(...positions.map((position) => position.col)) + 1;

  if (!plausible(rows, cols, placed.size)) {
    return flow(children, "implausible");
  }

  const cells: LaidOutCell<T>[] = [];
  for (let row = 0; row < rows; row += 1) {
    for (let col = 0; col < cols; col += 1) {
      const entry = placed.get(`${row}:${col}`);
      cells.push(
        entry === undefined
          ? {
              row,
              col,
              slotLabel: slotLabelFor({ row, col }),
              node: null,
              inferredLabel: true,
            }
          : { row, col, slotLabel: entry.label, node: entry.node, inferredLabel: false },
      );
    }
  }

  return { kind: "grid", rows, cols, cells, unplaced, reason: null };
}
