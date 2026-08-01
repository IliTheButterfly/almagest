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
 *
 * ## Several attempts, and all of them run
 *
 * One set of decoder settings is not enough for a label that has been in a bag.
 * A real DigiKey resistor label — creased across the symbol, one corner torn —
 * returned **nothing at all** from the settings that read every clean code
 * fixture: not a bad decode, no symbol found. What reads it is
 * `tryDenoise` (a morphological closing that repairs the thin white cracks a
 * crease puts through black modules) together with a different binarizer and a
 * different downscale factor. Error correction does not help there, because the
 * damage is across the *finder pattern*, which ECC does not protect — the
 * detector never locates the symbol to correct.
 *
 * So there is a ladder, and **every rung runs, even after one succeeds.** Early
 * exit is the obvious optimisation and it is wrong here: the cheap rung readily
 * finds the Code 128 along the edge of that same label while missing the Data
 * Matrix in the middle, so stopping at the first success is precisely how the
 * interesting code gets lost. Measured at ~520 ms for the whole ladder on a
 * 1152x2048 photo, against several seconds for the OCR pass that follows it, and
 * barcodes are reported to the UI before that pass even starts.
 */

import type { ReadResult } from "zxing-wasm/reader";
import { readBarcodes } from "zxing-wasm/reader";

import { prepareDecoder } from "../scan/decoder";
import type { BarcodeRegion, Quad } from "./types";

/** Generous: a busy reel label, plus the shipping barcodes around it. */
export const MAX_SYMBOLS_PER_CAPTURE = 32;

/**
 * One attempt's worth of decoder tuning. Ordered cheapest and most ordinary
 * first, so a clean label is read by the first rung and the rest only ever add
 * to it.
 *
 * `downscaleFactor` is in here because the default of 3 is not a safe middle:
 * the DigiKey label above decodes at 2 and at 4 and fails at 3, which is a
 * sampling artefact rather than anything meaningful about the image.
 */
export interface DecodeAttempt {
  readonly name: string;
  readonly tryDenoise: boolean;
  readonly binarizer: "LocalAverage" | "GlobalHistogram";
  readonly downscaleFactor?: number;
}

export const DECODE_ATTEMPTS: readonly DecodeAttempt[] = [
  // What every undamaged label needs, and nothing more.
  { name: "plain", tryDenoise: false, binarizer: "LocalAverage" },
  // Creased, scuffed or poorly printed modules.
  { name: "denoise", tryDenoise: true, binarizer: "LocalAverage" },
  // Uneven lighting across a curved bag, where a local threshold chases the
  // gradient instead of the ink.
  { name: "denoise-global", tryDenoise: true, binarizer: "GlobalHistogram" },
  // The two downscale factors either side of the default.
  { name: "denoise-global-x2", tryDenoise: true, binarizer: "GlobalHistogram", downscaleFactor: 2 },
  { name: "denoise-global-x4", tryDenoise: true, binarizer: "GlobalHistogram", downscaleFactor: 4 },
];

/**
 * Two results are the same symbol when they say the same thing, in the same
 * format, in the same place. Position is part of the key on purpose: a reel
 * label that prints its MPN in both a Data Matrix and a Code 128 has two
 * genuine symbols with identical text, and collapsing those would throw away
 * the one the user might actually be pointing at.
 */
const POSITION_TOLERANCE_PX = 12;

function keyOf(region: BarcodeRegion): string {
  const x = Math.round(region.quad[0].x / POSITION_TOLERANCE_PX);
  const y = Math.round(region.quad[0].y / POSITION_TOLERANCE_PX);
  return `${region.symbology}\u0000${region.text}\u0000${x},${y}`;
}

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

  const found = new Map<string, BarcodeRegion>();
  for (const attempt of DECODE_ATTEMPTS) {
    let results: ReadResult[];
    try {
      results = await readBarcodes(image, {
        formats: ["AllReadable"],
        tryHarder: true,
        tryRotate: true,
        tryInvert: true,
        tryDownscale: true,
        tryDenoise: attempt.tryDenoise,
        binarizer: attempt.binarizer,
        ...(attempt.downscaleFactor === undefined
          ? {}
          : { downscaleFactor: attempt.downscaleFactor }),
        maxNumberOfSymbols: MAX_SYMBOLS_PER_CAPTURE,
      });
    } catch {
      // One rung throwing must not lose the rungs that already succeeded — this
      // is a best-effort sweep, not a transaction.
      continue;
    }

    for (const result of results) {
      if (!result.isValid || result.text === "") {
        continue;
      }
      const region: BarcodeRegion = {
        kind: "barcode",
        text: result.text,
        quad: quadOf(result),
        symbology: result.format,
      };
      // First rung to find a symbol keeps it, so the position reported is the
      // one from the least-manipulated image.
      const key = keyOf(region);
      if (!found.has(key)) {
        found.set(key, region);
      }
    }
  }
  return [...found.values()];
}
