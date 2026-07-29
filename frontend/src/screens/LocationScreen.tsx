/**
 * `/locations/:id` — **the** page for one container. Using it, and editing it.
 *
 * This is where a tapped drawer tag lands, so it answers what is in here first,
 * and it is the most-visited screen in the system. It is now also the only place a
 * container is edited: the edit-mode toggle in the header turns the same page into
 * its own editor, and what used to be `/locations/:id/layout` and
 * `/containers/new?parent=:id` are panels over it (`components/ContainerEditMode`).
 * Iliana asked for exactly that shape — "a page and an edit mode... you can
 * customize the UI (storage) or use it normally" — and the two halves are kept
 * visually apart rather than blended, because putting a part away and rearranging
 * the furniture are different intentions and a mis-tap between them is expensive.
 *
 * Normal mode is for *using* storage: what is in here, how full it is, what is
 * inside, take it or put it back, empty this bin into that one.
 *
 * The path shown at the top is **derived**, never read off the tag. The tag carries
 * only the opaque short ID: a drawer moves between cabinets, and any hierarchy
 * encoded into a printed or written payload becomes a lie the moment it does.
 *
 * Capacity is advisory. An overfull bin says so and suggests a defrag; it never
 * blocks anything, here or anywhere else.
 */

import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ContainerLayout } from "../components/ContainerLayout";
import { ContainerEditMode, EditModeToggle, useEditMode } from "../components/ContainerEditMode";
import { ContainerPhoto } from "../components/ContainerPhoto";
import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import { FillMeter } from "../components/FillMeter";
import {
  assignLocationShortId,
  emptyBin,
  getLocation,
  getLocationPlan,
  getLocationTree,
  resolveShortId,
  type LocationRead,
  type LocationTree,
  type RoomPlanRead,
} from "../lib/api/client";
import { formatFillRatio, formatQty } from "../lib/format";
import { isInbox, isProjectStagingBox } from "../lib/locations/staging";
import { indexTree } from "../lib/locations/tree";
import { FLOOR_PLAN, known } from "../lib/locations/views";
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
  const { editing, setEditing } = useEditMode();
  const children = useAsync<LocationTree | null>(
    () => (location.child_count > 0 ? getLocationTree(location.id) : Promise.resolve(null)),
    [location.id, location.child_count],
  );
  /**
   * The drawn room, when this level is drawn as one — ADR 0009.
   *
   * Fetched on the strength of **this level's own** `effective_child_view`, which is
   * the single question ADR 0006 says decides the picture, asked here exactly as the
   * renderer asks it. Not on depth, and not on "is this a room": a shelf two levels
   * down that somebody chose to draw a plan of gets its plan through this same call.
   * A level drawn any other way never pays for the request.
   */
  const plan = useAsync<RoomPlanRead | null>(
    () =>
      known(location.effective_child_view) === FLOOR_PLAN
        ? getLocationPlan(location.id)
        : Promise.resolve(null),
    [location.id, location.effective_child_view],
  );

  return (
    <div className="stack">
      <div className={editing ? "card editing" : "card"}>
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
          <span className="spacer" />
          {/* The same toggle at every depth — a drawer inside a cabinet inside a
              room is edited by the identical component, which is what keeps this
              page honest about the tree having no named levels. */}
          <EditModeToggle editing={editing} onChange={setEditing} />
        </div>
      </div>

      {editing && <ContainerEditMode location={location} onChanged={onChanged} />}

      <Picture location={location} />

      <Capacity location={location} />

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

      <Inside location={location} tree={children.data} plan={plan.data} />
    </div>
  );
}

/**
 * What is inside, drawn as the thing it is.
 *
 * The same `ContainerLayout` the map uses, asked about **this** container's id —
 * so a cabinet's page shows drawer fronts and a room's page shows a floor plan,
 * for the same reason and through the same call (ADR 0006). Nothing here knows
 * which of those it is holding.
 */
function Inside({
  location,
  tree,
  plan,
}: {
  location: LocationRead;
  tree: LocationTree | null;
  plan: RoomPlanRead | null;
}) {
  const index = useMemo(() => indexTree(tree?.nodes ?? []), [tree]);
  // "Still loading" and "there is nothing to load" are different, and only the
  // first may show a spinner: a drawn but empty room has no tree to wait for.
  const loading = location.child_count > 0 && tree === null;
  // A room with walls drawn and nothing in it yet is still worth drawing — it is
  // what somebody was in the middle of doing. So "empty" here means empty of
  // containers *and* undrawn, which is a question about the data and not about
  // which level this is.
  const drawn = plan !== null && plan.shapes.length > 0;

  if (location.child_count === 0 && !drawn) {
    return (
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Inside</h3>
        <p className="dim">
          Nothing is laid out in here yet. Edit this container to add drawers, trays or bins — or to
          stamp a set of them out of a container type.
        </p>
      </div>
    );
  }

  return (
    <div className="card">
      <div className="row">
        <h3 style={{ margin: 0 }}>Inside ({location.child_count})</h3>
        <span className="spacer" />
        <Link to={`/tree?at=${location.id}`}>See it in the whole tree →</Link>
      </div>
      {loading ? (
        <Loading what="what is inside" />
      ) : (
        <ContainerLayout
          index={index}
          parentId={location.id}
          drillTo={(node) => `/locations/${node.id}`}
          plan={plan}
        />
      )}
    </div>
  );
}

/**
 * "What does this container look like" — the real photo (its own, falling back to
 * the container type's) and the fill state beside it. Read-only here: changing it
 * is an edit, and every edit lives in edit mode.
 */
function Picture({ location }: { location: LocationRead }) {
  return (
    <div className="card">
      <div className="row">
        <ContainerPhoto
          photo={location.effective_photo}
          glyph={location.effective_glyph}
          alt=""
          size="card"
        />
        {location.photo === null && location.effective_photo !== null && (
          <p className="muted-note" style={{ flex: 1, margin: 0 }}>
            This picture comes from the container type. Edit this container to give it one of its
            own.
          </p>
        )}
      </div>
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
 * Deliberately **not** behind edit mode: this is what makes a drawer scannable,
 * which is a using-storage act — you assign one because you are standing at the
 * printer, not because you are rearranging the furniture.
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

/**
 * "Empty this bin into that one" — workflow 4, from the bin's own screen.
 *
 * One lot failing validation commits the rest and reports just that failure, which
 * is what the endpoint does and what this renders. The destination is given by short
 * ID, because that is what is printed on the other drawer.
 */
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
