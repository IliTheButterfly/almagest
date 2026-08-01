/**
 * That we ship the OCR files the worker actually asks for.
 *
 * This exists because the first version of the build plugin did not, and nothing
 * caught it. The names are not self-explanatory — `tesseract-core-<v>.wasm.js`
 * is the **self-contained** module the worker fetches (the wasm is inlined as
 * base64, so it is *bigger* than the bare `.wasm` next to it), while
 * `tesseract-core-<v>.js` and `tesseract-core-<v>.wasm` are never requested at
 * all. Reading that backwards shipped two files nobody wants and omitted the
 * only one that matters.
 *
 * **The failure was completely silent.** The build succeeded, `dist/ocr/` looked
 * populated, and the app fetched `index.html` in place of the core — Vite's SPA
 * fallback answers 200 for any unmatched path, so even a status-code check would
 * have passed. It surfaced only as "the text reader could not be loaded" on a
 * phone, which is the one place it is expensive to discover.
 *
 * So this asserts against `worker.min.js` itself rather than a list written out
 * here: a hardcoded expectation would be one more thing to keep in step with the
 * package, which is the same class of mistake. If a future version renames its
 * cores, this fails at `pnpm test` instead of at a shelf.
 */

import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = join(HERE, "..", "..", "..");
const CORE_DIR = join(FRONTEND, "node_modules", "tesseract.js-core");
const WORKER = join(FRONTEND, "node_modules", "tesseract.js", "dist", "worker.min.js");
const VITE_CONFIG = readFileSync(join(FRONTEND, "vite.config.ts"), "utf8");

/** Every core filename `worker.min.js` can ask for, read out of the bundle. */
function coresTheWorkerRequests(): string[] {
  const source = readFileSync(WORKER, "utf8");
  return [...new Set(source.match(/tesseract-core[\w.-]*\.js/g) ?? [])];
}

/** What `ocrRuntimeFiles()` in vite.config.ts would select, by the same rule. */
function shipped(): string[] {
  return readdirSync(CORE_DIR).filter(
    (name) => name.endsWith(".wasm.js") && name.includes("-lstm"),
  );
}

describe("the OCR runtime we ship", () => {
  it("has the packages installed at all", () => {
    // `tesseract.js-core` is a *transitive* dependency of `tesseract.js`, and
    // pnpm does not link a transitive package into `node_modules/`. It is a
    // direct dependency for exactly this reason; if that is ever dropped the
    // directory vanishes and the plugin silently copies nothing.
    expect(existsSync(CORE_DIR), `${CORE_DIR} is missing — is tesseract.js-core a direct dep?`).toBe(
      true,
    );
    expect(existsSync(WORKER)).toBe(true);
  });

  it("ships a core for every LSTM variant the worker can request", () => {
    const wanted = coresTheWorkerRequests().filter((name) => name.includes("-lstm"));
    expect(wanted.length).toBeGreaterThan(0);

    const have = shipped();
    for (const name of wanted) {
      expect(have, `worker.min.js requests ${name}, which is not shipped`).toContain(name);
    }
  });

  it("ships only files that exist, so no path resolves to the SPA fallback", () => {
    // The original bug's real sting: a missing asset does not 404 under Vite, it
    // returns index.html with a 200. Anything selected here must be a real file.
    for (const name of shipped()) {
      expect(existsSync(join(CORE_DIR, name)), `${name} was selected but does not exist`).toBe(true);
    }
  });

  it("does not ship the two files the worker never asks for", () => {
    const requested = new Set(coresTheWorkerRequests());
    for (const name of shipped()) {
      expect(requested.has(name)).toBe(true);
    }
    // Bare `.js` and `.wasm` are dead weight — several MB each, never fetched.
    expect(shipped().some((name) => name.endsWith(".wasm"))).toBe(false);
  });

  it("keeps the plugin's filter and this test's copy of it identical", () => {
    // Two statements of one rule, so they are pinned to each other rather than
    // left to drift — the whole point of the test is that a wrong filter is
    // invisible at runtime.
    expect(VITE_CONFIG).toContain('name.endsWith(".wasm.js") && name.includes("-lstm")');
  });

  it("agrees with the paths ocr.ts asks the worker to use", () => {
    const ocr = readFileSync(join(HERE, "ocr.ts"), "utf8");
    expect(ocr).toContain('const CORE_PATH = "/ocr"');
    expect(ocr).toContain('const LANG_PATH = "/tessdata"');
    expect(VITE_CONFIG).toContain('const OCR_BASE = "/ocr"');
    // The model is committed, unlike the cores, so its absence is a repo problem
    // rather than an install one.
    expect(existsSync(join(FRONTEND, "public", "tessdata", "eng.traineddata.gz"))).toBe(true);
  });
});
