/**
 * One build, in three tenses. Each tab answers a different question, and they
 * are separate tabs because merging any two of them loses the distinction that
 * makes it useful:
 *
 * * **Shortages — can this be built?** The load-bearing distinction here is
 *   that `ShortageKind.SHORT` and `ShortageKind.UNIDENTIFIED` are not the same
 *   failure and must not read the same. A short line has a known part and a
 *   number — it is fixed by ordering more. An unidentified line
 *   (`bom_lines.part_id IS NULL`) has no part to check availability against at
 *   all, so `available_milli`/`shortfall_milli` come back `null` rather than
 *   zero; rendering that as "0 short" would be the false-green this whole
 *   report exists to prevent. So SHORT is red with a number, UNIDENTIFIED is
 *   amber with "needs identification" and a link to the BOM screen — different
 *   hue, different glyph, different words, different next action.
 * * **Pick list — where do I get them?** Rendered in exactly the order the
 *   server sent, which is `locations.id_path` order and therefore one stop per
 *   drawer with a cabinet's drawers consecutive. Re-sorting these by BOM line
 *   in the UI would silently undo the whole feature, so the render never sorts.
 * * **Roster — what actually went in?** The one view that has to admit it might
 *   be wrong. A row somebody typed in after the fact is marked as such rather
 *   than blended in with the movements the system witnessed
 *   (`is_after_the_fact`, from `stock_ledger.source == "reconciled"`), and parts
 *   consumed that no BOM line asked for get their own section — on an iterating
 *   prototype a stale BOM is the normal case, not an error.
 */

import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import { OpenTargetButton } from "../components/OpenTargetButton";
import { PathBar } from "../components/PathBar";
import {
  allocateStock,
  consumeStaged,
  getBuild,
  getProject,
  getPart,
  getPickList,
  getRoster,
  getShortages,
  recordUsed,
  releaseStock,
  stageStock,
  unstageStock,
  updateBuild,
  type BuildRead,
  type ProjectRead,
  type BuildStatus,
  type BuildUpdate,
  type LineShortageRead,
  type PartRead,
  type PickGapRead,
  type PickListResponse,
  type PickStopRead,
  type RosterEntryRead,
  type RosterLineRead,
  type RosterResponse,
  type ShortageResponse,
} from "../lib/api/client";
import { formatQty } from "../lib/format";
import { useAsync } from "../lib/hooks/useAsync";
import { uuid4 } from "../lib/scan/session";

/** The three tenses: what is missing, where to fetch it, what went in. */
const TABS = ["shortages", "pick", "roster"] as const;
type Tab = (typeof TABS)[number];

const TAB_LABELS: Record<Tab, string> = {
  shortages: "Shortages",
  pick: "Pick list",
  roster: "Roster",
};

export function BuildScreen() {
  const { buildId: raw } = useParams();
  const buildId = Number(raw);
  const valid = Number.isSafeInteger(buildId) && buildId > 0;

  const build = useAsync<BuildRead | null>(
    () => (valid ? getBuild(buildId) : Promise.resolve(null)),
    [buildId, valid],
  );

  if (!valid) {
    return <Notice kind="error" title="That is not a build id" />;
  }
  if (build.error !== null) {
    return <ErrorBanner error={build.error} fallback="That build could not be loaded." />;
  }
  if (build.data === null) {
    return <Loading what="the build" />;
  }
  return <Build build={build.data} onChanged={build.reload} />;
}

function Build({ build, onChanged }: { build: BuildRead; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  /**
   * The project this build belongs to, fetched for one reason: its name.
   *
   * `BuildRead` carries `project_id` and no name, and a trail whose middle step
   * reads "Project" is a worse thing to show than the extra request is to make —
   * the point of the path is to say *which* project you are inside. Until it
   * arrives the crumb still links, labelled from the id.
   */
  const project = useAsync<ProjectRead | null>(
    () => getProject(build.project_id),
    [build.project_id],
  );
  const [tab, setTab] = useState<Tab>("shortages");
  const shortages = useAsync<ShortageResponse>(() => getShortages(build.id), [build.id]);
  // Release-all had no rejection path at all: a 409 (a staged row that cannot be
  // given back) surfaced as an uncaught promise and the button simply sat there,
  // which is the exact silent failure every sibling action on this screen already
  // avoids. Busy state too, so a double tap cannot fire it twice.
  const [releasing, setReleasing] = useState(false);
  const [releaseError, setReleaseError] = useState<unknown>(null);

  const closed = build.status === "completed" || build.status === "abandoned";

  async function releaseAll(): Promise<void> {
    setReleasing(true);
    setReleaseError(null);
    try {
      await releaseStock(build.id, { client_op_id: uuid4() });
      shortages.reload();
      onChanged();
    } catch (cause) {
      setReleaseError(cause);
    } finally {
      setReleasing(false);
    }
  }

  return (
    <div className="stack">
      <div className="card">
        <PathBar
          trail={[
            { key: "projects", label: "Projects", to: "/projects" },
            {
              key: `project-${build.project_id}`,
              label: project.data?.name ?? `project ${build.project_id}`,
              to: `/projects/${build.project_id}`,
            },
            { key: `build-${build.id}`, label: `Build #${build.build_no}` },
          ]}
          label="Build path"
        />
        <div className="row">
          <h1 style={{ flex: 1 }}>
            Build #{build.build_no}
            {build.label !== null && ` — ${build.label}`}
          </h1>
          <StatusBadge status={build.status} />
        </div>
        <p className="muted-note" style={{ margin: 0 }}>
          {build.assembly_count} assembl{build.assembly_count === 1 ? "y" : "ies"}
          {build.bom_revision !== null && ` · planned against BOM ${build.bom_revision}`}
        </p>
        {build.notes !== null && <p style={{ margin: 0 }}>{build.notes}</p>}
        <div className="row">
          {/* ADR 0010: a build is a tab in its own right — "kitting rev C" is a
              build, not a project — and opening it here is what aims every later
              take at it. */}
          <OpenTargetButton
            target={{
              kind: "build",
              buildId: build.id,
              label: `Build #${build.build_no}${build.label === null ? "" : ` — ${build.label}`}`,
            }}
          />
          <button type="button" onClick={() => setEditing(!editing)}>
            {editing ? "Cancel" : "Edit"}
          </button>
          {!closed && (
            <button
              type="button"
              className="danger"
              disabled={releasing}
              onClick={() => void releaseAll()}
            >
              {releasing ? "Releasing…" : "Release all holds"}
            </button>
          )}
        </div>
        <ErrorBanner error={releaseError} fallback="Those holds were not released." />
      </div>

      {editing && (
        <EditBuild
          build={build}
          onDone={() => {
            setEditing(false);
            shortages.reload();
            onChanged();
          }}
        />
      )}

      {closed && (
        <Notice kind="info" title={`This build is ${build.status}`}>
          Every open reservation was released when it closed. The shortage report
          still shows what the build needed, for the record — and the roster still
          accepts corrections, which is exactly when most of them get made.
        </Notice>
      )}

      <div className="segmented" role="group" aria-label="Build view">
        {TABS.map((value) => (
          <button
            key={value}
            type="button"
            aria-pressed={tab === value}
            onClick={() => setTab(value)}
          >
            {TAB_LABELS[value]}
          </button>
        ))}
      </div>

      {tab === "shortages" && (
        <>
          <ErrorBanner
            error={shortages.error}
            fallback="The shortage report could not be loaded."
          />
          {shortages.data === null ? (
            <Loading what="the shortage report" />
          ) : (
            <ShortageReport build={build} report={shortages.data} onChanged={shortages.reload} />
          )}
        </>
      )}
      {tab === "pick" && <PickListView build={build} />}
      {/* The roster reloads the shortage report too: a correction consumes real
          stock, so leaving a cached "buildable" on the other tab would be the
          same stale-number failure ADR 0004 rejects a bookkeeping-only flag for. */}
      {tab === "roster" && <RosterView build={build} onRecorded={shortages.reload} />}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  if (status === "completed") {
    return <span className="badge badge-good">completed</span>;
  }
  if (status === "abandoned") {
    return <span className="badge badge-bad">abandoned</span>;
  }
  return <span className="badge">{status === "in_progress" ? "in progress" : status}</span>;
}

function EditBuild({ build, onDone }: { build: BuildRead; onDone: () => void }) {
  const [draft, setDraft] = useState<BuildUpdate>({
    label: build.label,
    notes: build.notes,
    status: build.status as BuildStatus,
    assembly_count: build.assembly_count,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const closesBuild =
    draft.status !== undefined &&
    draft.status !== null &&
    draft.status !== build.status &&
    (draft.status === "completed" || draft.status === "abandoned");

  // No recomputation step exists, and none is needed: `needed_milli` on the
  // shortage report is `qty_per_assembly_milli * assembly_count`, read fresh
  // on every request. Saving this only changes the number the next read
  // multiplies by — the parent reloads the shortage report right after
  // `onDone`, which is what actually moves the shortfall on screen.
  const raisingAssemblyCount =
    draft.assembly_count !== undefined &&
    draft.assembly_count !== null &&
    draft.assembly_count > build.assembly_count;

  async function save(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await updateBuild(build.id, draft);
      onDone();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="card"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <h3>Edit build</h3>
      <label className="field">
        <span>Label</span>
        <input
          value={draft.label ?? ""}
          onChange={(event) => setDraft({ ...draft, label: event.target.value })}
        />
      </label>
      <label className="field">
        <span>Status</span>
        <select
          value={draft.status ?? build.status}
          onChange={(event) => setDraft({ ...draft, status: event.target.value as BuildStatus })}
        >
          <option value="planned">Planned</option>
          <option value="in_progress">In progress</option>
          <option value="completed">Completed</option>
          <option value="abandoned">Abandoned</option>
        </select>
      </label>
      <label className="field">
        <span>Assemblies</span>
        <input
          type="number"
          min={1}
          value={draft.assembly_count ?? build.assembly_count}
          onChange={(event) =>
            setDraft({
              ...draft,
              assembly_count: Math.max(1, Number(event.target.value) || 1),
            })
          }
        />
      </label>
      <label className="field">
        <span>Notes</span>
        <textarea
          rows={3}
          value={draft.notes ?? ""}
          onChange={(event) => setDraft({ ...draft, notes: event.target.value })}
        />
      </label>
      {raisingAssemblyCount && (
        <p className="muted-note" style={{ margin: 0 }}>
          Raising this marks the extra parts as needed on the shortage report —
          nothing is reserved or moved automatically; it only shows what the build
          now needs.
        </p>
      )}
      {closesBuild && (
        <Notice kind="warn" title="This closes the build">
          Every reservation it still holds is released immediately — nothing else
          ever comes back to free them.
        </Notice>
      )}
      <ErrorBanner error={error} fallback="Those changes were not saved." />
      <button type="submit" className="primary wide" disabled={busy}>
        {busy ? "Saving…" : "Save"}
      </button>
    </form>
  );
}

// -------------------------------------------------------------- shortages --

function ShortageReport({
  build,
  report,
  onChanged,
}: {
  build: BuildRead;
  report: ShortageResponse;
  onChanged: () => void;
}) {
  const shortCount = report.lines.filter((line) => line.kind === "short").length;
  const unidentifiedCount = report.lines.filter((line) => line.kind === "unidentified").length;

  return (
    <div className="stack">
      <Notice
        kind={report.is_buildable ? "ok" : "error"}
        title={report.is_buildable ? "Buildable" : "Not buildable"}
      >
        {report.is_buildable ? (
          <p style={{ margin: 0 }}>
            Every line is either satisfied, not fitted, or already covered by what
            this build holds.
          </p>
        ) : (
          <p style={{ margin: 0 }}>
            {shortCount > 0 && `${shortCount} line(s) short of stock. `}
            {unidentifiedCount > 0 &&
              `${unidentifiedCount} line(s) have no part identified at all, so they cannot ` +
                "even be checked for availability."}
          </p>
        )}
      </Notice>

      <ul className="list">
        {report.lines.map((line) => (
          <ShortageLine
            key={line.bom_line_id}
            buildId={build.id}
            projectId={build.project_id}
            assemblyCount={build.assembly_count}
            closed={build.status === "completed" || build.status === "abandoned"}
            line={line}
            onChanged={onChanged}
          />
        ))}
      </ul>
    </div>
  );
}

function ShortageLine({
  buildId,
  projectId,
  assemblyCount,
  closed,
  line,
  onChanged,
}: {
  buildId: number;
  projectId: number;
  assemblyCount: number;
  closed: boolean;
  line: LineShortageRead;
  onChanged: () => void;
}) {
  const [reserving, setReserving] = useState(false);
  const [staging, setStaging] = useState(false);

  return (
    <li>
      <div className="list-item">
        <div className="row">
          <span className="title" style={{ flex: 1 }}>
            Line {line.line_no}
          </span>
          <KindBadge line={line} />
        </div>
        {/* **The arithmetic, not just its answer.** ADR 0007 names two numbers
            the UI was not making legible: "how many is needed per build and how
            many are being used". Both are already derived (ADR 0004) — what was
            missing is that a bare "15 required" asserts a product without its
            factors, so raising the assembly count moved a number with no visible
            cause. Written out, the same reload that changes ×3 to ×5 visibly
            changes the demand and the shortfall together. */}
        <DemandLine line={line} assemblyCount={assemblyCount} />
        <div className="sub">
          {line.needed_milli > 0
            ? `${formatQty(line.needed_milli)} still to get`
            : "nothing left to get"}
          {line.available_milli !== null && ` · ${formatQty(line.available_milli)} free in stock`}
          {line.shortfall_milli !== null && line.shortfall_milli > 0 && (
            <>
              {" · "}
              {/* A word and a symbol, not only a colour: `KindBadge` says
                  "short" in text and this repeats the number in the same breath,
                  so the one state that stops a build never depends on hue. */}
              <strong>▲ {formatQty(line.shortfall_milli)} not in stock anywhere</strong>
            </>
          )}
        </div>
        {/* **The split, not the sum.** This line used to render `allocated_milli`
            alone — the merge of all three — so three units held in a drawer, three
            in a project box on a shelf and three already soldered into board #1
            read identically, and `needed_milli` (the number ADR 0004 exists to
            surface) was never shown at all. The ADR's consequence is explicit that
            the UI has to keep them distinguishable: each has a different next
            action, and only the first can be released. Zeroes are dropped rather
            than printed, so an ordinary line stays one short phrase. */}
        {line.allocated_milli > 0 && (
          <div className="sub">
            {"In use — "}
            {[
              line.reserved_milli > 0 && `${formatQty(line.reserved_milli)} held in a bin`,
              line.staged_milli > 0 && `${formatQty(line.staged_milli)} set aside for this project`,
              line.consumed_milli > 0 && `${formatQty(line.consumed_milli)} built in`,
            ]
              .filter((part): part is string => part !== false)
              .join(" · ")}
          </div>
        )}
        {/* Without this line the report looks self-contradictory: "100 required,
            100 already held" beside "short 100". The held stock is real as a
            record and gone as inventory — the bin was emptied, or another build
            was promised it first — and saying so is the difference between a
            confusing number and an actionable one. */}
        {line.undeliverable_milli > 0 && (
          <div className="sub">
            {formatQty(line.undeliverable_milli)} of that hold can no longer be filled from its
            lot — release it or recount the bin.
          </div>
        )}
        {line.part_id !== null && (
          <div className="sub">
            <Link to={`/parts/${line.part_id}`}>part #{line.part_id}</Link>
          </div>
        )}
        {line.kind === "unidentified" && (
          <div className="sub">
            No part is matched to this line yet.{" "}
            <Link to={`/projects/${projectId}/bom?unmatched=1`}>Match it in the BOM →</Link>
          </div>
        )}
        {line.substitute_part_ids.length > 0 && (
          <div className="sub">
            Accepted substitute(s):{" "}
            {line.substitute_part_ids.map((id, index) => (
              <span key={id}>
                {index > 0 && ", "}
                <Link to={`/parts/${id}`}>#{id}</Link>
              </span>
            ))}
          </div>
        )}

        {line.part_id !== null && !closed && (
          <div className="row">
            {line.kind === "short" && (
              <button type="button" onClick={() => setReserving(!reserving)}>
                {reserving ? "Cancel" : "Reserve stock"}
              </button>
            )}
            {/* ADR 0004's namesake gesture, which had no button anywhere: the
                routes shipped, were tested, and nothing in the frontend called
                them. Offered on every identified line rather than only short ones
                — taking parts out to a project is what you do once the line *is*
                covered, and refusing to offer it there would be offering it only
                where it is least useful. */}
            <button type="button" onClick={() => setStaging(!staging)}>
              {staging ? "Cancel" : "Send to project"}
            </button>
          </div>
        )}
        {staging && line.part_id !== null && (
          <StageStock
            buildId={buildId}
            assemblyCount={assemblyCount}
            partId={line.part_id}
            bomLineId={line.bom_line_id}
            defaultQtyMilli={line.needed_milli}
            onDone={() => {
              setStaging(false);
              onChanged();
            }}
          />
        )}
        {reserving && line.part_id !== null && (
          <ReserveStock
            buildId={buildId}
            partId={line.part_id}
            bomLineId={line.bom_line_id}
            // Held minus the part of it its lot can no longer fill: an
            // undeliverable hold otherwise reads as covering the line, and the
            // one-tap quantity would be zero on exactly the line that needs the
            // most attention.
            defaultQtyMilli={Math.max(
              0,
              line.required_milli - (line.allocated_milli - line.undeliverable_milli),
            )}
            onDone={() => {
              setReserving(false);
              onChanged();
            }}
          />
        )}
      </div>
    </li>
  );
}

/**
 * `per assembly × assemblies = needed for this build`, and what is already in use.
 *
 * Two phrases, because they answer the two different questions Iliana asked in
 * one breath — *how many does this build need* and *how many are being used*. The
 * per-assembly figure comes off the wire rather than being divided back out of
 * `required_milli`: a DNP line reports zero required on purpose, and the division
 * would erase the only quantity such a line has.
 *
 * The "in use" half is the *next* line of the row, where the three states are
 * spelled out rather than summed — ADR 0004 is explicit that merging them lets a
 * BOM look covered off parts already soldered into last week's board, and each has
 * a different next action. All this one adds is the "none of it in use yet" case,
 * which that line cannot state because it does not render at all when there is
 * nothing to report.
 */
function DemandLine({
  line,
  assemblyCount,
}: {
  line: LineShortageRead;
  assemblyCount: number;
}) {
  return (
    <div className="sub">
      {line.kind === "not_fitted" ? (
        <>{formatQty(line.qty_per_assembly_milli)} per assembly, not fitted — 0 required</>
      ) : (
        <>
          <strong>{formatQty(line.qty_per_assembly_milli)}</strong> per assembly × {assemblyCount}{" "}
          {assemblyCount === 1 ? "assembly" : "assemblies"} ={" "}
          <strong>{formatQty(line.required_milli)} required</strong> for this build
        </>
      )}
      {line.allocated_milli === 0 && line.kind !== "not_fitted" && " · none of it in use yet"}
    </div>
  );
}

function KindBadge({ line }: { line: LineShortageRead }) {
  switch (line.kind) {
    case "satisfied":
      return <span className="badge badge-good">satisfied</span>;
    case "short":
      return <span className="badge badge-bad">short</span>;
    case "unidentified":
      return <span className="badge badge-warn">needs identification</span>;
    case "not_fitted":
      return <span className="badge">not fitted</span>;
    default:
      return <span className="badge">{line.kind}</span>;
  }
}

/**
 * Hold stock against one short line. Lists only this part's `ACTIVE` lots —
 * `reservations.reserve` refuses any other status — and defaults the quantity
 * to exactly the outstanding shortfall rather than the whole requirement, so
 * a partial reservation against a partially-stocked line is the one-tap case.
 */
function ReserveStock({
  buildId,
  partId,
  bomLineId,
  defaultQtyMilli,
  onDone,
}: {
  buildId: number;
  partId: number;
  bomLineId: number;
  defaultQtyMilli: number;
  onDone: () => void;
}) {
  const part = useAsync<PartRead | null>(() => getPart(partId), [partId]);
  const [lotId, setLotId] = useState<number | null>(null);
  const [qtyMilli, setQtyMilli] = useState(defaultQtyMilli);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const activeLots = part.data?.lots.filter((lot) => lot.status === "active") ?? [];

  async function reserve(): Promise<void> {
    if (lotId === null || qtyMilli <= 0) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await allocateStock(buildId, {
        lot_id: lotId,
        qty_milli: qtyMilli,
        bom_line_id: bomLineId,
        part_id: partId,
        client_op_id: uuid4(),
      });
      onDone();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ marginTop: "0.4rem" }}>
      {part.data === null ? (
        <Loading what="this part's lots" />
      ) : activeLots.length === 0 ? (
        <p className="dim">No active lot of this part exists to reserve from.</p>
      ) : (
        <>
          <label className="field">
            <span>Lot</span>
            <select
              value={lotId ?? ""}
              onChange={(event) => setLotId(event.target.value === "" ? null : Number(event.target.value))}
            >
              <option value="">choose a lot…</option>
              {activeLots.map((lot) => (
                <option key={lot.id} value={lot.id}>
                  {formatQty(lot.qty_milli - lot.qty_reserved_milli)} free ·{" "}
                  {lot.location_label_path ?? `location ${lot.location_id}`}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Quantity to hold</span>
            <input
              type="number"
              min={0}
              value={qtyMilli / 1000}
              onChange={(event) => setQtyMilli(Math.max(0, Number(event.target.value) || 0) * 1000)}
            />
          </label>
          <ErrorBanner error={error} fallback="That stock could not be reserved." />
          <button
            type="button"
            className="primary wide"
            onClick={() => void reserve()}
            disabled={busy || lotId === null || qtyMilli <= 0}
          >
            {busy ? "Reserving…" : `Reserve ${formatQty(qtyMilli)}`}
          </button>
        </>
      )}
    </div>
  );
}

/**
 * Send parts out of a bin to the project, or to one of its assemblies — ADR
 * 0004's central gesture, which had no UI at all until review pointed out that
 * the routes were unreachable.
 *
 * **The destination is a location, not a flag**, and the wording has to say so:
 * these parts physically leave the drawer and the drawer's count drops in the
 * same step. A user who reads this as "mark them as mine" and then goes looking
 * in the bin is exactly the failure the ADR rejected the cheap implementation
 * for, so the notice states the move rather than the bookkeeping.
 *
 * "The project" and "assembly N" are one `<select>` rather than a checkbox plus
 * a number, because the two are genuinely one choice with N+1 answers and a
 * disabled-number-beside-a-checkbox makes an invalid pair reachable.
 */
function StageStock({
  buildId,
  assemblyCount,
  partId,
  bomLineId,
  defaultQtyMilli,
  onDone,
}: {
  buildId: number;
  assemblyCount: number;
  partId: number;
  bomLineId: number;
  defaultQtyMilli: number;
  onDone: () => void;
}) {
  const part = useAsync<PartRead | null>(() => getPart(partId), [partId]);
  const [lotId, setLotId] = useState<number | null>(null);
  const [qtyMilli, setQtyMilli] = useState(defaultQtyMilli);
  // "" is the project itself (floating parts); a number is that assembly.
  const [assemblyNo, setAssemblyNo] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const activeLots = part.data?.lots.filter((lot) => lot.status === "active") ?? [];

  async function send(): Promise<void> {
    if (lotId === null || qtyMilli <= 0) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await stageStock(buildId, {
        lot_id: lotId,
        qty_milli: qtyMilli,
        bom_line_id: bomLineId,
        assembly_no: assemblyNo === "" ? null : Number(assemblyNo),
        client_op_id: uuid4(),
      });
      onDone();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ marginTop: "0.4rem" }}>
      <Notice kind="warn" title="This moves the parts for real">
        They leave the bin and land in the project's own box, so the bin's count
        drops in the same step — that is what makes the drawer's number stay true.
        Putting them back is one tap on the roster tab.
      </Notice>
      {part.data === null ? (
        <Loading what="this part's lots" />
      ) : activeLots.length === 0 ? (
        <p className="dim">No active lot of this part exists to take from.</p>
      ) : (
        <>
          <label className="field">
            <span>Take from</span>
            <select
              value={lotId ?? ""}
              onChange={(event) =>
                setLotId(event.target.value === "" ? null : Number(event.target.value))
              }
            >
              <option value="">choose a lot…</option>
              {activeLots.map((lot) => (
                <option key={lot.id} value={lot.id}>
                  {formatQty(lot.qty_milli)} on hand ·{" "}
                  {lot.location_label_path ?? `location ${lot.location_id}`}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Send to</span>
            <select value={assemblyNo} onChange={(event) => setAssemblyNo(event.target.value)}>
              <option value="">the project (not committed to a unit yet)</option>
              {Array.from({ length: assemblyCount }, (_unused, index) => index + 1).map((no) => (
                <option key={no} value={String(no)}>
                  assembly {no}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>How many</span>
            <input
              type="number"
              min={0}
              value={qtyMilli / 1000}
              onChange={(event) => setQtyMilli(Math.max(0, Number(event.target.value) || 0) * 1000)}
            />
          </label>
          <ErrorBanner error={error} fallback="Those parts were not sent to the project." />
          <button
            type="button"
            className="primary wide"
            onClick={() => void send()}
            disabled={busy || lotId === null || qtyMilli <= 0}
          >
            {busy ? "Sending…" : `Send ${formatQty(qtyMilli)}`}
          </button>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------- pick list --

/**
 * The walk: where to go, and how many to take while standing there.
 *
 * **Rendered in server order and never re-sorted.** The stops arrive sorted by
 * `locations.id_path`, which groups a cabinet's drawers together so the room is
 * crossed once. Sorting them here by BOM line — the ordering a reader of the
 * BOM would reach for — is exactly the failure the endpoint exists to avoid, so
 * this component contains no comparator at all.
 */
function PickListView({ build }: { build: BuildRead }) {
  const plan = useAsync<PickListResponse>(() => getPickList(build.id), [build.id]);

  if (plan.error !== null) {
    return <ErrorBanner error={plan.error} fallback="The pick list could not be loaded." />;
  }
  if (plan.data === null) {
    return <Loading what="the pick list" />;
  }
  const { stops, gaps } = plan.data;

  return (
    <div className="stack">
      {/* Three outcomes, not two. "Nothing to fetch" and "nothing left to fetch
          but some lines cannot be fetched at all" are opposite situations, and a
          single is_complete-shaped message would collapse them. */}
      {gaps.length > 0 ? (
        <Notice kind="warn" title={`${gaps.length} line(s) cannot be fully picked`}>
          Everything below is real and worth walking for, but the lines listed
          under it are not covered. Walking this list will not finish the build.
        </Notice>
      ) : stops.length === 0 ? (
        <Notice kind="ok" title="Nothing to fetch">
          Every line is already held, staged or built in. There is no walk to take.
        </Notice>
      ) : (
        <Notice kind="ok" title={`${stops.length} stop(s), ${formatQty(plan.data.qty_milli)} in total`}>
          In walking order — each cabinet's drawers are consecutive, so the room is
          crossed once rather than once per BOM line.
        </Notice>
      )}

      {stops.length > 0 && (
        <ol className="list">
          {stops.map((stop, index) => (
            <PickStop key={stop.location_id} stop={stop} index={index + 1} />
          ))}
        </ol>
      )}

      {gaps.length > 0 && (
        <div className="stack">
          <h2>Cannot be picked</h2>
          <ul className="list">
            {gaps.map((gap) => (
              <PickGap key={gap.bom_line_id} projectId={build.project_id} gap={gap} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function PickStop({ stop, index }: { stop: PickStopRead; index: number }) {
  return (
    <li>
      <div className="list-item">
        <div className="row">
          <span className="title" style={{ flex: 1 }}>
            {index}. <Link to={`/locations/${stop.location_id}`}>{stop.label_path}</Link>
          </span>
          <span className="big-number">{formatQty(stop.qty_milli)}</span>
        </div>
        {/* The printed code, when the bin has one, so the stop can be scanned
            rather than matched by eye. Most generated grid cells carry no label
            at all, so its absence is normal and never an error. */}
        {stop.short_id !== null && <div className="sub mono">{stop.short_id}</div>}
        <ul className="list" style={{ marginTop: "0.4rem" }}>
          {stop.takes.map((take) => (
            <li key={`${take.lot_id}-${take.bom_line_id ?? "none"}-${take.part_id}`} className="sub">
              <strong>{formatQty(take.qty_milli)}</strong> of{" "}
              <Link to={`/parts/${take.part_id}`}>{take.part_name}</Link>
              {take.part_mpn !== null && ` (${take.part_mpn})`} from{" "}
              <Link to={`/lots/${take.lot_id}`}>lot #{take.lot_id}</Link>
              {take.line_no !== null && ` · line ${take.line_no}`}
              {take.designators !== null && ` · ${take.designators}`}{" "}
              {/* Two badges that change what the hand does, so both are words and
                  glyphs rather than a colour: an emptied lot is carried away whole
                  with no count to get wrong, and a substitute is a decision
                  somebody made once that is now being acted on blind at a drawer. */}
              {take.whole_lot && <span className="badge badge-good">whole lot</span>}
              {take.is_substitute && <span className="badge badge-info">substitute</span>}
              {take.allocation_id !== null && <span className="badge">already held</span>}
            </li>
          ))}
        </ul>
      </div>
    </li>
  );
}

/**
 * A line the walk cannot finish. **Listed, never omitted** — a pick list that
 * quietly dropped its own gaps reads as complete, and the user finds out at the
 * bench.
 *
 * `UNIDENTIFIED` and `SHORT` get different words and different next actions for
 * the same reason they do on the shortage tab: one is fixed by a human saying
 * what the part is, the other by ordering more. An unmatched line has nothing to
 * go and look for at all, which is a fact worth stating rather than a zero.
 */
function PickGap({ projectId, gap }: { projectId: number; gap: PickGapRead }) {
  const unidentified = gap.kind === "unidentified";
  return (
    <li>
      <div className="list-item">
        <div className="row">
          <span className="title" style={{ flex: 1 }}>
            Line {gap.line_no}
          </span>
          <span className={`badge ${unidentified ? "badge-warn" : "badge-bad"}`}>
            {unidentified ? "nothing to look for" : "not enough in stock"}
          </span>
        </div>
        {unidentified ? (
          <div className="sub">
            No part is matched to this line, so there is no bin to walk to.{" "}
            <Link to={`/projects/${projectId}/bom?unmatched=1`}>Match it in the BOM →</Link>
          </div>
        ) : (
          <div className="sub">
            {formatQty(gap.needed_milli)} needed · {formatQty(gap.pickable_milli)} pickable ·{" "}
            {formatQty(gap.shortfall_milli)} not in stock anywhere
          </div>
        )}
        {gap.part_id !== null && (
          <div className="sub">
            <Link to={`/parts/${gap.part_id}`}>part #{gap.part_id}</Link>
          </div>
        )}
        {/* Only meaningful alongside a non-zero pickable quantity: this is the
            line the walk half-covers, which is the case a pick list lies about
            most easily by listing its takes and saying nothing about the rest. */}
        {!unidentified && gap.pickable_milli > 0 && (
          <div className="sub">
            Some of this line is on the walk above — do not read those stops as
            finishing it.
          </div>
        )}
      </div>
    </li>
  );
}

// ---------------------------------------------------------------- roster --

/**
 * What actually went into this build — the one view that admits it may be
 * incomplete, and offers the fix.
 *
 * Four quantities per line, never added together: required, reserved, staged and
 * consumed. A line with 30 required and 30 *reserved* has not had a single part
 * fitted and the stock is still in a drawer; one with 30 consumed is finished.
 * Same numbers, opposite situations, so a single "done" bar would be a lie.
 */
function RosterView({ build, onRecorded }: { build: BuildRead; onRecorded: () => void }) {
  const report = useAsync<RosterResponse>(() => getRoster(build.id), [build.id]);
  const [recording, setRecording] = useState<number | null | "none">("none");

  if (report.error !== null) {
    return <ErrorBanner error={report.error} fallback="The roster could not be loaded." />;
  }
  if (report.data === null) {
    return <Loading what="the roster" />;
  }
  // Bound to a local so the null check above narrows inside the JSX below:
  // `report.data` is a property read that TypeScript re-widens at every use.
  const data = report.data;
  const planned = data.lines.filter((line) => !line.is_off_bom);
  const offBom = data.lines.filter((line) => line.is_off_bom);

  function recorded(): void {
    setRecording("none");
    report.reload();
    onRecorded();
  }

  return (
    <div className="stack">
      {/* Stated at the top because "this roster has been edited" changes how every
          number under it should be read — and because a reader who scrolls past it
          would otherwise take a reconstruction for a measurement. */}
      {data.after_the_fact_milli > 0 && (
        <Notice kind="warn" title="Part of this roster was entered after the fact">
          {formatQty(data.after_the_fact_milli)} of what is recorded as consumed
          was reconstructed rather than captured as it happened. Every such row
          says so individually below — this is the total, not the detail.
        </Notice>
      )}
      {offBom.length > 0 && (
        <Notice kind="info" title={`${offBom.length} part(s) used that the BOM does not list`}>
          Not an error — on a board that is still changing this is the usual signal
          that the BOM is behind the hardware. They are listed at the bottom.
        </Notice>
      )}

      <ul className="list">
        {planned.map((line) => (
          <RosterLine
            key={line.bom_line_id}
            buildId={build.id}
            line={line}
            assemblyCount={data.assembly_count}
            recording={recording === line.bom_line_id}
            onRecord={() => setRecording(recording === line.bom_line_id ? "none" : line.bom_line_id)}
            onChanged={recorded}
          >
            <RecordUsed buildId={build.id} bomLineId={line.bom_line_id} onDone={recorded} />
          </RosterLine>
        ))}
      </ul>

      <div className="stack">
        <h2>Used but not on the BOM</h2>
        {offBom.length === 0 ? (
          <p className="dim">Nothing so far.</p>
        ) : (
          <ul className="list">
            {offBom.map((line) => (
              <RosterLine
                key={`off-${line.part_id}`}
                buildId={build.id}
                line={line}
                assemblyCount={data.assembly_count}
                recording={recording === line.part_id}
                onRecord={() => setRecording(recording === line.part_id ? "none" : line.part_id)}
                onChanged={recorded}
              >
                <RecordUsed buildId={build.id} bomLineId={null} onDone={recorded} />
              </RosterLine>
            ))}
          </ul>
        )}
        <div className="row">
          <button type="button" onClick={() => setRecording(recording === null ? "none" : null)}>
            {recording === null ? "Cancel" : "Record a part nobody planned for"}
          </button>
        </div>
        {recording === null && (
          <RecordUsed buildId={build.id} bomLineId={null} onDone={recorded} />
        )}
      </div>
    </div>
  );
}

function RosterLine({
  buildId,
  line,
  assemblyCount,
  recording,
  onRecord,
  onChanged,
  children,
}: {
  buildId: number;
  line: RosterLineRead;
  assemblyCount: number;
  recording: boolean;
  onRecord: () => void;
  onChanged: () => void;
  children: React.ReactNode;
}) {
  return (
    <li>
      <div className="list-item">
        <div className="row">
          <span className="title" style={{ flex: 1 }}>
            {line.is_off_bom ? (line.part_name ?? `part #${line.part_id}`) : `Line ${line.line_no}`}
          </span>
          {line.is_dnp && <span className="badge">not fitted</span>}
          {line.after_the_fact_milli > 0 && (
            <span className="badge badge-warn">edited</span>
          )}
        </div>
        {!line.is_off_bom && line.part_name !== null && (
          <div className="sub">
            <Link to={`/parts/${line.part_id}`}>{line.part_name}</Link>
            {line.part_mpn !== null && ` (${line.part_mpn})`}
            {line.designators !== null && ` · ${line.designators}`}
          </div>
        )}
        {/* Four numbers, kept apart. Merging reserved into consumed is what makes
            a build look accounted-for off parts that are still in a bin. */}
        <div className="sub">
          {line.is_off_bom
            ? "nobody planned this"
            : `${formatQty(line.required_milli)} planned (×${assemblyCount})`}{" "}
          · {formatQty(line.reserved_milli)} held · {formatQty(line.staged_milli)} set aside ·{" "}
          {formatQty(line.consumed_milli)} built in
        </div>
        {line.entries.length > 0 && (
          <ul className="list" style={{ marginTop: "0.4rem" }}>
            {line.entries.map((entry) => (
              <RosterEntry
                key={entry.allocation_id}
                buildId={buildId}
                entry={entry}
                onChanged={onChanged}
              />
            ))}
          </ul>
        )}
        <div className="row">
          <button type="button" onClick={onRecord}>
            {recording ? "Cancel" : "Record what was used"}
          </button>
        </div>
        {recording && children}
      </div>
    </li>
  );
}

/**
 * One allocation row, with its provenance stated rather than implied.
 *
 * **This is the honesty requirement.** A row somebody reconstructed and a row
 * the system witnessed both say "consumed 2" and both are true; only one of them
 * was observed. So the difference gets three channels, not a hue: a different
 * badge word ("recorded after the fact" against the plain state), the warning
 * badge's own glyph and heavier border, and a sentence saying it was entered by
 * hand. A reader who cannot see colour still gets the distinction.
 */
function RosterEntry({
  buildId,
  entry,
  onChanged,
}: {
  buildId: number;
  entry: RosterEntryRead;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<"back" | "built" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [built, setBuilt] = useState("");

  const builtMilli = Math.round((Number(built) || 0) * 1000);

  /** Both staged transitions, sharing one busy flag so neither can double-fire. */
  async function run(which: "back" | "built"): Promise<void> {
    setBusy(which);
    setError(null);
    try {
      if (which === "back") {
        await unstageStock(buildId, {
          allocation_id: entry.allocation_id,
          client_op_id: uuid4(),
        });
      } else {
        await consumeStaged(buildId, {
          allocation_id: entry.allocation_id,
          // Blank means all of it, which the server already spells `null` — a
          // half-populated board is the normal case, not the exception.
          qty_milli: builtMilli > 0 ? builtMilli : null,
          client_op_id: uuid4(),
        });
      }
      onChanged();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(null);
    }
  }

  return (
    <li className="sub">
      <strong>{formatQty(entry.qty_milli)}</strong>{" "}
      {entry.is_after_the_fact ? (
        <span className="badge badge-warn">recorded after the fact</span>
      ) : (
        <span className="badge">{STATE_WORDS[entry.state] ?? entry.state}</span>
      )}
      {entry.location_label_path !== null && (
        <>
          {" · "}
          {entry.location_id !== null ? (
            <Link to={`/locations/${entry.location_id}`}>{entry.location_label_path}</Link>
          ) : (
            entry.location_label_path
          )}
        </>
      )}
      {entry.lot_id !== null && (
        <>
          {" · "}
          <Link to={`/lots/${entry.lot_id}`}>lot #{entry.lot_id}</Link>
        </>
      )}
      {entry.note !== null && ` · ${entry.note}`}
      {entry.is_after_the_fact && (
        <div>
          Entered by hand, not captured as it happened — the stock did move, but
          nobody watched it go.
        </div>
      )}
      {/* The two transitions out of `staged`, which had no UI at all: the parts
          are on a shelf in the project's box, and the only two true things that
          can happen next are that they go back or that they go into the board.
          Both are offered here rather than on the shortage tab because this is
          the row that says where they actually are. */}
      {entry.state === "staged" && (
        <div className="stack" style={{ marginTop: "0.3rem" }}>
          <div className="row">
            <button type="button" disabled={busy !== null} onClick={() => void run("back")}>
              {busy === "back" ? "Putting back…" : "Put back on the shelf"}
            </button>
            <input
              type="number"
              min={0}
              style={{ maxWidth: "7rem" }}
              value={built}
              onChange={(event) => setBuilt(event.target.value)}
              aria-label="How many were built in"
              placeholder="all"
            />
            <button type="button" disabled={busy !== null} onClick={() => void run("built")}>
              {busy === "built" ? "Recording…" : "Built in"}
            </button>
          </div>
          <ErrorBanner error={error} fallback="That could not be recorded." />
        </div>
      )}
    </li>
  );
}

/** Plain-English states. `staged` is "set aside": ADR 0004's floating parts sit
 * in the project's own box, which is a real location and not a flag. */
const STATE_WORDS: Record<string, string> = {
  planned: "planned",
  reserved: "held in a bin",
  staged: "set aside for the project",
  consumed: "built in",
  released: "hold given back",
};

/**
 * Write down a part that really was used but never tracked.
 *
 * `bomLineId === null` is legitimate and expected rather than a fallback: the
 * part that went flying, or the fix nobody has drawn yet. Inventing a synthetic
 * BOM line for it would put a component on the BOM that the board does not have.
 *
 * The form does not offer a provenance choice, and cannot: the server forces
 * `reconciled`. A correction able to label itself a scan would destroy the only
 * property that makes the roster worth reading.
 */
function RecordUsed({
  buildId,
  bomLineId,
  onDone,
}: {
  buildId: number;
  bomLineId: number | null;
  onDone: () => void;
}) {
  const [lotId, setLotId] = useState("");
  const [qty, setQty] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const qtyMilli = Math.round((Number(qty) || 0) * 1000);
  const lot = Number(lotId);
  const lotInvalid = !(Number.isSafeInteger(lot) && lot > 0);
  const qtyInvalid = qtyMilli <= 0;
  const ready = !lotInvalid && !qtyInvalid;
  const readyMessage = lotInvalid && qtyInvalid
    ? "Enter the lot it came from and a quantity above zero before this can be recorded."
    : lotInvalid
      ? "Enter the lot it came from."
      : "Enter a quantity above zero.";

  async function submit(): Promise<void> {
    if (!ready) {
      // "Record as used" is disabled for the same reason; this guards a
      // stray Enter-key submit from going nowhere with no explanation.
      setError(new Error(readyMessage));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await recordUsed(buildId, {
        lot_id: lot,
        qty_milli: qtyMilli,
        bom_line_id: bomLineId,
        note: note === "" ? null : note,
        client_op_id: uuid4(),
      });
      onDone();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ marginTop: "0.4rem" }}>
      <Notice kind="warn" title="This is a correction, and it will say so">
        The parts leave the bin for real — the count drops in the same step, because
        a roster that got right by making a drawer wrong is the failure this whole
        system exists to prevent. The row is permanently marked as entered by hand.
      </Notice>
      <label className="field">
        <span>Lot it came out of</span>
        <input
          inputMode="numeric"
          value={lotId}
          onChange={(event) => setLotId(event.target.value)}
          placeholder="lot id"
        />
      </label>
      <label className="field">
        <span>Quantity actually used</span>
        <input
          type="number"
          min={0}
          value={qty}
          onChange={(event) => setQty(event.target.value)}
        />
      </label>
      <label className="field">
        <span>Why (optional, and worth writing)</span>
        <input value={note} onChange={(event) => setNote(event.target.value)} />
      </label>
      {!ready && <p className="muted-note">{readyMessage}</p>}
      <ErrorBanner error={error} fallback="That could not be recorded." />
      <button
        type="button"
        className="primary wide"
        onClick={() => void submit()}
        disabled={busy || !ready}
      >
        {busy ? "Recording…" : "Record as used"}
      </button>
    </div>
  );
}
