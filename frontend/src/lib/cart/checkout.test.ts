/**
 * Committing a tab's record.
 *
 * As with `intake/sync.test.ts`, the failure paths are the subject: what a record
 * looks like after a partial commit, that a line whose part has been deleted refuses
 * *itself* rather than the batch, that pressing the button twice — concurrently or
 * one after the other — cannot apply anything twice, and that committing one tab
 * leaves another tab's lines exactly where they were.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkTarget } from "../projectcontext/target";
import { ShoppingCart, type CartLineDraft, type CartStorage } from "./cart";
import { checkoutCart } from "./checkout";

const PROJECT: WorkTarget = { kind: "project", projectId: 12, label: "Bench PSU" };
const BUILD: WorkTarget = { kind: "build", buildId: 5, label: "rev B ×3" };

function memory(): CartStorage {
  const map = new Map<string, string>();
  return {
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => void map.set(key, value),
    removeItem: (key) => void map.delete(key),
  };
}

function cartFor(target: WorkTarget): ShoppingCart {
  return new ShoppingCart(target, memory());
}

function draft(overrides: Partial<CartLineDraft> = {}): CartLineDraft {
  return {
    partId: 42,
    partName: "GRM188R61A106KA73D",
    qtyMilli: 10_000,
    mpn: "GRM188R61A106KA73D",
    lotId: 7,
    locationId: 3,
    locationLabel: "A1-04",
    ...overrides,
  };
}

interface Sent {
  readonly url: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

const sent: Sent[] = [];

/** `reply` decides each request's fate from the payload it carried. */
function stubApi(reply: (request: Sent) => Response | Promise<Response>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const text = await request.text();
      const entry: Sent = {
        url: request.url,
        method: request.method,
        body: text === "" ? {} : (JSON.parse(text) as Record<string, unknown>),
      };
      sent.push(entry);
      return reply(entry);
    }),
  );
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** The batch's per-line verdicts, in the shape both endpoints share. */
function lineResults(body: Record<string, unknown>, key: "lines" | "edits"): unknown[] {
  const lines = body[key] as { client_line_id: string }[];
  return lines.map((line, index) => ({
    index,
    client_line_id: line.client_line_id,
    applied: true,
  }));
}

function holdsOk(request: Sent): Response {
  return json({
    applied_count: (request.body["lines"] as unknown[]).length,
    failed_count: 0,
    results: lineResults(request.body, "lines"),
  });
}

beforeEach(() => {
  sent.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("committing a build's record", () => {
  it("reserves every line and empties the record", async () => {
    stubApi(holdsOk);
    const cart = cartFor(BUILD);
    cart.add(draft({ lotId: 7 }));
    cart.add(draft({ lotId: 8 }));

    const outcome = await checkoutCart(cart);

    expect(sent[0]?.url).toContain("/api/builds/5/allocate-batch");
    expect((sent[0]?.body["lines"] as Record<string, unknown>[])[0]).toMatchObject({
      lot_id: 7,
      part_id: 42,
      qty_milli: 10_000,
    });
    expect(outcome.destination).toBe("build");
    expect(outcome.applied).toBe(2);
    expect(outcome.failed).toEqual([]);
    expect(cart.size).toBe(0);
  });

  it("keeps a refused line, with why, and drops the ones that applied", async () => {
    stubApi((request) => {
      const lines = request.body["lines"] as { client_line_id: string }[];
      return json({
        applied_count: 1,
        failed_count: 1,
        results: [
          { index: 0, client_line_id: lines[0]?.client_line_id, applied: true },
          {
            index: 1,
            client_line_id: lines[1]?.client_line_id,
            applied: false,
            reason: "insufficient_available",
            message: "lot 8 holds 3, not 10",
          },
        ],
      });
    });
    const cart = cartFor(BUILD);
    const good = cart.add(draft({ lotId: 7 }));
    const bad = cart.add(draft({ lotId: 8 }));

    const outcome = await checkoutCart(cart);

    expect(outcome.applied).toBe(1);
    expect(outcome.failed).toEqual([
      { id: bad?.id, reason: "insufficient_available", message: "lot 8 holds 3, not 10" },
    ]);
    // The reason lives on the surviving row: after a partial commit it is all
    // the user has to explain why the record is not empty.
    expect(cart.lines().map((line) => line.id)).toEqual([bad?.id]);
    expect(cart.lines()[0]?.failure?.reason).toBe("insufficient_available");
    expect(cart.lines().some((line) => line.id === good?.id)).toBe(false);
  });

  it("matches verdicts by client_line_id, not by position", async () => {
    stubApi((request) => {
      const lines = request.body["lines"] as { client_line_id: string }[];
      return json({
        applied_count: 1,
        failed_count: 1,
        // Deliberately out of order, and with indices that disagree.
        results: [
          {
            index: 0,
            client_line_id: lines[1]?.client_line_id,
            applied: false,
            reason: "unknown_lot",
            message: "lot 8 is gone",
          },
          { index: 1, client_line_id: lines[0]?.client_line_id, applied: true },
        ],
      });
    });
    const cart = cartFor(BUILD);
    cart.add(draft({ lotId: 7 }));
    const moved = cart.add(draft({ lotId: 8 }));

    await checkoutCart(cart);

    expect(cart.lines().map((line) => line.id)).toEqual([moved?.id]);
  });

  it("counts a replayed line as applied and drops it", async () => {
    // The honest retry after a partial failure: the lines that already went
    // through carry the same per-line key, so the server replays them.
    stubApi((request) => {
      const lines = request.body["lines"] as { client_line_id: string }[];
      return json({
        applied_count: 1,
        failed_count: 0,
        results: [
          {
            index: 0,
            client_line_id: lines[0]?.client_line_id,
            applied: true,
            replayed: true,
          },
        ],
      });
    });
    const cart = cartFor(BUILD);
    cart.add(draft());

    const outcome = await checkoutCart(cart);

    expect(outcome.applied).toBe(1);
    expect(outcome.replayed).toBe(1);
    expect(cart.size).toBe(0);
  });

  it("ignores a verdict about a line it never sent", async () => {
    // The bogus verdict carries `index: 0` deliberately. With an out-of-range
    // index it would be discarded by the position lookup finding nothing, and the
    // id-first matching this is really about — the protection against a record
    // reordered between building the request and reading the answer — would go
    // unasserted. As written, only an unrecognised `client_line_id` keeps the
    // refusal off the row that actually applied.
    stubApi((request) => {
      const lines = request.body["lines"] as { client_line_id: string }[];
      return json({
        applied_count: 1,
        failed_count: 0,
        results: [
          { index: 0, client_line_id: lines[0]?.client_line_id, applied: true },
          { index: 0, client_line_id: "someone-elses-row", applied: false, reason: "nope" },
        ],
      });
    });
    const cart = cartFor(BUILD);
    cart.add(draft());

    const outcome = await checkoutCart(cart);

    expect(outcome.applied).toBe(1);
    expect(outcome.failed).toEqual([]);
    expect(cart.size).toBe(0);
  });

  it("refuses a lotless row locally, without sending it", async () => {
    // A hold is a hold on a lot. Saying so here beats relaying a validation
    // error about a field the user has never seen.
    stubApi(holdsOk);
    const cart = cartFor(BUILD);
    const lotless = cart.add(draft({ lotId: null }));
    cart.add(draft({ lotId: 7 }));

    const outcome = await checkoutCart(cart);

    expect((sent[0]?.body["lines"] as unknown[]).length).toBe(1);
    expect(outcome.applied).toBe(1);
    expect(outcome.failed.map((failure) => failure.reason)).toEqual(["no_lot"]);
    expect(cart.lines().map((line) => line.id)).toEqual([lotless?.id]);
    expect(cart.lines()[0]?.failure?.reason).toBe("no_lot");
  });

  it("sends nothing at all when no row names a lot", async () => {
    stubApi(() => json({ applied_count: 0, failed_count: 0, results: [] }));
    const cart = cartFor(BUILD);
    cart.add(draft({ lotId: null }));

    const outcome = await checkoutCart(cart);

    expect(sent).toEqual([]);
    expect(outcome.failed).toHaveLength(1);
    expect(cart.size).toBe(1);
  });
});

describe("a line that nets to putting stock back", () => {
  it("is refused locally, with the way out named, and does not block the rest", async () => {
    // Neither endpoint has a negative: a BOM line asks for a quantity and an
    // allocation is a hold. The ordinary "took four, put one back" nets to a take
    // and never reaches this; a return of something this record never took does.
    stubApi(holdsOk);
    const cart = cartFor(BUILD);
    const back = cart.add(draft({ lotId: 8, qtyMilli: 2_000, direction: "return" }));
    cart.add(draft({ lotId: 7 }));

    const outcome = await checkoutCart(cart);

    expect((sent[0]?.body["lines"] as unknown[]).length).toBe(1);
    expect(outcome.applied).toBe(1);
    expect(outcome.failed.map((failure) => failure.reason)).toEqual(["net_return"]);
    expect(cart.lines().map((line) => line.id)).toEqual([back?.id]);
    expect(cart.lines()[0]?.failure?.message).toContain("with no tab open");
  });
});

describe("a line whose part has been deleted", () => {
  it("refuses itself and does not block the rest of the commit", async () => {
    stubApi((request) => {
      const lines = request.body["lines"] as { client_line_id: string; part_id?: number }[];
      return json({
        applied_count: 1,
        failed_count: 1,
        results: lines.map((line, index) => ({
          index,
          client_line_id: line.client_line_id,
          applied: line.part_id !== 999,
          reason: line.part_id === 999 ? "unknown_part" : null,
          message: line.part_id === 999 ? "there is no part with id 999" : null,
        })),
      });
    });
    const cart = cartFor(BUILD);
    const gone = cart.add(draft({ partId: 999, partName: "TL072CP", lotId: 11 }));
    const fine = cart.add(draft({ partId: 43, lotId: 12 }));

    // Not a throw, and not zero applied: the good line went through.
    const outcome = await checkoutCart(cart);
    expect(outcome.applied).toBe(1);
    expect(outcome.failed.map((failure) => failure.reason)).toEqual(["unknown_part"]);
    expect(cart.lines().some((line) => line.id === fine?.id)).toBe(false);

    // And the row that is left is still a named row a human can act on — the
    // captured name is all the UI needs, since the part it referred to is gone.
    const row = cart.lines()[0];
    expect(row?.id).toBe(gone?.id);
    expect(row?.partName).toBe("TL072CP");
    expect(row?.failure?.message).toBe("there is no part with id 999");

    cart.remove(gone?.id ?? "");
    expect(cart.size).toBe(0);
  });
});

describe("committing twice", () => {
  it("issues one request when the button is pressed twice before it answers", async () => {
    let release: (() => void) | undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    stubApi(async (request) => {
      await held;
      return holdsOk(request);
    });
    const cart = cartFor(BUILD);
    cart.add(draft());

    const first = checkoutCart(cart);
    const second = checkoutCart(cart);
    release?.();
    const [one, two] = await Promise.all([first, second]);

    expect(sent).toHaveLength(1);
    expect(one).toBe(two);
    expect(cart.size).toBe(0);
  });

  it("does nothing the second time, because the record is now empty", async () => {
    stubApi(holdsOk);
    const cart = cartFor(BUILD);
    cart.add(draft());

    await checkoutCart(cart);
    const again = await checkoutCart(cart);

    expect(sent).toHaveLength(1);
    expect(again.notAttempted).toBe("empty_cart");
    expect(again.applied).toBe(0);
  });

  it("reuses each line's key on a retry, so the server can replay it", async () => {
    stubApi((request) => {
      const lines = request.body["lines"] as { client_line_id: string }[];
      return json({
        applied_count: 0,
        failed_count: 1,
        results: [
          {
            index: 0,
            client_line_id: lines[0]?.client_line_id,
            applied: false,
            reason: "insufficient_available",
            message: "not enough",
          },
        ],
      });
    });
    const cart = cartFor(BUILD);
    const line = cart.add(draft());

    await checkoutCart(cart);
    await checkoutCart(cart);

    expect(sent).toHaveLength(2);
    const keys = sent.map(
      (request) => (request.body["lines"] as { client_op_id: string }[])[0]?.client_op_id,
    );
    expect(keys[0]).toBe(line?.clientOpId);
    expect(keys[1]).toBe(line?.clientOpId);
    // The batch's own key is fresh — the body is a different statement now.
    expect(sent[0]?.body["client_op_id"]).not.toBe(sent[1]?.body["client_op_id"]);
  });
});

describe("committing one tab", () => {
  it("leaves every other tab's lines exactly where they were", async () => {
    // The whole point of per-target records: two jobs, one walk to the shelf, and
    // finishing one of them must not touch the other.
    stubApi((request) =>
      request.url.includes("/bom")
        ? json({ lines: [], deleted_ids: [], results: lineResults(request.body, "edits") })
        : holdsOk(request),
    );
    const build = cartFor(BUILD);
    const project = cartFor(PROJECT);
    build.add(draft({ lotId: 7 }));
    const untouched = project.add(draft({ lotId: 8 }));

    await checkoutCart(build);

    expect(sent).toHaveLength(1);
    expect(sent[0]?.url).toContain("/api/builds/5/allocate-batch");
    expect(build.size).toBe(0);
    expect(project.lines().map((line) => line.id)).toEqual([untouched?.id]);
    expect(project.lines()[0]?.failure).toBeNull();
  });
});

describe("committing a project's record", () => {
  it("adds one line per row, partially, with the match confirmed", async () => {
    stubApi((request) =>
      json({ lines: [], deleted_ids: [], results: lineResults(request.body, "edits") }),
    );
    const cart = cartFor(PROJECT);
    cart.add(draft({ designator: "C7" }));

    const outcome = await checkoutCart(cart);

    expect(sent[0]?.method).toBe("PUT");
    expect(sent[0]?.url).toContain("/api/projects/12/bom");
    // Partial, or nineteen good rows would be discarded to protect the twentieth.
    expect(sent[0]?.body["partial"]).toBe(true);
    const edits = sent[0]?.body["edits"] as Record<string, unknown>[];
    expect(edits[0]).toMatchObject({
      part_id: 42,
      qty_per_assembly_milli: 10_000,
      designators: "C7",
      is_match_confirmed: true,
    });
    // A new line, not an edit of an existing one.
    expect(edits[0]).not.toHaveProperty("id");
    expect(outcome.destination).toBe("project");
    expect(outcome.groupUuid).toBeNull();
    expect(cart.size).toBe(0);
  });

  it("treats a 2xx with no per-line results as everything applied", async () => {
    // The route's non-partial contract: it either applied whole or 4xx'd.
    stubApi(() => json({ lines: [], deleted_ids: [] }));
    const cart = cartFor(PROJECT);
    cart.add(draft());

    const outcome = await checkoutCart(cart);

    expect(outcome.applied).toBe(1);
    expect(cart.size).toBe(0);
  });
});

describe("a commit that never lands", () => {
  it("keeps every row and says it is a connection problem when offline", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
    );
    const cart = cartFor(BUILD);
    cart.add(draft({ lotId: 7 }));
    cart.add(draft({ lotId: 8 }));

    const outcome = await checkoutCart(cart);

    expect(outcome.applied).toBe(0);
    expect(outcome.failed).toHaveLength(2);
    expect(outcome.failed[0]?.message).toContain("No connection");
    expect(cart.size).toBe(2);
    // On the rows, not just in the outcome — a banner is gone after one
    // navigation and the record is not.
    expect(cart.lines().every((line) => line.failure !== null)).toBe(true);
  });

  it("distinguishes a refusal that will never work from one that might", async () => {
    stubApi(() => json({ detail: { reason: "unknown_project", message: "no such project" } }, 404));
    const cart = cartFor(PROJECT);
    cart.add(draft());

    const outcome = await checkoutCart(cart);

    expect(outcome.applied).toBe(0);
    expect(outcome.failed[0]?.message).toContain("cannot be checked out as it is");
    expect(cart.size).toBe(1);
  });

  it("says a server error is worth retrying", async () => {
    stubApi(() => json({ detail: { reason: null, message: "boom" } }, 503));
    const cart = cartFor(BUILD);
    cart.add(draft());

    const outcome = await checkoutCart(cart);

    expect(outcome.failed[0]?.message).toContain("trying again may work");
    expect(cart.size).toBe(1);
  });
});
