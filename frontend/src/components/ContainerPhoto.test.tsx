import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { DocumentRead } from "../lib/api/client";
import { ContainerPhoto } from "./ContainerPhoto";

function makeDocument(overrides: Partial<DocumentRead> = {}): DocumentRead {
  return {
    id: 1,
    sha256: "a".repeat(64),
    kind: "photo",
    media_type: "image/jpeg",
    byte_size: 4096,
    page_count: null,
    source_url: null,
    original_filename: "drawer.jpg",
    created_at: "2026-01-01T00:00:00Z",
    url: `/api/documents/${"a".repeat(64)}`,
    ...overrides,
  };
}

describe("a photo is attached", () => {
  it("renders it, not the glyph", () => {
    render(<ContainerPhoto photo={makeDocument()} glyph="drawer" alt="Top drawer" />);
    const img = screen.getByRole("img", { name: "Top drawer" }) as HTMLImageElement;
    expect(img.tagName).toBe("IMG");
    expect(img.src).toContain(makeDocument().url);
  });

  it("falls back to the glyph if the image fails to load, rather than a broken-image icon", () => {
    render(<ContainerPhoto photo={makeDocument()} glyph="drawer" alt="Top drawer" />);
    const img = screen.getByRole("img", { name: "Top drawer" });
    fireEvent.error(img);
    // No <img> left in the document at all — the fallback is a glyph <span>,
    // never the browser's own broken-image glyph sitting where the photo was.
    expect(screen.queryByRole("img", { name: "Top drawer" })?.tagName).not.toBe("IMG");
    expect(screen.getByTitle("Drawer")).toBeTruthy();
  });
});

describe("no photo, a glyph is set", () => {
  it("renders the glyph with an accessible label, at tile size", () => {
    render(<ContainerPhoto photo={null} glyph="bin" alt="Small parts bin" />);
    expect(screen.queryByRole("img", { name: "Small parts bin" })).toBeNull();
    const glyph = screen.getByTitle("Bin");
    expect(glyph.className).toContain("cell-glyph");
  });

  it("renders the glyph in the larger placeholder box at card size", () => {
    render(<ContainerPhoto photo={null} glyph="bin" alt="Small parts bin" size="card" />);
    const glyph = screen.getByTitle("Bin");
    expect(glyph.className).toContain("container-photo-placeholder");
  });
});

describe("neither a photo nor a glyph, at tile size (the dense tree)", () => {
  it("renders nothing at all — no placeholder box, and never a broken image", () => {
    const { container } = render(<ContainerPhoto photo={null} glyph={null} alt="Empty shelf" />);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.queryByTitle("No picture set")).toBeNull();
    // A dashed placeholder in every one of ninety-six otherwise-unpictured
    // cells would be noise, not information — see the component's docstring.
    expect(container.textContent).toBe("");
  });

  it("is also what an unrecognised glyph name from a newer build renders as", () => {
    // `container_types.glyph`/`locations.glyph` carry no CHECK — a row can
    // legally hold a name this bundle predates, and the promise is that it is
    // drawn the same way an unset glyph is, never a crash.
    const { container } = render(
      <ContainerPhoto photo={null} glyph="isometric-hologram" alt="Future container" />,
    );
    expect(container.textContent).toBe("");
  });
});

describe("neither a photo nor a glyph, at card size (a container's own screen)", () => {
  it("renders a visible, dashed placeholder — the natural place to add one", () => {
    render(<ContainerPhoto photo={null} glyph={null} alt="Empty shelf" size="card" />);
    expect(screen.queryByRole("img")).toBeNull();
    const placeholder = screen.getByTitle("No picture set");
    expect(placeholder.className).toContain("container-photo-placeholder");
  });
});
