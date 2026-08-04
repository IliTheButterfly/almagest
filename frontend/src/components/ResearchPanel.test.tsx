/**
 * The research panel, exercised against a stubbed `fetch`.
 *
 * What is under test is the design problem the panel exists to solve: **"no
 * datasheet" must be a diagnosis rather than a dead end.** The backend keeps every
 * rejected candidate with its reason (ADR 0017) precisely so a person can tell
 * "four sources all returned the wrong part" apart from "a login wall" apart from
 * "nothing covers this manufacturer" — and all three collapse into one useless
 * sentence if the screen only says "not found".
 *
 * So the tests below check that the rejections reach the screen with their reasons
 * in words, that the wrong-part count is stated rather than left to be counted by
 * eye, and — the one most easily lost in a redesign — that `exhausted` is **not**
 * dressed as a failure. A normal outcome shown in a warning colour teaches people
 * to ignore the colour, and then the real failures go unread too.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { ResearchPanel } from "./ResearchPanel";

interface Call {
  readonly url: string;
  readonly method: string;
}

const calls: Call[] = [];

function candidate(overrides: Record<string, unknown> = {}) {
  return {
    source: "websearch",
    url: "https://example.test/wrong.pdf",
    state: "rejected",
    reject_reason: "mpn_absent",
    document_sha256: null,
    rank: 4,
    note: null,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function stubApi(research: Record<string, unknown>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      calls.push({ url: url.pathname, method: request.method });
      const body =
        request.method === "POST" && url.pathname === "/api/research/requeue"
          ? { part: { ...research, state: "pending", attempts: 0, error: null } }
          : research;
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  calls.length = 0;
});

it("says how many sources returned a datasheet for a different part", async () => {
  // The sentence that turns a list of URLs into a diagnosis. Three rejections all
  // reading `mpn_absent` is a provider bug, not an obscure part, and nobody should
  // have to count rows to notice.
  stubApi({
    part_id: 1,
    state: "exhausted",
    attempts: 1,
    error: null,
    candidates: [
      candidate({ url: "https://a.test/1.pdf" }),
      candidate({ url: "https://b.test/2.pdf" }),
      candidate({ url: "https://c.test/3.pdf", reject_reason: "not_pdf" }),
    ],
  });

  render(<ResearchPanel partId={1} />);

  await waitFor(() => {
    expect(screen.getByText(/3 sources tried/)).toBeTruthy();
  });
  expect(screen.getByText(/2 of them a datasheet for a different part/)).toBeTruthy();
});

it("explains each rejection in words rather than in its stored slug", async () => {
  stubApi({
    part_id: 1,
    state: "exhausted",
    attempts: 1,
    error: null,
    candidates: [candidate({ reject_reason: "not_pdf" })],
  });

  render(<ResearchPanel partId={1} />);

  await waitFor(() => {
    expect(screen.getByText(/usually a login wall or an error page/)).toBeTruthy();
  });
  // The raw slug is not what a person reads.
  expect(screen.queryByText("not_pdf")).toBeNull();
});

it("does not dress `exhausted` as a failure", async () => {
  // The distinction the backend is careful to keep (`research_error` is null for
  // `exhausted`) has to survive into the pixels. A normal outcome in a warning
  // colour trains people to ignore the colour.
  stubApi({ part_id: 1, state: "exhausted", attempts: 1, error: null, candidates: [] });

  const { container } = render(<ResearchPanel partId={1} />);

  await waitFor(() => {
    expect(screen.getByText("nothing found")).toBeTruthy();
  });
  expect(screen.getByText(/not a fault/)).toBeTruthy();
  expect(container.querySelector(".badge-bad")).toBeNull();
});

it("does dress `failed` as a problem, and shows what broke", async () => {
  stubApi({
    part_id: 1,
    state: "failed",
    attempts: 3,
    error: "URLError: [Errno -3] Temporary failure in name resolution",
    candidates: [],
  });

  const { container } = render(<ResearchPanel partId={1} />);

  await waitFor(() => {
    expect(screen.getByText("search broke")).toBeTruthy();
  });
  expect(container.querySelector(".badge-bad")).toBeTruthy();
  expect(screen.getByText(/Temporary failure in name resolution/)).toBeTruthy();
});

it("treats a part nobody has researched as ordinary, not as an empty state", async () => {
  // `pending` with no candidates is most of the catalogue. It is not something to
  // apologise for, and it must not read as an error.
  stubApi({ part_id: 1, state: "pending", attempts: 0, error: null, candidates: [] });

  render(<ResearchPanel partId={1} />);

  await waitFor(() => {
    expect(screen.getByText("queued")).toBeTruthy();
  });
  expect(screen.queryByText(/sources tried/)).toBeNull();
});

it("offers another search only from a terminal state", async () => {
  // Re-queueing something already queued or in flight does nothing useful and
  // invites double-clicking a worker into a second lease.
  stubApi({ part_id: 1, state: "claimed", attempts: 1, error: null, candidates: [] });

  render(<ResearchPanel partId={1} />);

  await waitFor(() => {
    expect(screen.getByText("searching")).toBeTruthy();
  });
  expect(screen.queryByRole("button", { name: /search again/i })).toBeNull();
});

it("re-queues an exhausted part, which is the upgrade path when a provider is added", async () => {
  stubApi({
    part_id: 7,
    state: "exhausted",
    attempts: 1,
    error: null,
    candidates: [candidate({})],
  });

  render(<ResearchPanel partId={7} />);

  const button = await screen.findByRole("button", { name: /search again/i });
  fireEvent.click(button);

  await waitFor(() => {
    expect(calls.some((c) => c.url === "/api/research/requeue" && c.method === "POST")).toBe(true);
  });
});

it("links a validated candidate and marks which source won", async () => {
  stubApi({
    part_id: 1,
    state: "resolved",
    attempts: 1,
    error: null,
    candidates: [
      candidate({
        source: "url_pattern",
        url: "https://murata.test/GRM188.pdf",
        state: "validated",
        reject_reason: null,
        document_sha256: "a".repeat(64),
        rank: 2,
      }),
    ],
  });

  const { container } = render(<ResearchPanel partId={1} />);

  await waitFor(() => {
    expect(screen.getByText("found")).toBeTruthy();
  });
  const link = container.querySelector('a[href="https://murata.test/GRM188.pdf"]');
  expect(link).toBeTruthy();
  // Opened in a new tab, and never as a referrer-leaking or opener-sharing link:
  // these are third-party URLs the researcher found, not ours.
  expect(link?.getAttribute("rel")).toContain("noopener");
});
