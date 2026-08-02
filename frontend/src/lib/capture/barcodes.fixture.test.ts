/**
 * @vitest-environment node
 *
 * The decode ladder, against a real distributor label that defeated one pass.
 *
 * `fixtures/digikey-creased-datamatrix.jpg` is a phone photograph of a DigiKey
 * resistor bag: a 2x2-region ECC200 Data Matrix, creased across the middle, torn
 * at one corner, printed on film that curves. It is the label that prompted this
 * ladder, and it is here for the same reason `tests/fixtures/ecia/*.bin` are the
 * ground truth on the backend — there is no substitute for a real one, and a
 * synthetic symbol is exactly the thing that already passed.
 *
 * **What it is testing is a detection failure, not a decode failure.** With one
 * set of settings zxing returned *no symbol at all*: the creases cut the
 * L-shaped finder pattern, and error correction does not cover the finder, so
 * the detector never locates the symbol to correct it. `tryDenoise` closes those
 * cracks morphologically, and the binarizer and downscale factor decide whether
 * the modules survive sampling.
 *
 * Runs in the `node` environment rather than jsdom: the fixture is read off disk
 * and handed to the wasm reader as a `Blob`, and jsdom's partial `Blob` is not
 * worth working around for a test whose subject is a decoder.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";
import { readBarcodes } from "zxing-wasm/reader";

import { DECODE_ATTEMPTS, MAX_SYMBOLS_PER_CAPTURE } from "./barcodes";

const FIXTURE = join(
  dirname(fileURLToPath(import.meta.url)),
  "fixtures",
  "digikey-creased-datamatrix.jpg",
);

/** The manufacturer part number printed on the label, carried in DI `1P`. */
const MPN = "CF14JT100K";
/** DigiKey's own ordering code, carried in DI `P` — deliberately not the MPN. */
const DIGIKEY_PN = "CF14JT100KCT-ND";

/** Exactly what `readBarcodeRegions` does, minus the DOM types. */
async function runLadder(image: Blob, attempts = DECODE_ATTEMPTS) {
  const found = new Map<string, { format: string; text: string }>();
  for (const attempt of attempts) {
    const results = await readBarcodes(image, {
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
    for (const result of results) {
      if (result.isValid && result.text !== "") {
        found.set(`${result.format} ${result.text}`, {
          format: result.format,
          text: result.text,
        });
      }
    }
  }
  return [...found.values()];
}

describe("a creased DigiKey label", () => {
  const image = new Blob([readFileSync(FIXTURE)]);

  it("is read by the full ladder", async () => {
    const found = await runLadder(image);
    const matrix = found.find((symbol) => symbol.format === "DataMatrix");
    expect(matrix, "the Data Matrix was not found by any rung").toBeDefined();
    expect(matrix?.text).toContain(MPN);
  }, 60_000);

  it("is not read by the first rung alone, which is why the ladder exists", async () => {
    // The regression this guards: someone reasonably decides the extra passes are
    // wasteful and keeps only the cheap one. This label goes back to returning
    // nothing, and the failure is silent — no error, just a capture with no code
    // on it.
    const found = await runLadder(image, DECODE_ATTEMPTS.slice(0, 1));
    expect(found.some((symbol) => symbol.format === "DataMatrix")).toBe(false);
  }, 60_000);

  it("carries the manufacturer part number in 1P and the order code in P", async () => {
    // The payload behind the chip labelling: `1P` is the Stackpole part printed
    // on the label as "Manufacturer Part Number", and `P` is DigiKey's own
    // ordering code. Calling either one "the MPN" without saying which field it
    // came from is how an order code gets filed as a part number.
    const found = await runLadder(image);
    const text = found.find((symbol) => symbol.format === "DataMatrix")?.text ?? "";
    expect(text).toContain(`1P${MPN}`);
    expect(text).toContain(`P${DIGIKEY_PN}`);
    // And the two are genuinely different strings, which is the whole problem.
    expect(MPN).not.toEqual(DIGIKEY_PN);
  }, 60_000);
});
