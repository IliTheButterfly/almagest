/**
 * Two copies of a form's state: the one being typed into, and the one being
 * fetched with.
 *
 * A parametric search fires two requests per change — the results and the facet
 * counts — so a dragged range input or a held-down key must not translate into a
 * request per pixel. But a ticked checkbox has to feel instant, and waiting 300ms
 * to acknowledge a tap reads as a broken app.
 *
 * Hence one hook with both: `commit(next)` defers, `commit(next, { immediate:
 * true })` does not, and `adopt(next)` replaces both copies without a request —
 * which is what a back button needs.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export interface DeferredCommit<T> {
  /** What the inputs render from. Updates on every keystroke. */
  readonly draft: T;
  /** What to fetch with. Trails `draft` by the delay. */
  readonly applied: T;
  readonly commit: (next: T, options?: { immediate?: boolean }) => void;
  /** Take an external value as both draft and applied, cancelling any pending
   * commit — the URL changed under us and the pending edit is stale. */
  readonly adopt: (next: T) => void;
}

export function useDeferredCommit<T>(initial: T, delayMs: number): DeferredCommit<T> {
  const [draft, setDraft] = useState<T>(initial);
  const [applied, setApplied] = useState<T>(initial);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const cancel = useCallback(() => {
    if (timer.current !== null) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  const commit = useCallback(
    (next: T, options?: { immediate?: boolean }) => {
      setDraft(next);
      cancel();
      if (options?.immediate === true) {
        setApplied(next);
        return;
      }
      timer.current = setTimeout(() => {
        timer.current = null;
        setApplied(next);
      }, delayMs);
    },
    [cancel, delayMs],
  );

  const adopt = useCallback(
    (next: T) => {
      cancel();
      setDraft(next);
      setApplied(next);
    },
    [cancel],
  );

  // A pending commit after unmount would set state on a dead component, which in
  // React 19 is a no-op with a warning — and in a slow network a wasted request.
  useEffect(() => cancel, [cancel]);

  return { draft, applied, commit, adopt };
}
