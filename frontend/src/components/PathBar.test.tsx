/**
 * The trail, and the two things it must never get wrong.
 *
 * What is under test is not "does it render crumbs" but the pair of failures the
 * shared component exists to prevent:
 *
 * - the **current** thing is not a link, and says it is where you are, because a
 *   breadcrumb whose last step navigates to the page you are already on is a
 *   control that does nothing;
 * - a container's ancestors come from `id_path`/`label_path` **paired**, and when
 *   the two disagree it claims no ancestors at all rather than pairing a name with
 *   somebody else's id. A mispaired crumb is a link to the wrong drawer that looks
 *   entirely correct, which is worse than no crumb.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ALL_STORAGE, containerTrail, containerTrailFromIndex } from "../lib/locations/trail";
import { indexTree } from "../lib/locations/tree";
import { PathBar } from "./PathBar";

function node(id: number, parentId: number | null, name: string, labelPath: string, idPath: string) {
  return {
    id,
    parent_id: parentId,
    name,
    slot_label: null,
    label_path: labelPath,
    id_path: idPath,
    depth: idPath.split("/").filter((p) => p !== "").length - 1,
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
    retired_at: null,
  };
}

const NODES = [
  node(2, null, "Workshop", "Workshop", "/2/"),
  node(3, 2, "Cabinet A", "Workshop / Cabinet A", "/2/3/"),
  node(7, 3, "Drawer 07", "Workshop / Cabinet A / Drawer 07", "/2/3/7/"),
];

function draw(ui: React.ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

describe("a container's trail", () => {
  it("links every ancestor and not the container itself", () => {
    const trail = containerTrail({
      id: 7,
      name: "Drawer 07",
      id_path: "/2/3/7/",
      label_path: "Workshop / Cabinet A / Drawer 07",
    });
    draw(<PathBar trail={trail} />);

    expect(screen.getByRole("link", { name: "Workshop" }).getAttribute("href")).toBe("/locations/2");
    expect(screen.getByRole("link", { name: "Cabinet A" }).getAttribute("href")).toBe(
      "/locations/3",
    );
    // The container itself: present, current, and not a link.
    expect(screen.queryByRole("link", { name: "Drawer 07" })).toBeNull();
    expect(screen.getByText("Drawer 07").getAttribute("aria-current")).toBe("page");
  });

  it("starts at the top of storage, so a drawer is never a dead end", () => {
    const trail = containerTrail({
      id: 2,
      name: "Workshop",
      id_path: "/2/",
      label_path: "Workshop",
    });
    draw(<PathBar trail={trail} />);

    expect(screen.getByRole("link", { name: ALL_STORAGE }).getAttribute("href")).toBe("/tree");
  });

  it("claims no ancestors when the two cached paths disagree", () => {
    // A name containing the separator: three label segments, two ids. Pairing
    // them would link "Cabinet" to the id of "Workshop / Cabinet".
    const trail = containerTrail({
      id: 3,
      name: "Cabinet / A",
      id_path: "/2/3/",
      label_path: "Workshop / Cabinet / A",
    });
    draw(<PathBar trail={trail} />);

    expect(screen.queryByRole("link", { name: "Workshop" })).toBeNull();
    expect(screen.getByText("Cabinet / A").getAttribute("aria-current")).toBe("page");
  });
});

describe("a trail over the tree index", () => {
  it("calls back instead of navigating, for a trail inside a form", () => {
    const went = vi.fn();
    const index = indexTree(NODES);
    draw(<PathBar trail={containerTrailFromIndex(index, 7, went)} />);

    // Buttons, not links: routing away from a picker would lose a typed quantity.
    expect(screen.queryByRole("link", { name: "Cabinet A" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Cabinet A" }));
    expect(went).toHaveBeenCalledWith(3);

    fireEvent.click(screen.getByRole("button", { name: ALL_STORAGE }));
    expect(went).toHaveBeenCalledWith(null);
  });

  it("makes the top level the current step when nothing is open", () => {
    const index = indexTree(NODES);
    draw(<PathBar trail={containerTrailFromIndex(index, null, vi.fn())} />);

    expect(screen.getByText(ALL_STORAGE).getAttribute("aria-current")).toBe("page");
    expect(screen.queryByRole("button", { name: ALL_STORAGE })).toBeNull();
  });
});
