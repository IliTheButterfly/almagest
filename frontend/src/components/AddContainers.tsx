/**
 * Putting real containers inside a container — the two forms, and the panel that
 * offers both where you are already standing.
 *
 * **A template is a starting point, and that is all it is.** Stamping from a
 * container type copies its layout into real `locations` rows once
 * (`POST /api/locations/{id}/instantiate`); nothing afterwards links the instance
 * back to the type, so every later change — relabel a slot, merge two cells, add
 * a drawer — happens in place on the container's own page. That is Iliana's
 * "templates should only be used as a starting point, the rest of the editing is
 * done in place", and the backend already worked this way; what was missing was a
 * surface that did not send you to another page to use it.
 *
 * The two forms are here rather than on a screen because there are two callers
 * with genuinely different amounts already decided:
 *
 * - the container's own page, in edit mode, where the parent **is** the page you
 *   are on — `AddContainersPanel`, which asks nothing it already knows;
 * - `/containers/new`, reached from a container *type* ("create containers from
 *   this"), which knows the type and has to ask where they go.
 *
 * Neither form invents a parent for `instantiate`: slots have to hang off
 * something, and the API has no route that stamps a type at the top of the tree.
 * A plain container is the one thing that can be created with no parent at all,
 * which is why a fresh, empty install can be started from either caller.
 */

import { useState } from "react";
import { Link } from "react-router-dom";

import { ContainerPhoto } from "./ContainerPhoto";
import { ErrorBanner, Notice } from "./Feedback";
import {
  createLocation,
  instantiateContainers,
  type ContainerGlyph,
  type ContainerTypeRead,
  type LocationRead,
  type TagGranularity,
} from "../lib/api/client";
import { namingProblem, previewNames, summariseNames } from "../lib/containers/naming";
import { describeOccupies, describePresents } from "../lib/containers/typeDraft";
import { ALL_GLYPHS, glyphLabel } from "../lib/locations/glyphs";
import { uuid4 } from "../lib/scan/session";
import { formatShortId } from "../lib/shortid";

export function StampFromType({
  types,
  initialTypeId,
  parentId,
  onCreated,
}: {
  types: readonly ContainerTypeRead[];
  initialTypeId: number | null;
  parentId: number | null;
  onCreated: (locations: readonly LocationRead[]) => void;
}) {
  const [typeId, setTypeId] = useState<number | null>(initialTypeId ?? types[0]?.id ?? null);
  const [count, setCount] = useState("1");
  const [pattern, setPattern] = useState("");
  const [granularity, setGranularity] = useState<TagGranularity>("container");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const type = types.find((candidate) => candidate.id === typeId) ?? null;
  const parsedCount = Number(count);
  const validCount = Number.isInteger(parsedCount) && parsedCount >= 1 && parsedCount <= 1000;
  const effectivePattern = pattern.trim() === "" ? (type?.display_name ?? "") : pattern.trim();
  const patternIssue = namingProblem(effectivePattern);
  const names = previewNames(effectivePattern, validCount ? parsedCount : 1);
  const ready = type !== null && parentId !== null && validCount && patternIssue === null;

  async function save(): Promise<void> {
    if (!ready || parentId === null || type === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onCreated(
        (
          await instantiateContainers(parentId, {
            container_type_id: type.id,
            count: parsedCount,
            naming_pattern: effectivePattern,
            tag_granularity: granularity,
            client_op_id: uuid4(),
          })
        ).locations,
      );
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  if (types.length === 0) {
    return (
      <Notice kind="info" title="No container types exist yet">
        <p style={{ margin: 0 }}>
          <Link to="/container-types">Design one, or clone a seed →</Link>
        </p>
      </Notice>
    );
  }

  return (
    <form
      className="card"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <h3>Stamp from a type</h3>
      <label className="field">
        <span>Which type</span>
        <select
          value={typeId === null ? "" : String(typeId)}
          onChange={(event) =>
            setTypeId(event.target.value === "" ? null : Number(event.target.value))
          }
        >
          {types.map((candidate) => (
            <option key={candidate.id} value={String(candidate.id)}>
              {candidate.display_name}
              {candidate.is_seed ? " (seed)" : ""}
            </option>
          ))}
        </select>
      </label>

      {type !== null && (
        <div className="row">
          <ContainerPhoto photo={null} glyph={type.glyph} alt="" />
          <span className="muted-note" style={{ flex: 1 }}>
            {describePresents(type)} · {describeOccupies(type)} · fullness by{" "}
            <span className="mono">{type.capacity_model}</span>
          </span>
          <Link to={`/container-types/${type.id}`}>Look at it →</Link>
        </div>
      )}

      <div className="fields">
        <label className="field">
          <span>How many</span>
          <input
            inputMode="numeric"
            value={count}
            onChange={(event) => setCount(event.target.value)}
          />
        </label>
        <label className="field">
          <span>Name them</span>
          <input
            value={pattern}
            onChange={(event) => setPattern(event.target.value)}
            placeholder={type?.display_name ?? "Cabinet {n}"}
          />
        </label>
      </div>
      <p className="muted-note" style={{ margin: 0 }}>
        {"Write {n} where the number goes. Leave it out and a number is appended anyway when "}
        you ask for more than one, so two containers never share a name.
      </p>
      {patternIssue !== null ? (
        <Notice kind="warn" title="That name pattern will be refused">
          {patternIssue}
        </Notice>
      ) : (
        names.length > 0 && (
          <p className="muted-note" style={{ margin: 0 }}>
            They will be called: <span className="mono">{summariseNames(names)}</span>
          </p>
        )
      )}
      {!validCount && (
        <p className="muted-note" style={{ margin: 0 }}>
          How many has to be a whole number from 1 to 1000.
        </p>
      )}

      <label className="field">
        <span>What gets a printed id</span>
        <select
          value={granularity}
          onChange={(event) => setGranularity(event.target.value as TagGranularity)}
        >
          <option value="container">Just the container — pick the slot on screen</option>
          <option value="slot">Every slot as well, so each drawer can carry its own tag</option>
        </select>
      </label>
      <p className="muted-note" style={{ margin: 0 }}>
        Tagging only the container cuts most of the physical labour and is the right call whenever
        the pick already involves a screen. Per-slot tags earn their cost when the drawers
        themselves travel to the bench. Either way a slot can be given one later.
      </p>
      <p className="muted-note" style={{ margin: 0 }}>
        The type's layout is copied in, once. From then on this container owns it: relabelling a
        slot, merging two of them or adding a drawer all happen here, and editing the type later
        reaches none of it.
      </p>

      {parentId === null && (
        <Notice kind="info" title="Pick where they go first">
          Stamping a type materialises its slots, and slots have to hang off something. To create
          the outermost container, switch to "One plain container" above.
        </Notice>
      )}

      <ErrorBanner error={error} fallback="Those containers could not be created." />
      <button type="submit" className="primary wide" disabled={busy || !ready}>
        {busy ? "Creating…" : `Create ${validCount ? parsedCount : 1} container(s)`}
      </button>
    </form>
  );
}

export function PlainContainer({
  parentId,
  onCreated,
}: {
  parentId: number | null;
  onCreated: (location: LocationRead) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [glyph, setGlyph] = useState("");
  const [placeable, setPlaceable] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const nameEmpty = name.trim() === "";

  async function save(): Promise<void> {
    if (nameEmpty) {
      // "Create it" is disabled for the same reason; this guards a stray
      // Enter-key submit from going nowhere with no explanation.
      setError(new Error("Give it a name — a container cannot be created without one."));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const response = await createLocation({
        name: name.trim(),
        parent_id: parentId,
        description: description.trim() === "" ? null : description.trim(),
        glyph: glyph === "" ? null : (glyph as ContainerGlyph),
        is_placeable: placeable,
        client_op_id: uuid4(),
      });
      onCreated(response.location);
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
      <h3>One plain container</h3>
      <p className="muted-note" style={{ margin: 0 }}>
        No type, and therefore no slots — a room, a bench, a shelf, a box. Give it a type later if
        it turns out to have compartments worth addressing.
      </p>
      <label className="field">
        <span>Name</span>
        <input value={name} onChange={(event) => setName(event.target.value)} required aria-required="true" />
      </label>
      {nameEmpty && (
        <p className="muted-note">Give it a name — that is why "Create it" below is disabled.</p>
      )}
      <label className="field">
        <span>Description</span>
        <input value={description} onChange={(event) => setDescription(event.target.value)} />
      </label>
      <label className="field">
        <span>Pictogram in the map view</span>
        <select value={glyph} onChange={(event) => setGlyph(event.target.value)}>
          <option value="">No glyph</option>
          {ALL_GLYPHS.map((value) => (
            <option key={value} value={value}>
              {glyphLabel(value)}
            </option>
          ))}
        </select>
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={placeable}
          onChange={(event) => setPlaceable(event.target.checked)}
        />
        <span>Stock can be put directly into it</span>
      </label>
      <p className="muted-note" style={{ margin: 0 }}>
        Turn that off for a room or a rack that only holds other containers — auto-assignment will
        then never propose it as somewhere to put a part.
      </p>
      <ErrorBanner error={error} fallback="That container could not be created." />
      <button type="submit" className="primary wide" disabled={busy || nameEmpty}>
        {busy ? "Creating…" : "Create it"}
      </button>
    </form>
  );
}

/** What was just made, with a way into each one. Shared by both callers. */
export function CreatedContainers({ created }: { created: readonly LocationRead[] }) {
  return (
    <ul className="list">
      {created.map((location) => (
        <li key={location.id}>
          <Link className="list-item" to={`/locations/${location.id}`}>
            <div className="row">
              <span className="title" style={{ flex: 1 }}>
                {location.name}
              </span>
              {location.short_id !== null && (
                <span className="badge mono">{formatShortId(location.short_id)}</span>
              )}
            </div>
            <div className="sub">{location.label_path}</div>
            <div className="sub">
              {location.child_count === 0
                ? "no slots inside"
                : `${location.child_count} slot(s) inside`}
            </div>
          </Link>
        </li>
      ))}
    </ul>
  );
}

/**
 * "Add containers in here", for a parent that is already decided — the panel the
 * edit mode opens.
 *
 * `parentId === null` is the top of the tree, and it is not a special case so
 * much as a fact about the API: `instantiate` needs somewhere to hang slots, so
 * the plain form is the only one offered there. Everything else is identical at
 * every depth, which is the point.
 */
export function AddContainersPanel({
  types,
  parentId,
  parentLabel,
  initialTypeId = null,
  onCreated,
}: {
  types: readonly ContainerTypeRead[];
  parentId: number | null;
  parentLabel: string | null;
  initialTypeId?: number | null;
  onCreated: () => void;
}) {
  const stampable = parentId !== null && types.length > 0;
  const [mode, setMode] = useState<"stamp" | "plain">(stampable ? "stamp" : "plain");
  const [created, setCreated] = useState<readonly LocationRead[]>([]);

  function record(locations: readonly LocationRead[]): void {
    setCreated((before) => [...before, ...locations]);
    onCreated();
  }

  return (
    <div className="stack">
      <p className="muted-note" style={{ margin: 0 }}>
        {parentLabel === null
          ? "These go at the top of the tree: the room, bench or wall everything else hangs off."
          : `These go inside ${parentLabel}.`}
      </p>
      <div className="segmented" role="group" aria-label="How to add">
        <button
          type="button"
          aria-pressed={mode === "stamp"}
          disabled={!stampable}
          onClick={() => setMode("stamp")}
        >
          From a type
        </button>
        <button type="button" aria-pressed={mode === "plain"} onClick={() => setMode("plain")}>
          One plain container
        </button>
      </div>
      {parentId === null && (
        <p className="muted-note" style={{ margin: 0 }}>
          Stamping a type materialises its slots, and slots have to hang off something — so at the
          top of the tree the plain form is the only one that can do anything.
        </p>
      )}

      {mode === "stamp" ? (
        <StampFromType
          types={types}
          initialTypeId={initialTypeId}
          parentId={parentId}
          onCreated={record}
        />
      ) : (
        <PlainContainer parentId={parentId} onCreated={(location) => record([location])} />
      )}

      {created.length > 0 && (
        <>
          <Notice kind="ok" title={`${created.length} container(s) created`}>
            <p style={{ margin: 0 }}>
              Each one holds its own copy of the layout from this moment on — editing the type
              afterwards will not reach into any of them.
            </p>
          </Notice>
          <CreatedContainers created={created} />
        </>
      )}
    </div>
  );
}
