/**
 * The open targets, and which one is focused — ADR 0010's tab strip, as state.
 *
 * The focused target is **what a take is attributed to**, which makes this the
 * most consequential piece of client state in the app: with it set, taking parts
 * writes a line to that target's record instead of the ledger. Three properties
 * follow from that and are enforced here rather than trusted to callers:
 *
 * 1. **Persisted**, because the strip has to survive a reload and a walk to the
 *    shelf. Same versioned-key convention as the cart and the intake queue.
 * 2. **Exactly one tab is focused whenever any tab is open.** Modelled as an
 *    invariant of the write path, not as a nullable the UI has to interpret, so
 *    "is anything open?" and "what will this take be attributed to?" are the same
 *    question — `focused === null` is precisely ADR 0010's "no tab open, take
 *    commits immediately".
 * 3. **Closing is not silent.** `close` refuses a tab that still holds
 *    uncommitted lines and says how many, because those lines are a statement
 *    about parts that physically moved. The caller may then ask the user and pass
 *    `discardLines`.
 *
 * The store idiom is `lib/theme.ts`'s and `lib/intake/queue.ts`'s: a module
 * singleton with `subscribe`, read from React through `useSyncExternalStore`. Not
 * a context — the take screen, the panel and the header all read it, and a
 * provider would add a second source of truth to keep in step.
 *
 * **Order is the order tabs were opened**, appended at the end, and there is no
 * reordering. Deliberate: the strip is small, position is a weak memory aid at
 * best, and a drag gesture next to a control that decides where stock is
 * attributed is a way to change focus by accident. Re-opening an already-open
 * target does not move it either; it refreshes the captured label and focuses it.
 */

import { carts } from "../cart/registry";
import { readTarget, sameTarget, targetKey, type TargetKey, type WorkTarget } from "./target";

const STORAGE_KEY = "almagest.opentargets.v1";

export interface TargetsStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/**
 * What happened when a tab was asked to close.
 *
 * A refusal carries the count rather than just saying no, so the caller can name
 * it — "4 lines have not been committed" is answerable, "cannot close" is not.
 */
export type CloseOutcome =
  | { readonly closed: true }
  | {
      readonly closed: false;
      readonly reason: "uncommitted_lines";
      readonly lines: number;
      readonly target: WorkTarget;
    };

interface StoredTargets {
  readonly open: readonly WorkTarget[];
  readonly focusedKey: TargetKey | null;
}

class OpenTargetsStore {
  readonly #storage: TargetsStorage | null;
  readonly #listeners = new Set<() => void>();
  #open: readonly WorkTarget[] = [];
  #focusedKey: TargetKey | null = null;

  constructor(storage: TargetsStorage | null = defaultStorage()) {
    this.#storage = storage;
    const stored = this.#load();
    this.#open = stored.open;
    this.#focusedKey = stored.focusedKey;
  }

  /** In the order they were opened. Referentially stable between writes. */
  open(): readonly WorkTarget[] {
    return this.#open;
  }

  /** `null` **only** when nothing is open — see the invariant above. */
  get focused(): WorkTarget | null {
    if (this.#focusedKey === null) {
      return null;
    }
    return this.#open.find((target) => targetKey(target) === this.#focusedKey) ?? null;
  }

  get focusedKey(): TargetKey | null {
    return this.#focusedKey;
  }

  isOpen(target: WorkTarget): boolean {
    return this.#open.some((candidate) => sameTarget(candidate, target));
  }

  /**
   * Open a project or a build as a tab, and focus it.
   *
   * Opening an already-open target is not an error and does not duplicate it: the
   * captured label is refreshed (the name may have changed since) and focus moves
   * there, which is what a second tap on "open" means.
   */
  openTarget(target: WorkTarget): void {
    const known = this.isOpen(target);
    const open = known
      ? this.#open.map((candidate) => (sameTarget(candidate, target) ? target : candidate))
      : [...this.#open, target];
    this.#write(open, targetKey(target));
  }

  focus(key: TargetKey): void {
    if (this.#open.some((target) => targetKey(target) === key)) {
      this.#write(this.#open, key);
    }
  }

  /**
   * Close a tab — unless it holds lines nobody has committed.
   *
   * `discardLines` is the caller having asked and been told yes; it empties that
   * target's record as well, because leaving the rows behind under a closed tab
   * would resurrect them the next time the target is opened, long after the user
   * said to throw them away.
   */
  close(key: TargetKey, options: { readonly discardLines?: boolean } = {}): CloseOutcome {
    const index = this.#open.findIndex((target) => targetKey(target) === key);
    const target = this.#open[index];
    if (index < 0 || target === undefined) {
      return { closed: true };
    }
    const cart = carts.for(target);
    if (cart.size > 0 && options.discardLines !== true) {
      return { closed: false, reason: "uncommitted_lines", lines: cart.size, target };
    }
    if (cart.size > 0) {
      cart.clear();
    }
    const open = this.#open.filter((_, at) => at !== index);
    // Focus lands on the neighbour that took its place, so closing the last tab
    // in the strip focuses the one before it rather than nothing.
    const next = open[Math.min(index, open.length - 1)];
    this.#write(open, next === undefined ? null : targetKey(next));
    return { closed: true };
  }

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };

  /** Re-read from storage: another tab wrote it, or a test is starting over. */
  reset(): void {
    const stored = this.#load();
    this.#open = stored.open;
    this.#focusedKey = stored.focusedKey;
    this.#notify();
  }

  #load(): StoredTargets {
    try {
      const raw = this.#storage?.getItem(STORAGE_KEY);
      if (raw === null || raw === undefined) {
        return { open: [], focusedKey: null };
      }
      const parsed: unknown = JSON.parse(raw);
      if (parsed === null || typeof parsed !== "object") {
        return { open: [], focusedKey: null };
      }
      const record = parsed as Record<string, unknown>;
      const raw_open = Array.isArray(record["open"]) ? record["open"] : [];
      const open: WorkTarget[] = [];
      for (const value of raw_open) {
        const target = readTarget(value);
        // An unreadable tab is dropped, and a duplicate collapses: both are
        // cheaper to lose than to carry forward as a strip that cannot be indexed
        // by key.
        if (target !== null && !open.some((candidate) => sameTarget(candidate, target))) {
          open.push(target);
        }
      }
      const stored = record["focusedKey"];
      const focusedKey = typeof stored === "string" ? stored : null;
      return { open, focusedKey: this.#validFocus(open, focusedKey) };
    } catch {
      return { open: [], focusedKey: null };
    }
  }

  /** The invariant: one focused tab while any is open, none when none is. */
  #validFocus(open: readonly WorkTarget[], wanted: TargetKey | null): TargetKey | null {
    if (open.length === 0) {
      return null;
    }
    if (wanted !== null && open.some((target) => targetKey(target) === wanted)) {
      return wanted;
    }
    const first = open[0];
    return first === undefined ? null : targetKey(first);
  }

  #write(open: readonly WorkTarget[], focusedKey: TargetKey | null): void {
    this.#open = [...open];
    this.#focusedKey = this.#validFocus(this.#open, focusedKey);
    try {
      if (this.#open.length === 0) {
        this.#storage?.removeItem(STORAGE_KEY);
      } else {
        this.#storage?.setItem(
          STORAGE_KEY,
          JSON.stringify({ open: this.#open, focusedKey: this.#focusedKey }),
        );
      }
    } catch {
      // In memory for the rest of the session, as everywhere else here: a
      // storage that will not write must not take the screen down with it.
    }
    this.#notify();
  }

  #notify(): void {
    for (const listener of this.#listeners) {
      listener();
    }
  }
}

function defaultStorage(): TargetsStorage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export const openTargets = new OpenTargetsStore();

export type { OpenTargetsStore };
