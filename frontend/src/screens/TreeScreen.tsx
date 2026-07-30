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

import { ContainerDetailPanel } from "../components/ContainerDetailPanel";
import { ContainerLayout } from "../components/ContainerLayout";
import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import { FillMeter } from "../components/FillMeter";
import { PathBar } from "../components/PathBar";
import { getLocationTree, type LocationNode, type LocationTree } from "../lib/api/client";
import { formatQty } from "../lib/format";
import { matchLocations } from "../lib/locations/match";
import { isInbox, isProjectStagingBox } from "../lib/locations/staging";
import { containerTrailFromIndex } from "../lib/locations/trail";
import { childrenOf, descendantsOf, indexTree } from "../lib/locations/tree";
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
  /**
   * Which container the detail panel is showing — the other half of a
   * master/detail workspace, and in the URL for the same reason `?at=` is: a
   * position somebody can send to somebody else, and one the Back button
   * restores.
   *
   * Falls back to the level in view, so the panel always describes something.
   * That matters at the moment you drill: you pressed into a cabinet because you
   * are interested in the cabinet, and an empty panel would make you press it
   * again to find out about it.
   */
  const rawSel = Number(params.get("sel"));
  const sel = Number.isSafeInteger(rawSel) && index.byId.has(rawSel) ? rawSel : null;
  const subject = sel ?? at;

  function go(next: {
    at?: number | null;
    view?: "map" | "list";
    sel?: number | null;
  }): void {
    const updated = new URLSearchParams(params);
    if ("at" in next) {
      if (next.at === null || next.at === undefined) {
        updated.delete("at");
      } else {
        updated.set("at", String(next.at));
      }
      // Drilling changes what the panel is about; keeping the old selection would
      // leave a drawer's details beside a different cabinet's map.
      updated.delete("sel");
    }
    if ("sel" in next) {
      if (next.sel === null || next.sel === undefined) {
        updated.delete("sel");
      } else {
        updated.set("sel", String(next.sel));
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
          <PathBar
            trail={containerTrailFromIndex(index, at, (id) => go({ at: id }))}
            label="Storage path"
          />
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

        {/*
          The tree is where somebody stands when they realise they need somewhere
          to put a part, so this is where "add one" has to be — carrying the
          position they are already looking at, so the new containers land here
          rather than making them pick their own location out of a list again.

          When there *is* a container to add into, this goes to that container's own
          page in edit mode rather than to `/containers/new?parent=`. There is one
          place a container is edited, and a second surface that also adds children
          to it is a second thing to keep in step — which is the whole complaint
          edit mode answers. The top of the tree is different in kind, not in level:
          there is no container page to open, so the standalone screen remains the
          only way to make the first one.
        */}
        <div className="row">
          <Link
            className="button-link"
            to={here === null ? "/containers/new" : `/locations/${here.id}?edit=1&panel=add`}
          >
            {here === null ? "Add a container" : `Add containers in ${here.name}`}
          </Link>
          <span className="spacer" />
          <Link to="/container-types">Container types →</Link>
        </div>
      </div>

      {view === "map" ? (
        /*
         * Master and detail, side by side on a desktop and stacked on a phone.
         *
         * The map never leaves the screen: pressing a cell fills the panel, and
         * the corner strip drills. That is the whole answer to "you end up having
         * a second view that is only slightly different from the other view" —
         * there is no second view to end up on.
         */
        <div className="workspace">
          <div className="card">
          {/* Drilling keeps you in the map; `?at=` is the shareable position. */}
            <ContainerLayout
              index={index}
              parentId={at}
              pick={{
                onPick: (node) => go({ sel: node.id }),
                onDrill: (node) => go({ at: node.id }),
                pickedId: sel,
                actionLabel: "Show",
              }}
            />
          </div>
          {subject !== null && (
            <ContainerDetailPanel
              locationId={subject}
              childCount={childrenOf(index, subject).length}
              onLookInside={(id) => go({ at: id })}
            />
          )}
        </div>
      ) : (
        <TreeGrid nodes={nodes} rootId={at} />
      )}

      <RemovedContainers />
    </div>
  );
}

/**
 * The containers that were removed but kept — and the only screen that lists them.
 *
 * A retirement is the reversible half of removing a container, and it is reversible
 * from the container's *own* page. But retirement takes the row out of every other
 * read: no parent's children, no slot canvas, no room plan, no assignment proposal.
 * So without this list the "Bring it back" button was reachable only by typing the
 * numeric id into the URL or by scanning the tag still stuck to the drawer, which
 * makes "it can be restored" a promise the UI did not keep.
 *
 * Fetched only when asked for, and rendered as plain rows rather than through
 * `ContainerLayout`: a retired container has no slot cell and no coordinate — that
 * is what retiring cleared — so it belongs in no picture of the furniture. The path
 * is what identifies it, because the name alone ("B3") does not.
 */
function RemovedContainers() {
  const [open, setOpen] = useState(false);
  const removed = useAsync<LocationTree | null>(
    () => (open ? getLocationTree(undefined, { includeRetired: true }) : Promise.resolve(null)),
    [open],
  );
  const rows = (removed.data?.nodes ?? []).filter((node) => node.retired_at !== null);

  return (
    <div className="card">
      <div className="row">
        <h3 style={{ margin: 0 }}>Removed containers</h3>
        <span className="spacer" />
        <button type="button" aria-pressed={open} onClick={() => setOpen(!open)}>
          {open ? "Hide them" : "Show them"}
        </button>
      </div>
      {!open ? (
        <p className="muted-note" style={{ margin: 0 }}>
          A container the stock ledger, a printed label or a tag names keeps its row and its
          history when it is removed — it just leaves the tree. Those can be brought back.
        </p>
      ) : (
        <>
          <ErrorBanner error={removed.error} fallback="The removed containers could not be loaded." />
          {removed.data === null && removed.error === null ? (
            <Loading what="the removed containers" />
          ) : rows.length === 0 ? (
            <p className="dim" style={{ margin: 0 }}>
              Nothing has been removed and kept.
            </p>
          ) : (
            <ul className="list">
              {rows.map((node) => (
                <li key={node.id}>
                  <Link className="list-item" to={`/locations/${node.id}`}>
                    <div className="row">
                      <span className="title">{node.name}</span>
                      <span className="spacer" />
                      <span className="badge badge-warn">removed</span>
                    </div>
                    <div className="sub mono">{node.label_path}</div>
                    <div className="sub">Open it to bring it back →</div>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

/**
 * What this **level** holds, counting everything below it.
 *
 * Trimmed to only what the detail panel does not say. It used to restate the
 * container's identity — name, slot, badges, its path, its own fill — beside a
 * panel now saying the same things about the same container, which is the
 * duplication this workspace exists to remove. What survives is what is genuinely
 * about the level rather than the container: the roll-up over everything beneath
 * it, and the two staging notices, which are advice about a whole subtree.
 *
 * The roll-up is over the client-side index rather than a second request: the
 * whole tree is already here, and a per-container endpoint would be N+1 requests
 * for a number the browser can add up.
 *
 * The "Open this container" link is gone rather than moved. It was the one-way
 * door onto the other view, and there is no other view now; the panel's own link
 * is named for what is over there instead.
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
      <p className="muted-note" style={{ margin: 0 }}>
        {childrenOf(index, here.id).length} inside · {lots} lot(s) in here and below ·{" "}
        {formatQty(qty)} total
      </p>
      {/* Two staging kinds, opposite advice. This notice used to fire on
          `is_staging` alone, so a project's box was told it "is meant to be
          emptied rather than lived in" — the exact opposite of true for a box
          deliberately holding a board's parts, and advice that would have someone
          undo a withdrawal they meant to make. */}
      {isInbox(here) && (
        <Notice kind="warn" title="This is the staging inbox">
          The permanent catch-all, not an ordinary bin. Anything here landed because
          auto-assignment ran out of options, and it is meant to be emptied rather
          than lived in. <Link to="/staging">Empty it →</Link>
        </Notice>
      )}
      {isProjectStagingBox(here) && (
        <Notice kind="info" title="These parts are set aside for a project">
          Not a catch-all and not somewhere to empty: parts here were taken out of
          stock on purpose and are waiting to be built in. They are still real stock
          and still findable — they are just not free for anything else, so no other
          build counts them as available. Put them back from the build's roster tab.
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

  // The same matcher the container picker uses, so the two screens cannot disagree
  // about what "cabinet a drawer 7" finds. See `lib/locations/match`.
  const matching = useMemo(
    () => (filter.trim() === "" ? null : matchLocations(nodes, filter)),
    [filter, nodes],
  );

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
                  {isInbox(node) && <span className="badge badge-accent">inbox</span>}
                  {isProjectStagingBox(node) && (
                    <span className="badge badge-accent">project parts</span>
                  )}
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
              No containers. Storage has not been laid out yet —{" "}
              <Link to="/containers/new">add the first one</Link>, a room or a bench to hang
              everything else off.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
