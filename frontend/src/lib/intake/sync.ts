/**
 * Draining the local intake queue to the server.
 *
 * The local queue is now a **write-behind buffer, not the record.** A scan parked
 * at the shelf lands in `localStorage` first — that has to keep working with no
 * network, because the whole value of the fast path is that it never stops to
 * talk to anything — and is then pushed to `POST /api/intake/pending`, after
 * which the queue is walked at a desk from the server's copy.
 *
 * Two properties do all the work here:
 *
 * - **The push is idempotent on `client_op_id`**, which was minted at scan time
 *   before either copy existed. So a lost response, a double-tapped sync, or two
 *   devices that somehow hold the same entry all converge on one row. This is why
 *   the local entry is only dropped *after* a confirmed push, and why dropping it
 *   twice would be harmless anyway.
 * - **A failed entry does not block the rest.** One malformed payload from an old
 *   app version must not wedge a queue of thirty good scans behind it, so each is
 *   pushed independently and failures are reported rather than thrown.
 *
 * What this deliberately does *not* do is retry on a timer or in a service
 * worker. Sync runs when the queue screen is opened or the user asks, because a
 * background retry loop that silently fails is indistinguishable from one that
 * silently works — and the failure mode of this feature is losing intake data,
 * which the user must be told about rather than have hidden by a spinner.
 */

import { ApiError, parkScan, type PendingIntakeIn } from "../api/client";
import { describeError } from "../api/errors";
import type { PendingScan } from "./queue";
import { intakeQueue, type IntakeQueue } from "./queue";

export interface SyncOutcome {
  /** Pushed and confirmed by the server, then dropped from local storage. */
  readonly uploaded: number;
  /** Already on the server under the same `client_op_id`. Also dropped. */
  readonly alreadyThere: number;
  /** Kept locally, with why. A 4xx will never succeed; a 5xx or offline might. */
  readonly failed: readonly { readonly id: string; readonly message: string }[];
}

/**
 * The resolver kinds the server accepts.
 *
 * Listed rather than cast. `decodedKind` reaches us from `localStorage`, written
 * by whatever version of this app was installed when the scan happened, so it is
 * a `string` and not provably one of these — and asserting otherwise would send a
 * value the server rejects, failing an *entire* upload over a display-only field.
 */
const DECODED_KINDS = [
  "short_id",
  "alias",
  "ecia",
  "lcsc",
  "mpn",
  "ean",
  "unknown",
] as const;

type DecodedKind = (typeof DECODED_KINDS)[number];

function knownKind(value: string | null): DecodedKind | null {
  return value !== null && (DECODED_KINDS as readonly string[]).includes(value)
    ? (value as DecodedKind)
    : null;
}

/** `PendingScan` as the endpoint wants it. Field-for-field, no interpretation. */
function toRequest(entry: PendingScan): PendingIntakeIn {
  return {
    client_op_id: entry.id,
    raw_payload: entry.code,
    symbology: entry.symbology,
    // The resolver's verdict at scan time, carried as a hint — the desk pass
    // re-resolves rather than trusting it, since an alias taught since may now do
    // better. Dropped when unrecognised: losing a hint costs nothing, and the raw
    // payload it was derived from is going up regardless.
    decoded_kind: knownKind(entry.decodedKind),
    mpn: entry.mpn,
    manufacturer: entry.manufacturer,
    supplier_part_number: entry.supplierPartNumber,
    date_code: entry.dateCode,
    lot_code: entry.lotCode,
    quantity_milli: entry.quantityMilli,
    part_id: entry.partId,
    note: entry.note,
    queued_at: new Date(entry.queuedAt).toISOString(),
  };
}

/**
 * Push every locally-parked scan, dropping each one the server confirms.
 *
 * Entries are pushed **oldest first** so the server's `id` order matches scan
 * order — the worklist is ordered by `id` precisely so a wrong device clock
 * cannot scramble it, which only holds if the upload preserves the order.
 * Sequential rather than concurrent for the same reason.
 */
export async function syncIntakeQueue(queue: IntakeQueue = intakeQueue): Promise<SyncOutcome> {
  let uploaded = 0;
  let alreadyThere = 0;
  const failed: { id: string; message: string }[] = [];

  for (const entry of [...queue.list()]) {
    try {
      const result = await parkScan(toRequest(entry));
      if (result.already_queued) {
        alreadyThere += 1;
      } else {
        uploaded += 1;
      }
      // Only after the server confirmed. Dropping first would lose the scan on
      // a failed request, which is the one outcome this feature exists to avoid.
      queue.remove(entry.id);
    } catch (cause) {
      failed.push({ id: entry.id, message: describeFailure(cause) });
    }
  }

  return { uploaded, alreadyThere, failed };
}

/**
 * Why one entry did not go up, in terms of whether retrying can help.
 *
 * The distinction matters more than the wording: a 422 means this entry will
 * *never* upload and needs a human, while a 503 or a dead network means try
 * again later. Reporting them identically would leave a permanently stuck entry
 * looking like a temporary blip forever.
 */
function describeFailure(cause: unknown): string {
  if (cause instanceof ApiError) {
    // Through `describeError`, not `cause.message`. The latter is the generic
    // string `parkScan` passed to `fail()` — "could not park that scan" — while
    // the server's actual `{reason, message}` is in `detail`. Reporting the
    // generic one would tell the user an entry is permanently stuck without ever
    // saying why, which is the least actionable possible message.
    const { headline } = describeError(cause);

    // `status` is null when the request never got a response, which is the
    // offline case rather than a refusal — so it must not fall into the "the
    // server could not take it" branch and imply one happened.
    if (cause.status !== null && cause.status >= 400 && cause.status < 500) {
      return `${headline} This entry cannot be uploaded as it is, and needs looking at.`;
    }
    if (cause.status !== null) {
      return `${headline} The server could not take it; trying again later may work.`;
    }
  }
  return "No connection to the server. The scan is still here; sync again when there is one.";
}
