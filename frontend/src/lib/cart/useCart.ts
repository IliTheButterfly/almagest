/**
 * The cart, subscribed to from React.
 *
 * `useSyncExternalStore` rather than a context provider: the cart is a module
 * singleton that outlives any tree (it is read by the nav badge, the shopping
 * view and the cart screen at once), and every mutation already notifies. Wrapping
 * it in a provider would add a second source of truth to keep in step for no
 * capability.
 *
 * Each hook reads one field so a component re-renders for the thing it shows —
 * the badge for the count, the cart screen for the rows. The snapshots are the
 * cart's own stored values, which are replaced rather than mutated on write, so
 * they are referentially stable between writes and the store never tears.
 */

import { useSyncExternalStore } from "react";

import { shoppingCart, NO_TARGET, type CartLine, type CartTarget } from "./cart";

const subscribe = (listener: () => void): (() => void) => shoppingCart.subscribe(listener);

/** Server-render and hydration snapshot: no `localStorage` exists there. */
const EMPTY: readonly CartLine[] = Object.freeze([]);

export function useCartLines(): readonly CartLine[] {
  return useSyncExternalStore(
    subscribe,
    () => shoppingCart.lines(),
    () => EMPTY,
  );
}

export function useCartSize(): number {
  return useSyncExternalStore(
    subscribe,
    () => shoppingCart.size,
    () => 0,
  );
}

export function useCartTarget(): CartTarget {
  return useSyncExternalStore(
    subscribe,
    () => shoppingCart.target,
    () => NO_TARGET,
  );
}
