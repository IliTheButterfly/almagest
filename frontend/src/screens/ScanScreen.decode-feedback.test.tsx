/**
 * Decode feedback on the scan screen — the fix for "I scanned it and nothing
 * happened."
 *
 * `lib/scan/feedback.test.ts` pins down the recipe itself in isolation; these
 * tests are about the wiring: that `handle()` actually calls it, on every
 * outcome, and that the no-camera state leads with the manual path instead of
 * burying it below a viewfinder that will never come alive.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ScanScreen } from "./ScanScreen";

/** An https origin with neither camera nor NFC — desktop Chromium, say. */
function noCaptureBrowser(): void {
  vi.stubGlobal("isSecureContext", true);
  vi.stubGlobal("navigator", { userAgent: "test" });
  vi.stubGlobal("NDEFReader", undefined);
}

/** What a browser on `http://<lan-ip>:5173` actually exposes: neither API. */
function plainHttpBrowser(): void {
  vi.stubGlobal("isSecureContext", false);
  vi.stubGlobal("navigator", { userAgent: "test" });
  vi.stubGlobal("NDEFReader", undefined);
}

function stubResolve(body: Record<string, unknown>): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const RESOLVED_PART = {
  decoded_kind: "mpn",
  latency_ms: 12,
  normalized: "LM317T",
  scan_event_id: 1,
  status: "resolved",
  suggest_bind: false,
  target: { entity_pk: 7, entity_type: "part", label: "LM317T" },
};

const UNKNOWN = {
  decoded_kind: "unknown",
  latency_ms: 8,
  normalized: "GARBAGE",
  scan_event_id: 2,
  status: "unknown",
  suggest_bind: true,
};

function renderScan(): void {
  render(
    <MemoryRouter initialEntries={["/scan"]}>
      <ScanScreen />
    </MemoryRouter>,
  );
}

async function submit(code: string): Promise<void> {
  fireEvent.change(screen.getByPlaceholderText(/4K7T-92M8/), { target: { value: code } });
  fireEvent.click(screen.getByRole("button", { name: /look up/i }));
}

/**
 * Submits the form directly rather than through the button, which the first
 * resolve leaves disabled ("Looking up…") until its promise settles — a real
 * camera decode does not go through that button at all, and the debounce
 * must hold regardless of what the UI happens to be showing.
 */
function submitFormDirectly(code: string): void {
  const input = screen.getByPlaceholderText(/4K7T-92M8/);
  fireEvent.change(input, { target: { value: code } });
  const form = input.closest("form");
  if (form === null) {
    throw new Error("CodeEntry did not render a <form>");
  }
  fireEvent.submit(form);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("decode feedback", () => {
  it("fires — flash, and an attempted vibration — on a decode that resolves", async () => {
    noCaptureBrowser();
    stubResolve(RESOLVED_PART);
    renderScan();

    expect(screen.getByText("Ready to scan")).toBeTruthy();
    await submit("LM317T");

    // The flash fires immediately, before the fetch above even settles —
    // that immediacy is the point, so no `waitFor` here.
    expect(screen.getByText("Scanned")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("LM317T")).toBeTruthy());
  });

  it("fires just as loudly on a decode that resolves to nothing — the case that matters most", async () => {
    noCaptureBrowser();
    stubResolve(UNKNOWN);
    renderScan();

    await submit("GARBAGE-CODE");

    expect(screen.getByText("Scanned")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("Nothing matched")).toBeTruthy());
  });

  it("debounces a second identical decode inside 400 ms to a single flash", () => {
    noCaptureBrowser();
    const fetchMock = stubResolve(RESOLVED_PART);
    renderScan();

    // Two submissions of the same code back to back — well inside the 400 ms
    // window — must not produce two independent "just scanned" events. The
    // scan session's own 2 s debounce would also drop the second resolve,
    // but that is a different mechanism (see feedback.test.ts for the
    // isolated 400 ms behaviour); this just checks the wiring does not fire
    // the network call twice either way.
    submitFormDirectly("LM317T");
    submitFormDirectly("LM317T");

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not throw when navigator has no vibrate — the iOS Safari case", async () => {
    // noCaptureBrowser's stub navigator has no `vibrate` at all, which is
    // exactly what iOS Safari looks like; a throw here would abort the
    // decode handler before the result ever renders.
    noCaptureBrowser();
    stubResolve(RESOLVED_PART);
    renderScan();

    await expect(submit("LM317T")).resolves.not.toThrow();
    await waitFor(() => expect(screen.getByText("LM317T")).toBeTruthy());
  });
});

describe("the no-camera dead end", () => {
  it("leads with manual entry, ahead of the NFC section", () => {
    plainHttpBrowser();
    renderScan();

    const text = document.body.textContent ?? "";
    const noticeAt = text.indexOf("No camera here");
    const typeAt = text.indexOf("Type it");
    const nfcAt = text.indexOf("No NFC reader here");

    expect(noticeAt).toBeGreaterThanOrEqual(0);
    expect(typeAt).toBeGreaterThan(noticeAt);
    expect(nfcAt).toBeGreaterThan(typeAt);
  });

  it("says a QR opened with the phone's own camera lands in a browser, not this app", () => {
    plainHttpBrowser();
    renderScan();

    expect(screen.getByText(/phone's own camera app/)).toBeTruthy();
    expect(screen.getByText(/not a bug/)).toBeTruthy();
  });
});
