/**
 * Both readers over one still, in the order the user experiences them.
 *
 * **Barcodes first, and reported before the text pass starts.** They take a few
 * hundred milliseconds against several seconds for OCR — and, unlike OCR, they
 * cannot fail to be available. Waiting for both before drawing anything would
 * make the fast, certain half of the answer hostage to the slow, optional half,
 * which is the wrong trade on a phone held over a bin. So this is a two-stage
 * callback rather than one promise of everything.
 *
 * **Where the two overlap, the barcode wins.** Tesseract reliably "reads" the
 * printed digits underneath a Code 128 strip and the bars themselves as
 * characters, which would put a garbage chip directly on top of a verified one.
 * The test is geometric, not textual (`overlaps`), because the same MPN appearing
 * both inside a DataMatrix and in ink *beside* it is genuinely two findings in
 * two places, and dropping one of those would lose the more useful one — the ink
 * is what a human can check.
 */

import { readBarcodeRegions } from "./barcodes";
import { toImageData } from "./grab";
import { readTextRegions, type OcrImage } from "./ocr";
import type { BarcodeRegion, Region, TextRegion, TextStatus } from "./types";
import { overlaps } from "./types";

export interface DetectionProgress {
  /** Fired once the barcode pass is done, before OCR is even started. */
  readonly onBarcodes?: (regions: readonly BarcodeRegion[]) => void;
}

export interface Detection {
  readonly regions: readonly Region[];
  readonly textStatus: TextStatus;
  /** Set when the text pass could not run or threw; safe to show the user. */
  readonly textMessage?: string;
}

/**
 * Drop OCR lines that sit on top of a decoded symbol.
 *
 * Exported for its own test: it is the one piece of judgement in this file, and
 * getting the threshold wrong is invisible in a screenshot — either duplicate
 * chips nobody notices are duplicates, or a legitimately separate printed value
 * silently missing.
 */
export function withoutBarcodeShadows(
  barcodes: readonly BarcodeRegion[],
  text: readonly TextRegion[],
): TextRegion[] {
  return text.filter((line) => !barcodes.some((barcode) => overlaps(barcode.quad, line.quad)));
}

export async function detectRegions(
  bitmap: ImageBitmap,
  ocrImage: OcrImage,
  progress: DetectionProgress = {},
): Promise<Detection> {
  const pixels = toImageData(bitmap);
  const barcodes = pixels === null ? [] : await readBarcodeRegions(pixels);
  progress.onBarcodes?.(barcodes);

  // The still's own JPEG, not the bitmap: Tesseract's `ImageLike` does not
  // include `ImageBitmap`. See `ocr.ts`.
  const outcome = await readTextRegions(ocrImage);
  const text = withoutBarcodeShadows(barcodes, outcome.regions);

  return {
    regions: [...barcodes, ...text],
    textStatus: outcome.status,
    ...(outcome.message === undefined ? {} : { textMessage: outcome.message }),
  };
}
