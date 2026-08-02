/**
 * The rule that stops a chip from carrying a heading and its value at once.
 *
 * These read as arithmetic because they are: the cut is decided by the gap
 * between two word boxes measured against the line height, so every case here
 * is a statement about geometry rather than about vocabulary.
 */

import { describe, expect, it } from "vitest";

import { segmentWords, COLUMN_GAP_RATIO, type OcrWord } from "./segment";

const H = 20; // line height for every fixture, so gaps read as multiples of it

/** Lay words out left to right, with an explicit gap before each after the first. */
function line(...words: [string, number, number?][]): OcrWord[] {
  let x = 0;
  return words.map(([text, gap, confidence], index) => {
    x += index === 0 ? 0 : gap;
    const width = text.length * 10;
    const word: OcrWord = {
      text,
      confidence: confidence ?? 90,
      bbox: { x0: x, y0: 0, x1: x + width, y1: H },
    };
    x += width;
    return word;
  });
}

describe("segmentWords", () => {
  it("splits a heading from its value at a column gap", () => {
    // The reported problem, exactly: one Tesseract line, two facts.
    const regions = segmentWords(line(["Manufacturer", 0], ["Murata", 60], ["Electronics", 6]), 55);
    expect(regions.map((r) => r.text)).toEqual(["Manufacturer", "Murata Electronics"]);
  });

  it("keeps ordinary word spacing together", () => {
    // A normal space is ~0.3x the line height; splitting here would cut phrases
    // into single words and make every chip useless.
    const regions = segmentWords(line(["22uF", 0], ["16V", 6], ["ceramic", 6]), 55);
    expect(regions).toHaveLength(1);
    expect(regions[0]?.text).toBe("22uF 16V ceramic");
  });

  it("does not split a value that contains a space, like a quantity", () => {
    const regions = segmentWords(line(["QTY", 0], ["5000", 8]), 55);
    expect(regions.map((r) => r.text)).toEqual(["QTY 5000"]);
  });

  it("cuts at exactly the documented ratio and not below it", () => {
    const under = segmentWords(line(["A", 0], ["B", H * COLUMN_GAP_RATIO - 1]), 55);
    const over = segmentWords(line(["A", 0], ["B", H * COLUMN_GAP_RATIO + 1]), 55);
    expect(under).toHaveLength(1);
    expect(over).toHaveLength(2);
  });

  it("strips the leader dots that tie a heading to its value", () => {
    // `Description ......... 22uF` — the dots belong to neither side.
    const regions = segmentWords(line(["Description", 0], [".........", 6], ["22uF", 60]), 55);
    expect(regions.map((r) => r.text)).toEqual(["Description", "22uF"]);
  });

  it("drops a segment that is only punctuation", () => {
    expect(segmentWords(line(["...", 0]), 55)).toHaveLength(0);
    expect(segmentWords(line(["|", 0], ["—", 60]), 55)).toHaveLength(0);
  });

  it("scores a segment by its worst word, not its average", () => {
    // A confident `QTY` must not carry a doubtful `5OOO` over the threshold:
    // the segment is pasted as one value, so it is only as good as its weakest
    // character. The mean here would be 72 and would have been shown.
    const regions = segmentWords(line(["QTY", 0, 95], ["5OOO", 8, 49]), 55);
    expect(regions).toHaveLength(0);
  });

  it("keeps a segment whose words are all above the floor", () => {
    const regions = segmentWords(line(["QTY", 0, 95], ["5000", 8, 60]), 55);
    expect(regions[0]?.confidence).toBe(60);
  });

  it("gives each segment a box around only its own words", () => {
    // The outline has to sit over the value, not over the whole line, or the
    // user cannot tell which half of the row they are about to copy.
    const [heading, value] = segmentWords(line(["Mfr", 0], ["Murata", 60]), 55);
    expect(heading?.quad[0]).toEqual({ x: 0, y: 0 });
    expect(heading?.quad[2]).toEqual({ x: 30, y: H });
    expect(value?.quad[0]).toEqual({ x: 90, y: 0 });
  });

  it("handles an empty line without inventing a region", () => {
    expect(segmentWords([], 55)).toEqual([]);
    expect(segmentWords(line(["   ", 0]), 55)).toEqual([]);
  });
});
