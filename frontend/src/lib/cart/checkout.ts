/**
 * Draining the cart to whichever of the three destinations it was aimed at.
 *
 * This is the cart's `lib/intake/sync.ts`, and it is the same shape on purpose
 * (ADR 0007). Three rules carry over unchanged, because they are what make a
 * gathered-then-committed list safe:
 *
 * - **A line is dropped from the cart only after the server says it applied.**
 *   Dropping first would lose the user's statement about what they physically did
 *   on any failed request, which is the single outcome this feature exists to
 *   avoid.
 * - **A failed line does not block the rest.** All three batch endpoints report
 *   per line and never roll a good line back for a bad one, so a lot that
 *   somebody else emptied while the cart was being filled fails *that* line. The
 *   reason is attached to the surviving row, so the cart itself explains why it
 *   is not empty.
 * - **It is safe to press twice.** Every line carries the `clientOpId` minted when
 *   it was *added*, so a resubmission — a double tap, a lost response, a retry
 *   after fixing one row — replays rather than doubles. On top of that a second
 *   press while the first is still in flight returns the *same* promise instead
 *   of issuing a second request, because two concurrent requests would both be
 *   guarded per line but would still race on which one gets to remove the rows.
 *
 * What this does not do is reconcile before committing. The cart deliberately
 * shows what it captured (see `cart.ts`), and the server is the thing that knows
 * whether a captured lot still holds what it did — so the reconciliation *is*
 * the checkout, and a line whose part has since been deleted comes back refused
 * and stays as a named, removable row rather than being an error that blocks the
 * whole checkout.
 */

import {
  allocateStockBatch,
  ApiError,
  moveStockBatch,
  updateBomLines,
  type AllocateLine,
  type BomLineEdit,
  type MovementLine,
} from "../api/client";
import { describeError } from "../api/errors";
import { uuid4 } from "../scan/session";
import { shoppingCart, type CartDirection, type CartLine, type ShoppingCart } from "./cart";

/** Which destination a checkout went to — ADR 0007's closed set of three. */
export type CheckoutDestination = "project" | "build" | "container";

/** One row that is still in the cart, and why. */
export interface CheckoutFailure {
  /** The `CartLine.id` of the row, so the UI can scroll to it. */
  readonly id: string;
  readonly reason: string | null;
  readonly message: string;
}

export interface CheckoutOutcome {
  readonly destination: CheckoutDestination | null;
  /** Rows the server accepted, now gone from the cart. */
  readonly applied: number;
  /** Of those, ones the server had already recorded under the same key. */
  readonly replayed: number;
  /** Rows kept, with why. */
  readonly failed: readonly CheckoutFailure[];
  /**
   * The stock destination's handle for undoing the entire checkout in one call.
   *
   * The batch shares one `group_uuid`, so "that was the wrong bin" is a single
   * `undoMovement` rather than one per row. `null` for the other two
   * destinations, which write no ledger rows.
   */
  readonly groupUuid: string | null;
  /** Why nothing was even attempted, when that is the case. */
  readonly notAttempted: "empty_cart" | "no_target" | null;
}

/** The per-line result shape all three endpoints share, narrowed to what we read. */
interface LineOutcome {
  readonly index: number;
  readonly client_line_id: string | null;
  readonly applied: boolean;
  readonly reason?: string | null;
  readonly message?: string | null;
  readonly replayed?: boolean;
}

/**
 * Checkouts in flight, keyed by cart.
 *
 * A `WeakMap` rather than a field on the cart: being mid-checkout is a fact about
 * this module's work, not part of the cart's persisted state, and it must not
 * survive a reload — a page refreshed mid-request has no promise left to join.
 */
const inFlight = new WeakMap<ShoppingCart, Promise<CheckoutOutcome>>();

export interface CheckoutOptions {
  /**
   * What a line means when it does not say — `take`, the overwhelming case.
   *
   * Only the stock destination reads this. Per-line `direction` still wins, so
   * one cart can hold both "I took five of these" and "I put three of those
   * back".
   */
  readonly defaultDirection?: CartDirection;
}

/**
 * Commit the cart. Idempotent, and safe to call again while it is running.
 *
 * Not `async`, deliberately: the in-flight promise has to be handed back
 * synchronously, and an `async` wrapper would return a *different* promise to the
 * second caller, which is exactly the double-press this guards.
 */
export function checkoutCart(
  cart: ShoppingCart = shoppingCart,
  options: CheckoutOptions = {},
): Promise<CheckoutOutcome> {
  const running = inFlight.get(cart);
  if (running !== undefined) {
    return running;
  }
  const attempt = run(cart, options).finally(() => {
    inFlight.delete(cart);
  });
  inFlight.set(cart, attempt);
  return attempt;
}

function nothing(notAttempted: "empty_cart" | "no_target"): CheckoutOutcome {
  return {
    destination: null,
    applied: 0,
    replayed: 0,
    failed: [],
    groupUuid: null,
    notAttempted,
  };
}

async function run(cart: ShoppingCart, options: CheckoutOptions): Promise<CheckoutOutcome> {
  const lines = [...cart.lines()];
  if (lines.length === 0) {
    return nothing("empty_cart");
  }
  const target = cart.target;
  if (target.kind === "unset") {
    // Refused locally rather than guessed. There is no default destination: the
    // three write to entirely different things.
    return nothing("no_target");
  }

  switch (target.kind) {
    case "project":
      return await toProjectBom(cart, lines, target.projectId);
    case "build":
      return await toBuild(cart, lines, target.buildId);
    case "container":
      return await toContainer(cart, lines, target.locationId, options.defaultDirection ?? "take");
  }
}

// ------------------------------------------------------- the three doors ----

/**
 * A project's BOM.
 *
 * `partial: true` matters: the route's default is all-or-nothing, which is right
 * for the BOM editor — a spreadsheet-style save should not half-apply — and wrong
 * for a cart, where nineteen good rows must not be discarded to protect the
 * twentieth. `is_match_confirmed` is set because a cart line is a part a human
 * picked out of search by hand; that is the definition of a confirmed match, and
 * leaving it unconfirmed would put every row into a review queue it does not
 * belong in.
 */
async function toProjectBom(
  cart: ShoppingCart,
  lines: readonly CartLine[],
  projectId: number,
): Promise<CheckoutOutcome> {
  const edits: BomLineEdit[] = lines.map((line) => ({
    client_line_id: line.id,
    client_op_id: line.clientOpId,
    part_id: line.partId,
    qty_per_assembly_milli: line.qtyMilli,
    designators: line.designator,
    mpn_raw: line.mpn,
    description: line.partName,
    is_match_confirmed: true,
  }));

  return await attempt(cart, lines, "project", null, async () => {
    const response = await updateBomLines(projectId, {
      client_op_id: uuid4(),
      partial: true,
      edits,
    });
    return { results: response.results, groupUuid: null };
  });
}

/**
 * A build's allocations.
 *
 * A hold is a hold *on a lot*, so a cart row with no lot cannot become one. That
 * is refused here without a request rather than sent for the server to reject:
 * the missing piece is a local one, and telling the user "this row needs a
 * container" is more useful than relaying a validation error about a field they
 * have never seen.
 */
async function toBuild(
  cart: ShoppingCart,
  lines: readonly CartLine[],
  buildId: number,
): Promise<CheckoutOutcome> {
  const allocatable: CartLine[] = [];
  const batch: AllocateLine[] = [];
  const localFailures: CheckoutFailure[] = [];
  for (const line of lines) {
    const lotId = line.lotId;
    if (lotId === null) {
      localFailures.push({
        id: line.id,
        reason: "no_lot",
        message:
          "Reserving stock needs a specific container, and this row does not name one. " +
          "Choose which package it comes from, or send it to the project's BOM instead.",
      });
      continue;
    }
    allocatable.push(line);
    batch.push({
      client_line_id: line.id,
      client_op_id: line.clientOpId,
      lot_id: lotId,
      part_id: line.partId,
      qty_milli: line.qtyMilli,
    });
  }
  for (const failure of localFailures) {
    cart.markFailed(failure.id, {
      reason: failure.reason,
      message: failure.message,
      at: Date.now(),
    });
  }
  if (allocatable.length === 0) {
    return {
      destination: "build",
      applied: 0,
      replayed: 0,
      failed: localFailures,
      groupUuid: null,
      notAttempted: null,
    };
  }

  const outcome = await attempt(cart, allocatable, "build", null, async () => {
    const response = await allocateStockBatch(buildId, {
      client_op_id: uuid4(),
      lines: batch,
    });
    return { results: response.results, groupUuid: null };
  });

  return { ...outcome, failed: [...localFailures, ...outcome.failed] };
}

/**
 * Plain stock, against one container — "pick a container, scan it and say how
 * many parts you took or put back", and the reason the cart is not a projects
 * feature.
 *
 * A line names its stock by `lot_id` when the user chose a package, and otherwise
 * by `part_id` resolved inside the scanned container *now*. Exactly one of the
 * two, which is what the endpoint validates: the first is exact and can go stale,
 * the second cannot go stale but is ambiguous if the bin holds two lots of the
 * part — and the server refuses that line rather than guessing.
 */
async function toContainer(
  cart: ShoppingCart,
  lines: readonly CartLine[],
  locationId: number,
  defaultDirection: CartDirection,
): Promise<CheckoutOutcome> {
  const movements: MovementLine[] = lines.map((line) => ({
    client_line_id: line.id,
    client_op_id: line.clientOpId,
    direction: line.direction ?? defaultDirection,
    qty_milli: line.qtyMilli,
    ...(line.lotId === null ? { part_id: line.partId } : { lot_id: line.lotId }),
  }));

  return await attempt(cart, lines, "container", null, async () => {
    const response = await moveStockBatch({
      client_op_id: uuid4(),
      location_id: locationId,
      lines: movements,
    });
    return { results: response.results, groupUuid: response.group_uuid };
  });
}

// ------------------------------------------------------------- plumbing ----

/**
 * Send one batch, then move the cart to match what came back.
 *
 * The whole-request failure path is the interesting one: an offline phone or a
 * 500 means *nothing* was applied, so every row stays and every row is told why.
 * Reporting that as "the checkout failed" without marking the rows would leave a
 * cart that looks untouched next to a banner that will be gone after one
 * navigation.
 */
async function attempt(
  cart: ShoppingCart,
  lines: readonly CartLine[],
  destination: CheckoutDestination,
  groupUuid: string | null,
  send: () => Promise<{
    readonly results: readonly LineOutcome[] | undefined;
    readonly groupUuid: string | null;
  }>,
): Promise<CheckoutOutcome> {
  try {
    const { results, groupUuid: group } = await send();
    return settle(cart, lines, destination, group ?? groupUuid, results);
  } catch (cause) {
    const message = describeFailure(cause);
    const reason = cause instanceof ApiError ? describeError(cause).reason : null;
    const at = Date.now();
    for (const line of lines) {
      cart.markFailed(line.id, { reason, message, at });
    }
    return {
      destination,
      applied: 0,
      replayed: 0,
      failed: lines.map((line) => ({ id: line.id, reason, message })),
      groupUuid: null,
      notAttempted: null,
    };
  }
}

/**
 * Apply the per-line verdicts.
 *
 * Matched on `client_line_id` when the verdict carries one, and on the index only
 * when it does not: the id is what the cart row is keyed by locally, whereas an
 * index is a promise that the cart was not reordered between building the request
 * and reading the answer — which, with a persisted cart open on two tabs, is not
 * a promise this code can make. A verdict naming an id this batch did not send is
 * therefore dropped rather than falling back to its position, because the
 * position is precisely the thing the id was preferred over: attributing it that
 * way would put a stranger's refusal on whichever row happens to sit there.
 *
 * A 2xx with no `results` at all is the non-partial contract: the batch either
 * applied whole or 4xx'd, so every line applied.
 */
function settle(
  cart: ShoppingCart,
  lines: readonly CartLine[],
  destination: CheckoutDestination,
  groupUuid: string | null,
  results: readonly LineOutcome[] | undefined,
): CheckoutOutcome {
  if (results === undefined) {
    cart.removeMany(lines.map((line) => line.id));
    return {
      destination,
      applied: lines.length,
      replayed: 0,
      failed: [],
      groupUuid,
      notAttempted: null,
    };
  }

  const appliedIds: string[] = [];
  const failed: CheckoutFailure[] = [];
  let replayed = 0;
  const at = Date.now();

  for (const result of results) {
    const line =
      result.client_line_id === null || result.client_line_id === undefined
        ? lines[result.index]
        : lines.find((candidate) => candidate.id === result.client_line_id);
    if (line === undefined) {
      // A verdict about a row we did not send. Nothing to do with it but ignore
      // it — acting on it would mean guessing which row it meant.
      continue;
    }
    if (result.applied) {
      appliedIds.push(line.id);
      if (result.replayed === true) {
        replayed += 1;
      }
      continue;
    }
    const failure = {
      id: line.id,
      reason: result.reason ?? null,
      message: result.message ?? "The server would not accept this line.",
    };
    failed.push(failure);
    cart.markFailed(line.id, { reason: failure.reason, message: failure.message, at });
  }

  // After the failures are marked, so one write does not undo the other.
  cart.removeMany(appliedIds);

  return {
    destination,
    applied: appliedIds.length,
    replayed,
    failed,
    groupUuid,
    notAttempted: null,
  };
}

/**
 * Why the whole request did not land, in terms of whether retrying can help.
 *
 * Same distinction `syncIntakeQueue` draws, and for the same reason: a 422 will
 * never succeed and needs a human, while a 503 or a dead network means try again.
 * `status === null` is no response at all — offline — and must not be reported as
 * a refusal that never happened.
 */
function describeFailure(cause: unknown): string {
  if (cause instanceof ApiError) {
    // Through `describeError`, not `cause.message`: the latter is the generic
    // string the client wrapper passed to `fail()`, while the server's real
    // `{reason, message}` is in `detail`.
    const { headline } = describeError(cause);
    if (cause.status !== null && cause.status >= 400 && cause.status < 500) {
      return `${headline} This cart cannot be checked out as it is, and needs looking at.`;
    }
    if (cause.status !== null) {
      return `${headline} The server could not take it; trying again may work.`;
    }
  }
  return "No connection to the server. The cart is still here; check out again when there is one.";
}
