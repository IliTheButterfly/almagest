/**
 * The open targets, subscribed to from React.
 *
 * `useSyncExternalStore` against the module singleton, the same idiom as
 * `lib/theme.ts` and the intake queue's badge. One hook per scalar rather than one
 * returning an object: `useSyncExternalStore` compares snapshots by identity, and a
 * fresh object every call is an infinite render loop.
 *
 * `useFocusedTarget` is the one that matters — it is what the take control reads to
 * decide whether it is filling a record or writing to the ledger, and `null` there
 * means "nothing is open", which is the immediate-commit path.
 */

import { useSyncExternalStore } from "react";

import { openTargets } from "./store";
import type { WorkTarget } from "./target";

const NONE: readonly WorkTarget[] = Object.freeze([]);

export function useOpenTargets(): readonly WorkTarget[] {
  return useSyncExternalStore(
    openTargets.subscribe,
    () => openTargets.open(),
    () => NONE,
  );
}

export function useFocusedTarget(): WorkTarget | null {
  return useSyncExternalStore(
    openTargets.subscribe,
    () => openTargets.focused,
    () => null,
  );
}
