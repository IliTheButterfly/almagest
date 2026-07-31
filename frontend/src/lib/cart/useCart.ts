/**
 * One target's record, subscribed to from React.
 *
 * `useSyncExternalStore` rather than a context: a cart is a module-level object
 * (owned by `lib/cart/registry.ts`) that outlives any tree, and every mutation
 * already notifies. Wrapping it in a provider would add a second source of truth.
 *
 * Each hook takes the **cart** rather than reading a singleton, because ADR 0010
 * makes the cart a fact about a tab: the take screen reads the focused tab's, and
 * the panel reads whichever tab it is drawing. `null` is accepted and means no tab
 * is open — the caller then gets an empty list rather than branching before the
 * hook, which would break the rules of hooks.
 *
 * Each hook reads one field so a component re-renders for the thing it shows. The
 * snapshots are the cart's own stored values, replaced rather than mutated on
 * write, so they are referentially stable between writes and the store never tears.
 */

import { useCallback, useSyncExternalStore } from "react";

import type { CartLine, ShoppingCart } from "./cart";

/** Server-render and hydration snapshot: no `localStorage` exists there. */
const EMPTY: readonly CartLine[] = Object.freeze([]);

/**
 * Memoised on the cart, not rebuilt per render: `useSyncExternalStore` tears down
 * and re-establishes the subscription whenever the function's identity changes.
 */
function useSubscriber(cart: ShoppingCart | null): (listener: () => void) => () => void {
  return useCallback(
    (listener: () => void): (() => void) => {
      if (cart === null) {
        return () => undefined;
      }
      return cart.subscribe(listener);
    },
    [cart],
  );
}

export function useCartLines(cart: ShoppingCart | null): readonly CartLine[] {
  return useSyncExternalStore(
    useSubscriber(cart),
    () => cart?.lines() ?? EMPTY,
    () => EMPTY,
  );
}

export function useCartSize(cart: ShoppingCart | null): number {
  return useSyncExternalStore(
    useSubscriber(cart),
    () => cart?.size ?? 0,
    () => 0,
  );
}
