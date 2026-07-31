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

/**
 * The build read `toBuild` makes on its way to attributing a staged part.
 *
 * Only `project_id` is load-bearing here — it is how the BOM is found — but the
 * rest is kept plausible so a test reading this is not left wondering whether a
 * missing field is the point.
 */
const BUILD_READ = {
  id: 5,
  project_id: 12,
  build_no: 2,
  label: "rev B",
  assembly_count: 3,
  bom_revision: "B",
  status: "planned",
  staging_location_id: null,
  started_at: null,
  completed_at: null,
  notes: null,
  created_at: "2026-07-30T00:00:00Z",
  updated_at: "2026-07-30T00:00:00Z",
};

/** Reset per test: what the project's BOM says, which decides attribution. */
let bomLines: { id: number; part_id: number | null }[] = [];

function stageResponse(extra: Record<string, unknown> = {}): Response {
  return json({
    replayed: false,
    staging_location_id: 77,
    seqs: [1],
    allocation: null,
    source_lot: null,
    staging_lot: null,
    ...extra,
  });
}

/**
 * Everything the build path touches, all succeeding.
 *
 * `/stage` is matched **before** the build read, because the staging URL contains
 * the build's own URL and matching the other way round would answer every
 * withdrawal with a build.
 */
function stageOk(request: Sent): Response {
  if (request.url.includes("/stage")) {
    return stageResponse();
  }
  if (request.url.includes("/bom")) {
    return json({ lines: bomLines, total: bomLines.length });
  }
  return json(BUILD_READ);
}

/** How many parts actually left a drawer. */
function staged(): Sent[] {
  return sent.filter((request) => request.url.includes("/stage"));
}

beforeEach(() => {
  sent.length = 0;
  bomLines = [];
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("committing a build's record", () => {
  it("moves every line out of its drawer and empties the record", async () => {
    // ADR 0011: the line said "I picked this up", so the commit is a ledger move,
    // not a hold. One request per line, because there is no batch stage route and
    // the per-line key makes one unnecessary.
    stubApi(stageOk);
    const cart = cartFor(BUILD);
    const first = cart.add(draft({ lotId: 7 }));
    const second = cart.add(draft({ lotId: 8 }));

    const outcome = await checkoutCart(cart);

    expect(staged()).toHaveLength(2);
    expect(staged()[0]?.url).toContain("/api/builds/5/stage");
    expect(staged()[0]?.body).toMatchObject({
      lot_id: 7,
      qty_milli: 10_000,
      client_op_id: first?.clientOpId,
    });
    expect(staged()[1]?.body).toMatchObject({ lot_id: 8, client_op_id: second?.clientOpId });
    expect(outcome.destination).toBe("build");
    expect(outcome.applied).toBe(2);
    expect(outcome.failed).toEqual([]);
    expect(cart.size).toBe(0);
  });

  it("counts it against the BOM line when exactly one names that part", async () => {
    bomLines = [
      { id: 31, part_id: 42 },
      { id: 32, part_id: 43 },
    ];
    stubApi(stageOk);
    const cart = cartFor(BUILD);
    cart.add(draft({ partId: 42 }));

    await checkoutCart(cart);

    expect(staged()[0]?.body["bom_line_id"]).toBe(31);
  });

  it("counts it against no line when two lines name the same part", async () => {
    // R1 and R7 of one resistor are two requirements that happen to share a part.
    // Crediting either would put work against a line the user never chose, so it
    // stages as an off-BOM part instead — which ADR 0004 already has a place for.
    bomLines = [
      { id: 31, part_id: 42 },
      { id: 33, part_id: 42 },
    ];
    stubApi(stageOk);
    const cart = cartFor(BUILD);
    cart.add(draft({ partId: 42 }));

    await checkoutCart(cart);

    expect(staged()[0]?.body["bom_line_id"]).toBeUndefined();
  });

  it("still moves the parts when the BOM cannot be read", async () => {
    // The parts physically moved. Losing the line attribution is a smaller loss
    // than refusing to record that.
    stubApi((request) =>
      request.url.includes("/stage")
        ? stageResponse()
        : json({ detail: { reason: "boom", message: "no" } }, 500),
    );
    const cart = cartFor(BUILD);
    cart.add(draft());

    const outcome = await checkoutCart(cart);

    expect(outcome.applied).toBe(1);
    expect(staged()[0]?.body["bom_line_id"]).toBeUndefined();
  });

  it("keeps a refused line, with why, and drops the ones that applied", async () => {
    stubApi((request) => {
      if (!request.url.includes("/stage")) {
        return stageOk(request);
      }
      return request.body["lot_id"] === 8
        ? json(
            { detail: { reason: "lot_not_active", message: "lot 8 is quarantined" } },
            409,
          )
        : stageResponse();
    });
    const cart = cartFor(BUILD);
    const good = cart.add(draft({ lotId: 7 }));
    const bad = cart.add(draft({ lotId: 8 }));

    const outcome = await checkoutCart(cart);

    expect(outcome.applied).toBe(1);
    expect(outcome.failed.map((failure) => failure.reason)).toEqual(["lot_not_active"]);
    // A refusal stops that line, not the loop: the second line was still sent.
    expect(staged()).toHaveLength(2);
    // The reason lives on the surviving row: after a partial commit it is all
    // the user has to explain why the record is not empty.
    expect(cart.lines().map((line) => line.id)).toEqual([bad?.id]);
    expect(cart.lines()[0]?.failure?.reason).toBe("lot_not_active");
    expect(cart.lines().some((line) => line.id === good?.id)).toBe(false);
  });

  it("counts a replayed line as applied and drops it", async () => {
    // The honest retry after a partial failure: the line carries the same key it
    // was minted with, so the server replays rather than moving the parts twice.
    stubApi((request) =>
      request.url.includes("/stage") ? stageResponse({ replayed: true }) : stageOk(request),
    );
    const cart = cartFor(BUILD);
    cart.add(draft());

    const outcome = await checkoutCart(cart);

    expect(outcome.applied).toBe(1);
    expect(outcome.replayed).toBe(1);
    expect(cart.size).toBe(0);
  });

  it("refuses a lotless row locally, without sending it", async () => {
    // Parts come out of a container. Saying so here beats relaying a validation
    // error about a field the user has never seen.
    stubApi(stageOk);
    const cart = cartFor(BUILD);
    const lotless = cart.add(draft({ lotId: null }));
    cart.add(draft({ lotId: 7 }));

    const outcome = await checkoutCart(cart);

    expect(staged()).toHaveLength(1);
    expect(outcome.applied).toBe(1);
    expect(outcome.failed.map((failure) => failure.reason)).toEqual(["no_lot"]);
    expect(cart.lines().map((line) => line.id)).toEqual([lotless?.id]);
    expect(cart.lines()[0]?.failure?.reason).toBe("no_lot");
  });

  it("sends nothing at all when no row names a lot", async () => {
    stubApi(stageOk);
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
    // Neither destination has a negative: a BOM line asks for a quantity, and a
    // withdrawal to a build is stock leaving a drawer, not returning to one. The ordinary "took four, put one back" nets to a take
    // and never reaches this; a return of something this record never took does.
    stubApi(stageOk);
    const cart = cartFor(BUILD);
    const back = cart.add(draft({ lotId: 8, qtyMilli: 2_000, direction: "return" }));
    cart.add(draft({ lotId: 7 }));

    const outcome = await checkoutCart(cart);

    expect(staged()).toHaveLength(1);
    expect(outcome.applied).toBe(1);
    expect(outcome.failed.map((failure) => failure.reason)).toEqual(["net_return"]);
    expect(cart.lines().map((line) => line.id)).toEqual([back?.id]);
    expect(cart.lines()[0]?.failure?.message).toContain("with no tab open");
  });
});

describe("a line whose part has been deleted", () => {
  it("refuses itself and does not block the rest of the commit", async () => {
    stubApi((request) => {
      if (!request.url.includes("/stage")) {
        return stageOk(request);
      }
      return request.body["lot_id"] === 11
        ? json(
            { detail: { reason: "unknown_part", message: "there is no part with id 999" } },
            404,
          )
        : stageResponse();
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
    expect(row?.failure?.message).toContain("there is no part with id 999");

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
      return stageOk(request);
    });
    const cart = cartFor(BUILD);
    cart.add(draft());

    const first = checkoutCart(cart);
    const second = checkoutCart(cart);
    release?.();
    const [one, two] = await Promise.all([first, second]);

    expect(staged()).toHaveLength(1);
    expect(one).toBe(two);
    expect(cart.size).toBe(0);
  });

  it("does nothing the second time, because the record is now empty", async () => {
    stubApi(stageOk);
    const cart = cartFor(BUILD);
    cart.add(draft());

    await checkoutCart(cart);
    const again = await checkoutCart(cart);

    expect(staged()).toHaveLength(1);
    expect(again.notAttempted).toBe("empty_cart");
    expect(again.applied).toBe(0);
  });

  it("reuses each line's key on a retry, so the server can replay it", async () => {
    stubApi((request) =>
      request.url.includes("/stage")
        ? json({ detail: { reason: "same_location", message: "already there" } }, 409)
        : stageOk(request),
    );
    const cart = cartFor(BUILD);
    const line = cart.add(draft());

    await checkoutCart(cart);
    await checkoutCart(cart);

    expect(staged()).toHaveLength(2);
    const keys = staged().map((request) => request.body["client_op_id"]);
    expect(keys[0]).toBe(line?.clientOpId);
    // Identical on the retry: that is what lets the server recognise the second
    // send as the same withdrawal rather than a second one.
    expect(keys[1]).toBe(line?.clientOpId);
  });
});

describe("committing one tab", () => {
  it("leaves every other tab's lines exactly where they were", async () => {
    // The whole point of per-target records: two jobs, one walk to the shelf, and
    // finishing one of them must not touch the other.
    stubApi((request) =>
      request.url.includes("/bom") && request.method !== "GET"
        ? json({ lines: [], deleted_ids: [], results: lineResults(request.body, "edits") })
        : stageOk(request),
    );
    const build = cartFor(BUILD);
    const project = cartFor(PROJECT);
    build.add(draft({ lotId: 7 }));
    const untouched = project.add(draft({ lotId: 8 }));

    await checkoutCart(build);

    expect(staged()).toHaveLength(1);
    expect(staged()[0]?.url).toContain("/api/builds/5/stage");
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

  it("matches verdicts by client_line_id, not by position", async () => {
    // Moved here with ADR 0011: the batch machinery this exercises is now the
    // project path's alone, since a build stages one line per request. The
    // property is unchanged — a persisted record can be open in two browser tabs,
    // so a position is not a promise this code can make.
    stubApi((request) => {
      const edits = request.body["edits"] as { client_line_id: string }[];
      return json({
        lines: [],
        deleted_ids: [],
        // Deliberately out of order, and with indices that disagree.
        results: [
          {
            index: 0,
            client_line_id: edits[1]?.client_line_id,
            applied: false,
            reason: "unknown_part",
            message: "part 43 is gone",
          },
          { index: 1, client_line_id: edits[0]?.client_line_id, applied: true },
        ],
      });
    });
    const cart = cartFor(PROJECT);
    cart.add(draft({ designator: "C1" }));
    const gone = cart.add(draft({ partId: 43, designator: "C2" }));

    await checkoutCart(cart);

    expect(cart.lines().map((line) => line.id)).toEqual([gone?.id]);
  });

  it("ignores a verdict about a line it never sent", async () => {
    // The bogus verdict carries `index: 0` deliberately. With an out-of-range
    // index it would be discarded by the position lookup finding nothing, and the
    // id-first matching this is really about would go unasserted.
    stubApi((request) => {
      const edits = request.body["edits"] as { client_line_id: string }[];
      return json({
        lines: [],
        deleted_ids: [],
        results: [
          { index: 0, client_line_id: edits[0]?.client_line_id, applied: true },
          { index: 0, client_line_id: "someone-elses-row", applied: false, reason: "nope" },
        ],
      });
    });
    const cart = cartFor(PROJECT);
    cart.add(draft());

    const outcome = await checkoutCart(cart);

    expect(outcome.applied).toBe(1);
    expect(outcome.failed).toEqual([]);
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
