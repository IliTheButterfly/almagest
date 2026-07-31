/**
 * The daily loop, simulated: walk up to a drawer, check it, take what the list asked for.
 *
 * > *I look at the list and go grab all the containers I need. I sit down at the
 * > desk, scan the first container. A confirmation or error message shows if I got
 * > the right container. I then select how many I need to take. [...] and I get a
 * > confirmation on if I took the right amount.*
 *
 * The case that matters most is the **wrong** container, because that is the error
 * no amount of care prevents: the drawer beside the right one looks identical from
 * two feet away, and taking from it leaves the ledger confidently wrong. So the
 * wrong-container assertion here checks that the screen *names both* — what was
 * scanned and what was wanted — since "wrong drawer" is not something a person can
 * act on.
 *
 * The other half is that being wrong does not trap anybody. CLAUDE.md is explicit
 * that a scan is never rejected: the person is holding the drawer and the database
 * is not, so the take stays possible and is simply marked as unchecked. Blocking
 * teaches people to stop scanning, and then nothing is checked at all.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GuidedPickStop } from "./GuidedPick";
import type { PickStopRead } from "../lib/api/client";
import { simulatedTagSource } from "../lib/tags/simulated";

const RIGHT_DRAWER = 101;
const WRONG_DRAWER = 102;
const RIGHT_UID = "04A1B2C3D4E580";
const WRONG_UID = "04A1B2C3D4E581";

const STOP: PickStopRead = {
  location_id: RIGHT_DRAWER,
  label_path: "Workshop / Cabinet A / A1",
  id_path: "/1/10/101/",
  short_id: "AAAAAAA1",
  qty_milli: 40_000,
  takes: [
    {
      lot_id: 900,
      part_id: 5,
      part_name: "100nF 0805 X7R",
      part_mpn: "CC0805KRX7R9BB104",
      qty_milli: 40_000,
      bom_line_id: 3,
      allocation_id: null,
      line_no: 3,
      designators: "C1-C40",
      is_substitute: false,
      whole_lot: false,
    },
  ],
};

const consumed: { lotId: number; body: Record<string, unknown> }[] = [];

function stubFetch(): void {
  consumed.length = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const json = (payload: unknown, status = 200): Response =>
        new Response(JSON.stringify(payload), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === "/api/location-tags/resolve") {
        const body = (await request.json()) as { tag_uid?: string };
        const isRight = body.tag_uid === RIGHT_UID;
        if (body.tag_uid !== RIGHT_UID && body.tag_uid !== WRONG_UID) {
          return json({ status: "unknown", matched_by: "none", location: null, tag: null, disagreement: false });
        }
        return json({
          status: "resolved",
          matched_by: "uid",
          disagreement: false,
          tag: null,
          location: {
            location_id: isRight ? RIGHT_DRAWER : WRONG_DRAWER,
            name: isRight ? "A1" : "A2",
            slot_label: isRight ? "A1" : "A2",
            label_path: isRight ? STOP.label_path : "Workshop / Cabinet A / A2",
            short_id: isRight ? "AAAAAAA1" : "AAAAAAA2",
          },
        });
      }
      if (url.pathname.endsWith("/consume")) {
        const lotId = Number(url.pathname.split("/").at(-2));
        consumed.push({ lotId, body: (await request.json()) as Record<string, unknown> });
        return json({ seq: 1, lot: { id: lotId, qty_milli_cached: 0 } });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderStop(source?: ReturnType<typeof simulatedTagSource>) {
  return render(
    <MemoryRouter>
      <GuidedPickStop stop={STOP} index={1} {...(source === undefined ? {} : { source })} />
    </MemoryRouter>,
  );
}

beforeEach(stubFetch);
afterEach(() => vi.unstubAllGlobals());

describe("picking a stop", () => {
  it("confirms the right drawer and records exactly what the list asked for", async () => {
    const reader = simulatedTagSource([
      { uid: RIGHT_UID, url: null },
      { uid: WRONG_UID, url: null },
    ]);
    renderStop(reader);
    fireEvent.click(screen.getByRole("button", { name: "Pick this stop…" }));

    reader.tap(RIGHT_UID);
    await waitFor(() => expect(screen.getByText("Right container")).toBeTruthy());

    // Pre-filled with the planned quantity, so the common case is one press.
    fireEvent.click(screen.getByRole("button", { name: /^Take / }));

    await waitFor(() => expect(consumed).toHaveLength(1));
    expect(consumed[0]?.lotId).toBe(900);
    expect(consumed[0]?.body["qty_milli"]).toBe(40_000);
    // The confirmation states both numbers, because "took 40" is not checkable
    // and "took 40 of 40" is.
    await waitFor(() => expect(screen.getByText(/Took 40 of 40/)).toBeTruthy());
  });

  it("names the drawer you actually scanned, and the one you wanted", async () => {
    const reader = simulatedTagSource([
      { uid: RIGHT_UID, url: null },
      { uid: WRONG_UID, url: null },
    ]);
    renderStop(reader);
    fireEvent.click(screen.getByRole("button", { name: "Pick this stop…" }));

    reader.tap(WRONG_UID);

    await waitFor(() => expect(screen.getByText("That is a different container")).toBeTruthy());
    expect(screen.getByText(/Cabinet A \/ A2/)).toBeTruthy();
    expect(screen.getAllByText(/Cabinet A \/ A1/).length).toBeGreaterThan(0);
  });

  it("still lets the take happen, and says it was not checked", async () => {
    renderStop();
    fireEvent.click(screen.getByRole("button", { name: "Pick this stop…" }));

    // No scan at all. A scan is never a gate that traps someone holding the right
    // parts and the wrong record.
    expect(screen.getByText(/Recording this without a confirmed scan/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^Take / }));

    await waitFor(() => expect(consumed).toHaveLength(1));
  });

  it("says a short take is short rather than calling it done", async () => {
    const reader = simulatedTagSource([{ uid: RIGHT_UID, url: null }]);
    renderStop(reader);
    fireEvent.click(screen.getByRole("button", { name: "Pick this stop…" }));
    reader.tap(RIGHT_UID);
    await waitFor(() => expect(screen.getByText("Right container")).toBeTruthy());

    // The drawer only had 38. The pad is a keypad, and typing starts a fresh
    // number rather than incrementing — a screen that opens on 40 must not turn a
    // tap of "3" into 403.
    fireEvent.click(screen.getByRole("button", { name: "3" }));
    fireEvent.click(screen.getByRole("button", { name: "8" }));
    fireEvent.click(screen.getByRole("button", { name: /^Take / }));

    // Both numbers again, and the word that makes it a finding rather than a tick.
    await waitFor(() => expect(screen.getByText(/Took 38 of 40 — short/)).toBeTruthy());
    expect(consumed[0]?.body["qty_milli"]).toBe(38_000);
  });

  it("offers the optical counter as unavailable rather than as a dead button", () => {
    renderStop();
    fireEvent.click(screen.getByRole("button", { name: "Pick this stop…" }));

    // Overstating what the hardware can do is the specific failure CLAUDE.md's
    // honest-capability-limits section exists to prevent. There is no counting
    // camera, so the affordance says so instead of doing nothing when pressed.
    const button = screen.getByRole("button", { name: "Count optically" });
    expect(button.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText(/No counting camera is attached/)).toBeTruthy();
  });
});
