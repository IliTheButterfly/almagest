/**
 * Choosing a container, without a scanner.
 *
 * Every put-away flow in PLAN.md starts "scan the destination", and there is no
 * reader on this bench yet — so the manual equivalent is not a fallback, it is the
 * only path. What existed before was a free-text field wanting `location_id`, a
 * database row id nobody can know, which made "use somewhere else instead" a dead
 * end and left stock in the INBOX because the exact id was unobtainable.
 *
 * Three ways in, because they answer different questions:
 *
 * - **Browse** the tree. One request holds the whole hierarchy (adjacency list plus
 *   cached paths), so drilling costs nothing further and the picker never fetches
 *   per level. Any node is choosable, not only leaves: a shelf holds stock too.
 * - **Type part of the path.** A deep tree is slow to walk, and by the time there
 *   are ninety-six cells, walking is the wrong verb. See `lib/locations/match`.
 * - **Type a short ID** — `4K7T-92M8`, what is printed on the label. Resolved
 *   through `GET /api/resolve/{short_id}`, so a code naming a part or a lot is
 *   refused *here*, in terms of what was being chosen. This supplements the tree
 *   rather than replacing it: a generated grid cell deliberately has no printed id
 *   until somebody mints one, so plenty of real containers cannot be named this way.
 *
 * The numeric id field survives only as a marked last resort, and mostly for the
 * case that makes the rest unavailable — the tree request itself failing.
 *
 * Deliberately a component with an `onPick` callback rather than a screen: assign
 * stock, empty a bin, move a lot and drain the INBOX all need the same choice, and
 * a picker welded into one of them is how the second one ends up with a numeric
 * field again.
 *
 * **Browsing is a list here, and should become the storage map** — the same
 * `ContainerLayout` the tree screen draws, so the drawer you pick looks like the
 * drawer you walk to, empty slots included. Not done in this pass because that
 * component navigates by `Link` and this runs inside a form on the way to a commit,
 * where routing away loses the quantity already typed: it needs a callback mode
 * rather than a fork. Issue #43 has the design constraints, and it is sequenced
 * *after* the project-context rework, which rewrites one of the four callers.
 */

import { useMemo, useState } from "react";

import { getLocationTree, resolveShortId, type LocationNode, type LocationTree } from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";
import { matchLocations } from "../lib/locations/match";
import { isInbox, isProjectStagingBox } from "../lib/locations/staging";
import { ancestorsOf, childrenOf, indexTree } from "../lib/locations/tree";
import { formatShortId, looksLikeShortId, normalizeShortId } from "../lib/shortid";
import { CodeEntry } from "./CodeEntry";
import { ErrorBanner, Loading, Notice } from "./Feedback";

/** Enough of a chosen container to label a button with. */
export interface PickedContainer {
  readonly id: number;
  readonly label: string;
}

export interface ContainerPickerProps {
  readonly onPick: (picked: PickedContainer) => void;
  /** Already-chosen container, so the row can say so. */
  readonly pickedId?: number | null;
  /** Containers that cannot be the destination — the bins being emptied. */
  readonly excludeIds?: readonly number[];
  /** Where browsing starts; the top level when absent. */
  readonly startAtId?: number | null;
  /** Verb on the choose button. "Choose" reads oddly for a move. */
  readonly actionLabel?: string;
}

/** Shown for as many matches as this before the count stands in for the rest. */
const MATCH_LIMIT = 40;

/** One shared empty array, so "the tree has not arrived" is a stable dependency. */
const NO_NODES: readonly LocationNode[] = [];

/** Likewise for "nothing is excluded", which is the common case. */
const NO_EXCLUSIONS: readonly number[] = [];

export function ContainerPicker({
  onPick,
  pickedId = null,
  excludeIds = NO_EXCLUSIONS,
  startAtId = null,
  actionLabel = "Choose",
}: ContainerPickerProps) {
  const tree = useAsync<LocationTree>(() => getLocationTree(), []);
  const nodes = useMemo(() => tree.data?.nodes ?? NO_NODES, [tree.data]);
  const index = useMemo(() => indexTree(nodes), [nodes]);

  const [at, setAt] = useState<number | null>(startAtId);
  const [filter, setFilter] = useState("");
  const [lookupBusy, setLookupBusy] = useState(false);
  const [lookupError, setLookupError] = useState<unknown>(null);
  const [rejected, setRejected] = useState<string | null>(null);
  const [numeric, setNumeric] = useState("");

  const matches = useMemo(() => matchLocations(nodes, filter), [nodes, filter]);
  const filtering = filter.trim() !== "";

  function choose(node: LocationNode): void {
    onPick({ id: node.id, label: node.label_path });
  }

  async function lookUp(code: string): Promise<void> {
    setLookupBusy(true);
    setLookupError(null);
    setRejected(null);
    try {
      const response = await resolveShortId(normalizeShortId(code));
      const target = response.target;
      if (target === null || target === undefined) {
        setRejected(`Nothing here answers to ${formatShortId(normalizeShortId(code))}.`);
        return;
      }
      if (target.entity_type !== "location") {
        setRejected(
          `That code is a ${target.entity_type.replace(/_/g, " ")}, not a container. ` +
            "It is the drawer or bin that needs naming here.",
        );
        return;
      }
      if (excludeIds.includes(target.entity_pk)) {
        setRejected("That is the container the stock is coming out of.");
        return;
      }
      onPick({ id: target.entity_pk, label: target.label_path ?? target.label });
    } catch (cause) {
      setLookupError(cause);
    } finally {
      setLookupBusy(false);
    }
  }

  function chooseNumeric(): void {
    const value = Number(numeric.trim());
    if (!Number.isSafeInteger(value) || value <= 0) {
      return;
    }
    const known = index.byId.get(value);
    onPick({ id: value, label: known?.label_path ?? `location ${value}` });
  }

  const here = at === null ? null : (index.byId.get(at) ?? null);
  const numericValid = numeric.trim() === "" || Number.isSafeInteger(Number(numeric.trim()));

  return (
    <div className="stack">
      <label className="field">
        <span>Find a container by name or path</span>
        <input
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="cabinet a drawer 7"
          autoComplete="off"
        />
      </label>

      {tree.error !== null && (
        <ErrorBanner
          error={tree.error}
          fallback="The storage tree could not be loaded, so browsing is unavailable. A short ID still works."
        />
      )}
      {tree.data === null && tree.error === null && <Loading what="the storage tree" />}

      {tree.data !== null &&
        (filtering ? (
          <>
            <p className="muted-note" style={{ margin: 0 }}>
              {matches.length === 0
                ? "Nothing matches that."
                : `${matches.length} match(es)` +
                  (matches.length > MATCH_LIMIT ? `, showing the first ${MATCH_LIMIT}` : "")}
            </p>
            <PickList
              nodes={matches.slice(0, MATCH_LIMIT)}
              index={index}
              showPaths
              pickedId={pickedId}
              excludeIds={excludeIds}
              actionLabel={actionLabel}
              onChoose={choose}
              onOpen={(node) => {
                setFilter("");
                setAt(node.id);
              }}
            />
          </>
        ) : (
          <>
            <nav className="crumbs" aria-label="Container breadcrumb">
              <button type="button" className="crumb" onClick={() => setAt(null)}>
                All storage
              </button>
              {(here === null ? [] : ancestorsOf(index, here.id)).map((node) => (
                <span key={node.id} className="row-tight">
                  <span className="sep" aria-hidden="true">
                    /
                  </span>
                  <button type="button" className="crumb" onClick={() => setAt(node.id)}>
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

            {here !== null && !excludeIds.includes(here.id) && (
              <button
                type="button"
                className="wide"
                onClick={() => choose(here)}
                aria-pressed={pickedId === here.id}
              >
                {pickedId === here.id ? `Chosen: ${here.name}` : `${actionLabel} ${here.name}`}
              </button>
            )}

            <PickList
              nodes={childrenOf(index, at)}
              index={index}
              showPaths={false}
              pickedId={pickedId}
              excludeIds={excludeIds}
              actionLabel={actionLabel}
              onChoose={choose}
              onOpen={(node) => setAt(node.id)}
            />
          </>
        ))}

      <details>
        <summary>Type a printed short ID instead</summary>
        <CodeEntry
          label="Container short ID"
          placeholder="4K7T-92M8"
          busy={lookupBusy}
          onSubmit={(code) => {
            if (!looksLikeShortId(code)) {
              setRejected("That is not a short ID. They are eight symbols, like 4K7T-92M8.");
              return;
            }
            void lookUp(code);
          }}
        />
        <p className="muted-note">
          Only containers that have been given a printed id can be named this way — a
          generated cell has none until one is minted.
        </p>
        {rejected !== null && (
          <Notice kind="warn" title="Not a container">
            {rejected}
          </Notice>
        )}
        <ErrorBanner error={lookupError} fallback="That code could not be looked up." />
      </details>

      <details>
        <summary>Last resort: a numeric location id</summary>
        <p className="muted-note">
          Only useful if you already know the database row id — from a URL, or because
          the tree above will not load. Browsing or a short ID is the normal way.
        </p>
        <label className="field">
          <span>Location id</span>
          <input
            inputMode="numeric"
            value={numeric}
            onChange={(event) => setNumeric(event.target.value)}
            placeholder="41"
          />
        </label>
        {!numericValid && <p className="muted-note">That is not a location id.</p>}
        <button
          type="button"
          onClick={chooseNumeric}
          disabled={numeric.trim() === "" || !numericValid || Number(numeric.trim()) <= 0}
        >
          Use that id
        </button>
      </details>
    </div>
  );
}

/**
 * One row per container: what it is, what is in it, and the two things you can do
 * with it — open it, or choose it. Both are buttons rather than links, because this
 * runs inside a form on somebody's way to a commit; navigating away would lose the
 * quantity they already typed.
 */
function PickList({
  nodes,
  index,
  showPaths,
  pickedId,
  excludeIds,
  actionLabel,
  onChoose,
  onOpen,
}: {
  nodes: readonly LocationNode[];
  index: ReturnType<typeof indexTree>;
  showPaths: boolean;
  pickedId: number | null;
  excludeIds: readonly number[];
  actionLabel: string;
  onChoose: (node: LocationNode) => void;
  onOpen: (node: LocationNode) => void;
}) {
  if (nodes.length === 0) {
    return (
      <p className="dim" style={{ margin: 0 }}>
        Nothing in here.
      </p>
    );
  }

  return (
    <ul className="list picker-list">
      {nodes.map((node) => {
        const children = childrenOf(index, node.id).length;
        const excluded = excludeIds.includes(node.id);
        return (
          <li key={node.id} className="list-item">
            <div className="row">
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="row-tight">
                  <span className="title">{showPaths ? node.label_path : node.name}</span>
                  {node.slot_label !== null && <span className="badge mono">{node.slot_label}</span>}
                  {isInbox(node) && <span className="badge badge-accent">inbox</span>}
                  {isProjectStagingBox(node) && (
                    <span className="badge badge-accent">project parts</span>
                  )}
                  {node.is_overfull && <span className="badge badge-warn">over</span>}
                  {node.is_placeable === false && <span className="badge">not a home</span>}
                </div>
                <div className="sub">
                  {children > 0 ? `${children} inside · ` : ""}
                  {node.lot_count} lot(s)
                </div>
              </div>
              {children > 0 && (
                <button type="button" onClick={() => onOpen(node)}>
                  Open
                </button>
              )}
              <button
                type="button"
                className={pickedId === node.id ? "" : "primary"}
                aria-pressed={pickedId === node.id}
                disabled={excluded}
                onClick={() => onChoose(node)}
              >
                {excluded ? "Where it is now" : pickedId === node.id ? "Chosen" : actionLabel}
              </button>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
