/**
 * A media query as state.
 *
 * Used for the one thing CSS cannot express: whether the filter panel is a
 * permanent sidebar or a collapsed `<details>`. `open` is a DOM attribute, so the
 * layout decision has to exist in JS as well as in the stylesheet — the
 * breakpoint is deliberately the same 52rem the `.search-layout` grid uses.
 *
 * `useSyncExternalStore` rather than an effect: the first render already gets the
 * right answer, so a phone does not paint the desktop layout and reflow.
 */

import { useCallback, useSyncExternalStore } from "react";

export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (listener: () => void) => {
      const list = globalThis.matchMedia?.(query);
      list?.addEventListener("change", listener);
      return () => list?.removeEventListener("change", listener);
    },
    [query],
  );

  return useSyncExternalStore(
    subscribe,
    () => globalThis.matchMedia?.(query).matches ?? false,
    // Server-rendered or pre-hydration: assume the narrow layout, which is the
    // one that works either way.
    () => false,
  );
}
