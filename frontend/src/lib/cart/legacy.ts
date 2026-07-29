/**
 * The single global cart #40 shipped, read once so its rows are not lost.
 *
 * `almagest.cart.v1` held `{target, lines}` for **one** cart. ADR 0010 makes the
 * cart per-target, so a user who upgrades mid-job has lines under a key nothing
 * reads any more — and those lines are a statement about parts they physically
 * moved. Dropping them silently is the one outcome this whole feature exists to
 * avoid, so they are carried over into the tab for the target they were aimed at,
 * and that tab is **opened**, because a migrated record nobody can see is the
 * invisible state the panel is the mitigation for.
 *
 * Two shapes cannot be carried over: a v1 cart with no destination chosen, and one
 * aimed at a *container* (v1's third door, which ADR 0010 retires — a tab is a
 * project or a build). Those are **left exactly where they are**, key and all.
 * Nothing reads them, but nothing has thrown them away either, so the rows are
 * still recoverable by hand rather than gone. `hasUnattributableLegacyCart` says
 * so out loud for the panel to mention.
 *
 * This module is deliberately dependency-light — it reads and deletes one key and
 * knows nothing about the store or the registry, so `migrateLegacyCart` can own
 * the ordering without a cycle.
 */

import { readTarget, type WorkTarget } from "../projectcontext/target";
import { type CartLine, type CartStorage } from "./cart";

export const LEGACY_STORAGE_KEY = "almagest.cart.v1";

export interface LegacyCart {
  /** `null` when the old cart had no destination, or one that no longer exists. */
  readonly target: WorkTarget | null;
  readonly lines: readonly CartLine[];
}

function isCartLine(value: unknown): value is CartLine {
  if (value === null || typeof value !== "object") {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    typeof record["id"] === "string" &&
    typeof record["clientOpId"] === "string" &&
    typeof record["partId"] === "number" &&
    typeof record["partName"] === "string" &&
    typeof record["qtyMilli"] === "number"
  );
}

export function readLegacyCart(storage: CartStorage | null): LegacyCart | null {
  try {
    const raw = storage?.getItem(LEGACY_STORAGE_KEY);
    if (raw === null || raw === undefined) {
      return null;
    }
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object") {
      return null;
    }
    const record = parsed as Record<string, unknown>;
    const lines = Array.isArray(record["lines"]) ? record["lines"].filter(isCartLine) : [];
    return { target: readTarget(record["target"]), lines };
  } catch {
    return null;
  }
}

/** True when a v1 cart is still sitting there because it could not be attributed. */
export function hasUnattributableLegacyCart(storage: CartStorage | null): boolean {
  const legacy = readLegacyCart(storage);
  return legacy !== null && legacy.target === null && legacy.lines.length > 0;
}

export function clearLegacyCart(storage: CartStorage | null): void {
  try {
    storage?.removeItem(LEGACY_STORAGE_KEY);
  } catch {
    // Nothing to do: the rows have already been copied into their tab, and a
    // storage that will not delete is not a reason to fail the migration.
  }
}
