/**
 * The picker, against a stubbed `fetch`.
 *
 * What is under test is the set of ways in — because the defect it fixes was not a
 * broken control, it was a control that only accepted an unknowable value:
 *
 * - browsing drills and any node can be chosen, not only leaves;
 * - typing filters across the whole tree rather than the level in view;
 * - a short ID that names a part is refused *as* "not a container", here, rather
 *   than becoming a validation error at commit about a field nobody saw;
 * - the container the stock is leaving cannot be its own destination;
 * - the tree failing to load still leaves a usable picker, which is the only
 *   reason the numeric field survives at all.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ContainerPicker, type PickedContainer } from "./ContainerPicker";

function node(
  id: number,
  parentId: number | null,
  name: string,
  labelPath: string,
  extra: Record<string, unknown> = {},
) {
  return {
    id,
    parent_id: parentId,
    name,
    slot_label: null,
    label_path: labelPath,
    id_path: `/${id}/`,
    depth: labelPath.split(" / ").length - 1,
    lot_count: 0,
    qty_milli: 0,
    fill_ratio: null,
    is_overfull: false,
    is_staging: false,
    is_placeable: true,
    container_type_id: null,
    child_grid_rows: null,
    child_grid_cols: null,
    effective_child_view: "list",
    effective_glyph: null,
    ...extra,
  };
}

const NODES = [
  node(1, null, "Workshop", "Workshop"),
  node(2, 1, "Cabinet A", "Workshop / Cabinet A"),
  node(3, 2, "07", "Workshop / Cabinet A / 07", { slot_label: "07" }),
  node(4, 1, "INBOX", "Workshop / INBOX", { is_staging: true, lot_count: 2 }),
];

interface ResolveStub {
  readonly entityType?: string;
  readonly entityPk?: number;
  readonly missing?: boolean;
}

function stubApi(options: { treeFails?: boolean; resolve?: ResolveStub } = {}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const json = (body: unknown, status = 200): Response =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === "/api/locations/tree") {
        if (options.treeFails === true) {
          return json({ detail: "boom" }, 500);
        }
        return json({ nodes: NODES });
      }
      if (url.pathname.startsWith("/api/resolve/")) {
        const stub = options.resolve ?? {};
        if (stub.missing === true) {
          return json({ status: "unknown", normalized: "4K7T92M8", target: null });
        }
        return json({
          status: "ok",
          normalized: "4K7T92M8",
          target: {
            short_id: "4K7T92M8",
            display: "BIN 4K7T-92M8",
            entity_type: stub.entityType ?? "location",
            entity_pk: stub.entityPk ?? 3,
            label: "07",
            label_path: "Workshop / Cabinet A / 07",
          },
        });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

const picked: PickedContainer[] = [];

function renderPicker(props: Partial<React.ComponentProps<typeof ContainerPicker>> = {}) {
  return render(<ContainerPicker onPick={(value) => picked.push(value)} {...props} />);
}

beforeEach(() => {
  picked.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("browsing", () => {
  it("starts at the top level and drills in", async () => {
    stubApi();
    renderPicker();

    fireEvent.click(await screen.findByRole("button", { name: "Open" }));

    // Inside Workshop: its two children, not the whole tree.
    expect(await screen.findByText("Cabinet A")).toBeTruthy();
    expect(screen.getByText("INBOX")).toBeTruthy();
  });

  it("chooses a container that has children, because a shelf holds stock too", async () => {
    stubApi();
    renderPicker({ actionLabel: "Put it in" });

    fireEvent.click(await screen.findByRole("button", { name: "Put it in" }));

    expect(picked).toEqual([{ id: 1, label: "Workshop" }]);
  });

  it("starts where it is told, so emptying a drawer offers its siblings first", async () => {
    stubApi();
    renderPicker({ startAtId: 2 });

    // Cabinet A itself is choosable, and its child is the row on offer. ("07" is
    // both the name and the slot badge, hence the plural query.)
    expect(await screen.findByRole("button", { name: "Choose Cabinet A" })).toBeTruthy();
    expect(screen.getAllByText("07").length).toBeGreaterThan(0);
  });

  it("marks the container the stock is leaving instead of offering it", async () => {
    stubApi();
    renderPicker({ startAtId: 1, excludeIds: [2] });

    const row = await screen.findByRole("button", { name: "Where it is now" });
    expect(row).toHaveProperty("disabled", true);
  });

  it("says which chosen container is chosen", async () => {
    stubApi();
    renderPicker({ startAtId: 1, pickedId: 2 });

    expect(await screen.findByRole("button", { name: "Chosen" })).toBeTruthy();
  });
});

describe("typing part of a path", () => {
  it("searches the whole tree, not the level in view", async () => {
    stubApi();
    renderPicker();

    await screen.findByText("Workshop");
    fireEvent.change(screen.getByLabelText(/Find a container/i), {
      target: { value: "cabinet a 07" },
    });

    // The match is three levels down and was never on screen.
    expect(await screen.findByText("Workshop / Cabinet A / 07")).toBeTruthy();
    expect(screen.getByText("1 match(es)")).toBeTruthy();
  });

  it("says so plainly when nothing matches", async () => {
    stubApi();
    renderPicker();

    await screen.findByText("Workshop");
    fireEvent.change(screen.getByLabelText(/Find a container/i), {
      target: { value: "nowhere" },
    });

    expect(await screen.findByText("Nothing matches that.")).toBeTruthy();
  });
});

describe("a typed short ID", () => {
  it("picks the container it names", async () => {
    stubApi();
    renderPicker();

    await screen.findByText("Workshop");
    fireEvent.change(screen.getByLabelText(/Container short ID/i), {
      target: { value: "4k7t-92m8" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Look up" }));

    await waitFor(() =>
      expect(picked).toEqual([{ id: 3, label: "Workshop / Cabinet A / 07" }]),
    );
  });

  it("refuses a code that names a part, in the terms the user was choosing in", async () => {
    stubApi({ resolve: { entityType: "part", entityPk: 9 } });
    renderPicker();

    await screen.findByText("Workshop");
    fireEvent.change(screen.getByLabelText(/Container short ID/i), {
      target: { value: "4k7t-92m8" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Look up" }));

    expect(await screen.findByText(/is a part, not a container/i)).toBeTruthy();
    expect(picked).toEqual([]);
  });

  it("refuses the container the stock is coming out of", async () => {
    stubApi({ resolve: { entityPk: 3 } });
    renderPicker({ excludeIds: [3] });

    await screen.findByText("Workshop");
    fireEvent.change(screen.getByLabelText(/Container short ID/i), {
      target: { value: "4k7t-92m8" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Look up" }));

    expect(await screen.findByText(/coming out of/i)).toBeTruthy();
    expect(picked).toEqual([]);
  });

  it("says nothing answers to a code that resolves to nothing", async () => {
    stubApi({ resolve: { missing: true } });
    renderPicker();

    await screen.findByText("Workshop");
    fireEvent.change(screen.getByLabelText(/Container short ID/i), {
      target: { value: "4k7t-92m8" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Look up" }));

    expect(await screen.findByText(/Nothing here answers to/i)).toBeTruthy();
  });
});

describe("when the tree will not load", () => {
  it("says so and still lets a container be named", async () => {
    stubApi({ treeFails: true });
    renderPicker();

    // This is the whole justification for keeping the numeric field: with the
    // tree gone, browsing and filtering are both unavailable.
    await waitFor(() => expect(screen.getByLabelText("Location id")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Location id"), { target: { value: "41" } });
    fireEvent.click(screen.getByRole("button", { name: "Use that id" }));

    expect(picked).toEqual([{ id: 41, label: "location 41" }]);
  });
});
