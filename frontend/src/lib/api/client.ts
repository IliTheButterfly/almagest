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

export type CategoryNode = Schemas["CategoryNode"];
export type FacetsRequest = Schemas["FacetsRequest"];
export type FacetsResponse = Schemas["FacetsResponse"];
export type TemplateFacets = Schemas["TemplateFacets"];
export type ChoiceFacet = Schemas["ChoiceFacet"];
export type NumericRange = Schemas["NumericRange"];

export type PartRead = Schemas["PartRead"];
export type PartCreate = Schemas["PartCreate"];
export type PartCreated = Schemas["PartCreated"];
export type PartUpdate = Schemas["PartUpdate"];

// --- datasheet full-text search (Phase 4's standalone value) ---------------
export type DatasheetSearchResponse = Schemas["DatasheetSearchResponse"];
export type DatasheetSearchHit = Schemas["DatasheetSearchHit"];
export type DatasheetSnippetSegment = Schemas["DatasheetSnippetSegment"];

// --- content-addressed documents: datasheets, and Phase 3's tray photos ----
export type DocumentRead = Schemas["DocumentRead"];
export type DocumentLinkRead = Schemas["DocumentLinkRead"];
export type DocumentLinkList = Schemas["DocumentLinkList"];
export type DocumentUploadResult = Schemas["DocumentUploadResult"];
export type DocumentAttachRequest = Schemas["DocumentAttachRequest"];
export type DocumentAttachResult = Schemas["DocumentAttachResult"];
/** A container type's own attach result: it also says *which* type it landed on,
 * because attaching to a seed clones it first. */
export type ContainerTypeDocumentAttached = Schemas["ContainerTypeDocumentAttached"];
export type DocumentDetachResult = Schemas["DocumentDetachResult"];
export type DocumentKind = Schemas["DocumentKind"];
export type DocumentRole = Schemas["DocumentRole"];

export type LocationTree = Schemas["LocationTree"];
export type LocationNode = Schemas["LocationNode"];
export type LocationRead = Schemas["LocationRead"];
export type LocationCreate = Schemas["LocationCreate"];
export type LocationCreated = Schemas["LocationCreated"];
export type CapacityRead = Schemas["CapacityRead"];
export type SuggestRequest = Schemas["SuggestRequest"];
export type SuggestResponse = Schemas["SuggestResponse"];
export type ShortIdRequest = Schemas["ShortIdRequest"];
export type ShortIdResponse = Schemas["ShortIdResponse"];

// --- removing a container (backend `app/services/removal.py`) --------------
export type RemovalPreview = Schemas["RemovalPreview"];
export type RemovalBlockerRead = Schemas["RemovalBlockerRead"];
export type RemovalNodeRead = Schemas["RemovalNodeRead"];
export type LocationRemoved = Schemas["LocationRemoved"];
export type LocationRestored = Schemas["LocationRestored"];

// --- ADR 0006: how each layer of the tree is drawn ------------------------
export type ChildView = Schemas["ChildView"];
export type LocationChildViewUpdate = Schemas["LocationChildViewUpdate"];
export type LocationChildViewResponse = Schemas["LocationChildViewResponse"];

// --- container pictograms: cheap, drawn at every node of the dense tree ----
export type ContainerGlyph = Schemas["ContainerGlyph"];
export type LocationGlyphUpdate = Schemas["LocationGlyphUpdate"];
export type LocationGlyphResponse = Schemas["LocationGlyphResponse"];

// --- container photos: one real image, drawn only on that container's own
// detail screen — the `document_links` role a container type's and a
// location's own photo both attach through.
export type ContainerTypeDocumentLinkList = Schemas["ContainerTypeDocumentLinkList"];
export type LocationDocumentLinkList = Schemas["LocationDocumentLinkList"];

// --- layout authoring: container types, their canvas, and one instance's --
// own copy of it (docs/PLAN.md, "Layout authoring"; ADR 0002). ---------------
export type ContainerTypeRead = Schemas["ContainerTypeRead"];
export type ContainerTypeCreate = Schemas["ContainerTypeCreate"];
export type ContainerTypeUpdate = Schemas["ContainerTypeUpdate"];
export type ContainerTypeCreated = Schemas["ContainerTypeCreated"];
export type ContainerTypeEdited = Schemas["ContainerTypeEdited"];
export type CloneRequest = Schemas["CloneRequest"];
/** ADR 0002's two questions are these two groups of columns; the enums that go
 * with them are exported here so a form can offer exactly what the API accepts
 * and nothing else. */
export type CapacityModel = Schemas["CapacityModel"];
export type ChildLayout = Schemas["ChildLayout"];
export type TagGranularity = Schemas["TagGranularity"];
export type InstantiateRequest = Schemas["InstantiateRequest"];
export type InstantiateResponse = Schemas["InstantiateResponse"];
export type SlotSpecIn = Schemas["SlotSpecIn"];
export type SlotSpecOut = Schemas["SlotSpecOut"];
export type SlotLabelScheme = Schemas["SlotLabelScheme"];
export type SlotTemplateRead = Schemas["SlotTemplateRead"];
export type SlotTemplateWrite = Schemas["SlotTemplateWrite"];
export type SlotTemplateWritten = Schemas["SlotTemplateWritten"];
export type LayoutRead = Schemas["LayoutRead"];
export type SlotStateRead = Schemas["SlotStateRead"];
export type ReapplyLayoutRequest = Schemas["ReapplyLayoutRequest"];
export type ReapplyLayoutResponse = Schemas["ReapplyLayoutResponse"];
export type PendingIntakeIn = Schemas["PendingIntakeIn"];
export type PendingIntakeRead = Schemas["PendingIntakeRead"];
export type PendingIntakeCreated = Schemas["PendingIntakeCreated"];
export type PendingIntakeList = Schemas["PendingIntakeList"];
export type PendingIntakeStatus = Schemas["PendingIntakeStatus"];

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

export type MovementDirection = Schemas["MovementDirection"];
export type MovementLine = Schemas["MovementLine"];
export type MovementLineResult = Schemas["MovementLineResult"];
export type BatchMovementRequest = Schemas["BatchMovementRequest"];
export type BatchMovementResponse = Schemas["BatchMovementResponse"];

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

export type ProjectRead = Schemas["ProjectRead"];
export type ProjectCreate = Schemas["ProjectCreate"];
export type ProjectCreated = Schemas["ProjectCreated"];
export type ProjectUpdate = Schemas["ProjectUpdate"];
export type ProjectList = Schemas["ProjectList"];
export type ProjectStatus = Schemas["ProjectStatus"];

export type BuildRead = Schemas["BuildRead"];
export type BuildCreate = Schemas["BuildCreate"];
export type BuildCreated = Schemas["BuildCreated"];
export type BuildUpdate = Schemas["BuildUpdate"];
export type BuildStatus = Schemas["BuildStatus"];

export type BomLineRead = Schemas["BomLineRead"];
export type BomLineList = Schemas["BomLineList"];
export type BomLineEdit = Schemas["BomLineEdit"];
export type BomLinesUpdateRequest = Schemas["BomLinesUpdateRequest"];
export type BomLinesUpdateResponse = Schemas["BomLinesUpdateResponse"];
export type BomImportRequest = Schemas["BomImportRequest"];
export type BomImportResponse = Schemas["BomImportResponse"];

// --- the prose front door: requirement lines in, ranked candidates out -----
export type RequirementLineIn = Schemas["RequirementLineIn"];
export type RequirementRead = Schemas["RequirementRead"];
export type RequirementFilterRead = Schemas["RequirementFilterRead"];
export type RequirementRejectionRead = Schemas["RequirementRejectionRead"];
export type SuggestionRequest = Schemas["SuggestionRequest"];
export type SuggestionBatchResponse = Schemas["SuggestionBatchResponse"];
export type SuggestionLineRead = Schemas["SuggestionLineRead"];
export type PartCandidateRead = Schemas["PartCandidateRead"];
export type SubstitutionReasonRead = Schemas["SubstitutionReasonRead"];

export type AllocationRead = Schemas["AllocationRead"];
export type AllocateRequest = Schemas["AllocateRequest"];
export type AllocateResponse = Schemas["AllocateResponse"];
export type AllocateLine = Schemas["AllocateLine"];
export type AllocateLineResult = Schemas["AllocateLineResult"];
export type AllocateBatchRequest = Schemas["AllocateBatchRequest"];
export type AllocateBatchResponse = Schemas["AllocateBatchResponse"];
export type BomLineOutcome = Schemas["BomLineOutcome"];
export type ReleaseRequest = Schemas["ReleaseRequest"];
export type ReleaseResponse = Schemas["ReleaseResponse"];

export type StageRequest = Schemas["StageRequest"];
export type StageResponse = Schemas["StageResponse"];
export type UnstageRequest = Schemas["UnstageRequest"];
export type UnstageResponse = Schemas["UnstageResponse"];
export type ConsumeStagedRequest = Schemas["ConsumeStagedRequest"];
export type ConsumeStagedResponse = Schemas["ConsumeStagedResponse"];

export type ShortageResponse = Schemas["ShortageResponse"];
export type LineShortageRead = Schemas["LineShortageRead"];

export type RosterResponse = Schemas["RosterResponse"];
export type RosterLineRead = Schemas["RosterLineRead"];
export type RosterEntryRead = Schemas["RosterEntryRead"];
export type RecordUsedRequest = Schemas["RecordUsedRequest"];
export type RecordUsedResponse = Schemas["RecordUsedResponse"];

export type PickListResponse = Schemas["PickListResponse"];
export type PickStopRead = Schemas["PickStopRead"];
export type PickTakeRead = Schemas["PickTakeRead"];
export type PickGapRead = Schemas["PickGapRead"];
export type EnrichmentQueueResponse = Schemas["EnrichmentQueueResponse"];
export type EnrichmentPartGroup = Schemas["EnrichmentPartGroup"];
export type EnrichmentFieldGroup = Schemas["EnrichmentFieldGroup"];
export type EnrichmentCandidateRead = Schemas["EnrichmentCandidateRead"];
export type EnrichmentCorrectRequest = Schemas["EnrichmentCorrectRequest"];
export type EnrichmentBulkAcceptRequest = Schemas["EnrichmentBulkAcceptRequest"];
export type EnrichmentBulkAcceptResponse = Schemas["EnrichmentBulkAcceptResponse"];
export type EnrichmentBulkAcceptResult = Schemas["EnrichmentBulkAcceptResult"];

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

/**
 * The filterable attributes, with counts against the filters already applied.
 *
 * Counts are the point rather than a nicety: a facet list without them makes
 * every option look equally promising, so the user finds the empty result set by
 * clicking into it. They are computed against the current filter set, which is
 * why this has to be re-requested whenever the filters change — the numbers
 * answer "what can I narrow to from *here*", not "what exists".
 */
export async function getParameterFacets(request: FacetsRequest): Promise<FacetsResponse> {
  const { data, error, response } = await api.POST("/api/parameter-templates", { body: request });
  if (error !== undefined) {
    fail("could not load the filters", error, response);
  }
  return data;
}

/** The category tree for the browse-by-type rail. Counts include descendants. */
export async function listPartCategories(): Promise<CategoryNode[]> {
  const { data, error, response } = await api.GET("/api/part-categories", {});
  if (error !== undefined) {
    fail("could not load the categories", error, response);
  }
  return data;
}

/**
 * Full-text search across every stored PDF's extracted text —
 * `docs/PLAN.md`'s "useful standalone: full-text search across every PDF you
 * own." A document nobody has extracted yet is simply absent from `results`;
 * that is `app.services.document_text`'s first-class "not extracted" state,
 * never surfaced here as an error.
 */
export async function searchDatasheets(
  query: string,
  options: { limit?: number; offset?: number } = {},
): Promise<DatasheetSearchResponse> {
  const { data, error, response } = await api.GET("/api/search/datasheets", {
    params: { query: { q: query, ...options } },
  });
  if (error !== undefined) {
    fail("datasheet search failed", error, response);
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

// --------------------------------------------------------------- documents --

/** Every document attached to a part, primary first within each role. */
export async function listPartDocuments(partId: number): Promise<DocumentLinkList> {
  const { data, error, response } = await api.GET("/api/parts/{part_id}/documents", {
    params: { path: { part_id: partId } },
  });
  if (error !== undefined) {
    fail("could not load the attached documents", error, response);
  }
  return data;
}

/**
 * Upload a file, storing it under the sha256 of its bytes and — when `partId`,
 * `containerTypeId` or `locationId` is given — attaching it in one request. At
 * most one of those three may be set, mirroring the backend route's own refusal
 * of an ambiguous attachment.
 *
 * Not routed through `api.POST`. `openapi-fetch`'s default body serializer
 * JSON-encodes anything that is not `FormData`, which would corrupt a binary
 * body; the backend route deliberately avoids `multipart/form-data` too (see
 * `app.api.routes.documents`'s module docstring — it would pull in
 * `python-multipart`, a dependency the default API install does not carry), so
 * there is no `FormData` escape hatch to use instead. The raw bytes go in the
 * body and every other field rides in the query string, which is what a plain
 * `fetch()` sends without fighting the typed client's assumptions.
 */
export async function uploadDocument(
  file: Blob,
  options: {
    mediaType: string;
    kind?: DocumentKind;
    partId?: number;
    containerTypeId?: number;
    locationId?: number;
    role?: DocumentRole;
    isPrimary?: boolean;
    filename?: string;
    sourceUrl?: string;
  },
): Promise<DocumentUploadResult> {
  const query = new URLSearchParams({ media_type: options.mediaType });
  if (options.kind !== undefined) query.set("kind", options.kind);
  if (options.partId !== undefined) query.set("part_id", String(options.partId));
  if (options.containerTypeId !== undefined) {
    query.set("container_type_id", String(options.containerTypeId));
  }
  if (options.locationId !== undefined) query.set("location_id", String(options.locationId));
  if (options.role !== undefined) query.set("role", options.role);
  if (options.isPrimary !== undefined) query.set("is_primary", String(options.isPrimary));
  if (options.filename !== undefined) query.set("filename", options.filename);
  if (options.sourceUrl !== undefined) query.set("source_url", options.sourceUrl);

  const response = await globalThis.fetch(`${currentOrigin()}/api/documents?${query.toString()}`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: file,
  });
  // Mirrors `problemOf`'s expectation (`lib/api/errors.ts`): the raw response
  // body, `{"detail": {"reason", "message"}}` on a refusal, same as what
  // `openapi-fetch`'s `error` field would hold for this same route.
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    fail("could not upload that document", body, response);
  }
  return body as DocumentUploadResult;
}

/**
 * Attach an already-stored document to a part, or promote its existing link to
 * primary. There is no separate "make primary" route — re-attaching with
 * `is_primary: true` is that operation, per the backend service's upsert.
 */
export async function attachPartDocument(
  partId: number,
  request: DocumentAttachRequest,
): Promise<DocumentAttachResult> {
  const { data, error, response } = await api.POST("/api/parts/{part_id}/documents", {
    params: { path: { part_id: partId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not attach that document", error, response);
  }
  return data;
}

/**
 * Unlink a document from a part. Neither the row nor the blob is deleted — one
 * family sheet can serve a dozen parts, so a part dropping its link says
 * nothing about whether the file is still wanted.
 */
export async function detachPartDocument(
  partId: number,
  sha256: string,
): Promise<DocumentDetachResult> {
  const { data, error, response } = await api.DELETE("/api/parts/{part_id}/documents/{sha256}", {
    params: { path: { part_id: partId, sha256 } },
  });
  if (error !== undefined) {
    fail("could not remove that document", error, response);
  }
  return data;
}

/**
 * The last hop of `docs/PLAN.md`'s "QR-to-datasheet" path. A scanned tag
 * already lands the phone on this part's screen via `/s/{short_id}`
 * (`app.api.routes.resolve.open_short_id` redirects a resolved part straight to
 * `/parts/{id}`); this is the one tap from there. A plain URL rather than a
 * fetch helper — the caller hands it to the browser (`window.open` or an
 * `<a href>`) so the 307 and the eventual `Content-Disposition: inline` are
 * handled by the browser's own PDF viewer, not by this app.
 */
export function partDatasheetUrl(partId: number): string {
  return `/api/parts/${partId}/datasheet`;
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

// --- removing a container -------------------------------------------------
//
// Three calls rather than one, because the interesting part of removing a
// container is finding out what would happen. The backend decides per node
// between deleting the row, retiring it (the ledger names it, so the row and its
// history stay while the container leaves the tree) and refusing outright
// because stock is inside — see `app/services/removal.py`. The preview returns
// that same decision without writing anything, so the confirm panel states the
// real consequence instead of a generic warning it might be wrong about.

/**
 * What removing this container *would* do. Writes nothing.
 *
 * A refusal is a 200 with `removable: false` and a `blockers` list naming the
 * contents, not an error — the caller asked a question and this is the answer.
 */
export async function previewLocationRemoval(
  locationId: number,
  recursive = false,
): Promise<RemovalPreview> {
  const { data, error, response } = await api.GET("/api/locations/{location_id}/removal", {
    params: { path: { location_id: locationId }, query: { recursive } },
  });
  if (error !== undefined) {
    fail("could not work out what removing this would do", error, response);
  }
  return data;
}

/**
 * Remove it. `recursive` is required for a container with anything inside it,
 * and the server refuses rather than recursing silently.
 */
export async function removeLocation(
  locationId: number,
  recursive = false,
): Promise<LocationRemoved> {
  const { data, error, response } = await api.DELETE("/api/locations/{location_id}", {
    params: { path: { location_id: locationId }, query: { recursive } },
  });
  if (error !== undefined) {
    fail("could not remove that container", error, response);
  }
  return data;
}

/** Undo a retirement — this container and everything retired under it. */
export async function restoreLocation(locationId: number): Promise<LocationRestored> {
  const { data, error, response } = await api.POST("/api/locations/{location_id}/restore", {
    params: { path: { location_id: locationId } },
  });
  if (error !== undefined) {
    fail("could not bring that container back", error, response);
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

/**
 * Give a container a printed identity: minted, or one it already carries.
 *
 * Omit `short_id` to promote a generated grid cell that has none — safe to call
 * unconditionally, since it returns the existing id when there is one. Pass a
 * `short_id` to adopt a code that is already printed on a card or written to a
 * tag; the server verifies the check symbol and refuses a code held elsewhere
 * rather than substituting a free one, because a substitute would leave the
 * label and the database permanently disagreeing.
 */
/**
 * Pin — or, with `child_view: null`, stop pinning — how one container draws its
 * children (ADR 0006).
 *
 * A narrow route rather than a `PATCH /api/locations/{id}`, because there is no
 * such route and inventing one to carry a single field would put every other
 * column of `locations` on the wire as writable.
 */
export async function setLocationChildView(
  locationId: number,
  request: LocationChildViewUpdate,
): Promise<LocationChildViewResponse> {
  const { data, error, response } = await api.PUT("/api/locations/{location_id}/child-view", {
    params: { path: { location_id: locationId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not change how this container is drawn", error, response);
  }
  return data;
}

/**
 * Pin — or, with `glyph: null`, stop pinning — this one container's own
 * pictogram, overriding its container type's. Same narrow-route shape as
 * `setLocationChildView`, one field over.
 */
export async function setLocationGlyph(
  locationId: number,
  request: LocationGlyphUpdate,
): Promise<LocationGlyphResponse> {
  const { data, error, response } = await api.PUT("/api/locations/{location_id}/glyph", {
    params: { path: { location_id: locationId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not change this container's pictogram", error, response);
  }
  return data;
}

export async function assignLocationShortId(
  locationId: number,
  request: ShortIdRequest = {},
): Promise<ShortIdResponse> {
  const { data, error, response } = await api.POST("/api/locations/{location_id}/short-id", {
    params: { path: { location_id: locationId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not assign that printed id", error, response);
  }
  return data;
}

/** Every document attached to this one location — its own photo, overriding
 * its container type's, if it has one. */
export async function listLocationDocuments(locationId: number): Promise<LocationDocumentLinkList> {
  const { data, error, response } = await api.GET("/api/locations/{location_id}/documents", {
    params: { path: { location_id: locationId } },
  });
  if (error !== undefined) {
    fail("could not load this container's attached documents", error, response);
  }
  return data;
}

export async function attachLocationDocument(
  locationId: number,
  request: DocumentAttachRequest,
): Promise<DocumentAttachResult> {
  const { data, error, response } = await api.POST("/api/locations/{location_id}/documents", {
    params: { path: { location_id: locationId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not attach that document", error, response);
  }
  return data;
}

/** Detach this location's own photo. Falls back to the container type's,
 * which this call never touches — see `LocationRead.effective_photo`. */
export async function detachLocationDocument(
  locationId: number,
  sha256: string,
): Promise<DocumentDetachResult> {
  const { data, error, response } = await api.DELETE(
    "/api/locations/{location_id}/documents/{sha256}",
    { params: { path: { location_id: locationId, sha256 } } },
  );
  if (error !== undefined) {
    fail("could not remove that document", error, response);
  }
  return data;
}

// ------------------------------------------------------- container types ----
//
// The reusable template a cabinet or a baseplate is stamped from, and the
// canvas editor that authors its slot layout. `slot-template` is the one
// door onto that canvas — see `app.services.layout_authoring` for the half
// that decides what a merge, a split or a relabel actually means.

export async function listContainerTypes(
  options: { isSeed?: boolean } = {},
): Promise<ContainerTypeRead[]> {
  const { data, error, response } = await api.GET("/api/container-types", {
    params: { query: options.isSeed === undefined ? {} : { is_seed: options.isSeed } },
  });
  if (error !== undefined) {
    fail("could not load the container types", error, response);
  }
  return data;
}

/**
 * Author a brand-new type.
 *
 * `slug` is the one field with no `PATCH` counterpart — `ContainerTypeWrite`
 * omits it — so it is chosen exactly once, here. A collision comes back as a 409
 * `duplicate_slug`, checked before the insert rather than caught after it, so it
 * is a refusal a form can point at a field rather than a bare 500.
 */
export async function createContainerType(
  request: ContainerTypeCreate,
): Promise<ContainerTypeCreated> {
  const { data, error, response } = await api.POST("/api/container-types", { body: request });
  if (error !== undefined) {
    fail("could not create that container type", error, response);
  }
  return data;
}

export async function getContainerType(containerTypeId: number): Promise<ContainerTypeRead> {
  const { data, error, response } = await api.GET("/api/container-types/{container_type_id}", {
    params: { path: { container_type_id: containerTypeId } },
  });
  if (error !== undefined) {
    fail(`no container type ${containerTypeId}`, error, response);
  }
  return data;
}

/**
 * Edit a type's own fields (name, description, capacity, …) — never its
 * slot layout, which is `putSlotTemplate`'s job alone. A seed clones itself
 * first, which is why this carries `client_op_id` unlike `updatePart`: a
 * retried edit of a seed must replay rather than mint a second clone.
 */
export async function updateContainerType(
  containerTypeId: number,
  request: ContainerTypeUpdate,
): Promise<ContainerTypeEdited> {
  const { data, error, response } = await api.PATCH("/api/container-types/{container_type_id}", {
    params: { path: { container_type_id: containerTypeId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not save that container type", error, response);
  }
  return data;
}

/** "This cabinet is identical to that one" — clone with no edit attached. */
export async function cloneContainerType(
  containerTypeId: number,
  request: CloneRequest,
): Promise<ContainerTypeCreated> {
  const { data, error, response } = await api.POST(
    "/api/container-types/{container_type_id}/clone",
    { params: { path: { container_type_id: containerTypeId } }, body: request },
  );
  if (error !== undefined) {
    fail("could not clone that container type", error, response);
  }
  return data;
}

/** Every document attached to a container type — its photo, if it has one. */
export async function listContainerTypeDocuments(
  containerTypeId: number,
): Promise<ContainerTypeDocumentLinkList> {
  const { data, error, response } = await api.GET(
    "/api/container-types/{container_type_id}/documents",
    { params: { path: { container_type_id: containerTypeId } } },
  );
  if (error !== undefined) {
    fail("could not load this type's attached documents", error, response);
  }
  return data;
}

/** Attach an already-stored document to a container type, or promote its
 * existing link — "every instance of this type looks like this" for `role:
 * "photo"`. */
/**
 * Attach an already-stored document to a container type.
 *
 * A seed type is read-only, so this clones it first — `cloned` and
 * `container_type_id` on the response say whether the id just written is the one
 * that was asked for, exactly as `putSlotTemplate` does.
 */
export async function attachContainerTypeDocument(
  containerTypeId: number,
  request: DocumentAttachRequest,
): Promise<ContainerTypeDocumentAttached> {
  const { data, error, response } = await api.POST(
    "/api/container-types/{container_type_id}/documents",
    { params: { path: { container_type_id: containerTypeId } }, body: request },
  );
  if (error !== undefined) {
    fail("could not attach that document", error, response);
  }
  return data;
}

export async function detachContainerTypeDocument(
  containerTypeId: number,
  sha256: string,
): Promise<DocumentDetachResult> {
  const { data, error, response } = await api.DELETE(
    "/api/container-types/{container_type_id}/documents/{sha256}",
    { params: { path: { container_type_id: containerTypeId, sha256 } } },
  );
  if (error !== undefined) {
    fail("could not remove that document", error, response);
  }
  return data;
}

/** The type's current effective layout — generated or materialised, indistinguishably. */
export async function getSlotTemplate(containerTypeId: number): Promise<SlotTemplateRead> {
  const { data, error, response } = await api.GET(
    "/api/container-types/{container_type_id}/slot-template",
    { params: { path: { container_type_id: containerTypeId } } },
  );
  if (error !== undefined) {
    fail("could not load that container type's layout", error, response);
  }
  return data;
}

/**
 * Save the canvas. `slots` is always the complete desired layout, never a
 * delta. A seed clones first — `cloned` and `container_type_id` on the
 * response say whether the id just written is the one in the URL.
 */
export async function putSlotTemplate(
  containerTypeId: number,
  request: SlotTemplateWrite,
): Promise<SlotTemplateWritten> {
  const { data, error, response } = await api.PUT(
    "/api/container-types/{container_type_id}/slot-template",
    { params: { path: { container_type_id: containerTypeId } }, body: request },
  );
  if (error !== undefined) {
    fail("could not save that layout", error, response);
  }
  return data;
}

/**
 * "Give me N of these, in here" — the step that turns a type into containers you
 * can put parts in.
 *
 * Each instance materialises the type's layout into its **own** child locations
 * and keeps no live link back to the type, so editing the type afterwards touches
 * none of what this created. Idempotency-guarded because it writes a whole
 * subtree per instance: a retry without a key stamps the cabinet twice.
 *
 * Two refusals are worth distinguishing at the call site: a 422
 * `bad_naming_pattern` (the pattern is client text handed to `str.format`, and
 * only `{n}` may be substituted) and a 409 naming a hard geometric
 * incompatibility — `pitch_mismatch`, `footprint_too_wide`, `footprint_too_deep`
 * — which, unlike capacity, is never advisory: a 42 mm bin does not seat on a
 * 50 mm plate.
 */
export async function instantiateContainers(
  locationId: number,
  request: InstantiateRequest,
): Promise<InstantiateResponse> {
  const { data, error, response } = await api.POST("/api/locations/{location_id}/instantiate", {
    params: { path: { location_id: locationId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not create those containers", error, response);
  }
  return data;
}

/**
 * Grid + tag + contents state for one location's own children — the same
 * document the provisioning and verification walks read, so an instance's
 * layout editor never guesses at what a slot holds.
 */
export async function getLocationLayout(locationId: number): Promise<LayoutRead> {
  const { data, error, response } = await api.GET("/api/locations/{location_id}/layout", {
    params: { path: { location_id: locationId } },
  });
  if (error !== undefined) {
    fail("could not load that container's layout", error, response);
  }
  return data;
}

/**
 * Edit an already-instantiated location's own layout, through the change
 * guard. Never touches `container_types` — editing a type and pushing a
 * change into one of its instances are deliberately two different calls.
 *
 * Three outcomes, distinguished by the caller on the thrown `ApiError`:
 * success (every change was safe), a 409 naming every slot a deletion is
 * blocked on (`reason: "slots_hold_content"`, see `AffectedSlotProblem` in
 * `lib/api/errors.ts`), or a 422 for a structurally refused request (most
 * often `slot_identity_reinterpreted`).
 */
export async function reapplyLayout(
  locationId: number,
  request: ReapplyLayoutRequest,
): Promise<ReapplyLayoutResponse> {
  const { data, error, response } = await api.POST("/api/locations/{location_id}/reapply-layout", {
    params: { path: { location_id: locationId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not reapply that layout", error, response);
  }
  return data;
}

// ---------------------------------------------------------------- intake ----

/**
 * Park a scan on the server.
 *
 * Idempotent on `client_op_id`, which the scan minted before this call, so a
 * retry after a lost response returns the same entry with `already_queued: true`
 * rather than duplicating it. That is the normal case at a shelf with bad wifi,
 * not an edge case — which is what makes re-sending a whole synced queue safe.
 */
export async function parkScan(request: PendingIntakeIn): Promise<PendingIntakeCreated> {
  const { data, error, response } = await api.POST("/api/intake/pending", { body: request });
  if (error !== undefined) {
    fail("could not park that scan", error, response);
  }
  return data;
}

/** The worklist by default; pass every status for the full history. */
export async function listPendingIntake(
  options: { status?: PendingIntakeStatus[]; deviceId?: string; limit?: number } = {},
): Promise<PendingIntakeList> {
  const { data, error, response } = await api.GET("/api/intake/pending", {
    params: {
      query: {
        ...(options.status === undefined ? {} : { status: options.status }),
        ...(options.deviceId === undefined ? {} : { device_id: options.deviceId }),
        ...(options.limit === undefined ? {} : { limit: options.limit }),
      },
    },
  });
  if (error !== undefined) {
    fail("could not load the intake queue", error, response);
  }
  return data;
}

/** Mark an entry dealt with. Records the outcome; it does not perform it. */
export async function resolvePendingIntake(
  entryId: number,
  request: { resolved_part_id?: number | null; note?: string | null } = {},
): Promise<PendingIntakeRead> {
  const { data, error, response } = await api.POST("/api/intake/pending/{entry_id}/resolve", {
    params: { path: { entry_id: entryId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not resolve that entry", error, response);
  }
  return data;
}

/** Not a real intake: a duplicate scan, a shipping label, someone else's box. */
export async function dismissPendingIntake(
  entryId: number,
  request: { note?: string | null } = {},
): Promise<PendingIntakeRead> {
  const { data, error, response } = await api.POST("/api/intake/pending/{entry_id}/dismiss", {
    params: { path: { entry_id: entryId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not dismiss that entry", error, response);
  }
  return data;
}

/** Undo a wrong resolve or dismiss — a status change, not a compensating row. */
export async function reopenPendingIntake(entryId: number): Promise<PendingIntakeRead> {
  const { data, error, response } = await api.POST("/api/intake/pending/{entry_id}/reopen", {
    params: { path: { entry_id: entryId } },
  });
  if (error !== undefined) {
    fail("could not reopen that entry", error, response);
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
 * A cart's worth of takes and returns against one container, in one request.
 *
 * ADR 0007's third destination — "pick a container, scan it and say how many
 * parts you took or put back". **This never 4xx's for a bad line**: every line
 * comes back in `results` with `applied` true or false plus a `reason`, and a
 * good line is not rolled back because a later one was refused. That is what
 * lets the cart keep the refused rows and drop the rest, exactly as
 * `syncIntakeQueue` does. The whole batch shares one `group_uuid`, so undoing
 * the checkout is a single `undoMovement`.
 */
export async function moveStockBatch(
  request: BatchMovementRequest,
): Promise<BatchMovementResponse> {
  const { data, error, response } = await api.POST("/api/stock/movements", { body: request });
  if (error !== undefined) {
    fail("could not record those movements", error, response);
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

// -------------------------------------------------------------- projects ----

export async function listProjects(
  options: { status?: ProjectStatus[]; limit?: number; offset?: number } = {},
): Promise<ProjectList> {
  const { data, error, response } = await api.GET("/api/projects", {
    params: {
      query: {
        ...(options.status === undefined ? {} : { status: options.status }),
        ...(options.limit === undefined ? {} : { limit: options.limit }),
        ...(options.offset === undefined ? {} : { offset: options.offset }),
      },
    },
  });
  if (error !== undefined) {
    fail("could not load the projects", error, response);
  }
  return data;
}

export async function createProject(request: ProjectCreate): Promise<ProjectCreated> {
  const { data, error, response } = await api.POST("/api/projects", { body: request });
  if (error !== undefined) {
    fail("could not create that project", error, response);
  }
  return data;
}

export async function getProject(projectId: number): Promise<ProjectRead> {
  const { data, error, response } = await api.GET("/api/projects/{project_id}", {
    params: { path: { project_id: projectId } },
  });
  if (error !== undefined) {
    fail(`no project ${projectId}`, error, response);
  }
  return data;
}

/** Unguarded, like `updatePart`: a replayed PATCH is the same fields set to
 * the same values, so there is nothing an idempotency key would protect. */
export async function updateProject(
  projectId: number,
  request: ProjectUpdate,
): Promise<ProjectRead> {
  const { data, error, response } = await api.PATCH("/api/projects/{project_id}", {
    params: { path: { project_id: projectId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not save that project", error, response);
  }
  return data;
}

// -------------------------------------------------------------------- BOM ----

/**
 * Land a CSV/TSV export as `bom_lines`. **Appends; never replaces** — a
 * re-import of the same file doubles every line, so the caller is responsible
 * for only importing once per revision. Idempotency-guarded on the server, so
 * a retried upload after a lost response does not double anyway.
 */
export async function importBom(
  projectId: number,
  request: BomImportRequest,
): Promise<BomImportResponse> {
  const { data, error, response } = await api.POST("/api/projects/{project_id}/bom/import", {
    params: { path: { project_id: projectId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not import that BOM", error, response);
  }
  return data;
}

export async function listBomLines(
  projectId: number,
  options: { unmatchedOnly?: boolean; limit?: number; offset?: number } = {},
): Promise<BomLineList> {
  const { data, error, response } = await api.GET("/api/projects/{project_id}/bom", {
    params: {
      path: { project_id: projectId },
      query: {
        ...(options.unmatchedOnly === undefined ? {} : { unmatched_only: options.unmatchedOnly }),
        ...(options.limit === undefined ? {} : { limit: options.limit }),
        ...(options.offset === undefined ? {} : { offset: options.offset }),
      },
    },
  });
  if (error !== undefined) {
    fail("could not load the BOM", error, response);
  }
  return data;
}

/** A batch of per-line edits — corrections, DNP toggles, and manual matching. */
export async function updateBomLines(
  projectId: number,
  request: BomLinesUpdateRequest,
): Promise<BomLinesUpdateResponse> {
  const { data, error, response } = await api.PUT("/api/projects/{project_id}/bom", {
    params: { path: { project_id: projectId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not save those BOM edits", error, response);
  }
  return data;
}

// ------------------------------------------------------------ requirements --

/**
 * Prose in, ranked candidates out — the fuzzy front door, not a match.
 *
 * Writes nothing: `app.api.routes.requirements` is a pure read, so there is no
 * `client_op_id` here to guard. Accepting a candidate is an ordinary BOM edit
 * through `updateBomLines`, same as a hand-typed line — see
 * `SuggestionLineRead`'s server-side docstring.
 */
export async function suggestRequirements(
  request: SuggestionRequest,
): Promise<SuggestionBatchResponse> {
  const { data, error, response } = await api.POST("/api/requirements/suggest", { body: request });
  if (error !== undefined) {
    fail("could not read those requirements", error, response);
  }
  return data;
}

// ----------------------------------------------------------------- builds ----

export async function createBuild(projectId: number, request: BuildCreate): Promise<BuildCreated> {
  const { data, error, response } = await api.POST("/api/projects/{project_id}/builds", {
    params: { path: { project_id: projectId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not plan that build", error, response);
  }
  return data;
}

export async function getBuild(buildId: number): Promise<BuildRead> {
  const { data, error, response } = await api.GET("/api/builds/{build_id}", {
    params: { path: { build_id: buildId } },
  });
  if (error !== undefined) {
    fail(`no build ${buildId}`, error, response);
  }
  return data;
}

/** Edit a build, including the status transition that closes it and releases
 * its reservations — see `BuildUpdate`'s server-side docstring. Idempotent by
 * construction, like `updateProject`. */
export async function updateBuild(buildId: number, request: BuildUpdate): Promise<BuildRead> {
  const { data, error, response } = await api.PATCH("/api/builds/{build_id}", {
    params: { path: { build_id: buildId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not save that build", error, response);
  }
  return data;
}

/** What stands between this build and being built, line by line. A pure read. */
export async function getShortages(buildId: number): Promise<ShortageResponse> {
  const { data, error, response } = await api.GET("/api/builds/{build_id}/shortages", {
    params: { path: { build_id: buildId } },
  });
  if (error !== undefined) {
    fail("could not load the shortage report", error, response);
  }
  return data;
}

export async function allocateStock(
  buildId: number,
  request: AllocateRequest,
): Promise<AllocateResponse> {
  const { data, error, response } = await api.POST("/api/builds/{build_id}/allocate", {
    params: { path: { build_id: buildId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not reserve that stock", error, response);
  }
  return data;
}

/**
 * A cart's worth of holds against one build, in one request (ADR 0007).
 *
 * Lines are applied **in order**, so two lines drawing on the same lot compete
 * exactly as two separate requests would; the second is refused with
 * `available_milli` rather than quietly overcommitting. Per-line `client_op_id`
 * is not optional in practice: `stock_allocations` has no UNIQUE to fall back
 * on, so a cart resent after one refusal would otherwise double every hold that
 * had already been placed.
 */
export async function allocateStockBatch(
  buildId: number,
  request: AllocateBatchRequest,
): Promise<AllocateBatchResponse> {
  const { data, error, response } = await api.POST("/api/builds/{build_id}/allocate-batch", {
    params: { path: { build_id: buildId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not reserve that stock", error, response);
  }
  return data;
}

/** Release one hold (`allocationId` given) or every open hold this build has. */
export async function releaseStock(
  buildId: number,
  request: ReleaseRequest,
): Promise<ReleaseResponse> {
  const { data, error, response } = await api.POST("/api/builds/{build_id}/release", {
    params: { path: { build_id: buildId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not release that hold", error, response);
  }
  return data;
}

// ----------------------------------------------------- staging (ADR 0004) ----

/**
 * Withdraw parts to a project, or to one of its assemblies — ADR 0004's
 * namesake workflow, and the reason these three functions exist at all: review
 * found the routes implemented, tested and in the schema with **nothing in the
 * frontend calling them**, so the one gesture the ADR was written for could not
 * be performed.
 *
 * **This moves real stock**, so it is `client_op_id`-guarded like every other
 * movement: the source bin's count drops in the same transaction, and a retried
 * request without a key empties the drawer twice. `assembly_no` omitted means
 * the project's floating parts; given, it is the specific unit.
 */
export async function stageStock(
  buildId: number,
  request: StageRequest,
): Promise<StageResponse> {
  const { data, error, response } = await api.POST("/api/builds/{build_id}/stage", {
    params: { path: { build_id: buildId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not send those parts to the project", error, response);
  }
  return data;
}

/**
 * Put a staged withdrawal back on the shelf — the ledger's existing undo, not a
 * fresh move in the opposite direction, so the history reads "this happened,
 * then it was undone".
 *
 * Refused (409) when the box no longer holds it all, or when the row is a
 * remainder with no single movement to compensate. Both are worth surfacing
 * verbatim: the message names the quantity and says what to do instead.
 */
export async function unstageStock(
  buildId: number,
  request: UnstageRequest,
): Promise<UnstageResponse> {
  const { data, error, response } = await api.POST("/api/builds/{build_id}/unstage", {
    params: { path: { build_id: buildId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not put those parts back", error, response);
  }
  return data;
}

/**
 * Build staged parts into the assembly: `staged → consumed`. Consumes the
 * *project box's* lot, because that is where the parts are — the bin's count
 * dropped when they were staged.
 *
 * `qty_milli` below the staged quantity is the normal case, not an edge one: a
 * half-populated board leaves the remainder staged.
 */
export async function consumeStaged(
  buildId: number,
  request: ConsumeStagedRequest,
): Promise<ConsumeStagedResponse> {
  const { data, error, response } = await api.POST("/api/builds/{build_id}/consume-staged", {
    params: { path: { build_id: buildId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not record those parts as built in", error, response);
  }
  return data;
}

// ----------------------------------------------- roster and the pick list ----

/**
 * What this build actually used, corrections included. A pure read.
 *
 * Distinct from `getShortages`, which asks "can this be built": this asks "what
 * went into it", so it also reports parts consumed that no BOM line asked for
 * and marks every row somebody wrote down after the fact.
 */
export async function getRoster(buildId: number): Promise<RosterResponse> {
  const { data, error, response } = await api.GET("/api/builds/{build_id}/roster", {
    params: { path: { build_id: buildId } },
  });
  if (error !== undefined) {
    fail("could not load the roster", error, response);
  }
  return data;
}

/**
 * Record a part that was really used but never tracked.
 *
 * Guarded by `client_op_id` for the sharper reason than usual: the server's
 * ledger is append-only, so a doubled correction can only be taken back by
 * writing a third row — and the user reaching for this is already
 * reconstructing history by hand. No `source` field: the server forces
 * `reconciled`, which is what makes the roster's own edits visible.
 */
export async function recordUsed(
  buildId: number,
  request: RecordUsedRequest,
): Promise<RecordUsedResponse> {
  const { data, error, response } = await api.POST("/api/builds/{build_id}/record-used", {
    params: { path: { build_id: buildId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not record that part as used", error, response);
  }
  return data;
}

/**
 * Where to go and what to take, **already in walking order** — the server sorts
 * the stops by `locations.id_path`. Never re-sort this by BOM line on the way to
 * the screen; that ordering is the entire feature.
 */
export async function getPickList(buildId: number): Promise<PickListResponse> {
  const { data, error, response } = await api.GET("/api/builds/{build_id}/pick-list", {
    params: { path: { build_id: buildId } },
  });
  if (error !== undefined) {
    fail("could not load the pick list", error, response);
  }
  return data;
}

// ---------------------------------------------------------------- enrichment --

/**
 * The review queue: every pending candidate, grouped by part then by field.
 *
 * `partId` scopes to one part's fields (the "one pass" view from `PartScreen`);
 * omitted, this is the whole worklist, capped at `limit` distinct parts —
 * `total_parts` still reports the true count, so a badge does not undercount.
 */
export async function getEnrichmentQueue(
  options: { partId?: number; limit?: number } = {},
): Promise<EnrichmentQueueResponse> {
  const { data, error, response } = await api.GET("/api/enrichment/candidates", {
    params: {
      query: {
        ...(options.partId === undefined ? {} : { part_id: options.partId }),
        ...(options.limit === undefined ? {} : { limit: options.limit }),
      },
    },
  });
  if (error !== undefined) {
    fail("could not load the review queue", error, response);
  }
  return data;
}

/**
 * Take a candidate's value exactly as its source wrote it.
 *
 * Returns the field's post-action state — normally with `candidates: []`,
 * because accepting one candidate closes every other pending one for the same
 * field too (see the server docstring): the caller's job is to drop the field
 * from view when the list comes back empty, not to re-fetch the whole queue.
 */
export async function acceptEnrichmentCandidate(candidateId: number): Promise<EnrichmentFieldGroup> {
  const { data, error, response } = await api.POST("/api/enrichment/candidates/{candidate_id}/accept", {
    params: { path: { candidate_id: candidateId } },
  });
  if (error !== undefined) {
    fail("could not accept that value", error, response);
  }
  return data;
}

/**
 * A human's replacement value. Recorded as a fresh `manual` candidate — which
 * outranks every automated source — and promoted in the same call.
 */
export async function correctEnrichmentCandidate(
  candidateId: number,
  request: EnrichmentCorrectRequest,
): Promise<EnrichmentFieldGroup> {
  const { data, error, response } = await api.POST("/api/enrichment/candidates/{candidate_id}/correct", {
    params: { path: { candidate_id: candidateId } },
    body: request,
  });
  if (error !== undefined) {
    fail("could not save that correction", error, response);
  }
  return data;
}

/** A human said no to this one reading. Does not touch its siblings. */
export async function dismissEnrichmentCandidate(candidateId: number): Promise<EnrichmentCandidateRead> {
  const { data, error, response } = await api.POST("/api/enrichment/candidates/{candidate_id}/dismiss", {
    params: { path: { candidate_id: candidateId } },
  });
  if (error !== undefined) {
    fail("could not dismiss that candidate", error, response);
  }
  return data;
}

/**
 * Accept many candidates in one call — the common case of a whole decoded
 * family being obviously right. One bad id is reported in `results`, not
 * thrown, so the caller can show a partial success rather than losing the
 * whole batch to it.
 */
export async function bulkAcceptEnrichmentCandidates(
  candidateIds: number[],
): Promise<EnrichmentBulkAcceptResponse> {
  const { data, error, response } = await api.POST("/api/enrichment/candidates/bulk-accept", {
    body: { candidate_ids: candidateIds },
  });
  if (error !== undefined) {
    fail("could not accept that batch", error, response);
  }
  return data;
}
