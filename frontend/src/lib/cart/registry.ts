/**
 * One cart per open target, looked up by target.
 *
 * ADR 0010 makes "currently adding" a fact about a *tab*, so there is no longer a
 * single cart to import. Every consumer asks for the cart of a target instead, and
 * gets the **same instance** each time — which is what makes `useSyncExternalStore`
 * work at all: a fresh `ShoppingCart` per render would resubscribe to a different
 * object every time and never see a write.
 *
 * The instances are cached, not the data: each one owns its own `localStorage` key,
 * so a cart dropped from this map and asked for again reads the same rows back. That
 * is what `reset()` relies on — it exists for tests and for the legacy migration,
 * both of which write storage behind the instances' backs and need the next lookup
 * to re-read.
 */

import { targetKey, type TargetKey, type WorkTarget } from "../projectcontext/target";
import { ShoppingCart, type CartStorage } from "./cart";

class CartRegistry {
  readonly #storage: CartStorage | null;
  readonly #carts = new Map<TargetKey, ShoppingCart>();

  constructor(storage: CartStorage | null = defaultStorage()) {
    this.#storage = storage;
  }

  for(target: WorkTarget): ShoppingCart {
    const key = targetKey(target);
    const existing = this.#carts.get(key);
    if (existing !== undefined) {
      return existing;
    }
    const cart = new ShoppingCart(target, this.#storage);
    this.#carts.set(key, cart);
    return cart;
  }

  /** Forget the instances, keeping what they persisted. */
  reset(): void {
    this.#carts.clear();
  }
}

function defaultStorage(): CartStorage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export const carts = new CartRegistry();

export type { CartRegistry };
