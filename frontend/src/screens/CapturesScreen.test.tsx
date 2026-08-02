import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CapturesScreen } from "./CapturesScreen";

const listCaptures = vi.fn();
const resolveScan = vi.fn();
const deleteCapture = vi.fn();

// Spread the real module rather than replacing it: `ErrorBanner` narrows on the
// real `ApiError` class, so a mock that omits it turns an error-path assertion
// into a crash inside the component being asserted on.
vi.mock("../lib/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api/client")>()),
  listCaptures: (...args: unknown[]) => listCaptures(...args),
  resolveScan: (...args: unknown[]) => resolveScan(...args),
  deleteCapture: (...args: unknown[]) => deleteCapture(...args),
}));

function capture(id: number, regions: unknown[], textStatus = "ok") {
  return {
    id,
    created_at: "2026-08-01T10:00:00Z",
    width_px: 800,
    height_px: 600,
    text_status: textStatus,
    device_id: null,
    note: null,
    document: { sha256: "a".repeat(64), url: `/api/documents/${"a".repeat(64)}` },
    regions,
  };
}

const BARCODE = {
  id: 1,
  kind: "barcode",
  text: "RC0805FR-0710KL",
  corners: [
    { x: 0, y: 0 },
    { x: 10, y: 0 },
    { x: 10, y: 10 },
    { x: 0, y: 10 },
  ],
  symbology: "DataMatrix",
  confidence: null,
  scan_event_id: null,
  order_index: 0,
};

beforeEach(() => {
  vi.clearAllMocks();
  resolveScan.mockResolvedValue({ status: "unknown", decoded_kind: "mpn" });
});

describe("CapturesScreen", () => {
  it("says plainly when nothing has been captured", async () => {
    listCaptures.mockResolvedValue({ items: [], total: 0 });
    render(<CapturesScreen />);
    expect(await screen.findByText(/Nothing captured yet/)).toBeTruthy();
  });

  it("lists what was on each capture so the grid can be skimmed", async () => {
    listCaptures.mockResolvedValue({
      items: [capture(1, [BARCODE, { ...BARCODE, id: 2, kind: "text", text: "Murata" }])],
      total: 1,
    });
    render(<CapturesScreen />);
    expect(await screen.findByText("1 code · 1 line")).toBeTruthy();
  });

  it("distinguishes a capture nobody read from one with nothing on it", async () => {
    // The same distinction `CaptureTextStatus` draws on the server: "no text
    // found" and "no text looked for" are different facts about the photograph.
    listCaptures.mockResolvedValue({
      items: [capture(1, [], "not_attempted"), capture(2, [], "empty")],
      total: 2,
    });
    render(<CapturesScreen />);
    expect(await screen.findByText("no text read yet")).toBeTruthy();
    expect(screen.getByText("nothing read")).toBeTruthy();
  });

  it("surfaces a load failure rather than showing an empty gallery", async () => {
    // An empty grid and a broken request look identical, and one of them is a
    // lie about what you have kept.
    listCaptures.mockRejectedValue(new Error("the network went away"));
    render(<CapturesScreen />);
    // `describeError` prefers the error's own message over the banner's
    // fallback, so what the user sees is the cause, not a generic sentence.
    expect(await screen.findByText(/the network went away/)).toBeTruthy();
    expect(screen.queryByText(/Nothing captured yet/)).toBeNull();
  });

  it("does not re-resolve anything until a capture is opened", async () => {
    listCaptures.mockResolvedValue({ items: [capture(1, [BARCODE])], total: 1 });
    render(<CapturesScreen />);
    await waitFor(() => expect(screen.getByText("1 code")).toBeTruthy());
    // A gallery of thirty captures must not fire thirty resolves on load.
    expect(resolveScan).not.toHaveBeenCalled();
  });
});
