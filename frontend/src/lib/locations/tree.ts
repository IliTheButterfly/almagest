/**
 * The flat tree, indexed.
 *
 * `GET /api/locations/tree` returns every node in one array with `parent_id` and
 * a cached `depth`/`id_path`/`label_path`, because that is how it is stored: an
 * adjacency list plus a path cache rebuilt by one recursive CTE. One request is
 * therefore the whole hierarchy, and building the parent → children map here is
 * cheaper than any amount of per-container fetching.
 *
 * Nodes arrive in `id_path` order, so the children of a parent come out in
 * creation order. `locations.sort_order` is not exposed on `LocationNode`, so
 * that ordering is the closest thing to the authored one available to a client.
 */

import type { LocationNode } from "../api/client";

export interface TreeIndex {
  readonly byId: ReadonlyMap<number, LocationNode>;
  readonly byParent: ReadonlyMap<number | null, readonly LocationNode[]>;
  readonly roots: readonly LocationNode[];
}

export function indexTree(nodes: readonly LocationNode[]): TreeIndex {
  const byId = new Map<number, LocationNode>();
  const byParent = new Map<number | null, LocationNode[]>();
  const ids = new Set(nodes.map((node) => node.id));

  for (const node of nodes) {
    byId.set(node.id, node);
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

  return { byId, byParent, roots: byParent.get(null) ?? [] };
}

export function childrenOf(index: TreeIndex, parentId: number | null): readonly LocationNode[] {
  return index.byParent.get(parentId) ?? [];
}

/**
 * Root-first ancestors of `id`, excluding the node itself.
 *
 * Walked through `parent_id` rather than parsed out of `id_path`: both would
 * work, but a walk cannot disagree with the map the rest of the screen uses.
 * The visited set is a cycle guard — a cycle is impossible by construction and
 * an infinite loop in a breadcrumb is a white screen on a phone.
 */
export function ancestorsOf(index: TreeIndex, id: number): readonly LocationNode[] {
  const chain: LocationNode[] = [];
  const seen = new Set<number>([id]);
  let parentId = index.byId.get(id)?.parent_id ?? null;
  while (parentId !== null && !seen.has(parentId)) {
    const parent = index.byId.get(parentId);
    if (parent === undefined) {
      break;
    }
    chain.push(parent);
    seen.add(parentId);
    parentId = parent.parent_id;
  }
  return chain.reverse();
}

/** Everything below `id`, itself included — for a subtree roll-up. */
export function descendantsOf(index: TreeIndex, id: number): readonly LocationNode[] {
  const out: LocationNode[] = [];
  const queue: number[] = [id];
  const seen = new Set<number>();
  while (queue.length > 0) {
    const next = queue.shift();
    if (next === undefined || seen.has(next)) {
      continue;
    }
    seen.add(next);
    const node = index.byId.get(next);
    if (node !== undefined) {
      out.push(node);
    }
    for (const child of childrenOf(index, next)) {
      queue.push(child.id);
    }
  }
  return out;
}
