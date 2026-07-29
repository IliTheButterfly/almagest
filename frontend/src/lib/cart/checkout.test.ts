/**
 * Draining the cart.
 *
 * As with `intake/sync.test.ts`, the failure paths are the subject: what the cart
 * looks like after a partial checkout, that a line whose part has been deleted
 * refuses *itself* rather than the batch, and that pressing the button twice —
 * concurrently or one after the other — cannot apply anything twice.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ShoppingCart, type CartLineDraft, type CartStorage } from "./cart";
import { checkoutCart } from "./checkout";

function memory(): CartStorage {
  const map = new Map<string, string>();
  return {
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => void map.set(key, value),
    removeItem: (key) => void map.delete(key),
  };
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

/** The batch's per-line verdicts, in the shape all three endpoints share. */
function lineResults(body: Record<string, unknown>, key: "lines" | "edits"): unknown[] {
  const lines = body[key] as { client_line_id: string }[];
  return lines.map((line, index) => ({
    index,
    client_line_id: line.client_line_id,
    applied: true,
  }));
}

function movementsOk(request: Sent): Response {
  return json({
    applied_count: (request.body["lines"] as unknown[]).length,
    failed_count: 0,
    group_uuid: "group-1",
    results: lineResults(request.body, "lines"),
  });
}

beforeEach(() => {
  sent.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("checking a cart out to a container", () => {
  it("applies every line and empties the cart", async () => {
    stubApi(movementsOk);
    const cart = new ShoppingCart(memory());
    cart.add(draft({ lotId: 7 }));
    cart.add(draft({ lotId: 8 }));
    cart.setTarget({ kind: "container", locationId: 3, label: "A1-04" });

    const outcome = await checkoutCart(cart);

    expect(outcome.destination).toBe("container");
    expect(outcome.applied).toBe(2);
    expect(outcome.failed).toEqual([]);
    // The whole checkout shares one group, so undoing it is one call.
    expect(outcome.groupUuid).toBe("group-1");
    expect(cart.size).toBe(0);
  });

  it("sends the lot when the row named one, and the part when it did not", async () => {
    stubApi(movementsOk);
    const cart = new ShoppingCart(memory());
    cart.add(draft({ lotId: 7 }));
    cart.add(draft({ partId: 43, lotId: null }));
    cart.setTarget({ kind: "container", locationId: 3, label: "A1-04" });

    await checkoutCart(cart);

    const lines = sent[0]?.body["lines"] as Record<string, unknown>[];
    expect(sent[0]?.url).toContain("/api/stock/movements");
    expect(sent[0]?.body["location_id"]).toBe(3);
    expect(lines[0]).toMatchObject({ lot_id: 7, direction: "take" });
    expect(lines[0]).not.toHaveProperty("part_id");
    expect(lines[1]).toMatchObject({ part_id: 43, direction: "take" });
    expect(lines[1]).not.toHaveProperty("lot_id");
  });

  it("lets a line pin its own direction against the checkout default", async () => {
    stubApi(movementsOk);
    const cart = new ShoppingCart(memory());
    cart.add(draft({ lotId: 7 }));
    cart.add(draft({ lotId: 8, direction: "return" }));
    cart.setTarget({ kind: "container", locationId: 3, label: "A1-04" });

    await checkoutCart(cart, { defaultDirection: "take" });

    const lines = sent[0]?.body["lines"] as Record<string, unknown>[];
    expect(lines.map((line) => line["direction"])).toEqual(["take", "return"]);
  });

  it("keeps a refused line, with why, and drops the ones that applied", async () => {
    stubApi((request) => {
      const lines = request.body["lines"] as { client_line_id: string }[];
      return json({
        applied_count: 1,
        failed_count: 1,
        group_uuid: "group-1",
        results: [
          { index: 0, client_line_id: lines[0]?.client_line_id, applied: true },
          {
            index: 1,
            client_line_id: lines[1]?.client_line_id,
            applied: false,
            reason: "insufficient_stock",
            message: "lot 8 holds 3, not 10",
          },
        ],
      });
    });
    const cart = new ShoppingCart(memory());
    const good = cart.add(draft({ lotId: 7 }));
    const bad = cart.add(draft({ lotId: 8 }));
    cart.setTarget({ kind: "container", locationId: 3, label: "A1-04" });

    const outcome = await checkoutCart(cart);

    expect(outcome.applied).toBe(1);
    expect(outcome.failed).toEqual([
      { id: bad.id, reason: "insufficient_stock", message: "lot 8 holds 3, not 10" },
    ]);
    // The reason lives on the surviving row: after a partial checkout it is all
    // the user has to explain why the cart is not empty.
    expect(cart.lines().map((line) => line.id)).toEqual([bad.id]);
    expect(cart.lines()[0]?.failure?.reason).toBe("insufficient_stock");
    expect(cart.lines().some((line) => line.id === good.id)).toBe(false);
  });

  it("matches verdicts by client_line_id, not by position", async () => {
    stubApi((request) => {
      const lines = request.body["lines"] as { client_line_id: string }[];
      return json({
        applied_count: 1,
        failed_count: 1,
        group_uuid: "group-1",
        // Deliberately out of order, and with indices that disagree.
        results: [
          {
            index: 0,
            client_line_id: lines[1]?.client_line_id,
            applied: false,
            reason: "lot_moved",
            message: "lot 8 is no longer in A1-04",
          },
          { index: 1, client_line_id: lines[0]?.client_line_id, applied: true },
        ],
      });
    });
    const cart = new ShoppingCart(memory());
    cart.add(draft({ lotId: 7 }));
    const moved = cart.add(draft({ lotId: 8 }));
    cart.setTarget({ kind: "container", locationId: 3, label: "A1-04" });

    await checkoutCart(cart);

    expect(cart.lines().map((line) => line.id)).toEqual([moved.id]);
  });

  it("counts a replayed line as applied and drops it", async () => {
    // The honest retry after a partial failure: the lines that already went
    // through carry the same per-line key, so the server replays them.
    stubApi((request) => {
      const lines = request.body["lines"] as { client_line_id: string }[];
      return json({
        applied_count: 1,
        failed_count: 0,
        group_uuid: "group-2",
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
    const cart = new ShoppingCart(memory());
    cart.add(draft());
    cart.setTarget({ kind: "container", locationId: 3, label: "A1-04" });

    const outcome = await checkoutCart(cart);

    expect(outcome.applied).toBe(1);
    expect(outcome.replayed).toBe(1);
    expect(cart.size).toBe(0);
  });

  it("ignores a verdict about a line it never sent", async () => {
    stubApi((request) => {
      const lines = request.body["lines"] as { client_line_id: string }[];
      return json({
        applied_count: 1,
        failed_count: 0,
        group_uuid: "group-1",
        results: [
          { index: 0, client_line_id: lines[0]?.client_line_id, applied: true },
          { index: 9, client_line_id: "someone-elses-row", applied: false, reason: "nope" },
        ],
      });
    });
    const cart = new ShoppingCart(memory());
    cart.add(draft());
    cart.setTarget({ kind: "container", locationId: 3, label: "A1-04" });

    const outcome = await checkoutCart(cart);

    expect(outcome.applied).toBe(1);
    expect(outcome.failed).toEqual([]);
    expect(cart.size).toBe(0);
  });
});

describe("a cart line whose part has been deleted", () => {
  it("refuses itself and does not block the rest of the checkout", async () => {
    stubApi((request) => {
      const lines = request.body["lines"] as { client_line_id: string; part_id?: number }[];
      return json({
        applied_count: 1,
        failed_count: 1,
        group_uuid: "group-1",
        results: lines.map((line, index) => ({
          index,
          client_line_id: line.client_line_id,
          applied: line.part_id !== 999,
          reason: line.part_id === 999 ? "unknown_part" : null,
          message: line.part_id === 999 ? "there is no part with id 999" : null,
        })),
      });
    });
    const cart = new ShoppingCart(memory());
    const gone = cart.add(draft({ partId: 999, partName: "TL072CP", lotId: null }));
    const fine = cart.add(draft({ partId: 43, lotId: null }));
    cart.setTarget({ kind: "container", locationId: 3, label: "A1-04" });

    // Not a throw, and not zero applied: the good line went through.
    const outcome = await checkoutCart(cart);
    expect(outcome.applied).toBe(1);
    expect(outcome.failed.map((failure) => failure.reason)).toEqual(["unknown_part"]);
    expect(cart.lines().some((line) => line.id === fine.id)).toBe(false);

    // And the row that is left is still a named row a human can act on — the
    // captured name is all the UI needs, since the part it referred to is gone.
    const row = cart.lines()[0];
    expect(row?.id).toBe(gone.id);
    expect(row?.partName).toBe("TL072CP");
    expect(row?.failure?.message).toBe("there is no part with id 999");

    cart.remove(gone.id);
    expect(cart.size).toBe(0);
  });
});

describe("checking a cart out twice", () => {
  it("issues one request when the button is pressed twice before it answers", async () => {
    let release: (() => void) | undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    stubApi(async (request) => {
      await held;
      return movementsOk(request);
    });
    const cart = new ShoppingCart(memory());
    cart.add(draft());
    cart.setTarget({ kind: "container", locationId: 3, label: "A1-04" });

    const first = checkoutCart(cart);
    const second = checkoutCart(cart);
    release?.();
    const [one, two] = await Promise.all([first, second]);

    expect(sent).toHaveLength(1);
    expect(one).toBe(two);
    expect(cart.size).toBe(0);
  });

  it("does nothing the second time, because the cart is now empty", async () => {
    stubApi(movementsOk);
    const cart = new ShoppingCart(memory());
    cart.add(draft());
    cart.setTarget({ kind: "container", locationId: 3, label: "A1-04" });

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
        group_uuid: "group-1",
        results: [
          {
            index: 0,
            client_line_id: lines[0]?.client_line_id,
            applied: false,
            reason: "insufficient_stock",
            message: "not enough",
          },
        ],
      });
    });
    const cart = new ShoppingCart(memory());
    const line = cart.add(draft());
    cart.setTarget({ kind: "container", locationId: 3, label: "A1-04" });

    await checkoutCart(cart);
    await checkoutCart(cart);

    expect(sent).toHaveLength(2);
    const keys = sent.map(
      (request) => (request.body["lines"] as { client_op_id: string }[])[0]?.client_op_id,
    );
    expect(keys[0]).toBe(line.clientOpId);
    expect(keys[1]).toBe(line.clientOpId);
    // The batch's own key is fresh — the body is a different statement now.
    expect(sent[0]?.body["client_op_id"]).not.toBe(sent[1]?.body["client_op_id"]);
  });
});

describe("checking a cart out to a project's BOM", () => {
  it("adds one line per row, partially, with the match confirmed", async () => {
    stubApi((request) =>
      json({ lines: [], deleted_ids: [], results: lineResults(request.body, "edits") }),
    );
    const cart = new ShoppingCart(memory());
    cart.add(draft({ designator: "C7" }));
    cart.setTarget({ kind: "project", projectId: 12, label: "Bench PSU" });

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
    const cart = new ShoppingCart(memory());
    cart.add(draft());
    cart.setTarget({ kind: "project", projectId: 12, label: "Bench PSU" });

    const outcome = await checkoutCart(cart);

    expect(outcome.applied).toBe(1);
    expect(cart.size).toBe(0);
  });
});

describe("checking a cart out to a build", () => {
  it("reserves the rows that name a lot", async () => {
    stubApi((request) =>
      json({
        applied_count: 1,
        failed_count: 0,
        results: lineResults(request.body, "lines"),
      }),
    );
    const cart = new ShoppingCart(memory());
    cart.add(draft({ lotId: 7 }));
    cart.setTarget({ kind: "build", buildId: 5, label: "rev B ×3" });

    const outcome = await checkoutCart(cart);

    expect(sent[0]?.url).toContain("/api/builds/5/allocate-batch");
    expect((sent[0]?.body["lines"] as Record<string, unknown>[])[0]).toMatchObject({
      lot_id: 7,
      part_id: 42,
      qty_milli: 10_000,
    });
    expect(outcome.applied).toBe(1);
    expect(cart.size).toBe(0);
  });

  it("refuses a lotless row locally, without sending it", async () => {
    // A hold is a hold on a lot. Saying so here beats relaying a validation
    // error about a field the user has never seen.
    stubApi((request) =>
      json({
        applied_count: 1,
        failed_count: 0,
        results: lineResults(request.body, "lines"),
      }),
    );
    const cart = new ShoppingCart(memory());
    const lotless = cart.add(draft({ lotId: null }));
    cart.add(draft({ lotId: 7 }));
    cart.setTarget({ kind: "build", buildId: 5, label: "rev B ×3" });

    const outcome = await checkoutCart(cart);

    expect((sent[0]?.body["lines"] as unknown[]).length).toBe(1);
    expect(outcome.applied).toBe(1);
    expect(outcome.failed.map((failure) => failure.reason)).toEqual(["no_lot"]);
    expect(cart.lines().map((line) => line.id)).toEqual([lotless.id]);
    expect(cart.lines()[0]?.failure?.reason).toBe("no_lot");
  });

  it("sends nothing at all when no row names a lot", async () => {
    stubApi(() => json({ applied_count: 0, failed_count: 0, results: [] }));
    const cart = new ShoppingCart(memory());
    cart.add(draft({ lotId: null }));
    cart.setTarget({ kind: "build", buildId: 5, label: "rev B ×3" });

    const outcome = await checkoutCart(cart);

    expect(sent).toEqual([]);
    expect(outcome.failed).toHaveLength(1);
    expect(cart.size).toBe(1);
  });
});

describe("a checkout that never lands", () => {
  it("refuses to guess a destination when none was chosen", async () => {
    stubApi(movementsOk);
    const cart = new ShoppingCart(memory());
    cart.add(draft());

    const outcome = await checkoutCart(cart);

    expect(sent).toEqual([]);
    expect(outcome.notAttempted).toBe("no_target");
    expect(outcome.destination).toBeNull();
    expect(cart.size).toBe(1);
  });

  it("keeps every row and says it is a connection problem when offline", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
    );
    const cart = new ShoppingCart(memory());
    cart.add(draft({ lotId: 7 }));
    cart.add(draft({ lotId: 8 }));
    cart.setTarget({ kind: "container", locationId: 3, label: "A1-04" });

    const outcome = await checkoutCart(cart);

    expect(outcome.applied).toBe(0);
    expect(outcome.failed).toHaveLength(2);
    expect(outcome.failed[0]?.message).toContain("No connection");
    expect(cart.size).toBe(2);
    // On the rows, not just in the outcome — a banner is gone after one
    // navigation and the cart is not.
    expect(cart.lines().every((line) => line.failure !== null)).toBe(true);
  });

  it("distinguishes a refusal that will never work from one that might", async () => {
    stubApi(() => json({ detail: { reason: "unknown_project", message: "no such project" } }, 404));
    const cart = new ShoppingCart(memory());
    cart.add(draft());
    cart.setTarget({ kind: "project", projectId: 12, label: "gone" });

    const outcome = await checkoutCart(cart);

    expect(outcome.applied).toBe(0);
    expect(outcome.failed[0]?.message).toContain("cannot be checked out as it is");
    expect(cart.size).toBe(1);
  });

  it("says a server error is worth retrying", async () => {
    stubApi(() => json({ detail: { reason: null, message: "boom" } }, 503));
    const cart = new ShoppingCart(memory());
    cart.add(draft());
    cart.setTarget({ kind: "container", locationId: 3, label: "A1-04" });

    const outcome = await checkoutCart(cart);

    expect(outcome.failed[0]?.message).toContain("trying again may work");
    expect(cart.size).toBe(1);
  });
});
