/**
 * Cutting an OCR'd line into the separate values it actually contains.
 *
 * A Tesseract "line" is a run of glyphs on one baseline, which on a real label
 * is very often **two or three different facts**:
 *
 *     Manufacturer          Murata Electronics
 *     QTY  5000             DC 2438
 *     Description ......... 22uF 16V ceramic
 *
 * Taken whole, each is useless as a value to copy: tapping it puts
 * `Manufacturer          Murata Electronics` into a field that wanted
 * `Murata Electronics`. The heading, the leader dots and the value are all one
 * string, and no amount of trimming the ends fixes the middle.
 *
 * **The cut is geometric, not lexical.** Printed labels separate columns with
 * whitespace far wider than a word space, so the gap between two words is the
 * evidence — measured against the line's own height, which makes it independent
 * of resolution, font size and how close the phone was held. A rule based on
 * *what the words say* ("does this look like a heading?") would need a
 * vocabulary of every field name every distributor prints, and would quietly
 * fail on the first label that used a word nobody listed.
 *
 * Two things this deliberately does not do:
 *
 * - **It does not drop headings.** A segment that is just `Manufacturer` is a
 *   real reading and gets its own chip. It is not useful to copy, but deciding
 *   that for the user means guessing which side of a cut is the "value", and a
 *   label that prints `RC0805FR-0710KL   Yageo` would lose the wrong half.
 * - **It does not join lines.** A wrapped description that spans two lines stays
 *   two regions, because merging them requires assuming a reading order that a
 *   two-column label breaks.
 */

import type { TextRegion } from "./types";
import { boxToQuad } from "./types";

/**
 * A gap wider than this many times the line's height is a column break rather
 * than a word space.
 *
 * A word space in normal type is roughly 0.25-0.35x the line height, so 1.2 is
 * comfortably clear of one while still catching the modest gaps on a cramped
 * label. Erring high is the safer direction: failing to split leaves the value
 * usable-but-noisy, whereas splitting mid-phrase would cut `10 uF` in half.
 */
export const COLUMN_GAP_RATIO = 1.2;

/** Leader characters that tie a heading to its value: `Name ....... Value`. */
const LEADERS = /^[\s.·—–\-_:|]+|[\s.·—–\-_:|]+$/g;

/** At least one thing worth copying. Pure punctuation is not a reading. */
const MEANINGFUL = /[A-Za-z0-9]/;

export interface OcrWord {
  readonly text: string;
  readonly confidence: number;
  readonly bbox: { x0: number; y0: number; x1: number; y1: number };
}

/**
 * Split one line's words into segments at column-width gaps.
 *
 * Confidence per segment is the **minimum** of its words, not the mean: a
 * segment is offered as one value to paste, so it is only as trustworthy as its
 * worst-read character. Averaging would let a confident `QTY` carry a doubtful
 * `5OOO` over the display threshold, which is the exact case where a wrong
 * number looks authoritative.
 */
export function segmentWords(words: readonly OcrWord[], minConfidence: number): TextRegion[] {
  const usable = words.filter((word) => word.text.trim() !== "");
  if (usable.length === 0) {
    return [];
  }

  const groups: OcrWord[][] = [[usable[0]!]];
  for (let index = 1; index < usable.length; index += 1) {
    const word = usable[index]!;
    const previous = usable[index - 1]!;
    const gap = word.bbox.x0 - previous.bbox.x1;
    // Each word's own height, so a line mixing sizes still measures sensibly.
    const height = Math.max(1, Math.max(heightOf(previous), heightOf(word)));
    if (gap > height * COLUMN_GAP_RATIO) {
      groups.push([word]);
    } else {
      groups[groups.length - 1]!.push(word);
    }
  }

  const regions: TextRegion[] = [];
  for (const group of groups) {
    const text = group
      .map((word) => word.text)
      .join(" ")
      .replace(LEADERS, "")
      .trim();
    if (text === "" || !MEANINGFUL.test(text)) {
      continue;
    }
    const confidence = Math.round(Math.min(...group.map((word) => word.confidence)));
    if (confidence < minConfidence) {
      continue;
    }
    regions.push({
      kind: "text",
      text,
      quad: boxToQuad(
        Math.round(Math.min(...group.map((word) => word.bbox.x0))),
        Math.round(Math.min(...group.map((word) => word.bbox.y0))),
        Math.round(Math.max(...group.map((word) => word.bbox.x1))),
        Math.round(Math.max(...group.map((word) => word.bbox.y1))),
      ),
      confidence,
    });
  }
  return regions;
}

function heightOf(word: OcrWord): number {
  return word.bbox.y1 - word.bbox.y0;
}
