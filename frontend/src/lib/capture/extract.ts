/**
 * Reading a label the way a person does: find the heading, take what is under it.
 *
 * A distributor label is a form. `Manufacturer` sits above
 * `STACKPOLE ELECTRONICS INC`; `Quantity` sits above `100`. Once lines have been
 * cut into columns (`segment.ts`) those are two separate regions with nothing
 * connecting them, and this module is what puts them back together — by
 * geometry, because that is what the printed layout actually encodes.
 *
 * ## Suggestions, never answers
 *
 * Everything here returns a *ranked list per field*, and the reason is visible in
 * the fixtures. On `digikey-label-26` the OCR pass missed the line
 * `CF14JT100KCT-ND` entirely, so the region under the heading `Part Number` is
 * the manufacturer's part from the row below it. Pairing gives a confident,
 * well-formed, **wrong** answer. There is no way to detect that from the text;
 * the only honest response is to offer it as a candidate and let a person who is
 * holding the label decide.
 *
 * That is the same rule the rest of this feature already follows — a value read
 * off a photograph is a suggestion until someone taps it — and it is why the
 * three rules below matter more than the matching does:
 *
 * 1. **A value that is itself a heading is not a value.** When OCR drops a
 *    field's content, the next region below is the *next* heading. Recognising
 *    that and offering nothing is the difference between "Date Code: (nothing
 *    read)" and "Date Code: Manufacturer".
 * 2. **Barcodes outrank text, always.** A checksummed payload parsed by the ECIA
 *    handler is a fact; an OCR'd line is a guess. Both are offered, in that
 *    order, with their provenance attached.
 * 3. **Headings are matched loosely, values are not touched.** `Pan Number` and
 *    `Pant Description` are real OCR output from these labels, so heading
 *    matching has to tolerate damage. The *value* is passed through exactly as
 *    read — correcting it would be inventing data.
 */

import type { ScanResolveResponse } from "../api/client";
import { formatQty } from "../format";
import type { FillField } from "./chips";
import type { Region, TextRegion } from "./types";
import { bounds } from "./types";

/**
 * How far below a heading its value may start, as a multiple of the heading's
 * own height.
 *
 * Generous, because the gap is set by the label's line spacing and by whatever
 * the OCR pass did with the intervening blank. On the fixtures the real pairs sit
 * between 0.1x and 2.2x. Beyond about three line-heights the "value" is more
 * likely the next row of the form.
 */
export const VALUE_GAP_RATIO = 3;

/**
 * How far a value's left edge may sit from its heading's, in the same units.
 *
 * Distributor labels left-align a value under its heading. A different column
 * entirely is the thing this rejects — `Purchase Order` at x=443 must not adopt
 * `Customer Reference`'s value at x=90.
 */
export const VALUE_ALIGN_RATIO = 2.5;

/** Minimum similarity for a damaged heading to still count as that heading. */
export const HEADING_SIMILARITY = 0.7;

export type SuggestionSource = "barcode" | "text";

export interface Suggestion {
  readonly value: string;
  readonly source: SuggestionSource;
  /** Where it came from, for display: a DI, or the heading it sat under. */
  readonly via: string;
  /** 0-100, text only. */
  readonly confidence?: number;
}

export type Suggestions = Partial<Record<FillField, Suggestion[]>>;

/**
 * Headings seen on real labels, and the field each fills.
 *
 * `null` means "recognised as a heading, but not something we fill" — which is
 * still worth listing, because rule 1 above needs to know that
 * `Customer Reference` is a heading so it is never mistaken for a value.
 */
const HEADINGS: readonly (readonly [string, FillField | null])[] = [
  ["manufacturer part number", "mpn"],
  ["mfr part number", "mpn"],
  ["mfg part number", "mpn"],
  // DigiKey's own ordering code, not the manufacturer's part. Offered as an
  // `mpn` candidate too, because on many labels this row *is* the MPN — see
  // `chips.ts`, which refuses to guess between the two DIs for the same reason.
  ["part number", "supplier_part_number"],
  ["digi-key part number", "supplier_part_number"],
  ["part description", "name"],
  ["description", "name"],
  ["manufacturer", "manufacturer"],
  ["date code", "date_code"],
  ["lot code", "lot_code"],
  ["quantity", "quantity"],
  ["qty", "quantity"],
  ["master part id", null],
  ["customer reference", null],
  ["purchase order", null],
  ["order", null],
  ["country of origin", null],
  ["coo", null],
  ["idx", null],
];

function normalise(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9 ]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Levenshtein distance, iterative and small — the strings here are a few words. */
function distance(a: string, b: string): number {
  if (a === b) {
    return 0;
  }
  let previous = Array.from({ length: b.length + 1 }, (_, index) => index);
  for (let i = 1; i <= a.length; i += 1) {
    const current = [i];
    for (let j = 1; j <= b.length; j += 1) {
      current[j] = Math.min(
        (previous[j] ?? 0) + 1,
        (current[j - 1] ?? 0) + 1,
        (previous[j - 1] ?? 0) + (a[i - 1] === b[j - 1] ? 0 : 1),
      );
    }
    previous = current;
  }
  return previous[b.length] ?? 0;
}

function similarity(a: string, b: string): number {
  const longest = Math.max(a.length, b.length);
  return longest === 0 ? 1 : 1 - distance(a, b) / longest;
}

export interface HeadingMatch {
  readonly heading: string;
  readonly field: FillField | null;
  readonly similarity: number;
}

/**
 * Which known heading this text is, if any.
 *
 * Exported because "is this a heading?" is asked twice — once to find headings,
 * and once to refuse a candidate value that turns out to be the next one.
 */
export function matchHeading(text: string): HeadingMatch | null {
  const normalised = normalise(text);
  if (normalised === "") {
    return null;
  }
  let best: HeadingMatch | null = null;
  for (const [heading, field] of HEADINGS) {
    const score = similarity(normalised, heading);
    if (score >= HEADING_SIMILARITY && (best === null || score > best.similarity)) {
      best = { heading, field, similarity: score };
    }
  }
  return best;
}

/**
 * The value region printed under `heading`, or null when none qualifies.
 *
 * **The nearest region in the column wins, and if that is another heading there
 * is no value.** Written this way rather than as "skip headings and keep
 * looking", which is the obvious form and is wrong: on `digikey-label-26` the
 * OCR pass dropped the date code, so the column under `Date Code` reads
 * `Manufacturer`, then `STACKPOLE ELECTRONICS INC`. Skipping the heading and
 * continuing hands `Date Code` the manufacturer's name — a value that belongs to
 * the heading in between. Stopping at the first thing in the column is what
 * makes an intervening heading act as the boundary it visually is.
 */
function valueUnder(heading: TextRegion, candidates: readonly TextRegion[]): TextRegion | null {
  const box = bounds(heading.quad);
  const height = Math.max(1, box.height);
  let best: { region: TextRegion; gap: number } | null = null;

  for (const candidate of candidates) {
    if (candidate === heading) {
      continue;
    }
    const other = bounds(candidate.quad);
    // Strictly below: a region starting at or above the heading's top is part of
    // the same row, not its value.
    if (other.y <= box.y) {
      continue;
    }
    const gap = other.y - (box.y + box.height);
    if (gap > height * VALUE_GAP_RATIO) {
      continue;
    }
    if (Math.abs(other.x - box.x) > height * VALUE_ALIGN_RATIO) {
      continue;
    }
    if (best === null || gap < best.gap) {
      best = { region: candidate, gap };
    }
  }
  if (best === null || matchHeading(best.region.text) !== null) {
    return null;
  }
  return best.region;
}

function add(into: Suggestions, field: FillField, suggestion: Suggestion): void {
  const list = into[field] ?? [];
  // Same value from the same kind of source twice is one suggestion. A barcode
  // and an OCR pass agreeing is *also* one, and the barcode is already first.
  if (list.some((existing) => existing.value === suggestion.value)) {
    return;
  }
  list.push(suggestion);
  into[field] = list;
}

export interface ExtractInput {
  readonly regions: readonly Region[];
  /** Keyed by region index, as `useCapture` holds them. */
  readonly resolved: Readonly<Record<number, ScanResolveResponse>>;
}

/**
 * Everything this capture suggests, per field, best first.
 *
 * Barcode-derived values are added before any text, so the ordering of the
 * returned lists *is* the ranking — a caller can take `[0]` and be taking the
 * most trustworthy answer available.
 */
export function extractSuggestions({ regions, resolved }: ExtractInput): Suggestions {
  const out: Suggestions = {};

  // --- barcodes first, so they lead every list -----------------------------
  regions.forEach((region, index) => {
    if (region.kind !== "barcode") {
      return;
    }
    const parsed = resolved[index]?.parsed;
    if (parsed === null || parsed === undefined) {
      return;
    }
    const barcode = (field: FillField, value: string | null | undefined, via: string): void => {
      if (value !== null && value !== undefined && value !== "") {
        add(out, field, { value, source: "barcode", via });
      }
    };
    // Both part-number DIs are offered as MPN candidates, in the same order and
    // for the same reason `chips.ts` gives: distributors disagree about which
    // one carries the manufacturer's part, and nothing on the label says which.
    barcode("mpn", parsed.supplier_part_number, "code · 1P");
    barcode("mpn", parsed.mpn, "code · P");
    barcode("supplier_part_number", parsed.mpn, "code · P");
    barcode("manufacturer", parsed.manufacturer, "code · 1V");
    barcode("date_code", parsed.date_code, "code · 9D");
    barcode("lot_code", parsed.lot_code, "code · 1T");
    if (parsed.quantity_milli !== null && parsed.quantity_milli !== undefined) {
      barcode("quantity", formatQty(parsed.quantity_milli), "code · Q");
    }
  });

  // --- then the printed form ------------------------------------------------
  const text = regions.filter((region): region is TextRegion => region.kind === "text");
  for (const region of text) {
    const heading = matchHeading(region.text);
    if (heading === null || heading.field === null) {
      continue;
    }
    const value = valueUnder(region, text);
    if (value === null) {
      continue;
    }
    const suggestion: Suggestion = {
      value: value.text,
      source: "text",
      via: heading.heading,
      ...(value.confidence === undefined ? {} : { confidence: value.confidence }),
    };
    add(out, heading.field, suggestion);
    // A row labelled `Part Number` is the manufacturer's part on plenty of
    // labels and the distributor's code on others, so it is offered for both —
    // last, behind anything the barcode said.
    if (heading.field === "supplier_part_number") {
      add(out, "mpn", suggestion);
    }
    // The description is the only sensible name for a part nobody has named.
    if (heading.field === "name") {
      add(out, "name", suggestion);
    }
  }

  return out;
}
