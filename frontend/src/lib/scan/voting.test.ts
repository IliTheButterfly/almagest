import { describe, expect, it } from "vitest";

import { FrameVoter } from "./voting";

describe("3-frame voting", () => {
  it("does not accept a payload seen only once", () => {
    const voter = new FrameVoter();
    expect(voter.observe("PAYLOAD")).toBeNull();
  });

  it("accepts on the second of two consecutive identical frames", () => {
    const voter = new FrameVoter();
    expect(voter.observe("PAYLOAD")).toBeNull();
    expect(voter.observe("PAYLOAD")).toBe("PAYLOAD");
  });

  it("accepts two of three when the middle frame decoded nothing", () => {
    // The realistic case: the hand moves, one frame blurs, the label is still the
    // same label. Two of the last three agree, so it counts.
    const voter = new FrameVoter();
    expect(voter.observe("PAYLOAD")).toBeNull();
    expect(voter.observe(null)).toBeNull();
    expect(voter.observe("PAYLOAD")).toBe("PAYLOAD");
  });

  it("accepts two of three when a different label flickers through", () => {
    const voter = new FrameVoter();
    expect(voter.observe("A")).toBeNull();
    expect(voter.observe("B")).toBeNull();
    expect(voter.observe("A")).toBe("A");
  });

  it("never accepts a payload whose votes fall out of the window", () => {
    // A single stray read followed by three of something else must not combine
    // with a fourth stray read four frames later.
    const voter = new FrameVoter();
    expect(voter.observe("STRAY")).toBeNull();
    expect(voter.observe(null)).toBeNull();
    expect(voter.observe(null)).toBeNull();
    expect(voter.observe("STRAY")).toBeNull();
  });

  it("resets on acceptance so one more sighting does not re-fire", () => {
    const voter = new FrameVoter();
    voter.observe("PAYLOAD");
    expect(voter.observe("PAYLOAD")).toBe("PAYLOAD");
    // Without the reset, the accepted frames would still be in the window and this
    // single further sighting would win again immediately.
    expect(voter.observe("PAYLOAD")).toBeNull();
    expect(voter.observe("PAYLOAD")).toBe("PAYLOAD");
  });

  it("never accepts a frame that decoded nothing", () => {
    const voter = new FrameVoter();
    expect(voter.observe(null)).toBeNull();
    expect(voter.observe(null)).toBeNull();
    expect(voter.observe(null)).toBeNull();
  });

  it("keeps at most the window size", () => {
    const voter = new FrameVoter();
    voter.observe("A");
    voter.observe("B");
    voter.observe("C");
    voter.observe("D");
    expect(voter.frames).toEqual(["B", "C", "D"]);
  });

  it("refuses a nonsensical configuration rather than voting oddly", () => {
    expect(() => new FrameVoter(0)).toThrow(RangeError);
    expect(() => new FrameVoter(3, 4)).toThrow(RangeError);
  });
});
