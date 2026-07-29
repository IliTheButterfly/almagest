/**
 * The library of container types — the reusable templates a cabinet, a
 * drawer or a baseplate is stamped from.
 *
 * **The library being visible is the feature.** Eleven types ship with every
 * install, and cloning the closest one is both faster and much harder to get
 * wrong than filling in a blank form — so every row carries its own Clone button
 * and the blank form is offered once, at the top, rather than being the only door.
 * Each row also states ADR 0002's two answers separately ("offers 6 x 4", "takes
 * up 2 x 1"), because those are the two facts a person is choosing between when
 * they pick which type to copy.
 *
 * A seed type (`is_seed`) is read-only — opening one to edit it clones instead of
 * mutating the row every other install starts from, which is why the badge says
 * so here rather than only after the fact on the editor.
 *
 * The canvas itself (merges, relabels, size classes) is `ContainerTypeScreen`;
 * stamping real containers out of a type is `/containers/new`. Neither is
 * duplicated here — this screen lists and links on.
 */

import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ContainerPhoto } from "../components/ContainerPhoto";
import { Empty, ErrorBanner, Loading } from "../components/Feedback";
import {
  cloneContainerType,
  listContainerTypes,
  type ContainerTypeRead,
} from "../lib/api/client";
import { describeOccupies, describePresents } from "../lib/containers/typeDraft";
import { useAsync } from "../lib/hooks/useAsync";
import { uuid4 } from "../lib/scan/session";

type Filter = "all" | "seed" | "mine";

const FILTER_LABELS: Readonly<Record<Filter, string>> = {
  all: "All",
  seed: "Shipped with Almagest",
  mine: "Mine",
};

export function ContainerTypesScreen() {
  const navigate = useNavigate();
  const [filter, setFilter] = useState<Filter>("all");
  const [cloning, setCloning] = useState<number | null>(null);
  const [cloneError, setCloneError] = useState<unknown>(null);

  const types = useAsync<ContainerTypeRead[]>(
    () => listContainerTypes(filter === "all" ? {} : { isSeed: filter === "seed" }),
    [filter],
  );

  async function clone(type: ContainerTypeRead): Promise<void> {
    setCloning(type.id);
    setCloneError(null);
    try {
      // No slug given: the server picks `{slug}-copy`, then `-copy-2`, so two
      // clones of the same seed do not collide. Idempotency-guarded, because a
      // retried clone must replay rather than mint a third copy.
      const response = await cloneContainerType(type.id, { client_op_id: uuid4() });
      navigate(`/container-types/${response.container_type.id}`);
    } catch (cause) {
      setCloneError(cause);
    } finally {
      setCloning(null);
    }
  }

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <h1 style={{ flex: 1 }}>Container types</h1>
          <Link className="button-link" to="/container-types/new">
            New type
          </Link>
        </div>
        <p className="muted-note" style={{ margin: 0 }}>
          Reusable templates for storage: a cabinet's grid of drawers, a baseplate's grid of
          bins. A type is not a container — nothing goes in one until you stamp a real
          container from it. Editing a type never touches a cabinet already built from it;
          that is the separate, explicit "reapply" action on the container itself.
        </p>
        <div className="row">
          <Link to="/containers/new">Add real containers to storage →</Link>
          <span className="spacer" />
          <Link to="/tree">See what already exists →</Link>
        </div>
        <div className="segmented" role="group" aria-label="Which types">
          {(Object.keys(FILTER_LABELS) as Filter[]).map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={filter === value}
              onClick={() => setFilter(value)}
            >
              {FILTER_LABELS[value]}
            </button>
          ))}
        </div>
      </div>

      <ErrorBanner error={cloneError} fallback="That type could not be copied." />
      <ErrorBanner error={types.error} fallback="The container types could not be loaded." />
      {types.data === null ? (
        <Loading what="container types" />
      ) : types.data.length === 0 ? (
        <Empty>
          {filter === "mine"
            ? "You have not made any of your own yet. Clone one of the shipped types, or start a new one above."
            : "No container types exist yet."}
        </Empty>
      ) : (
        <ul className="list">
          {types.data.map((type) => (
            <li key={type.id}>
              <div className="list-item">
                <div className="row">
                  {/* Glyph-only on purpose: this list can be eleven rows and a
                      photo apiece is eleven image fetches to draw something a
                      few dozen pixels wide. The photo is on the type's own
                      screen, where it is drawn once and large. */}
                  <ContainerPhoto photo={null} glyph={type.glyph} alt="" />
                  <Link className="title" style={{ flex: 1 }} to={`/container-types/${type.id}`}>
                    {type.display_name}
                  </Link>
                  {type.is_seed && <span className="badge">shipped — editing copies it</span>}
                </div>
                <div className="sub mono">{type.slug}</div>
                {/* ADR 0002's two questions, kept apart in the summary as well as
                    in the form: a reader choosing what to clone is choosing
                    between exactly these two facts. */}
                <div className="sub">
                  Offers: {describePresents(type)} · {type.slot_label_scheme}
                  {type.materialize_slots ? " · hand-edited layout" : " · plain grid"}
                </div>
                <div className="sub">Takes up: {describeOccupies(type)}</div>
                <div className="row">
                  <button
                    type="button"
                    onClick={() => void clone(type)}
                    disabled={cloning !== null}
                  >
                    {cloning === type.id ? "Copying…" : "Clone"}
                  </button>
                  <Link to={`/containers/new?type=${type.id}`}>Create containers from it →</Link>
                  <span className="spacer" />
                  <Link to={`/container-types/${type.id}`}>Edit →</Link>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
