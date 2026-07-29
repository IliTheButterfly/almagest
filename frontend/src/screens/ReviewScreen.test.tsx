/**
 * The review queue screen, exercised against a stubbed `fetch`.
 *
 * What is under test is the design problem the screen exists to solve: an
 * item's evidence has to actually be on screen (not just its confidence
 * score), a disagreement has to show both sides rather than pick a winner for
 * the reviewer, correcting a value has to leave the record of what the
 * original source said, and bulk accept has to touch only what was selected.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReviewScreen } from "./ReviewScreen";

function candidate(overrides: Partial<Record<string, unknown>>) {
  return {
    id: 1,
    source: "llm_inferred",
    source_ref: "sha256:" + "a".repeat(64),
    confidence: 0.6,
    raw_value: "100nF",
    choice_key: null,
    status: "pending",
    review_reason: "low_confidence",
    requires_human: false,
    note: 'vendor/model-x read "100nF ±10%, page 5" (self-reported confidence 0.60)',
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function field(overrides: Partial<Record<string, unknown>>) {
  return {
    template_id: 10,
    template_name: "capacitance",
    template_unit: "F",
    existing_raw_input: null,
    existing_provenance: null,
    existing_confidence: null,
    recommended_candidate_id: null,
    candidates: [candidate({})],
    ...overrides,
  };
}

function part(overrides: Partial<Record<string, unknown>>) {
  return {
    part_id: 1,
    part_name: "test part",
    part_mpn: "GRM188R71H104KA93D",
    fields: [field({})],
    ...overrides,
  };
}

interface Call {
  readonly url: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

const calls: Call[] = [];

function stubApi(options: { queue?: readonly unknown[] } = {}): void {
  // A single accept/correct in these fixtures always closes the only field the
  // only part has, so a reload afterward has nothing left — this stub models
  // that one fact statefully rather than pretending the GET is unaffected by
  // the writes, which is what the server's route docstring guarantees.
  let closed = false;

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

      const json = (body: unknown): Response =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === "/api/enrichment/candidates" && request.method === "GET") {
        const parts = closed
          ? []
          : ((options.queue ?? [part({})]) as { fields: { candidates: unknown[] }[] }[]);
        const totalCandidates = parts.reduce(
          (sum: number, p) => sum + p.fields.reduce((s: number, f) => s + f.candidates.length, 0),
          0,
        );
        return json({ total_candidates: totalCandidates, total_parts: parts.length, parts });
      }
      if (
        url.pathname.match(/^\/api\/enrichment\/candidates\/\d+\/accept$/) &&
        request.method === "POST"
      ) {
        closed = true;
        return json({
          template_id: 10,
          template_name: "capacitance",
          template_unit: "F",
          existing_raw_input: "100nF",
          existing_provenance: "llm_inferred",
          existing_confidence: 0.6,
          recommended_candidate_id: null,
          candidates: [],
        });
      }
      if (
        url.pathname.match(/^\/api\/enrichment\/candidates\/\d+\/correct$/) &&
        request.method === "POST"
      ) {
        closed = true;
        return json({
          template_id: 10,
          template_name: "capacitance",
          template_unit: "F",
          existing_raw_input: "4.7uF",
          existing_provenance: "manual",
          existing_confidence: 1.0,
          recommended_candidate_id: null,
          candidates: [],
        });
      }
      if (
        url.pathname.match(/^\/api\/enrichment\/candidates\/\d+\/dismiss$/) &&
        request.method === "POST"
      ) {
        return json(candidate({ status: "dismissed" }));
      }
      if (url.pathname === "/api/enrichment/candidates/bulk-accept" && request.method === "POST") {
        const body = JSON.parse(raw) as { candidate_ids: number[] };
        return json({
          results: body.candidate_ids.map((id) => ({ candidate_id: id, accepted: true })),
        });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}${url.search}`);
    }),
  );
}

function renderScreen() {
  return render(
    <MemoryRouter initialEntries={["/review"]}>
      <Routes>
        <Route path="/review" element={<ReviewScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("an item's evidence", () => {
  it("shows the candidate's source and the text it was read from", async () => {
    stubApi();
    renderScreen();

    expect(await screen.findByText("Model reading")).toBeTruthy();
    expect(screen.getByText(/100nF ±10%, page 5/)).toBeTruthy();
    expect(screen.getByText("100nF")).toBeTruthy();
  });

  it("flags a candidate with no recorded evidence rather than presenting it as trustworthy", async () => {
    stubApi({
      queue: [part({ fields: [field({ candidates: [candidate({ note: null })] })] })],
    });
    renderScreen();

    expect(
      await screen.findByText(/No evidence recorded for this reading/),
    ).toBeTruthy();
  });

  it("marks a value that must never auto-promote, even though this queue lets a human accept it", async () => {
    stubApi({
      queue: [
        part({
          fields: [
            field({
              candidates: [
                candidate({ source: "mpn_decoder", requires_human: true, review_reason: "requires_human" }),
              ],
            }),
          ],
        }),
      ],
    });
    renderScreen();

    expect(await screen.findByText(/needs a human/)).toBeTruthy();
  });
});

/**
 * Phase 6 review regression, the frontend half of
 * `backend/tests/integration/test_phase6_review_findings.py`.
 *
 * The server used to accept a `>=50V` candidate and store it as
 * `(value_min=50, value_max=NULL)`, which matches no range query at all. It now
 * refuses with `reason: "one_sided_limit"`, so the screen must stop offering the
 * button that gets refused — and must name *which* refusal it is, because the
 * two are fixed differently: `unparseable` is a grammar gap to report, a
 * one-sided limit is a two-sided value to type.
 */
describe("a candidate the server will refuse to accept", () => {
  it.each([
    ["unparseable", "could not be parsed"],
    ["one_sided_limit", "a limit, not a value"],
  ])("hides Accept and says why, for %s", async (reason, badge) => {
    stubApi({
      queue: [
        part({
          fields: [
            field({
              candidates: [candidate({ review_reason: reason, raw_value: ">=50V" })],
            }),
          ],
        }),
      ],
    });
    renderScreen();

    expect(await screen.findByText(badge)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    // No bulk-select box either: bulk accept would report the same refusal, one
    // id at a time, for a row the reviewer was invited to tick.
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
    // Correct is the way out, and it is still offered.
    expect(screen.getByRole("button", { name: "Correct" })).toBeTruthy();
  });

  it("still offers Accept for a reason that only a rule refused", async () => {
    // `requires_human` and `low_confidence` are refusals by the *promotion
    // rules*, not by the write path: a human accepting one is exactly what this
    // queue is for, so hiding Accept there would break the screen's whole job.
    stubApi({
      queue: [
        part({
          fields: [
            field({
              candidates: [candidate({ requires_human: true, review_reason: "requires_human" })],
            }),
          ],
        }),
      ],
    });
    renderScreen();

    expect(await screen.findByRole("button", { name: "Accept" })).toBeTruthy();
  });
});

describe("a disagreement", () => {
  it("shows both values and both sources, and marks which one the priority order would pick", async () => {
    stubApi({
      queue: [
        part({
          fields: [
            field({
              recommended_candidate_id: 2,
              candidates: [
                candidate({ id: 2, source: "mpn_decoder", raw_value: "4.7uF", confidence: 0.9 }),
                candidate({ id: 3, source: "llm_inferred", raw_value: "10uF", confidence: 0.94 }),
              ],
            }),
          ],
        }),
      ],
    });
    renderScreen();

    expect(await screen.findByText("disagreement")).toBeTruthy();
    // Both readings are on screen — the losing one is not hidden.
    expect(screen.getByText("4.7uF")).toBeTruthy();
    expect(screen.getByText("10uF")).toBeTruthy();
    expect(screen.getByText("Part-number decoder")).toBeTruthy();
    expect(screen.getByText("Model reading")).toBeTruthy();
    // Exactly one row carries the priority order's pick.
    expect(screen.getAllByText("priority pick")).toHaveLength(1);
  });
});

describe("correcting a value", () => {
  it("submits the correction, which the server records as a fresh manual candidate", async () => {
    stubApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Correct" }));
    fireEvent.change(screen.getByDisplayValue("100nF"), { target: { value: "4.7uF" } });
    fireEvent.change(screen.getByPlaceholderText(/checked the physical part/), {
      target: { value: "checked the part, it reads 475" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save correction" }));

    await waitFor(() =>
      expect(calls.some((call) => call.url === "/api/enrichment/candidates/1/correct")).toBe(true),
    );
    const post = calls.find((call) => call.url === "/api/enrichment/candidates/1/correct");
    expect(post?.body["raw_value"]).toBe("4.7uF");
    expect(post?.body["note"]).toBe("checked the part, it reads 475");

    // Correcting closes the whole field server-side (see the route's
    // docstring), so the follow-up reload finds nothing left to show.
    expect(
      await screen.findByText(/Nothing is waiting on a human right now/),
    ).toBeTruthy();
  });
});

describe("accepting one candidate", () => {
  it("sends an accept for that candidate alone", async () => {
    stubApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Accept" }));

    await waitFor(() =>
      expect(calls.some((call) => call.url === "/api/enrichment/candidates/1/accept")).toBe(true),
    );
  });
});

describe("bulk accept", () => {
  it("applies only to the selected set, not the whole queue", async () => {
    stubApi({
      queue: [
        part({
          part_id: 1,
          fields: [field({ candidates: [candidate({ id: 1 })] })],
        }),
        part({
          part_id: 2,
          part_name: "second part",
          fields: [field({ template_id: 20, candidates: [candidate({ id: 2 })] })],
        }),
      ],
    });
    renderScreen();

    const checkboxes = await screen.findAllByRole("checkbox");
    expect(checkboxes).toHaveLength(2);
    const [first] = checkboxes;
    if (first === undefined) {
      throw new Error("expected a checkbox");
    }
    fireEvent.click(first);

    fireEvent.click(await screen.findByRole("button", { name: "Accept 1 selected" }));

    await waitFor(() =>
      expect(calls.some((call) => call.url === "/api/enrichment/candidates/bulk-accept")).toBe(
        true,
      ),
    );
    const post = calls.find((call) => call.url === "/api/enrichment/candidates/bulk-accept");
    expect(post?.body["candidate_ids"]).toEqual([1]);
  });
});
