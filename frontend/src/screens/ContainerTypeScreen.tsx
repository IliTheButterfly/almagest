/**
 * `/container-types/:id` — the canvas editor for one container type's
 * reusable slot layout.
 *
 * **Editing this never touches a cabinet already built from it.** A type
 * has no children of its own, so there is nothing here for the change
 * guard to protect and no `GuardedLayoutChange` this screen can ever
 * receive — that is precisely what makes an already-instantiated
 * container's own layout (`LocationLayoutScreen`) a separate screen with a
 * separate, explicit "reapply" step, rather than this one reaching out and
 * touching every instance the moment it saves.
 *
 * A seed type (`is_seed`) is read-only; saving any change here clones it
 * first (`app.services.layout_authoring.ensure_editable`) and this screen
 * follows the clone by navigating to its id, so a second save lands on the
 * copy rather than minting a third.
 */

import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { LayoutEditor } from "../components/LayoutEditor";
import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import {
  getContainerType,
  getSlotTemplate,
  putSlotTemplate,
  updateContainerType,
  type ContainerTypeRead,
  type SlotLabelScheme,
  type SlotTemplateRead,
} from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";
import { toSlotSpecIn, type DraftSlot } from "../lib/locations/layoutDraft";
import { slotLabelFor } from "../lib/locations/slots";
import { uuid4 } from "../lib/scan/session";

function suggestLabel(rowIdx: number, colIdx: number): string {
  return slotLabelFor({ row: rowIdx, col: colIdx });
}

function specOutToDraft(spec: SlotTemplateRead["slots"][number]): DraftSlot {
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

export function ContainerTypeScreen() {
  const { containerTypeId: raw } = useParams();
  const containerTypeId = Number(raw);
  const valid = Number.isSafeInteger(containerTypeId) && containerTypeId > 0;

  const bundle = useAsync<{ type: ContainerTypeRead; template: SlotTemplateRead } | null>(
    () =>
      valid
        ? Promise.all([getContainerType(containerTypeId), getSlotTemplate(containerTypeId)]).then(
            ([type, template]) => ({ type, template }),
          )
        : Promise.resolve(null),
    [containerTypeId, valid],
  );

  if (!valid) {
    return <Notice kind="error" title="That is not a container type id" />;
  }
  if (bundle.error !== null) {
    return <ErrorBanner error={bundle.error} fallback="That container type could not be loaded." />;
  }
  if (bundle.data === null) {
    return <Loading what="the container type" />;
  }
  return (
    <TypeEditor type={bundle.data.type} template={bundle.data.template} onReload={bundle.reload} />
  );
}

function TypeEditor({
  type,
  template,
  onReload,
}: {
  type: ContainerTypeRead;
  template: SlotTemplateRead;
  onReload: () => void;
}) {
  const navigate = useNavigate();
  const [rows, setRows] = useState(template.grid_rows ?? 0);
  const [cols, setCols] = useState(template.grid_cols ?? 0);
  const [scheme, setScheme] = useState<SlotLabelScheme>(template.slot_label_scheme as SlotLabelScheme);
  const [slots, setSlots] = useState<readonly DraftSlot[]>(() => template.slots.map(specOutToDraft));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [editingDetails, setEditingDetails] = useState(false);

  async function save(): Promise<void> {
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      const response = await putSlotTemplate(type.id, {
        grid_rows: rows,
        grid_cols: cols,
        slot_label_scheme: scheme,
        slots: slots.map(toSlotSpecIn),
        client_op_id: uuid4(),
      });
      if (response.cloned) {
        setSaved(
          `Saved as a new type, since "${type.display_name}" is a seed and cannot be edited ` +
            "directly — the rest of this screen now points at that copy.",
        );
        navigate(`/container-types/${response.container_type_id}`, { replace: true });
      } else {
        setSaved("Saved.");
        onReload();
      }
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <Link to="/container-types">← types</Link>
        </div>
        <div className="row">
          <h1 style={{ flex: 1 }}>{type.display_name}</h1>
          {type.is_seed && <span className="badge badge-warn">seed</span>}
          <button type="button" onClick={() => setEditingDetails(!editingDetails)}>
            {editingDetails ? "Cancel" : "Edit details"}
          </button>
        </div>
        <p className="muted-note mono" style={{ margin: 0 }}>
          {type.slug}
        </p>
        {type.description !== null && <p style={{ margin: 0 }}>{type.description}</p>}
        {type.is_seed && (
          <Notice kind="info" title="This is a seed type">
            It ships with every fresh install. Saving any change on this screen creates your
            own editable copy rather than changing the shared original that every install
            starts with.
          </Notice>
        )}
      </div>

      {editingDetails && (
        <EditDetails
          type={type}
          onDone={() => {
            setEditingDetails(false);
            onReload();
          }}
        />
      )}

      <Notice kind="info" title="This never reaches into an already-built container">
        A cabinet already stamped from this type keeps whatever layout it had at the moment
        it was created — nothing saved here touches it. To push a change into one specific
        cabinet, open that cabinet's own "Edit layout" screen and choose to load this type's
        current layout there, which goes through its own change guard.
      </Notice>

      <div className="card">
        <h3>Canvas</h3>
        <label className="field">
          <span>Label scheme</span>
          <select value={scheme} onChange={(event) => setScheme(event.target.value as SlotLabelScheme)}>
            <option value="row_alpha_col_num">A1, B2, … (rows as letters)</option>
            <option value="sequential">1, 2, 3, …</option>
            <option value="custom">custom — set every label directly on the canvas</option>
          </select>
        </label>
        {/*
          KNOWN LIMITATION — `slot_label_params` (the sequential scheme's
          zero-pad width, the custom scheme's label list) is passed through
          unmodified rather than edited here. It only matters before the
          first materialising edit, since `materialize_slots` is a one-way
          flip after which the generator this canvas writes through is never
          consulted again for this type (see the module docstring on
          `app.services.layout_authoring`) — so exposing it here would be
          real UI for a state this editor itself is about to leave.
        */}
        <LayoutEditor
          slots={slots}
          onChange={setSlots}
          rows={rows}
          cols={cols}
          onResize={(nextRows, nextCols) => {
            setRows(nextRows);
            setCols(nextCols);
          }}
          labelMode="auto"
          suggestLabel={suggestLabel}
        />
      </div>

      <ErrorBanner error={error} fallback="That layout could not be saved." />
      {saved !== null && <Notice kind="ok">{saved}</Notice>}
      <button type="button" className="primary wide" onClick={() => void save()} disabled={busy}>
        {busy ? "Saving…" : "Save layout"}
      </button>
    </div>
  );
}

function EditDetails({ type, onDone }: { type: ContainerTypeRead; onDone: () => void }) {
  const [displayName, setDisplayName] = useState(type.display_name);
  const [description, setDescription] = useState(type.description ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function save(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await updateContainerType(type.id, {
        display_name: displayName,
        description: description === "" ? null : description,
        client_op_id: uuid4(),
      });
      onDone();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="card"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <h3>Edit details</h3>
      <label className="field">
        <span>Display name</span>
        <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
      </label>
      <label className="field">
        <span>Description</span>
        <textarea rows={3} value={description} onChange={(event) => setDescription(event.target.value)} />
      </label>
      <ErrorBanner error={error} fallback="Those details could not be saved." />
      <button type="submit" className="primary wide" disabled={busy || displayName.trim() === ""}>
        {busy ? "Saving…" : "Save"}
      </button>
    </form>
  );
}
