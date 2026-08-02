/**
 * The overlay's job in one sentence: show where each value came from, and let a
 * person take it without ever taking it for them.
 *
 * These cases lean on the two rules that are easy to regress by making the UI
 * "helpful" — that a read value is visibly distinguished from a decoded one, and
 * that nothing is copied or filled without a deliberate tap.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CaptureOverlay } from "./CaptureOverlay";
import { boxToQuad, type Region } from "../lib/capture/types";

const REGIONS: Region[] = [
  {
    kind: "barcode",
    text: "RC0805FR-0710KL",
    quad: boxToQuad(10, 10, 110, 60),
    symbology: "DataMatrix",
  },
  {
    kind: "text",
    text: "Murata Electronics",
    quad: boxToQuad(10, 200, 260, 240),
    confidence: 74,
  },
];

function renderOverlay(props: Partial<React.ComponentProps<typeof CaptureOverlay>> = {}) {
  return render(
    <CaptureOverlay
      imageUrl="blob:capture"
      width={1000}
      height={500}
      regions={REGIONS}
      resolved={{}}
      textStatus="ok"
      {...props}
    />,
  );
}

beforeEach(() => {
  Object.assign(navigator, {
    clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
});

describe("outlines", () => {
  it("places each region as a percentage of the capture's own dimensions", () => {
    // Percentages rather than pixels are what let one overlay serve a phone and
    // a desktop panel with no measurement — so the arithmetic is worth pinning.
    renderOverlay();
    const code = screen.getByRole("button", { name: /DataMatrix code/ });
    expect(code.style.left).toBe("1%");
    expect(code.style.top).toBe("2%");
    expect(code.style.width).toBe("10%");
    expect(code.style.height).toBe("10%");
  });

  it("distinguishes a decoded region from a read one in the markup", () => {
    renderOverlay();
    expect(screen.getByRole("button", { name: /DataMatrix code/ }).className).toContain("is-barcode");
    expect(screen.getByRole("button", { name: /Read text/ }).className).toContain("is-text");
  });

  it("names what it outlined, so the values are reachable without a mouse", () => {
    renderOverlay();
    expect(screen.getByRole("button", { name: "Read text: Murata Electronics" })).toBeTruthy();
  });
});

describe("taking a value", () => {
  it("shows nothing to copy until an outline is tapped", () => {
    renderOverlay();
    expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
    expect(screen.getByText(/2 things read/)).toBeTruthy();
  });

  it("copies only on a deliberate tap, and says so", async () => {
    renderOverlay();
    fireEvent.click(screen.getByRole("button", { name: /Read text/ }));
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));

    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith("Murata Electronics"),
    );
    expect(await screen.findByRole("button", { name: "Copied" })).toBeTruthy();
  });

  it("marks a read value with its confidence", () => {
    renderOverlay();
    fireEvent.click(screen.getByRole("button", { name: /Read text/ }));
    expect(screen.getByText(/Read text · 74%/)).toBeTruthy();
  });

  it("tapping the same outline again closes it", () => {
    renderOverlay();
    const region = screen.getByRole("button", { name: /Read text/ });
    fireEvent.click(region);
    expect(screen.getByRole("button", { name: "Copy" })).toBeTruthy();
    fireEvent.click(region);
    expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
  });
});

describe("filling a field", () => {
  it("offers no Use button when the caller is not pointing at a field", () => {
    // A read value carries no target field of its own, so with nothing armed
    // there is nothing it could fill — only copying.
    renderOverlay({ onFill: vi.fn() });
    fireEvent.click(screen.getByRole("button", { name: /Read text/ }));
    expect(screen.queryByRole("button", { name: /^Use/ })).toBeNull();
  });

  it("lets an armed field take a read value, naming the field on the button", () => {
    // This is the mechanism that makes an OCR'd value usable at all: the user
    // pointed at MPN first, which is the human decision the never-auto-accept
    // rule requires.
    const onFill = vi.fn();
    renderOverlay({ fillInto: { field: "mpn", label: "MPN" }, onFill });

    fireEvent.click(screen.getByRole("button", { name: /Read text/ }));
    fireEvent.click(screen.getByRole("button", { name: "Use as MPN" }));

    expect(onFill).toHaveBeenCalledWith("mpn", "Murata Electronics");
  });
});

describe("saying why there is no text", () => {
  it("does not claim the label was blank when the reader could not load", () => {
    renderOverlay({
      regions: [REGIONS[0]!],
      textStatus: "unavailable",
      textMessage: "The text reader could not be loaded on this device.",
    });
    expect(screen.getByText(/could not be loaded/)).toBeTruthy();
    // And is explicit that the barcode half still worked.
    expect(screen.getByText(/codes above were still read/)).toBeTruthy();
  });

  it("distinguishes a frame with no readable text from one nobody read", () => {
    renderOverlay({ regions: [REGIONS[0]!], textStatus: "empty" });
    expect(screen.getByText(/No readable text in this frame/)).toBeTruthy();
  });

  it("says the codes are ready while the slow pass is still running", () => {
    renderOverlay({ regions: [REGIONS[0]!], textStatus: "not_attempted", readingText: true });
    expect(screen.getByText(/Reading the text/)).toBeTruthy();
  });
});
