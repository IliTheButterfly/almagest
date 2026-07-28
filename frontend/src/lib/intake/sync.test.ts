/**
 * Draining the local queue to the server.
 *
 * The failure mode of this module is **losing intake data**, so the tests are
 * about what happens when the push does not work, not when it does: an entry is
 * dropped locally only after the server confirms it, one bad entry does not take
 * the rest with it, and a permanently-stuck entry is described differently from a
 * temporarily-unreachable one.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { IntakeQueue, type PendingScan, type QueueStorage } from "./queue";
import { syncIntakeQueue } from "./sync";

/** In-memory storage, so no test touches real `localStorage`. */
function memory(): QueueStorage {
  const map = new Map<string, string>();
  return {
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => void map.set(key, value),
    removeItem: (key) => void map.delete(key),
  };
}

function scan(id: string, overrides: Partial<PendingScan> = {}): PendingScan {
  return {
    id,
    code: `payload-${id}`,
    symbology: "DataMatrix",
    queuedAt: 1_700_000_000_000,
    decodedKind: "ecia",
    mpn: "LM358N",
    manufacturer: null,
    supplierPartNumber: null,
    quantityMilli: 25_000,
    dateCode: null,
    lotCode: null,
    partId: null,
    note: null,
    ...overrides,
  };
}

const posted: Record<string, unknown>[] = [];

/** `reply` decides each request's fate, keyed on the payload it carried. */
function stubApi(reply: (body: Record<string, unknown>) => Response | Promise<Response>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const body = JSON.parse(await request.text()) as Record<string, unknown>;
      posted.push(body);
      return reply(body);
    }),
  );
}

function ok(alreadyQueued = false): Response {
  return new Response(
    JSON.stringify({
      entry: { id: 1, status: "pending" },
      already_queued: alreadyQueued,
    }),
    { status: 201, headers: { "content-type": "application/json" } },
  );
}

beforeEach(() => {
  posted.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("syncing the intake queue", () => {
  it("pushes every parked scan and empties the local queue", async () => {
    const queue = new IntakeQueue(memory());
    queue.add(scan("a"));
    queue.add(scan("b"));
    stubApi(() => ok());

    const outcome = await syncIntakeQueue(queue);

    expect(outcome.uploaded).toBe(2);
    expect(outcome.failed).toEqual([]);
    expect(queue.size).toBe(0);
  });

  it("sends the client_op_id minted at scan time as the identity", async () => {
    const queue = new IntakeQueue(memory());
    queue.add(scan("0189d1c0-dead-4000-8000-000000000001"));
    stubApi(() => ok());

    await syncIntakeQueue(queue);

    // Not a fresh id: the whole idempotency story rests on this being the one
    // the device minted before the row existed.
    expect(posted[0]?.["client_op_id"]).toBe("0189d1c0-dead-4000-8000-000000000001");
  });

  it("pushes oldest first, so server ids match scan order", async () => {
    const queue = new IntakeQueue(memory());
    queue.add(scan("first"));
    queue.add(scan("second"));
    queue.add(scan("third"));
    stubApi(() => ok());

    await syncIntakeQueue(queue);

    // The worklist is ordered by server `id` precisely so a wrong device clock
    // cannot scramble it — which only holds if the upload preserves the order.
    expect(posted.map((body) => body["client_op_id"])).toEqual(["first", "second", "third"]);
  });

  it("counts an entry the server already had, and still drops it locally", async () => {
    const queue = new IntakeQueue(memory());
    queue.add(scan("a"));
    stubApi(() => ok(true));

    const outcome = await syncIntakeQueue(queue);

    expect(outcome).toMatchObject({ uploaded: 0, alreadyThere: 1 });
    expect(queue.size).toBe(0);
  });

  it("keeps an entry the server never confirmed", async () => {
    const queue = new IntakeQueue(memory());
    queue.add(scan("a"));
    stubApi(() => new Response("", { status: 503 }));

    const outcome = await syncIntakeQueue(queue);

    // **The property this module exists for.** Dropping before confirmation
    // would lose the scan, which is the one outcome that must not happen.
    expect(outcome.uploaded).toBe(0);
    expect(outcome.failed).toHaveLength(1);
    expect(queue.size).toBe(1);
  });

  it("does not let one bad entry block the rest", async () => {
    const queue = new IntakeQueue(memory());
    queue.add(scan("bad"));
    queue.add(scan("good-1"));
    queue.add(scan("good-2"));
    stubApi((body) =>
      body["client_op_id"] === "bad"
        ? new Response(JSON.stringify({ detail: { reason: "unknown_part" } }), {
            status: 422,
            headers: { "content-type": "application/json" },
          })
        : ok(),
    );

    const outcome = await syncIntakeQueue(queue);

    // A malformed payload from an old app version must not wedge a queue of
    // thirty good scans behind it.
    expect(outcome.uploaded).toBe(2);
    expect(outcome.failed.map((failure) => failure.id)).toEqual(["bad"]);
    expect(queue.list().map((entry) => entry.id)).toEqual(["bad"]);
  });

  it("distinguishes a permanently stuck entry from a temporary blip", async () => {
    const stuck = new IntakeQueue(memory());
    stuck.add(scan("a"));
    stubApi(() => new Response(JSON.stringify({}), { status: 422 }));
    const permanent = await syncIntakeQueue(stuck);

    vi.unstubAllGlobals();
    const blip = new IntakeQueue(memory());
    blip.add(scan("b"));
    stubApi(() => new Response("", { status: 503 }));
    const temporary = await syncIntakeQueue(blip);

    // Reporting these identically leaves a permanently stuck entry looking like
    // a passing blip forever.
    expect(permanent.failed[0]?.message).toMatch(/needs looking at/);
    expect(temporary.failed[0]?.message).toMatch(/trying again later/);
  });

  it("says it is offline rather than blaming the server", async () => {
    const queue = new IntakeQueue(memory());
    queue.add(scan("a"));
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
    );

    const outcome = await syncIntakeQueue(queue);

    expect(outcome.failed[0]?.message).toMatch(/No connection/);
    expect(queue.size).toBe(1);
  });

  it("drops an unrecognised decoded_kind rather than failing the upload", async () => {
    const queue = new IntakeQueue(memory());
    // What a future app version, or a corrupted entry, could leave behind.
    queue.add(scan("a", { decodedKind: "some_future_handler" }));
    stubApi(() => ok());

    const outcome = await syncIntakeQueue(queue);

    // The hint is display-only and the raw payload goes up regardless, so losing
    // it costs nothing — where sending it would have 422'd the whole entry.
    expect(posted[0]?.["decoded_kind"]).toBeNull();
    expect(posted[0]?.["raw_payload"]).toBe("payload-a");
    expect(outcome.uploaded).toBe(1);
  });

  it("keeps a recognised decoded_kind", async () => {
    const queue = new IntakeQueue(memory());
    queue.add(scan("a", { decodedKind: "ecia" }));
    stubApi(() => ok());

    await syncIntakeQueue(queue);

    expect(posted[0]?.["decoded_kind"]).toBe("ecia");
  });

  it("sends the device's scan time, not the sync time", async () => {
    const queue = new IntakeQueue(memory());
    queue.add(scan("a", { queuedAt: Date.parse("2026-07-28T09:15:00Z") }));
    stubApi(() => ok());

    await syncIntakeQueue(queue);

    // Stored server-side for display: an offline batch syncs minutes later, and
    // "when I scanned it" is what the user remembers.
    expect(posted[0]?.["queued_at"]).toBe("2026-07-28T09:15:00.000Z");
  });

  it("does nothing, successfully, on an empty queue", async () => {
    const queue = new IntakeQueue(memory());
    stubApi(() => ok());

    expect(await syncIntakeQueue(queue)).toEqual({
      uploaded: 0,
      alreadyThere: 0,
      failed: [],
    });
    expect(posted).toEqual([]);
  });

  it("carries an ApiError's own message through", async () => {
    const queue = new IntakeQueue(memory());
    queue.add(scan("a"));
    stubApi(
      () =>
        new Response(JSON.stringify({ detail: { reason: "unknown_part", message: "no part 9" } }), {
          status: 422,
          headers: { "content-type": "application/json" },
        }),
    );

    const outcome = await syncIntakeQueue(queue);
    expect(outcome.failed[0]?.message).toContain("no part 9");
  });
});

describe("ApiError shape assumptions", () => {
  it("has a nullable status, which is the offline case", () => {
    // Pinned because `describeFailure` branches on it: treating a null status as
    // a server refusal would tell the user the server rejected a request it
    // never received.
    expect(new ApiError("offline", null, null).status).toBeNull();
  });
});
