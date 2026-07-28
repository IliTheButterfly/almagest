import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

/**
 * The typed API client.
 *
 * `schema.ts` is **generated** from the backend's OpenAPI document
 * (`pnpm generate:api`) and is never edited by hand. That is what makes the
 * repo splits safe: the contract between backend, frontend and deviceagent is
 * machine-checked rather than maintained by hand in three places, so a route
 * signature change breaks the build here instead of failing at runtime on a
 * phone in front of a parts drawer.
 *
 * CI regenerates it and fails on any diff, so a stale client cannot ship.
 */
/**
 * Always the current origin.
 *
 * The PWA is served by the API in production and Vite proxies `/api` and `/s`
 * to it in development, so there is genuinely nothing to configure and no CORS
 * to arrange. It cannot be the empty string, though: `openapi-fetch` builds
 * request URLs with `new URL()`, which requires an absolute base.
 *
 * Deriving it from `location` rather than an env var also means the deployed
 * host — the same host baked into every NFC tag — can change without a rebuild.
 */
function currentOrigin(): string {
  return typeof window === "undefined" ? "http://localhost" : window.location.origin;
}

export const api = createClient<paths>({
  baseUrl: currentOrigin(),
  // Resolve `fetch` at call time rather than capturing it when this module is
  // first imported. That matters in a PWA — a service worker may install after
  // load and replace it — and it is also what makes the client stubbable in
  // tests without a network stack.
  fetch: (request) => globalThis.fetch(request),
});

export type SearchRequest = components["schemas"]["SearchRequest"];
export type SearchResponse = components["schemas"]["SearchResponse"];
export type PartSummary = components["schemas"]["PartSummary"];
export type ResolveResponse = components["schemas"]["ResolveResponse"];

/** One parametric predicate: a template name plus a raw value string. */
export type SearchFilter = components["schemas"]["FilterIn"];

export async function searchParts(request: SearchRequest): Promise<SearchResponse> {
  const { data, error } = await api.POST("/api/search/parts", { body: request });
  if (error !== undefined) {
    throw new ApiError("search failed", error);
  }
  return data;
}

export async function resolveShortId(shortId: string): Promise<ResolveResponse> {
  const { data, error } = await api.GET("/api/resolve/{short_id}", {
    params: { path: { short_id: shortId } },
  });
  if (error !== undefined) {
    throw new ApiError(`could not resolve ${shortId}`, error);
  }
  return data;
}

export class ApiError extends Error {
  readonly detail: unknown;

  constructor(message: string, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.detail = detail;
  }
}
