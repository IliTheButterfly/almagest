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

type Schemas = components["schemas"];

export type SearchRequest = Schemas["SearchRequest"];
export type SearchResponse = Schemas["SearchResponse"];
export type PartSummary = Schemas["PartSummary"];
export type ResolveResponse = Schemas["ResolveResponse"];

/** One parametric predicate: a template name plus a raw value string. */
export type SearchFilter = Schemas["FilterIn"];

export type PartRead = Schemas["PartRead"];
export type PartCreate = Schemas["PartCreate"];
export type PartCreated = Schemas["PartCreated"];
export type PartUpdate = Schemas["PartUpdate"];

export type LocationTree = Schemas["LocationTree"];
export type LocationNode = Schemas["LocationNode"];
export type LocationRead = Schemas["LocationRead"];
export type LocationCreate = Schemas["LocationCreate"];
export type LocationCreated = Schemas["LocationCreated"];
export type CapacityRead = Schemas["CapacityRead"];
export type SuggestRequest = Schemas["SuggestRequest"];
export type SuggestResponse = Schemas["SuggestResponse"];

export type LotRead = Schemas["LotRead"];
export type LedgerEntry = Schemas["LedgerEntry"];
export type MovementResponse = Schemas["MovementResponse"];
export type QuantityRequest = Schemas["QuantityRequest"];
export type AdjustRequest = Schemas["AdjustRequest"];
export type RecountRequest = Schemas["RecountRequest"];
export type MoveRequest = Schemas["MoveRequest"];
export type ReceiveRequest = Schemas["ReceiveRequest"];
export type EmptyBinRequest = Schemas["EmptyBinRequest"];
export type EmptyBinResponse = Schemas["EmptyBinResponse"];
export type UndoRequest = Schemas["UndoRequest"];
export type UndoResponse = Schemas["UndoResponse"];

export type ScanResolveRequest = Schemas["ScanResolveRequest"];
export type ScanResolveResponse = Schemas["ScanResolveResponse"];
export type ScanAliasRequest = Schemas["ScanAliasRequest"];
export type ScanAliasResponse = Schemas["ScanAliasResponse"];
export type ScanTarget = Schemas["ScanTarget"];
export type ScanCandidate = Schemas["ScanCandidate"];
export type ScanParsed = Schemas["ScanParsed"];
export type ScanExistingLot = Schemas["ScanExistingLot"];
export type EntityType = Schemas["EntityType"];
export type AliasKind = Schemas["AliasKind"];

export class ApiError extends Error {
  readonly detail: unknown;
  /** The HTTP status, so a screen can tell a 409 refusal from a 404. */
  readonly status: number | null;

  constructor(message: string, detail: unknown, status: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.detail = detail;
    this.status = status;
  }
}

/**
 * Raise on a non-success response.
 *
 * Split out so every helper below reads as one line of intent. Returns `never`,
 * which is what lets TypeScript narrow `data` to non-`undefined` at the call
 * site without a cast.
 */
function fail(message: string, detail: unknown, response: Response | undefined): never {
  throw new ApiError(message, detail, response?.status ?? null);
}

// ---------------------------------------------------------------- search ----

export async function searchParts(request: SearchRequest): Promise<SearchResponse> {
  const { data, error, response } = await api.POST("/api/search/parts", { body: request });
  if (error !== undefined) {
    fail("search failed", error, response);
  }
  return data;
}

// --------------------------------------------------------------- resolve ----

export async function resolveShortId(shortId: string): Promise<ResolveResponse> {
  const { data, error, response } = await api.GET("/api/resolve/{short_id}", {
    params: { path: { short_id: shortId } },
  });
  if (error !== undefined) {
    fail(`could not resolve ${shortId}`, error, response);
  }
  return data;
}

export async function resolveScan(request: ScanResolveRequest): Promise<ScanResolveResponse> {
  const { data, error, response } = await api.POST("/api/scan/resolve", { body: request });
  if (error !== undefined) {
    fail("could not resolve that scan", error, response);
  }
  return data;
}

export async function bindScanAlias(request: ScanAliasRequest): Promise<ScanAliasResponse> {
  const { data, error, response } = await api.POST("/api/scan/alias", { body: request });
  if (error !== undefined) {
    fail("could not bind that code", error, response);
  }
  return data;
}

// ----------------------------------------------------------------- parts ----

export async function getPart(partId: number): Promise<PartRead> {
  const { data, error, response } = await api.GET("/api/parts/{part_id}", {
    params: { path: { part_id: partId } },
  });
  if (error !== undefined) {
    fail(`no part ${partId}`, error, response);
  }
  return data;
}

export async function createPart(request: PartCreate): Promise<PartCreated> {
  const { data, error, response } = await api.POST("/api/parts", { body: request });
  if (error !== undefined) {
    fail("could not create that part", error, response);
  }
  return data;
}

export async function updatePart(partId: number, request: PartUpdate): Promise<PartRead> {
  const { data, error, response } = await api.PATCH("/api/parts/{part_id}", {
    params: { path: { part_id: partId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not save that part", error, response);
  }
  return data;
}

// ------------------------------------------------------------- locations ----

export async function getLocationTree(rootId?: number): Promise<LocationTree> {
  const { data, error, response } = await api.GET("/api/locations/tree", {
    params: { query: rootId === undefined ? {} : { root_id: rootId } },
  });
  if (error !== undefined) {
    fail("could not load the storage tree", error, response);
  }
  return data;
}

export async function getLocation(locationId: number): Promise<LocationRead> {
  const { data, error, response } = await api.GET("/api/locations/{location_id}", {
    params: { path: { location_id: locationId } },
  });
  if (error !== undefined) {
    fail(`no location ${locationId}`, error, response);
  }
  return data;
}

export async function createLocation(request: LocationCreate): Promise<LocationCreated> {
  const { data, error, response } = await api.POST("/api/locations", { body: request });
  if (error !== undefined) {
    fail("could not create that container", error, response);
  }
  return data;
}

/**
 * Ask where a new lot should go.
 *
 * Guarded by an idempotency key on the server because one rung of the escalation
 * ladder *materialises* an empty grid cell — so a retried suggestion without a
 * key leaves a spare cell behind every time the wifi drops.
 */
export async function suggestLocation(request: SuggestRequest): Promise<SuggestResponse> {
  const { data, error, response } = await api.POST("/api/locations/suggest", { body: request });
  if (error !== undefined) {
    fail("could not suggest a location", error, response);
  }
  return data;
}

// ----------------------------------------------------------------- stock ----

export async function getLot(lotId: number): Promise<LotRead> {
  const { data, error, response } = await api.GET("/api/stock/lots/{lot_id}", {
    params: { path: { lot_id: lotId } },
  });
  if (error !== undefined) {
    fail(`no lot ${lotId}`, error, response);
  }
  return data;
}

export async function getLotHistory(lotId: number, limit?: number): Promise<LedgerEntry[]> {
  const { data, error, response } = await api.GET("/api/stock/lots/{lot_id}/history", {
    params: {
      path: { lot_id: lotId },
      query: limit === undefined ? {} : { limit },
    },
  });
  if (error !== undefined) {
    fail(`could not load history for lot ${lotId}`, error, response);
  }
  return data;
}

export async function receiveStock(request: ReceiveRequest): Promise<MovementResponse> {
  const { data, error, response } = await api.POST("/api/stock/receive", { body: request });
  if (error !== undefined) {
    fail("could not receive that stock", error, response);
  }
  return data;
}

export async function consumeLot(
  lotId: number,
  request: QuantityRequest,
): Promise<MovementResponse> {
  const { data, error, response } = await api.POST("/api/stock/lots/{lot_id}/consume", {
    params: { path: { lot_id: lotId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not record that take", error, response);
  }
  return data;
}

export async function returnLot(lotId: number, request: QuantityRequest): Promise<MovementResponse> {
  const { data, error, response } = await api.POST("/api/stock/lots/{lot_id}/return", {
    params: { path: { lot_id: lotId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not record that return", error, response);
  }
  return data;
}

export async function adjustLot(lotId: number, request: AdjustRequest): Promise<MovementResponse> {
  const { data, error, response } = await api.POST("/api/stock/lots/{lot_id}/adjust", {
    params: { path: { lot_id: lotId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not record that adjustment", error, response);
  }
  return data;
}

export async function recountLot(lotId: number, request: RecountRequest): Promise<MovementResponse> {
  const { data, error, response } = await api.POST("/api/stock/lots/{lot_id}/recount", {
    params: { path: { lot_id: lotId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not record that count", error, response);
  }
  return data;
}

export async function moveLot(lotId: number, request: MoveRequest): Promise<MovementResponse> {
  const { data, error, response } = await api.POST("/api/stock/lots/{lot_id}/move", {
    params: { path: { lot_id: lotId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not move that lot", error, response);
  }
  return data;
}

export async function emptyBin(
  locationId: number,
  request: EmptyBinRequest,
): Promise<EmptyBinResponse> {
  const { data, error, response } = await api.POST("/api/stock/locations/{location_id}/empty", {
    params: { path: { location_id: locationId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not empty that bin", error, response);
  }
  return data;
}

/**
 * Undo by appending a compensating row. Never by deleting one.
 *
 * `client_op_id_to_undo` is the handle the UI uses: it already minted that key at
 * scan time, so the eight-second button needs to remember nothing else. The undo
 * itself carries its own fresh `client_op_id`, so a double tap on *it* is
 * collapsed too.
 */
export async function undoMovement(request: UndoRequest): Promise<UndoResponse> {
  const { data, error, response } = await api.POST("/api/stock/undo", { body: request });
  if (error !== undefined) {
    fail("could not undo that movement", error, response);
  }
  return data;
}
