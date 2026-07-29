/**
 * `/containers/new` — add real containers to storage.
 *
 * A container *type* is a template; nothing can be put in one. This is the step
 * that turns a type into a cabinet standing in a room, which is why it is a
 * first-class screen with its own URL rather than a panel folded into the type
 * editor: it is reached from the storage tree, from a container's own screen, and
 * from a type, and all three arrive with a different amount already decided
 * (`?parent=`, `?type=`, or neither).
 *
 * **Two ways in, because the API genuinely has two.**
 *
 * - *Stamp from a type* → `POST /api/locations/{id}/instantiate`. Materialises
 *   the type's current layout into each new container's own child locations, and
 *   is the only route that does — so this is what you want for anything with
 *   compartments. It needs a parent: there is no route that instantiates a type
 *   at the top of the tree.
 * - *One plain container* → `POST /api/locations`. No slots, no layout, parent
 *   optional. That last part is why it is here rather than being a lesser
 *   version of the first: a fresh install has nowhere to stamp *into*, so the
 *   room or bench that everything else hangs off has to be creatable somehow.
 *
 * Errors the routes really return are handled as themselves rather than as "that
 * failed": a 422 `bad_naming_pattern` (only `{n}` may be substituted), and a 409
 * naming a hard geometric incompatibility — `pitch_mismatch`,
 * `footprint_too_wide`, `footprint_too_deep`. The second is the one refusal in
 * the capacity area that is *not* advisory, and the message says why.
 */

import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ContainerPhoto } from "../components/ContainerPhoto";
import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import {
  createLocation,
  instantiateContainers,
  listContainerTypes,
  getLocationTree,
  type ContainerGlyph,
  type ContainerTypeRead,
  type LocationRead,
  type LocationTree,
  type TagGranularity,
} from "../lib/api/client";
import { namingProblem, previewNames, summariseNames } from "../lib/containers/naming";
import { describeOccupies, describePresents } from "../lib/containers/typeDraft";
import { useAsync } from "../lib/hooks/useAsync";
import { ALL_GLYPHS, glyphLabel } from "../lib/locations/glyphs";
import { uuid4 } from "../lib/scan/session";
import { formatShortId } from "../lib/shortid";

interface Loaded {
  readonly types: readonly ContainerTypeRead[];
  readonly tree: LocationTree;
}

export function NewContainersScreen() {
  const bundle = useAsync<Loaded>(
    () =>
      Promise.all([listContainerTypes(), getLocationTree()]).then(([types, tree]) => ({
        types,
        tree,
      })),
    [],
  );

  if (bundle.error !== null) {
    return <ErrorBanner error={bundle.error} fallback="The container types could not be loaded." />;
  }
  if (bundle.data === null) {
    return <Loading what="the container types and the storage tree" />;
  }
  return <AddContainers types={bundle.data.types} tree={bundle.data.tree} />;
}

function AddContainers({
  types,
  tree,
}: {
  types: readonly ContainerTypeRead[];
  tree: LocationTree;
}) {
  const [params] = useSearchParams();
  const parentParam = Number(params.get("parent"));
  const typeParam = Number(params.get("type"));

  const knownParent = tree.nodes.some((node) => node.id === parentParam) ? parentParam : null;
  const knownType = types.some((type) => type.id === typeParam) ? typeParam : null;

  const [mode, setMode] = useState<"stamp" | "plain">(types.length === 0 ? "plain" : "stamp");
  const [parentId, setParentId] = useState<number | null>(knownParent);
  const [created, setCreated] = useState<readonly LocationRead[] | null>(null);

  const parentName = useMemo(
    () => tree.nodes.find((node) => node.id === parentId)?.label_path ?? null,
    [tree.nodes, parentId],
  );

  if (created !== null) {
    return (
      <div className="stack">
        <Notice kind="ok" title={`${created.length} container(s) created`}>
          <p style={{ margin: 0 }}>
            Each one holds its own copy of the layout from this moment on — editing the type
            afterwards will not reach into any of them.
          </p>
        </Notice>
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
        <div className="card">
          <div className="row">
            <button type="button" onClick={() => setCreated(null)}>
              Add more
            </button>
            <span className="spacer" />
            <Link to={parentId === null ? "/tree" : `/tree?at=${parentId}`}>
              See them in storage →
            </Link>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <Link to={parentId === null ? "/tree" : `/locations/${parentId}`}>← storage</Link>
        </div>
        <h1>Add containers</h1>
        <p className="muted-note" style={{ margin: 0 }}>
          Real containers this time, not templates: a cabinet you can open, a drawer you can
          put a part in.
        </p>
        <div className="segmented" role="group" aria-label="How to add">
          <button type="button" aria-pressed={mode === "stamp"} onClick={() => setMode("stamp")}>
            From a type
          </button>
          <button type="button" aria-pressed={mode === "plain"} onClick={() => setMode("plain")}>
            One plain container
          </button>
        </div>
      </div>

      <div className="card">
        <label className="field">
          <span>{mode === "stamp" ? "Put them inside" : "Put it inside"}</span>
          <select
            value={parentId === null ? "" : String(parentId)}
            onChange={(event) =>
              setParentId(event.target.value === "" ? null : Number(event.target.value))
            }
          >
            <option value="">
              {mode === "stamp"
                ? "— pick where they go —"
                : "Nowhere — a new top-level container"}
            </option>
            {tree.nodes.map((node) => (
              <option key={node.id} value={String(node.id)}>
                {node.label_path}
              </option>
            ))}
          </select>
        </label>
        {parentName !== null && (
          <p className="muted-note" style={{ margin: 0 }}>
            Inside <span className="mono">{parentName}</span>. A container is not tied to
            where it starts — moving it later is a move, not a rebuild, because nothing about
            the hierarchy is written on its label or its tag.
          </p>
        )}
        {mode === "plain" && parentId === null && (
          <p className="muted-note" style={{ margin: 0 }}>
            A top-level container is the room, bench or wall everything else hangs off. If
            storage is empty, this is where to start.
          </p>
        )}
      </div>

      {mode === "stamp" ? (
        <StampFromType
          types={types}
          initialTypeId={knownType}
          parentId={parentId}
          onCreated={setCreated}
        />
      ) : (
        <PlainContainer parentId={parentId} onCreated={(location) => setCreated([location])} />
      )}
    </div>
  );
}

function StampFromType({
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
          onChange={(event) => setTypeId(event.target.value === "" ? null : Number(event.target.value))}
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
          <input inputMode="numeric" value={count} onChange={(event) => setCount(event.target.value)} />
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
        Tagging only the container cuts most of the physical labour and is the right call
        whenever the pick already involves a screen. Per-slot tags earn their cost when the
        drawers themselves travel to the bench. Either way a slot can be given one later.
      </p>

      {parentId === null && (
        <Notice kind="info" title="Pick where they go first">
          Stamping a type materialises its slots, and slots have to hang off something. To
          create the outermost container, switch to "One plain container" above.
        </Notice>
      )}

      <ErrorBanner error={error} fallback="Those containers could not be created." />
      <button type="submit" className="primary wide" disabled={busy || !ready}>
        {busy ? "Creating…" : `Create ${validCount ? parsedCount : 1} container(s)`}
      </button>
    </form>
  );
}

function PlainContainer({
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
        No type, and therefore no slots — a room, a bench, a shelf, a box. Give it a type
        later if it turns out to have compartments worth addressing.
      </p>
      <label className="field">
        <span>Name</span>
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          autoFocus
          required
          aria-required="true"
        />
      </label>
      {nameEmpty && (
        <p className="muted-note">
          Give it a name — that is why "Create it" below is disabled.
        </p>
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
        Turn that off for a room or a rack that only holds other containers — auto-assignment
        will then never propose it as somewhere to put a part.
      </p>
      <ErrorBanner error={error} fallback="That container could not be created." />
      <button type="submit" className="primary wide" disabled={busy || nameEmpty}>
        {busy ? "Creating…" : "Create it"}
      </button>
    </form>
  );
}
