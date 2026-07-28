/**
 * Centre-ROI crop.
 *
 * The decoder reads a centre crop rather than the whole frame, for two reasons:
 * it is cheaper per frame, and it makes aiming explicit — the user points the
 * marked rectangle at one label instead of holding a whole reel up and hoping
 * the right barcode wins. The viewfinder draws the same rectangle so what is
 * decoded is what is shown.
 *
 * The crop keeps the frame's aspect ratio instead of being square: a Code 128
 * strip on a reel label is much wider than it is tall, and squaring the crop is
 * what makes those reads fail at a comfortable working distance.
 */

/** Fraction of each axis kept. Mirrored by `.viewfinder .roi` in `styles.css`. */
export const ROI_FRACTION = 0.6;

export interface Rect {
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
}

/**
 * The centred crop rectangle, in whole pixels.
 *
 * A zero-sized source yields a zero-sized rect rather than throwing: a `<video>`
 * reports 0×0 for the first frames after `play()`, and the decode loop skipping
 * those is not an error worth an exception.
 */
export function centreRoi(
  sourceWidth: number,
  sourceHeight: number,
  fraction: number = ROI_FRACTION,
): Rect {
  if (fraction <= 0 || fraction > 1) {
    throw new RangeError("the ROI fraction must be within (0, 1]");
  }
  if (sourceWidth <= 0 || sourceHeight <= 0) {
    return { x: 0, y: 0, width: 0, height: 0 };
  }

  const width = Math.max(1, Math.round(sourceWidth * fraction));
  const height = Math.max(1, Math.round(sourceHeight * fraction));
  return {
    x: Math.floor((sourceWidth - width) / 2),
    y: Math.floor((sourceHeight - height) / 2),
    width,
    height,
  };
}
