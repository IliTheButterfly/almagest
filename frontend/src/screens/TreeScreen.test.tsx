/**
 * The storage map, over a stubbed tree.
 *
 * What is worth asserting here is not that tiles appeared, but the decisions:
 * where a cell links (deeper into the map, or out to the bin screen), that the
 * position in the map is in the URL so it can be sent to someone, that a gap in
 * a cabinet is drawn as a gap, and that "over capacity", "staging" and "fill not
 * measured" each say so in text — the palette's two identity hues are a
 * luminance match, so no state may rest on colour.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TreeScreen } from "./TreeScreen";

interface NodeSpec {
  id: number;
  name: string;
  parent_id: number | null;
  slot_label: string | null;
  /** How *this* node draws its own children — ADR 0006. */
  view?: string;
  is_overfull?: boolean;
  is_staging?: boolean;
  fill_ratio?: number | null;
  lot_count?: number;
}

function node(spec: NodeSpec) {
  return {
    id: spec.id,
    name: spec.name,
    parent_id: spec.parent_id,
    depth: spec.parent_id === null ? 0 : 1,
    id_path: `/${spec.id}/`,
    label_path: spec.name,
    container_type_id: null,
    slot_label: spec.slot_label,
    is_placeable: null,
    // The API always resolves this: instance override, else the container type's,
    // else derived from the type's geometry. A cabinet is a face of drawer fronts.
    effective_child_view: spec.view ?? "cabinet_face",
    is_overfull: spec.is_overfull ?? false,
    is_staging: spec.is_staging ?? false,
    fill_ratio: spec.fill_ratio === undefined ? 0.25 : spec.fill_ratio,
    lot_count: spec.lot_count ?? 1,
    qty_milli: 5000,
  };
}

/*
 * Cabinet A holds A1 and B2 — so a 2x2 grid with two positions empty, which is
 * the case a text tree cannot express. A1 has a bin inside it (so it drills), B2
 * is a leaf and over capacity, and INBOX is a second root that is staging.
 *
 * The roots are drawn as a floor plan, which is what the top level of the tree
 * resolves to on the server as well — furniture standing in a space, with no
 * position to be empty.
 */
const NODES = [
  node({ id: 1, name: "Cabinet A", parent_id: null, slot_label: null, view: "cabinet_face" }),
  node({ id: 2, name: "Drawer A1", parent_id: 1, slot_label: "A1", view: "grid_cells" }),
  node({ id: 3, name: "Drawer B2", parent_id: 1, slot_label: "B2", is_overfull: true }),
  node({ id: 4, name: "Bin 1", parent_id: 2, slot_label: "1", fill_ratio: null }),
  node({ id: 5, name: "INBOX", parent_id: null, slot_label: null, is_staging: true }),
];

function stubTree(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const json = (body: unknown): Response =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (url.pathname === "/api/locations/tree") {
        return json({ nodes: NODES });
      }
      // The detail panel beside the map reads the container it is describing —
      // the selected one, or the level in view when nothing is selected.
      const one = /^\/api\/locations\/(\d+)$/.exec(url.pathname);
      if (one !== null) {
        const found = NODES.find((n) => n.id === Number(one[1]));
        return json({
          ...found,
          description: null,
          short_id: null,
          effective_esd_safe: null,
          effective_glyph: null,
          child_count: NODES.filter((n) => n.parent_id === Number(one[1])).length,
          lots: [],
          capacity: {
            model: "none",
            unit: "none",
            used: 0,
            capacity: null,
            fill_ratio: null,
            is_overfull: found?.is_overfull ?? false,
          },
        });
      }
      throw new Error(`unstubbed request: ${url.pathname}`);
    }),
  );
}

function UrlProbe() {
  return <div data-testid="url">{useLocation().search}</div>;
}

function renderTree(initial = "/tree"): void {
  render(
    <MemoryRouter initialEntries={[initial]}>
      <Routes>
        <Route
          path="/tree"
          element={
            <>
              <TreeScreen />
              <UrlProbe />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

const url = (): string => screen.getByTestId("url").textContent ?? "";
/**
 * A cell on the map.
 *
 * A button rather than a link: the map is a master/detail workspace now, so
 * pressing a cell fills the panel beside it instead of navigating away.
 */
/**
 * Matches the cell *body* only.
 *
 * A cell with children carries two controls — the body ("Show: <container>...")
 * and the strip that looks inside it ("Look inside <container>") — so a bare name
 * match finds both. The body's accessible name begins with the map's action verb,
 * which is what distinguishes them.
 */
const cellName = (name: RegExp): RegExp => new RegExp(`^Show: ${name.source}`);
const cellFor = (name: RegExp): HTMLElement =>
  screen.getByRole("button", { name: cellName(name) });
const findCell = (name: RegExp): Promise<HTMLElement> =>
  screen.findByRole("button", { name: cellName(name) });

beforeEach(() => {
  stubTree();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the top level", () => {
  it("draws the roots as a floor plan rather than as a failed grid", async () => {
    renderTree();
    expect(await findCell(/Cabinet A/)).toBeTruthy();
    expect(cellFor(/INBOX/)).toBeTruthy();
    // A room of cabinets is not a grid that could not align — it is a different
    // kind of picture, so it is stated as one rather than framed as a fallback.
    expect(screen.getByText(/placed rather than slotted/)).toBeTruthy();
    expect(screen.queryByText("no bin")).toBeNull();
  });

  it("marks the staging inbox as something other than an ordinary bin", async () => {
    renderTree();
    const inbox = await findCell(/INBOX/);
    expect(inbox.className).toContain("cell-staging");
    // In words too: the dashed pink edge is not available to a monochrome screen.
    expect(inbox.textContent).toContain("inbox");
  });
});

describe("selecting and drilling", () => {
  it("puts the drilled position in the URL, so the view is still a link", async () => {
    renderTree();
    fireEvent.click(await screen.findByRole("button", { name: "Look inside Cabinet A" }));

    await waitFor(() => expect(url()).toContain("at=1"));
    expect(await findCell(/Drawer A1/)).toBeTruthy();
  });

  it("opens a shared position directly", async () => {
    renderTree("/tree?at=1");
    expect(await findCell(/Drawer A1/)).toBeTruthy();
  });

  it("selects a container into the panel instead of leaving the map", async () => {
    // The whole point of the workspace: pressing a cell does not navigate. The
    // map is still on screen afterwards, and the selection is in the URL so the
    // position can be shared and the Back button restores it.
    renderTree("/tree?at=1");
    fireEvent.click(await findCell(/Drawer B2/));

    await waitFor(() => expect(url()).toContain("sel=3"));
    // Still on the map, not on a second page.
    expect(cellFor(/Drawer A1/)).toBeTruthy();
    expect(await screen.findByRole("complementary", { name: /Drawer B2 details/ })).toBeTruthy();
  });

  it("keeps the two verbs apart on a container that has children", async () => {
    // A shelf holds stock as well as bins, so "choose this" and "look inside"
    // cannot be the same press.
    renderTree("/tree?at=1");
    fireEvent.click(await findCell(/Drawer A1/));

    await waitFor(() => expect(url()).toContain("sel=2"));
    // Selecting did not drill: Bin 1 is not on screen.
    expect(screen.queryByRole("button", { name: /Bin 1/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Look inside Drawer A1" }));
    await waitFor(() => expect(url()).toContain("at=2"));
    expect(await findCell(/Bin 1/)).toBeTruthy();
  });

  it("drops the selection when the map drills, so the panel matches the level", async () => {
    renderTree("/tree?at=1&sel=3");
    fireEvent.click(await screen.findByRole("button", { name: "Look inside Drawer A1" }));

    await waitFor(() => expect(url()).not.toContain("sel="));
  });

  it("falls back to the top level for a position that no longer exists", async () => {
    // A stale link or a hand-edited id must not produce a blank screen.
    renderTree("/tree?at=999");
    expect(await findCell(/Cabinet A/)).toBeTruthy();
  });
});

describe("a container laid out as a grid", () => {
  it("places each child at the row and column its slot label gives", async () => {
    renderTree("/tree?at=1");
    // cell -> .cell-pick wrapper -> the div that carries the grid position. The
    // wrapper is what pick mode added; `closest` rather than counting parents so
    // this does not break again the next time the chrome changes.
    const positioned = (cell: HTMLElement): HTMLElement | null =>
      cell.closest<HTMLElement>("[style*='grid-row']");
    const a1 = positioned(await findCell(/Drawer A1/));
    const b2 = positioned(cellFor(/Drawer B2/));

    expect(a1?.style.gridRow).toBe("1");
    expect(a1?.style.gridColumn).toBe("1");
    expect(b2?.style.gridRow).toBe("2");
    expect(b2?.style.gridColumn).toBe("2");
  });

  it("draws the gaps in the grid as empty positions, labelled", async () => {
    renderTree("/tree?at=1");
    await findCell(/Drawer A1/);

    // A1 and B2 are filled; A2 and B1 exist as places with nothing in them,
    // which is a fact about the furniture that a text tree cannot show.
    expect(screen.getByLabelText("slot A2, no container")).toBeTruthy();
    expect(screen.getByLabelText("slot B1, no container")).toBeTruthy();
    expect(screen.getAllByText("no bin")).toHaveLength(2);
  });

  it("says the geometry is inferred, because it is", async () => {
    renderTree("/tree?at=1");
    expect(await screen.findByText(/read from the slot labels/)).toBeTruthy();
  });
});

describe("fill state", () => {
  it("says 'over' in words as well as in colour", async () => {
    renderTree("/tree?at=1");
    const overfull = await findCell(/Drawer B2/);

    expect(overfull.className).toContain("cell-over");
    expect(overfull.textContent).toContain("over");
    // Said by the cell's own label and by the meter inside it.
    expect(screen.getAllByLabelText(/over capacity/).length).toBeGreaterThan(1);
  });

  it("distinguishes 'not measured' from 'empty'", async () => {
    // Bin 1 has fill_ratio null: no capacity model, so there is nothing to be a
    // fraction of. Drawing that as 0% would invent a measurement.
    renderTree("/tree?at=2");
    await findCell(/Bin 1/);

    expect(screen.getByLabelText(/no capacity model/)).toBeTruthy();
    expect(screen.getByText("n/m")).toBeTruthy();
  });
});

/**
 * Adding a container has to be *here*, because this is where somebody is standing
 * when they discover they need one. Reported twice as "I still can't create my own
 * containers" while every route to do it already existed.
 */
describe("adding a container", () => {
  it("offers it at the top level, with nothing preselected", async () => {
    renderTree();
    const add = await screen.findByRole("link", { name: "Add a container" });
    expect(add.getAttribute("href")).toBe("/containers/new");
  });

  it("carries the position you are already looking at, so it lands here", async () => {
    // Otherwise the parent has to be picked out of a list of every location in
    // the system, having just navigated to the one that was meant.
    //
    // And it goes to that container's *own* page in edit mode, with the add panel
    // open, rather than to a separate create screen: one container, one place it is
    // edited. The top level keeps the standalone screen because there is no
    // container page there to open — a fact about the API, not a level's privilege.
    renderTree("/tree?at=1");
    const add = await screen.findByRole("link", { name: "Add containers in Cabinet A" });
    expect(add.getAttribute("href")).toBe("/locations/1?edit=1&panel=add");
  });

  it("links on to the type library, which is what a container is stamped from", async () => {
    renderTree();
    expect(
      (await screen.findByRole("link", { name: /Container types/ })).getAttribute("href"),
    ).toBe(
      "/container-types",
    );
  });
});

describe("the list view", () => {
  it("is still reachable, and keeps the path filter", async () => {
    renderTree();
    fireEvent.click(await screen.findByRole("button", { name: "List" }));

    await waitFor(() => expect(url()).toContain("view=list"));
    expect(screen.getByLabelText(/Filter by path/i)).toBeTruthy();
    expect(screen.getByRole("table", { name: "Storage tree" })).toBeTruthy();
  });
});
