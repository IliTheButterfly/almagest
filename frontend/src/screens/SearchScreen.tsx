/**
 * Search: browse everything by default, then narrow like DigiKey.
 *
 * **Landing here with no query lists the whole catalogue**, paginated and ordered
 * stock-first by the server. That is deliberate: an inventory you have to
 * describe before it will show you anything is one you stop opening, and the
 * search box was the previous version's mandatory gate. Typing is now one of
 * three ways in, alongside the category rail and the facet panel.
 *
 * Three requests, on three different keys:
 *
 * - the **results**, keyed on the whole query including the page;
 * - the **facets**, keyed on the narrowing only (`facetsKey`) — so paging does
 *   not re-request counts that describe the same set, while touching any filter
 *   does, because the counts are computed against the filters already applied;
 * - the **categories**, once.
 *
 * The querystring is the query (see lib/search/query.ts), so a search is a
 * shareable link and the back button is an undo. The URL is written with
 * `replace`, though: a debounced text field would otherwise stack thirty history
 * entries per word and turn Back into "delete one character".
 *
 * A 422 gets careful treatment. The server answers an uninterpretable value with
 * a machine-readable `reason` and the offending `template`, and the useful case
 * is `implausible`: `1M` under capacitance is syntactically perfect and means
 * megafarads, so the panel says *that* rather than "invalid input", against the
 * row that caused it.
 */

import { useEffect, useMemo, useRef } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { CategoryRail } from "../components/CategoryRail";
import { FacetPanel } from "../components/FacetPanel";
import { ErrorBanner, Loading } from "../components/Feedback";
import {
  getParameterFacets,
  listPartCategories,
  searchParts,
  type CategoryNode,
  type FacetsResponse,
  type SearchResponse,
} from "../lib/api/client";
import { describeError } from "../lib/api/errors";
import { useAsync } from "../lib/hooks/useAsync";
import { useDeferredCommit } from "../lib/hooks/useDeferredCommit";
import { useMediaQuery } from "../lib/hooks/useMediaQuery";
import {
  facetsKey,
  facetsRequestFrom,
  paramsFromState,
  searchRequestFrom,
  stateFromParams,
  withPage,
  withText,
  PAGE_SIZE,
  type SearchState,
} from "../lib/search/query";

/** Long enough to swallow a word being typed, short enough not to feel stuck. */
const COMMIT_DELAY_MS = 300;

/** The same breakpoint `.search-layout` uses for its two-column grid. */
const WIDE = "(min-width: 52rem)";

export function SearchScreen() {
  const [params, setParams] = useSearchParams();
  const urlKey = params.toString();
  const wide = useMediaQuery(WIDE);

  const { draft, applied, commit, adopt } = useDeferredCommit<SearchState>(
    useMemo(() => stateFromParams(params), []), // eslint-disable-line react-hooks/exhaustive-deps
    COMMIT_DELAY_MS,
  );

  /**
   * The last querystring this screen wrote.
   *
   * Without it, the URL write below would race the adopt effect: the debounce
   * fires for "cer", the URL changes, and the adopt effect would reset a draft
   * that has since become "ceramic". Comparing against what we wrote makes the
   * effect fire only for changes that came from *outside* — a pasted link, a
   * back button, a tapped tag.
   */
  const written = useRef<string>(paramsFromState(applied).toString());

  useEffect(() => {
    if (urlKey !== written.current) {
      written.current = urlKey;
      adopt(stateFromParams(new URLSearchParams(urlKey)));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlKey]);

  useEffect(() => {
    const encoded = paramsFromState(applied).toString();
    if (encoded !== urlKey) {
      written.current = encoded;
      setParams(new URLSearchParams(encoded), { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applied]);

  const searchKey = paramsFromState(applied).toString();
  const results = useAsync<SearchResponse>(() => searchParts(searchRequestFrom(applied)), [
    searchKey,
  ]);
  const facetKey = facetsKey(applied);
  const facets = useAsync<FacetsResponse>(() => getParameterFacets(facetsRequestFrom(applied)), [
    facetKey,
  ]);
  const categories = useAsync<CategoryNode[]>(() => listPartCategories(), []);

  // A filter value the server could not interpret. `template` is what makes it
  // placeable against the row that caused it rather than only at the top.
  const problem = useMemo(() => {
    const error = results.error ?? facets.error;
    return error === null || error === undefined ? null : describeError(error);
  }, [results.error, facets.error]);

  function change(next: SearchState, immediate = false): void {
    commit(next, { immediate });
  }

  const side = (
    <>
      <CategoryRail
        categories={categories.data}
        selected={draft.category}
        onSelect={(slug) => change({ ...draft, category: slug, page: 1 }, true)}
      />
      <FacetPanel
        facets={facets.data}
        state={draft}
        onChange={change}
        invalidTemplate={problem?.template ?? null}
        invalidMessage={problem?.headline ?? null}
      />
      <Options state={draft} onChange={change} />
    </>
  );

  return (
    <div className="stack">
      <div className="card">
        <label className="field">
          <span>Search text</span>
          <input
            value={draft.text}
            onChange={(event) => change(withText(draft, event.target.value))}
            placeholder="name, MPN, keywords — or leave it empty and browse"
            enterKeyHint="search"
            autoComplete="off"
            type="search"
          />
        </label>
        <p className="muted-note">
          Nothing typed lists everything, stock first. Narrow with the type rail and the
          filters; every count below is against what you have already picked.
        </p>
      </div>

      <div className="search-layout">
        {wide ? (
          <aside className="search-side stack">{side}</aside>
        ) : (
          <details className="card">
            <summary>
              Browse by type and filter
              {draft.filters.length > 0 ? ` (${draft.filters.length} active)` : ""}
            </summary>
            <div className="stack" style={{ marginTop: "0.6rem" }}>
              {side}
            </div>
          </details>
        )}

        <div className="stack">
          <ErrorBanner error={results.error ?? facets.error} />
          <Results
            state={applied}
            results={results.data}
            loading={results.loading}
            onPage={(page) => change(withPage(applied, page), true)}
          />
        </div>
      </div>
    </div>
  );
}

function Options({
  state,
  onChange,
}: {
  state: SearchState;
  onChange: (next: SearchState, immediate?: boolean) => void;
}) {
  return (
    <div className="card">
      <h3>Options</h3>
      <label className="check">
        <input
          type="checkbox"
          checked={state.inStockOnly}
          onChange={(event) =>
            onChange({ ...state, inStockOnly: event.target.checked, page: 1 }, true)
          }
        />
        In stock only
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={state.includeStubs}
          onChange={(event) =>
            onChange({ ...state, includeStubs: event.target.checked, page: 1 }, true)
          }
        />
        Include stubs
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={state.mode === "substitute"}
          onChange={(event) =>
            onChange(
              { ...state, mode: event.target.checked ? "substitute" : "search", page: 1 },
              true,
            )
          }
        />
        Find substitutes
      </label>
      {state.mode === "substitute" && (
        <p className="muted-note">
          Substitution runs the same SQL filter with each parameter&apos;s
          <span className="mono"> substitution_direction</span> applied, so a higher voltage
          rating satisfies a lower requirement and never the other way round. It is
          deterministic by construction — nothing here is inferred.
        </p>
      )}
    </div>
  );
}

function Results({
  state,
  results,
  loading,
  onPage,
}: {
  state: SearchState;
  results: SearchResponse | null;
  loading: boolean;
  onPage: (page: number) => void;
}) {
  if (results === null) {
    return loading ? <Loading what="parts" /> : null;
  }

  const first = (state.page - 1) * PAGE_SIZE + 1;
  const last = first + results.results.length - 1;
  const hasMore = last < results.total;

  return (
    <>
      <div className="row">
        <p className="muted-note" style={{ margin: 0 }}>
          {results.total === 0
            ? "Nothing matched. The counts in the filters show where the parts actually are."
            : `${first}–${last} of ${results.total}, parts you have in stock first.`}
        </p>
        <span className="spacer" />
        {/* The previous page stays on screen while the next one loads, so the
            list never flashes empty on a slow connection. */}
        {loading && <span className="muted-note">updating…</span>}
      </div>

      <ul className="list">
        {results.results.map((part) => (
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

      {(state.page > 1 || hasMore) && (
        <div className="pager">
          <button type="button" disabled={state.page <= 1} onClick={() => onPage(state.page - 1)}>
            ← Previous
          </button>
          <span className="muted-note">
            Page {state.page} of {Math.max(1, Math.ceil(results.total / PAGE_SIZE))}
          </span>
          <span className="spacer" />
          <button type="button" disabled={!hasMore} onClick={() => onPage(state.page + 1)}>
            Next →
          </button>
        </div>
      )}
    </>
  );
}
