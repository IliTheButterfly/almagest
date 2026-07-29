/**
 * The uncommitted lines of **one open target** — what ADR 0010 calls "currently
 * adding", and what the panel shows under a tab.
 *
 * The machinery here is #40's, kept deliberately: a versioned `localStorage`
 * shape, merge rules, captured-and-therefore-stale display text, a per-line
 * idempotency key, and the rule that a row whose part has since been deleted is
 * still a legible, removable row. What ADR 0010 changed is *whose* list it is.
 *
 * **A cart belongs to a target and never changes target.** One shared cart with a
 * settable destination — which is what #40 built — would mean that switching the
 * focused tab silently re-aimed everything already gathered, and that
 * misattribution is the exact failure this design exists to prevent. So the
 * target is a constructor argument, it is part of the storage key, and there is no
 * `setTarget`: re-aiming a gathered record is not an operation, opening a
 * different tab is.
 *
 * Two consequences of persisting it are load-bearing rather than incidental:
 *
 * - **Adding writes nothing.** Nothing touches the ledger until the tab is
 *   committed, which is what makes it safe to gather with, and it is
 *   `lib/cart/checkout.ts` that drains it — the same shape as
 *   `lib/intake/queue.ts` + `sync.ts`, down to the "a failed line stays put, with
 *   why" rule.
 * - **Captured names and quantities go stale.** A row shows what it captured when
 *   it was added and reconciles at commit; it never re-fetches to keep itself
 *   looking fresh, because its job is to record what the user did, not to mirror
 *   the database. Per-target carts multiply that exposure by however many tabs are
 *   open (ADR 0010), which is why the reconcile-at-commit rule survives unchanged.
 *
 * `localStorage` does not cross devices, so gathering on a phone at the shelf and
 * committing at the desktop will not work until this is server-side. Known cliff,
 * stated in the ADR; the intake queue hit the same wall.
 */

import { targetKey, type WorkTarget } from "../projectcontext/target";
import { uuid4 } from "../scan/session";

/**
 * One key per target, and a new version prefix.
 *
 * `v1` was the single global cart; `lib/cart/legacy.ts` migrates it. The target
 * key carries its kind, so a project 7 record and a build 7 record cannot land on
 * the same key.
 */
export function cartStorageKey(target: WorkTarget): string {
  return `almagest.cart.v2.${targetKey(target)}`;
}

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
/** `return` is a negative contribution to the record; unset means `take`. */
function sign(direction: CartDirection | null | undefined): 1 | -1 {
  return direction === "return" ? -1 : 1;
}

function signedQty(line: CartLine): number {
  return sign(line.direction) * line.qtyMilli;
}

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
  readonly #key: string;
  readonly #target: WorkTarget;
  readonly #listeners = new Set<() => void>();
  #lines: CartLine[];

  constructor(target: WorkTarget, storage: CartStorage | null = defaultStorage()) {
    this.#target = target;
    this.#key = cartStorageKey(target);
    this.#storage = storage;
    this.#lines = [...this.#load().lines];
  }

  lines(): readonly CartLine[] {
    return this.#lines;
  }

  /** The number a badge shows. Rows, not pieces — a cart of one reel is "1". */
  get size(): number {
    return this.#lines.length;
  }

  /** Fixed for the cart's whole life; see the module comment. */
  get target(): WorkTarget {
    return this.#target;
  }

  /**
   * Add what was just picked up — or **net it against** the row that already
   * holds it.
   *
   * Returns the row as it now stands, or `null` when the addition cancelled the
   * row out exactly and it was removed.
   *
   * The netting is ADR 0010's "return is symmetric": *"I took four and put one
   * back"* is **one activity** and has to read as one, so a `return` of a part
   * already taken in this record subtracts rather than opening a second row that
   * contradicts the first. If it nets past zero the row flips direction and keeps
   * the magnitude; if it nets to exactly zero the row goes, because a row that
   * says "nothing happened" is noise the user then has to interpret.
   *
   * Merging clears any refusal recorded on the row: the line has changed, so a
   * reason describing the quantity it *used* to ask for no longer describes it,
   * and leaving the stale text there would read as a fresh refusal of an attempt
   * that never happened.
   *
   * It also mints a fresh `clientOpId`, for the reason `setQuantity` does: this
   * changes the quantity, the server keys a line replay on a digest of the line,
   * and the old key may already have been accepted for the old quantity after a
   * commit whose response was lost. Keeping it would make every later commit of
   * this row a `request_mismatch` refusal — a row that can never be committed
   * again, and whose only escapes are Remove or nudging the quantity field.
   */
  add(draft: CartLineDraft): CartLine | null {
    const existing = this.#lines.find((line) => sameChoice(line, draft));
    if (existing !== undefined) {
      const net = signedQty(existing) + sign(draft.direction) * draft.qtyMilli;
      if (net === 0) {
        this.remove(existing.id);
        return null;
      }
      const merged: CartLine = {
        ...existing,
        qtyMilli: Math.abs(net),
        direction: net < 0 ? "return" : "take",
        clientOpId: uuid4(),
        // The freshest capture wins for display: the user just saw this name.
        partName: draft.partName,
        mpn: draft.mpn ?? existing.mpn,
        locationId: draft.locationId ?? existing.locationId,
        locationLabel: draft.locationLabel ?? existing.locationLabel,
        failure: null,
      };
      this.#write(this.#lines.map((line) => (line.id === existing.id ? merged : line)));
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
    this.#write([...this.#lines, line]);
    return line;
  }

  /**
   * Take on rows that already exist, keys and all.
   *
   * Only the v1 migration uses this, and it exists because `add` would re-key
   * every row: those rows may already have been accepted by the server under
   * their old key with the response lost, and a fresh key would apply them a
   * second time rather than replaying. A row whose id is already here is skipped,
   * so running the migration twice cannot duplicate anything.
   */
  adopt(lines: readonly CartLine[]): void {
    const known = new Set(this.#lines.map((line) => line.id));
    const adopted = lines.filter((line) => !known.has(line.id));
    if (adopted.length === 0) {
      return;
    }
    this.#write([...this.#lines, ...adopted]);
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
    );
  }

  /**
   * Name the physical package a row comes out of — or clear it.
   *
   * A reservation is a hold *on a lot*, so the build destination cannot use a row
   * that does not say which one; search knows what part was picked, not which
   * reel it comes off, so this is where that gap is closed. A fresh `clientOpId`
   * for the same reason as `setQuantity`: `lot_id` is in the line the server
   * digests, so an edited row is a different operation.
   */
  setLot(
    id: string,
    lot: { readonly lotId: number; readonly locationId: number | null; readonly label: string | null } | null,
  ): void {
    this.#write(
      this.#lines.map((line) =>
        line.id === id
          ? {
              ...line,
              lotId: lot?.lotId ?? null,
              locationId: lot === null ? null : lot.locationId,
              locationLabel: lot === null ? null : lot.label,
              clientOpId: uuid4(),
              failure: null,
            }
          : line,
      ),
    );
  }

  /** Attach why a line was refused, so the row can say so itself. */
  markFailed(id: string, failure: CartLineFailure): void {
    this.#write(
      this.#lines.map((line) => (line.id === id ? { ...line, failure } : line)),
    );
  }

  remove(id: string): void {
    this.#write(
      this.#lines.filter((line) => line.id !== id),
    );
  }

  /** Drop several rows at once — what a checkout does with the applied ones. */
  removeMany(ids: readonly string[]): void {
    const dropping = new Set(ids);
    this.#write(
      this.#lines.filter((line) => !dropping.has(line.id)),
    );
  }

  /**
   * Drop every row.
   *
   * The only place this is called from is closing a tab whose lines the user has
   * explicitly agreed to discard (ADR 0010 forbids doing it silently). The
   * target is not cleared because it cannot be: a cart *is* its target.
   */
  clear(): void {
    this.#write([]);
  }

  subscribe(listener: () => void): () => void {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  }

  #load(): StoredCart {
    try {
      const raw = this.#storage?.getItem(this.#key);
      if (raw === null || raw === undefined) {
        return { lines: [] };
      }
      const parsed: unknown = JSON.parse(raw);
      if (parsed === null || typeof parsed !== "object") {
        return { lines: [] };
      }
      const record = parsed as Record<string, unknown>;
      return { lines: Array.isArray(record["lines"]) ? record["lines"].filter(isCartLine) : [] };
    } catch {
      return { lines: [] };
    }
  }

  #write(lines: readonly CartLine[]): void {
    this.#lines = [...lines];
    try {
      if (lines.length === 0) {
        // An empty record leaves no key behind, so a tab that was opened, used
        // and emptied does not accumulate storage for the next reload to read.
        this.#storage?.removeItem(this.#key);
      } else {
        this.#storage?.setItem(this.#key, JSON.stringify({ target: this.#target, lines }));
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

/**
 * The **net** quantity a record holds for one part, signed: positive is stock
 * leaving the shelf for the target, negative is stock going back.
 *
 * Exported because the take screen states it back to the user ("3 of this lot in
 * this record now") and the panel totals it, and one arithmetic is one bug.
 */
export function netQtyMilli(lines: readonly CartLine[]): number {
  return lines.reduce((total, line) => total + signedQty(line), 0);
}
