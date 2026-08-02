/**
 * Editing a part's field values.
 *
 * Asserted here is the thing that was missing rather than the rendering: that a
 * value can be typed at all, that the shorthand goes to the server *as typed* (the
 * grammar is the server's, and a client that pre-parsed it would be a second
 * grammar free to disagree), that a multi-valued field offers ticks rather than a
 * select, and that the server's refusal lands against the field that caused it —
 * `1M` under capacitance is the most useful error this system produces.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PartFields } from "./PartFields";

interface Call {
  readonly url: string;
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

function field(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    template_id: 1,
    name: "capacitance",
    display_name: "Capacitance",
    value_type: "numeric",
    base_unit: "farad",
    allow_multiple: false,
    inherited: false,
    sort_order: 10,
    raw_input: null,
    value_nominal: null,
    value_min: null,
    value_max: null,
    display: null,
    value_text: null,
    value_bool: null,
    choices: [],
    provenance: null,
    options: [],
    ...overrides,
  };
}

const INTERFACE = field({
  template_id: 2,
  name: "interface",
  display_name: "Interface",
  value_type: "enum",
  base_unit: null,
  allow_multiple: true,
  options: [
    { id: 11, key: "i2c", label: "I²C" },
    { id: 12, key: "spi", label: "SPI" },
  ],
});

function stubApi(options: { filed?: boolean; refuse?: boolean } = {}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      const body = raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>);
      calls.push({ url: url.pathname, method: request.method, body });

      if (url.pathname === "/api/parts/7/parameters" && request.method === "GET") {
        return json({
          part_id: 7,
          category: options.filed === false ? null : "capacitor",
          filed: options.filed !== false,
          parameters: [field(), INTERFACE, field({ template_id: 3, name: "package", display_name: "Package", value_type: "text", base_unit: null, inherited: true })],
        });
      }
      if (url.pathname.startsWith("/api/parts/7/parameters/") && request.method === "PUT") {
        if (options.refuse === true) {
          return json(
            {
              detail: {
                reason: "implausible",
                template: "capacitance",
                message:
                  "1 is outside the plausible range for capacitance [1e-12, 1]: megafarads are not a thing",
              },
            },
            422,
          );
        }
        return json({ parameter: field({ raw_input: String(body["value"] ?? "") }) });
      }
      if (url.pathname.startsWith("/api/parts/7/parameters/") && request.method === "DELETE") {
        return json({ template_id: 1, name: "capacitance", removed: true });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderFields(): void {
  render(
    <MemoryRouter>
      <PartFields partId={7} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("PartFields", () => {
  it("sends the shorthand exactly as typed, for the server's grammar to parse", async () => {
    stubApi();
    renderFields();
    await waitFor(() => expect(screen.getByText("Capacitance")).toBeTruthy());

    fireEvent.change(screen.getByLabelText(/shorthand works/i), { target: { value: "22uF" } });
    fireEvent.click(screen.getAllByRole("button", { name: /record it/i })[0]!);

    await waitFor(() =>
      expect(calls.some((call) => call.method === "PUT")).toBe(true),
    );
    const put = calls.find((call) => call.method === "PUT");
    // Not 2.2e-5: the grammar belongs to the server, and a client that parsed it
    // here would be a second grammar free to disagree with the one search uses.
    expect(put?.url).toBe("/api/parts/7/parameters/capacitance");
    expect(put?.body).toEqual({ value: "22uF" });
  });

  it("offers ticks for a field that holds several options, and sends the whole set", async () => {
    stubApi();
    renderFields();
    await waitFor(() => expect(screen.getByText("Interface")).toBeTruthy());

    expect(screen.getByText(/can hold several at once/i)).toBeTruthy();
    fireEvent.click(screen.getByLabelText(/I²C/));
    fireEvent.click(screen.getByLabelText(/SPI/));
    fireEvent.click(screen.getAllByRole("button", { name: /record it/i })[1]!);

    await waitFor(() => expect(calls.some((call) => call.method === "PUT")).toBe(true));
    const put = calls.find((call) => call.method === "PUT");
    expect(put?.url).toBe("/api/parts/7/parameters/interface");
    expect(put?.body).toEqual({ choices: ["i2c", "spi"] });
  });

  it("puts the server's refusal against the field that caused it", async () => {
    stubApi({ refuse: true });
    renderFields();
    await waitFor(() => expect(screen.getByText("Capacitance")).toBeTruthy());

    fireEvent.change(screen.getByLabelText(/shorthand works/i), { target: { value: "1M" } });
    fireEvent.click(screen.getAllByRole("button", { name: /record it/i })[0]!);

    // The wording that makes this the most useful error in the system.
    await waitFor(() => expect(screen.getByText(/megafarads are not a thing/i)).toBeTruthy());
  });

  it("says why an unfiled part has almost no fields", async () => {
    stubApi({ filed: false });
    renderFields();
    await waitFor(() =>
      expect(screen.getByText(/not filed under a category/i)).toBeTruthy(),
    );
    expect(screen.getByText(/reaches a part through the category it sits in/i)).toBeTruthy();
  });

  it("separates the fields every part of this sort has", async () => {
    stubApi();
    renderFields();
    await waitFor(() => expect(screen.getByText("Capacitance")).toBeTruthy());
    expect(screen.getByText(/1 field every part of this sort has/i)).toBeTruthy();
  });
});

describe("a parsed value is shown as it was parsed", () => {
  it("marks the confirmation badge as a value, so it is not uppercased", async () => {
    // Not a style assertion for its own sake: `.badge` uppercases, and this is
    // the badge that tells a person their "22u" became 22 μF. Uppercased it
    // reads 22 MF — off by twelve orders of magnitude, on the one control whose
    // job is to confirm the value was understood. `styles.tokens.test.ts` pins
    // the CSS half; this pins that the class reaches the element.
    //
    // Its own stub, because the shared one returns every field unrecorded and
    // the badge only exists once there is a value to confirm.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json({
          part_id: 7,
          category: "capacitor",
          filed: true,
          parameters: [field({ display: "22 \u03bcF", raw_input: "22uF" })],
        }),
      ),
    );
    renderFields();

    const badge = await waitFor(() => {
      const found = document.querySelector(".badge-good");
      if (found === null) {
        throw new Error("no confirmation badge rendered");
      }
      return found;
    });
    expect(badge.textContent).toBe("22 \u03bcF");
    expect(badge.className).toContain("badge-value");
  });
});
