/**
 * Pure logic behind the layout editor: turning a click-and-shift-click
 * selection into a merge, a split, a delete or a new slot, and previewing
 * what saving the result would actually do before it is sent anywhere.
 *
 * Deliberately free of React and of the API client. `app/services
 * /layout_authoring.py` decides these questions once already —
 * "does this selection tile its bounding rectangle exactly", "does it cut
 * across an existing slot", "which slot does this label already belong
 * to" — and this module answers the same questions the same way, so what
 * the editor shows *before* a save matches what the guard decides *during*
 * one. A client that guessed differently would either nag about changes the
 * server would accept, or promise a save that a 409 then reverses.
 */

import type { SlotSpecIn } from "../api/client";
import { uuid4 } from "../scan/session";

/** The rectangle a slot occupies, and nothing else — the minimum a merge,
 * split or overlap check needs. Every `DraftSlot` satisfies it. */
export interface Cell {
  readonly rowIdx: number;
  readonly colIdx: number;
  readonly rowSpan: number;
  readonly colSpan: number;
}

/** One compartment as the editor is currently proposing it. */
export interface DraftSlot extends Cell {
  /** Stable across edits so React and the diff-preview can both track "the
   * same slot", even after its region or label changes. Never sent to the
   * server — the wire shape (`SlotSpecIn`) has no such field. */
  readonly id: string;
  readonly slotLabel: string;
  readonly sizeClass: string | null;
  readonly innerVolumeMm3: number | null;
}

/**
 * A slot as it existed before this editing session started, plus whatever
 * it physically holds. `locationId` is `null` for a container **type**'s
 * canvas — a type has no children of its own to hold anything, which is
 * exactly why editing one never needs the change guard at all. An
 * **instance**'s slots always carry a real `locationId`.
 */
export interface OriginalSlot extends DraftSlot {
  readonly locationId: number | null;
  readonly shortId: string | null;
  /** When a card was last actually printed for this slot, not when a code was
   *  minted for it. With `tag_granularity="slot"` every slot has a `short_id`
   *  from instantiation, so the two are very different questions. */
  readonly lastPrintedAt: string | null;
  readonly hasTag: boolean;
  readonly lotCount: number;
  readonly qtyMilli: number;
}

export function draftOf(original: OriginalSlot): DraftSlot {
  return {
    id: original.id,
    rowIdx: original.rowIdx,
    colIdx: original.colIdx,
    rowSpan: original.rowSpan,
    colSpan: original.colSpan,
    slotLabel: original.slotLabel,
    sizeClass: original.sizeClass,
    innerVolumeMm3: original.innerVolumeMm3,
  };
}

/** The wire shape `PUT .../slot-template` and `POST .../reapply-layout`
 * both take: the complete desired layout, never a delta. An empty
 * `slotLabel` is sent as `undefined` rather than `""` — a type's canvas
 * reads that as "generate this one"; an instance's reapply rejects it
 * outright, which is a 422 the caller should never actually reach once
 * `requireLabels` (below) has been honoured client-side. */
export function toSlotSpecIn(slot: DraftSlot): SlotSpecIn {
  return {
    row_idx: slot.rowIdx,
    col_idx: slot.colIdx,
    row_span: slot.rowSpan,
    col_span: slot.colSpan,
    // Omitted entirely rather than set to `undefined` when blank — with
    // `exactOptionalPropertyTypes` those are not the same thing, and only
    // omitting it reads as "not specified" to the server's `str | None`.
    ...(slot.slotLabel === "" ? {} : { slot_label: slot.slotLabel }),
    size_class:
      slot.sizeClass === null
        ? null
        : (slot.sizeClass as Exclude<SlotSpecIn["size_class"], null | undefined>),
    inner_volume_mm3: slot.innerVolumeMm3,
  };
}

/** Every slot needs a non-empty label before an instance's reapply will
 * accept it — a type's canvas has no such requirement, since the server
 * fills a blank one in from the generator. */
export function requireLabels(slots: readonly DraftSlot[]): readonly DraftSlot[] {
  return slots.filter((slot) => slot.slotLabel.trim() === "");
}

// --------------------------------------------------------------- geometry ---

/** Inclusive on every edge, in the same 0-based row/col space as `row_idx`. */
export interface Rect {
  readonly r0: number;
  readonly c0: number;
  readonly r1: number;
  readonly c1: number;
}

export function rectFromCells(a: Cell, b: Cell): Rect {
  return {
    r0: Math.min(a.rowIdx, b.rowIdx),
    c0: Math.min(a.colIdx, b.colIdx),
    r1: Math.max(a.rowIdx + a.rowSpan - 1, b.rowIdx + b.rowSpan - 1),
    c1: Math.max(a.colIdx + a.colSpan - 1, b.colIdx + b.colSpan - 1),
  };
}

export function rectArea(rect: Rect): number {
  return (rect.r1 - rect.r0 + 1) * (rect.c1 - rect.c0 + 1);
}

function overlapsRect(cell: Cell, rect: Rect): boolean {
  return !(
    cell.colIdx + cell.colSpan - 1 < rect.c0 ||
    cell.colIdx > rect.c1 ||
    cell.rowIdx + cell.rowSpan - 1 < rect.r0 ||
    cell.rowIdx > rect.r1
  );
}

function fullyInsideRect(cell: Cell, rect: Rect): boolean {
  return (
    cell.rowIdx >= rect.r0 &&
    cell.rowIdx + cell.rowSpan - 1 <= rect.r1 &&
    cell.colIdx >= rect.c0 &&
    cell.colIdx + cell.colSpan - 1 <= rect.c1
  );
}

export function slotsTouching<T extends Cell>(slots: readonly T[], rect: Rect): T[] {
  return slots.filter((slot) => overlapsRect(slot, rect));
}

export function slotsFullyInside<T extends Cell>(slots: readonly T[], rect: Rect): T[] {
  return slots.filter((slot) => fullyInsideRect(slot, rect));
}

/**
 * `true` when `slots` tile `rect` with no gap — a plain area-sum check,
 * legitimate only because the slots are already known not to overlap *each
 * other* (every draft list this module produces keeps that invariant, and
 * `validate_no_overlaps` on the backend would refuse one that didn't).
 * Mirrors `merge_type_region`'s `gap_in_region` check.
 */
export function tilesExactly(slots: readonly Cell[], rect: Rect): boolean {
  if (slots.length === 0) {
    return false;
  }
  const area = slots.reduce((total, slot) => total + slot.rowSpan * slot.colSpan, 0);
  return area === rectArea(rect);
}

export function gridExtent(slots: readonly Cell[]): { rows: number; cols: number } {
  let rows = 0;
  let cols = 0;
  for (const slot of slots) {
    rows = Math.max(rows, slot.rowIdx + slot.rowSpan);
    cols = Math.max(cols, slot.colIdx + slot.colSpan);
  }
  return { rows, cols };
}

// -------------------------------------------------------------- selection ---

export type Selection =
  /** Touches nothing — legal target for "Add a slot here". */
  | { readonly kind: "empty"; readonly rect: Rect }
  /** Exactly one slot, exactly as big as the selection — the inspector, and
   * "Split" if it is itself a merged region. */
  | { readonly kind: "single"; readonly rect: Rect; readonly slot: DraftSlot }
  /** Two or more slots tiling the selection exactly — "Merge" is legal. */
  | { readonly kind: "mergeable"; readonly rect: Rect; readonly slots: readonly DraftSlot[] }
  /** Touches something but does not exactly cover it — cuts across an
   * existing slot, or mixes a slot with empty space. Nothing is legal here;
   * see `not_contiguous` on the backend for the case this mirrors. */
  | { readonly kind: "partial"; readonly rect: Rect };

export function classifySelection(slots: readonly DraftSlot[], rect: Rect): Selection {
  const touching = slotsTouching(slots, rect);
  if (touching.length === 0) {
    return { kind: "empty", rect };
  }
  const covering = slotsFullyInside(slots, rect);
  if (covering.length !== touching.length || !tilesExactly(covering, rect)) {
    return { kind: "partial", rect };
  }
  if (covering.length === 1) {
    return { kind: "single", rect, slot: covering[0]! };
  }
  return { kind: "mergeable", rect, slots: covering };
}

// -------------------------------------------------------------- mutations ---

export function mergeSlots(
  slots: readonly DraftSlot[],
  merging: readonly DraftSlot[],
  rect: Rect,
  label: string,
): DraftSlot[] {
  const ids = new Set(merging.map((slot) => slot.id));
  const template = merging[0];
  const merged: DraftSlot = {
    id: uuid4(),
    rowIdx: rect.r0,
    colIdx: rect.c0,
    rowSpan: rect.r1 - rect.r0 + 1,
    colSpan: rect.c1 - rect.c0 + 1,
    slotLabel: label,
    sizeClass: template?.sizeClass ?? null,
    innerVolumeMm3: template?.innerVolumeMm3 ?? null,
  };
  return [...slots.filter((slot) => !ids.has(slot.id)), merged];
}

/** Decompose a merged region back to its base cells. A no-op if `slotId`
 * names a slot that is not actually merged (`rowSpan * colSpan === 1`) —
 * callers should disable "Split" in that case, but this stays harmless if
 * one slips through. */
export function splitSlot(
  slots: readonly DraftSlot[],
  slotId: string,
  labelFor: (rowIdx: number, colIdx: number) => string,
): DraftSlot[] {
  const target = slots.find((slot) => slot.id === slotId);
  if (target === undefined || target.rowSpan * target.colSpan <= 1) {
    return slots.slice();
  }
  const created: DraftSlot[] = [];
  for (let r = target.rowIdx; r < target.rowIdx + target.rowSpan; r += 1) {
    for (let c = target.colIdx; c < target.colIdx + target.colSpan; c += 1) {
      created.push({
        id: uuid4(),
        rowIdx: r,
        colIdx: c,
        rowSpan: 1,
        colSpan: 1,
        slotLabel: labelFor(r, c),
        sizeClass: null,
        innerVolumeMm3: null,
      });
    }
  }
  return [...slots.filter((slot) => slot.id !== slotId), ...created];
}

export function removeSlot(slots: readonly DraftSlot[], slotId: string): DraftSlot[] {
  return slots.filter((slot) => slot.id !== slotId);
}

export function addSlot(slots: readonly DraftSlot[], rect: Rect, label: string): DraftSlot[] {
  return [
    ...slots,
    {
      id: uuid4(),
      rowIdx: rect.r0,
      colIdx: rect.c0,
      rowSpan: rect.r1 - rect.r0 + 1,
      colSpan: rect.c1 - rect.c0 + 1,
      slotLabel: label,
      sizeClass: null,
      innerVolumeMm3: null,
    },
  ];
}

export function updateSlot(
  slots: readonly DraftSlot[],
  slotId: string,
  patch: Partial<Pick<DraftSlot, "slotLabel" | "sizeClass" | "innerVolumeMm3">>,
): DraftSlot[] {
  return slots.map((slot) => (slot.id === slotId ? { ...slot, ...patch } : slot));
}

// ------------------------------------------------------------- the preview --

export interface ChangePreview {
  readonly creates: readonly DraftSlot[];
  /** Same region as before, different label and/or size class and/or
   * volume. Always safe regardless of what the slot holds. */
  readonly updates: readonly { readonly before: OriginalSlot; readonly after: DraftSlot }[];
  /** Slots whose region no longer appears in the draft at all — each one
   * is the guard's business: blocked if `hasTag` or `lotCount > 0`. */
  readonly deletes: readonly OriginalSlot[];
  /** A draft slot's label is already held by a *different* original slot.
   * Never applied — this is the "refused outright" case, checked ahead of
   * the region match so it wins even when the region side would otherwise
   * look like an ordinary safe rename of some other slot. */
  readonly reinterpreted: readonly { readonly before: OriginalSlot; readonly afterLabel: string }[];
}

function regionKey(cell: Cell): string {
  return `${cell.rowIdx}:${cell.colIdx}:${cell.rowSpan}:${cell.colSpan}`;
}

/**
 * Classify every draft slot against what currently exists — the same
 * priority `app.services.layout_authoring.diff_instance_layout` applies.
 */
export function previewChanges(
  original: readonly OriginalSlot[],
  draft: readonly DraftSlot[],
): ChangePreview {
  const byRegion = new Map<string, OriginalSlot>();
  const byLabel = new Map<string, OriginalSlot>();
  for (const slot of original) {
    byRegion.set(regionKey(slot), slot);
    if (slot.slotLabel !== "") {
      byLabel.set(slot.slotLabel, slot);
    }
  }

  const creates: DraftSlot[] = [];
  const updates: { before: OriginalSlot; after: DraftSlot }[] = [];
  const reinterpreted: { before: OriginalSlot; afterLabel: string }[] = [];
  const survived = new Set<string>();
  const reinterpretedIds = new Set<string>();

  for (const spec of draft) {
    const regionOwner = byRegion.get(regionKey(spec));
    const labelOwner = spec.slotLabel === "" ? undefined : byLabel.get(spec.slotLabel);

    if (labelOwner !== undefined && labelOwner !== regionOwner) {
      reinterpreted.push({ before: labelOwner, afterLabel: spec.slotLabel });
      reinterpretedIds.add(labelOwner.id);
      continue;
    }

    if (regionOwner !== undefined) {
      survived.add(regionOwner.id);
      if (
        regionOwner.slotLabel !== spec.slotLabel ||
        regionOwner.sizeClass !== spec.sizeClass ||
        regionOwner.innerVolumeMm3 !== spec.innerVolumeMm3
      ) {
        updates.push({ before: regionOwner, after: spec });
      }
      continue;
    }

    creates.push(spec);
  }

  const deletes = original.filter(
    (slot) => !survived.has(slot.id) && !reinterpretedIds.has(slot.id),
  );

  return { creates, updates, deletes, reinterpreted };
}
