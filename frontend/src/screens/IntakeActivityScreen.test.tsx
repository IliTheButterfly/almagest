/**
 * The per-entry activity screen.
 *
 * What these tests guard is the *wording*, because that is where a diagnostic screen
 * actually fails. The payload can be perfectly correct and the screen can still print a
 * missing token count as `0`, render an empty section that reads as a failure, or show
 * the model's own self-reported confidence as though it were the stored value.
 *
 * Specifically:
 *
 * * a null count says **"not recorded"** and never `0` — `CallStats`' rule kept intact
 *   at the last step, where a zero would read as "the prompt was empty";
 * * where nothing has run the screen **says so in words** rather than drawing an empty
 *   list, because an empty list reads as breakage;
 * * the prompt and the raw response are behind `<details>`, and the raw response is
 *   labelled as the model's own claim so it cannot be confused with the clamped stored
 *   confidence beside the candidate;
 * * `finish_reason: length` is called out as a budget, not a broken model.
 */

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { IntakeActivityRead, ModelRunRead } from "../lib/api/client";
import { IntakeActivityScreen } from "./IntakeActivityScreen";

function run(overrides: Partial<ModelRunRead> = {}): ModelRunRead {
  return {
    id: 1,
    kind: "vision",
    provider: "local-ollama",
    model: "qwen3-vl:8b",
    intake_id: 1,
    document_sha256: "a".repeat(64),
    started_at: "2026-08-07T13:57:00Z",
    finished_at: "2026-08-07T13:57:53Z",
    latency_ms: 53_200,
    prompt_tokens: 3084,
    completion_tokens: 41,
    finish_reason: "stop",
    request_json: '{"messages":[{"images":[{"image_sha256":"aaa"}]}]}',
    response_text: '{"candidates":[{"mpn":"CF14JT100K","confidence":0.95}]}',
    error: null,
    truncated: false,
    ...overrides,
  };
}

function activity(overrides: Partial<IntakeActivityRead> = {}): IntakeActivityRead {
  return {
    entry: {
      id: 1,
      client_op_id: "op-1",
      raw_payload: "capture:abc",
      symbology: null,
      decoded_kind: null,
      mpn: null,
      status: "pending",
      device_id: null,
      note: null,
      queued_at: null,
      created_at: "2026-08-07T16:19:00Z",
      resolved_at: null,
      resolved_part_id: null,
    },
    capture: {
      id: 7,
      created_at: "2026-08-07T16:19:00Z",
      document_sha256: "a".repeat(64),
      width_px: 1600,
      height_px: 1200,
      text_status: "ok",
      regions: [
        {
          kind: "text",
          text: "CFI4JT100K",
          symbology: null,
          confidence: null,
          order_index: 0,
        },
      ],
    },
    dispatch: {
      state: "not_requested",
      attempts: 0,
      error: null,
      label_kind: null,
      max_attempts: 2,
    },
    model_runs: [],
    identity_candidates: [],
    resolved_part: null,
    ...overrides,
  };
}

/**
 * The real client against a stubbed `fetch`, as `ReviewScreen.test.tsx` does.
 *
 * Mocking `../lib/api/client` instead would also mock the module `IntakeQueueScreen`
 * imports — this screen borrows `headlineFor` from it — and would leave that module's
 * own imports undefined for reasons nothing in this file explains. The generated client
 * is worth exercising anyway: it is what turns a route path into a typed read.
 */
function draw(data: IntakeActivityRead) {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(data), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    ),
  );
  return render(
    <MemoryRouter initialEntries={["/intake/1/activity"]}>
      <Routes>
        <Route path="/intake/:entryId/activity" element={<IntakeActivityScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("IntakeActivityScreen", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("says no worker has run rather than drawing an empty list", async () => {
    draw(activity());

    expect(await screen.findByText(/No worker has run/i)).toBeTruthy();
    // The reason, not just the absence: dispatch is opt-in because it costs the card.
    expect(screen.getByText(/costs the graphics card/i)).toBeTruthy();
    expect(screen.getByText(/Nobody has chosen yet/i)).toBeTruthy();
  });

  it("prints a missing count as 'not recorded' and never as zero", async () => {
    draw(
      activity({
        model_runs: [
          run({ prompt_tokens: null, completion_tokens: null, latency_ms: null, finish_reason: null }),
        ],
      }),
    );

    const line = await screen.findByText(/prompt \/.*completion tokens/i);
    expect(line.textContent).toContain("not recorded");
    // The specific failure being guarded: a zero here would read as an empty prompt,
    // and would pull any average over these rows toward whichever servers were quiet.
    expect(line.textContent).not.toMatch(/\b0 prompt\b/);
  });

  it("keeps the prompt and the raw answer behind a disclosure", async () => {
    draw(activity({ model_runs: [run()] }));

    expect(await screen.findByText(/Show the prompt/i)).toBeTruthy();
    expect(screen.getByText(/Show the raw response/i)).toBeTruthy();
    // Open in the DOM either way — `<details>` renders its children — so the content is
    // assertable without a click. What matters is that it is *inside* a disclosure.
    expect(screen.getByText(/image_sha256/)).toBeTruthy();
  });

  it("labels a self-reported confidence in the transcript as the model's own claim", async () => {
    draw(
      activity({
        model_runs: [run()],
        identity_candidates: [
          {
            mpn: "CF14JT100K",
            manufacturer: "Stackpole",
            package: "axial",
            confidence: 0.79,
            source_text: "MFR PART NO: CF14JT100K",
            note: null,
            part_id: 42,
            rank: 0,
            provider: "local-ollama",
            model: "qwen3-vl:8b",
            created_at: "2026-08-07T13:57:53Z",
          },
        ],
      }),
    );

    // The stored number, said as stored and clamped.
    expect(await screen.findByText(/stored confidence 0\.79 \(clamped/i)).toBeTruthy();
    // And the transcript's own number disclaimed, so the 0.95 inside the raw response
    // cannot be read as the value the system acted on.
    expect(screen.getByText(/own claim about itself/i)).toBeTruthy();
  });

  it("calls a length finish a budget rather than a broken model", async () => {
    draw(activity({ model_runs: [run({ finish_reason: "length", response_text: "…" })] }));

    expect(await screen.findByText(/It ran out of room/i)).toBeTruthy();
    expect(screen.getByText(/budget set too low/i)).toBeTruthy();
  });

  it("records a broken run with what broke and no invented transcript", async () => {
    draw(
      activity({
        dispatch: {
          state: "failed",
          attempts: 2,
          error: "ModelUnavailable: connection refused",
          label_kind: null,
          max_attempts: 2,
        },
        model_runs: [
          run({
            error: "ModelUnavailable: connection refused",
            response_text: null,
            request_json: null,
          }),
        ],
      }),
    );

    expect(await screen.findByText(/What broke/i)).toBeTruthy();
    expect(screen.getByText(/The prompt was not recorded/i)).toBeTruthy();
    expect(screen.getByText(/No completion came back at all/i)).toBeTruthy();
  });

  it("says a datasheet that is stored but unread is normal", async () => {
    draw(
      activity({
        entry: { ...activity().entry, resolved_part_id: 42, status: "resolved" },
        resolved_part: {
          id: 42,
          name: "CF14JT100K",
          mpn: "CF14JT100K",
          is_stub: true,
          research_state: "resolved",
          research_attempts: 1,
          research_error: null,
          research_candidates: [],
          documents: [
            {
              sha256: "b".repeat(64),
              media_type: "application/pdf",
              byte_size: 1024,
              extraction_state: "pending",
              extraction_attempts: 0,
              extraction_error: null,
            },
          ],
          field_candidates: [],
        },
      }),
    );

    expect(await screen.findByText(/That is normal, not a\s*failure/i)).toBeTruthy();
  });
});
