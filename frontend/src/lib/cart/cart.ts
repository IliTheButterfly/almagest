/**
 * The cart — a staging area for parts chosen while browsing your own stock.
 *
 * ADR 0007: there are two ways of choosing parts. One is a BOM that arrived from
 * somewhere else, which the import path already serves. The other is *"you are
 * making the project and need to look at what parts we already have"* — a
 * session, not a gesture, inherently multi-item and exploratory. The cart is
 * that second path, and it is populated from the **ordinary search screen**,
 * because when the question is *what do I have* the facet counts and the
 * stock-per-row are the answer.
 *
 * **Adding to the cart writes nothing.** Nothing touches the ledger until
 * checkout, which is precisely what makes it safe to browse with, and it is
 * `lib/cart/checkout.ts` that drains it — deliberately the same shape as
 * `lib/intake/queue.ts` + `sync.ts`, down to the versioned `localStorage` key
 * and the "a failed line stays put, with why" rule. A second persistence
 * convention would be a second set of bugs.
 *
 * Two consequences of persisting it are load-bearing rather than incidental:
 *
 * - **It must be explicitly clearable.** The ADR is blunt that choosing the cart
 *   over project-as-a-mode does not avoid the invisible-state failure, it *moves*
 *   it — from "a mode I forgot is set" to "a cart I forgot is full". A visible
 *   count and a clear action are the mitigation, so `clear()` is part of the core
 *   and not a UI afterthought.
 * - **Captured names and quantities go stale.** The cart shows what it captured
 *   at the moment of adding and reconciles at checkout; it never re-fetches to
 *   keep a row looking fresh, because the row's job is to record what the user
 *   chose, not to mirror the database. A row whose part has since been *deleted*
 *   therefore still renders — from its own captured text — and stays removable.
 *
 * `localStorage` does not cross devices, so gathering on a phone at the shelf and
 * checking out at the desktop will not work until the cart is server-side. Known
 * cliff, stated in the ADR; the intake queue hit the same wall.
 */

import { uuid4 } from "../scan/session";

const STORAGE_KEY = "almagest.cart.v1";

/**
 * Where a cart is headed, as a state rather than a nullable field.
 *
 * A cart drains to exactly one of ADR 0007's three destinations, and *not having
 * chosen yet* is a legitimate, common state — you fill a cart while browsing and
 * decide what it was for afterwards. Modelling that as `null` scatters a
 * null-check through every caller and loses the distinction between "no target"
 * and "a target whose id we failed to read"; a discriminated union makes the
 * unchosen case something the type system forces you to handle exactly once.
 *
 * The label is captured, like everything else here: a project renamed after the
 * cart was filled shows the name that was on screen when the choice was made.
 */
export type CartTarget =
  | { readonly kind: "unset" }
  | { readonly kind: "project"; readonly projectId: number; readonly label: string }
  | { readonly kind: "build"; readonly buildId: number; readonly label: string }
  | { readonly kind: "container"; readonly locationId: number; readonly label: string };

export const NO_TARGET: CartTarget = { kind: "unset" };

/** Which way a stock-movement line goes. Mirrors the API's `MovementDirection`. */
export type CartDirection = "take" | "return";

/**
 * Why one line did not go through, kept **on the line** after a checkout.
 *
 * The refusal has to survive on the row rather than in a transient banner: after
 * a partial checkout the applied lines are gone and the refused ones are all
 * that is left in the cart, so if the reason lived anywhere else the user would
 * be looking at a cart of rows with no explanation for why they are still there.
 */
export interface CartLineFailure {
  /** The server's machine-readable code, e.g. `insufficient_stock`. */
  readonly reason: string | null;
  /** Prose fit to render. */
  readonly message: string;
  readonly at: number;
}

/**
 * One chosen part, with everything needed to render it without a fetch.
 *
 * `id` is the local row identity and travels as `client_line_id`, so a per-line
 * result can be matched back to the row the user is looking at without trusting
 * that the cart was not reordered mid-request. `clientOpId` is separate and is
 * the server's per-line idempotency key: minted **when the line is added**, not
 * at checkout, so that pressing checkout twice — or retrying a cart after one
 * line was refused — cannot double-apply a line that already went through.
 */
export interface CartLine {
  readonly id: string;
  readonly clientOpId: string;
  readonly partId: number;
  /** Captured at add time. Stale by design; it is what the user saw. */
  readonly partName: string;
  readonly mpn: string | null;
  readonly qtyMilli: number;
  /**
   * The physical package this came from, when the user chose a specific one.
   *
   * Not merely informational: a lot is a physical package, so the stock-movement
   * destination needs to know *which* reel or cut-tape strip was in hand, and it
   * is why two lines for the same part from different lots stay separate rows.
   */
  readonly lotId: number | null;
  readonly locationId: number | null;
  readonly locationLabel: string | null;
  /** `R1, R4` — carried to the BOM destination, ignored by the other two. */
  readonly designator: string | null;
  /** `take` or `return` for the stock destination; unset means the default. */
  readonly direction: CartDirection | null;
  readonly addedAt: number;
  readonly failure: CartLineFailure | null;
}

/** What `add` needs; the identities, the clock and the failure slot are ours. */
export interface CartLineDraft {
  readonly partId: number;
  readonly partName: string;
  readonly qtyMilli: number;
  readonly mpn?: string | null;
  readonly lotId?: number | null;
  readonly locationId?: number | null;
  readonly locationLabel?: string | null;
  readonly designator?: string | null;
  readonly direction?: CartDirection | null;
}

/**
 * The three methods of `localStorage` this uses.
 *
 * Structurally identical to `intake.QueueStorage` and deliberately *not* imported
 * from it — the two features share a convention, not a dependency, and a test
 * substituting a map for one must not have to know about the other.
 */
export interface CartStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

interface StoredCart {
  readonly target: CartTarget;
  readonly lines: readonly CartLine[];
}

function isCartLine(value: unknown): value is CartLine {
  if (value === null || typeof value !== "object") {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    typeof record["id"] === "string" &&
    typeof record["clientOpId"] === "string" &&
    typeof record["partId"] === "number" &&
    typeof record["partName"] === "string" &&
    typeof record["qtyMilli"] === "number"
  );
}

/**
 * A stored target, or `unset`.
 *
 * An unrecognised `kind` — a cart written by a newer version of the app, or a
 * hand-edited key — degrades to "no target chosen" rather than being carried
 * forward, because a target this code cannot interpret is one it must not check
 * out to. Losing the choice costs one tap; guessing it wrong writes to the wrong
 * project.
 */
function readTarget(value: unknown): CartTarget {
  if (value === null || typeof value !== "object") {
    return NO_TARGET;
  }
  const record = value as Record<string, unknown>;
  const label = typeof record["label"] === "string" ? record["label"] : "";
  switch (record["kind"]) {
    case "project":
      return typeof record["projectId"] === "number"
        ? { kind: "project", projectId: record["projectId"], label }
        : NO_TARGET;
    case "build":
      return typeof record["buildId"] === "number"
        ? { kind: "build", buildId: record["buildId"], label }
        : NO_TARGET;
    case "container":
      return typeof record["locationId"] === "number"
        ? { kind: "container", locationId: record["locationId"], label }
        : NO_TARGET;
    default:
      return NO_TARGET;
  }
}

/**
 * Do these two lines describe the same choice?
 *
 * **Part plus lot plus designator.** The part-and-lot half is ADR 0007's rule:
 * adding the same part from the same package again means "make it more", not
 * "add a second row", while adding the same part from a *different* lot must not
 * merge, because a lot is a physical package and the stock destination has to
 * know which one was in hand. The designator joins the key because it says what
 * a line is *for* — `R1` and `C4` of the same resistor are two requirements that
 * happen to share a part, and silently fusing them would destroy one of the two
 * designators the user typed.
 */
function sameChoice(line: CartLine, draft: CartLineDraft): boolean {
  return (
    line.partId === draft.partId &&
    line.lotId === (draft.lotId ?? null) &&
    line.designator === (draft.designator ?? null)
  );
}

/**
 * A cart that never throws at the caller.
 *
 * Storage can fail for reasons that have nothing to do with this app — private
 * browsing, a full quota, a locked-down profile. Losing a gathered cart is
 * annoying; taking the search screen down with it is worse, so every storage
 * failure degrades to an in-memory cart for the session, exactly as the intake
 * queue does.
 */
export class ShoppingCart {
  readonly #storage: CartStorage | null;
  readonly #listeners = new Set<() => void>();
  #lines: CartLine[];
  #target: CartTarget;

  constructor(storage: CartStorage | null = defaultStorage()) {
    this.#storage = storage;
    const stored = this.#load();
    this.#lines = [...stored.lines];
    this.#target = stored.target;
  }

  lines(): readonly CartLine[] {
    return this.#lines;
  }

  /** The number a badge shows. Rows, not pieces — a cart of one reel is "1". */
  get size(): number {
    return this.#lines.length;
  }

  get target(): CartTarget {
    return this.#target;
  }

  /**
   * Add a chosen part, or bump the quantity of the row that already holds it.
   *
   * Returns the row as it now stands. Merging clears any refusal recorded on the
   * row: the line has changed, so a reason describing the quantity it *used* to
   * ask for no longer describes it, and leaving the stale text there would read
   * as a fresh refusal of an attempt that never happened.
   */
  add(draft: CartLineDraft): CartLine {
    const existing = this.#lines.find((line) => sameChoice(line, draft));
    if (existing !== undefined) {
      const merged: CartLine = {
        ...existing,
        qtyMilli: existing.qtyMilli + draft.qtyMilli,
        // The freshest capture wins for display: the user just saw this name.
        partName: draft.partName,
        mpn: draft.mpn ?? existing.mpn,
        locationId: draft.locationId ?? existing.locationId,
        locationLabel: draft.locationLabel ?? existing.locationLabel,
        direction: draft.direction ?? existing.direction,
        failure: null,
      };
      this.#write(
        this.#lines.map((line) => (line.id === existing.id ? merged : line)),
        this.#target,
      );
      return merged;
    }

    const line: CartLine = {
      id: uuid4(),
      clientOpId: uuid4(),
      partId: draft.partId,
      partName: draft.partName,
      mpn: draft.mpn ?? null,
      qtyMilli: draft.qtyMilli,
      lotId: draft.lotId ?? null,
      locationId: draft.locationId ?? null,
      locationLabel: draft.locationLabel ?? null,
      designator: draft.designator ?? null,
      direction: draft.direction ?? null,
      addedAt: Date.now(),
      failure: null,
    };
    this.#write([...this.#lines, line], this.#target);
    return line;
  }

  /**
   * Set a row's quantity outright.
   *
   * A fresh `clientOpId` comes with it. The old key may already have been
   * accepted by the server for the old quantity — after a partial checkout, in
   * particular — and reusing it would make this edit replay as that earlier,
   * different movement rather than being applied.
   */
  setQuantity(id: string, qtyMilli: number): void {
    this.#write(
      this.#lines.map((line) =>
        line.id === id ? { ...line, qtyMilli, clientOpId: uuid4(), failure: null } : line,
      ),
      this.#target,
    );
  }

  /** Attach why a line was refused, so the row can say so itself. */
  markFailed(id: string, failure: CartLineFailure): void {
    this.#write(
      this.#lines.map((line) => (line.id === id ? { ...line, failure } : line)),
      this.#target,
    );
  }

  remove(id: string): void {
    this.#write(
      this.#lines.filter((line) => line.id !== id),
      this.#target,
    );
  }

  /** Drop several rows at once — what a checkout does with the applied ones. */
  removeMany(ids: readonly string[]): void {
    const dropping = new Set(ids);
    this.#write(
      this.#lines.filter((line) => !dropping.has(line.id)),
      this.#target,
    );
  }

  /**
   * Empty the cart, target and all.
   *
   * The mitigation for the failure ADR 0007 says the cart *moves* rather than
   * avoids: a forgotten full cart. Clearing the target too is the point — a
   * leftover "you are shopping for project X" is the invisible mode all over
   * again.
   */
  clear(): void {
    this.#write([], NO_TARGET);
  }

  setTarget(target: CartTarget): void {
    this.#write(this.#lines, target);
  }

  subscribe(listener: () => void): () => void {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  }

  #load(): StoredCart {
    try {
      const raw = this.#storage?.getItem(STORAGE_KEY);
      if (raw === null || raw === undefined) {
        return { target: NO_TARGET, lines: [] };
      }
      const parsed: unknown = JSON.parse(raw);
      if (parsed === null || typeof parsed !== "object") {
        return { target: NO_TARGET, lines: [] };
      }
      const record = parsed as Record<string, unknown>;
      const lines = Array.isArray(record["lines"]) ? record["lines"].filter(isCartLine) : [];
      return { target: readTarget(record["target"]), lines };
    } catch {
      return { target: NO_TARGET, lines: [] };
    }
  }

  #write(lines: readonly CartLine[], target: CartTarget): void {
    this.#lines = [...lines];
    this.#target = target;
    try {
      if (lines.length === 0 && target.kind === "unset") {
        this.#storage?.removeItem(STORAGE_KEY);
      } else {
        this.#storage?.setItem(STORAGE_KEY, JSON.stringify({ target, lines }));
      }
    } catch {
      // In-memory only for the rest of the session.
    }
    for (const listener of this.#listeners) {
      listener();
    }
  }
}

function defaultStorage(): CartStorage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

/** The one cart. A second would be a second thing to forget you had filled. */
export const shoppingCart = new ShoppingCart();
