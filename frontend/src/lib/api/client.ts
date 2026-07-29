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

// --- layout authoring: container types, their canvas, and one instance's --
// own copy of it (docs/PLAN.md, "Layout authoring"; ADR 0002). ---------------
export type ContainerTypeRead = Schemas["ContainerTypeRead"];
export type ContainerTypeUpdate = Schemas["ContainerTypeUpdate"];
export type ContainerTypeCreated = Schemas["ContainerTypeCreated"];
export type ContainerTypeEdited = Schemas["ContainerTypeEdited"];
export type CloneRequest = Schemas["CloneRequest"];
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

export type AllocationRead = Schemas["AllocationRead"];
export type AllocateRequest = Schemas["AllocateRequest"];
export type AllocateResponse = Schemas["AllocateResponse"];
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
