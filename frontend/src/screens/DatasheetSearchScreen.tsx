/**
 * Full-text search over every stored PDF's extracted text.
 *
 * `docs/PLAN.md` calls this out as Phase 4's own standalone value ("useful
 * standalone: full-text search across every PDF you own"), separate from the
 * part search `SearchScreen` already does — this screen answers "which
 * document, and where in it," not "which part." A hit's title *is* the jump to
 * the PDF: `hit.url` is the same content-addressed, cache-forever route
 * `DocumentsPanel` already opens with `window.open`, so a search result and an
 * attached-document link behave identically once tapped.
 *
 * The querystring (`?q=&page=`) is the query, the same choice `SearchScreen`
 * makes and for the same reason: a search here is a shareable link, and typing
 * is debounced (`useDeferredCommit`) so the URL is not rewritten on every
 * keystroke.
 *
 * A blank box shows nothing rather than "browsing" every stored PDF — unlike
 * part search, there is no useful "everything" order over raw document text,
 * so an empty query does not fire a request at all.
 */

import { useEffect, useMemo, useRef } from "react";
import { useSearchParams } from "react-router-dom";

import { ErrorBanner, Loading } from "../components/Feedback";
import {
  searchDatasheets,
  type DatasheetSearchHit,
  type DatasheetSearchResponse,
} from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";
import { useDeferredCommit } from "../lib/hooks/useDeferredCommit";

/** Long enough to swallow a word being typed, short enough not to feel stuck —
 * the same value `SearchScreen` uses, for the same reason. */
const COMMIT_DELAY_MS = 300;

const PAGE_SIZE = 20;

interface QueryState {
  readonly q: string;
  readonly page: number;
}

function stateFromParams(params: URLSearchParams): QueryState {
  const page = Number.parseInt(params.get("page") ?? "1", 10);
  return {
    q: params.get("q") ?? "",
    // Clamped rather than trusted: a stale or hand-edited link with `page=0` or
    // `page=banana` must not turn into a negative offset at the server.
    page: Number.isFinite(page) && page >= 1 ? page : 1,
  };
}

function paramsFromState(state: QueryState): URLSearchParams {
  const params = new URLSearchParams();
  if (state.q !== "") {
    params.set("q", state.q);
  }
  if (state.page > 1) {
    params.set("page", String(state.page));
  }
  return params;
}

export function DatasheetSearchScreen() {
  const [params, setParams] = useSearchParams();
  const urlKey = params.toString();

  const { draft, applied, commit, adopt } = useDeferredCommit<QueryState>(
    useMemo(() => stateFromParams(params), []), // eslint-disable-line react-hooks/exhaustive-deps
    COMMIT_DELAY_MS,
  );

  // Mirrors `SearchScreen`'s `written` ref: without it, the URL-sync effect
  // below would race the debounce, resetting a draft the user is still typing
  // every time its own `setParams` call comes back around.
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

  const trimmed = applied.q.trim();
  const searchKey = paramsFromState(applied).toString();
  const results = useAsync<DatasheetSearchResponse | null>(
    () =>
      trimmed === ""
        ? Promise.resolve(null)
        : searchDatasheets(trimmed, {
            limit: PAGE_SIZE,
            offset: (applied.page - 1) * PAGE_SIZE,
          }),
    [searchKey],
  );

  function changeText(q: string): void {
    commit({ q, page: 1 });
  }

  function changePage(page: number): void {
    commit({ ...applied, page }, { immediate: true });
  }

  return (
    <div className="stack">
      <div className="card">
        <label className="field">
          <span>Search datasheet text</span>
          <input
            value={draft.q}
            onChange={(event) => changeText(event.target.value)}
            placeholder='e.g. "thermal resistance" or a package name'
            enterKeyHint="search"
            autoComplete="off"
            type="search"
          />
        </label>
        <p className="muted-note">
          Matches the extracted text of every stored PDF, not part names or fields — a
          datasheet nobody has read yet simply is not indexed, and that is not an error.
        </p>
      </div>

      <ErrorBanner error={results.error} fallback="That search could not be run." />

      {trimmed === "" ? (
        <p className="dim">Type something to search across every stored PDF.</p>
      ) : (
        <Results state={applied} results={results.data} loading={results.loading} onPage={changePage} />
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
  state: QueryState;
  results: DatasheetSearchResponse | null;
  loading: boolean;
  onPage: (page: number) => void;
}) {
  if (results === null) {
    return loading ? <Loading what="datasheets" /> : null;
  }

  const first = (state.page - 1) * PAGE_SIZE + 1;
  const last = first + results.results.length - 1;
  const hasMore = last < results.total;

  return (
    <>
      <div className="row">
        <p className="muted-note" style={{ margin: 0 }}>
          {results.total === 0
            ? "No stored PDF's extracted text matches that."
            : `${first}–${last} of ${results.total} document${results.total === 1 ? "" : "s"}.`}
        </p>
        <span className="spacer" />
        {/* The previous page stays on screen while the next loads, so the list
            never flashes empty on a slow connection — same as `SearchScreen`. */}
        {loading && <span className="muted-note">updating…</span>}
      </div>

      <ul className="list">
        {results.results.map((hit) => (
          <li key={hit.sha256} className="list-item">
            {/* A real anchor, not a router `Link`: the destination is a PDF, not
                a screen in this app — the same reasoning `DocumentsPanel` uses
                for its own "View datasheet" button. */}
            <a href={hit.url} target="_blank" rel="noreferrer" className="title">
              {hit.original_filename ?? `${hit.sha256.slice(0, 12)}…`}
            </a>
            <div className="sub">
              {hit.media_type}
              {hit.page_count !== null && ` · ${hit.page_count} page${hit.page_count === 1 ? "" : "s"}`}
            </div>
            <Snippet hit={hit} />
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

/**
 * Why this document matched. `hit.snippet` is already split into plain-text
 * segments by the backend (`app.services.search.datasheets._split_snippet`) —
 * never raw markup — specifically so this can render a matched term as a real
 * `<mark>` element instead of trusting HTML embedded in a stranger's PDF text.
 */
function Snippet({ hit }: { hit: DatasheetSearchHit }) {
  return (
    <p className="snippet">
      {hit.snippet.map((segment, index) =>
        segment.highlighted ? (
          <mark key={index}>{segment.text}</mark>
        ) : (
          <span key={index}>{segment.text}</span>
        ),
      )}
    </p>
  );
}
