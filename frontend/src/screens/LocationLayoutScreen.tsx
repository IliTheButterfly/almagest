/**
 * `/locations/:id/layout` — edit one already-instantiated container's own
 * slots, through the three-way change guard.
 *
 * **Deliberately its own screen, not a panel bolted onto the bin view.**
 * Editing a container *type*'s canvas (`ContainerTypeScreen`) never touches
 * a cabinet already built from it — `reapply-layout` is the separate,
 * explicit action that pushes a change into *this one* instance, and it
 * goes through the guard exactly like any hand-drawn edit does. Loading the
 * type's current layout here is one more explicit step (below), not a
 * button that silently overwrites and saves in the same click — a user who
 * expected a preview must still get to see one.
 *
 * **The three outcomes are kept visually and verbally distinct, per ADR
 * 0002 and `app.services.layout_authoring`:**
 * - *safe* → the request went through; a plain "Saved" notice.
 * - *guarded* (409, `slots_hold_content`) → an amber notice naming every
 *   blocked slot and what it holds, each linking to that container so its
 *   contents can be moved out — not a generic "are you sure".
 * - *refused* (422, most often `slot_identity_reinterpreted`) → a red
 *   notice. Detected client-side before Save is even pressed wherever
 *   possible (`previewChanges`), because it can never succeed by retrying.
 */

import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import { LayoutEditor, type LayoutEditorContentInfo } from "../components/LayoutEditor";
import {
  getContainerType,
  getLocation,
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
 * Its own card, above the canvas and separate from it, because the two answer
 * different questions: the canvas says where the slots are, this says what the
 * picture looks like. It also saves on its own rather than through Save, and that
 * is deliberate — the change guard exists to protect slots that hold stock, and a
 * drawing cannot swallow a neighbour's contents, so putting it behind the same
 * button would imply a risk it does not carry.
 */
function ChildViewPicker({
  location,
  typeName,
}: {
  location: LocationRead;
  typeName: string | null;
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
        {choice === "" ? "Not overridden here." : "Overridden for this container only."} This
        changes the picture and nothing else — no slot moves, and nothing inside is touched.
        It applies to this container's own contents, not to anything deeper: each level
        answers for itself.
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

export function LocationLayoutScreen() {
  const { locationId: raw } = useParams();
  const locationId = Number(raw);
  const valid = Number.isSafeInteger(locationId) && locationId > 0;

  const location = useAsync<LocationRead | null>(
    () => (valid ? getLocation(locationId) : Promise.resolve(null)),
    [locationId, valid],
  );

  if (!valid) {
    return <Notice kind="error" title="That is not a location id" />;
  }
  if (location.error !== null) {
    return <ErrorBanner error={location.error} fallback="That container could not be loaded." />;
  }
  if (location.data === null) {
    return <Loading what="the container" />;
  }
  return <LayoutLoader location={location.data} />;
}

function LayoutLoader({ location }: { location: LocationRead }) {
  const layout = useAsync<LayoutRead>(() => getLocationLayout(location.id), [location.id]);

  if (layout.error !== null) {
    return <ErrorBanner error={layout.error} fallback="This container's layout could not be loaded." />;
  }
  if (layout.data === null) {
    return <Loading what="the layout" />;
  }
  return <Editor location={location} initialLayout={layout.data} />;
}

type Outcome =
  | { readonly kind: "ok"; readonly message: string }
  | { readonly kind: "guarded"; readonly affected: readonly AffectedSlotProblem[] }
  | { readonly kind: "refused"; readonly message: string };

function Editor({ location, initialLayout }: { location: LocationRead; initialLayout: LayoutRead }) {
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
      <div className="card">
        <div className="row">
          <Link to={`/locations/${location.id}`}>← {location.name}</Link>
        </div>
        <h1>Edit layout</h1>
        <p className="muted-note" style={{ margin: 0 }}>
          {location.label_path}
        </p>
      </div>

      {location.container_type_id !== null && (
        <div className="card">
          <div className="row">
            <p className="muted-note" style={{ flex: 1, margin: 0 }}>
              {containerType.data === null
                ? "Loading the container type this was stamped from…"
                : `Stamped from "${containerType.data.display_name}". Loading its current ` +
                  "layout below replaces the draft you are editing — nothing is saved until " +
                  "you press Save, and the change guard still applies exactly as it would to " +
                  "a hand-drawn edit."}
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

      <ChildViewPicker location={location} typeName={containerType.data?.display_name ?? null} />

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
                </li>
              );
            })}
            {preview.reinterpreted.map(({ before, afterLabel }) => (
              <li key={before.id} className="sub">
                <span className="badge badge-bad">refused</span> "{afterLabel}" would reinterpret{" "}
                {before.slotLabel}'s identity at a new position — delete it and create a new
                slot instead
              </li>
            ))}
          </ul>
        )}
        {blankLabels.length > 0 && (
          <p className="muted-note" style={{ margin: 0 }}>
            {blankLabels.length} slot(s) have no label — an instance has no generator to fill
            one in, unlike a type's canvas.
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
            This did not save. Move the contents of the slot(s) below out of the way, then
            press Save again — everything else in this edit stays exactly as drawn.
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

      <button type="button" className="primary wide" onClick={() => void save()} disabled={!canSave || busy}>
        {busy ? "Saving…" : "Save"}
      </button>
    </div>
  );
}
