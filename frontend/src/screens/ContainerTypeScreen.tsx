/**
 * `/container-types/:id` — one container type: what it is, and the canvas
 * editor for its reusable slot layout.
 *
 * Two halves, deliberately in this order. **What it is** — ADR 0002's two
 * questions, its capacity model, its label scheme, its picture — is edited
 * through `ContainerTypeForm`, the same component the create screen uses, so the
 * two questions are never phrased two ways. **What its slots are** is the canvas,
 * which is `.../slot-template`'s single door and stays exactly as it was.
 *
 * A type is a template, so this screen also has to answer "and now what?": the
 * actions that turn it into something usable — stamp containers from it, or copy
 * it — are a card of their own near the top rather than being left implicit.
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

import { ContainerPhotoPanel } from "../components/ContainerPhotoPanel";
import { ContainerTypeForm } from "../components/ContainerTypeForm";
import { LayoutEditor } from "../components/LayoutEditor";
import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import { PathBar } from "../components/PathBar";
import {
  cloneContainerType,
  detachContainerTypeDocument,
  getContainerType,
  getSlotTemplate,
  putSlotTemplate,
  updateContainerType,
  uploadDocument,
  type ContainerTypeRead,
  type SlotLabelScheme,
  type SlotTemplateRead,
} from "../lib/api/client";
import {
  describeOccupies,
  describePresents,
  draftFromType,
  toUpdateRequest,
  type TypeDraft,
} from "../lib/containers/typeDraft";
import { useAsync } from "../lib/hooks/useAsync";
import { toSlotSpecIn, type DraftSlot } from "../lib/locations/layoutDraft";
import { slotLabelFor } from "../lib/locations/slots";
import { known, VIEW_LABELS } from "../lib/locations/views";
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
        <PathBar
          trail={[
            { key: "types", label: "Container types", to: "/container-types" },
            { key: `type-${type.id}`, label: type.display_name },
          ]}
          label="Container type path"
        />
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
        {/* ADR 0002's two answers, stated apart before the form is even opened —
            they are what somebody is checking when they ask "is this the right
            type", and one merged "size" line would answer neither question. */}
        <p className="muted-note" style={{ margin: 0 }}>
          Offers: {describePresents(type)} · Takes up: {describeOccupies(type)}
        </p>
        <p className="muted-note" style={{ margin: 0 }}>
          {VIEW_LABELS[known(type.effective_child_view)]}
          {type.child_view === null && " (worked out from the layout)"}
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

      <UseThisType type={type} />

      {editingDetails && (
        <EditDetails
          type={type}
          onDone={(nextId) => {
            setEditingDetails(false);
            if (nextId === type.id) {
              onReload();
            } else {
              // The edit landed on a clone, because this is a seed. Follow it, or
              // the next save would clone the seed a second time.
              navigate(`/container-types/${nextId}`, { replace: true });
            }
          }}
        />
      )}

      <div className="card">
        <h3>Picture</h3>
        <p className="muted-note" style={{ margin: 0 }}>
          What every instance of this type looks like by default. One particular
          container can still be given its own photo on its own screen.
        </p>
        <ContainerPhotoPanel
          displayPhoto={type.photo}
          ownPhoto={type.photo}
          glyph={type.glyph}
          onUpload={async (file) => {
            const result = await uploadDocument(file, {
              mediaType: file.type !== "" ? file.type : "image/jpeg",
              kind: "photo",
              role: "photo",
              containerTypeId: type.id,
              isPrimary: true,
            });
            // A photo is an edit like any other, so a seed clones — and this
            // screen has to follow the copy for the same reason `EditDetails`
            // and "Save layout" do: staying put would leave every later save
            // aimed at the seed, minting a fresh clone per click.
            if (result.cloned_container_type && result.container_type_id !== null) {
              setSaved(
                `Saved as a new type, since "${type.display_name}" is a seed and cannot be ` +
                  "changed directly — the rest of this screen now points at that copy.",
              );
              navigate(`/container-types/${result.container_type_id}`, { replace: true });
              return;
            }
            onReload();
          }}
          onRemoveOwn={
            type.photo === null
              ? null
              : async () => {
                  if (type.photo !== null) {
                    await detachContainerTypeDocument(type.id, type.photo.sha256);
                  }
                  onReload();
                }
          }
        />
      </div>

      <Notice kind="info" title="This never reaches into an already-built container">
        A cabinet already stamped from this type keeps whatever layout it had at the moment
        it was created — nothing saved here touches it. To push a change into one specific
        cabinet, open that cabinet, press "Edit this container", then "Slots inside…" and load
        this type's current layout there — which goes through its own change guard.
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

/**
 * "And now what?" — the two things you do *with* a type, rather than to it.
 *
 * Prominent and near the top because a type on its own holds nothing, and both
 * routes already exist: `POST .../clone` for "this cabinet is identical to that
 * one", and `POST /api/locations/{id}/instantiate` for turning the template into
 * containers. Leaving those to be discovered elsewhere is how a complete backend
 * still reads as "I can't create my own containers".
 */
function UseThisType({ type }: { type: ContainerTypeRead }) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function clone(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const response = await cloneContainerType(type.id, { client_op_id: uuid4() });
      navigate(`/container-types/${response.container_type.id}`);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h3>Use this type</h3>
      <div className="row">
        <Link className="button-link" to={`/containers/new?type=${type.id}`}>
          Create containers from it
        </Link>
        <button type="button" onClick={() => void clone()} disabled={busy}>
          {busy ? "Copying…" : "Clone it"}
        </button>
      </div>
      <p className="muted-note" style={{ margin: 0 }}>
        A type is a template — nothing can be put inside one. Stamping it produces real
        containers under a parent you choose, each with its own copy of the layout below.
        Cloning is for "the same, but with two things changed"; the copy is yours to edit
        even when this one is not.
      </p>
      <ErrorBanner error={error} fallback="That type could not be copied." />
    </div>
  );
}

/**
 * Everything about the type that is not its slot canvas, through the same form
 * the create screen uses — so ADR 0002's two questions are asked in one voice.
 *
 * `onDone` is handed the id that was actually written. Editing a seed clones it
 * (`ensure_editable`), so that id is not always the one in the URL, and a screen
 * that ignored the difference would keep showing the untouched seed while a
 * second save minted a second copy.
 */
function EditDetails({
  type,
  onDone,
}: {
  type: ContainerTypeRead;
  onDone: (writtenTypeId: number) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function save(draft: TypeDraft): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const response = await updateContainerType(type.id, toUpdateRequest(draft, uuid4()));
      onDone(response.container_type.id);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h3>What it is</h3>
      <ErrorBanner error={error} fallback="Those details could not be saved." />
      <ContainerTypeForm
        initial={draftFromType(type)}
        mode="edit"
        clonesOnSave={type.is_seed}
        busy={busy}
        derivedChildView={type.effective_child_view}
        onSubmit={(draft) => void save(draft)}
        onCancel={() => onDone(type.id)}
      />
    </div>
  );
}
