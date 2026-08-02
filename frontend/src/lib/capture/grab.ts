/**
 * Take one still off the running camera.
 *
 * **Deliberately not `ImageCapture.takePhoto()`.** That API reaches for the
 * sensor's full-resolution stills pipeline, which sounds like exactly what a
 * capture wants and is the wrong trade here for three reasons: it is absent on
 * every Safari and on Firefox, so the fallback would be the common path rather
 * than the rare one; on Android it frequently fires the *shutter sound and a
 * focus hunt*, adding a second or more to a workflow whose whole justification
 * is speed; and it can hand back a frame that is not the one the user was
 * looking at when they tapped. Drawing the current video frame to a canvas is
 * available everywhere, is instantaneous, and captures precisely what was on
 * screen — which is what makes the outlines drawn over it truthful.
 *
 * The resolution is whatever `getUserMedia` granted, read off the element rather
 * than assumed. `useScanner` asks for 1920×1080 and prints back what it actually
 * got, because a silent fallback to 640×480 is the difference between a dense
 * DataMatrix that reads and one that never will — and that applies at least as
 * much to a still being OCR'd as to a live decode.
 *
 * JPEG at quality 0.92 rather than PNG: a photograph of a label compresses to a
 * few hundred KB instead of several MB, and the store this lands in caps a
 * document at 64 MiB. It is deliberately *higher* than the 0.85
 * `images/resize.ts` uses for container photos — that image only ever gets drawn
 * a few hundred pixels wide, whereas this one has to survive an OCR pass, and
 * JPEG ringing around small dark glyphs on white is exactly what costs a
 * character.
 */

import type { CameraRotation } from "../scan/orientation";
import { defaultRotationStore, readCameraRotation } from "../scan/orientation";
import type { Still } from "./types";

/** What the still is encoded as. Matches one of `blobstore.MEDIA_TYPES`. */
export const CAPTURE_MEDIA_TYPE = "image/jpeg";

const CAPTURE_QUALITY = 0.92;

/** The subset of `<video>` this needs, so a test can pass a stub. */
export interface CaptureSource {
  readonly videoWidth: number;
  readonly videoHeight: number;
}

export class CaptureUnavailableError extends Error {}

/**
 * Draw the current frame and encode it.
 *
 * Throws rather than returning null: unlike a decode miss — which is routine and
 * silent — a capture is something the user explicitly asked for, so every way it
 * can fail has to produce a sentence they can act on.
 */
export async function grabStill(
  source: CaptureSource & CanvasImageSource,
  rotation: CameraRotation = readCameraRotation(defaultRotationStore()),
): Promise<Still> {
  const width = source.videoWidth;
  const height = source.videoHeight;
  if (width === 0 || height === 0) {
    // A `<video>` reports 0×0 for the first frames after `play()`. Skipping
    // those is normal in the decode loop; here it means the user tapped before
    // the camera was ready.
    throw new CaptureUnavailableError("The camera is still starting up. Try again in a moment.");
  }

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  if (context === null) {
    throw new CaptureUnavailableError("This browser would not give us a canvas to capture into.");
  }
  if (rotation === 180) {
    // The one place the camera's mounting has to reach the pixels rather than
    // just the preview. `lib/scan/orientation.ts` argues at length that turning
    // only the picture is enough for *decoding* — a centred crop is invariant
    // under a half turn and ZXing already tries rotations — and both halves of
    // that argument stop applying here. A still is looked at by a person, and
    // the OCR pass in `ocr.ts` does not degrade on upside-down text, it returns
    // nothing at all. So the capture is drawn the way the operator saw it.
    context.translate(width, height);
    context.rotate(Math.PI);
  }
  context.drawImage(source, 0, 0, width, height);

  const blob = await new Promise<Blob | null>((resolve) => {
    canvas.toBlob(resolve, CAPTURE_MEDIA_TYPE, CAPTURE_QUALITY);
  });
  if (blob === null) {
    throw new CaptureUnavailableError("The captured frame could not be encoded.");
  }
  return { blob, width, height };
}

/**
 * The still as something both decoders can read.
 *
 * `createImageBitmap` is the one input `zxing-wasm` and Tesseract both accept
 * without a second decode of the same JPEG, which matters because the two passes
 * run over the identical image and re-decoding it per reader would double the
 * slowest part of the work.
 */
export async function toBitmap(still: Still): Promise<ImageBitmap> {
  return createImageBitmap(still.blob);
}

/** Pixels for `zxing-wasm`, which wants `ImageData` rather than a bitmap. */
export function toImageData(bitmap: ImageBitmap): ImageData | null {
  const canvas = document.createElement("canvas");
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (context === null) {
    return null;
  }
  context.drawImage(bitmap, 0, 0);
  return context.getImageData(0, 0, bitmap.width, bitmap.height);
}
