/**
 * Committing one tab's record against that tab's target.
 *
 * This is the cart's `lib/intake/sync.ts`, and #40's machinery survives ADR 0010
 * intact — only the *choice* of destination is gone, because the destination is
 * the tab. Three rules carry over unchanged, because they are what make a
 * gathered-then-committed list safe:
 *
 * - **A line is dropped from the record only after the server says it applied.**
 *   Dropping first would lose the user's statement about what they physically did
 *   on any failed request, which is the single outcome this feature exists to
 *   avoid.
 * - **A failed line does not block the rest.** Both batch endpoints report per
 *   line and never roll a good line back for a bad one, so a lot that somebody
 *   else emptied while the record was being gathered fails *that* line. The reason
 *   is attached to the surviving row, so the record itself explains why it is not
 *   empty.
 * - **It is safe to press twice.** Every line carries the `clientOpId` minted when
 *   it was *added*, so a resubmission — a double tap, a lost response, a retry
 *   after fixing one row — replays rather than doubles. On top of that a second
 *   press while the first is still in flight returns the *same* promise instead
 *   of issuing a second request, because two concurrent requests would both be
 *   guarded per line but would still race on which one gets to remove the rows.
 *
 * Committing one tab touches nothing in any other tab: each has its own cart, its
 * own keys and its own target, and this function is handed one of them.
 *
 * What it does not do is reconcile before committing. The record deliberately shows
 * what it captured (see `cart.ts`), and the server is the thing that knows whether
 * a captured lot still holds what it did — so the reconciliation *is* the commit,
 * and a line whose part has since been deleted comes back refused and stays as a
 * named, removable row rather than being an error that blocks the whole batch.
 */

import {
  ApiError,
  getBuild,
  listBomLines,
  stageStock,
  updateBomLines,
  type BomLineEdit,
} from "../api/client";
import { describeError } from "../api/errors";
import { uuid4 } from "../scan/session";
import { type CartLine, type ShoppingCart } from "./cart";

/** What kind of target the record was applied to — ADR 0010's two tab kinds. */
export type CheckoutDestination = "project" | "build";

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
   * The handle for undoing an entire batch of ledger rows in one call.
   *
   * Always `null` as things stand: neither of ADR 0010's two destinations writes
   * ledger rows — a project's BOM is a plan, and a build's allocations are holds.
   * Kept because the shape is what the batch endpoints report and because the
   * ledger-writing batch route still exists; a destination that does write rows
   * would fill it in rather than inventing a second outcome type.
   */
  readonly groupUuid: string | null;
  /** Why nothing was even attempted, when that is the case. */
  readonly notAttempted: "empty_cart" | null;
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

/**
 * Commit one tab's record. Idempotent, and safe to call again while it is running.
 *
 * Not `async`, deliberately: the in-flight promise has to be handed back
 * synchronously, and an `async` wrapper would return a *different* promise to the
 * second caller, which is exactly the double-press this guards.
 */
export function checkoutCart(cart: ShoppingCart): Promise<CheckoutOutcome> {
  const running = inFlight.get(cart);
  if (running !== undefined) {
    return running;
  }
  const attempt = run(cart).finally(() => {
    inFlight.delete(cart);
  });
  inFlight.set(cart, attempt);
  return attempt;
}

function nothing(notAttempted: "empty_cart"): CheckoutOutcome {
  return {
    destination: null,
    applied: 0,
    replayed: 0,
    failed: [],
    groupUuid: null,
    notAttempted,
  };
}

async function run(cart: ShoppingCart): Promise<CheckoutOutcome> {
  const all = [...cart.lines()];
  if (all.length === 0) {
    return nothing("empty_cart");
  }
  const target = cart.target;

  /*
   * A row that nets to *putting stock back* cannot be applied by either endpoint:
   * a BOM line asks for a quantity, and an allocation is a hold, so neither has a
   * negative. It happens when a return is added to a record that never took the
   * part in the first place — the ordinary "took four, put one back" nets to three
   * and is a plain take. Refused locally, with the way out named, rather than
   * relayed as a validation error about a field the user never saw.
   */
  const lines = all.filter((line) => line.direction !== "return");
  const returns: CheckoutFailure[] = all
    .filter((line) => line.direction === "return")
    .map((line) => ({
      id: line.id,
      reason: "net_return",
      message:
        "This row nets to putting stock back, and a record can only take stock for " +
        "its target. Record it on the lot itself with no tab open, which writes it " +
        "to the ledger straight away.",
    }));
  const at = Date.now();
  for (const failure of returns) {
    cart.markFailed(failure.id, { reason: failure.reason, message: failure.message, at });
  }
  if (lines.length === 0) {
    return { destination: target.kind, applied: 0, replayed: 0, failed: returns, groupUuid: null, notAttempted: null };
  }

  const outcome =
    target.kind === "project"
      ? await toProjectBom(cart, lines, target.projectId)
      : await toBuild(cart, lines, target.buildId);
  return { ...outcome, failed: [...returns, ...outcome.failed] };
}

// ------------------------------------------------------- the two doors ----

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
 * A build's staged parts — a real ledger move, not a hold.
 *
 * ADR 0011. A cart line says *I picked this up*, and the only honest record of
 * that is the one `stock_lots.qty_milli_cached` can be read against: the parts
 * left the drawer, so the ledger has to say so. Reserving instead — which is what
 * this did until now — leaves the count in a bin nobody can find the parts in,
 * which is the failure ADR 0004 wrote the staging location to prevent. Reserving
 * is still the right verb for planning a hold you have not walked to the shelf
 * for; that lives on the build screen, and it is a different gesture.
 *
 * A hold is a hold *on a lot*, so a row with no lot cannot become one. Refused
 * here without a request rather than sent for the server to reject: the missing
 * piece is local, and "this row needs a container" is more useful than a
 * validation error about a field the user has never seen.
 *
 * **One request per line, deliberately.** There is no batch stage route, and the
 * per-line rules survive without one because each line carries the `clientOpId`
 * minted when it was added: a resend replays rather than doubling, a refusal
 * stops that line and not the loop, and a row is dropped only once the server has
 * said it applied.
 */
async function toBuild(
  cart: ShoppingCart,
  lines: readonly CartLine[],
  buildId: number,
): Promise<CheckoutOutcome> {
  const stageable: CartLine[] = [];
  const localFailures: CheckoutFailure[] = [];
  for (const line of lines) {
    if (line.lotId === null) {
      localFailures.push({
        id: line.id,
        reason: "no_lot",
        message:
          "Sending parts to a build needs the container they came out of, and this " +
          "row does not name one. Choose which package it comes from.",
      });
      continue;
    }
    stageable.push(line);
  }
  const at = Date.now();
  for (const failure of localFailures) {
    cart.markFailed(failure.id, { reason: failure.reason, message: failure.message, at });
  }
  if (stageable.length === 0) {
    return {
      destination: "build",
      applied: 0,
      replayed: 0,
      failed: localFailures,
      groupUuid: null,
      notAttempted: null,
    };
  }

  const bomLineFor = await bomLineResolver(buildId);
  const appliedIds: string[] = [];
  const failed: CheckoutFailure[] = [...localFailures];
  let replayed = 0;

  for (const line of stageable) {
    const lotId = line.lotId;
    if (lotId === null) {
      continue;
    }
    try {
      const response = await stageStock(buildId, {
        lot_id: lotId,
        qty_milli: line.qtyMilli,
        client_op_id: line.clientOpId,
        ...(bomLineFor(line.partId) === null ? {} : { bom_line_id: bomLineFor(line.partId) }),
      });
      appliedIds.push(line.id);
      if (response.replayed) {
        replayed += 1;
      }
    } catch (cause) {
      const message = describeFailure(cause);
      const reason = cause instanceof ApiError ? describeError(cause).reason : null;
      failed.push({ id: line.id, reason, message });
      cart.markFailed(line.id, { reason, message, at: Date.now() });
    }
  }

  // After the failures are marked, so one write does not undo the other.
  cart.removeMany(appliedIds);

  return {
    destination: "build",
    applied: appliedIds.length,
    replayed,
    failed,
    groupUuid: null,
    notAttempted: null,
  };
}

/**
 * Which BOM line a staged part counts against — **only when that is not a guess.**
 *
 * Attribution is what makes staged parts show up as progress against a line
 * instead of as stock that merely moved somewhere, so it is worth one extra read.
 * But two BOM lines can legitimately name the same part (`R1` and `R7` of one
 * resistor are two requirements), and picking either would credit work to a line
 * the user never chose. So: exactly one candidate attributes, anything else
 * attributes to nothing and stages as an off-BOM part, which ADR 0004 already
 * calls a first-class case — "the part nobody planned for" is what the roster is
 * for.
 *
 * A failure to read the BOM is not a failure to stage. The parts moved; losing
 * the line attribution is a smaller loss than refusing to record the movement.
 */
async function bomLineResolver(buildId: number): Promise<(partId: number) => number | null> {
  const byPart = new Map<number, number | null>();
  try {
    const build = await getBuild(buildId);
    const bom = await listBomLines(build.project_id);
    for (const line of bom.lines) {
      if (line.part_id === null) {
        continue;
      }
      // Second sighting poisons the entry rather than overwriting it: ambiguous
      // is a different answer from unknown, and both mean "do not attribute".
      byPart.set(line.part_id, byPart.has(line.part_id) ? null : line.id);
    }
  } catch {
    return () => null;
  }
  return (partId: number) => byPart.get(partId) ?? null;
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
