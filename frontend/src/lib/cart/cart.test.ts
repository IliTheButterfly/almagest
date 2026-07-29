/**
 * One target's record, as a data structure.
 *
 * The tests are about the rules that are easy to get subtly wrong and expensive
 * when they are: what merges and what does not (a lot is a physical package, so
 * merging across lots would lose which reel was in hand), that a take and a return
 * of the same part net into **one** line rather than two that contradict each other,
 * that two targets' records are separate objects with separate keys, that a gathered
 * record survives a reload, and that a row whose part has been deleted server-side is
 * still a legible, removable row rather than a hole in the list.
 */

import { beforeEach, describe, expect, it } from "vitest";

import type { WorkTarget } from "../projectcontext/target";
import { cartStorageKey, ShoppingCart, type CartLineDraft, type CartStorage } from "./cart";

const PROJECT: WorkTarget = { kind: "project", projectId: 12, label: "Bench PSU" };
const BUILD: WorkTarget = { kind: "build", buildId: 5, label: "rev B ×3" };

function memoryStorage(): CartStorage & { readonly map: Map<string, string> } {
  const map = new Map<string, string>();
  return {
    map,
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => {
      map.set(key, value);
    },
    removeItem: (key) => {
      map.delete(key);
    },
  };
}

function draft(overrides: Partial<CartLineDraft> = {}): CartLineDraft {
  return {
    partId: 42,
    partName: "GRM188R61A106KA73D",
    qtyMilli: 10_000,
    mpn: "GRM188R61A106KA73D",
    lotId: 7,
    locationId: 3,
    locationLabel: "A1-04",
    ...overrides,
  };
}

describe("a target's record", () => {
  let storage: CartStorage & { readonly map: Map<string, string> };

  beforeEach(() => {
    storage = memoryStorage();
  });

  it("starts empty, aimed at the target it was constructed with", () => {
    const cart = new ShoppingCart(PROJECT, storage);
    expect(cart.lines()).toEqual([]);
    expect(cart.size).toBe(0);
    expect(cart.target).toEqual(PROJECT);
  });

  it("adds nothing to the server — a record is a staging area", () => {
    // There is no fetch stub here on purpose: if adding ever started talking to
    // the API, jsdom's absent `fetch` would make this test fail rather than pass
    // quietly.
    const cart = new ShoppingCart(PROJECT, storage);
    cart.add(draft());
    expect(cart.size).toBe(1);
  });

  it("keeps rows in the order they were chosen", () => {
    const cart = new ShoppingCart(PROJECT, storage);
    cart.add(draft({ partId: 1, partName: "first", lotId: 1 }));
    cart.add(draft({ partId: 2, partName: "second", lotId: 2 }));
    expect(cart.lines().map((line) => line.partName)).toEqual(["first", "second"]);
  });

  it("captures the name and mpn it was given, staleness and all", () => {
    const cart = new ShoppingCart(PROJECT, storage);
    const line = cart.add(draft({ partName: "LM358N", mpn: "LM358N" }));
    expect(line?.partName).toBe("LM358N");
    expect(line?.mpn).toBe("LM358N");
    expect(line?.locationLabel).toBe("A1-04");
  });

  it("mints a row id and a separate per-line idempotency key", () => {
    const cart = new ShoppingCart(PROJECT, storage);
    const line = cart.add(draft());
    expect(line?.id).not.toBe("");
    expect(line?.clientOpId).not.toBe("");
    expect(line?.clientOpId).not.toBe(line?.id);
  });

  // ---------------------------------------------- one record per target ----

  it("keeps two targets' lines apart, under two keys", () => {
    // The reason the cart is per-target at all: one shared list would mean that
    // switching the focused tab silently re-aimed everything already gathered.
    const project = new ShoppingCart(PROJECT, storage);
    const build = new ShoppingCart(BUILD, storage);
    project.add(draft({ partName: "for the project" }));

    expect(project.size).toBe(1);
    expect(build.size).toBe(0);
    expect([...storage.map.keys()]).toEqual([cartStorageKey(PROJECT)]);

    build.add(draft({ partName: "for the build" }));
    expect(project.lines().map((line) => line.partName)).toEqual(["for the project"]);
    expect(build.lines().map((line) => line.partName)).toEqual(["for the build"]);
  });

  it("cannot collide a project's key with a build's of the same number", () => {
    expect(cartStorageKey({ kind: "project", projectId: 7, label: "" })).not.toBe(
      cartStorageKey({ kind: "build", buildId: 7, label: "" }),
    );
  });

  // ------------------------------------------------------------ merging ----

  it("bumps the quantity when the same part and lot is added again", () => {
    const cart = new ShoppingCart(PROJECT, storage);
    const first = cart.add(draft({ qtyMilli: 10_000 }));
    const again = cart.add(draft({ qtyMilli: 5_000 }));
    expect(cart.size).toBe(1);
    expect(again?.id).toBe(first?.id);
    expect(again?.qtyMilli).toBe(15_000);
  });

  it("does not merge the same part from a different lot", () => {
    // A lot is a physical package. Merging would lose which reel the parts came
    // out of, which is exactly what a reservation needs to know.
    const cart = new ShoppingCart(PROJECT, storage);
    cart.add(draft({ lotId: 7 }));
    cart.add(draft({ lotId: 8 }));
    expect(cart.size).toBe(2);
    expect(cart.lines().map((line) => line.lotId)).toEqual([7, 8]);
  });

  it("does not merge a lotless row into a lotted one", () => {
    const cart = new ShoppingCart(PROJECT, storage);
    cart.add(draft({ lotId: 7 }));
    cart.add(draft({ lotId: null }));
    expect(cart.size).toBe(2);
  });

  it("does not merge two rows with different designators", () => {
    // Same resistor, two places on the board: two requirements, and fusing them
    // would throw one of the designators away.
    const cart = new ShoppingCart(PROJECT, storage);
    cart.add(draft({ designator: "R1" }));
    cart.add(draft({ designator: "R4" }));
    expect(cart.size).toBe(2);
  });

  it("clears a stale refusal when a row is merged into", () => {
    const cart = new ShoppingCart(PROJECT, storage);
    const line = cart.add(draft());
    cart.markFailed(line?.id ?? "", {
      reason: "insufficient_stock",
      message: "only 3 left",
      at: 1,
    });
    cart.add(draft({ qtyMilli: 1_000 }));
    expect(cart.lines()[0]?.failure).toBeNull();
  });

  it("re-keys a row that is merged into, because its quantity changed", () => {
    // Same reason `setQuantity` re-keys, and the merge path mutates the same
    // field. The server keys a line replay on a digest of the line, so a key
    // already accepted for the old quantity — after a commit whose response was
    // lost — makes every later commit of this row a `request_mismatch` refusal
    // that no amount of retrying clears.
    const cart = new ShoppingCart(PROJECT, storage);
    const first = cart.add(draft({ qtyMilli: 1_000 }));
    const again = cart.add(draft({ qtyMilli: 1_000 }));
    expect(again?.qtyMilli).toBe(2_000);
    expect(again?.clientOpId).not.toBe(first?.clientOpId);
  });

  // -------------------------------------------- a return is symmetric ----

  it("nets a return against the take it undoes, as one line", () => {
    // "I took four and put one back" is one activity and has to read as one. Two
    // rows saying opposite things about the same package is a record the user then
    // has to reconcile in their head.
    const cart = new ShoppingCart(PROJECT, storage);
    cart.add(draft({ qtyMilli: 4_000, direction: "take" }));
    const netted = cart.add(draft({ qtyMilli: 1_000, direction: "return" }));

    expect(cart.size).toBe(1);
    expect(netted?.qtyMilli).toBe(3_000);
    expect(netted?.direction).toBe("take");
  });

  it("removes the row when a return cancels a take out exactly", () => {
    const cart = new ShoppingCart(PROJECT, storage);
    cart.add(draft({ qtyMilli: 4_000, direction: "take" }));
    const gone = cart.add(draft({ qtyMilli: 4_000, direction: "return" }));

    expect(gone).toBeNull();
    expect(cart.size).toBe(0);
  });

  it("flips the row's direction when a return nets past zero", () => {
    const cart = new ShoppingCart(PROJECT, storage);
    cart.add(draft({ qtyMilli: 1_000, direction: "take" }));
    const flipped = cart.add(draft({ qtyMilli: 3_000, direction: "return" }));

    expect(flipped?.direction).toBe("return");
    expect(flipped?.qtyMilli).toBe(2_000);
  });

  it("re-keys a row whose quantity is edited", () => {
    // The old key may already have been accepted for the old quantity, so
    // reusing it would replay that movement instead of applying this one.
    const cart = new ShoppingCart(PROJECT, storage);
    const line = cart.add(draft());
    cart.setQuantity(line?.id ?? "", 99_000);
    const updated = cart.lines()[0];
    expect(updated?.qtyMilli).toBe(99_000);
    expect(updated?.clientOpId).not.toBe(line?.clientOpId);
  });

  it("names the package a row comes out of, and re-keys it", () => {
    const cart = new ShoppingCart(PROJECT, storage);
    const line = cart.add(draft({ lotId: null, locationId: null, locationLabel: null }));
    cart.setLot(line?.id ?? "", { lotId: 91, locationId: 4, label: "B2-11" });
    const updated = cart.lines()[0];
    expect(updated?.lotId).toBe(91);
    expect(updated?.locationLabel).toBe("B2-11");
    expect(updated?.clientOpId).not.toBe(line?.clientOpId);
  });

  // -------------------------------------------------------- persistence ----

  it("survives a reload, which is why closing a tab has to ask", () => {
    const first = new ShoppingCart(PROJECT, storage);
    first.add(draft({ partName: "LM358N" }));

    const reloaded = new ShoppingCart(PROJECT, storage);
    expect(reloaded.size).toBe(1);
    expect(reloaded.lines()[0]?.partName).toBe("LM358N");
    expect(reloaded.target).toEqual(PROJECT);
  });

  it("round-trips every captured field, not just the identifiers", () => {
    const first = new ShoppingCart(PROJECT, storage);
    const line = first.add(draft({ designator: "C7", direction: "return" }));
    const reloaded = new ShoppingCart(PROJECT, storage).lines()[0];
    expect(reloaded).toEqual(line);
  });

  it("uses a versioned, per-target storage key", () => {
    new ShoppingCart(PROJECT, storage).add(draft());
    expect([...storage.map.keys()]).toEqual(["almagest.cart.v2.project.12"]);
  });

  it("leaves no key behind once its rows are gone", () => {
    const cart = new ShoppingCart(PROJECT, storage);
    cart.add(draft());
    cart.clear();
    expect(cart.size).toBe(0);
    expect([...storage.map.keys()]).toEqual([]);
    expect(new ShoppingCart(PROJECT, storage).size).toBe(0);
  });

  it("adopts existing rows without re-keying them", () => {
    // The v1 migration's path. A fresh key on a row the server may already have
    // accepted would apply it twice instead of replaying it.
    const source = new ShoppingCart(BUILD, memoryStorage());
    const line = source.add(draft());
    const cart = new ShoppingCart(PROJECT, storage);
    cart.adopt(source.lines());
    cart.adopt(source.lines());

    expect(cart.size).toBe(1);
    expect(cart.lines()[0]?.clientOpId).toBe(line?.clientOpId);
  });

  it("removes one row and several at once", () => {
    const cart = new ShoppingCart(PROJECT, storage);
    const one = cart.add(draft({ lotId: 1 }));
    const two = cart.add(draft({ lotId: 2 }));
    const three = cart.add(draft({ lotId: 3 }));
    cart.remove(two?.id ?? "");
    expect(cart.lines().map((line) => line.id)).toEqual([one?.id, three?.id]);
    cart.removeMany([one?.id ?? "", three?.id ?? ""]);
    expect(cart.size).toBe(0);
  });

  it("notifies subscribers on every change", () => {
    const cart = new ShoppingCart(PROJECT, storage);
    let notifications = 0;
    const unsubscribe = cart.subscribe(() => {
      notifications += 1;
    });
    const line = cart.add(draft());
    cart.setQuantity(line?.id ?? "", 5_000);
    cart.remove(line?.id ?? "");
    unsubscribe();
    cart.add(draft());
    expect(notifications).toBe(3);
  });

  it("ignores corrupt stored data rather than breaking the screen", () => {
    storage.setItem(cartStorageKey(PROJECT), "{not json");
    expect(new ShoppingCart(PROJECT, storage).lines()).toEqual([]);
  });

  it("drops stored rows that are not cart lines", () => {
    const real = new ShoppingCart(PROJECT, storage).add(draft());
    storage.setItem(
      cartStorageKey(PROJECT),
      JSON.stringify({ target: PROJECT, lines: [{ nope: true }, real] }),
    );
    expect(new ShoppingCart(PROJECT, storage).lines().map((line) => line.id)).toEqual([real?.id]);
  });

  it("degrades to memory when storage refuses to write", () => {
    const hostile: CartStorage = {
      getItem: () => null,
      setItem: () => {
        throw new DOMException("QuotaExceededError");
      },
      removeItem: () => undefined,
    };
    const cart = new ShoppingCart(PROJECT, hostile);
    expect(() => cart.add(draft())).not.toThrow();
    expect(cart.size).toBe(1);
  });

  it("works with no storage at all", () => {
    const cart = new ShoppingCart(PROJECT, null);
    cart.add(draft());
    expect(cart.size).toBe(1);
  });

  // ------------------------------------------------- the deleted part ----

  it("keeps a row whose part has since been deleted legible and removable", () => {
    // The captured name is the whole point: nothing here needs the part to still
    // exist in order to render the row or to take it out of the record. The
    // commit's own degradation is covered in `checkout.test.ts`.
    const cart = new ShoppingCart(PROJECT, storage);
    const line = cart.add(draft({ partId: 999, partName: "TL072CP (deleted)" }));
    cart.markFailed(line?.id ?? "", {
      reason: "unknown_part",
      message: "there is no part with id 999",
      at: 1,
    });

    const row = new ShoppingCart(PROJECT, storage).lines()[0];
    expect(row?.partName).toBe("TL072CP (deleted)");
    expect(row?.failure?.reason).toBe("unknown_part");

    cart.remove(line?.id ?? "");
    expect(cart.size).toBe(0);
  });
});
