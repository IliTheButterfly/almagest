/**
 * `/containers/new` — add real containers to storage, from a **container type**.
 *
 * A container type is a template; nothing can be put in one. This is the step
 * that turns a type into a cabinet standing in a room, and it survives as its own
 * screen for exactly one reason: it is reached from the type library and from a
 * type's own page (`?type=`), which know *what* to stamp and not *where* it goes,
 * so this is where that question gets asked.
 *
 * **It is no longer how you add containers to a container you are looking at.**
 * That was the "multiple pages per container" problem: the parent's own page
 * already knows the parent, so its edit mode opens the same forms in a panel and
 * you never leave the page (`components/AddContainers.tsx`, hosted by
 * `components/ContainerEditMode.tsx`). Both callers share the forms below, so
 * there is one place that knows what `instantiate` needs.
 *
 * The two ways in are the two the API genuinely has — stamp a type's layout into
 * new rows, or create one plain container with no slots — and either can land at
 * the top of the tree, which is what makes this usable on a fresh, empty install
 * whose first container is a cabinet rather than a bare box.
 */

import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { CreatedContainers, PlainContainer, StampFromType } from "../components/AddContainers";
import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import {
  listContainerTypes,
  getLocationTree,
  type ContainerTypeRead,
  type LocationRead,
  type LocationTree,
} from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";

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
        <CreatedContainers created={created} />
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
          Real containers this time, not templates: a cabinet you can open, a drawer you can put a
          part in.
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
                ? "Nowhere — new top-level containers"
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
            Inside <span className="mono">{parentName}</span>. A container is not tied to where it
            starts — moving it later is a move, not a rebuild, because nothing about the hierarchy
            is written on its label or its tag.
          </p>
        )}
        {parentId === null && (
          <p className="muted-note" style={{ margin: 0 }}>
            A top-level container is the room, bench or wall everything else hangs off. If storage
            is empty, this is where to start — and it can be a type as readily as a plain box, since
            the first container is the one most likely to have drawers or a floor to draw.
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
