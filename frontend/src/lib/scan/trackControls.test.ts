/**
 * Every one of `getCapabilities`, `getSettings` and `applyConstraints` is
 * optional in the spec, and a browser that has the method can still throw from
 * it. So most of what is worth asserting here is the *absence* of a crash: a
 * missing or misbehaving capability must degrade to "not offered", never to a
 * scanner that stopped decoding.
 */

import { describe, expect, it } from "vitest";

import type { VideoTrackLike } from "./trackControls";
import {
  applyContinuousFocus,
  safeCapabilities,
  safeSettings,
  setTorch,
  setZoom,
  supportsContinuousFocus,
  torchAvailable,
  zoomRange,
} from "./trackControls";

/** A track that records what was applied to it. */
function trackWith(
  capabilities: unknown,
  settings: unknown = {},
): VideoTrackLike & { readonly applied: unknown[] } {
  const applied: unknown[] = [];
  return {
    applied,
    getCapabilities: () => capabilities,
    getSettings: () => settings,
    applyConstraints: (constraints?: { advanced?: unknown[] }) => {
      applied.push(...(constraints?.advanced ?? []));
      return Promise.resolve();
    },
  };
}

const THROWING: VideoTrackLike = {
  getCapabilities: () => {
    throw new Error("not implemented on this browser");
  },
  getSettings: () => {
    throw new Error("not implemented on this browser");
  },
  applyConstraints: () => Promise.reject(new Error("OverconstrainedError")),
};

describe("reading a track's capabilities", () => {
  it("returns what the track reports", () => {
    expect(safeCapabilities(trackWith({ torch: true }))).toEqual({ torch: true });
  });

  it("degrades to nothing when the method is absent", () => {
    expect(safeCapabilities({})).toEqual({});
    expect(safeSettings({})).toEqual({});
  });

  it("degrades to nothing when the method throws", () => {
    expect(safeCapabilities(THROWING)).toEqual({});
    expect(safeSettings(THROWING)).toEqual({});
  });

  it("degrades to nothing when the method returns a non-object", () => {
    expect(safeCapabilities({ getCapabilities: () => null })).toEqual({});
    expect(safeCapabilities({ getCapabilities: () => "capabilities" })).toEqual({});
    expect(safeSettings({ getSettings: () => 42 })).toEqual({});
  });
});

describe("continuous autofocus", () => {
  it("is offered only when the mode is actually listed", () => {
    expect(supportsContinuousFocus({ focusMode: ["continuous", "manual"] })).toBe(true);
    expect(supportsContinuousFocus({ focusMode: ["manual"] })).toBe(false);
    expect(supportsContinuousFocus({})).toBe(false);
  });

  it("is not offered when the browser reports a non-array", () => {
    // Firefox has been observed returning a string here.
    expect(supportsContinuousFocus({ focusMode: "continuous" as unknown as string[] })).toBe(false);
  });
});

describe("torch", () => {
  it("is offered only on an explicit true", () => {
    expect(torchAvailable({ torch: true })).toBe(true);
    expect(torchAvailable({ torch: false })).toBe(false);
    expect(torchAvailable({})).toBe(false);
  });
});

describe("zoom range", () => {
  it("is read from min and max", () => {
    expect(zoomRange({ zoom: { min: 1, max: 4, step: 0.5 } })).toEqual({
      min: 1,
      max: 4,
      step: 0.5,
    });
  });

  it("defaults a missing or useless step to 1 rather than to zero", () => {
    // A step of 0 would make the slider unusable, which is worse than coarse.
    expect(zoomRange({ zoom: { min: 1, max: 4 } })?.step).toBe(1);
    expect(zoomRange({ zoom: { min: 1, max: 4, step: 0 } })?.step).toBe(1);
  });

  it("is null when the range is absent, partial or empty", () => {
    expect(zoomRange({})).toBeNull();
    expect(zoomRange({ zoom: { min: 1 } })).toBeNull();
    expect(zoomRange({ zoom: { max: 4 } })).toBeNull();
    // A single-valued range is not a range; offering a slider for it is a lie.
    expect(zoomRange({ zoom: { min: 2, max: 2 } })).toBeNull();
    expect(zoomRange({ zoom: { min: 4, max: 1 } })).toBeNull();
  });
});

describe("applying a constraint", () => {
  it("goes through `advanced`, one constraint at a time", async () => {
    const track = trackWith({});
    expect(await applyContinuousFocus(track)).toBe(true);
    expect(await setTorch(track, true)).toBe(true);
    expect(await setZoom(track, 2.5)).toBe(true);
    expect(track.applied).toEqual([{ focusMode: "continuous" }, { torch: true }, { zoom: 2.5 }]);
  });

  it("reports failure rather than throwing when the browser rejects it", async () => {
    // A browser that advertises a capability and then rejects the constraint
    // anyway must be treated exactly like one that never advertised it.
    expect(await setTorch(THROWING, true)).toBe(false);
    expect(await setZoom(THROWING, 2)).toBe(false);
    expect(await applyContinuousFocus(THROWING)).toBe(false);
  });

  it("reports failure when the method is absent entirely", async () => {
    expect(await setTorch({}, true)).toBe(false);
  });

  it("turns the torch back off through the same door", async () => {
    const track = trackWith({});
    await setTorch(track, false);
    expect(track.applied).toEqual([{ torch: false }]);
  });
});
