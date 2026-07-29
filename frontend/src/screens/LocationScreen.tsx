/**
 * Bin contents — what is physically in this container, and its children.
 *
 * `/locations/:id` is where a tapped drawer tag lands, so it is the most-visited
 * screen in the system and it answers exactly one question first: what is in here.
 *
 * The path shown at the top is **derived**, never read off the tag. The tag carries
 * only the opaque short ID: a drawer moves between cabinets, and any hierarchy
 * encoded into a printed or written payload becomes a lie the moment it does.
 *
 * Capacity is advisory. An overfull bin says so and suggests a defrag; it never
 * blocks anything, here or anywhere else.
 */

import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import { FillMeter } from "../components/FillMeter";
import {
  assignLocationShortId,
  emptyBin,
  getLocation,
  getLocationTree,
  resolveShortId,
  type LocationRead,
  type LocationTree,
} from "../lib/api/client";
import { formatFillRatio, formatQty } from "../lib/format";
import { isInbox, isProjectStagingBox } from "../lib/locations/staging";
import { useAsync } from "../lib/hooks/useAsync";
import { uuid4 } from "../lib/scan/session";
import { formatShortId, looksLikeShortId, normalizeShortId } from "../lib/shortid";

export function LocationScreen() {
  const { locationId: raw } = useParams();
  const locationId = Number(raw);
  const valid = Number.isSafeInteger(locationId) && locationId > 0;

  const location = useAsync<LocationRead | null>(
    () => (valid ? getLocation(locationId) : Promise.resolve(null)),
    [locationId, valid],
  );

  if (!valid) {
    return <Notice kind="error" title="That is not a location id" />;
  }
  if (location.error !== null) {
    return <ErrorBanner error={location.error} fallback="That container could not be loaded." />;
  }
  if (location.data === null) {
    return <Loading what="the container" />;
  }
  return <Bin location={location.data} onChanged={location.reload} />;
}

function Bin({ location, onChanged }: { location: LocationRead; onChanged: () => void }) {
  const children = useAsync<LocationTree | null>(
    () => (location.child_count > 0 ? getLocationTree(location.id) : Promise.resolve(null)),
    [location.id, location.child_count],
  );

  return (
    <div className="stack">
      <div className="card">
        <p className="muted-note" style={{ margin: 0 }}>
          {location.label_path}
        </p>
        <div className="row">
          <h1 style={{ flex: 1 }}>{location.name}</h1>
          {location.slot_label !== null && (
            <span className="badge mono">{location.slot_label}</span>
          )}
          {location.short_id !== null && (
            <span className="badge mono">{formatShortId(location.short_id)}</span>
          )}
        </div>
        {location.description !== null && <p style={{ margin: 0 }}>{location.description}</p>}
        <div className="row">
          {/* Two staging kinds, two words. A project box used to render the bare
              "inbox" badge, which tells a reader the opposite of the truth: the
              INBOX is a catch-all to empty, a project box is where a board's parts
              are meant to sit until it is built. See `lib/locations/staging`. */}
          {isInbox(location) && <span className="badge badge-accent">inbox</span>}
          {isProjectStagingBox(location) && (
            <span className="badge badge-accent">project parts</span>
          )}
          {location.is_overfull && <span className="badge badge-warn">over</span>}
          {location.effective_esd_safe === true && <span className="badge badge-good">ESD safe</span>}
          {location.is_placeable === false && <span className="badge">not placeable</span>}
          {location.parent_id !== null && (
            <Link to={`/locations/${location.parent_id}`}>Up one level</Link>
          )}
        </div>
      </div>

      <Capacity location={location} />

      <div className="card">
        <div className="row">
          <p className="muted-note" style={{ flex: 1, margin: 0 }}>
            {location.child_count === 0
              ? "No slots laid out here yet."
              : `${location.child_count} slot(s) laid out here.`}{" "}
            Merging, splitting and relabelling go through the change guard: a slot that
            still holds stock or a bound tag blocks the change rather than losing it.
          </p>
          <Link to={`/locations/${location.id}/layout`}>Edit layout →</Link>
        </div>
      </div>

      <PrintedId location={location} onDone={onChanged} />

      <div className="card">
        <div className="row">
          <h3 style={{ margin: 0 }}>Contents</h3>
          <span className="spacer" />
          <span className="muted-note">{location.lots.length} lot(s)</span>
        </div>
        {location.lots.length === 0 ? (
          <p className="dim">Empty.</p>
        ) : (
          <ul className="list">
            {location.lots.map((lot) => (
              <li key={lot.id}>
                <Link className="list-item" to={`/lots/${lot.id}`}>
                  <div className="row">
                    <span className="title">{formatQty(lot.qty_milli)}</span>
                    <span className="spacer" />
                    {lot.status !== "active" && <span className="badge">{lot.status}</span>}
                  </div>
                  <div className="sub">
                    part {lot.part_id}
                    {lot.batch_code === null || lot.batch_code === undefined
                      ? ""
                      : ` · batch ${lot.batch_code}`}
                  </div>
                  <div className="sub">Take or return →</div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>

      {location.lots.length > 0 && <EmptyInto location={location} onDone={onChanged} />}

      {location.child_count > 0 && (
        <div className="card">
          <div className="row">
            <h3 style={{ margin: 0 }}>Inside ({location.child_count})</h3>
            <span className="spacer" />
            {/* The spatial view of the same children, laid out from their slot
                labels rather than listed. */}
            <Link to={`/tree?at=${location.id}`}>See the layout →</Link>
          </div>
          {children.data === null ? (
            <Loading what="the children" />
          ) : (
            <ul className="list">
              {children.data.nodes
                .filter((node) => node.parent_id === location.id)
                .map((node) => (
                  <li key={node.id}>
                    <Link className="list-item" to={`/locations/${node.id}`}>
                      <div className="row">
                        <span className="title">{node.name}</span>
                        <span className="spacer" />
                        {node.slot_label !== null && (
                          <span className="badge mono">{node.slot_label}</span>
                        )}
                        {node.is_overfull && <span className="badge badge-warn">over</span>}
                      </div>
                      <div className="sub">
                        {node.lot_count} lot(s) · {formatQty(node.qty_milli)} ·{" "}
                        {/* Null fill is "no capacity model", which is not the same
                            claim as "empty" and must not read like it. */}
                        {node.fill_ratio === null
                          ? "fill not measured"
                          : `${formatFillRatio(node.fill_ratio)} full`}
                      </div>
                    </Link>
                  </li>
                ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function Capacity({ location }: { location: LocationRead }) {
  const { capacity } = location;
  return (
    <div className="card">
      <div className="row">
        <h3 style={{ margin: 0 }}>Capacity</h3>
        <span className="spacer" />
        <span className="muted-note mono">{capacity.model}</span>
      </div>
      <FillMeter ratio={capacity.fill_ratio} overfull={capacity.is_overfull} />
      <p className="muted-note" style={{ margin: 0 }}>
        {capacity.used} of {capacity.capacity ?? "?"} {capacity.unit} used
        {capacity.fill_ratio === null ? "" : ` · ${formatFillRatio(capacity.fill_ratio)}`}
      </p>
      {capacity.is_overfull && (
        <Notice kind="warn" title="Over capacity">
          Recorded, not refused. The put-away that did this was accepted on purpose —
          a rejected scan teaches the user to stop scanning — and a defrag suggestion
          exists for it instead.
        </Notice>
      )}
    </div>
  );
}

/**
 * "Empty this bin into that one" — workflow 4, from the bin's own screen.
 *
 * One lot failing validation commits the rest and reports just that failure, which
 * is what the endpoint does and what this renders. The destination is given by short
 * ID, because that is what is printed on the other drawer.
 */
/**
 * The container's printed identity: mint one, or adopt one already printed.
 *
 * A generated grid cell starts with no printed id — nobody sticks 96 labels on an
 * 8×12 box — so this is where one is earned. Two paths, because they differ only
 * in who chose the code:
 *
 * - **Assign one** mints it. Safe to press twice: the server returns the existing
 *   id rather than a second one, so this needs no "does it already have one?"
 *   check and cannot mint a spare by double-tap.
 * - **I already have a label** adopts the code you type or scan, for pre-printed
 *   label stock and pre-encoded tags. The server verifies the check symbol and
 *   refuses a code held elsewhere instead of substituting a free one — a
 *   substitute would leave the label and the database permanently disagreeing,
 *   which is the failure the whole scheme exists to prevent.
 *
 * Relabelling is offered even when there is already an id, because it is
 * non-destructive: the old code stays resolvable, so the label still stuck to the
 * drawer and the one in your hand both keep working.
 *
 * The 409 is worth its own branch. `held_by` names the drawer that holds the code
 * — "already bound to Cabinet A / Drawer B2" tells you which drawer to walk to,
 * where "already bound to location 41" makes you go and look it up.
 */
function PrintedId({ location, onDone }: { location: LocationRead; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<string | null>(null);

  async function run(adopt: boolean): Promise<void> {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const response = await assignLocationShortId(location.id, {
        short_id: adopt ? normalizeShortId(code) : null,
        client_op_id: uuid4(),
      });
      setResult(
        response.adopted
          ? `Adopted ${formatShortId(response.short_id)}.` +
              (response.previous_short_id === null
                ? ""
                : ` ${formatShortId(response.previous_short_id)} still resolves here, so the old label keeps working.`)
          : `Printed id is ${formatShortId(response.short_id)}.`,
      );
      setCode("");
      setOpen(false);
      onDone();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <div className="row">
        <h3 style={{ margin: 0 }}>Printed id</h3>
        <span className="spacer" />
        {location.short_id === null ? (
          <span className="muted-note">none yet</span>
        ) : (
          <span className="badge mono">{formatShortId(location.short_id)}</span>
        )}
      </div>

      <p className="muted-note">
        {location.short_id === null
          ? "Generated cells have no printed id until one is needed. Assign one to print a card or write a tag."
          : "This is what is on the card and the tag. A new one can be adopted without breaking the old label."}
      </p>

      <ErrorBanner error={error} fallback="The printed id was not changed." />
      {result !== null && <Notice kind="ok">{result}</Notice>}

      {open ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void run(true);
          }}
        >
          <label className="field">
            <span>The code already on the label or tag</span>
            <input
              className="mono"
              value={code}
              onChange={(event) => setCode(event.target.value)}
              placeholder="4K7T-92M8"
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
            />
          </label>
          <div className="row">
            <button type="button" onClick={() => setOpen(false)}>
              Cancel
            </button>
            <span className="spacer" />
            <button type="submit" className="primary" disabled={busy || !looksLikeShortId(code)}>
              {busy ? "Adopting…" : "Adopt this code"}
            </button>
          </div>
        </form>
      ) : (
        <div className="row">
          {location.short_id === null && (
            <button type="button" className="primary" disabled={busy} onClick={() => void run(false)}>
              {busy ? "Assigning…" : "Assign one"}
            </button>
          )}
          <span className="spacer" />
          <button type="button" onClick={() => setOpen(true)}>
            I already have a label…
          </button>
        </div>
      )}
    </div>
  );
}

function EmptyInto({ location, onDone }: { location: LocationRead; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<string | null>(null);

  async function run(): Promise<void> {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const resolved = await resolveShortId(normalizeShortId(code));
      const target = resolved.target;
      if (target === null || target === undefined || target.entity_type !== "location") {
        setError(new Error("That code does not name a container."));
        return;
      }
      if (target.entity_pk === location.id) {
        setError(new Error("Source and destination are the same container."));
        return;
      }
      const response = await emptyBin(location.id, {
        to_location_id: target.entity_pk,
        client_op_id: uuid4(),
        source: "scan",
      });
      const moved = response.moved_lot_ids.length;
      const failed = response.failures.length;
      setResult(
        `Moved ${moved} lot(s) to ${target.label_path ?? target.label}` +
          (failed === 0
            ? "."
            : `; ${failed} could not move: ${response.failures
                .map((failure) => `lot ${failure.lot_id} (${failure.reason})`)
                .join(", ")}.`),
      );
      setCode("");
      onDone();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <div className="card">
        <button type="button" className="wide" onClick={() => setOpen(true)}>
          Empty this bin into another…
        </button>
      </div>
    );
  }

  return (
    <form
      className="card"
      onSubmit={(event) => {
        event.preventDefault();
        void run();
      }}
    >
      <h3>Empty into</h3>
      <label className="field">
        <span>Destination short ID</span>
        <input
          className="mono"
          value={code}
          onChange={(event) => setCode(event.target.value)}
          placeholder="4K7T-92M8"
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
        />
      </label>
      <ErrorBanner error={error} fallback="Nothing was moved." />
      {result !== null && <Notice kind="ok">{result}</Notice>}
      <div className="row">
        <button type="button" onClick={() => setOpen(false)}>
          Cancel
        </button>
        <span className="spacer" />
        <button
          type="submit"
          className="primary"
          disabled={busy || !looksLikeShortId(code)}
        >
          {busy ? "Moving…" : `Move ${location.lots.length} lot(s)`}
        </button>
      </div>
    </form>
  );
}
