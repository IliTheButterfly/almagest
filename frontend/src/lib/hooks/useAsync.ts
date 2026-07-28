/**
 * The one data-fetching hook.
 *
 * Deliberately not a query library. Every screen here loads one or two documents
 * and reloads them after a write; a cache with invalidation would be more code
 * than the screens it serves. `reload()` covers the only cache-coherence problem
 * that exists — "I just committed a movement, show me the new balance".
 */

import { useCallback, useEffect, useRef, useState } from "react";

export interface AsyncState<T> {
  readonly data: T | null;
  readonly error: unknown;
  readonly loading: boolean;
  readonly reload: () => void;
}

export function useAsync<T>(load: () => Promise<T>, deps: readonly unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  // Held in a ref so `deps` alone drives re-fetching. Putting the callback in the
  // dependency list would re-fetch on every render for any caller that writes the
  // loader inline, which is every caller.
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    let live = true;
    setLoading(true);
    loadRef
      .current()
      .then((result) => {
        if (live) {
          setData(result);
          setError(null);
        }
      })
      .catch((cause: unknown) => {
        if (live) {
          setError(cause);
        }
      })
      .finally(() => {
        if (live) {
          setLoading(false);
        }
      });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  return { data, error, loading, reload };
}
