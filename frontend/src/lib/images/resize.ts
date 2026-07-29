/**
 * Downscale a photo before it ever leaves the phone.
 *
 * The API deliberately never processes an uploaded image —
 * `app.services.blobstore` checks five bytes of magic and stops there, per its
 * own module docstring, because ADR 0005's whole point is that the API does not
 * carry an image/PDF pipeline. Left alone, a 12 MP photo taken standing in front
 * of a drawer (commonly 3-8 MB) is stored, served and fetched at full resolution
 * forever — even though every place a container photo is actually drawn
 * (`ContainerPhoto`) renders it at a few hundred pixels. Downscaling has to
 * happen somewhere, and the browser holding the camera, before the bytes are
 * sent, is the only place left that is honest about it.
 *
 * Bounded to `maxDimension` on the longer edge and re-encoded as JPEG at
 * `quality` — plenty for "what does this drawer look like", and it turns a
 * 4000x3000 shot into roughly 1600x1200, several MB into a few hundred KB.
 *
 * **Never blocks the upload.** Decoding, canvas support and `toBlob` can all
 * fail — an old browser, a stripped-down embedded WebView, or (see
 * `resize.test.ts`) a test environment with no canvas at all — and every one of
 * those paths returns the original file untouched rather than refusing the
 * photo. A container photo that occasionally uploads at full size is a cost;
 * one that occasionally refuses to upload at all is a worse one.
 */

export interface DownscaleOptions {
  /** The longer edge is capped to this many pixels. */
  readonly maxDimension?: number;
  /** JPEG quality, 0-1, used only when a resize actually happens. */
  readonly quality?: number;
}

const DEFAULT_MAX_DIMENSION = 1600;
const DEFAULT_QUALITY = 0.85;

export async function downscaleForUpload(
  file: File,
  { maxDimension = DEFAULT_MAX_DIMENSION, quality = DEFAULT_QUALITY }: DownscaleOptions = {},
): Promise<File> {
  if (!file.type.startsWith("image/")) {
    return file;
  }
  // Feature-detected rather than assumed: this is the branch that fires in the
  // test suite's jsdom environment, which has neither `createImageBitmap` nor a
  // real 2D canvas context, and it must behave exactly as it would on a real but
  // older browser — pass the original bytes through.
  if (typeof createImageBitmap !== "function") {
    return file;
  }

  try {
    const bitmap = await createImageBitmap(file);
    try {
      if (bitmap.width <= maxDimension && bitmap.height <= maxDimension) {
        // Already small enough — returned untouched rather than re-encoded, so
        // a modest PNG someone already cropped is not quietly turned into a
        // slightly-lossy JPEG for no benefit.
        return file;
      }

      const scale = maxDimension / Math.max(bitmap.width, bitmap.height);
      const width = Math.max(1, Math.round(bitmap.width * scale));
      const height = Math.max(1, Math.round(bitmap.height * scale));

      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext("2d");
      if (context === null) {
        return file;
      }
      context.drawImage(bitmap, 0, 0, width, height);

      const blob = await new Promise<Blob | null>((resolve) => {
        canvas.toBlob(resolve, "image/jpeg", quality);
      });
      if (blob === null) {
        return file;
      }
      const name = `${file.name.replace(/\.\w+$/, "")}.jpg`;
      return new File([blob], name, { type: "image/jpeg" });
    } finally {
      bitmap.close();
    }
  } catch {
    // Decode failure, an exotic format, a browser that half-implements the
    // API — all the same answer: upload what the camera actually produced.
    return file;
  }
}
