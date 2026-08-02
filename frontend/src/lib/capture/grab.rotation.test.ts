/**
 * A capture from a camera that is mounted upside down.
 *
 * `lib/scan/orientation.ts` argues that turning only the *preview* is enough,
 * and for decoding it is: the decoder reads a centred crop, which a half turn
 * maps onto itself, and `decodeImageData` already passes `tryRotate: true`.
 *
 * Neither half of that argument survives here. A still is looked at by a
 * person, and the OCR pass does not degrade gracefully on upside-down text — it
 * returns nothing. So this is the one place the mounting has to reach the
 * pixels, and this file is what stops the two paths drifting apart again.
 */

import { describe, expect, it, vi } from "vitest";

import { grabStill } from "./grab";

interface DrawCall {
  readonly op: string;
  readonly args: readonly number[];
}

/**
 * A canvas that records the transform ops instead of rasterising.
 *
 * jsdom has no 2D context, and asserting on pixels would need a real one; what
 * is actually being pinned is *whether the frame is turned before it is drawn*,
 * which is exactly what these calls say.
 */
function stubCanvas(): { calls: DrawCall[] } {
  const calls: DrawCall[] = [];
  const context = {
    translate: (...args: number[]) => calls.push({ op: "translate", args }),
    rotate: (...args: number[]) => calls.push({ op: "rotate", args }),
    drawImage: (...args: unknown[]) =>
      calls.push({ op: "drawImage", args: args.slice(1) as number[] }),
  };
  const canvas = {
    width: 0,
    height: 0,
    getContext: () => context,
    toBlob: (cb: (blob: Blob | null) => void) => cb(new Blob(["x"], { type: "image/jpeg" })),
  };
  vi.spyOn(document, "createElement").mockImplementation(() => canvas as unknown as HTMLElement);
  return { calls };
}

const source = { videoWidth: 1920, videoHeight: 1080 } as unknown as Parameters<
  typeof grabStill
>[0];

describe("a still from an inverted mount", () => {
  it("is drawn the way the operator saw it, not the way the sensor did", async () => {
    const { calls } = stubCanvas();
    await grabStill(source, 180);

    const ops = calls.map((c) => c.op);
    expect(ops).toEqual(["translate", "rotate", "drawImage"]);
    // Translate to the far corner, then a half turn: the standard 180° about
    // the centre. Rotating without the translate would draw the frame off-canvas
    // entirely, which would be a blank capture rather than an upside-down one.
    expect(calls[0]?.args).toEqual([1920, 1080]);
    expect(calls[1]?.args).toEqual([Math.PI]);
  });

  it("leaves an upright camera completely alone", async () => {
    const { calls } = stubCanvas();
    await grabStill(source, 0);

    // No transform at all on the overwhelmingly common path — a phone held by a
    // person. This is the control: if `grabStill` ever rotated unconditionally,
    // every phone capture would come out upside down and this is what says so.
    expect(calls.map((c) => c.op)).toEqual(["drawImage"]);
  });
});
