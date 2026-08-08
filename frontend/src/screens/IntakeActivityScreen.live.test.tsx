/**
 * Watching a read happen, and — the part that matters — stopping.
 *
 * ## What these tests guard
 *
 * **The polling not stopping.** This is the one that cannot be caught by looking at the
 * screen: a finished timeline that keeps refreshing looks exactly like one that does
 * not, and the cost lands on the single SQLite writer that also serves every scan and
 * every put-away. So the fetches are *counted*, with fake timers, in both directions:
 * a run in flight must produce more of them over time and a finished one must produce
 * none. Deleting the `inFlight === 0` guard turns the second of these red and nothing
 * else in the suite would notice.
 *
 * **An open call rendered as a broken one.** Every cost field is null while a call is
 * open, and a row of "not recorded" reads as breakage rather than as an answer that has
 * not come back. The badge must say neither "answered" nor "broke", because it is
 * neither yet.
 *
 * **The partial being hidden.** A finished run keeps its transcript behind a
 * disclosure, which is right — it is long and it is for when something looks wrong. A
 * running one must show it *open*, or the loading card says nothing more than a spinner
 * does, and the forty seconds of reasoning ADR 0021 measured stays invisible.
 *
 * **The chat button promising too much.** It must say the model cannot see the
 * photograph, and that the conversation cannot change what the scan is recorded as.
 * Somebody who believes either of those is false will trust the wrong kind of answer.
 */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { IntakeActivityRead, ModelRunRead } from "../lib/api/client";
import { IntakeActivityScreen } from "./IntakeActivityScreen";

const STARTED = "2026-08-07T13:57:00Z";

function run(overrides: Partial<ModelRunRead> = {}): ModelRunRead {
  return {
    id: 1,
    kind: "vision",
    provider: "local-ollama",
    model: "qwen3-vl:8b",
    intake_id: 1,
    document_sha256: "a".repeat(64),
    started_at: STARTED,
    finished_at: null,
    latency_ms: null,
    prompt_tokens: null,
    completion_tokens: null,
    finish_reason: null,
    request_json: '{"messages":[{"images":[{"image_sha256":"aaa"}]}]}',
    response_text: null,
    error: null,
    truncated: false,
    in_flight: true,
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
    capture: null,
    dispatch: {
      state: "pending",
      attempts: 1,
      error: null,
      label_kind: null,
      max_attempts: 2,
    },
    model_runs: [],
    identity_candidates: [],
    resolved_part: null,
    runs_in_flight: 0,
    ...overrides,
  };
}

/**
 * A `fetch` stub that separates the two calls this screen makes, **by path**.
 *
 * By path rather than by method, deliberately: `openapi-fetch` builds a `Request` and
 * hands it over as the sole argument, so `init.method` is undefined and a method-based
 * split silently classifies every call as a GET — which is how the first draft of this
 * file counted zero POSTs while the navigation it was checking had plainly happened.
 * The URL is the one thing available in both call shapes.
 */
function stubFetch(data: IntakeActivityRead, postBody: unknown = { thread_id: 42, created: true }) {
  const gets: string[] = [];
  const posts: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown) => {
      const url = typeof input === "string" ? input : String((input as Request).url ?? input);
      const isChat = url.includes("/chat");
      (isChat ? posts : gets).push(url);
      return new Response(JSON.stringify(isChat ? postBody : data), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return { gets, posts };
}

function draw() {
  return render(
    <MemoryRouter initialEntries={["/intake/1/activity"]}>
      <Routes>
        <Route path="/intake/:entryId/activity" element={<IntakeActivityScreen />} />
        <Route path="/chat/:threadId" element={<div>the conversation</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("IntakeActivityScreen, live", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("says a model is reading and does not call it answered or broken", async () => {
    stubFetch(activity({ model_runs: [run()], runs_in_flight: 1 }));

    draw();

    expect(await screen.findByText(/A model is reading this now/i)).toBeTruthy();
    expect(screen.getByText(/refreshing itself about once a second/i)).toBeTruthy();
    expect(screen.getByText(/reading…/i)).toBeTruthy();
    // Neither verdict, because there is no verdict yet.
    expect(screen.queryByText("answered")).toBeNull();
    expect(screen.queryByText("broke")).toBeNull();
    // And the wall of nulls is explained rather than left looking like breakage.
    expect(screen.getByText(/the cost is unknown until the model finishes/i)).toBeTruthy();
  });

  it("shows the partial answer open rather than behind a disclosure", async () => {
    const partial = "[reasoning, no answer yet]\nthe third digit is creased";
    stubFetch(activity({ model_runs: [run({ response_text: partial })], runs_in_flight: 1 }));

    draw();

    expect(await screen.findByText(/the third digit is creased/)).toBeTruthy();
    expect(screen.getByText(/updated about once a second/i)).toBeTruthy();
    // The finished-run affordance is absent: hiding a live transcript behind a click
    // leaves the loading card saying no more than a spinner.
    expect(screen.queryByText(/Show the raw response/i)).toBeNull();
  });

  /**
   * How long the two polling tests below watch, in **real** time.
   *
   * Real timers, deliberately, and this cost two wrong turns worth recording:
   *
   * 1. Installing fake timers *after* the render left the polling interval scheduled on
   *    the real clock, so `advanceTimersByTimeAsync` moved a clock nothing was listening
   *    to. Both directions then "passed" — including against a build with the
   *    `inFlight === 0` guard deleted, which is exactly the regression these two tests
   *    exist to catch.
   * 2. Installing them *before* the render did not work either: the interval and the
   *    fetch promises interleave through `act`, and ten simulated seconds produced one
   *    poll rather than ten. The mutation was caught, but the healthy case failed too,
   *    which is a test that cannot distinguish the thing it is measuring.
   *
   * So both wait real time, and they are asymmetric on purpose: the "it polls" direction
   * *waits for* a poll (a rate assertion starved below one under a full parallel suite),
   * while the "it stopped" direction counts over a fixed window, because zero is the
   * assertion with the sharp edge and it cannot be reached by waiting.
   *
   * **A green test that cannot fail is worse than no test**, and the leak these guard
   * against is invisible on screen by construction — which is why the seconds are worth
   * paying.
   */
  const WATCH_MS = 2_500;

  async function pollsWhileWatching(data: IntakeActivityRead, settled: RegExp): Promise<number> {
    const { gets } = stubFetch(data);
    draw();
    await waitFor(() => expect(screen.queryByText(settled)).not.toBeNull());

    const before = gets.length;
    // `act` because each poll lands state updates: without it React warns, and a warning
    // here would train the next reader to ignore the one that matters.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, WATCH_MS));
    });
    return gets.length - before;
  }

  it("keeps refreshing while a run is in flight", async () => {
    // **Waits for a poll rather than counting over a fixed window**, and that is a
    // flakiness fix rather than a stylistic one: how many `act`-wrapped ticks land inside
    // a real 2.5 s window depends on how jsdom's timers interleave with React's flushing,
    // and under a full parallel suite that starved below one. The property under test is
    // "it refreshes itself at all"; the sharp-edged assertion is its counterpart below.
    const { gets } = stubFetch(activity({ model_runs: [run()], runs_in_flight: 1 }));
    draw();
    await waitFor(() => expect(screen.queryByText(/A model is reading this now/i)).not.toBeNull());

    const before = gets.length;
    await waitFor(() => expect(gets.length).toBeGreaterThan(before), { timeout: 6_000 });
  });

  it("stops refreshing once nothing is in flight", async () => {
    // **The leak test.** A finished timeline that kept polling would look identical on
    // screen; the only symptom is requests against the one writer, so they are counted.
    const finished = run({
      finished_at: "2026-08-07T13:57:53Z",
      in_flight: false,
      latency_ms: 53_200,
      response_text: '{"candidates":[]}',
    });

    const polls = await pollsWhileWatching(
      activity({ model_runs: [finished], runs_in_flight: 0 }),
      /answered/,
    );

    expect(polls).toBe(0);
    expect(screen.queryByText(/A model is reading this now/i)).toBeNull();
  });

  it("offers a conversation about the scan, and says what it cannot do", async () => {
    stubFetch(activity());

    draw();
    const button = await screen.findByRole("button", { name: /Ask about this scan/i });

    // The two honest limits, both on screen before anything is clicked.
    expect(screen.getByText(/cannot change what this scan is recorded as/i)).toBeTruthy();
    expect(screen.getByText(/cannot see the photograph/i)).toBeTruthy();

    fireEvent.click(button);

    expect(await screen.findByText("the conversation")).toBeTruthy();
  });
});

describe("the conversation it opens", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts to the entry's own chat route rather than starting a loose thread", async () => {
    // The route matters: a plain `POST /api/chat/threads` would create an unseeded
    // thread with no link back to the entry, so pressing the button twice would orphan
    // the first conversation and the model would be told nothing about the scan.
    const { posts } = stubFetch(activity());

    draw();
    fireEvent.click(await screen.findByRole("button", { name: /Ask about this scan/i }));

    await waitFor(() => expect(posts.length).toBe(1));
    expect(posts[0]).toContain("/api/intake/pending/1/chat");
  });
});
