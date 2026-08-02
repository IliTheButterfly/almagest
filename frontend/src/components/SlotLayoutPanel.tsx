/**
 * One container's own slots, edited in place — the panel that used to be
 * `/locations/:id/layout`.
 *
 * It is a panel and not a page now, because "go to the layout editor page" was
 * exactly the step Iliana asked to lose: a drawer is added, a slot relabelled and
 * two cells merged on the container's own screen, in edit mode. What has *not*
 * changed is the thing the old screen's docstring was really defending, and it
 * was never the URL:
 *
 * - Editing a container **type**'s canvas still never touches a cabinet already
 *   built from it. Pushing the type's current layout into this one instance is a
 *   deliberate button here, it only replaces the draft, and it still has to be
 *   saved through the guard like any hand-drawn edit. A user who expected a
 *   preview still gets one.
 * - **The three outcomes stay three different things** (ADR 0002,
 *   `app.services.layout_authoring`): *safe* is a plain "Saved"; *guarded* (409,
 *   `slots_hold_content`) is an amber notice naming every blocked slot and what it
 *   holds, each linking to that container so its contents can be moved out;
 *   *refused* (422, usually `slot_identity_reinterpreted`) is red, and is caught
 *   client-side before Save is pressable wherever `previewChanges` can see it,
 *   because retrying can never make it succeed.
 * - Nothing is saved until Save. `dirty` is reported upward so the panel's frame
 *   can say "unsaved" — a panel is much easier to walk away from than a page was.
 */

import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { ErrorBanner, Loading, Notice } from "./Feedback";
import { LayoutEditor, type LayoutEditorContentInfo } from "./LayoutEditor";
import {
  getContainerType,
  getLocationLayout,
  getSlotTemplate,
  reapplyLayout,
  setLocationChildView,
  type ContainerTypeRead,
  type LayoutRead,
  type LocationRead,
  type SlotSpecOut,
} from "../lib/api/client";
import { type AffectedSlotProblem, describeError } from "../lib/api/errors";
import { useAsync } from "../lib/hooks/useAsync";
import {
  draftOf,
  gridExtent,
  previewChanges,
  requireLabels,
  toSlotSpecIn,
  type DraftSlot,
  type OriginalSlot,
} from "../lib/locations/layoutDraft";
import { slotLabelFor } from "../lib/locations/slots";
import { known, VIEW_LABELS, type ChildView } from "../lib/locations/views";
import { uuid4 } from "../lib/scan/session";

/**
 * How *this one* container draws its children — the instance half of ADR 0006's
 * override.
 *
 * Its own section, above the canvas and separate from it, because the two answer
 * different questions: the canvas says where the slots are, this says what the
 * picture looks like. It also saves on its own rather than through Save, and that
 * is deliberate — the change guard exists to protect slots that hold stock, and a
 * drawing cannot swallow a neighbour's contents, so putting it behind the same
 * button would imply a risk it does not carry.
 */
function ChildViewPicker({
  location,
  typeName,
  onSaved,
}: {
  location: LocationRead;
  typeName: string | null;
  /** Re-read the container. **Not optional in practice**: the page behind this
   * panel decides which picture to draw — and whether to fetch a room plan at all
   * — from `effective_child_view` on its own `LocationRead`. Without this the
   * write succeeded and nothing behind the panel ever heard, so the cabinet you
   * had just asked to be drawn as a floor plan kept drawing drawer fronts until a
   * manual reload, and reopening this panel re-initialised the select from the
   * stale row and showed the *old* choice. */
  onSaved?: (() => void) | undefined;
}) {
  // "" is "use the container type", which is a real choice — sending null is what
  // clears the override — rather than an absence.
  const [choice, setChoice] = useState<string>(location.child_view ?? "");
  const [effective, setEffective] = useState<string>(location.effective_child_view);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function apply(next: string): Promise<void> {
    setChoice(next);
    setBusy(true);
    setError(null);
    try {
      const response = await setLocationChildView(location.id, {
        child_view: next === "" ? null : (next as ChildView),
        client_op_id: uuid4(),
      });
      setEffective(response.effective_child_view);
      onSaved?.();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h3>How this one is drawn</h3>
      <label className="field">
        <span>Picture</span>
        <select value={choice} onChange={(event) => void apply(event.target.value)} disabled={busy}>
          <option value="">
            {typeName === null
              ? "Work it out from what this container is"
              : `Whatever "${typeName}" says`}
          </option>
          {(Object.keys(VIEW_LABELS) as ChildView[]).map((kind) => (
            <option key={kind} value={kind}>
              {VIEW_LABELS[kind]}
            </option>
          ))}
        </select>
      </label>
      <p className="muted-note" style={{ margin: 0 }}>
        Currently drawn as: {VIEW_LABELS[known(effective)].toLowerCase()}.{" "}
        {choice === "" ? "Not overridden here." : "Overridden for this container only."} Saved as
        soon as you pick it: this changes the picture and nothing else — no slot moves, and nothing
        inside is touched. It applies to this container's own contents, not to anything deeper:
        each level answers for itself.
      </p>
      <ErrorBanner error={error} fallback="That could not be changed." />
    </div>
  );
}

const REASON_WORDS: Readonly<Record<string, string>> = {
  has_stock: "holds stock",
  has_tag: "has a bound tag",
  has_children: "has something placed inside it",
};

function wordsFor(reasons: readonly string[]): string {
  return reasons.map((reason) => REASON_WORDS[reason] ?? reason).join(", ");
}

function suggestLabel(rowIdx: number, colIdx: number): string {
  return slotLabelFor({ row: rowIdx, col: colIdx });
}

function slotSpecOutToOriginal(spec: {
  location_id: number;
  slot_label: string;
  row_idx: number;
  col_idx: number;
  row_span: number;
  col_span: number;
  size_class: string | null;
  inner_volume_mm3: number | null;
  short_id: string | null;
  has_tag: boolean;
  lot_count: number;
  qty_milli: number;
}): OriginalSlot {
  return {
    id: String(spec.location_id),
    rowIdx: spec.row_idx,
    colIdx: spec.col_idx,
    rowSpan: spec.row_span,
    colSpan: spec.col_span,
    slotLabel: spec.slot_label,
    sizeClass: spec.size_class,
    innerVolumeMm3: spec.inner_volume_mm3,
    locationId: spec.location_id,
    shortId: spec.short_id,
    hasTag: spec.has_tag,
    lotCount: spec.lot_count,
    qtyMilli: spec.qty_milli,
  };
}

function specOutToDraft(spec: SlotSpecOut): DraftSlot {
  return {
    id: uuid4(),
    rowIdx: spec.row_idx,
    colIdx: spec.col_idx,
    rowSpan: spec.row_span,
    colSpan: spec.col_span,
    slotLabel: spec.slot_label,
    sizeClass: spec.size_class,
    innerVolumeMm3: spec.inner_volume_mm3,
  };
}

export interface SlotLayoutPanelProps {
  readonly location: LocationRead;
  /** Called after a save the server accepted, so the page behind can re-read. */
  readonly onSaved?: (() => void) | undefined;
  /** Whether there is an unsent edit in the draft. Lifted so the panel's frame
   * can say so where the close button is. */
  readonly onDirtyChange?: ((dirty: boolean) => void) | undefined;
}

export function SlotLayoutPanel({ location, onSaved, onDirtyChange }: SlotLayoutPanelProps) {
  const layout = useAsync<LayoutRead>(() => getLocationLayout(location.id), [location.id]);

  if (layout.error !== null) {
    return (
      <ErrorBanner error={layout.error} fallback="This container's layout could not be loaded." />
    );
  }
  if (layout.data === null) {
    return <Loading what="the layout" />;
  }
  return (
    <Editor
      location={location}
      initialLayout={layout.data}
      onSaved={onSaved}
      onDirtyChange={onDirtyChange}
    />
  );
}

type Outcome =
  | { readonly kind: "ok"; readonly message: string }
  | { readonly kind: "guarded"; readonly affected: readonly AffectedSlotProblem[] }
  | { readonly kind: "refused"; readonly message: string };

function Editor({
  location,
  initialLayout,
  onSaved,
  onDirtyChange,
}: {
  location: LocationRead;
  initialLayout: LayoutRead;
  onSaved?: (() => void) | undefined;
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
}) {
  const [baseline, setBaseline] = useState<readonly OriginalSlot[]>(() =>
    initialLayout.slots.map(slotSpecOutToOriginal),
  );
  const [slots, setSlots] = useState<readonly DraftSlot[]>(() => baseline.map(draftOf));
  const [rows, setRows] = useState(initialLayout.grid_rows);
  const [cols, setCols] = useState(initialLayout.grid_cols);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [loadingType, setLoadingType] = useState(false);

  const containerType = useAsync<ContainerTypeRead | null>(
    () =>
      location.container_type_id !== null
        ? getContainerType(location.container_type_id)
        : Promise.resolve(null),
    [location.container_type_id],
  );

  const contentById = useMemo(() => {
    const map = new Map<string, LayoutEditorContentInfo>();
    for (const slot of baseline) {
      map.set(slot.id, {
        shortId: slot.shortId,
        hasTag: slot.hasTag,
        lotCount: slot.lotCount,
        qtyMilli: slot.qtyMilli,
      });
    }
    return map;
  }, [baseline]);

  const preview = useMemo(() => previewChanges(baseline, slots), [baseline, slots]);
  const dirty =
    preview.creates.length > 0 ||
    preview.updates.length > 0 ||
    preview.deletes.length > 0 ||
    preview.reinterpreted.length > 0;
  const blankLabels = requireLabels(slots);
  const canSave = dirty && preview.reinterpreted.length === 0 && blankLabels.length === 0;

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  async function loadTypeLayout(): Promise<void> {
    if (location.container_type_id === null) {
      return;
    }
    setLoadingType(true);
    setOutcome(null);
    try {
      const template = await getSlotTemplate(location.container_type_id);
      const nextSlots = template.slots.map(specOutToDraft);
      setSlots(nextSlots);
      const extent = gridExtent(nextSlots);
      setRows(Math.max(template.grid_rows ?? 0, extent.rows));
      setCols(Math.max(template.grid_cols ?? 0, extent.cols));
    } catch (cause) {
      setOutcome({ kind: "refused", message: describeError(cause).headline });
    } finally {
      setLoadingType(false);
    }
  }

  async function save(): Promise<void> {
    if (!canSave) {
      return;
    }
    setBusy(true);
    setOutcome(null);
    try {
      const response = await reapplyLayout(location.id, {
        slots: slots.map(toSlotSpecIn),
        client_op_id: uuid4(),
      });
      setOutcome({
        kind: "ok",
        message: `${response.created} created, ${response.updated} updated, ${response.deleted} removed.`,
      });
      const nextBaseline = response.layout.slots.map(slotSpecOutToOriginal);
      setBaseline(nextBaseline);
      setSlots(nextBaseline.map(draftOf));
      setRows(response.layout.grid_rows);
      setCols(response.layout.grid_cols);
      onSaved?.();
    } catch (cause) {
      const report = describeError(cause);
      if (report.reason === "slots_hold_content" && report.affectedSlots !== null) {
        setOutcome({ kind: "guarded", affected: report.affectedSlots });
      } else {
        setOutcome({ kind: "refused", message: report.headline });
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      {location.container_type_id !== null && (
        <div className="card">
          <div className="row">
            <p className="muted-note" style={{ flex: 1, margin: 0 }}>
              {containerType.data === null
                ? "Loading the container type this was stamped from…"
                : `Stamped from "${containerType.data.display_name}" — a starting point, and ` +
                  "nothing more: this container has owned its layout ever since. Loading the " +
                  "type's current layout below replaces the draft you are editing, nothing is " +
                  "saved until you press Save, and the change guard still applies exactly as it " +
                  "would to a hand-drawn edit."}
            </p>
            <button
              type="button"
              onClick={() => void loadTypeLayout()}
              disabled={loadingType || containerType.data === null}
            >
              {loadingType ? "Loading…" : "Load the type's current layout"}
            </button>
          </div>
        </div>
      )}

      <ChildViewPicker
        location={location}
        typeName={containerType.data?.display_name ?? null}
        onSaved={onSaved}
      />

      <div className="card">
        <h3>Canvas</h3>
        <LayoutEditor
          slots={slots}
          onChange={setSlots}
          rows={rows}
          cols={cols}
          onResize={(nextRows, nextCols) => {
            setRows(nextRows);
            setCols(nextCols);
          }}
          labelMode="required"
          suggestLabel={suggestLabel}
          contentById={contentById}
        />
      </div>

      <div className="card">
        <h3>Review before saving</h3>
        {!dirty ? (
          <p className="dim">No changes yet.</p>
        ) : (
          <ul className="list">
            {preview.creates.map((slot) => (
              <li key={slot.id} className="sub">
                <span className="badge badge-good">new</span> {slot.slotLabel || "(unlabelled)"}
              </li>
            ))}
            {preview.updates.map(({ before, after }) => (
              <li key={before.id} className="sub">
                <span className="badge">relabelled</span> {before.slotLabel} → {after.slotLabel}
              </li>
            ))}
            {preview.deletes.map((slot) => {
              const blocked = slot.hasTag || slot.lotCount > 0;
              return (
                <li key={slot.id} className="sub">
                  {blocked ? (
                    <span className="badge badge-warn">blocked</span>
                  ) : (
                    <span className="badge badge-good">removed</span>
                  )}{" "}
                  {slot.slotLabel}
                  {blocked &&
                    ` — ${wordsFor([
                      ...(slot.lotCount > 0 ? ["has_stock"] : []),
                      ...(slot.hasTag ? ["has_tag"] : []),
                    ])}; this will not save until it is moved or cleared`}
                  {/* A printed card does not block — cardstock is cheap and a
                      re-layout is a legitimate thing to do. But the card in the
                      drawer front stops working the moment this saves, and the
                      person doing it is the person who will find that out, so
                      they are told before rather than after. */}
                  {!blocked && slot.shortId !== null && (
                    <span className="muted-note">
                      {" "}
                      — the card printed for this slot ({slot.shortId}) will stop working
                    </span>
                  )}
                </li>
              );
            })}
            {preview.reinterpreted.map(({ before, afterLabel }) => (
              <li key={before.id} className="sub">
                <span className="badge badge-bad">refused</span> "{afterLabel}" would reinterpret{" "}
                {before.slotLabel}'s identity at a new position — delete it and create a new slot
                instead
              </li>
            ))}
          </ul>
        )}
        {blankLabels.length > 0 && (
          <p className="muted-note" style={{ margin: 0 }}>
            {blankLabels.length} slot(s) have no label — an instance has no generator to fill one
            in, unlike a type's canvas.
          </p>
        )}
      </div>

      {outcome?.kind === "ok" && (
        <Notice kind="ok" title="Saved">
          {outcome.message}
        </Notice>
      )}
      {outcome?.kind === "guarded" && (
        <Notice kind="warn" title="Blocked — some slots still hold content">
          <p style={{ margin: 0 }}>
            This did not save. Move the contents of the slot(s) below out of the way, then press
            Save again — everything else in this edit stays exactly as drawn.
          </p>
          <ul className="list">
            {outcome.affected.map((affected) => (
              <li key={affected.locationId} className="sub">
                <Link to={`/locations/${affected.locationId}`}>
                  {affected.slotLabel || `location ${affected.locationId}`}
                </Link>{" "}
                — {wordsFor(affected.reasons)}
              </li>
            ))}
          </ul>
        </Notice>
      )}
      {outcome?.kind === "refused" && (
        <Notice kind="error" title="Refused">
          {outcome.message}
        </Notice>
      )}

      <div className="row">
        {dirty ? (
          <span className="badge badge-warn">unsaved</span>
        ) : (
          <span className="muted-note">Everything here is saved.</span>
        )}
        <span className="spacer" />
        <button
          type="button"
          className="primary"
          onClick={() => void save()}
          disabled={!canSave || busy}
        >
          {busy ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  );
}
