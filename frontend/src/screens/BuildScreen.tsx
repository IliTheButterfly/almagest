/**
 * One build: its own status, and the shortage report that says whether it can
 * actually be built.
 *
 * **The load-bearing distinction**: `ShortageKind.SHORT` and
 * `ShortageKind.UNIDENTIFIED` are not the same failure and must not read the
 * same. A short line has a known part and a number — it is fixed by ordering
 * more. An unidentified line (`bom_lines.part_id IS NULL`) has no part to
 * check availability against at all, so `available_milli`/`shortfall_milli`
 * come back `null` rather than zero; rendering that as "0 short" would be the
 * false-green this whole report exists to prevent. So SHORT is red with a
 * number, UNIDENTIFIED is amber with "needs identification" and a link to the
 * BOM screen — different hue, different glyph, different words, different
 * next action.
 */

import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import {
  allocateStock,
  getBuild,
  getPart,
  getShortages,
  releaseStock,
  updateBuild,
  type BuildRead,
  type BuildStatus,
  type BuildUpdate,
  type LineShortageRead,
  type PartRead,
  type ShortageResponse,
} from "../lib/api/client";
import { formatQty } from "../lib/format";
import { useAsync } from "../lib/hooks/useAsync";
import { uuid4 } from "../lib/scan/session";

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
  const shortages = useAsync<ShortageResponse>(() => getShortages(build.id), [build.id]);

  const closed = build.status === "completed" || build.status === "abandoned";

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <Link to={`/projects/${build.project_id}`}>← project</Link>
        </div>
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
          <button type="button" onClick={() => setEditing(!editing)}>
            {editing ? "Cancel" : "Edit"}
          </button>
          {!closed && (
            <button
              type="button"
              className="danger"
              onClick={() =>
                void releaseStock(build.id, { client_op_id: uuid4() }).then(() => {
                  shortages.reload();
                  onChanged();
                })
              }
            >
              Release all holds
            </button>
          )}
        </div>
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
          below still shows what the build needed, for the record.
        </Notice>
      )}

      <ErrorBanner error={shortages.error} fallback="The shortage report could not be loaded." />
      {shortages.data === null ? (
        <Loading what="the shortage report" />
      ) : (
        <ShortageReport build={build} report={shortages.data} onChanged={shortages.reload} />
      )}
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
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const closesBuild =
    draft.status !== undefined &&
    draft.status !== null &&
    draft.status !== build.status &&
    (draft.status === "completed" || draft.status === "abandoned");

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
        <span>Notes</span>
        <textarea
          rows={3}
          value={draft.notes ?? ""}
          onChange={(event) => setDraft({ ...draft, notes: event.target.value })}
        />
      </label>
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
  line,
  onChanged,
}: {
  buildId: number;
  projectId: number;
  line: LineShortageRead;
  onChanged: () => void;
}) {
  const [reserving, setReserving] = useState(false);

  return (
    <li>
      <div className="list-item">
        <div className="row">
          <span className="title" style={{ flex: 1 }}>
            Line {line.line_no}
          </span>
          <KindBadge line={line} />
        </div>
        <div className="sub">
          {formatQty(line.required_milli)} required · {formatQty(line.allocated_milli)} already
          held
          {line.available_milli !== null && ` · ${formatQty(line.available_milli)} free`}
          {line.shortfall_milli !== null &&
            line.shortfall_milli > 0 &&
            ` · short ${formatQty(line.shortfall_milli)}`}
        </div>
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

        {line.kind === "short" && line.part_id !== null && (
          <div className="row">
            <button type="button" onClick={() => setReserving(!reserving)}>
              {reserving ? "Cancel" : "Reserve stock"}
            </button>
          </div>
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
