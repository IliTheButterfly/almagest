import { fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";

import type { CameraControls } from "./Viewfinder";
import { Viewfinder } from "./Viewfinder";

function controls(overrides: Partial<CameraControls> = {}): CameraControls {
  return {
    resolution: { width: 1920, height: 1080 },
    torch: { available: false, enabled: false, toggle: () => undefined },
    zoom: { available: false, min: 0, max: 0, step: 1, value: 0, set: () => undefined },
    pass: "roi",
    ...overrides,
  };
}

/**
 * A fresh rotation store per render, so one test flipping the preview cannot
 * change what the next one sees. The default is real `localStorage`, which in
 * jsdom is shared by every test in the file.
 */
function rotationStore(initial: Record<string, string> = {}) {
  const entries: Record<string, string> = { ...initial };
  return {
    entries,
    getItem: (key: string) => entries[key] ?? null,
    setItem: (key: string, value: string) => {
      entries[key] = value;
    },
  };
}

function renderViewfinder(props: Partial<Parameters<typeof Viewfinder>[0]> = {}) {
  return render(
    <Viewfinder
      videoRef={createRef<HTMLVideoElement>()}
      status="live"
      message={null}
      unavailableNotice={null}
      rotationStore={rotationStore()}
      {...props}
    />,
  );
}

function video(container: HTMLElement): HTMLVideoElement {
  const element = container.querySelector("video");
  if (element === null) {
    throw new Error("no preview rendered");
  }
  return element;
}

function roi(container: HTMLElement): HTMLElement {
  const element = container.querySelector(".roi");
  if (element === null) {
    throw new Error("no ROI overlay rendered");
  }
  return element as HTMLElement;
}

describe("the ROI overlay tells the truth about what is being decoded", () => {
  it("is a boundary while the cheap centre-crop pass is running", () => {
    const { container } = renderViewfinder({ camera: controls({ pass: "roi" }) });
    expect(roi(container).className).not.toContain("is-advisory");
    expect(screen.getByText("Hold one label inside the box")).toBeTruthy();
  });

  it("becomes advisory once the ladder escalates to the full frame", () => {
    // The old overlay always meant "outside this is not decoded", which is a lie
    // the moment the decoder starts reading the whole frame — and it is a lie
    // exactly when the user most needs to be told they are being helped anyway.
    const { container } = renderViewfinder({ camera: controls({ pass: "full-frame" }) });
    expect(roi(container).className).toContain("is-advisory");
    // Colour and border style are never the only signal: the caption says it too.
    expect(screen.getByText(/whole frame/)).toBeTruthy();
  });

  it("says so on the expensive pass as well", () => {
    renderViewfinder({ camera: controls({ pass: "hard" }) });
    expect(screen.getByText(/every symbology/)).toBeTruthy();
  });

  it("is drawn from the granted resolution, not at a fixed inset", () => {
    // 16:9 into the 4:3 box: `object-fit: cover` hides the frame's left and right
    // edges, so the crop the decoder reads is 10% in horizontally and 20% in
    // vertically. Drawing 20% on both axes points the user at too narrow a strip.
    const wide = renderViewfinder({
      camera: controls({ resolution: { width: 1920, height: 1080 } }),
    });
    expect(roi(wide.container).style.inset).toBe("20.00% 10.00%");

    const square = renderViewfinder({
      camera: controls({ resolution: { width: 1440, height: 1080 } }),
    });
    expect(roi(square.container).style.inset).toBe("20.00% 20.00%");
  });

  it("is a boundary again before the first frame has been attempted", () => {
    const { container } = renderViewfinder({ camera: controls({ pass: null }) });
    expect(roi(container).className).not.toContain("is-advisory");
  });

  it("does not claim a pass is running when the camera is not live", () => {
    const { container } = renderViewfinder({
      status: "starting",
      camera: controls({ pass: "hard" }),
    });
    expect(roi(container).className).not.toContain("is-advisory");
    expect(screen.getByText("Opening the camera…")).toBeTruthy();
  });

  it("lets an explicit hint win, because a caller's busy state matters more", () => {
    renderViewfinder({ camera: controls({ pass: "hard" }), hint: "Resolving…" });
    expect(screen.getByText("Resolving…")).toBeTruthy();
  });

  it("survives a pass name it has no wording for", () => {
    renderViewfinder({ camera: controls({ pass: "some-future-pass" }) });
    expect(screen.getByText("Hold one label inside the box")).toBeTruthy();
  });
});

describe("camera diagnostics and tuning", () => {
  it("prints the resolution actually granted, not the one asked for", () => {
    // A silent getUserMedia fallback to 640×480 is the difference between a QR
    // that reads and one that never will, and it is invisible from the picture.
    renderViewfinder({ camera: controls({ resolution: { width: 640, height: 480 } }) });
    expect(screen.getByText("Camera 640×480")).toBeTruthy();
  });

  it("offers the torch only when the track reports one", () => {
    renderViewfinder({ camera: controls() });
    expect(screen.queryByRole("button", { name: /torch/i })).toBeNull();

    const toggle = vi.fn();
    renderViewfinder({
      camera: controls({ torch: { available: true, enabled: false, toggle } }),
    });
    const button = screen.getByRole("button", { name: "Torch off" });
    expect(button.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(button);
    expect(toggle).toHaveBeenCalledOnce();
  });

  it("shows the torch as pressed when it is on", () => {
    renderViewfinder({
      camera: controls({ torch: { available: true, enabled: true, toggle: () => undefined } }),
    });
    expect(screen.getByRole("button", { name: "Torch on" }).getAttribute("aria-pressed")).toBe(
      "true",
    );
  });

  it("offers zoom only when the track reports a range, and applies the number", () => {
    renderViewfinder({ camera: controls() });
    expect(screen.queryByRole("slider")).toBeNull();

    const set = vi.fn();
    renderViewfinder({
      camera: controls({
        zoom: { available: true, min: 1, max: 4, step: 0.5, value: 1, set },
      }),
    });
    const slider = screen.getByRole("slider");
    fireEvent.change(slider, { target: { value: "2.5" } });
    expect(set).toHaveBeenCalledWith(2.5);
  });

  it("still offers the mount setting when the platform reports nothing else", () => {
    // A bench webcam advertises neither torch nor zoom, and on the station it is
    // the only camera there is. The row used to disappear entirely in that case,
    // which is precisely the machine that needs the half turn.
    const { container } = renderViewfinder({
      camera: controls({ resolution: null }),
    });
    expect(container.querySelector(".camera-tuning")).toBeTruthy();
    expect(screen.getByRole("button", { name: /mount/i })).toBeTruthy();
  });

  it("renders no tuning row before the camera is live", () => {
    const { container } = renderViewfinder({ status: "starting", camera: controls() });
    expect(container.querySelector(".camera-tuning")).toBeNull();
  });

  it("renders the preview unchanged when no camera handle is passed", () => {
    const { container } = renderViewfinder();
    expect(container.querySelector(".viewfinder")).toBeTruthy();
    expect(container.querySelector(".camera-tuning")).toBeNull();
  });
});

describe("a camera mounted upside down", () => {
  it("previews upright by default", () => {
    const { container } = renderViewfinder({ camera: controls() });
    expect(video(container).className).not.toContain("is-half-turned");
    expect(screen.getByRole("button", { name: "Upright mount" }).getAttribute("aria-pressed")).toBe(
      "false",
    );
  });

  it("turns the picture half way round when the mount is inverted", () => {
    const { container } = renderViewfinder({ camera: controls() });
    fireEvent.click(screen.getByRole("button", { name: "Upright mount" }));
    expect(video(container).className).toContain("is-half-turned");
    expect(
      screen.getByRole("button", { name: "Upside-down mount" }).getAttribute("aria-pressed"),
    ).toBe("true");
  });

  it("remembers the choice, because a bracket does not move between page loads", () => {
    const store = rotationStore();
    renderViewfinder({ camera: controls(), rotationStore: store });
    fireEvent.click(screen.getByRole("button", { name: "Upright mount" }));
    expect(store.entries["almagest.camera-rotation"]).toBe("180");
  });

  it("starts turned when the store already says so", () => {
    const { container } = renderViewfinder({
      camera: controls(),
      rotationStore: rotationStore({ "almagest.camera-rotation": "180" }),
    });
    expect(video(container).className).toContain("is-half-turned");
  });

  it("leaves the ROI overlay exactly where it was", () => {
    // The decoder reads a *centred* crop, which a half turn maps onto itself, so
    // the aiming box must not move. If this ever fails, the preview and the
    // decoded region have come apart and the box is pointing at the wrong place.
    const { container } = renderViewfinder({ camera: controls() });
    const before = roi(container).style.inset;
    fireEvent.click(screen.getByRole("button", { name: "Upright mount" }));
    expect(roi(container).style.inset).toBe(before);
  });
});

describe("no camera at all", () => {
  it("explains, and points at the manual path", () => {
    renderViewfinder({ status: "unavailable", camera: controls() });
    expect(screen.getByText(/Type the code instead/)).toBeTruthy();
    expect(screen.queryByRole("slider")).toBeNull();
  });
});
