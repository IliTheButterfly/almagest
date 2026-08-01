import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { IntakeCapture } from "./IntakeCapture";

const readCapture = vi.fn();
const resolveScan = vi.fn();

vi.mock("../lib/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api/client")>()),
  readCapture: (...args: unknown[]) => readCapture(...args),
  resolveScan: (...args: unknown[]) => resolveScan(...args),
  createPart: vi.fn(),
}));

vi.mock("./CategorySelect", () => ({ CategorySelect: () => null }));
vi.mock("./PartKindPicker", () => ({ PartKindPicker: () => null }));

function box(x: number, y: number, w = 120, h = 16) {
  return [
    { x, y },
    { x: x + w, y },
    { x: x + w, y: y + h },
    { x, y: y + h },
  ];
}

/** A cut-down version of the real resistor label: two heading/value pairs. */
const CAPTURE = {
  id: 26,
  created_at: "2026-08-01T21:33:34Z",
  width_px: 1080,
  height_px: 1080,
  text_status: "ok",
  device_id: null,
  note: null,
  document: { sha256: "a".repeat(64), url: "/api/documents/" + "a".repeat(64) },
  regions: [
    { id: 1, kind: "text", text: "Manufacturer", corners: box(90, 814), confidence: 95,
      symbology: null, scan_event_id: null, order_index: 0 },
    { id: 2, kind: "text", text: "STACKPOLE ELECTRONICS INC", corners: box(97, 831, 187, 12),
      confidence: 89, symbology: null, scan_event_id: null, order_index: 1 },
    { id: 3, kind: "text", text: "Quantity", corners: box(603, 842, 67, 28), confidence: 96,
      symbology: null, scan_event_id: null, order_index: 2 },
    { id: 4, kind: "text", text: "100", corners: box(610, 866, 48, 29), confidence: 94,
      symbology: null, scan_event_id: null, order_index: 3 },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  readCapture.mockResolvedValue(CAPTURE);
  resolveScan.mockResolvedValue({ status: "unknown", decoded_kind: "mpn" });
});

describe("IntakeCapture", () => {
  it("shows the photograph that was parked with the entry", async () => {
    render(<IntakeCapture captureId={26} onCreated={vi.fn()} />);
    const picture = await screen.findByAltText(/frame you captured/i);
    expect(picture.getAttribute("src")).toContain("/api/documents/");
  });

  it("fills the form from what the label says", async () => {
    // The point of the whole feature: the desk pass gets the printed values, not
    // just whatever the barcode encoded.
    render(<IntakeCapture captureId={26} onCreated={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByDisplayValue("STACKPOLE ELECTRONICS INC")).toBeTruthy(),
    );
  });

  it("says where a filled value came from", async () => {
    // A value read off a photograph and one decoded from a checksummed symbol
    // must never look like the same kind of claim.
    render(<IntakeCapture captureId={26} onCreated={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/read \d+%/).length).toBeGreaterThan(0));
  });

  it("shows the shipment values apart from the part's own", async () => {
    // Quantity belongs to a lot, not to a part definition, so it is shown to be
    // copied rather than filed into the form.
    render(<IntakeCapture captureId={26} onCreated={vi.fn()} />);
    expect(await screen.findByText(/Also on the label/)).toBeTruthy();
    expect(screen.getByText("Quantity")).toBeTruthy();
  });

  it("reports a picture that will not load instead of rendering an empty form", async () => {
    readCapture.mockRejectedValue(new Error("the picture is gone"));
    render(<IntakeCapture captureId={26} onCreated={vi.fn()} />);
    expect(await screen.findByText(/the picture is gone/)).toBeTruthy();
  });

  it("does not re-read the text, only re-resolves codes", async () => {
    // A fresh OCR pass could disagree with the reading the entry was parked on,
    // and evidence that argues with the record it justifies is worse than none.
    // There are no barcodes on this capture, so nothing should be resolved.
    render(<IntakeCapture captureId={26} onCreated={vi.fn()} />);
    await screen.findByAltText(/frame you captured/i);
    expect(resolveScan).not.toHaveBeenCalled();
  });
});
