/**
 * Carrying a v1 cart into the tab it was aimed at, once, at startup.
 *
 * It lives here rather than in `lib/cart/` because it is the one operation that
 * needs both halves — the rows come from `lib/cart/legacy.ts` and the tab from the
 * store — and putting it in either would make the two import each other.
 *
 * Called from `main.tsx` before the first render, so nothing can read a half
 * migrated state. Idempotent: the v1 key is removed once its rows have been
 * written, and a v1 cart whose destination cannot be expressed as a tab is left
 * untouched (see `legacy.ts`), so running it again is a no-op either way.
 */

import { carts } from "../cart/registry";
import { clearLegacyCart, readLegacyCart } from "../cart/legacy";
import type { CartStorage } from "../cart/cart";
import { openTargets } from "./store";

export interface MigrationResult {
  /** How many rows were carried over. Zero when there was nothing to do. */
  readonly migrated: number;
  /** True when a v1 cart was left in place because it named no tab-able target. */
  readonly leftBehind: boolean;
}

export function migrateLegacyCart(
  storage: CartStorage | null = defaultStorage(),
): MigrationResult {
  const legacy = readLegacyCart(storage);
  if (legacy === null || legacy.lines.length === 0) {
    if (legacy !== null) {
      clearLegacyCart(storage);
    }
    return { migrated: 0, leftBehind: false };
  }
  const target = legacy.target;
  if (target === null) {
    return { migrated: 0, leftBehind: true };
  }

  // Opened as a tab, not just filled: a migrated record nobody can see is the
  // invisible state the panel exists to prevent, and these rows say parts moved.
  openTargets.openTarget(target);
  // `adopt`, not `add`: the rows keep their per-line keys, because a v1 row may
  // already have been accepted by the server with the response lost, and a fresh
  // key would apply it a second time instead of replaying it.
  carts.for(target).adopt(legacy.lines);
  clearLegacyCart(storage);
  return { migrated: legacy.lines.length, leftBehind: false };
}

function defaultStorage(): CartStorage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}
