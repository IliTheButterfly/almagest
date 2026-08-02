/**
 * One form, used for authoring a container type and for editing one.
 *
 * **Shared rather than duplicated on purpose.** The thing this form has to get
 * across is ADR 0002's decoupling, and a create form that phrased the two
 * questions differently from the edit form would teach two different mental
 * models of the same columns. So the legends are the ADR's two questions,
 * verbatim, in both places:
 *
 * > | Question | Expressed as |
 * > |---|---|
 * > | What grid do I present to my children? | `grid_rows`, `grid_cols`, `grid_pitch_mm`, `grid_height_unit_mm` |
 * > | What footprint do I occupy in my parent's grid? | `footprint_cols`, `footprint_rows`, `footprint_height_u` |
 *
 * They are two fieldsets and never adjacent columns of one row, because the
 * failure this form exists to prevent is a user reading "rows / columns" twice
 * and filling one of them in with the other's answer. A Gridfinity bin is the
 * example given in both legends' notes for the same reason it is the ADR's
 * reference case: it answers both at once, and the answers are unrelated.
 *
 * What is deliberately **not** here:
 *
 * - the slot canvas (merges, relabels, size classes). That is
 *   `.../slot-template`'s single door and already has an editor — `LayoutEditor`,
 *   reached from `ContainerTypeScreen` — so this form links to it rather than
 *   growing a second way to write the same rows.
 * - `is_seed`, which the API cannot accept from any client: seeding is a data
 *   migration.
 * - `materialize_slots`, which is a consequence of what the canvas was asked to
 *   do, not a switch.
 */

import { useState } from "react";

import { ALL_GLYPHS, glyphLabel } from "../lib/locations/glyphs";
import { VIEW_LABELS, known, type ChildView } from "../lib/locations/views";
import {
  draftProblems,
  slugify,
  type TypeDraft,
} from "../lib/containers/typeDraft";
import type { CapacityModel, ChildLayout, SlotLabelScheme } from "../lib/api/client";

/** Honest wording, including for the model that has no formula yet. */
const CAPACITY_LABELS: Readonly<Record<CapacityModel, string>> = {
  none: "Nothing to measure — a room or a shelf is never full",
  slots: "Compartments — a count of slots",
  volume: "Volume — worked out from the inner dimensions below",
  positions: "Positions — a reel or tube rack, split by pitch",
  mass: "Mass — reserved, and not implemented: fill will read as unsupported",
  grid_units: "Grid units — the area of the grid it offers (Gridfinity)",
};

const LAYOUT_LABELS: Readonly<Record<ChildLayout, string>> = {
  grid: "A grid — addressable row/column positions",
  list: "A list — an order, but no positions",
  none: "Nothing goes inside it",
};

const SCHEME_LABELS: Readonly<Record<SlotLabelScheme, string>> = {
  row_alpha_col_num: "A1, B2, … — rows as letters, columns as numbers",
  sequential: "1, 2, 3, … — one running number",
  custom: "Custom — every label set by hand on the canvas",
};

export interface ContainerTypeFormProps {
  readonly initial: TypeDraft;
  /** `create` also asks for the slug, which has no `PATCH` counterpart and so
   * can only ever be chosen once. */
  readonly mode: "create" | "edit";
  /**
   * True when the row being edited is a seed, so saving will **clone it** rather
   * than change it (`layout_authoring.ensure_editable`). Said before the button
   * is pressed and spelled out on the button itself — a change that quietly
   * produces a different row than the one on screen is exactly the kind of thing
   * a confirmation has to name rather than ask "are you sure" about.
   */
  readonly clonesOnSave: boolean;
  readonly busy: boolean;
  /**
   * `effective_child_view`, so the "work it out" option can say what it
   * currently works out to instead of being a leap of faith.
   *
   * `null` on the create path, where there is no answer to report yet: the
   * derivation is a **server** rule (`app.services.views.derive_child_view`) and
   * recomputing it here to fill this label in would be a second copy of it, free
   * to disagree with the one that decides. Saying nothing is better than saying
   * something that might be wrong about a picture the user is about to see.
   */
  readonly derivedChildView: string | null;
  readonly onSubmit: (draft: TypeDraft) => void;
  readonly onCancel: (() => void) | null;
}

export function ContainerTypeForm({
  initial,
  mode,
  clonesOnSave,
  busy,
  derivedChildView,
  onSubmit,
  onCancel,
}: ContainerTypeFormProps) {
  const [draft, setDraft] = useState<TypeDraft>(initial);
  // Stops deriving the slug the moment it is typed in, so a suggestion never
  // overwrites a deliberate choice.
  const [slugTouched, setSlugTouched] = useState(mode === "edit");

  function set<K extends keyof TypeDraft>(key: K, value: TypeDraft[K]): void {
    setDraft({ ...draft, [key]: value });
  }

  function setName(value: string): void {
    setDraft({
      ...draft,
      displayName: value,
      slug: slugTouched ? draft.slug : slugify(value),
    });
  }

  const problems = draftProblems(draft, { requireSlug: mode === "create" });

  return (
    <form
      className="stack"
      onSubmit={(event) => {
        event.preventDefault();
        if (problems.length === 0) {
          onSubmit(draft);
        }
      }}
    >
      <fieldset className="fieldgroup">
        <legend>What is it?</legend>
        <label className="field">
          <span>Name</span>
          <input value={draft.displayName} onChange={(event) => setName(event.target.value)} />
        </label>
        {mode === "create" ? (
          <label className="field">
            <span>Slug — the short permanent id, and it cannot be changed later</span>
            <input
              className="mono"
              value={draft.slug}
              onChange={(event) => {
                setSlugTouched(true);
                set("slug", event.target.value);
              }}
              placeholder="raaco-c8-30"
              autoComplete="off"
              spellCheck={false}
            />
          </label>
        ) : (
          <p className="muted-note" style={{ margin: 0 }}>
            Slug <span className="mono">{draft.slug}</span> — permanent, so it is not editable
            here. Clone the type if you want a different one.
          </p>
        )}
        <label className="field">
          <span>Description</span>
          <textarea
            rows={2}
            value={draft.description}
            onChange={(event) => set("description", event.target.value)}
          />
        </label>
        <label className="field">
          <span>Pictogram in the map view</span>
          <select value={draft.glyph} onChange={(event) => set("glyph", event.target.value)}>
            <option value="">No glyph — draws a neutral placeholder</option>
            {ALL_GLYPHS.map((value) => (
              <option key={value} value={value}>
                {glyphLabel(value)}
              </option>
            ))}
          </select>
        </label>
        <p className="muted-note" style={{ margin: 0 }}>
          A pictogram is drawn at every node of the dense map, where a real photo per
          cell would be too slow to load. A photo is separate and lives on this type's own
          screen — one container can override either.
        </p>
      </fieldset>

      {/* ADR 0002, question one. */}
      <fieldset className="fieldgroup">
        <legend>What grid does it offer the things inside it?</legend>
        <p className="muted-note" style={{ margin: 0 }}>
          The slots it presents to whatever goes in — a cabinet's drawer positions, a
          baseplate's cells. Nothing to do with how big the container itself is: a Gridfinity
          bin offers its own 1 x 3 of dividers while taking up 2 x 1 of the plate under it.
        </p>
        <label className="field">
          <span>What goes inside</span>
          <select
            value={draft.presentsLayout}
            onChange={(event) => set("presentsLayout", event.target.value as ChildLayout)}
          >
            {(Object.keys(LAYOUT_LABELS) as ChildLayout[]).map((value) => (
              <option key={value} value={value}>
                {LAYOUT_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
        <div className="fields">
          <label className="field">
            <span>Rows it offers</span>
            <input
              inputMode="numeric"
              value={draft.presentsRows}
              onChange={(event) => set("presentsRows", event.target.value)}
            />
          </label>
          <label className="field">
            <span>Columns it offers</span>
            <input
              inputMode="numeric"
              value={draft.presentsCols}
              onChange={(event) => set("presentsCols", event.target.value)}
            />
          </label>
        </div>
        <div className="fields">
          <label className="field">
            <span>Grid pitch (mm)</span>
            <input
              inputMode="decimal"
              value={draft.presentsPitchMm}
              onChange={(event) => set("presentsPitchMm", event.target.value)}
              placeholder="42"
            />
          </label>
          <label className="field">
            <span>Height unit (mm)</span>
            <input
              inputMode="decimal"
              value={draft.presentsHeightUnitMm}
              onChange={(event) => set("presentsHeightUnitMm", event.target.value)}
              placeholder="7"
            />
          </label>
        </div>
        <p className="muted-note" style={{ margin: 0 }}>
          A pitch is only for a measured grid — Gridfinity is 42 mm, 7 mm per height unit.
          Leave both blank for a cabinet, where the drawers are whatever size they are. A
          pitch that disagrees with the pitch of the things placed on it is refused outright
          rather than flagged, because a 42 mm bin does not physically seat on a 50 mm plate.
        </p>
        <label className="field">
          <span>How its slots are labelled</span>
          <select
            value={draft.slotLabelScheme}
            onChange={(event) => set("slotLabelScheme", event.target.value as SlotLabelScheme)}
          >
            {(Object.keys(SCHEME_LABELS) as SlotLabelScheme[]).map((value) => (
              <option key={value} value={value}>
                {SCHEME_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
        {draft.slotLabelScheme === "sequential" && (
          <label className="field">
            <span>Pad the numbers to this many digits</span>
            <input
              inputMode="numeric"
              value={draft.slotLabelZeroPad}
              onChange={(event) => set("slotLabelZeroPad", event.target.value)}
              placeholder="2 gives 01, 02, 03"
            />
          </label>
        )}
      </fieldset>

      {/* ADR 0002, question two. */}
      <fieldset className="fieldgroup">
        <legend>What space does it take up in whatever it sits in?</legend>
        <p className="muted-note" style={{ margin: 0 }}>
          Its footprint in its parent's grid, in that parent's units — not millimetres, and
          not the same numbers as above. A 2 x 1 Gridfinity bin takes up two cells of the
          baseplate. Leave blank for something that is not placed into a measured grid at
          all, like a cabinet standing on the floor.
        </p>
        <div className="fields">
          <label className="field">
            <span>Columns it takes up</span>
            <input
              inputMode="numeric"
              value={draft.occupiesCols}
              onChange={(event) => set("occupiesCols", event.target.value)}
            />
          </label>
          <label className="field">
            <span>Rows it takes up</span>
            <input
              inputMode="numeric"
              value={draft.occupiesRows}
              onChange={(event) => set("occupiesRows", event.target.value)}
            />
          </label>
          <label className="field">
            <span>Height in units</span>
            <input
              inputMode="numeric"
              value={draft.occupiesHeightU}
              onChange={(event) => set("occupiesHeightU", event.target.value)}
            />
          </label>
        </div>
      </fieldset>

      <fieldset className="fieldgroup">
        <legend>When does it count as full?</legend>
        <p className="muted-note" style={{ margin: 0 }}>
          Advisory in every case. An over-capacity put-away is accepted and the container is
          flagged, never refused — a scan that gets rejected teaches you to stop scanning.
        </p>
        <label className="field">
          <span>How fullness is measured</span>
          <select
            value={draft.capacityModel}
            onChange={(event) => set("capacityModel", event.target.value as CapacityModel)}
          >
            {(Object.keys(CAPACITY_LABELS) as CapacityModel[]).map((value) => (
              <option key={value} value={value}>
                {CAPACITY_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
        <div className="fields">
          <label className="field">
            <span>Drawer front width (mm)</span>
            <input
              inputMode="decimal"
              value={draft.frontWidthMm}
              onChange={(event) => set("frontWidthMm", event.target.value)}
            />
          </label>
          <label className="field">
            <span>Drawer front height (mm)</span>
            <input
              inputMode="decimal"
              value={draft.frontHeightMm}
              onChange={(event) => set("frontHeightMm", event.target.value)}
            />
          </label>
        </div>
        <p className="muted-note" style={{ margin: 0 }}>
          The face a label goes on, not the compartment inside. Without both, no card can
          be printed for anything of this type — the sheet is sized from these minus the
          label lip, so there is nothing to guess from. Measure the drawer front, not the
          existing card.
        </p>
        {draft.capacityModel === "slots" && (
          <label className="field">
            <span>How many compartments</span>
            <input
              inputMode="numeric"
              value={draft.capacitySlots}
              onChange={(event) => set("capacitySlots", event.target.value)}
            />
          </label>
        )}
        {draft.capacityModel === "volume" && (
          <>
            <div className="fields">
              <label className="field">
                <span>Inner length (mm)</span>
                <input
                  inputMode="decimal"
                  value={draft.innerLengthMm}
                  onChange={(event) => set("innerLengthMm", event.target.value)}
                />
              </label>
              <label className="field">
                <span>Inner width (mm)</span>
                <input
                  inputMode="decimal"
                  value={draft.innerWidthMm}
                  onChange={(event) => set("innerWidthMm", event.target.value)}
                />
              </label>
              <label className="field">
                <span>Inner height (mm)</span>
                <input
                  inputMode="decimal"
                  value={draft.innerHeightMm}
                  onChange={(event) => set("innerHeightMm", event.target.value)}
                />
              </label>
            </div>
            <p className="muted-note" style={{ margin: 0 }}>
              Volume needs all three. Leave any of them blank and fill will read as "not
              measured" rather than as empty — which is the truth, but not what you meant.
            </p>
          </>
        )}
        {draft.capacityModel === "grid_units" && (
          <p className="muted-note" style={{ margin: 0 }}>
            Measured against the rows and columns it offers, above, as an area: a 2 x 1 bin
            consumes two units, not one slot.
          </p>
        )}
      </fieldset>

      {/* ADR 0006 — a third, independent axis, and the note says why it is not
          folded into the geometry above. */}
      <fieldset className="fieldgroup">
        <legend>How is it drawn?</legend>
        <label className="field">
          <span>Picture used for its contents</span>
          <select value={draft.childView} onChange={(event) => set("childView", event.target.value)}>
            <option value="">
              {derivedChildView === null
                ? "Work it out from the answers above"
                : `Work it out from the answers above — currently ${VIEW_LABELS[
                    known(derivedChildView)
                  ].toLowerCase()}`}
            </option>
            {(Object.keys(VIEW_LABELS) as ChildView[]).map((kind) => (
              <option key={kind} value={kind}>
                {VIEW_LABELS[kind]}
              </option>
            ))}
          </select>
        </label>
        <p className="muted-note" style={{ margin: 0 }}>
          The picture only. It never moves a slot, never changes what fits, and never decides
          where a scan can land — a cabinet and a Gridfinity baseplate offer the same grid and
          look nothing alike. One container can still be drawn differently on its own screen.
        </p>
        <label className="check">
          <input
            type="checkbox"
            checked={draft.isPlaceable}
            onChange={(event) => set("isPlaceable", event.target.checked)}
          />
          <span>Stock can be put directly into it</span>
        </label>
        <p className="muted-note" style={{ margin: 0 }}>
          Turn this off for something that only holds other containers — a room, a rack, a
          cabinet carcass. Auto-assignment skips it, and nothing lands there by accident.
        </p>
      </fieldset>

      {clonesOnSave && (
        <div className="notice notice-info">
          <h3>Saving will make you a copy</h3>
          <p style={{ margin: 0 }}>
            This is a seed type — part of the shared library every fresh install starts
            with — so it is not edited in place. Saving writes a separate new type with
            these values under its own slug, leaves the original exactly as it is, and takes
            you to the copy. Nothing already built from the original changes either way.
          </p>
        </div>
      )}

      {problems.length > 0 && (
        <div className="notice notice-warn">
          <h3>Not ready to save</h3>
          <ul className="list">
            {problems.map((problem) => (
              <li key={problem} className="sub">
                {problem}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="row">
        {onCancel !== null && (
          <button type="button" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
        )}
        <span className="spacer" />
        <button type="submit" className="primary" disabled={busy || problems.length > 0}>
          {busy
            ? "Saving…"
            : clonesOnSave
              ? "Save as my own copy"
              : mode === "create"
                ? "Create this type"
                : "Save changes"}
        </button>
      </div>
    </form>
  );
}
