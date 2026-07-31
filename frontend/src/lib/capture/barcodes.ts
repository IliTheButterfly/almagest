/**
 * Every symbol in a still, with the corners the live loop throws away.
 *
 * `lib/scan/decoder.ts` already reads barcodes, and this is deliberately not a
 * second copy of it — it shares the same prepared wasm module — but it is a
 * different *pass*, because a still is a different problem from a video frame:
 *
 * - **No ROI, ever.** The live ladder starts on a 60% centre crop because it is
 *   trying to settle in under a second at 10 fps. A capture runs once, on a
 *   frame the user deliberately framed, and the whole point is to find
 *   everything in it — including the Code 128 along the edge that the ROI pass
 *   would never have seen.
 * - **The expensive settings, immediately.** `tryHarder` plus every readable
 *   format is the live decoder's rung 3, reached only after repeated misses
 *   because it is too slow to run at frame rate. Once per capture it costs a
 *   few hundred milliseconds and is simply the right answer.
 * - **Positions are kept.** `ReadResult.position` carries four corner points and
 *   the live path drops them, having no use for geometry once it has a payload.
 *   Here the geometry *is* half the feature: it is what lets the user see which
 *   mark on the label a value came from.
 *
 * `maxNumberOfSymbols` is raised well above the live loop's 5. A distributor
 * reel label routinely carries a 2D code, a 1D supplier part number and a 1D
 * quantity, and a bag can carry several more; capping low would silently drop
 * the ones at the bottom of the label.
 */

import type { ReadResult } from "zxing-wasm/reader";
import { readBarcodes } from "zxing-wasm/reader";

import { prepareDecoder } from "../scan/decoder";
import type { BarcodeRegion, Quad } from "./types";

/** Generous: a busy reel label, plus the shipping barcodes around it. */
export const MAX_SYMBOLS_PER_CAPTURE = 32;

/**
 * `zxing-wasm` reports a `position` as four named corners. Normalised into the
 * quad order the rest of this module uses — top-left, top-right, bottom-right,
 * bottom-left — which is the order it already reports them in; naming them here
 * is what makes that assumption checkable rather than implicit.
 */
function quadOf(result: ReadResult): Quad {
  const position = result.position;
  return [
    { x: Math.round(position.topLeft.x), y: Math.round(position.topLeft.y) },
    { x: Math.round(position.topRight.x), y: Math.round(position.topRight.y) },
    { x: Math.round(position.bottomRight.x), y: Math.round(position.bottomRight.y) },
    { x: Math.round(position.bottomLeft.x), y: Math.round(position.bottomLeft.y) },
  ];
}

export async function readBarcodeRegions(image: ImageData): Promise<BarcodeRegion[]> {
  prepareDecoder();
  const results = await readBarcodes(image, {
    formats: ["AllReadable"],
    tryHarder: true,
    tryRotate: true,
    tryInvert: true,
    tryDownscale: true,
    maxNumberOfSymbols: MAX_SYMBOLS_PER_CAPTURE,
  });

  const regions: BarcodeRegion[] = [];
  for (const result of results) {
    if (!result.isValid || result.text === "") {
      continue;
    }
    regions.push({
      kind: "barcode",
      text: result.text,
      quad: quadOf(result),
      symbology: result.format,
    });
  }
  return regions;
}
