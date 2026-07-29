/**
 * The canvas editor: select cells, merge them, split a merged one back
 * apart, relabel, resize the grid — for a container **type**'s reusable
 * template and for one **instance**'s own copy of it alike (docs/PLAN.md,
 * "Layout authoring"). The two differ only in the props the caller passes:
 * `labelMode` (a type's blank cell lets the server generate a label; an
 * instance has no generator to fall back on) and `contentById` (only an
 * instance has physical contents to show).
 *
 * **This component never talks to the API and never decides what a delete
 * means** — it only produces the next `DraftSlot[]` and hands it to
 * `onChange`. The guard, the three outcomes, and "what will this delete"
 * are the caller's job (`lib/locations/layoutDraft.previewChanges`), which
 * is what lets a type's screen skip the guard preview entirely rather than
 * this component silently assuming nothing can go wrong.
 *
 * Selection is click, and shift+click (or the "Extending" toggle, for a
 * touchscreen with no shift key) to grow it into a rectangle — the
 * long-press-drag and Shift+Arrow interactions docs/PLAN.md describes are a
 * follow-up, not faked here; a selection that can only ever be *correct* is
 * worth more than one that also feels fluent.
 */

import { useState } from "react";

import {
  addSlot,
  classifySelection,
  mergeSlots,
  rectFromCells,
  removeSlot,
  splitSlot,
  updateSlot,
  type Cell,
  type DraftSlot,
  type Rect,
} from "../lib/locations/layoutDraft";

export interface LayoutEditorContentInfo {
  readonly shortId: string | null;
  readonly hasTag: boolean;
  readonly lotCount: number;
  readonly qtyMilli: number;
}

const SIZE_CLASSES = ["tiny", "small", "medium", "large", "bulky"] as const;

export interface LayoutEditorProps {
  readonly slots: readonly DraftSlot[];
  readonly onChange: (next: readonly DraftSlot[]) => void;
  readonly rows: number;
  readonly cols: number;
  readonly onResize: (rows: number, cols: number) => void;
  /** "auto": a blank label is legal — the caller's save path fills one in
   * from the type's own generator. "required": every slot must carry an
   * explicit label before this can be saved at all. */
  readonly labelMode: "auto" | "required";
  /** What to type into a new or freshly split cell's label field before the
   * user overrides it — never sent as a silent default. */
  readonly suggestLabel: (rowIdx: number, colIdx: number) => string;
  /** Instance mode only: what each slot currently holds, keyed by its
   * `DraftSlot.id`. A slot minted by a merge or a split has no entry here —
   * correctly, since it has no recorded state of its own yet. */
  readonly contentById?: ReadonlyMap<string, LayoutEditorContentInfo> | undefined;
}

function edgeOccupied(slots: readonly Cell[], index: number, axis: "row" | "col"): boolean {
  return slots.some((slot) =>
    axis === "row"
      ? slot.rowIdx <= index && slot.rowIdx + slot.rowSpan - 1 >= index
      : slot.colIdx <= index && slot.colIdx + slot.colSpan - 1 >= index,
  );
}

export function LayoutEditor({
  slots,
  onChange,
  rows,
  cols,
  onResize,
  labelMode,
  suggestLabel,
  contentById,
}: LayoutEditorProps) {
  const [anchor, setAnchor] = useState<Cell | null>(null);
  const [focus, setFocus] = useState<Cell | null>(null);
  const [extending, setExtending] = useState(false);
  const [mergeLabel, setMergeLabel] = useState("");
  const [addLabel, setAddLabel] = useState("");

  const rect: Rect | null = anchor !== null && focus !== null ? rectFromCells(anchor, focus) : null;
  const selection = rect !== null ? classifySelection(slots, rect) : null;

  function clearSelection(): void {
    setAnchor(null);
    setFocus(null);
  }

  function selectCell(cell: Cell, event: { shiftKey: boolean }): void {
    if (anchor !== null && (extending || event.shiftKey)) {
      setFocus(cell);
      return;
    }
    setAnchor(cell);
    setFocus(cell);
  }

  function withinSelection(cell: Cell): boolean {
    if (rect === null) {
      return false;
    }
    return (
      cell.rowIdx >= rect.r0 &&
      cell.rowIdx + cell.rowSpan - 1 <= rect.r1 &&
      cell.colIdx >= rect.c0 &&
      cell.colIdx + cell.colSpan - 1 <= rect.c1
    );
  }

  // ------------------------------------------------------------- render ---

  const occupied = new Map<string, DraftSlot>();
  for (const slot of slots) {
    occupied.set(`${slot.rowIdx}:${slot.colIdx}`, slot);
  }
  const covered = new Set<string>();
  for (const slot of slots) {
    for (let r = slot.rowIdx; r < slot.rowIdx + slot.rowSpan; r += 1) {
      for (let c = slot.colIdx; c < slot.colIdx + slot.colSpan; c += 1) {
        covered.add(`${r}:${c}`);
      }
    }
  }

  const cells: { cell: Cell; slot: DraftSlot | null }[] = [];
  for (let r = 0; r < rows; r += 1) {
    for (let c = 0; c < cols; c += 1) {
      const key = `${r}:${c}`;
      const top = occupied.get(key);
      if (top !== undefined) {
        cells.push({ cell: top, slot: top });
      } else if (!covered.has(key)) {
        cells.push({ cell: { rowIdx: r, colIdx: c, rowSpan: 1, colSpan: 1 }, slot: null });
      }
    }
  }

  const lastRowRemovable = rows > 0 && !edgeOccupied(slots, rows - 1, "row");
  const lastColRemovable = cols > 0 && !edgeOccupied(slots, cols - 1, "col");

  return (
    <div className="stack">
      <div className="row">
        <button type="button" onClick={() => onResize(rows + 1, cols)}>
          + Row
        </button>
        <button type="button" onClick={() => onResize(rows, cols + 1)}>
          + Column
        </button>
        <button
          type="button"
          disabled={!lastRowRemovable}
          title={lastRowRemovable ? undefined : "The last row still has a slot in it"}
          onClick={() => onResize(Math.max(0, rows - 1), cols)}
        >
          − Row
        </button>
        <button
          type="button"
          disabled={!lastColRemovable}
          title={lastColRemovable ? undefined : "The last column still has a slot in it"}
          onClick={() => onResize(rows, Math.max(0, cols - 1))}
        >
          − Column
        </button>
        <span className="spacer" />
        <button
          type="button"
          className="toggle"
          aria-pressed={extending}
          onClick={() => setExtending(!extending)}
          title="Turn on before tapping a second cell to select a range on a touchscreen"
        >
          Extending
        </button>
        <button type="button" onClick={clearSelection} disabled={anchor === null}>
          Clear selection
        </button>
      </div>

      <div className="layout-scroll">
        <div
          className="layout-grid"
          role="group"
          aria-label={`${rows} by ${cols} canvas`}
          style={{
            gridTemplateColumns: `repeat(${cols}, minmax(var(--cell-min), 1fr))`,
            gridTemplateRows: `repeat(${rows}, auto)`,
          }}
        >
          {cells.map(({ cell, slot }) => {
            const content = slot === null ? undefined : contentById?.get(slot.id);
            const selected = withinSelection(cell);
            const classes = ["cell", "cell-editable"];
            if (slot === null) {
              classes.push("cell-empty");
            }
            if (selected) {
              classes.push("cell-current");
            }
            return (
              <button
                key={`${cell.rowIdx}:${cell.colIdx}`}
                type="button"
                className={classes.join(" ")}
                style={{
                  gridRow: `${cell.rowIdx + 1} / span ${cell.rowSpan}`,
                  gridColumn: `${cell.colIdx + 1} / span ${cell.colSpan}`,
                }}
                aria-pressed={selected}
                onClick={(event) => selectCell(cell, { shiftKey: event.shiftKey })}
              >
                {slot === null ? (
                  <span className="cell-sub">empty</span>
                ) : (
                  <>
                    <span className="cell-slot mono">{slot.slotLabel || "(auto)"}</span>
                    <span className="cell-name">
                      {slot.rowSpan * slot.colSpan > 1 && `${slot.rowSpan}×${slot.colSpan}`}
                    </span>
                    {slot.sizeClass !== null && <span className="cell-sub">{slot.sizeClass}</span>}
                    {content !== undefined && (
                      <span className="cell-sub">
                        {content.lotCount > 0 && (
                          <span className="badge badge-warn">{content.lotCount} lot(s)</span>
                        )}
                        {content.hasTag && <span className="badge badge-accent">tag</span>}
                      </span>
                    )}
                  </>
                )}
              </button>
            );
          })}
        </div>
      </div>

      {selection?.kind === "single" && (
        <SingleSlotInspector
          slot={selection.slot}
          labelMode={labelMode}
          content={contentById?.get(selection.slot.id)}
          onEdit={(patch) => onChange(updateSlot(slots, selection.slot.id, patch))}
          onSplit={
            selection.slot.rowSpan * selection.slot.colSpan > 1
              ? () => {
                  onChange(splitSlot(slots, selection.slot.id, suggestLabel));
                  clearSelection();
                }
              : undefined
          }
          onRemove={() => {
            onChange(removeSlot(slots, selection.slot.id));
            clearSelection();
          }}
        />
      )}

      {selection?.kind === "mergeable" && (
        <div className="card">
          <h3>Merge {selection.slots.length} cells</h3>
          <label className="field">
            <span>Label for the merged slot</span>
            <input
              value={mergeLabel}
              onChange={(event) => setMergeLabel(event.target.value)}
              placeholder={
                labelMode === "auto"
                  ? "leave blank to auto-generate"
                  : suggestLabel(selection.rect.r0, selection.rect.c0)
              }
            />
          </label>
          <button
            type="button"
            className="primary"
            onClick={() => {
              const label =
                mergeLabel.trim() !== ""
                  ? mergeLabel.trim()
                  : labelMode === "auto"
                    ? ""
                    : suggestLabel(selection.rect.r0, selection.rect.c0);
              onChange(mergeSlots(slots, selection.slots, selection.rect, label));
              setMergeLabel("");
              clearSelection();
            }}
          >
            Merge
          </button>
        </div>
      )}

      {selection?.kind === "empty" && (
        <div className="card">
          <h3>Add a slot here</h3>
          <label className="field">
            <span>Label</span>
            <input
              value={addLabel}
              onChange={(event) => setAddLabel(event.target.value)}
              placeholder={suggestLabel(selection.rect.r0, selection.rect.c0)}
            />
          </label>
          <button
            type="button"
            className="primary"
            onClick={() => {
              const label =
                addLabel.trim() !== ""
                  ? addLabel.trim()
                  : suggestLabel(selection.rect.r0, selection.rect.c0);
              onChange(addSlot(slots, selection.rect, label));
              setAddLabel("");
              clearSelection();
            }}
          >
            Add slot
          </button>
        </div>
      )}

      {selection?.kind === "partial" && (
        <p className="muted-note">
          That selection cuts across an existing slot instead of covering it exactly. Only
          contiguous, non-overlapping rectangles can be merged, added or removed together —
          adjust the selection so its edges line up with the slots already there.
        </p>
      )}
    </div>
  );
}

function SingleSlotInspector({
  slot,
  labelMode,
  content,
  onEdit,
  onSplit,
  onRemove,
}: {
  slot: DraftSlot;
  labelMode: "auto" | "required";
  content: LayoutEditorContentInfo | undefined;
  onEdit: (patch: Partial<Pick<DraftSlot, "slotLabel" | "sizeClass" | "innerVolumeMm3">>) => void;
  onSplit: (() => void) | undefined;
  onRemove: () => void;
}) {
  return (
    <div className="card">
      <div className="row">
        <h3 style={{ flex: 1 }}>Slot {slot.slotLabel || "(auto)"}</h3>
        {content !== undefined && content.lotCount > 0 && (
          <span className="badge badge-warn">{content.lotCount} lot(s) here</span>
        )}
        {content?.hasTag === true && <span className="badge badge-accent">tag bound</span>}
      </div>
      <label className="field">
        <span>Label{labelMode === "required" && " (required)"}</span>
        <input
          value={slot.slotLabel}
          onChange={(event) => onEdit({ slotLabel: event.target.value })}
          placeholder={labelMode === "auto" ? "blank = auto-generated" : undefined}
        />
      </label>
      <label className="field">
        <span>Size class</span>
        <select
          value={slot.sizeClass ?? ""}
          onChange={(event) => onEdit({ sizeClass: event.target.value === "" ? null : event.target.value })}
        >
          <option value="">not set</option>
          {SIZE_CLASSES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Inner volume (mm³)</span>
        <input
          type="number"
          min={0}
          value={slot.innerVolumeMm3 ?? ""}
          onChange={(event) =>
            onEdit({ innerVolumeMm3: event.target.value === "" ? null : Number(event.target.value) })
          }
        />
      </label>
      <div className="row">
        {onSplit !== undefined && (
          <button type="button" onClick={onSplit}>
            Split into {slot.rowSpan * slot.colSpan} cells
          </button>
        )}
        <span className="spacer" />
        <button type="button" className="danger" onClick={onRemove}>
          Remove this slot
        </button>
      </div>
      {content !== undefined && (content.lotCount > 0 || content.hasTag) && (
        <p className="muted-note" style={{ margin: 0 }}>
          Removing this slot will be blocked until{" "}
          {content.lotCount > 0 && `its ${content.lotCount} lot(s)`}
          {content.lotCount > 0 && content.hasTag && " and "}
          {content.hasTag && "its bound tag"} move or are cleared.
        </p>
      )}
    </div>
  );
}
