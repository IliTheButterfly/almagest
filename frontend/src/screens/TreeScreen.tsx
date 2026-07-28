/**
 * The storage tree.
 *
 * The API returns the tree **flat** — one row per node with `parent_id`, `depth`
 * and a cached `label_path` — because that is how it is stored: an adjacency list
 * plus a path cache rebuilt by one recursive CTE. So this screen renders a flat
 * list into a CSS grid rather than nesting elements: depth becomes indentation on
 * the label cell, which keeps the numeric columns aligned however deep the tree
 * goes, and the sticky header keeps them labelled while scrolling a cabinet with
 * ninety-six cells.
 *
 * Fill is drawn as a bar and never as a barrier. Capacity in this system is
 * advisory in every case — an over-capacity put-away is accepted and flagged,
 * because a scan that gets rejected teaches the user to stop scanning.
 */

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { ErrorBanner, Loading } from "../components/Feedback";
import { getLocationTree, type LocationNode, type LocationTree } from "../lib/api/client";
import { formatFillRatio, formatQty } from "../lib/format";
import { useAsync } from "../lib/hooks/useAsync";

export function TreeScreen() {
  const tree = useAsync<LocationTree>(() => getLocationTree(), []);

  if (tree.error !== null) {
    return <ErrorBanner error={tree.error} fallback="The storage tree could not be loaded." />;
  }
  if (tree.data === null) {
    return <Loading what="the storage tree" />;
  }
  return <TreeGrid nodes={tree.data.nodes} />;
}

interface Indexed {
  readonly byParent: Map<number | null, LocationNode[]>;
  readonly roots: LocationNode[];
}

function index(nodes: readonly LocationNode[]): Indexed {
  const byParent = new Map<number | null, LocationNode[]>();
  const ids = new Set(nodes.map((node) => node.id));
  for (const node of nodes) {
    // A subtree fetch returns a root whose parent is outside the result set;
    // treating that parent as absent is what makes the same render work for both
    // the whole tree and one cabinet.
    const parent = node.parent_id !== null && ids.has(node.parent_id) ? node.parent_id : null;
    const siblings = byParent.get(parent);
    if (siblings === undefined) {
      byParent.set(parent, [node]);
    } else {
      siblings.push(node);
    }
  }
  return { byParent, roots: byParent.get(null) ?? [] };
}

function TreeGrid({ nodes }: { nodes: readonly LocationNode[] }) {
  const { byParent, roots } = useMemo(() => index(nodes), [nodes]);
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
    walk(null, 0);
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
                  {node.is_staging && <span className="badge">staging</span>}
                  {node.is_overfull && <span className="badge badge-bad">overfull</span>}
                </Link>
              </div>
            </div>
            <div role="cell">
              <FillBar ratio={node.fill_ratio} overfull={node.is_overfull} />
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

export function FillBar({ ratio, overfull }: { ratio: number | null; overfull: boolean }) {
  if (ratio === null) {
    return <span className="dim">—</span>;
  }
  return (
    <span title={`${formatFillRatio(ratio)} full`}>
      <span className={overfull ? "fill-bar over" : "fill-bar"}>
        <span style={{ width: `${Math.min(100, Math.max(0, ratio * 100))}%` }} />
      </span>
    </span>
  );
}
