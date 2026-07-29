/**
 * Carrying the v1 single cart into a tab.
 *
 * The rows under `almagest.cart.v1` are a statement about parts somebody
 * physically moved, so the subject here is that they are **moved, not dropped**,
 * that the tab they land in is opened so they are visible, that their per-line keys
 * survive (a re-keyed row the server already accepted applies twice instead of
 * replaying), and that a v1 cart nobody can attribute is left in place rather than
 * quietly deleted.
 */

import { beforeEach, describe, expect, it } from "vitest";

import { carts } from "../cart/registry";
import { LEGACY_STORAGE_KEY, hasUnattributableLegacyCart } from "../cart/legacy";
import { migrateLegacyCart } from "./migrate";
import { openTargets } from "./store";
import type { WorkTarget } from "./target";

const PROJECT: WorkTarget = { kind: "project", projectId: 12, label: "Bench PSU" };

function legacyLine(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "row-1",
    clientOpId: "key-1",
    partId: 42,
    partName: "GRM188R61A106KA73D",
    mpn: "GRM188R61A106KA73D",
    qtyMilli: 4_000,
    lotId: 7,
    locationId: 3,
    locationLabel: "A1-04",
    designator: "C7",
    direction: "take",
    addedAt: 1,
    failure: null,
    ...overrides,
  };
}

function writeLegacy(target: unknown, lines: readonly unknown[]): void {
  globalThis.localStorage.setItem(LEGACY_STORAGE_KEY, JSON.stringify({ target, lines }));
}

beforeEach(() => {
  globalThis.localStorage.clear();
  carts.reset();
  openTargets.reset();
});

describe("a v1 cart aimed at a project", () => {
  it("becomes that project's record, in an open tab", () => {
    writeLegacy({ kind: "project", projectId: 12, label: "Bench PSU" }, [legacyLine()]);

    const result = migrateLegacyCart();

    expect(result).toEqual({ migrated: 1, leftBehind: false });
    expect(openTargets.open()).toEqual([PROJECT]);
    expect(openTargets.focused).toEqual(PROJECT);
    const lines = carts.for(PROJECT).lines();
    expect(lines).toHaveLength(1);
    expect(lines[0]?.partName).toBe("GRM188R61A106KA73D");
    expect(lines[0]?.qtyMilli).toBe(4_000);
    expect(lines[0]?.designator).toBe("C7");
  });

  it("keeps each row's per-line key, so a row the server took is replayed", () => {
    writeLegacy({ kind: "project", projectId: 12, label: "Bench PSU" }, [legacyLine()]);
    migrateLegacyCart();
    expect(carts.for(PROJECT).lines()[0]?.clientOpId).toBe("key-1");
  });

  it("removes the old key, and running again changes nothing", () => {
    writeLegacy({ kind: "project", projectId: 12, label: "Bench PSU" }, [legacyLine()]);
    migrateLegacyCart();
    expect(globalThis.localStorage.getItem(LEGACY_STORAGE_KEY)).toBeNull();

    expect(migrateLegacyCart()).toEqual({ migrated: 0, leftBehind: false });
    expect(carts.for(PROJECT).size).toBe(1);
  });

  it("survives being read back from storage rather than from the instance", () => {
    writeLegacy({ kind: "project", projectId: 12, label: "Bench PSU" }, [legacyLine()]);
    migrateLegacyCart();

    carts.reset();
    openTargets.reset();

    expect(openTargets.open()).toEqual([PROJECT]);
    expect(carts.for(PROJECT).size).toBe(1);
  });
});

describe("a v1 cart that cannot be attributed", () => {
  it("is left exactly where it is when it had no destination", () => {
    // v1's "no target chosen yet" state. There is no tab to put it in, and ADR
    // 0010 forbids the silent discard, so nothing is touched and it stays
    // recoverable.
    writeLegacy({ kind: "unset" }, [legacyLine()]);

    expect(migrateLegacyCart()).toEqual({ migrated: 0, leftBehind: true });
    expect(globalThis.localStorage.getItem(LEGACY_STORAGE_KEY)).not.toBeNull();
    expect(hasUnattributableLegacyCart(globalThis.localStorage)).toBe(true);
    expect(openTargets.open()).toEqual([]);
  });

  it("is left in place when it was aimed at a container, which is no longer a tab", () => {
    writeLegacy({ kind: "container", locationId: 3, label: "A1-04" }, [legacyLine()]);
    expect(migrateLegacyCart()).toEqual({ migrated: 0, leftBehind: true });
    expect(globalThis.localStorage.getItem(LEGACY_STORAGE_KEY)).not.toBeNull();
  });
});

describe("nothing to migrate", () => {
  it("does nothing when there is no v1 key", () => {
    expect(migrateLegacyCart()).toEqual({ migrated: 0, leftBehind: false });
    expect(openTargets.open()).toEqual([]);
  });

  it("clears an empty v1 cart rather than leaving a dead key", () => {
    writeLegacy({ kind: "project", projectId: 12, label: "Bench PSU" }, []);
    expect(migrateLegacyCart()).toEqual({ migrated: 0, leftBehind: false });
    expect(globalThis.localStorage.getItem(LEGACY_STORAGE_KEY)).toBeNull();
  });

  it("ignores a corrupt v1 key", () => {
    globalThis.localStorage.setItem(LEGACY_STORAGE_KEY, "{not json");
    expect(migrateLegacyCart()).toEqual({ migrated: 0, leftBehind: false });
  });
});
