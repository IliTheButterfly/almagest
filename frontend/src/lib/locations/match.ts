/**
 * Finding a container by typing part of where it is.
 *
 * The tree arrives whole in one request, so this is a filter over data already in
 * hand rather than a search endpoint — but the naive `label_path.includes(needle)`
 * the list view started with fails the way a deep tree is actually described out
 * loud: "cabinet a drawer 7" is three words in path order with the separators left
 * out, and a single substring test misses it. So every whitespace-separated token
 * must match, in any order.
 *
 * A token matches a **word prefix**, not any substring. Substring matching looks
 * more generous and is worse here: the single letter in "Cabinet A" is a real and
 * common token, and as a substring it appears inside almost every word in the tree,
 * so "cabinet a 07" would match every drawer of every cabinet. Word prefixes also
 * make the filter predictable: typing more always narrows, instead of widening
 * because a two-letter token turned up inside an unrelated word.
 *
 * Ranking exists because the first row is the one that gets pressed. A hit on the
 * container's **own name** beats a hit that only matched an ancestor's name — "07"
 * typed while looking for a drawer should not be outranked by the cabinet whose
 * path happens to contain it — and a shallower container wins ties, because that
 * is the one a person names when they mean a region rather than a cell.
 *
 * `slot_label` is searched too: a generated grid cell's printed identity is often
 * only its slot ("B3"), and that cell has no short ID until somebody mints one.
 */

import type { LocationNode } from "../api/client";

/** Tokens of a typed query, lowercased. Empty when nothing was typed. */
export function queryTokens(query: string): readonly string[] {
  return query
    .toLowerCase()
    .split(/[\s/]+/)
    .filter((token) => token !== "");
}

/** Words of the node's path and slot label, lowercased. */
function words(node: LocationNode): readonly string[] {
  return `${node.label_path} ${node.slot_label ?? ""}`
    .toLowerCase()
    .split(/[\s/]+/)
    .filter((word) => word !== "");
}

function score(node: LocationNode, tokens: readonly string[]): number {
  const name = node.name.toLowerCase();
  const slot = (node.slot_label ?? "").toLowerCase();
  let points = 0;
  for (const token of tokens) {
    if (name === token || slot === token) {
      points += 3;
    } else if (name.startsWith(token)) {
      points += 2;
    } else if (name.includes(token)) {
      points += 1;
    }
  }
  return points;
}

/**
 * Nodes matching every token, best first.
 *
 * Order is total and stable: score descending, then shallower first, then by path
 * so two equal candidates never swap places between renders.
 */
export function matchLocations(
  nodes: readonly LocationNode[],
  query: string,
): readonly LocationNode[] {
  const tokens = queryTokens(query);
  if (tokens.length === 0) {
    return [];
  }
  return nodes
    .filter((node) => {
      const text = words(node);
      return tokens.every((token) => text.some((word) => word.startsWith(token)));
    })
    .sort((left, right) => {
      const byScore = score(right, tokens) - score(left, tokens);
      if (byScore !== 0) {
        return byScore;
      }
      if (left.depth !== right.depth) {
        return left.depth - right.depth;
      }
      return left.label_path.localeCompare(right.label_path);
    });
}
