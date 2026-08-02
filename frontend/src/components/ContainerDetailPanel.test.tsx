/**
 * The container detail panel says one thing about how full a bin is.
 *
 * `locations.is_overfull` is written only by the nightly pass; `capacity` is
 * computed live on every read. The panel drew the badge from the first and the
 * meter from the second, so for up to a day after emptying an over-full bin the
 * meter read 20% with no "Over capacity" notice while an amber "over" badge sat
 * beside the name — two statements about the same bin, on one screen, a scroll
 * apart. It is the same defect the API payload itself had before #80, and this
 * was the last copy of it.
 */

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { Detail } from "./ContainerDetailPanel";
import type { LocationRead } from "../lib/api/client";

function container(overrides: {
  persisted: boolean;
  live: boolean;
  fillRatio: number;
}): LocationRead {
  return {
    id: 4,
    name: "Drawer B2",
    slot_label: null,
    short_id: null,
    effective_glyph: null,
    effective_esd_safe: null,
    is_placeable: null,
    // What the nightly pass last persisted.
    is_overfull: overrides.persisted,
    lots: [],
    path: [],
    capacity: {
      model: "slots",
      capacity: 10,
      used: overrides.fillRatio * 10,
      fill_ratio: overrides.fillRatio,
      is_full: overrides.fillRatio >= 1,
      // What is true now.
      is_overfull: overrides.live,
      unit: "parts",
    },
  } as unknown as LocationRead;
}

function draw(location: LocationRead): void {
  render(
    <MemoryRouter>
      <Detail location={location} childCount={0} />
    </MemoryRouter>,
  );
}

describe("how full this container is", () => {
  it("drops the badge as soon as the bin is emptied, without waiting for the pass", () => {
    draw(container({ persisted: true, live: false, fillRatio: 0.2 }));

    // The stale column still says over; nothing on screen may.
    expect(screen.queryByText("over")).toBeNull();
    expect(screen.queryByText("Over capacity")).toBeNull();
  });

  it("shows it the moment a bin goes over, without waiting for the pass", () => {
    draw(container({ persisted: false, live: true, fillRatio: 1.4 }));

    expect(screen.getByText("over")).toBeTruthy();
    expect(screen.getByText("Over capacity")).toBeTruthy();
  });
});
