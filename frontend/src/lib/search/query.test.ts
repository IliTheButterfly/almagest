/**
 * The querystring *is* the query, so these are the tests that keep a shared link
 * meaning what it meant when it was sent.
 *
 * Three properties matter:
 *
 * 1. **Round trip.** state → params → state has to be the identity, or a back
 *    button silently changes the search.
 * 2. **The facet key changes exactly when the counts would.** Facet counts are
 *    computed against the applied filters, so a stale key shows numbers that
 *    describe a different query — and a key that changes on pagination costs a
 *    request per page for an identical answer.
 * 3. **The range grammar joins and splits the way the server parses.** `-` is
 *    overloaded there (sign, exponent, "to"), and composing an ambiguous value
 *    would turn a valid filter into a 422.
 */

import { describe, expect, it } from "vitest";

import {
  decodeChoices,
  decodeRange,
  encodeChoices,
  encodeRange,
  EMPTY_SEARCH,
  facetsKey,
  facetsRequestFrom,
  filterValue,
  paramsFromState,
  searchRequestFrom,
  splitRange,
  stateFromParams,
  withCategory,
  withChoice,
  withFilter,
  withPage,
  withText,
  type SearchState,
} from "./query";

const parse = (query: string): SearchState => stateFromParams(new URLSearchParams(query));
const encode = (state: SearchState): string => paramsFromState(state).toString();

describe("the URL round trip", () => {
  it("treats an empty querystring as 'everything'", () => {
    expect(parse("")).toEqual(EMPTY_SEARCH);
    // And the empty search encodes back to nothing, so /search stays clean.
    expect(encode(EMPTY_SEARCH)).toBe("");
  });

  it("survives a full query unchanged", () => {
    const query =
      "text=ceramic&category=capacitor&part_kind=component" +
      "&f=capacitance%3A20-30uF&f=mounting_type%3ATHT" +
      "&in_stock_only=1&include_stubs=0&mode=substitute&page=3";

    const state = parse(query);
    expect(state).toEqual({
      text: "ceramic",
      category: "capacitor",
      partKind: "component",
      filters: [
        { template: "capacitance", value: "20-30uF" },
        { template: "mounting_type", value: "THT" },
      ],
      inStockOnly: true,
      includeStubs: false,
      mode: "substitute",
      page: 3,
    });
    expect(parse(encode(state))).toEqual(state);
  });

  it("keeps filter order, because the panel reads it back", () => {
    const state = parse("f=b%3A2&f=a%3A1");
    expect(state.filters.map((filter) => filter.template)).toEqual(["b", "a"]);
    expect(encode(state)).toBe("f=b%3A2&f=a%3A1");
  });

  it("drops malformed and empty filters rather than sending them", () => {
    // `f=` with no colon is not a filter; the GET alias 400s on it.
    const state = parse("f=nocolon&f=%3Anovalue&f=empty%3A&f=good%3A1");
    expect(state.filters).toEqual([{ template: "good", value: "1" }]);
  });

  it("preserves a value containing a colon", () => {
    // Only the first colon separates; the rest belongs to the value.
    expect(parse("f=note%3Aa%3Ab").filters).toEqual([{ template: "note", value: "a:b" }]);
  });

  it("clamps a nonsense page instead of asking for offset NaN", () => {
    expect(parse("page=0").page).toBe(1);
    expect(parse("page=-4").page).toBe(1);
    expect(parse("page=banana").page).toBe(1);
  });
});

describe("the search request", () => {
  it("asks for everything when nothing is set", () => {
    const request = searchRequestFrom(EMPTY_SEARCH);
    expect(request.filters).toEqual([]);
    expect(request.text).toBeUndefined();
    expect(request.category).toBeUndefined();
    expect(request.offset).toBe(0);
    expect(request.limit).toBe(50);
  });

  it("turns the page into an offset", () => {
    expect(searchRequestFrom(withPage(EMPTY_SEARCH, 3)).offset).toBe(100);
  });

  it("omits absent optional fields rather than sending null", () => {
    // `exactOptionalPropertyTypes` is on, and the API's `str | None` fields
    // behave differently for "absent" and "null" in a POST body.
    const request = searchRequestFrom(EMPTY_SEARCH);
    expect("text" in request).toBe(false);
    expect("category" in request).toBe(false);
    expect("part_kind" in request).toBe(false);
  });
});

describe("the facets request", () => {
  it("mirrors the filters the results are narrowed by", () => {
    const state = withFilter(withCategory(EMPTY_SEARCH, "capacitor"), "capacitance", "20-30uF");
    expect(facetsRequestFrom(state)).toEqual({
      category: "capacitor",
      filters: [{ template: "capacitance", value: "20-30uF" }],
      in_stock_only: false,
      include_stubs: true,
    });
  });

  it("rebuilds as each filter is added, so counts follow the narrowing", () => {
    let state = EMPTY_SEARCH;
    const keys = [facetsKey(state)];

    state = withCategory(state, "capacitor");
    keys.push(facetsKey(state));

    state = withChoice(state, "mounting_type", "THT", true);
    keys.push(facetsKey(state));

    state = withFilter(state, "capacitance", "20-30uF");
    keys.push(facetsKey(state));

    state = withText(state, "ceramic");
    keys.push(facetsKey(state));

    expect(new Set(keys).size).toBe(keys.length);
    expect(facetsRequestFrom(state).filters).toEqual([
      { template: "mounting_type", value: "THT" },
      { template: "capacitance", value: "20-30uF" },
    ]);
  });

  it("does not rebuild when only the page changes", () => {
    // Facets describe the whole matching set. Re-requesting them per page would
    // be one wasted round trip per Next click for a byte-identical answer.
    const state = withFilter(EMPTY_SEARCH, "capacitance", "20-30uF");
    expect(facetsKey(withPage(state, 4))).toBe(facetsKey(state));
  });

  it("does not rebuild when only the mode changes", () => {
    // FacetsRequest has no `mode`, so the request really is identical — better a
    // shared key than a second request for the same answer.
    const state = withFilter(EMPTY_SEARCH, "voltage_rating", ">=50V");
    expect(facetsKey({ ...state, mode: "substitute" })).toBe(facetsKey(state));
  });

  it("treats a reordered but equivalent filter set as one key", () => {
    const a = withFilter(withFilter(EMPTY_SEARCH, "a", "1"), "b", "2");
    const b = withFilter(withFilter(EMPTY_SEARCH, "b", "2"), "a", "1");
    expect(facetsKey(a)).toBe(facetsKey(b));
  });

  it("rebuilds when a flag changes", () => {
    const state = withCategory(EMPTY_SEARCH, "resistor");
    expect(facetsKey({ ...state, inStockOnly: true })).not.toBe(facetsKey(state));
    expect(facetsKey({ ...state, includeStubs: false })).not.toBe(facetsKey(state));
  });
});

describe("editing filters", () => {
  it("replaces a template's value in place rather than appending", () => {
    const first = withFilter(EMPTY_SEARCH, "capacitance", "10uF");
    const second = withFilter(withFilter(first, "package", "0805"), "capacitance", "22uF");

    expect(second.filters).toEqual([
      { template: "capacitance", value: "22uF" },
      { template: "package", value: "0805" },
    ]);
  });

  it("removes the filter when the value is cleared", () => {
    const state = withFilter(withFilter(EMPTY_SEARCH, "a", "1"), "b", "2");
    expect(withFilter(state, "a", "").filters).toEqual([{ template: "b", value: "2" }]);
    expect(withFilter(state, "a", null).filters).toEqual([{ template: "b", value: "2" }]);
  });

  it("resets to page 1, because page 3 of the old query is meaningless", () => {
    const state = withPage(withFilter(EMPTY_SEARCH, "a", "1"), 5);
    expect(withFilter(state, "b", "2").page).toBe(1);
    expect(withCategory(state, "resistor").page).toBe(1);
    expect(withText(state, "10k").page).toBe(1);
  });

  it("accumulates enum choices into one comma-separated filter", () => {
    let state = withChoice(EMPTY_SEARCH, "dielectric", "X7R", true);
    state = withChoice(state, "dielectric", "C0G", true);

    expect(filterValue(state, "dielectric")).toBe("X7R,C0G");
    expect(decodeChoices(filterValue(state, "dielectric"))).toEqual(["X7R", "C0G"]);

    state = withChoice(state, "dielectric", "X7R", false);
    expect(filterValue(state, "dielectric")).toBe("C0G");

    state = withChoice(state, "dielectric", "C0G", false);
    expect(filterValue(state, "dielectric")).toBeNull();
  });

  it("does not double-add a choice that is already ticked", () => {
    const once = withChoice(EMPTY_SEARCH, "dielectric", "X7R", true);
    expect(filterValue(withChoice(once, "dielectric", "X7R", true), "dielectric")).toBe("X7R");
  });

  it("round-trips choices through the URL", () => {
    const state = withChoice(withChoice(EMPTY_SEARCH, "d", "X7R", true), "d", "C0G", true);
    expect(decodeChoices(filterValue(parse(encode(state)), "d"))).toEqual(["X7R", "C0G"]);
  });

  it("encodes no choices as no filter", () => {
    expect(encodeChoices([])).toBeNull();
    expect(encodeChoices([" ", ""])).toBeNull();
  });
});

describe("the numeric range grammar", () => {
  it("joins two ends the way the shorthand documents", () => {
    expect(encodeRange({ min: "20", max: "30uF" })).toBe("20-30uF");
    expect(encodeRange({ min: "20uF", max: "30uF" })).toBe("20uF-30uF");
  });

  it("turns one end into a comparison, since '20-' is not grammar", () => {
    expect(encodeRange({ min: "50V", max: "" })).toBe(">=50V");
    expect(encodeRange({ min: "", max: "50V" })).toBe("<=50V");
  });

  it("is nothing when both ends are blank", () => {
    expect(encodeRange({ min: "", max: "  " })).toBeNull();
  });

  it("switches separator when an operand contains a minus", () => {
    // "-40-125" would give the parser two '-' candidates and it refuses to
    // guess, so the unambiguous separator is used instead.
    expect(encodeRange({ min: "-40", max: "125" })).toBe("-40..125");
    expect(splitRange("-40..125")).toEqual({ min: "-40", max: "125" });
  });

  it("round-trips every form it produces", () => {
    for (const range of [
      { min: "20", max: "30uF" },
      { min: "1k", max: "10k" },
      { min: "50V", max: "" },
      { min: "", max: "100nF" },
      { min: "-40", max: "125" },
    ]) {
      const encoded = encodeRange(range);
      expect(encoded, JSON.stringify(range)).not.toBeNull();
      const decoded = decodeRange(encoded);
      expect(decoded, encoded ?? "").toEqual(range);
    }
  });

  it("reads the comparison forms the server accepts", () => {
    expect(decodeRange(">=50V")).toEqual({ min: "50V", max: "" });
    expect(decodeRange("≥50V")).toEqual({ min: "50V", max: "" });
    expect(decodeRange("<=50V")).toEqual({ min: "", max: "50V" });
    expect(decodeRange("≤50V")).toEqual({ min: "", max: "50V" });
  });

  it("reads the range separators the server tries first", () => {
    expect(decodeRange("20..30uF")).toEqual({ min: "20", max: "30uF" });
    expect(decodeRange("20~30uF")).toEqual({ min: "20", max: "30uF" });
  });

  it("leaves a scalar alone rather than inventing a range from it", () => {
    // `4k7` is an exact value. Rewriting it as `4k7-4k7` would change what the
    // user typed, so the panel shows it raw instead.
    for (const scalar of ["4k7", "0R22", "100nF", "1e-6", "-40"]) {
      expect(decodeRange(scalar), scalar).toBeNull();
    }
  });

  it("refuses an ambiguous split exactly where the server does", () => {
    // Two candidate separators: the server raises `ambiguous_range` rather than
    // picking one, and neither does this.
    expect(splitRange("1-2-3")).toBeNull();
    expect(decodeRange("1-2-3")).toBeNull();
  });

  it("does not mistake an exponent sign for a range", () => {
    expect(splitRange("1e-6")).toBeNull();
    expect(splitRange("1e-6-1e-3")).toEqual({ min: "1e-6", max: "1e-3" });
  });

  it("does not mistake a leading sign for a range", () => {
    expect(splitRange("-40")).toBeNull();
    expect(splitRange("-40-125")).toEqual({ min: "-40", max: "125" });
  });
});
