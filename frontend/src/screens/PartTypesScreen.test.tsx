/**
 * Authoring part types from the app, against a stubbed `fetch`.
 *
 * What is asserted is what the brief and ADR 0011 say the screen exists to do,
 * not that the inputs render:
 *
 * - **a field can be authored at all**, which was impossible before these routes:
 *   every filterable field came out of a migration, so "capacitors also have an
 *   ESR" was a code change;
 * - **the request carries the answers that cannot be defaulted** — the quantity
 *   picked from the parser's own list, and a substitution direction, without which
 *   a rating silently stops satisfying a lower requirement;
 * - **an inherited field says so**, because it is authored on an ancestor and
 *   editing it changes every sibling category too;
 * - **a name collision is a decision on screen**, with the existing field visible,
 *   rather than a 409 to retype past. One real concept is one field.
 * - **a kind is visibly not a category**, since only one of the two owns fields
 *   and picking wrong is the mistake this screen is shaped to prevent.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PartTypesScreen } from "./PartTypesScreen";

interface Call {
  readonly url: string;
  readonly search: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

const calls: Call[] = [];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const CATEGORIES = [
  { id: 1, slug: "passives", name: "Passives", parent_slug: null, depth: 0, part_count: 40 },
  {
    id: 2,
    slug: "capacitors",
    name: "Capacitors",
    parent_slug: "passives",
    depth: 1,
    part_count: 12,
  },
];

const UNITS = [
  { name: "ohm", symbol: "Ω" },
  { name: "farad", symbol: "F" },
];

function parameterField(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 12,
    name: "capacitance",
    display_name: "Capacitance",
    value_type: "numeric",
    base_unit: "farad",
    substitution_direction: "range_overlap",
    applies_to_category: "capacitors",
    sort_order: 10,
    plausible_min: 1e-12,
    plausible_max: 1,
    inherited: false,
    is_seed: true,
    value_count: 12,
    choices: [],
    ...overrides,
  };
}

/** A field of the user's own that parts already use — so it cannot be deleted. */
const ESR_USED = parameterField({
  id: 20,
  name: "esr",
  display_name: "ESR",
  base_unit: "ohm",
  substitution_direction: "lower_ok",
  is_seed: false,
  value_count: 3,
});

/** `mounting_type` as the API reports it on a category: global, so inherited. */
const MOUNTING = parameterField({
  id: 3,
  name: "mounting_type",
  display_name: "Mounting",
  value_type: "enum",
  base_unit: null,
  substitution_direction: "exact",
  applies_to_category: null,
  inherited: true,
  value_count: 30,
  choices: [
    { id: 5, key: "smd", label: "SMD", aliases: ["smt"], sort_order: 0, use_count: 22 },
    { id: 6, key: "tht", label: "Through hole", aliases: [], sort_order: 1, use_count: 8 },
  ],
});

function stubApi(options: { createStatus?: number } = {}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      const body = raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>);
      calls.push({ url: url.pathname, search: url.search, method: request.method, body });

      if (url.pathname === "/api/part-categories" && request.method === "GET") {
        return json(CATEGORIES);
      }
      if (url.pathname === "/api/part-kinds" && request.method === "GET") {
        return json([
          { id: 1, slug: "component", display_name: "Component", sort_order: 0, part_count: 40 },
          { id: 2, slug: "tool", display_name: "Tool", sort_order: 10, part_count: 3 },
        ]);
      }
      if (url.pathname === "/api/parameter-fields/base-units" && request.method === "GET") {
        return json(UNITS);
      }
      if (url.pathname === "/api/parameter-fields" && request.method === "GET") {
        return json(
          url.searchParams.get("category") === null
            ? []
            : [parameterField(), ESR_USED, MOUNTING],
        );
      }
      if (url.pathname === "/api/parameter-fields" && request.method === "POST") {
        if (options.createStatus === 409 && body["on_name_conflict"] === "fail") {
          return json(
            {
              detail: {
                reason: "duplicate_name",
                message: "a field named 'esr' already exists ('ESR at 100 kHz')",
                existing: parameterField({
                  id: 44,
                  name: "esr",
                  display_name: "ESR at 100 kHz",
                  base_unit: "ohm",
                  substitution_direction: "lower_ok",
                  applies_to_category: "passives",
                  is_seed: false,
                  value_count: 2,
                }),
              },
            },
            409,
          );
        }
        const reused = body["on_name_conflict"] === "reuse";
        return json(
          {
            field: parameterField({
              id: reused ? 44 : 99,
              name: body["name"],
              display_name: body["display_name"],
              base_unit: body["base_unit"],
              substitution_direction: body["substitution_direction"],
              applies_to_category: body["applies_to_category"],
              is_seed: false,
              value_count: 0,
            }),
            reused,
            replayed: false,
          },
          201,
        );
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}${url.search}`);
    }),
  );
}

function renderScreen(entry = "/part-types?category=capacitors"): void {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/part-types" element={<PartTypesScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

/**
 * Whether the rendered page says something, ignoring how it is split across
 * elements — several of these sentences deliberately embed a count or a `<strong>`
 * mid-phrase, which breaks `getByText`'s single-node match.
 */
function textSomewhere(pattern: RegExp): boolean {
  return pattern.test(document.body.textContent ?? "");
}

/** Fills in everything the API requires, leaving the name to the caller. */
function fillField(displayName: string, unit: string): void {
  fireEvent.change(screen.getByLabelText(/as it appears in the filter panel/i), {
    target: { value: displayName },
  });
  fireEvent.change(screen.getByLabelText(/what it measures/i), { target: { value: unit } });
  fireEvent.click(screen.getByLabelText(/smaller number is always acceptable/i));
}

beforeEach(() => {
  calls.length = 0;
  // Both columns rendered, rather than the mobile `<details>`: jsdom's matchMedia
  // is absent, so `useMediaQuery` answers false and the rail collapses. Opening it
  // is not what these tests are about.
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: true,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("PartTypesScreen", () => {
  it("says which of the two objects owns fields, so nobody has to guess", async () => {
    stubApi();
    renderScreen();
    await waitFor(() => expect(screen.getByText("Capacitance")).toBeTruthy());

    // The distinction, in the words the ADR settles it in.
    expect(textSomewhere(/a kind carries no fields/i)).toBe(true);
    expect(screen.getAllByText(/Fields on Capacitors/i).length).toBeGreaterThan(0);
  });

  it("marks an inherited field as inherited, since editing it changes every sibling", async () => {
    stubApi();
    renderScreen();
    await waitFor(() => expect(screen.getByText("Mounting")).toBeTruthy());

    expect(screen.getByRole("heading", { name: "Inherited" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Authored here" })).toBeTruthy();
  });

  it("authors a field with a quantity and a substitution rule, on the selected category", async () => {
    stubApi();
    renderScreen();
    await waitFor(() => expect(screen.getByText("Capacitance")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /add a field/i }));
    fillField("ESR", "ohm");
    fireEvent.click(screen.getByRole("button", { name: /create this field/i }));

    await waitFor(() =>
      expect(calls.some((call) => call.url === "/api/parameter-fields" && call.method === "POST")).toBe(
        true,
      ),
    );
    const posted = calls.find(
      (call) => call.url === "/api/parameter-fields" && call.method === "POST",
    );
    expect(posted?.body).toMatchObject({
      name: "esr",
      display_name: "ESR",
      value_type: "numeric",
      base_unit: "ohm",
      substitution_direction: "lower_ok",
      applies_to_category: "capacitors",
      on_name_conflict: "fail",
    });
    // Idempotency-guarded like every other create here: a doubled tap on bad wifi
    // must not file two fields.
    expect(typeof posted?.body["client_op_id"]).toBe("string");
  });

  it("cannot be submitted until the substitution question is answered", async () => {
    stubApi();
    renderScreen();
    await waitFor(() => expect(screen.getByText("Capacitance")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /add a field/i }));
    fireEvent.change(screen.getByLabelText(/as it appears in the filter panel/i), {
      target: { value: "ESR" },
    });
    fireEvent.change(screen.getByLabelText(/what it measures/i), { target: { value: "ohm" } });

    const submit = screen.getByRole("button", { name: /create this field/i });
    expect(submit.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText(/no safe default/i)).toBeTruthy();

    fireEvent.click(screen.getByLabelText(/smaller number is always acceptable/i));
    expect(screen.getByRole("button", { name: /create this field/i }).hasAttribute("disabled")).toBe(
      false,
    );
  });

  it("turns a name collision into a choice, with the existing field on screen", async () => {
    stubApi({ createStatus: 409 });
    renderScreen();
    await waitFor(() => expect(screen.getByText("Capacitance")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /add a field/i }));
    fillField("ESR", "ohm");
    fireEvent.click(screen.getByRole("button", { name: /create this field/i }));

    // The collision names the field in the way, what it measures and who uses it —
    // which is what makes "yes, that is the one I meant" answerable.
    await waitFor(() => expect(screen.getByText(/already exists/i)).toBeTruthy());
    expect(screen.getByText(/ESR at 100 kHz/)).toBeTruthy();
    expect(screen.getByText(/2 parts already use it/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /use the existing field/i }));

    await waitFor(() => expect(screen.getByText(/Reusing/)).toBeTruthy());
    const attempts = calls.filter(
      (call) => call.url === "/api/parameter-fields" && call.method === "POST",
    );
    expect(attempts.map((call) => call.body["on_name_conflict"])).toEqual(["fail", "reuse"]);
    // A different key for the retry: replaying the first would return its 409,
    // and reusing the same one is what makes a doubled tap safe.
    expect(attempts[0]?.body["client_op_id"]).not.toBe(attempts[1]?.body["client_op_id"]);
  });

  it("offers a separate name only when there is a category to name it after", async () => {
    stubApi({ createStatus: 409 });
    renderScreen("/part-types");
    await waitFor(() =>
      expect(screen.getAllByText(/fields every part has/i).length).toBeGreaterThan(0),
    );

    fireEvent.click(screen.getByRole("button", { name: /add a field/i }));
    fillField("ESR", "ohm");
    fireEvent.click(screen.getByRole("button", { name: /create this field/i }));

    await waitFor(() => expect(screen.getByText(/already exists/i)).toBeTruthy());
    // `namespace` names the field `<category>.<name>`, so with no category there is
    // nothing to name it after and the button would only produce a 422.
    expect(screen.queryByRole("button", { name: /keep mine separate/i })).toBeNull();
    expect(screen.getByText(/needs a category/i)).toBeTruthy();
  });

  it("does not offer to delete a field parts are using, and says which", async () => {
    stubApi();
    renderScreen();
    await waitFor(() => expect(screen.getByText("Capacitance")).toBeTruthy());

    // Three parts hold an ESR, and the FK is CASCADE: deleting the field would
    // delete every one of those values without asking. A shipped field says
    // something different again, because its refusal never lifts.
    expect(textSomewhere(/3 parts hold a value/)).toBe(true);
    // Both shipped fields say it — the refusal is per field, not a page banner.
    expect(screen.getAllByText(/shipped library — it cannot be deleted/).length).toBe(2);
    expect(screen.queryByRole("button", { name: /^Delete$/ })).toBeNull();
  });

  it("refuses to remove an option parts are filed under, and says how many", async () => {
    stubApi();
    renderScreen();
    await waitFor(() => expect(screen.getByText("Mounting")).toBeTruthy());

    // `parameter_value.choice_id` is RESTRICT, so an unguarded delete is an
    // IntegrityError the user reads as a 500 with no number in it.
    expect(screen.getByText(/22 parts filed under it/)).toBeTruthy();
    expect(screen.getByText(/8 parts filed under it/)).toBeTruthy();
  });
});
