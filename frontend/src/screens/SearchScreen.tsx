/**
 * Parametric search — the DigiKey-style question, asked of your own shelves.
 *
 * The filter rows are `template` + a raw value string, exactly as the API takes
 * them: the shorthand grammar (`4k7`, `20-30uF`, `>=50V`) is parsed server-side
 * *with template context*, because the same text means different things under
 * different quantities and guessing is what the design forbids.
 *
 * That is also why a 422 here gets careful treatment. The server answers an
 * uninterpretable value with a `reason` code, and `implausible` under capacitance
 * is the case worth having: `1M` is syntactically perfect and means megafarads,
 * and telling the user *that* is far more use than "invalid input".
 *
 * The whole query round-trips through the querystring in the same `f=template:value`
 * form the GET alias accepts, so a search is a link you can send someone.
 */

import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ErrorBanner, Loading } from "../components/Feedback";
import { searchParts, type SearchFilter, type SearchRequest, type SearchResponse } from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";

/**
 * Hints for the template field, not a source of truth.
 *
 * No endpoint enumerates `parameter_template` yet, so this is a `datalist` of the
 * names the demo seed creates. It is a typing aid and nothing more — any template
 * name the backend knows works whether or not it appears here, and an unknown one
 * comes back as a 400 naming itself.
 */
const TEMPLATE_HINTS = [
  "resistance",
  "capacitance",
  "inductance",
  "voltage_rating",
  "current_rating",
  "power_rating",
  "tolerance",
  "mounting_type",
  "package",
  "dielectric",
  "capacitor_technology",
] as const;

interface FormState {
  readonly text: string;
  readonly category: string;
  readonly partKind: string;
  readonly filters: readonly SearchFilter[];
  readonly inStockOnly: boolean;
  readonly includeStubs: boolean;
  readonly mode: "search" | "substitute";
}

function stateFromParams(params: URLSearchParams): FormState {
  const filters = params
    .getAll("f")
    .map((raw) => {
      const at = raw.indexOf(":");
      return at <= 0
        ? null
        : { template: raw.slice(0, at).trim(), value: raw.slice(at + 1).trim() };
    })
    .filter((filter): filter is SearchFilter => filter !== null && filter.value !== "");

  return {
    text: params.get("text") ?? "",
    category: params.get("category") ?? "",
    partKind: params.get("part_kind") ?? "",
    filters: filters.length > 0 ? filters : [{ template: "", value: "" }],
    inStockOnly: params.get("in_stock_only") === "1",
    includeStubs: params.get("include_stubs") !== "0",
    mode: params.get("mode") === "substitute" ? "substitute" : "search",
  };
}

function paramsFromState(state: FormState): URLSearchParams {
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
  return params;
}

function requestFromParams(params: URLSearchParams): SearchRequest | null {
  const state = stateFromParams(params);
  const filters = state.filters.filter(
    (filter) => filter.template.trim() !== "" && filter.value.trim() !== "",
  );
  if (state.text.trim() === "" && filters.length === 0 && state.category.trim() === "") {
    return null;
  }
  return {
    ...(state.text.trim() === "" ? {} : { text: state.text.trim() }),
    ...(state.category.trim() === "" ? {} : { category: state.category.trim() }),
    ...(state.partKind.trim() === "" ? {} : { part_kind: state.partKind.trim() }),
    filters,
    in_stock_only: state.inStockOnly,
    include_stubs: state.includeStubs,
    mode: state.mode,
    limit: 50,
  };
}

export function SearchScreen() {
  const [params, setParams] = useSearchParams();
  const [form, setForm] = useState<FormState>(() => stateFromParams(params));
  const [showFilters, setShowFilters] = useState(() => params.getAll("f").length > 0);

  // The URL is the query. Editing the form does not search; submitting rewrites
  // the querystring, and the querystring is what is fetched.
  const key = params.toString();
  const request = useMemo(() => requestFromParams(new URLSearchParams(key)), [key]);
  const results = useAsync<SearchResponse | null>(
    () => (request === null ? Promise.resolve(null) : searchParts(request)),
    [key],
  );

  function patch(next: Partial<FormState>): void {
    setForm({ ...form, ...next });
  }

  function setFilter(index: number, next: SearchFilter): void {
    patch({ filters: form.filters.map((filter, at) => (at === index ? next : filter)) });
  }

  return (
    <div className="stack">
      <form
        className="card"
        onSubmit={(event) => {
          event.preventDefault();
          setParams(paramsFromState(form));
        }}
      >
        <label className="field">
          <span>Text</span>
          <input
            value={form.text}
            onChange={(event) => patch({ text: event.target.value })}
            placeholder="name, MPN, keywords"
            enterKeyHint="search"
            autoComplete="off"
          />
        </label>

        <div className="row">
          <button type="button" onClick={() => setShowFilters(!showFilters)}>
            {showFilters ? "Hide filters" : `Filters${countFilters(form) > 0 ? ` (${countFilters(form)})` : ""}`}
          </button>
          <span className="spacer" />
          <button type="submit" className="primary">
            Search
          </button>
        </div>

        {showFilters && (
          <div className="stack">
            <datalist id="template-hints">
              {TEMPLATE_HINTS.map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>

            {form.filters.map((filter, index) => (
              <div className="row" key={index}>
                <label className="field">
                  <span>Parameter</span>
                  <input
                    list="template-hints"
                    value={filter.template}
                    onChange={(event) => setFilter(index, { ...filter, template: event.target.value })}
                    placeholder="capacitance"
                    autoComplete="off"
                    spellCheck={false}
                  />
                </label>
                <label className="field">
                  <span>Value</span>
                  <input
                    className="mono"
                    value={filter.value}
                    onChange={(event) => setFilter(index, { ...filter, value: event.target.value })}
                    placeholder="20-30uF"
                    autoComplete="off"
                    spellCheck={false}
                  />
                </label>
                <button
                  type="button"
                  aria-label="remove this filter"
                  onClick={() =>
                    patch({
                      filters:
                        form.filters.length === 1
                          ? [{ template: "", value: "" }]
                          : form.filters.filter((_, at) => at !== index),
                    })
                  }
                >
                  ✕
                </button>
              </div>
            ))}

            <p className="muted-note">
              Values take the shorthand grammar: a scalar (<span className="mono">4k7</span>,{" "}
              <span className="mono">0R22</span>, <span className="mono">100nF</span>), a range (
              <span className="mono">20-30uF</span>) or a comparison (
              <span className="mono">&gt;=50V</span>). Enum parameters take a choice key or any of
              its aliases, comma-separated for OR. Case matters —{" "}
              <span className="mono">m</span> is milli and <span className="mono">M</span> is mega.
            </p>

            <div className="row">
              <button
                type="button"
                onClick={() => patch({ filters: [...form.filters, { template: "", value: "" }] })}
              >
                + Add filter
              </button>
            </div>

            <div className="row">
              <label className="field">
                <span>Category slug</span>
                <input
                  value={form.category}
                  onChange={(event) => patch({ category: event.target.value })}
                  placeholder="capacitor"
                  autoComplete="off"
                />
              </label>
              <label className="field">
                <span>Part kind</span>
                <input
                  value={form.partKind}
                  onChange={(event) => patch({ partKind: event.target.value })}
                  placeholder="component"
                  autoComplete="off"
                />
              </label>
            </div>

            <div className="row">
              <label className="check">
                <input
                  type="checkbox"
                  checked={form.inStockOnly}
                  onChange={(event) => patch({ inStockOnly: event.target.checked })}
                />
                In stock only
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={form.includeStubs}
                  onChange={(event) => patch({ includeStubs: event.target.checked })}
                />
                Include stubs
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={form.mode === "substitute"}
                  onChange={(event) =>
                    patch({ mode: event.target.checked ? "substitute" : "search" })
                  }
                />
                Find substitutes
              </label>
            </div>

            {form.mode === "substitute" && (
              <p className="muted-note">
                Substitution runs the same SQL filter with each parameter&apos;s
                <span className="mono"> substitution_direction</span> applied, so a higher voltage
                rating satisfies a lower requirement and never the other way round. It is
                deterministic by construction — nothing here is inferred.
              </p>
            )}
          </div>
        )}
      </form>

      <ErrorBanner error={results.error} fallback="That search could not be run." />

      {request === null && (
        <p className="dim">
          Enter some text, a category, or at least one parameter filter.
        </p>
      )}

      {request !== null && results.loading && <Loading what="results" />}

      {results.data !== null && (
        <>
          <p className="muted-note">
            {results.data.total === 0
              ? "Nothing matched."
              : `Showing ${results.data.results.length} of ${results.data.total}.`}
          </p>
          <ul className="list">
            {results.data.results.map((part) => (
              <li key={part.id}>
                <Link className="list-item" to={`/parts/${part.id}`}>
                  <div className="title">{part.name}</div>
                  <div className="sub">
                    {part.mpn !== null && <span className="mono">{part.mpn}</span>}
                    {part.mpn !== null && part.description !== null && " · "}
                    {part.description}
                  </div>
                  {part.is_stub && <span className="badge badge-warn">stub</span>}
                </Link>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function countFilters(form: FormState): number {
  return form.filters.filter(
    (filter) => filter.template.trim() !== "" && filter.value.trim() !== "",
  ).length;
}
