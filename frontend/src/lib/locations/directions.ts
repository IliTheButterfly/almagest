/**
 * The walk from the front door to one container, as a list of turns.
 *
 * `label_path` — "Workshop / Cabinet A / Drawer B3 / Bin 7" — is a correct answer
 * to "where is this part" and a poor one, because it is the answer a *filing
 * system* would give. Standing in the room, the question is not what the drawer is
 * called; it is which of the forty identical drawer fronts to pull. The map screen
 * has always been able to show that, one level at a time, and nothing has ever
 * pointed it at a part.
 *
 * So this turns a destination into the sequence of levels somebody actually
 * passes through, each paired with the one child to head for. `components/
 * WhereIsIt` draws each step with the existing map renderer and lights up `to` —
 * no new picture of storage, and therefore no second drawing that can disagree
 * with `/tree` about what the workshop looks like.
 *
 * Pure, and deliberately so: it reads the tree index every map screen already
 * holds and fetches nothing. The whole hierarchy arrives in one request
 * (`lib/locations/tree`), so the walk costs no round trips at all.
 */

import { childrenOf, type TreeIndex } from "./tree";
import type { LocationNode } from "../api/client";

/** One turn of the walk: a level to look at, and what to look for in it. */
export interface Direction {
  /**
   * The container whose inside is drawn. `null` is the top level — a real
   * position, not a missing one, and the step that says which shed to walk into.
   */
  readonly at: LocationNode | null;
  /** The child to head for, drawn lit up at this level. */
  readonly to: LocationNode;
  /** Nothing is inside `to`, so this is the last turn — it is the container. */
  readonly last: boolean;
}

/**
 * Root-first directions to `locationId`, or an empty list if the tree does not
 * hold it.
 *
 * Empty rather than a partial walk when the destination is unknown: a subtree
 * fetch legitimately returns a node whose ancestors are outside the result set,
 * and half a walk starting in mid-air is worse than the plain text path the
 * caller can always fall back to. It says "I cannot show you this", which is
 * true, instead of drawing a cabinet that is not the one you want.
 */
export function directionsTo(index: TreeIndex, locationId: number): readonly Direction[] {
  const target = index.byId.get(locationId);
  if (target === undefined) {
    return [];
  }

  // Walked through `parent_id` by `ancestorsOf`'s sibling logic rather than
  // parsed out of `id_path`, for the reason `tree.ts` gives: a walk cannot
  // disagree with the map the rest of the screen is drawing from.
  const chain: LocationNode[] = [target];
  const seen = new Set<number>([target.id]);
  let parentId = target.parent_id;
  while (parentId !== null && !seen.has(parentId)) {
    const parent = index.byId.get(parentId);
    if (parent === undefined) {
      break;
    }
    chain.unshift(parent);
    seen.add(parent.id);
    parentId = parent.parent_id;
  }

  return chain.map((node, position) => ({
    at: position === 0 ? null : (chain[position - 1] ?? null),
    to: node,
    last: node.id === target.id,
  }));
}

/**
 * The sentence above the pictures: "Workshop, then Cabinet A, then Drawer B3".
 *
 * Not `label_path` re-punctuated for fun — the separator is doing work. A path
 * reads as an address, which invites scanning it as one token and missing a level;
 * "then" reads as instructions, which is what somebody walking to a drawer wants.
 * The text is also what a screen reader gets *instead of* the drawings, so it has
 * to stand on its own.
 */
export function directionsSentence(directions: readonly Direction[]): string {
  return directions.map((step) => step.to.name).join(", then ");
}

/**
 * How many containers sit at the same level as `to`, so a step can say how much
 * of a haystack this needle is in.
 *
 * "One of 40 drawers" is the fact that makes the picture worth drawing, and its
 * absence is the fact that makes it not worth drawing: a cabinet with one drawer
 * in it needs no map. The caller uses this to keep quiet in the second case.
 */
export function siblingCount(index: TreeIndex, step: Direction): number {
  return childrenOf(index, step.at?.id ?? null).length;
}
