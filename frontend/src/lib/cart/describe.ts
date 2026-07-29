/**
 * Saying where a cart is headed, in one phrase, in the same words everywhere.
 *
 * Shared rather than inlined at each site because the destination is stated in
 * three places — the nav badge's tooltip, the shopping view and the cart screen —
 * and three different phrasings for one state is how a user ends up unsure
 * whether they are looking at the same cart.
 */

import type { CartTarget } from "./cart";

export function describeTarget(target: CartTarget): string {
  switch (target.kind) {
    case "unset":
      return "No destination chosen yet";
    case "project":
      return `Headed for the BOM of ${target.label}`;
    case "build":
      return `Headed for ${target.label}, as reserved stock`;
    case "container":
      return `Headed for ${target.label} as a stock movement`;
  }
}
