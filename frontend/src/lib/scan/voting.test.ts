import { describe, expect, it } from "vitest";

import { FrameVoter, MultiFrameVoter } from "./voting";

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

describe("3-frame voting on a frame carrying several symbols", () => {
  it("applies the same 2-of-3 rule to a single payload", () => {
    const voter = new MultiFrameVoter();
    expect(voter.observe(["A"])).toEqual([]);
    expect(voter.observe(["A"])).toEqual(["A"]);
  });

  it("accepts both symbols of a reel label together", () => {
    // A reel commonly carries a DataMatrix and a Code 128 of the same MPN, and
    // reading both is more information than reading one.
    const voter = new MultiFrameVoter();
    expect(voter.observe(["DM", "C128"])).toEqual([]);
    expect(voter.observe(["DM", "C128"]).sort()).toEqual(["C128", "DM"]);
  });

  it("does not make one symbol wait for the other", () => {
    // The DataMatrix has been in view for two frames; the Code 128 has just
    // appeared. The DataMatrix surfaces now — it has its own two votes.
    const voter = new MultiFrameVoter();
    voter.observe(["DM"]);
    expect(voter.observe(["DM", "C128"])).toEqual(["DM"]);
    expect(voter.observe(["C128"])).toEqual(["C128"]);
  });

  it("forgets only the winner, leaving a payload still accumulating alone", () => {
    const voter = new MultiFrameVoter();
    voter.observe(["DM", "C128"]);
    expect(voter.observe(["DM"])).toEqual(["DM"]);
    // C128 kept its one vote from the first frame, so one more sighting wins.
    expect(voter.observe(["C128"])).toEqual(["C128"]);
  });

  it("makes a winner earn fresh votes before it can win again", () => {
    const voter = new MultiFrameVoter();
    voter.observe(["A"]);
    expect(voter.observe(["A"])).toEqual(["A"]);
    expect(voter.observe(["A"])).toEqual([]);
    expect(voter.observe(["A"])).toEqual(["A"]);
  });

  it("lets votes fall out of the window", () => {
    const voter = new MultiFrameVoter();
    expect(voter.observe(["STRAY"])).toEqual([]);
    expect(voter.observe([])).toEqual([]);
    expect(voter.observe([])).toEqual([]);
    expect(voter.observe(["STRAY"])).toEqual([]);
  });

  it("pushes an empty frame into the window rather than skipping it", () => {
    const voter = new MultiFrameVoter();
    voter.observe(["A"]);
    voter.observe([]);
    voter.observe([]);
    expect(voter.frames).toEqual([["A"], [], []]);
  });

  it("keeps at most the window size", () => {
    const voter = new MultiFrameVoter();
    voter.observe(["A"]);
    voter.observe(["B"]);
    voter.observe(["C"]);
    voter.observe(["D"]);
    expect(voter.frames).toEqual([["B"], ["C"], ["D"]]);
  });

  it("resets the whole window", () => {
    const voter = new MultiFrameVoter();
    voter.observe(["A"]);
    voter.reset();
    expect(voter.frames).toEqual([]);
    expect(voter.observe(["A"])).toEqual([]);
  });

  it("refuses a nonsensical configuration rather than voting oddly", () => {
    expect(() => new MultiFrameVoter(0)).toThrow(RangeError);
    expect(() => new MultiFrameVoter(3, 4)).toThrow(RangeError);
  });
});
