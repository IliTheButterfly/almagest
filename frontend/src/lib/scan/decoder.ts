/**
 * The `zxing-wasm` decode step.
 *
 * **Three passes, escalating.** The previous version of this file ran exactly
 * one pass — a centre-ROI crop at `tryHarder: false`, four hardcoded formats —
 * on the claim that "every enabled format costs a finder-pattern pass per
 * frame". That claim was never measured here and is now retracted; what *is*
 * measured (informally, by using the thing) is that the single-pass design
 * made two large classes of code invisible: anything the user did not centre
 * in the ROI, and anything the cheap pass is too conservative to read even
 * when centred. `escalation.ts` is what decides *when* to move up a pass;
 * this file only defines what each pass actually does:
 *
 * 1. **ROI, cheap.** What existed before: a 60% centre crop, the four formats
 *    a scan is overwhelmingly likely to be (our own QR/DataMatrix tags,
 *    Code 128 distributor strips, EAN-13 retail packaging), `tryHarder: false`.
 *    Fast enough that 2-of-3 voting settles in well under a second.
 * 2. **Full frame, same formats.** The user did not centre the label — still
 *    common, and previously undecodable no matter how long they held still.
 *    Same cheap options, so this pass costs about what pass 1 costs; it is
 *    just reading a bigger image.
 * 3. **Full frame, tryHarder, every readable format.** The expensive pass,
 *    reached only after repeated misses. `formats: ["AllReadable"]` rather
 *    than a hand-picked list — enumerating "every useful symbology" one at a
 *    time is how a comment quietly drifts out of date with what `zxing-wasm`
 *    actually ships; the meta-format is the version that stays correct.
 *    `escalation.ts` also gives this pass a longer floor between attempts
 *    (`ESCALATED_INTERVAL_MS`), so escalating does not mean decoding at full
 *    speed with the most expensive settings.
 *
 * Containers identify themselves by NFC; the camera's remaining purpose is
 * vendor labels at intake, on a phone, where autofocus exists.
 */

import type { ReadInputBarcodeFormat } from "zxing-wasm/reader";
import { prepareZXingModule, readBarcodes } from "zxing-wasm/reader";
// Bundled rather than fetched: `zxing-wasm` defaults `locateFile` to the jsDelivr
// CDN, and this app is deployed on a LAN behind a private CA with no promise of
// internet access. A scanner that only works when the WAN is up is not a scanner.
import wasmUrl from "zxing-wasm/reader/zxing_reader.wasm?url";

import type { EscalationLevel } from "./escalation";
import { centreRoi } from "./roi";

/**
 * Pass 1 and 2: the formats a scan is overwhelmingly likely to be.
 *
 * `satisfies` rather than a cast, here and below: a format name `zxing-wasm`
 * does not recognise is silently ignored at runtime, so a typo would show up
 * as "that symbology never decodes" rather than as an error. The compiler is
 * the only thing that catches it.
 */
export const FAST_FORMATS = [
  "QRCode",
  "DataMatrix",
  "Code128",
  "EAN13",
] as const satisfies readonly ReadInputBarcodeFormat[];

/**
 * Pass 3: literally everything `zxing-wasm` can read. Adds, among others,
 * Aztec, PDF417, Code 39/93, ITF, Codabar, and the DataBar/UPC families — real
 * symbologies used on shipping labels and older component packaging that the
 * fast pass has never covered.
 */
export const ESCALATED_FORMATS = ["AllReadable"] as const satisfies readonly ReadInputBarcodeFormat[];

/** Target decode cadence for the two cheap passes. Fast enough for 3-frame voting to settle quickly. */
export const FRAME_INTERVAL_MS = 100;

/** Floor between attempts of the expensive pass — an escalated phone must not also run at 10 fps. */
export const ESCALATED_INTERVAL_MS = 500;

/** A reel label routinely carries more than one symbol of the same MPN; read all of them. */
export const MAX_SYMBOLS_PER_FRAME = 5;

export interface Decoded {
  readonly text: string;
  /** Whatever the decoder called the format. Recorded, never validated. */
  readonly symbology: string;
}

export interface DecodePass {
  readonly name: string;
  /** `1` reads the whole frame; anything less crops to the centre first. */
  readonly roiFraction: number;
  readonly formats: readonly ReadInputBarcodeFormat[];
  readonly tryHarder: boolean;
  readonly tryDownscale?: boolean;
}

/** The escalation ladder, and what each rung actually asks the decoder to do. */
export const DECODE_PASSES: readonly DecodePass[] = [
  { name: "roi", roiFraction: 0.6, formats: FAST_FORMATS, tryHarder: false },
  { name: "full-frame", roiFraction: 1, formats: FAST_FORMATS, tryHarder: false },
  {
    name: "hard",
    roiFraction: 1,
    formats: ESCALATED_FORMATS,
    tryHarder: true,
    tryDownscale: true,
  },
];

/** The cadence half of the ladder, kept next to the passes it describes. */
export const ESCALATION_LEVELS: readonly EscalationLevel[] = DECODE_PASSES.map((pass) => ({
  name: pass.name,
  minIntervalMs: pass.name === "hard" ? ESCALATED_INTERVAL_MS : FRAME_INTERVAL_MS,
}));

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
 * Draw one pass's crop of `source` into `canvas` and hand back the pixels.
 *
 * Returns `null` for a source that has no dimensions yet — a `<video>` reports
 * 0×0 for the first frames after `play()`, and skipping those is normal.
 */
export function cropFrame(
  source: FrameSource & CanvasImageSource,
  canvas: HTMLCanvasElement,
  roiFraction: number = DECODE_PASSES[0]?.roiFraction ?? 0.6,
): ImageData | null {
  const rect = centreRoi(source.videoWidth, source.videoHeight, roiFraction);
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
 * Decode one already-cropped frame with one pass's settings.
 *
 * Returns every valid symbol found (up to {@link MAX_SYMBOLS_PER_FRAME}), not
 * just the first — `maxNumberOfSymbols: 1` on the previous version silently
 * discarded a second barcode in the same frame.
 */
export async function decodeImageData(image: ImageData, pass: DecodePass): Promise<Decoded[]> {
  prepare();
  const results = await readBarcodes(image, {
    formats: [...pass.formats],
    tryHarder: pass.tryHarder,
    // Code 128 and EAN are not rotation-invariant and nobody holds a reel
    // square to the camera.
    tryRotate: true,
    tryInvert: true,
    maxNumberOfSymbols: MAX_SYMBOLS_PER_FRAME,
    ...(pass.tryDownscale === undefined ? {} : { tryDownscale: pass.tryDownscale }),
  });
  const decoded: Decoded[] = [];
  for (const result of results) {
    if (result.isValid && result.text !== "") {
      decoded.push({ text: result.text, symbology: result.format });
    }
  }
  return decoded;
}
