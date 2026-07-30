/**
 * Centre-ROI crop.
 *
 * The decoder reads a centre crop rather than the whole frame, for two reasons:
 * it is cheaper per frame, and it makes aiming explicit — the user points the
 * marked rectangle at one label instead of holding a whole reel up and hoping
 * the right barcode wins. The viewfinder draws the same rectangle so what is
 * decoded is what is shown — which takes {@link roiOverlayInset}, not a fixed
 * inset, because the preview is `object-fit: cover` and the camera does not
 * deliver the viewfinder's aspect ratio.
 *
 * The crop keeps the frame's aspect ratio instead of being square: a Code 128
 * strip on a reel label is much wider than it is tall, and squaring the crop is
 * what makes those reads fail at a comfortable working distance.
 */

/** Fraction of each axis kept by the cheap pass. */
export const ROI_FRACTION = 0.6;

/**
 * Aspect ratio of the viewfinder box — must match `.viewfinder`'s `aspect-ratio`
 * in `styles.css`, which is the one place it can be authored. Read by
 * {@link roiOverlayInset}, because the crop the decoder reads and the picture the
 * user sees are not the same rectangle unless the camera happens to agree with
 * the box.
 */
export const VIEWFINDER_ASPECT = 4 / 3;

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

/** Inset of the drawn overlay from each edge of the viewfinder box, as a fraction. */
export interface Inset {
  /** From the left and right edges. */
  readonly x: number;
  /** From the top and bottom edges. */
  readonly y: number;
}

/** What to draw when the frame's dimensions are not known yet. */
const NAIVE_INSET: Inset = { x: (1 - ROI_FRACTION) / 2, y: (1 - ROI_FRACTION) / 2 };

/**
 * Where to draw the ROI rectangle over the preview.
 *
 * Not `(1 - fraction) / 2` on both axes, which is what a fixed CSS `inset` gave
 * and what made the box a lie: the preview is `object-fit: cover`, so a 16∶9
 * camera in a 4∶3 box has its left and right edges cropped out of the picture
 * entirely. The decoder's 60% crop of the *frame* is therefore a much smaller
 * inset horizontally than vertically — 10% against 20% for 1920×1080 in a 4∶3
 * box — and a box drawn 20% in on both axes tells the user to aim at a region
 * narrower than the one being read. Since the whole point of the rectangle is
 * aiming, drawing it wrong costs reads.
 *
 * Falls back to the naive inset for a frame with no dimensions yet, rather than
 * dividing by zero or hiding the box for the first few frames.
 */
export function roiOverlayInset(
  frameWidth: number,
  frameHeight: number,
  fraction: number = ROI_FRACTION,
  boxAspect: number = VIEWFINDER_ASPECT,
): Inset {
  if (frameWidth <= 0 || frameHeight <= 0 || boxAspect <= 0) {
    return NAIVE_INSET;
  }
  const frameAspect = frameWidth / frameHeight;

  // The part of the frame `cover` actually shows, in frame pixels: the wider of
  // the two axes is the one that gets cropped away.
  const visibleWidth = frameAspect > boxAspect ? frameHeight * boxAspect : frameWidth;
  const visibleHeight = frameAspect > boxAspect ? frameHeight : frameWidth / boxAspect;

  // Clamped at zero: a crop wider than the visible picture extends past the edge
  // of the box, and an overlay drawn outside it would be worse than one flush
  // with it.
  return {
    x: Math.max(0, (visibleWidth - frameWidth * fraction) / 2 / visibleWidth),
    y: Math.max(0, (visibleHeight - frameHeight * fraction) / 2 / visibleHeight),
  };
}
