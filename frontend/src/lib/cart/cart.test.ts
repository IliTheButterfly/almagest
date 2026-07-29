/**
 * The cart's data structure.
 *
 * The tests are about the rules that are easy to get subtly wrong and expensive
 * when they are: what merges and what does not (a lot is a physical package, so
 * merging across lots would lose which reel was in hand), that a gathered cart
 * survives a reload, and that a row whose part has been deleted server-side is
 * still a legible, removable row rather than a hole in the list.
 */

import { beforeEach, describe, expect, it } from "vitest";

import { NO_TARGET, ShoppingCart, type CartLineDraft, type CartStorage } from "./cart";

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

describe("the cart", () => {
  let storage: CartStorage & { readonly map: Map<string, string> };

  beforeEach(() => {
    storage = memoryStorage();
  });

  it("starts empty, with no target chosen", () => {
    const cart = new ShoppingCart(storage);
    expect(cart.lines()).toEqual([]);
    expect(cart.size).toBe(0);
    expect(cart.target).toEqual(NO_TARGET);
  });

  it("adds nothing to the server — the cart is a staging area", () => {
    // There is no fetch stub here on purpose: if adding ever started talking to
    // the API, jsdom's absent `fetch` would make this test fail rather than pass
    // quietly.
    const cart = new ShoppingCart(storage);
    cart.add(draft());
    expect(cart.size).toBe(1);
  });

  it("keeps rows in the order they were chosen", () => {
    const cart = new ShoppingCart(storage);
    cart.add(draft({ partId: 1, partName: "first", lotId: 1 }));
    cart.add(draft({ partId: 2, partName: "second", lotId: 2 }));
    expect(cart.lines().map((line) => line.partName)).toEqual(["first", "second"]);
  });

  it("captures the name and mpn it was given, staleness and all", () => {
    const cart = new ShoppingCart(storage);
    const line = cart.add(draft({ partName: "LM358N", mpn: "LM358N" }));
    expect(line.partName).toBe("LM358N");
    expect(line.mpn).toBe("LM358N");
    expect(line.locationLabel).toBe("A1-04");
  });

  it("mints a row id and a separate per-line idempotency key", () => {
    const cart = new ShoppingCart(storage);
    const line = cart.add(draft());
    expect(line.id).not.toBe("");
    expect(line.clientOpId).not.toBe("");
    expect(line.clientOpId).not.toBe(line.id);
  });

  // ------------------------------------------------------------ merging ----

  it("bumps the quantity when the same part and lot is added again", () => {
    const cart = new ShoppingCart(storage);
    const first = cart.add(draft({ qtyMilli: 10_000 }));
    const again = cart.add(draft({ qtyMilli: 5_000 }));
    expect(cart.size).toBe(1);
    expect(again.id).toBe(first.id);
    expect(again.qtyMilli).toBe(15_000);
  });

  it("does not merge the same part from a different lot", () => {
    // A lot is a physical package. Merging would lose which reel the parts came
    // out of, which is exactly what the stock-movement checkout needs to know.
    const cart = new ShoppingCart(storage);
    cart.add(draft({ lotId: 7 }));
    cart.add(draft({ lotId: 8 }));
    expect(cart.size).toBe(2);
    expect(cart.lines().map((line) => line.lotId)).toEqual([7, 8]);
  });

  it("does not merge a lotless row into a lotted one", () => {
    const cart = new ShoppingCart(storage);
    cart.add(draft({ lotId: 7 }));
    cart.add(draft({ lotId: null }));
    expect(cart.size).toBe(2);
  });

  it("does not merge two rows with different designators", () => {
    // Same resistor, two places on the board: two requirements, and fusing them
    // would throw one of the designators away.
    const cart = new ShoppingCart(storage);
    cart.add(draft({ designator: "R1" }));
    cart.add(draft({ designator: "R4" }));
    expect(cart.size).toBe(2);
  });

  it("clears a stale refusal when a row is merged into", () => {
    const cart = new ShoppingCart(storage);
    const line = cart.add(draft());
    cart.markFailed(line.id, { reason: "insufficient_stock", message: "only 3 left", at: 1 });
    cart.add(draft({ qtyMilli: 1_000 }));
    expect(cart.lines()[0]?.failure).toBeNull();
  });

  it("re-keys a row whose quantity is edited", () => {
    // The old key may already have been accepted for the old quantity, so
    // reusing it would replay that movement instead of applying this one.
    const cart = new ShoppingCart(storage);
    const line = cart.add(draft());
    cart.setQuantity(line.id, 99_000);
    const updated = cart.lines()[0];
    expect(updated?.qtyMilli).toBe(99_000);
    expect(updated?.clientOpId).not.toBe(line.clientOpId);
  });

  // -------------------------------------------------------- persistence ----

  it("survives a reload, which is why it needs clearing", () => {
    const first = new ShoppingCart(storage);
    first.add(draft({ partName: "LM358N" }));
    first.setTarget({ kind: "project", projectId: 12, label: "Bench PSU" });

    const reloaded = new ShoppingCart(storage);
    expect(reloaded.size).toBe(1);
    expect(reloaded.lines()[0]?.partName).toBe("LM358N");
    expect(reloaded.target).toEqual({ kind: "project", projectId: 12, label: "Bench PSU" });
  });

  it("round-trips every captured field, not just the identifiers", () => {
    const first = new ShoppingCart(storage);
    const line = first.add(draft({ designator: "C7", direction: "return" }));
    const reloaded = new ShoppingCart(storage).lines()[0];
    expect(reloaded).toEqual(line);
  });

  it("uses a versioned storage key", () => {
    new ShoppingCart(storage).add(draft());
    expect([...storage.map.keys()]).toEqual(["almagest.cart.v1"]);
  });

  it("clears the rows and the target together", () => {
    // A leftover "you are shopping for project X" is the invisible mode the ADR
    // chose the cart to avoid.
    const cart = new ShoppingCart(storage);
    cart.add(draft());
    cart.setTarget({ kind: "build", buildId: 5, label: "rev B ×3" });
    cart.clear();
    expect(cart.size).toBe(0);
    expect(cart.target).toEqual(NO_TARGET);
    expect(new ShoppingCart(storage).target).toEqual(NO_TARGET);
  });

  it("removes one row and several at once", () => {
    const cart = new ShoppingCart(storage);
    const one = cart.add(draft({ lotId: 1 }));
    const two = cart.add(draft({ lotId: 2 }));
    const three = cart.add(draft({ lotId: 3 }));
    cart.remove(two.id);
    expect(cart.lines().map((line) => line.id)).toEqual([one.id, three.id]);
    cart.removeMany([one.id, three.id]);
    expect(cart.size).toBe(0);
  });

  it("notifies subscribers on every change", () => {
    const cart = new ShoppingCart(storage);
    let notifications = 0;
    const unsubscribe = cart.subscribe(() => {
      notifications += 1;
    });
    const line = cart.add(draft());
    cart.setTarget({ kind: "container", locationId: 9, label: "A1-04" });
    cart.remove(line.id);
    unsubscribe();
    cart.add(draft());
    expect(notifications).toBe(3);
  });

  it("ignores corrupt stored data rather than breaking the search screen", () => {
    storage.setItem("almagest.cart.v1", "{not json");
    expect(new ShoppingCart(storage).lines()).toEqual([]);
  });

  it("drops stored rows that are not cart lines", () => {
    const real = new ShoppingCart(storage).add(draft());
    storage.setItem(
      "almagest.cart.v1",
      JSON.stringify({ target: NO_TARGET, lines: [{ nope: true }, real] }),
    );
    expect(new ShoppingCart(storage).lines().map((line) => line.id)).toEqual([real.id]);
  });

  it("degrades an uninterpretable target to no target, rather than guessing one", () => {
    // A cart written by a newer version of the app. Losing the choice costs one
    // tap; guessing it wrong writes to the wrong project.
    storage.setItem(
      "almagest.cart.v1",
      JSON.stringify({ target: { kind: "workshop", workshopId: 3 }, lines: [] }),
    );
    expect(new ShoppingCart(storage).target).toEqual(NO_TARGET);
  });

  it("degrades a target missing its id to no target", () => {
    storage.setItem(
      "almagest.cart.v1",
      JSON.stringify({ target: { kind: "project", label: "Bench PSU" }, lines: [] }),
    );
    expect(new ShoppingCart(storage).target).toEqual(NO_TARGET);
  });

  it("degrades to memory when storage refuses to write", () => {
    const hostile: CartStorage = {
      getItem: () => null,
      setItem: () => {
        throw new DOMException("QuotaExceededError");
      },
      removeItem: () => undefined,
    };
    const cart = new ShoppingCart(hostile);
    expect(() => cart.add(draft())).not.toThrow();
    expect(cart.size).toBe(1);
  });

  it("works with no storage at all", () => {
    const cart = new ShoppingCart(null);
    cart.add(draft());
    expect(cart.size).toBe(1);
  });

  // ------------------------------------------------- the deleted part ----

  it("keeps a row whose part has since been deleted legible and removable", () => {
    // The captured name is the whole point: nothing here needs the part to still
    // exist in order to render the row or to take it out of the cart. The
    // checkout's own degradation is covered in `checkout.test.ts`.
    const cart = new ShoppingCart(storage);
    const line = cart.add(draft({ partId: 999, partName: "TL072CP (deleted)" }));
    cart.markFailed(line.id, {
      reason: "unknown_part",
      message: "there is no part with id 999",
      at: 1,
    });

    const row = new ShoppingCart(storage).lines()[0];
    expect(row?.partName).toBe("TL072CP (deleted)");
    expect(row?.failure?.reason).toBe("unknown_part");

    cart.remove(line.id);
    expect(cart.size).toBe(0);
  });
});
