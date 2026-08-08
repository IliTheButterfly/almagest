/**
 * The proposed-fields screen, exercised against a stubbed `fetch`.
 *
 * What is under test is the design problem the screen exists to solve, which is
 * **not** "can a field be created". It is:
 *
 * - a proposal that resembles an existing field has to *say so, before the create
 *   form*, and offer to merge — because a second `parameter_template` for one
 *   concept fragments parametric search with nothing to report it;
 * - the datasheet quote has to be on screen, since a proposal nobody can trace to
 *   a line of a document cannot be reviewed at all;
 * - **creating a field goes through `POST /api/parameter-fields`**, the route that
 *   already existed, and the proposal is only then recorded as what it became. A
 *   single call that did both would be a second door onto that write. The call log
 *   is asserted, in order, for exactly that reason;
 * - `substitution_direction` cannot be left unchosen, because a wrong one returns
 *   plausible substitutes rather than an error.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FieldProposalsScreen } from "./FieldProposalsScreen";

interface Call {
  readonly url: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

const calls: Call[] = [];

function proposal(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: 7,
    name: "Rds(on)",
    name_norm: "rdson",
    display_name: "On resistance Rds(on)",
    suggested_value_type: "numeric",
    suggested_base_unit: "ohm",
    suggested_category: "semiconductors",
    suggested_substitution_direction: null,
    status: "pending",
    seen_count: 3,
    nearest: null,
    created_template_id: null,
    decision_note: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    decided_at: null,
    evidence: [
      {
        id: 1,
        source: "llm_inferred",
        document_ref: "sha256:aaa",
        part_id: null,
        quote: "Static drain-source on-resistance Rds(on) ... 45 mOhm",
        stated_value: "45 mOhm",
        page: 2,
        note: null,
        provider: "local-vllm",
        model: "qwen3:27b",
        created_at: "2026-01-01T00:00:00Z",
      },
    ],
    ...overrides,
  };
}

const NEAREST = {
  template_id: 42,
  name: "on_resistance",
  display_name: "On resistance",
  value_type: "numeric",
  base_unit: "ohm",
  match: "synonym",
  score: 0.9,
};

function stubApi(rows: readonly unknown[]): void {
  // Stateful in one respect only: a decision empties the queue, which is what the
  // server does — a decided proposal leaves the pending list. A stub that kept
  // returning the row would let a test pass while the screen failed to reload.
  let decided = false;

  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      calls.push({
        url: url.pathname + url.search,
        method: request.method,
        body: raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>),
      });

      const json = (body: unknown, status = 200): Response =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === "/api/field-proposals" && request.method === "GET") {
        return json({ proposals: decided ? [] : rows });
      }
      if (url.pathname === "/api/parameter-fields/base-units") {
        return json([
          { name: "ohm", symbol: "Ω" },
          { name: "volt", symbol: "V" },
        ]);
      }
      if (url.pathname === "/api/parameter-fields" && request.method === "POST") {
        return json({ field: { id: 99, name: "on_resistance" }, reused: false }, 201);
      }
      if (url.pathname.match(/^\/api\/field-proposals\/\d+\/decision$/)) {
        decided = true;
        return json({ proposal: { ...proposal(), status: "accepted" } });
      }
      return json({ detail: "unexpected" }, 500);
    }),
  );
}

describe("FieldProposalsScreen", () => {
  beforeEach(() => {
    calls.length = 0;
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function mount() {
    return render(
      <MemoryRouter>
        <FieldProposalsScreen />
      </MemoryRouter>,
    );
  }

  it("shows the datasheet quote and how many sheets stated it", async () => {
    stubApi([proposal()]);
    mount();
    expect(await screen.findByText(/Static drain-source on-resistance/)).toBeTruthy();
    expect(screen.getByText("3 datasheets")).toBeTruthy();
  });

  it("warns that an existing field may already be this one, and offers to merge", async () => {
    stubApi([proposal({ nearest: NEAREST })]);
    mount();
    expect(await screen.findByText(/You may already have this: on_resistance/)).toBeTruthy();
    expect(screen.getByText(/a known other name for the same quantity/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Merge into on_resistance/ })).toBeTruthy();
  });

  it("merging records the existing field and creates nothing", async () => {
    stubApi([proposal({ nearest: NEAREST })]);
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /Merge into on_resistance/ }));

    await waitFor(() => {
      expect(calls.some((call) => call.url.endsWith("/decision"))).toBe(true);
    });
    const decision = calls.find((call) => call.url.endsWith("/decision"));
    expect(decision?.body).toMatchObject({ status: "accepted", field_id: 42 });
    // The whole point: no field was authored. A merge that posted to
    // /api/parameter-fields would be creating the duplicate it exists to avoid.
    expect(calls.some((call) => call.url === "/api/parameter-fields")).toBe(false);
  });

  it("says plainly when nothing resembles the proposal", async () => {
    stubApi([proposal()]);
    mount();
    expect(await screen.findByText(/Nothing you already have resembles this/)).toBeTruthy();
  });

  it("will not create a field until a substitution direction is chosen", async () => {
    stubApi([proposal()]);
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /Create this field/ }));

    const submit = screen.getByRole("button", { name: "Create field" });
    expect(submit.hasAttribute("disabled")).toBe(true);

    fireEvent.change(screen.getByLabelText(/When looking for a substitute/), {
      target: { value: "lower_ok" },
    });
    expect(screen.getByRole("button", { name: "Create field" }).hasAttribute("disabled")).toBe(
      false,
    );
  });

  it("creates through the authoring route first, then records the decision", async () => {
    stubApi([proposal()]);
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /Create this field/ }));
    fireEvent.change(screen.getByLabelText(/When looking for a substitute/), {
      target: { value: "lower_ok" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create field" }));

    await waitFor(() => {
      expect(calls.some((call) => call.url.endsWith("/decision"))).toBe(true);
    });
    const writes = calls.filter((call) => call.method === "POST").map((call) => call.url);
    // Order matters: the field has to exist before the proposal can name it, and
    // the decision route refuses `accepted` without a field that resolves.
    expect(writes[0]).toBe("/api/parameter-fields");
    expect(writes[1]).toMatch(/\/decision$/);

    const created = calls.find((call) => call.url === "/api/parameter-fields");
    expect(created?.body).toMatchObject({
      name: "Rds(on)",
      value_type: "numeric",
      base_unit: "ohm",
      substitution_direction: "lower_ok",
      applies_to_category: "semiconductors",
      // The honest answer to a taken name is that the field exists, so the screen
      // does not quietly namespace one.
      on_name_conflict: "fail",
    });
    expect(calls.find((call) => call.url.endsWith("/decision"))?.body).toMatchObject({
      status: "accepted",
      field_id: 99,
    });
  });

  it("dismissing keeps nothing to click and says the row survives", async () => {
    stubApi([proposal()]);
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Not a field" }));
    await waitFor(() => {
      expect(calls.find((call) => call.url.endsWith("/decision"))?.body).toMatchObject({
        status: "dismissed",
      });
    });
    expect(await screen.findByText(/will not be proposed again/)).toBeTruthy();
  });
});
