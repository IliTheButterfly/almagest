/**
 * Storage, as a place rather than as an outline.
 *
 * Two views over the same single request. **Map** is the default: a container's
 * children drawn as a grid, so a cabinet looks like a cabinet and a baseplate
 * like a baseplate, with a cell per slot — including the slots that are empty,
 * because "drawer B3 has nothing in it" is a fact about the furniture and a text
 * tree cannot show it. **List** is the dense flat outline, kept because
 * filtering ninety-six cells by path is faster to read as rows than as tiles.
 *
 * The API returns the tree flat — one row per node with `parent_id`, `depth` and
 * a cached `label_path` — because that is how it is stored: an adjacency list
 * plus a path cache rebuilt by one recursive CTE. So one request is the entire
 * hierarchy, and drilling from a cabinet into a drawer into a bin costs nothing
 * further; `?at=` is a filter over data already in hand, which is also what
 * makes it a shareable link.
 *
 * Fill is drawn and never enforced. Capacity in this system is advisory in every
 * case — an over-capacity put-away is accepted and flagged, because a scan that
 * gets rejected teaches the user to stop scanning — so `over` is a state to
 * surface, not an impossibility to guard against. See components/FillMeter.tsx.
 */

import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ContainerLayout } from "../components/ContainerLayout";
import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import { FillMeter } from "../components/FillMeter";
import { getLocationTree, type LocationNode, type LocationTree } from "../lib/api/client";
import { formatQty } from "../lib/format";
import { ancestorsOf, childrenOf, descendantsOf, indexTree } from "../lib/locations/tree";
import { useAsync } from "../lib/hooks/useAsync";

export function TreeScreen() {
  const tree = useAsync<LocationTree>(() => getLocationTree(), []);

  if (tree.error !== null) {
    return <ErrorBanner error={tree.error} fallback="The storage tree could not be loaded." />;
  }
  if (tree.data === null) {
    return <Loading what="the storage tree" />;
  }
  return <Storage nodes={tree.data.nodes} />;
}

function Storage({ nodes }: { nodes: readonly LocationNode[] }) {
  const [params, setParams] = useSearchParams();
  const index = useMemo(() => indexTree(nodes), [nodes]);

  const rawAt = Number(params.get("at"));
  // A stale or hand-edited `?at=` must land somewhere real rather than on a
  // blank screen: an unknown id falls back to the top level.
  const at = Number.isSafeInteger(rawAt) && index.byId.has(rawAt) ? rawAt : null;
  const view = params.get("view") === "list" ? "list" : "map";

  function go(next: { at?: number | null; view?: "map" | "list" }): void {
    const updated = new URLSearchParams(params);
    if ("at" in next) {
      if (next.at === null || next.at === undefined) {
        updated.delete("at");
      } else {
        updated.set("at", String(next.at));
      }
    }
    if (next.view !== undefined) {
      if (next.view === "map") {
        updated.delete("view");
      } else {
        updated.set("view", next.view);
      }
    }
    setParams(updated);
  }

  const here = at === null ? null : (index.byId.get(at) ?? null);

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <Crumbs index={index} here={here} onGo={(id) => go({ at: id })} />
          <span className="spacer" />
          <div className="segmented" style={{ flex: "0 0 auto" }}>
            <button
              type="button"
              aria-pressed={view === "map"}
              onClick={() => go({ view: "map" })}
            >
              Map
            </button>
            <button
              type="button"
              aria-pressed={view === "list"}
              onClick={() => go({ view: "list" })}
            >
              List
            </button>
          </div>
        </div>

        {here === null ? (
          <p className="muted-note" style={{ margin: 0 }}>
            {index.roots.length} root container(s), {nodes.length} location(s) in all.
          </p>
        ) : (
          <HereSummary index={index} here={here} />
        )}
      </div>

      {view === "map" ? (
        <div className="card">
          {/* Drilling keeps you in the map; `?at=` is the shareable position. */}
          <ContainerLayout index={index} parentId={at} drillTo={(node) => `/tree?at=${node.id}`} />
        </div>
      ) : (
        <TreeGrid nodes={nodes} rootId={at} />
      )}
    </div>
  );
}

function Crumbs({
  index,
  here,
  onGo,
}: {
  index: ReturnType<typeof indexTree>;
  here: LocationNode | null;
  onGo: (id: number | null) => void;
}) {
  const chain = here === null ? [] : ancestorsOf(index, here.id);
  return (
    <nav className="crumbs" aria-label="Breadcrumb">
      <button type="button" className="crumb" onClick={() => onGo(null)}>
        All storage
      </button>
      {chain.map((node) => (
        <span key={node.id} className="row-tight">
          <span className="sep" aria-hidden="true">
            /
          </span>
          <button type="button" className="crumb" onClick={() => onGo(node.id)}>
            {node.name}
          </button>
        </span>
      ))}
      {here !== null && (
        <span className="row-tight">
          <span className="sep" aria-hidden="true">
            /
          </span>
          <span className="here" aria-current="page">
            {here.name}
          </span>
        </span>
      )}
    </nav>
  );
}

/**
 * What this container holds, counting everything below it.
 *
 * The roll-up is over the client-side index rather than a second request: the
 * whole tree is already here, and a per-container endpoint would be N+1 requests
 * for a number the browser can add up.
 */
function HereSummary({
  index,
  here,
}: {
  index: ReturnType<typeof indexTree>;
  here: LocationNode;
}) {
  const subtree = descendantsOf(index, here.id);
  const lots = subtree.reduce((total, node) => total + node.lot_count, 0);
  const qty = subtree.reduce((total, node) => total + node.qty_milli, 0);
  const overfull = subtree.filter((node) => node.is_overfull);

  return (
    <>
      <div className="row">
        <h1 style={{ flex: 1 }}>{here.name}</h1>
        {here.slot_label !== null && <span className="badge mono">{here.slot_label}</span>}
        {here.is_staging && <span className="badge badge-accent">inbox</span>}
        {here.is_overfull && <span className="badge badge-warn">over</span>}
        <Link to={`/locations/${here.id}`}>Open this container →</Link>
      </div>
      <p className="muted-note" style={{ margin: 0 }}>
        {here.label_path}
      </p>
      <div className="row">
        <FillMeter ratio={here.fill_ratio} overfull={here.is_overfull} />
        <span className="muted-note">
          {childrenOf(index, here.id).length} inside · {lots} lot(s) in here and below ·{" "}
          {formatQty(qty)} total
        </span>
      </div>
      {here.is_staging && (
        <Notice kind="warn" title="This is the staging inbox">
          The permanent catch-all, not an ordinary bin. Anything here landed because
          auto-assignment ran out of options, and it is meant to be emptied rather
          than lived in.
        </Notice>
      )}
      {overfull.length > 0 && (
        <Notice kind="warn" title={`${overfull.length} container(s) below here are over capacity`}>
          Recorded, not refused — the put-aways that did this were accepted on purpose.
          A defrag suggestion exists for them instead.
        </Notice>
      )}
    </>
  );
}

/**
 * The flat outline, unchanged in behaviour: one row per node in a CSS grid, with
 * depth as inline padding on the label cell so the numeric columns stay aligned
 * however deep the tree goes.
 */
function TreeGrid({ nodes, rootId }: { nodes: readonly LocationNode[]; rootId: number | null }) {
  const { byParent, roots } = useMemo(() => indexTree(nodes), [nodes]);
  // Collapsed rather than expanded: the whole tree open is hundreds of grid cells
  // on a phone. Roots start open so the screen is never blank.
  const [collapsed, setCollapsed] = useState<ReadonlySet<number>>(() => new Set());
  const [filter, setFilter] = useState("");

  const needle = filter.trim().toLowerCase();
  const matching = useMemo(() => {
    if (needle === "") {
      return null;
    }
    return nodes.filter((node) => node.label_path.toLowerCase().includes(needle));
  }, [needle, nodes]);

  function toggle(id: number): void {
    const next = new Set(collapsed);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    setCollapsed(next);
  }

  const rows: { node: LocationNode; depth: number; children: number }[] = [];
  if (matching === null) {
    const walk = (parent: number | null, depth: number): void => {
      for (const node of byParent.get(parent) ?? []) {
        const children = byParent.get(node.id)?.length ?? 0;
        rows.push({ node, depth, children });
        if (children > 0 && !collapsed.has(node.id)) {
          walk(node.id, depth + 1);
        }
      }
    };
    walk(rootId, 0);
  } else {
    // A filtered view is a flat list of hits: an indented subtree of matches would
    // imply a structure the filter has already broken.
    for (const node of matching) {
      rows.push({ node, depth: 0, children: 0 });
    }
  }

  return (
    <div className="stack">
      <div className="card">
        <label className="field">
          <span>Filter by path</span>
          <input
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder="cabinet a / drawer"
            autoComplete="off"
          />
        </label>
        <p className="muted-note">
          {roots.length} root container(s), {nodes.length} location(s).
          {matching !== null && ` ${matching.length} match the filter.`}
        </p>
      </div>

      <div className="tree" role="table" aria-label="Storage tree">
        <div className="tree-head" role="row">
          <span role="columnheader">Container</span>
          <span role="columnheader">Fill</span>
          <span role="columnheader">Lots</span>
        </div>
        {rows.map(({ node, depth, children }) => (
          <div className="tree-row" role="row" key={node.id}>
            <div role="cell" style={{ paddingLeft: `${0.5 + depth * 1}rem` }}>
              <div className="row-tight">
                {children > 0 ? (
                  <button
                    type="button"
                    className="tree-twisty"
                    aria-expanded={!collapsed.has(node.id)}
                    aria-label={`${collapsed.has(node.id) ? "expand" : "collapse"} ${node.name}`}
                    onClick={() => toggle(node.id)}
                  >
                    {collapsed.has(node.id) ? "▸" : "▾"}
                  </button>
                ) : (
                  <span className="tree-twisty-blank" />
                )}
                <Link className="tree-label" to={`/locations/${node.id}`}>
                  <span className="name">{matching === null ? node.name : node.label_path}</span>
                  {node.slot_label !== null && <span className="badge mono">{node.slot_label}</span>}
                  {node.is_staging && <span className="badge badge-accent">inbox</span>}
                  {node.is_overfull && <span className="badge badge-warn">over</span>}
                </Link>
              </div>
            </div>
            <div role="cell">
              <FillMeter ratio={node.fill_ratio} overfull={node.is_overfull} showText={false} />
            </div>
            <div role="cell" className="dim">
              {node.lot_count > 0 ? `${node.lot_count} / ${formatQty(node.qty_milli)}` : "—"}
            </div>
          </div>
        ))}
        {rows.length === 0 && (
          <div className="tree-row" role="row">
            <div role="cell" className="dim" style={{ gridColumn: "1 / -1" }}>
              No containers. Storage has not been laid out yet.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
