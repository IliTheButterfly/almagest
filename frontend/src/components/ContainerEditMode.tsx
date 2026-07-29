/**
 * Edit mode: the same container page, turned into its own editor.
 *
 * Iliana: *"I don't like the multiple pages per container to edit them. I'd much
 * prefer a page and an edit mode. Think about the layout that home assistant has.
 * you can customize the UI (storage) or use it normally."* So there is one page
 * per container, and this is the mode switch on it. What used to be
 * `/locations/:id/layout` and `/containers/new?parent=:id` are panels over that
 * page now — nothing about *what* they do changed, only that you no longer leave
 * the container to use them.
 *
 * **This component knows nothing about depth, and must not learn.** It is handed a
 * `LocationRead` and renders the same four panels whether that row is a room, a
 * cabinet, a drawer or a divider inside a bin — the same rule
 * `lib/locations/views.ts` documents for drawing a level: every level is asked
 * about itself. A branch here on "is this a room" or `depth === 0` would be the
 * bug ADR 0006 warns about, and there is a test that renders this at depth 3 and
 * asserts it is the same component.
 *
 * The panels differ in *when* they save, and each one says which it is, because
 * ambiguity about that is how work gets lost:
 *
 * - **Name and description** and **Slots inside** hold a draft and save on a
 *   button. Both show "unsaved" in the panel's titlebar and both refuse to close
 *   silently on an unsent edit.
 * - **Picture** (photo, pictogram) and **how this one is drawn** save the moment
 *   you choose, and say so. They have nothing a guard could protect: a pictogram
 *   cannot swallow a neighbour's stock.
 *
 * Which panel is open lives in `?panel=`, so a deep link — including the redirect
 * that keeps the old `/locations/:id/layout` URL working — opens the right one.
 */

import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { ContainerPhotoPanel } from "./ContainerPhotoPanel";
import { Dialog, DiscardPrompt, useDiscardGuard } from "./Dialog";
import { ErrorBanner, Loading, Notice } from "./Feedback";
import { RemoveContainer } from "./RemoveContainer";
import { SlotLayoutPanel } from "./SlotLayoutPanel";
import { AddContainersPanel } from "./AddContainers";
import {
  detachLocationDocument,
  listContainerTypes,
  setLocationDetails,
  setLocationGlyph,
  uploadDocument,
  type ContainerGlyph,
  type ContainerTypeRead,
  type LocationRead,
} from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";
import { ALL_GLYPHS, glyphLabel } from "../lib/locations/glyphs";
import { uuid4 } from "../lib/scan/session";

const PANELS = ["details", "picture", "layout", "add"] as const;

export type EditPanel = (typeof PANELS)[number];

function knownPanel(value: string | null): EditPanel | null {
  return PANELS.includes((value ?? "") as EditPanel) ? (value as EditPanel) : null;
}

/**
 * The mode itself, and which panel is open — both in the URL.
 *
 * In the URL rather than in component state so that edit mode survives a reload,
 * is shareable ("open this drawer's layout"), and can be arrived at by a redirect
 * from the routes this replaced. The container page is a `/s/{short_id}` redirect
 * target, so the *path* may never change; a query parameter is free.
 */
export function useEditMode(): {
  readonly editing: boolean;
  readonly setEditing: (next: boolean) => void;
  readonly panel: EditPanel | null;
  readonly openPanel: (next: EditPanel | null) => void;
} {
  const [params, setParams] = useSearchParams();
  const editing = params.get("edit") === "1";

  function write(changes: { edit?: boolean; panel?: EditPanel | null }): void {
    const next = new URLSearchParams(params);
    if (changes.edit !== undefined) {
      if (changes.edit) {
        next.set("edit", "1");
      } else {
        next.delete("edit");
        next.delete("panel");
      }
    }
    if (changes.panel !== undefined) {
      if (changes.panel === null) {
        next.delete("panel");
      } else {
        next.set("panel", changes.panel);
      }
    }
    setParams(next, { replace: true });
  }

  return {
    editing,
    setEditing: (nextEditing) => write({ edit: nextEditing }),
    panel: editing ? knownPanel(params.get("panel")) : null,
    openPanel: (next) => write({ edit: true, panel: next }),
  };
}

/** The toggle. Its own component so every level's page uses the same one. */
export function EditModeToggle({
  editing,
  onChange,
}: {
  editing: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      className="toggle"
      aria-pressed={editing}
      onClick={() => onChange(!editing)}
    >
      {editing ? "Done editing" : "Edit this container"}
    </button>
  );
}

export function ContainerEditMode({
  location,
  onChanged,
}: {
  location: LocationRead;
  /** Re-read the container: a rename changes the heading and the path, a save in
   * the layout panel changes what is inside. */
  onChanged: () => void;
}) {
  const { panel, openPanel } = useEditMode();
  const close = (): void => openPanel(null);

  return (
    <div className="card editing">
      <div className="editing-strip">
        <span className="badge">edit mode</span>
        <p className="muted-note" style={{ flex: 1, margin: 0 }}>
          Customising this container. Everything below happens here, in place — a container type is
          only ever what a container *started* as, so relabelling a slot, merging two of them or
          adding a drawer changes this one container and nothing else of its kind.
        </p>
      </div>

      <div className="row">
        <button type="button" onClick={() => openPanel("details")}>
          Name and description…
        </button>
        <button type="button" onClick={() => openPanel("picture")}>
          Picture…
        </button>
        <button type="button" onClick={() => openPanel("layout")}>
          Slots inside…
        </button>
        <button type="button" onClick={() => openPanel("add")}>
          Add containers inside…
        </button>
      </div>

      <RemoveContainer location={location} onChanged={onChanged} />

      {panel === "details" && (
        <DetailsDialog location={location} onClose={close} onSaved={onChanged} />
      )}
      {panel === "picture" && (
        <PictureDialog location={location} onClose={close} onSaved={onChanged} />
      )}
      {panel === "layout" && (
        <LayoutDialog location={location} onClose={close} onSaved={onChanged} />
      )}
      {panel === "add" && <AddDialog location={location} onClose={close} onCreated={onChanged} />}
    </div>
  );
}

// --------------------------------------------------------------- details ----

/** "" is the third state — inherit — and not an absence. */
type Tri = "" | "yes" | "no";

function triOf(value: boolean | null | undefined): Tri {
  return value === null || value === undefined ? "" : value ? "yes" : "no";
}

function triValue(tri: Tri): boolean | null {
  return tri === "" ? null : tri === "yes";
}

function DetailsDialog({
  location,
  onClose,
  onSaved,
}: {
  location: LocationRead;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(location.name);
  const [description, setDescription] = useState(location.description ?? "");
  const [esd, setEsd] = useState<Tri>(triOf(location.esd_safe));
  const [placeable, setPlaceable] = useState<Tri>(triOf(location.is_placeable));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const trimmed = name.trim();
  const dirty =
    trimmed !== location.name ||
    description.trim() !== (location.description ?? "") ||
    esd !== triOf(location.esd_safe) ||
    placeable !== triOf(location.is_placeable);
  const guard = useDiscardGuard(dirty, onClose);

  async function save(): Promise<void> {
    if (trimmed === "") {
      setError(new Error("Give it a name — every screen here identifies a container by its name."));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await setLocationDetails(location.id, {
        name: trimmed,
        description: description.trim() === "" ? null : description.trim(),
        esd_safe: triValue(esd),
        is_placeable: triValue(placeable),
        client_op_id: uuid4(),
      });
      onSaved();
      onClose();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title="Name and description"
      onClose={guard.requestClose}
      unsaved={dirty}
      note="Nothing here is saved until you press Save. Renaming is free: a label and a tag carry the opaque code and never the name, so nothing has to be reprinted."
    >
      <form
        className="stack"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        {guard.asking && (
          <DiscardPrompt
            what="the name and description you typed"
            onKeepEditing={guard.keepEditing}
            onDiscard={guard.discard}
          />
        )}
        <label className="field">
          <span>Name</span>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
            aria-required="true"
          />
        </label>
        {trimmed === "" && (
          <p className="muted-note" style={{ margin: 0 }}>
            Give it a name — that is why Save is disabled.
          </p>
        )}
        <label className="field">
          <span>Description</span>
          <textarea
            rows={3}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </label>
        <label className="field">
          <span>ESD safe</span>
          <select value={esd} onChange={(event) => setEsd(event.target.value as Tri)}>
            <option value="">
              Inherit —{" "}
              {location.effective_esd_safe === null
                ? "nothing above here says"
                : location.effective_esd_safe
                  ? "currently ESD safe"
                  : "currently not ESD safe"}
            </option>
            <option value="yes">Yes, and everything inside it</option>
            <option value="no">No</option>
          </select>
        </label>
        <label className="field">
          <span>Stock can be put directly into it</span>
          <select value={placeable} onChange={(event) => setPlaceable(event.target.value as Tri)}>
            <option value="">Whatever this kind of container says</option>
            <option value="yes">Yes</option>
            <option value="no">No — it only holds other containers</option>
          </select>
        </label>
        <p className="muted-note" style={{ margin: 0 }}>
          "No" keeps auto-assignment from ever proposing this as somewhere to put a part, which is
          what you want for a room or a rack. Inherit hands both answers back: ESD to the nearest
          container above that states one, placeability to this container's type.
        </p>

        <ErrorBanner error={error} fallback="Those details were not saved." />
        <div className="row">
          <span className="muted-note" style={{ flex: 1 }}>
            {dirty ? "Not saved yet." : "Saved."}
          </span>
          <button type="button" onClick={guard.requestClose} disabled={busy}>
            Close
          </button>
          <button type="submit" className="primary" disabled={busy || !dirty || trimmed === ""}>
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

// --------------------------------------------------------------- picture ----

/**
 * The real photo and the cheap pictogram — see `ContainerPhoto`'s docstring for
 * why they are two different things with two different costs.
 *
 * Both save on the spot, which the panel says out loud. There is no draft to
 * lose, so there is nothing to guard.
 */
function PictureDialog({
  location,
  onClose,
  onSaved,
}: {
  location: LocationRead;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [glyph, setGlyph] = useState(location.glyph ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function saveGlyph(next: string): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await setLocationGlyph(location.id, {
        glyph: next === "" ? null : (next as ContainerGlyph),
        client_op_id: uuid4(),
      });
      setGlyph(next);
      onSaved();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  async function uploadPhoto(file: File): Promise<void> {
    await uploadDocument(file, {
      mediaType: file.type !== "" ? file.type : "image/jpeg",
      kind: "photo",
      role: "photo",
      locationId: location.id,
      isPrimary: true,
    });
    onSaved();
  }

  async function removePhoto(): Promise<void> {
    if (location.photo === null) {
      return;
    }
    await detachLocationDocument(location.id, location.photo.sha256);
    onSaved();
  }

  return (
    <Dialog
      title="Picture"
      onClose={onClose}
      note="Saved as soon as you choose — there is no draft here to lose."
    >
      <div className="stack">
        <ContainerPhotoPanel
          displayPhoto={location.effective_photo}
          ownPhoto={location.photo}
          glyph={location.effective_glyph}
          note={
            location.photo === null && location.effective_photo !== null
              ? "Inherited from the container type's photo. Uploading one here overrides it just " +
                "for this one container."
              : null
          }
          onUpload={uploadPhoto}
          onRemoveOwn={location.photo === null ? null : removePhoto}
        />
        <label className="field">
          <span>Pictogram in the map view</span>
          <select
            value={glyph}
            disabled={busy}
            onChange={(event) => void saveGlyph(event.target.value)}
          >
            <option value="">
              {location.effective_glyph === null
                ? "None chosen"
                : `Use the container type's — currently ${glyphLabel(location.effective_glyph) ?? "none"}`}
            </option>
            {ALL_GLYPHS.map((value) => (
              <option key={value} value={value}>
                {glyphLabel(value)}
              </option>
            ))}
          </select>
        </label>
        <p className="muted-note" style={{ margin: 0 }}>
          The pictogram is what the map draws, dozens of cells at a time; the photo is only ever
          shown for one container at a time, on this page.
        </p>
        <ErrorBanner error={error} fallback="That could not be saved." />
        <div className="row">
          <span className="spacer" />
          <button type="button" className="primary" onClick={onClose}>
            Done
          </button>
        </div>
      </div>
    </Dialog>
  );
}

// ---------------------------------------------------------------- layout ----

function LayoutDialog({
  location,
  onClose,
  onSaved,
}: {
  location: LocationRead;
  onClose: () => void;
  onSaved: () => void;
}) {
  // The draft lives inside the panel, so "is there unsent work" has to come back
  // up to the frame that owns the close button and the titlebar.
  const [dirty, setDirty] = useState(false);
  const guard = useDiscardGuard(dirty, onClose);

  return (
    <Dialog
      title="Slots inside this container"
      onClose={guard.requestClose}
      unsaved={dirty}
      note="Add, relabel, merge or remove the positions inside this one container. Nothing is saved until you press Save, and a slot that still holds stock or a bound tag blocks the change rather than losing it."
    >
      <div className="stack">
        {guard.asking && (
          <DiscardPrompt
            what="the layout you have drawn"
            onKeepEditing={guard.keepEditing}
            onDiscard={guard.discard}
          />
        )}
        <SlotLayoutPanel location={location} onSaved={onSaved} onDirtyChange={setDirty} />
        <div className="row">
          <span className="spacer" />
          <button type="button" onClick={guard.requestClose}>
            Close
          </button>
        </div>
      </div>
    </Dialog>
  );
}

// ------------------------------------------------------------------- add ----

function AddDialog({
  location,
  onClose,
  onCreated,
}: {
  location: LocationRead;
  onClose: () => void;
  onCreated: () => void;
}) {
  const types = useAsync<readonly ContainerTypeRead[]>(() => listContainerTypes(), []);

  return (
    <Dialog
      title={`Add containers inside ${location.name}`}
      onClose={onClose}
      note="Drawers, trays or bins that live in here are containers of their own. A type stamps one out; everything after that is edited here."
    >
      <div className="stack">
        {types.error !== null && (
          <ErrorBanner error={types.error} fallback="The container types could not be loaded." />
        )}
        {types.data === null && types.error === null ? (
          <Loading what="the container types" />
        ) : (
          <AddContainersPanel
            types={types.data ?? []}
            parentId={location.id}
            parentLabel={location.label_path}
            onCreated={onCreated}
          />
        )}
        {location.is_placeable === false && (
          <Notice kind="info" title="This one only holds other containers">
            Which is exactly what it is for — stock goes in the containers you add here, not in
            this.
          </Notice>
        )}
        <div className="row">
          <span className="spacer" />
          <button type="button" onClick={onClose}>
            Done
          </button>
        </div>
      </div>
    </Dialog>
  );
}
