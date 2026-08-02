/**
 * Assigning a container its printed identity, against a stubbed `fetch`.
 *
 * The behaviours under test are the ones that keep the printed world and the
 * database in step, which is the whole reason this endpoint refuses things:
 *
 * - minting is safe to press twice, so a print button needs no prior check;
 * - a code that fails its check symbol is explained as a *misread*, not as
 *   "invalid input", because that is what it almost always is;
 * - a taken code names the drawer that holds it, so the next action is obvious;
 * - adopting says the old code still resolves, because a user who thinks
 *   relabelling breaks the old label will never relabel anything.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LocationScreen } from "./LocationScreen";

const LOCATION = {
  id: 11,
  parent_id: 2,
  name: "Drawer 07",
  slot_label: "07",
  short_id: null as string | null,
  label_path: "Workshop / Workbench cabinet / 07",
  description: null,
  depth: 2,
  child_count: 0,
  lot_count: 0,
  qty_milli: 0,
  is_staging: false,
  is_overfull: false,
  is_placeable: true,
  effective_esd_safe: null,
  child_view: null,
  effective_child_view: "floor_plan",
  glyph: null,
  effective_glyph: null,
  photo: null,
  effective_photo: null,
  container_type_id: null,
  lots: [],
  capacity: {
    model: "none",
    used: 0,
    capacity: null,
    unit: "slots",
    fill_ratio: null,
    is_overfull: false,
  },
};

interface Call {
  readonly url: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

const calls: Call[] = [];

/** `shortId` is what `/short-id` answers with; `conflict` makes it refuse. */
function stubApi(
  options: {
    existing?: string | null;
    shortId?: string;
    adopted?: boolean;
    previous?: string | null;
    conflict?: { reason: string; message: string; held_by?: string | null };
    /** Merged into the location payload, for the cards that render conditionally. */
    location?: Record<string, unknown>;
  } = {},
): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      calls.push({
        url: url.pathname,
        method: request.method,
        body: raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>),
      });

      const json = (body: unknown, status = 200): Response =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === "/api/locations/11") {
        return json({ ...LOCATION, short_id: options.existing ?? null, ...options.location });
      }
      if (url.pathname === "/api/locations/11/short-id") {
        if (options.conflict !== undefined) {
          return json({ detail: options.conflict }, 409);
        }
        const code = options.shortId ?? "4K7T92M8";
        return json({
          location_id: 11,
          short_id: code,
          display: `BIN ${code.slice(0, 4)}-${code.slice(4)}`,
          adopted: options.adopted ?? false,
          previous_short_id: options.previous ?? null,
          replayed: false,
        });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderScreen() {
  return render(
    <MemoryRouter initialEntries={["/locations/11"]}>
      <Routes>
        <Route path="/locations/:locationId" element={<LocationScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

function callsTo(pathname: string): Call[] {
  return calls.filter((call) => call.url === pathname);
}

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the printed id", () => {
  it("says a generated cell has none yet, rather than showing nothing", async () => {
    stubApi();
    renderScreen();

    expect(await screen.findByText("none yet")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Assign one" })).toBeTruthy();
  });

  it("mints one, sending no code so the server chooses", async () => {
    stubApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Assign one" }));

    await waitFor(() => expect(callsTo("/api/locations/11/short-id").length).toBe(1));
    const body = callsTo("/api/locations/11/short-id")[0]?.body;
    expect(body?.["short_id"]).toBeNull();
    // Idempotency key on a write that mints: a retried request must not produce
    // a second id for the same drawer.
    expect(typeof body?.["client_op_id"]).toBe("string");

    expect(await screen.findByText(/Printed id is 4K7T-92M8/)).toBeTruthy();
  });

  it("offers relabelling even when there is already an id", async () => {
    stubApi({ existing: "4K7T92M8" });
    renderScreen();

    // No "Assign one" — it has one — but adoption stays available, because
    // relabelling is non-destructive.
    expect(await screen.findByRole("button", { name: "I already have a label…" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Assign one" })).toBeNull();
  });

  it("adopts a code typed as it is printed, hyphen and all", async () => {
    stubApi({ adopted: true });
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "I already have a label…" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "4k7t-92m8" } });
    fireEvent.click(screen.getByRole("button", { name: "Adopt this code" }));

    await waitFor(() => expect(callsTo("/api/locations/11/short-id").length).toBe(1));
    // Normalised before it is sent: upper-cased, hyphen dropped.
    expect(callsTo("/api/locations/11/short-id")[0]?.body["short_id"]).toBe("4K7T92M8");
  });

  it("will not submit a code that is not even the right shape", async () => {
    stubApi({ adopted: true });
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "I already have a label…" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "4K7T" } });

    // Shape only — the check symbol is the server's call, because this field
    // must not refuse anything the server would have accepted.
    expect(screen.getByRole("button", { name: "Adopt this code" })).toHaveProperty("disabled", true);
    expect(callsTo("/api/locations/11/short-id")).toHaveLength(0);
  });

  it("says the old code still resolves after relabelling", async () => {
    stubApi({ existing: "4K7T92M8", adopted: true, shortId: "9P2XR4T7", previous: "4K7T92M8" });
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "I already have a label…" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "9P2X-R4T7" } });
    fireEvent.click(screen.getByRole("button", { name: "Adopt this code" }));

    // The reassurance is the point: someone who believes relabelling breaks the
    // old label will never relabel anything.
    expect(await screen.findByText(/4K7T-92M8 still resolves here/)).toBeTruthy();
  });

  it("explains a failed check symbol as a misread, not as invalid input", async () => {
    stubApi({
      conflict: { reason: "check", message: "check symbol does not match" },
    });
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "I already have a label…" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "4K7T-92MQ" } });
    fireEvent.click(screen.getByRole("button", { name: "Adopt this code" }));

    expect(await screen.findByText(/mistyped or misread/)).toBeTruthy();
  });

  it("names the drawer that already holds a taken code", async () => {
    stubApi({
      conflict: {
        reason: "short_id_taken",
        message: "4K7T-92M8 is already on Workshop / Workbench cabinet / 02",
        held_by: "Workshop / Workbench cabinet / 02",
      },
    });
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "I already have a label…" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "4K7T-92M8" } });
    fireEvent.click(screen.getByRole("button", { name: "Adopt this code" }));

    // Which drawer to walk to, not "already bound to location 41".
    expect(await screen.findByText(/Workbench cabinet \/ 02/)).toBeTruthy();
  });

  it("keeps the form open after a refusal, so the code can be corrected", async () => {
    stubApi({
      conflict: { reason: "check", message: "check symbol does not match" },
    });
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "I already have a label…" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "4K7T-92MQ" } });
    fireEvent.click(screen.getByRole("button", { name: "Adopt this code" }));

    await screen.findByText(/mistyped or misread/);
    // Retyping beats starting over: the code is in your hand, on a label.
    expect(screen.getByRole("textbox")).toHaveProperty("value", "4K7T-92MQ");
  });
});

describe("the cards a drawer only shows when it has something to say", () => {
  it("draws neither a picture nor a capacity meter when there is neither", async () => {
    // This is the screen every tag tap and every QR lands on. An unconditional
    // `Picture` drew a 150px dashed "?" that is not a link and not a button, and
    // `Capacity` printed "0 of ? none used" under it — together pushing the
    // contents of the drawer off the bottom of a phone. Absence is communicated
    // by absence (ADR 0003), including here.
    stubApi();
    renderScreen();
    await waitFor(() => expect(screen.getAllByText("Drawer 07")[0]).toBeTruthy());

    // The placeholder, not `role="img"`: with nothing to show `ContainerPhoto`
    // renders a dashed box carrying no role at all, which is exactly the empty
    // affordance being removed — so querying for an image would pass on the very
    // markup this test exists to prevent.
    expect(document.querySelector(".container-photo-placeholder")).toBeNull();
    expect(document.querySelector(".container-photo")).toBeNull();
    expect(screen.queryByText(/capacity/i)).toBeNull();
  });

  it("draws them when there is a photograph and a real capacity model", async () => {
    // The control. Without it the test above passes on a screen that has lost
    // the ability to show either.
    stubApi({
      location: {
        effective_photo: "/api/documents/7/content",
        capacity: {
          model: "slots",
          used: 3,
          capacity: 4,
          unit: "slots",
          fill_ratio: 0.75,
          is_overfull: false,
        },
      },
    });
    renderScreen();
    await waitFor(() => expect(screen.getAllByText("Drawer 07")[0]).toBeTruthy());

    expect(document.querySelector(".container-photo, .container-photo-placeholder")).toBeTruthy();
    expect(screen.getByText(/capacity/i)).toBeTruthy();
  });
});
