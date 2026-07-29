/**
 * The library of container types — the reusable templates a cabinet, a
 * drawer or a baseplate is stamped from. This screen only lists and links
 * on; the canvas itself is `ContainerTypeScreen`.
 *
 * A seed type (`is_seed`) ships with every fresh install and is read-only —
 * opening one to edit it clones instead of mutating the row every other
 * install starts from, which is why the badge says so here rather than
 * only after the fact on the editor.
 */

import { Link } from "react-router-dom";

import { Empty, ErrorBanner, Loading } from "../components/Feedback";
import { listContainerTypes, type ContainerTypeRead } from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";

export function ContainerTypesScreen() {
  const types = useAsync<ContainerTypeRead[]>(() => listContainerTypes(), []);

  return (
    <div className="stack">
      <div className="card">
        <h1>Container types</h1>
        <p className="muted-note" style={{ margin: 0 }}>
          Reusable templates for storage: a cabinet's grid of drawers, a baseplate's grid of
          bins. Editing one here never touches a cabinet already built from it — that is the
          separate, explicit "reapply" action on the container itself.
        </p>
      </div>

      <ErrorBanner error={types.error} fallback="The container types could not be loaded." />
      {types.data === null ? (
        <Loading what="container types" />
      ) : types.data.length === 0 ? (
        <Empty>No container types exist yet.</Empty>
      ) : (
        <ul className="list">
          {types.data.map((type) => (
            <li key={type.id}>
              <Link className="list-item" to={`/container-types/${type.id}`}>
                <div className="row">
                  <span className="title" style={{ flex: 1 }}>
                    {type.display_name}
                  </span>
                  {type.is_seed && <span className="badge">seed — editing clones it</span>}
                </div>
                <div className="sub mono">{type.slug}</div>
                <div className="sub">
                  {type.grid_rows !== null && type.grid_cols !== null
                    ? `${type.grid_rows} × ${type.grid_cols} grid, ${type.slot_label_scheme}`
                    : "no grid defined"}
                  {type.materialize_slots ? " · custom-edited" : " · plain grid"}
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
