/**
 * Reading the *printed* half of a label — the part no barcode encodes.
 *
 * ## Why this runs in the browser
 *
 * The commit that taught the camera to escalate its decode passes left a note
 * saying the server half — reading text — was deliberately absent, and belonged
 * in a worker under ADR 0005. That was the right call for **datasheets** and is
 * the wrong one here, for two reasons that only became clear once the feature
 * had a shape:
 *
 * 1. **The existing contract has no geometry.** `ExtractedText` is pages of
 *    plain strings, deliberately, because per-page character counts are the
 *    escalation signal for a PDF. An outline the user can tap needs a box per
 *    line, so riding that queue would mean widening a contract built for a
 *    different question.
 * 2. **Its worker does not exist yet, and is allowed not to.** ADR 0005's own
 *    load-bearing consequence is that the extraction stack may be absent
 *    indefinitely. For a datasheet that is fine — only search over its contents
 *    waits. For a person standing at a shelf holding a reel, "the text will be
 *    readable at some unspecified future point" is the same as never.
 *
 * So this follows the precedent the scanner already set — `zxing-wasm` decodes
 * in the browser, `images/resize.ts` downsamples in the browser, and the API
 * checks five bytes of magic and touches no pixels. The server stores an
 * interpretation; it never performs one.
 *
 * ## Why absence is a first-class state
 *
 * Everything below is loaded lazily and is allowed to fail. The model is ~2 MB
 * and the wasm core ~3 MB; a stripped WebView, a browser without the APIs, or a
 * first use before those assets are cached all end at "no OCR". Every one of
 * those returns `unavailable` rather than throwing, because the barcode regions
 * are already on screen and useful, and a capture that reads its DataMatrix and
 * not its ink is a *good* outcome that must not be presented as a failure.
 * `unavailable` and `empty` are kept apart for the same reason the server keeps
 * them apart: "we could not look" and "there was nothing to read" call for
 * different sentences.
 *
 * ## Why the assets are self-hosted
 *
 * `tesseract.js` defaults all three of its runtime assets to a jsDelivr CDN.
 * `decoder.ts` already refused that for `zxing-wasm` — this deployment is a LAN
 * behind a private CA with no promise of internet access. `vite.config.ts`
 * serves the worker and core out of `node_modules` and the model out of
 * `public/tessdata/`; the paths below are the other half of that arrangement.
 */

import type { TextRegion, TextStatus } from "./types";
import { boxToQuad } from "./types";

/** Mirrors `OCR_BASE` in `vite.config.ts`. */
const CORE_PATH = "/ocr";
/** Where `public/tessdata/eng.traineddata.gz` is served from. */
const LANG_PATH = "/tessdata";
const WORKER_PATH = `${CORE_PATH}/worker.min.js`;

/**
 * Lines below this are noise, not readings.
 *
 * Tesseract emits low-confidence garbage for label edges, barcode bars read as
 * characters, and JPEG artefacts around small glyphs. Those become tappable
 * chips if let through, and a chip that fills a field with `|1l|` is worse than
 * no chip. 55 is deliberately permissive — a genuinely blurry MPN the user can
 * still confirm by eye is worth offering, since **they** decide, not this
 * threshold.
 */
export const MIN_LINE_CONFIDENCE = 55;

/** Runs of punctuation and stray marks that carry no value worth copying. */
const MEANINGFUL = /[A-Za-z0-9]/;

export interface OcrOutcome {
  readonly status: TextStatus;
  readonly regions: readonly TextRegion[];
  /** Set when `status` is `unavailable` or `failed`; safe to show the user. */
  readonly message?: string;
}

/**
 * A worker is expensive to start (it fetches and instantiates several MB) and
 * cheap to keep, so one is reused for the life of the page. Held as a promise
 * rather than a value so two captures in quick succession share one
 * initialisation instead of racing to build two.
 */
let workerPromise: Promise<OcrWorker> | null = null;

/**
 * What Tesseract will actually accept as an image.
 *
 * **Not `ImageBitmap`**, which is the obvious thing to pass here and does not
 * work: `tesseract.js`'s `ImageLike` is `string | HTMLImageElement |
 * HTMLCanvasElement | HTMLVideoElement | CanvasRenderingContext2D | File | Blob
 * | Buffer | OffscreenCanvas`, and an `ImageBitmap` is none of them. Handing it
 * one throws inside the worker, which surfaces here as `failed` — a capture
 * whose barcodes read perfectly and whose text never appears.
 *
 * So the still's own JPEG `Blob` is what gets passed. `zxing-wasm` needs pixels
 * and Tesseract needs a file; the two readers want genuinely different things
 * from the same capture, and pretending otherwise is what caused the bug.
 */
export type OcrImage = Blob | HTMLCanvasElement;

/**
 * The slice of `tesseract.js` this module uses, named locally.
 *
 * Not `import type { Worker } from "tesseract.js"` — that would make the type
 * position a static import of a package that must only ever load lazily, and
 * bundlers have historically been willing to pull the runtime in behind one.
 * Declaring the shape costs six lines and keeps the dynamic import genuinely
 * dynamic.
 *
 * The cost of that choice, paid once already: a locally-declared signature is
 * only as correct as the person writing it. This one originally said
 * `ImageBitmap | Blob`, so the compiler cheerfully accepted the one input the
 * library cannot take. Hence `OcrImage` above, written from `ImageLike` rather
 * than from memory.
 */
interface OcrWorker {
  recognize(
    image: OcrImage,
    options?: unknown,
    output?: { blocks?: boolean; text?: boolean },
  ): Promise<{ data: { blocks: OcrBlock[] | null } }>;
}

interface OcrBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

interface OcrLine {
  text: string;
  confidence: number;
  bbox: OcrBox;
}

interface OcrBlock {
  paragraphs: { lines: OcrLine[] }[];
}

async function getWorker(): Promise<OcrWorker> {
  workerPromise ??= (async () => {
    const { createWorker } = await import("tesseract.js");
    // `legacyCore`/`legacyLang` are left off: the legacy Tesseract engine needs
    // a different and much larger model, and the LSTM engine is both smaller and
    // better on the printed sans-serif that labels actually use.
    return (await createWorker("eng", 1, {
      workerPath: WORKER_PATH,
      corePath: CORE_PATH,
      langPath: LANG_PATH,
      // The vendored model is the `.gz` from `tessdata_fast`.
      gzip: true,
    })) as unknown as OcrWorker;
  })();
  return workerPromise;
}

/**
 * Read every legible line, or say honestly why not.
 *
 * Never throws. See the module comment: the barcode regions are already drawn
 * and useful by the time this is called, so any failure here degrades the
 * capture rather than breaking it.
 */
export async function readTextRegions(image: OcrImage): Promise<OcrOutcome> {
  let worker: OcrWorker;
  try {
    worker = await getWorker();
  } catch (cause) {
    // The assets could not be fetched or instantiated at all. A fact about this
    // browser and this deployment, not about the image — so re-capturing would
    // not help, and the message says so.
    workerPromise = null;
    return {
      status: "unavailable",
      regions: [],
      message: describe(cause, "The text reader could not be loaded on this device."),
    };
  }

  try {
    // `blocks: true` is what makes per-line boxes come back at all; the default
    // output is the flat string, which would give text with nothing to outline.
    const result = await worker.recognize(image, undefined, { blocks: true, text: false });
    const regions = linesOf(result.data.blocks ?? []);
    return { status: regions.length > 0 ? "ok" : "empty", regions };
  } catch (cause) {
    // The pass ran and threw. Distinct from `unavailable` because retrying this
    // one is reasonable.
    return {
      status: "failed",
      regions: [],
      message: describe(cause, "Reading the text failed."),
    };
  }
}

function linesOf(blocks: readonly OcrBlock[]): TextRegion[] {
  const regions: TextRegion[] = [];
  for (const block of blocks) {
    for (const paragraph of block.paragraphs) {
      for (const line of paragraph.lines) {
        // Tesseract keeps the trailing newline on every line, and a chip
        // labelled "MURATA\n" copies a stray newline into whatever field it
        // fills.
        const text = line.text.trim();
        if (text === "" || !MEANINGFUL.test(text) || line.confidence < MIN_LINE_CONFIDENCE) {
          continue;
        }
        regions.push({
          kind: "text",
          text,
          quad: boxToQuad(
            Math.round(line.bbox.x0),
            Math.round(line.bbox.y0),
            Math.round(line.bbox.x1),
            Math.round(line.bbox.y1),
          ),
          confidence: Math.round(line.confidence),
        });
      }
    }
  }
  return regions;
}

function describe(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message !== "" ? cause.message : fallback;
}

/** Test seam: drop the memoised worker so a case can install its own. */
export function resetOcrWorkerForTests(): void {
  workerPromise = null;
}
