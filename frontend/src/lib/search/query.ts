/**
 * The search query, as a URL.
 *
 * **The querystring is the state.** Category, text, every parametric filter and
 * the page all live in it, in the same `f=template:value` shape the GET alias
 * accepts (`/api/search/parts?f=capacitance:20-30uF&f=mounting_type:THT`), so a
 * search is a link you can send someone and a back button is an undo. This module
 * is the only thing that knows that encoding, and it is pure — which is what
 * makes the round trip testable without a browser.
 *
 * Values stay **raw text** all the way to the server. The shorthand grammar
 * (`4k7`, `0R22`, `20-30uF`, `>=50V`) is parsed server-side *with template
 * context*, because the same text means different things under different physical
 * quantities and guessing client-side is precisely what the design forbids: `1M`
 * under capacitance is megafarads, and only the backend knows that is implausible.
 * So nothing here interprets a value. It only splits and joins the two ends of a
 * range, and refuses when it cannot do that unambiguously.
 */

import type { FacetsRequest, SearchFilter, SearchRequest } from "../api/client";

/** Matches the API's own default and its `le=500` cap comfortably. */
export const PAGE_SIZE = 50;

export interface SearchState {
  readonly text: string;
  /** Category slug; `""` is "everything". Includes descendants server-side. */
  readonly category: string;
  readonly partKind: string;
  readonly filters: readonly SearchFilter[];
  readonly inStockOnly: boolean;
  readonly includeStubs: boolean;
  readonly mode: "search" | "substitute";
  /** One-based, so `page=1` is absent from the URL. */
  readonly page: number;
}

export const EMPTY_SEARCH: SearchState = {
  text: "",
  category: "",
  partKind: "",
  filters: [],
  inStockOnly: false,
  includeStubs: true,
  mode: "search",
  page: 1,
};

// ------------------------------------------------------------ URL <-> state ----

export function stateFromParams(params: URLSearchParams): SearchState {
  const filters: SearchFilter[] = [];
  for (const raw of params.getAll("f")) {
    const at = raw.indexOf(":");
    if (at <= 0) {
      continue;
    }
    const template = raw.slice(0, at).trim();
    const value = raw.slice(at + 1).trim();
    if (template !== "" && value !== "") {
      filters.push({ template, value });
    }
  }

  const page = Number.parseInt(params.get("page") ?? "1", 10);

  return {
    text: params.get("text") ?? "",
    category: params.get("category") ?? "",
    partKind: params.get("part_kind") ?? "",
    filters,
    inStockOnly: params.get("in_stock_only") === "1",
    includeStubs: params.get("include_stubs") !== "0",
    mode: params.get("mode") === "substitute" ? "substitute" : "search",
    page: Number.isSafeInteger(page) && page >= 1 ? page : 1,
  };
}

/**
 * The canonical encoding: defaults are omitted, so the empty search is the empty
 * querystring and "everything" is a clean `/search`.
 */
export function paramsFromState(state: SearchState): URLSearchParams {
  const params = new URLSearchParams();
  if (state.text.trim() !== "") {
    params.set("text", state.text.trim());
  }
  if (state.category.trim() !== "") {
    params.set("category", state.category.trim());
  }
  if (state.partKind.trim() !== "") {
    params.set("part_kind", state.partKind.trim());
  }
  for (const filter of state.filters) {
    if (filter.template.trim() !== "" && filter.value.trim() !== "") {
      params.append("f", `${filter.template.trim()}:${filter.value.trim()}`);
    }
  }
  if (state.inStockOnly) {
    params.set("in_stock_only", "1");
  }
  if (!state.includeStubs) {
    params.set("include_stubs", "0");
  }
  if (state.mode === "substitute") {
    params.set("mode", "substitute");
  }
  if (state.page > 1) {
    params.set("page", String(state.page));
  }
  return params;
}

// ------------------------------------------------------------- requests ----

/**
 * An empty state is a **valid** request: no text, no category, no filters means
 * "list everything", which the backend orders stock-first. Browsing must not
 * require typing, so there is deliberately no null case here.
 */
export function searchRequestFrom(state: SearchState): SearchRequest {
  return {
    ...(state.text.trim() === "" ? {} : { text: state.text.trim() }),
    ...(state.category.trim() === "" ? {} : { category: state.category.trim() }),
    ...(state.partKind.trim() === "" ? {} : { part_kind: state.partKind.trim() }),
    filters: state.filters.map((filter) => ({ ...filter })),
    in_stock_only: state.inStockOnly,
    include_stubs: state.includeStubs,
    mode: state.mode,
    limit: PAGE_SIZE,
    offset: (state.page - 1) * PAGE_SIZE,
  };
}

/**
 * The same narrowing, minus pagination — the facet counts describe the whole
 * matching set, not the page being looked at.
 *
 * `mode` and `part_kind` are dropped because `FacetsRequest` does not accept
 * them. Under `mode=substitute` the counts therefore describe the equivalent
 * `search`-mode set, which is a real if small mismatch; the alternative would be
 * inventing a request field the API does not have.
 */
export function facetsRequestFrom(state: SearchState): FacetsRequest {
  return {
    ...(state.text.trim() === "" ? {} : { text: state.text.trim() }),
    ...(state.category.trim() === "" ? {} : { category: state.category.trim() }),
    filters: state.filters.map((filter) => ({
      template: filter.template,
      value: filter.value,
    })),
    in_stock_only: state.inStockOnly,
    include_stubs: state.includeStubs,
  };
}

/**
 * A stable identity for the facets request, for use as a fetch key.
 *
 * Two states that narrow identically share a key, so paging through results does
 * **not** re-request the facets, while touching any filter does.
 */
export function facetsKey(state: SearchState): string {
  const request = facetsRequestFrom(state);
  return JSON.stringify([
    request.text ?? "",
    request.category ?? "",
    // Sorted, so `f=a:1&f=b:2` and `f=b:2&f=a:1` are one cache entry: they are
    // the same conjunction and the server answers both identically.
    (request.filters ?? [])
      .map((filter) => `${filter["template"] ?? ""}=${filter["value"] ?? ""}`)
      .sort(),
    request.in_stock_only ?? false,
    request.include_stubs ?? true,
  ]);
}

// --------------------------------------------------------- filter editing ----

export function filterValue(state: SearchState, template: string): string | null {
  return state.filters.find((filter) => filter.template === template)?.value ?? null;
}

export function countFilters(state: SearchState): number {
  return state.filters.length;
}

/**
 * Set, replace or clear one template's filter. Always returns page 1: the parts
 * that were on page 3 of the old query are not the parts on page 3 of the new one.
 */
export function withFilter(
  state: SearchState,
  template: string,
  value: string | null,
): SearchState {
  const trimmed = value?.trim() ?? "";
  const others = state.filters.filter((filter) => filter.template !== template);
  const kept =
    trimmed === ""
      ? others
      : state.filters.some((filter) => filter.template === template)
        ? // Replaced in place, so the URL's filter order — and therefore the
          // panel's — does not jump around as values are edited.
          state.filters.map((filter) =>
            filter.template === template ? { template, value: trimmed } : filter,
          )
        : [...state.filters, { template, value: trimmed }];
  return { ...state, filters: kept, page: 1 };
}

export function withCategory(state: SearchState, category: string): SearchState {
  return { ...state, category, page: 1 };
}

export function withText(state: SearchState, text: string): SearchState {
  return { ...state, text, page: 1 };
}

export function withPage(state: SearchState, page: number): SearchState {
  return { ...state, page: Math.max(1, Math.trunc(page)) };
}

// ------------------------------------------------------- the range grammar ----

export interface RangeText {
  readonly min: string;
  readonly max: string;
}

/** The separators the value parser accepts, in the order it tries them. */
const WORD_SEPARATORS = ["...", "..", "~"] as const;
const COMPARATORS = [">=", "=>", "≥", "⩾", "<=", "=<", "≤", "⩽"] as const;

function isExponentSign(text: string, index: number): boolean {
  const previous = text[index - 1] ?? "";
  const before = text[index - 2] ?? "";
  return (previous === "e" || previous === "E") && /[0-9.]/.test(before);
}

/**
 * Split on the range separator the way the server does, or return `null`.
 *
 * Mirrors `elec_value_parser._split_range`, including why `-` is awkward: it is
 * also a leading sign and an exponent sign, so `-40-125` is one range and
 * `1e-6` is one scalar. More than one candidate is *ambiguous* and refused
 * rather than guessed, exactly as the server refuses it.
 */
export function splitRange(value: string): RangeText | null {
  const text = value.trim();
  for (const separator of WORD_SEPARATORS) {
    const at = text.indexOf(separator);
    if (at > 0) {
      const min = text.slice(0, at).trim();
      const max = text.slice(at + separator.length).trim();
      if (min !== "" && max !== "") {
        return { min, max };
      }
    }
  }

  const candidates: number[] = [];
  for (let index = 1; index < text.length; index += 1) {
    if (text[index] !== "-") {
      continue;
    }
    const previous = text[index - 1] ?? "";
    if (previous === "+" || previous === "-" || isExponentSign(text, index)) {
      continue;
    }
    candidates.push(index);
  }
  if (candidates.length !== 1) {
    return null;
  }
  const at = candidates[0] ?? 0;
  const min = text.slice(0, at).trim();
  const max = text.slice(at + 1).trim();
  return min === "" || max === "" ? null : { min, max };
}

/**
 * Read a filter value back into a min/max pair, or `null` if it is not a range.
 *
 * A bare scalar (`4k7`) deliberately returns `null`: it is an exact value, and
 * rewriting it as `4k7-4k7` would put words in the user's mouth. The panel shows
 * those in a raw text field instead, which also preserves anything exotic the
 * grammar accepts and this does not model.
 */
export function decodeRange(value: string | null): RangeText | null {
  if (value === null) {
    return null;
  }
  const text = value.trim();
  for (const comparator of COMPARATORS) {
    if (text.startsWith(comparator)) {
      const operand = text.slice(comparator.length).trim();
      if (operand === "") {
        return null;
      }
      return comparator.startsWith(">") || comparator === "=>" || comparator === "≥" || comparator === "⩾"
        ? { min: operand, max: "" }
        : { min: "", max: operand };
    }
  }
  return splitRange(text);
}

/**
 * Join a min/max pair into one filter value, or `null` for "no filter".
 *
 * One end only becomes a comparison rather than an open range, because the
 * grammar has no `20-` form. Both ends normally join with `-`, the form the
 * shorthand documents (`20-30uF`); if either end already contains a `-` — a
 * negative temperature, say — `..` is used instead, since the parser tries the
 * unambiguous separators first and would otherwise see two candidates and refuse.
 */
export function encodeRange({ min, max }: RangeText): string | null {
  const low = min.trim();
  const high = max.trim();
  if (low === "" && high === "") {
    return null;
  }
  if (high === "") {
    return `>=${low}`;
  }
  if (low === "") {
    return `<=${high}`;
  }
  const separator = low.includes("-") || high.includes("-") ? ".." : "-";
  return `${low}${separator}${high}`;
}

// -------------------------------------------------------- the enum grammar ----

/** Enum filters are comma-separated for OR: "ceramic,film" is one facet. */
export function decodeChoices(value: string | null): readonly string[] {
  if (value === null) {
    return [];
  }
  return value
    .split(",")
    .map((token) => token.trim())
    .filter((token) => token !== "");
}

export function encodeChoices(keys: readonly string[]): string | null {
  const kept = keys.map((key) => key.trim()).filter((key) => key !== "");
  return kept.length === 0 ? null : kept.join(",");
}

export function withChoice(
  state: SearchState,
  template: string,
  key: string,
  checked: boolean,
): SearchState {
  const current = decodeChoices(filterValue(state, template));
  const next = checked
    ? current.includes(key)
      ? current
      : [...current, key]
    : current.filter((existing) => existing !== key);
  return withFilter(state, template, encodeChoices(next));
}
