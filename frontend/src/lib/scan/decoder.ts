/**
 * The `zxing-wasm` decode step.
 *
 * **Exactly four symbologies.** Every enabled format costs a finder-pattern pass
 * per frame, and these four are the whole job: QR and DataMatrix for our own tags
 * and for reel/bag labels, Code 128 for distributor strip labels, EAN-13 for
 * retail packaging. Adding "just one more" is how the frame budget goes.
 *
 * Containers identify themselves by NFC, so the camera's remaining purpose is
 * vendor labels at intake — on a phone, where autofocus exists. That is what
 * makes a 10 mm dense DataMatrix readable at all.
 */

import { prepareZXingModule, readBarcodes } from "zxing-wasm/reader";
// Bundled rather than fetched: `zxing-wasm` defaults `locateFile` to the jsDelivr
// CDN, and this app is deployed on a LAN behind a private CA with no promise of
// internet access. A scanner that only works when the WAN is up is not a scanner.
import wasmUrl from "zxing-wasm/reader/zxing_reader.wasm?url";

import { centreRoi } from "./roi";

export const ENABLED_FORMATS = ["QRCode", "DataMatrix", "Code128", "EAN13"] as const;

/** Target decode cadence. Fast enough for 3-frame voting to settle quickly. */
export const FRAME_INTERVAL_MS = 100;

export interface Decoded {
  readonly text: string;
  /** Whatever the decoder called the format. Recorded, never validated. */
  readonly symbology: string;
}

let prepared = false;

function prepare(): void {
  if (prepared) {
    return;
  }
  prepareZXingModule({
    overrides: {
      locateFile: (path: string, prefix: string) =>
        path.endsWith(".wasm") ? wasmUrl : `${prefix}${path}`,
    },
  });
  prepared = true;
}

/**
 * A source that can be drawn to a canvas — the `<video>` element in practice,
 * narrowed to what this module actually needs so tests can pass a stub.
 */
export interface FrameSource {
  readonly videoWidth: number;
  readonly videoHeight: number;
}

/**
 * Draw the centre ROI of `source` into `canvas` and hand back the pixels.
 *
 * Returns `null` for a source that has no dimensions yet — a `<video>` reports
 * 0×0 for the first frames after `play()`, and skipping those is normal.
 */
export function cropFrame(
  source: FrameSource & CanvasImageSource,
  canvas: HTMLCanvasElement,
): ImageData | null {
  const rect = centreRoi(source.videoWidth, source.videoHeight);
  if (rect.width === 0 || rect.height === 0) {
    return null;
  }
  canvas.width = rect.width;
  canvas.height = rect.height;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (context === null) {
    return null;
  }
  context.drawImage(
    source,
    rect.x,
    rect.y,
    rect.width,
    rect.height,
    0,
    0,
    rect.width,
    rect.height,
  );
  return context.getImageData(0, 0, rect.width, rect.height);
}

/**
 * Decode one already-cropped frame.
 *
 * `tryHarder` is off: at ten frames a second the cheap pass plus 2-of-3 voting
 * beats one expensive pass, and it keeps a mid-range phone from dropping to two
 * frames a second. `tryRotate` stays on because Code 128 and EAN are not
 * rotation-invariant and nobody holds a reel square to the camera.
 */
export async function decodeImageData(image: ImageData): Promise<Decoded | null> {
  prepare();
  const results = await readBarcodes(image, {
    formats: [...ENABLED_FORMATS],
    tryHarder: false,
    tryRotate: true,
    tryInvert: true,
    maxNumberOfSymbols: 1,
  });
  for (const result of results) {
    if (result.isValid && result.text !== "") {
      return { text: result.text, symbology: result.format };
    }
  }
  return null;
}
