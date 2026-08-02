/**
 * A search result that says *where*, not only *how many*.
 *
 * The row could always say "in 2 bins" and never which two, so the only way to
 * find out where anything was involved opening the part. These pin the three
 * things that make the named list trustworthy rather than merely present:
 *
 * * the fullest container leads, because that is the one worth walking to and
 *   the drawn walk on the search screen goes to exactly it;
 * * the cap is visible — "and 2 more" — so a capped list can never be read as
 *   the whole story;
 * * an unstocked part says nothing at all rather than showing an empty rail.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PartResultRow, PlaceLine } from "./PartResultRow";
import type { PartSummary } from "../lib/api/client";

function summary(over: Partial<PartSummary> = {}): PartSummary {
  return {
    id: 1,
    name: "10k 1% 0603",
    mpn: "DEMO-RES-10K",
    description: null,
    is_stub: false,
    category_id: null,
    qty_milli: 620_000,
    lot_count: 2,
    location_count: 2,
    locations: [
      { location_id: 4, label_path: "Workshop / Workbench cabinet / 01", qty_milli: 500_000 },
      { location_id: 5, label_path: "Workshop / Workbench cabinet / 02", qty_milli: 120_000 },
    ],
    ...over,
  } as PartSummary;
}

describe("PlaceLine", () => {
  it("names the containers, in the order the server ranked them", () => {
    render(<PlaceLine part={summary()} />);

    const chips = screen.getAllByText(/Workbench cabinet/);
    expect(chips.map((chip) => chip.textContent)).toEqual([
      expect.stringContaining("Workshop / Workbench cabinet / 01"),
      expect.stringContaining("Workshop / Workbench cabinet / 02"),
    ]);
  });

  it("says how many it is not showing, so the cap cannot read as the total", () => {
    // The server names three and reports five; the row must not imply three.
    render(
      <PlaceLine
        part={summary({
          location_count: 5,
          locations: [
            { location_id: 1, label_path: "Bin A", qty_milli: 5_000 },
            { location_id: 2, label_path: "Bin B", qty_milli: 4_000 },
            { location_id: 3, label_path: "Bin C", qty_milli: 3_000 },
          ],
        })}
      />,
    );

    expect(screen.getByText("and 2 more")).toBeTruthy();
  });

  it("shows the split only when there is a split to show", () => {
    // One container already has its quantity stated by the stock badge above;
    // repeating it on every single-bin row is noise.
    const { container } = render(
      <PlaceLine
        part={summary({
          location_count: 1,
          locations: [{ location_id: 4, label_path: "Bin A", qty_milli: 620_000 }],
        })}
      />,
    );

    expect(container.textContent).toBe("Bin A");
  });

  it("renders nothing for a part that is nowhere", () => {
    const { container } = render(
      <PlaceLine part={summary({ qty_milli: 0, lot_count: 0, location_count: 0, locations: [] })} />,
    );

    expect(container.innerHTML).toBe("");
  });

  it("survives a payload that predates the field", () => {
    // `locations` is read with `?? []` rather than assumed: a cached response
    // from an older API must not blank the whole result list.
    const { locations: _dropped, ...older } = summary();
    const { container } = render(<PlaceLine part={older as PartSummary} />);

    expect(container.innerHTML).toBe("");
  });
});

describe("PartResultRow", () => {
  it("keeps the stock badge and adds the places under it", () => {
    render(<PartResultRow part={summary()} />);

    expect(screen.getByText(/in stock/)).toBeTruthy();
    expect(screen.getByText(/Workbench cabinet \/ 01/)).toBeTruthy();
  });
});
