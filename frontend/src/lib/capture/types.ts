/**
 * What a capture is, on the client, before any of it has been saved.
 *
 * One shape for two very different readers. `zxing-wasm` reports four corners
 * per symbol and no score; Tesseract reports an axis-aligned box per line and a
 * 0-100 confidence. Rather than let the UI branch on which produced a region,
 * both are normalised to a quad here — an axis-aligned box is just a quad whose
 * corners happen to be square — and the difference that actually matters is kept
 * as `kind`.
 *
 * **`kind` is about how it was read, never about what it means.** A DataMatrix
 * checksummed or it did not; an OCR'd line is a guess with a number attached.
 * `docs/PLAN.md` is explicit that a model-read part number is never
 * auto-accepted, and keeping the two apart in the type is what stops a component
 * downstream from quietly treating them the same. It is also why `confidence`
 * lives only on the text branch: there is no 87%-true barcode, and inventing a
 * score for one would make a guessed word and a verified payload look
 * comparable.
 */

/** A corner, in the captured image's own pixel space. */
export interface Point {
  readonly x: number;
  readonly y: number;
}

/** Four corners, in the order the decoder gave them, so rotation survives. */
export type Quad = readonly [Point, Point, Point, Point];

export interface BarcodeRegion {
  readonly kind: "barcode";
  /** Verbatim, control characters and all — this is posted to the resolver. */
  readonly text: string;
  readonly quad: Quad;
  /** Whatever the decoder called the format. Recorded, never validated. */
  readonly symbology: string;
}

export interface TextRegion {
  readonly kind: "text";
  readonly text: string;
  readonly quad: Quad;
  /** 0-100, as Tesseract reports it. */
  readonly confidence: number;
}

export type Region = BarcodeRegion | TextRegion;

/** A still, with its own dimensions, before anything has been read off it. */
export interface Still {
  readonly blob: Blob;
  readonly width: number;
  readonly height: number;
}

/**
 * Mirrors the server's `CaptureTextStatus`, and exists for the same reason:
 * "found no text" and "never looked" must not render identically. A phone that
 * could not load the OCR model at all is a fact about the reader, and saying
 * "no text found" there would be a lie about the label.
 */
export type TextStatus = "not_attempted" | "ok" | "empty" | "unavailable" | "failed";

/** Squares an axis-aligned box off into the quad everything else speaks. */
export function boxToQuad(x0: number, y0: number, x1: number, y1: number): Quad {
  return [
    { x: x0, y: y0 },
    { x: x1, y: y0 },
    { x: x1, y: y1 },
    { x: x0, y: y1 },
  ];
}

/**
 * The axis-aligned bounds of a quad, which is what the overlay actually draws.
 *
 * Drawing the true rotated quad would need an SVG polygon per region; a bounding
 * box is one absolutely-positioned `<div>` and is enough to say *which* part of
 * the frame a value came from — the only question the outline has to answer.
 * The quad is still what gets stored, so a later overlay can draw the real
 * shape without a migration.
 */
export function bounds(quad: Quad): { x: number; y: number; width: number; height: number } {
  const xs = quad.map((point) => point.x);
  const ys = quad.map((point) => point.y);
  const x = Math.min(...xs);
  const y = Math.min(...ys);
  return { x, y, width: Math.max(...xs) - x, height: Math.max(...ys) - y };
}

/**
 * Whether two regions cover essentially the same pixels.
 *
 * Needed because the two readers overlap: Tesseract regularly "reads" the
 * printed digits underneath a Code 128 strip, and a reel label whose MPN appears
 * both in the DataMatrix and in ink beside it is genuinely two regions. The
 * first is a duplicate worth dropping; the second is not, because they are in
 * different places. Comparing geometry rather than text is what tells them
 * apart — and the barcode is always the one kept, since it checksummed.
 */
export function overlaps(a: Quad, b: Quad, threshold = 0.6): boolean {
  const first = bounds(a);
  const second = bounds(b);
  const width = Math.min(first.x + first.width, second.x + second.width) - Math.max(first.x, second.x);
  const height =
    Math.min(first.y + first.height, second.y + second.height) - Math.max(first.y, second.y);
  if (width <= 0 || height <= 0) {
    return false;
  }
  const intersection = width * height;
  const smaller = Math.min(first.width * first.height, second.width * second.height);
  return smaller > 0 && intersection / smaller >= threshold;
}
