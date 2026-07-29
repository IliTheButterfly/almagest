/**
 * The cart screen: three destinations, one list.
 *
 * What is worth testing here is not that the buttons render but that each door
 * sends the *right* request and that the cart is left in the right state
 * afterwards — including the states that only exist when something went wrong,
 * since a refused line staying put with its reason is the behaviour that makes a
 * partial checkout recoverable rather than mysterious.
 *
 * The scan path is exercised through a typed short ID rather than a fake camera:
 * both go through the same `resolveScan` call by construction, and typing is not a
 * lesser path — ADR 0001 means the camera does not exist at all over plain HTTP.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { shoppingCart, type CartLineDraft } from "../lib/cart/cart";
import { CartScreen } from "./CartScreen";

const PROJECT = {
  id: 7,
  name: "Widget rev B",
  revision: "B",
  status: "active",
  description: null,
  source_ref: null,
  notes: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  builds: [
    {
      id: 12,
      project_id: 7,
      build_no: 2,
      label: "second run",
      assembly_count: 5,
      bom_revision: "B",
      status: "planned",
      staging_location_id: null,
      started_at: null,
      completed_at: null,
      notes: null,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    },
  ],
};

const CONTAINER = {
  status: "resolved",
  decoded_kind: "short_id",
  normalized: "4K7T92M8",
  suggest_bind: false,
  target: {
    entity_type: "location",
    entity_pk: 31,
    label: "A1-04",
    label_path: "Bench / Cabinet A / A1-04",
    short_id: "4K7T92M8",
    display: null,
  },
  candidates: [],
  parsed: null,
  existing_lots: [],
  latency_ms: 2,
  scan_event_id: 1,
};

interface Call {
  readonly path: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

let calls: Call[] = [];

/** Per-path replies; anything not overridden gets the happy default. */
interface Stubs {
  readonly bom?: unknown;
  readonly allocate?: unknown;
  readonly move?: unknown;
  readonly resolve?: unknown;
  readonly status?: number;
}

function stubApi(stubs: Stubs = {}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      calls.push({
        path: url.pathname,
        method: request.method,
        body: raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>),
      });
      const json = (body: unknown, status = 200): Response =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === "/api/projects" && request.method === "GET") {
        return json({ total: 1, projects: [PROJECT] });
      }
      if (url.pathname === "/api/projects/7" && request.method === "GET") {
        return json(PROJECT);
      }
      if (url.pathname === "/api/projects/7/bom" && request.method === "PUT") {
        const body = JSON.parse(raw) as { edits: { client_line_id: string }[] };
        return json(
          stubs.bom ?? {
            lines: [],
            deleted_ids: [],
            results: body.edits.map((edit, index) => ({
              index,
              client_line_id: edit.client_line_id,
              applied: true,
            })),
          },
          stubs.status ?? 200,
        );
      }
      if (url.pathname === "/api/builds/12/allocate-batch") {
        const body = JSON.parse(raw) as { lines: { client_line_id: string }[] };
        return json(
          stubs.allocate ?? {
            results: body.lines.map((line, index) => ({
              index,
              client_line_id: line.client_line_id,
              applied: true,
            })),
            replayed: false,
          },
        );
      }
      if (url.pathname === "/api/stock/movements") {
        const body = JSON.parse(raw) as { lines: { client_line_id: string }[] };
        return json(
          stubs.move ?? {
            group_uuid: "group-1",
            results: body.lines.map((line, index) => ({
              index,
              client_line_id: line.client_line_id,
              applied: true,
            })),
            replayed: false,
          },
        );
      }
      if (url.pathname === "/api/scan/resolve") {
        return json(stubs.resolve ?? CONTAINER);
      }
      if (url.pathname === "/api/stock/undo") {
        return json({ seqs: [9], reversed_seqs: [8], lots: [], replayed: false });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function draft(overrides: Partial<CartLineDraft> = {}): CartLineDraft {
  return {
    partId: 42,
    partName: "10k 0603 resistor",
    mpn: "RC0603FR-0710KL",
    qtyMilli: 3_000,
    ...overrides,
  };
}

function renderCart(): void {
  render(
    <MemoryRouter initialEntries={["/cart"]}>
      <Routes>
        <Route path="/cart" element={<CartScreen />} />
        <Route path="/cart/add" element={<p>the shopping view</p>} />
        <Route path="/parts/:partId" element={<p>a part screen</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

const sent = (path: string): Call[] => calls.filter((call) => call.path === path);

/** Choosing a destination: the segmented control, then its select. */
async function chooseProject(): Promise<void> {
  fireEvent.click(await screen.findByRole("button", { name: "A project's BOM" }));
  const select = await screen.findByLabelText("Project");
  await waitFor(() => expect(select.querySelectorAll("option").length).toBe(2));
  fireEvent.change(select, { target: { value: "7" } });
}

async function chooseBuild(): Promise<void> {
  fireEvent.click(await screen.findByRole("button", { name: "A build" }));
  const project = await screen.findByLabelText("Project");
  await waitFor(() => expect(project.querySelectorAll("option").length).toBe(2));
  fireEvent.change(project, { target: { value: "7" } });
  const build = await screen.findByLabelText("Build");
  fireEvent.change(build, { target: { value: "12" } });
}

async function scanContainer(code = "4K7T-92M8"): Promise<void> {
  fireEvent.click(await screen.findByRole("button", { name: "Take or put back" }));
  fireEvent.change(await screen.findByLabelText("Container short ID"), {
    target: { value: code },
  });
  fireEvent.click(screen.getByRole("button", { name: "Look up" }));
}

const checkout = async (): Promise<void> => {
  fireEvent.click(await screen.findByRole("button", { name: /^(Check out|Retry)/ }));
};

beforeEach(() => {
  calls = [];
  shoppingCart.clear();
  stubApi();
});

afterEach(() => {
  shoppingCart.clear();
  vi.unstubAllGlobals();
});

describe("an empty cart", () => {
  it("says so and offers the way to fill it, with nothing to check out", async () => {
    renderCart();

    expect(await screen.findByText(/Nothing in the cart/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Check out/ })).toBeNull();
  });
});

describe("checking out to a project's BOM", () => {
  it("sends one batch of BOM lines, confirmed, and empties the cart", async () => {
    shoppingCart.add(draft({ designator: "R1" }));
    shoppingCart.add(draft({ partId: 43, partName: "100n cap", designator: "C1" }));
    renderCart();

    await chooseProject();
    await checkout();

    await waitFor(() => expect(sent("/api/projects/7/bom")).toHaveLength(1));
    const body = sent("/api/projects/7/bom")[0]?.body as {
      partial: boolean;
      edits: { part_id: number; qty_per_assembly_milli: number; is_match_confirmed: boolean }[];
    };
    // One request for both rows, and partial: nineteen good rows must not be
    // discarded to protect the twentieth.
    expect(body.partial).toBe(true);
    expect(body.edits).toHaveLength(2);
    expect(body.edits[0]?.qty_per_assembly_milli).toBe(3_000);
    // A part picked out of search by hand *is* a confirmed match.
    expect(body.edits.every((edit) => edit.is_match_confirmed)).toBe(true);

    await waitFor(() => expect(shoppingCart.size).toBe(0));
    expect(await screen.findByText("2 line(s) applied")).toBeTruthy();
  });
});

describe("checking out to a build", () => {
  it("reserves the lot each row names", async () => {
    shoppingCart.add(draft({ lotId: 900, locationId: 3, locationLabel: "A1-04" }));
    renderCart();

    await chooseBuild();
    await checkout();

    await waitFor(() => expect(sent("/api/builds/12/allocate-batch")).toHaveLength(1));
    const body = sent("/api/builds/12/allocate-batch")[0]?.body as {
      lines: { lot_id: number; qty_milli: number }[];
    };
    expect(body.lines[0]?.lot_id).toBe(900);
    expect(body.lines[0]?.qty_milli).toBe(3_000);
    await waitFor(() => expect(shoppingCart.size).toBe(0));
  });

  it("refuses a row with no package, locally, and keeps it with a reason", async () => {
    // A hold is a hold on a lot. Told in terms of what is missing rather than
    // relayed as a validation error about a field the user never saw.
    shoppingCart.add(draft());
    renderCart();

    await chooseBuild();
    await checkout();

    expect(await screen.findByText(/does not name one/)).toBeTruthy();
    expect(sent("/api/builds/12/allocate-batch")).toHaveLength(0);
    expect(shoppingCart.size).toBe(1);
  });
});

describe("checking out against a scanned container", () => {
  it("takes the parts out of the container the code resolved to", async () => {
    shoppingCart.add(draft({ lotId: 900 }));
    renderCart();

    await scanContainer();
    expect(await screen.findByText("Bench / Cabinet A / A1-04")).toBeTruthy();

    await checkout();

    await waitFor(() => expect(sent("/api/stock/movements")).toHaveLength(1));
    const body = sent("/api/stock/movements")[0]?.body as {
      location_id: number;
      lines: { direction: string; qty_milli: number; lot_id?: number }[];
    };
    expect(body.location_id).toBe(31);
    expect(body.lines[0]?.direction).toBe("take");
    expect(body.lines[0]?.lot_id).toBe(900);
  });

  it("puts them back instead when that is what happened", async () => {
    shoppingCart.add(draft());
    renderCart();

    await scanContainer();
    fireEvent.click(await screen.findByRole("button", { name: "I put these back" }));
    await checkout();

    await waitFor(() => expect(sent("/api/stock/movements")).toHaveLength(1));
    const body = sent("/api/stock/movements")[0]?.body as {
      lines: { direction: string; part_id?: number; lot_id?: number }[];
    };
    expect(body.lines[0]?.direction).toBe("return");
    // No package chosen, so the row names its part and the server resolves it
    // inside the container as it stands now.
    expect(body.lines[0]?.part_id).toBe(42);
    expect(body.lines[0]?.lot_id).toBeUndefined();
  });

  it("refuses a code that is a part rather than a container", async () => {
    shoppingCart.add(draft());
    stubApi({
      resolve: {
        ...CONTAINER,
        target: { ...CONTAINER.target, entity_type: "part", entity_pk: 42, label: "10k" },
      },
    });
    renderCart();

    await scanContainer("PART-CODE");

    expect(await screen.findByText(/not a container/)).toBeTruthy();
    // And nothing was aimed anywhere: checkout stays refused.
    expect(
      (screen.getByRole("button", { name: /^Check out/ }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("offers one undo for the whole trip to the drawer", async () => {
    shoppingCart.add(draft({ lotId: 900 }));
    renderCart();

    await scanContainer();
    await checkout();

    fireEvent.click(await screen.findByRole("button", { name: "Undo the movement" }));

    await waitFor(() => expect(sent("/api/stock/undo")).toHaveLength(1));
    expect(sent("/api/stock/undo")[0]?.body["group_uuid_to_undo"]).toBe("group-1");
  });
});

describe("a line the server refused", () => {
  it("stays in the cart saying why, while its sibling leaves", async () => {
    shoppingCart.add(draft({ lotId: 900, partName: "the good row" }));
    shoppingCart.add(draft({ lotId: 901, partId: 43, partName: "the emptied row" }));
    stubApi({
      move: {
        group_uuid: "group-1",
        results: [
          { index: 0, client_line_id: shoppingCart.lines()[0]?.id ?? "", applied: true },
          {
            index: 1,
            client_line_id: shoppingCart.lines()[1]?.id ?? "",
            applied: false,
            reason: "insufficient_stock",
            message: "That bin holds 0 of these now.",
          },
        ],
        replayed: false,
      },
    });
    renderCart();

    await scanContainer();
    await checkout();

    expect(await screen.findByText("That bin holds 0 of these now.")).toBeTruthy();
    expect(await screen.findByText("insufficient_stock")).toBeTruthy();
    // The reason is on the row, not only in the banner — a banner is gone after
    // one navigation and the cart is not.
    expect(screen.getByText("not applied")).toBeTruthy();
    await waitFor(() => expect(shoppingCart.size).toBe(1));
    expect(shoppingCart.lines()[0]?.partName).toBe("the emptied row");
    expect(screen.queryByText("the good row")).toBeNull();
  });

  it("retries only what is left", async () => {
    shoppingCart.add(draft({ lotId: 900 }));
    const only = shoppingCart.lines()[0]?.id ?? "";
    shoppingCart.markFailed(only, {
      reason: "insufficient_stock",
      message: "That bin holds 0 of these now.",
      at: Date.now(),
    });
    renderCart();

    await scanContainer();
    fireEvent.click(await screen.findByRole("button", { name: "Retry the 1 remaining line(s)" }));

    await waitFor(() => expect(sent("/api/stock/movements")).toHaveLength(1));
    const body = sent("/api/stock/movements")[0]?.body as { lines: unknown[] };
    expect(body.lines).toHaveLength(1);
  });
});

describe("editing the cart", () => {
  it("changes a quantity", async () => {
    shoppingCart.add(draft());
    renderCart();

    fireEvent.change(await screen.findByLabelText("Quantity of 10k 0603 resistor"), {
      target: { value: "12" },
    });

    await waitFor(() => expect(shoppingCart.lines()[0]?.qtyMilli).toBe(12_000));
  });

  it("removes one row without touching the other", async () => {
    shoppingCart.add(draft());
    shoppingCart.add(draft({ partId: 43, partName: "100n cap" }));
    renderCart();

    fireEvent.click(await screen.findByRole("button", { name: "Remove 100n cap" }));

    await waitFor(() => expect(shoppingCart.size).toBe(1));
    expect(shoppingCart.lines()[0]?.partId).toBe(42);
  });

  it("clears the cart and the destination with it, once confirmed", async () => {
    shoppingCart.add(draft());
    renderCart();
    await chooseProject();
    await waitFor(() => expect(shoppingCart.target.kind).toBe("project"));

    fireEvent.click(await screen.findByRole("button", { name: "Clear cart" }));
    fireEvent.click(await screen.findByRole("button", { name: "Yes, clear it" }));

    await waitFor(() => expect(shoppingCart.size).toBe(0));
    // The destination goes too: a leftover "you are shopping for project X" is
    // the invisible mode the cart was chosen to avoid.
    expect(shoppingCart.target.kind).toBe("unset");
  });

  it("keeps a deleted part legible and removable from what the row captured", async () => {
    shoppingCart.add(draft({ partId: 9999, partName: "a part that no longer exists" }));
    renderCart();

    expect(await screen.findByText("a part that no longer exists")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Remove a part that no longer exists" }));

    await waitFor(() => expect(shoppingCart.size).toBe(0));
  });
});
