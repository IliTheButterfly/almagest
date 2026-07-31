/**
 * The trail of crumbs above a page: where this thing sits, and a way back up.
 *
 * Data, not markup — `components/PathBar` renders whatever this produces, and
 * every page that has a place in some hierarchy supplies one. It exists because
 * the alternative had already happened twice: the storage tree grew clickable
 * crumbs, the container page grew a plain `label_path` string and a single "Up
 * one level" link, and the two screens then disagreed about what the path even
 * was. A reader who could see the path could not use it.
 *
 * **A container's trail costs nothing extra.** `LocationRead` and `LocationNode`
 * both carry `id_path` ("/2/3/") and `label_path` ("Workshop / Cabinet A"), which
 * between them are the ids and the names of every ancestor — the cache the tree
 * service rebuilds with one recursive CTE, already on the wire for other reasons.
 * So no page needs to fetch its ancestors, and none of them walks a parent chain
 * one request at a time.
 *
 * The two builders differ only in what they have in hand: a page showing one
 * container has its `id_path`/`label_path`; a page holding the whole tree has an
 * index and an id. Neither invents a crumb it cannot place — see `zip` below.
 */

import type { TreeIndex } from "./tree";
import { ancestorsOf } from "./tree";

/**
 * One step of the trail.
 *
 * `to` navigates and `onSelect` calls back; a crumb carries at most one. The
 * callback form is not a nicety — the container picker runs inside a form on
 * somebody's way to a commit, where routing away would lose the quantity they
 * already typed, so its crumbs must move the picker rather than the page. The
 * last crumb in a trail is the current thing and carries neither.
 */
export interface PathCrumb {
  /** Stable across renders; ids where there are ids, else the position. */
  readonly key: string;
  readonly label: string;
  readonly to?: string | undefined;
  readonly onSelect?: (() => void) | undefined;
}

/** What the top of a storage trail is called, wherever one starts. */
export const ALL_STORAGE = "All storage";

/**
 * The path segments of a `label_path`, as the tree service joins them.
 *
 * Kept next to the split so the separator is written once: a page that guessed
 * `"/"` instead would cut every name containing a slash in half.
 */
const LABEL_SEP = " / ";

/** Numeric ids out of an `id_path` like `/2/3/41/`. */
function idsOf(idPath: string): number[] {
  return idPath
    .split("/")
    .filter((part) => part !== "")
    .map((part) => Number(part));
}

/**
 * A container's ancestors and itself, from the two cached paths it already
 * carries.
 *
 * If the two paths disagree about how many levels there are, this returns just
 * the container itself rather than pairing a name with somebody else's id. That
 * is the one failure mode worth guarding: a name *can* contain the separator, and
 * a mispaired crumb is a link that silently goes to the wrong drawer — worse than
 * no crumb at all, because it looks right.
 */
export function containerTrail(
  location: {
    readonly id: number;
    readonly name: string;
    // Tolerated as absent, and deliberately not required: these are a *cache*,
    // rebuilt from `parent_id` by a recursive CTE. A page whose whole job is
    // "what is in this drawer" must not go blank because the path cache is
    // missing — it degrades to the one crumb it is certain of.
    readonly id_path?: string | null;
    readonly label_path?: string | null;
  },
  options: { readonly hrefFor?: (id: number) => string; readonly rootTo?: string } = {},
): PathCrumb[] {
  const hrefFor = options.hrefFor ?? ((id: number) => `/locations/${id}`);
  const ids = idsOf(location.id_path ?? "");
  const labels = (location.label_path ?? "").split(LABEL_SEP);

  const root: PathCrumb = { key: "root", label: ALL_STORAGE, to: options.rootTo ?? "/tree" };

  if (ids.length === 0 || ids.length !== labels.length || ids.some((id) => !Number.isSafeInteger(id))) {
    // Cannot place the ancestors, so it claims none of them.
    return [root, { key: String(location.id), label: location.name }];
  }

  return [
    root,
    ...ids.map((id, position) => {
      const label = labels[position] ?? String(id);
      const last = position === ids.length - 1;
      return last
        ? { key: String(id), label }
        : { key: String(id), label, to: hrefFor(id) };
    }),
  ];
}

/**
 * The same trail for a page that holds the whole tree, addressed by callback.
 *
 * `at === null` is the top level, which is a real position rather than a missing
 * one: the map and the picker both start there, and it is the crumb that gets you
 * back out of a drawer.
 */
export function containerTrailFromIndex(
  index: TreeIndex,
  at: number | null,
  onSelect: (id: number | null) => void,
): PathCrumb[] {
  const root: PathCrumb = { key: "root", label: ALL_STORAGE, onSelect: () => onSelect(null) };
  if (at === null) {
    return [{ key: "root", label: ALL_STORAGE }];
  }
  const here = index.byId.get(at);
  if (here === undefined) {
    return [root];
  }
  return [
    root,
    ...ancestorsOf(index, at).map((node) => ({
      key: String(node.id),
      label: node.name,
      onSelect: () => onSelect(node.id),
    })),
    { key: String(here.id), label: here.name },
  ];
}
