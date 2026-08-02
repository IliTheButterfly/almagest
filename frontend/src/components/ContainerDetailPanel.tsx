/**
 * The container you just pressed on the map, beside the map.
 *
 * Iliana, on the state this replaces: "you kinda get stuck when you click on open
 * this container, then you end up having a second view that is only slightly
 * different from the other view and this one does not have the path interactable.
 * It's not a good ui."
 *
 * Two pages claimed to be "the container". The tree drew its children as
 * furniture and had crumbs; the container page had a photo, capacity, a printed
 * id, its contents and an edit mode. One link joined them, in one direction, onto
 * the half that could not be navigated. So the fix is not a third page — it is to
 * stop leaving the map at all: press a cell and *this* opens next to the drawing,
 * with the map still on screen and still showing where you are.
 *
 * **What it lists is the immediate contents of the open container** — the stock
 * actually in it, not counting anything in the containers below it. That division
 * is what keeps it from repeating the map: the map draws the child *containers* as
 * furniture, and this says what is loose inside the one you opened. The roll-up
 * over everything beneath ("5 lots in here and below") stays above the map, where
 * it describes the level rather than the container.
 *
 * "Immediate" is the load-bearing word. A container showing its descendants' stock
 * as though it were its own is how somebody comes to look for a resistor in a
 * cabinet and find it is really three drawers down.
 *
 * **Deliberately not a copy of `LocationScreen`.** It hands off for everything
 * else: editing, the room plan, the printed id, emptying a bin all live on the
 * container's own page, one link away. A second editor would be the exact
 * duplication this exists to remove.
 *
 * The full page keeps existing regardless of this component, and not for
 * politeness: `/s/{short_id}` redirects a scanned tag to `/locations/{id}`, and
 * that URL is physically written into NFC tags and printed onto labels already
 * stuck to drawers.
 */

import { Link } from "react-router-dom";

import { getLocation, type LocationRead } from "../lib/api/client";
import { formatQty } from "../lib/format";
import { useAsync } from "../lib/hooks/useAsync";
import { isInbox, isProjectStagingBox } from "../lib/locations/staging";
import { formatShortId } from "../lib/shortid";
import { ContainerPhoto } from "./ContainerPhoto";
import { ErrorBanner, Loading, Notice } from "./Feedback";
import { FillMeter } from "./FillMeter";

export interface ContainerDetailPanelProps {
  readonly locationId: number;
  /**
   * How many containers are in here, per the tree the map already holds.
   *
   * Stated rather than listed: they are the cells next to this panel, and listing
   * them here would be the second representation of the same shelf that the map
   * exists to avoid.
   */
  readonly childCount?: number | undefined;
}

export function ContainerDetailPanel({
  locationId,
  childCount = 0,
}: ContainerDetailPanelProps) {
  const location = useAsync<LocationRead | null>(() => getLocation(locationId), [locationId]);

  if (location.error !== null) {
    return (
      <div className="card">
        <ErrorBanner error={location.error} fallback="That container could not be loaded." />
      </div>
    );
  }
  if (location.data === null) {
    return (
      <div className="card">
        <Loading what="the container" />
      </div>
    );
  }
  return <Detail location={location.data} childCount={childCount} />;
}

/** Exported for the test that pins the badge and the meter to one source. */
export function Detail({
  location,
  childCount,
}: {
  location: LocationRead;
  childCount: number;
}) {
  const { capacity } = location;

  return (
    <aside className="card detail-panel" aria-label={`${location.name} details`}>
      <div className="row">
        <ContainerPhoto glyph={location.effective_glyph} alt="" />
        <h2 style={{ flex: 1, margin: 0, fontSize: "1.05rem" }}>{location.name}</h2>
        {location.slot_label !== null && <span className="badge mono">{location.slot_label}</span>}
      </div>

      {/* The path is *not* repeated here: the trail above the map already says
          where this sits, and this panel sits under it. Two paths on one screen
          is how the old pair of pages came to disagree. */}
      <div className="row">
        {isInbox(location) && <span className="badge badge-accent">inbox</span>}
        {isProjectStagingBox(location) && <span className="badge badge-accent">project parts</span>}
        {/* From the live snapshot, not `location.is_overfull`.
         *
         * That column is written only by the nightly pass, so this badge and the
         * meter twelve lines below — which has always read the snapshot —
         * disagreed for up to a day: empty an over-full bin and the meter drops
         * to 20% and the "Over capacity" notice goes, while the amber badge next
         * to the name stays. Two statements about the same bin, on one screen,
         * a scroll apart. Same defect the payload itself had before #80; this is
         * the last copy of it in the UI. */}
        {capacity.is_overfull && <span className="badge badge-warn">over</span>}
        {location.effective_esd_safe === true && <span className="badge badge-good">ESD safe</span>}
        {location.is_placeable === false && <span className="badge">not a home</span>}
        {location.short_id !== null && (
          <span className="badge mono">{formatShortId(location.short_id)}</span>
        )}
      </div>

      {location.description !== null && (
        <p className="muted-note" style={{ margin: 0 }}>
          {location.description}
        </p>
      )}

      <FillMeter ratio={capacity.fill_ratio} overfull={capacity.is_overfull} />
      <p className="muted-note" style={{ margin: 0 }}>
        {capacity.used} of {capacity.capacity ?? "?"} {capacity.unit} used
      </p>
      {capacity.is_overfull && (
        <Notice kind="warn" title="Over capacity">
          Recorded, not refused. A defrag suggestion exists for it instead.
        </Notice>
      )}

      <div className="row">
        <h3 style={{ margin: 0, fontSize: "0.9rem" }}>In here</h3>
        <span className="spacer" />
        <span className="muted-note">{location.lots.length} lot(s)</span>
      </div>
      {childCount > 0 && (
        <p className="muted-note" style={{ margin: 0 }}>
          {childCount} container(s) inside, drawn on the map. Press one to open it.
        </p>
      )}
      {location.lots.length === 0 ? (
        <p className="dim" style={{ margin: 0 }}>
          {childCount > 0 ? "No stock loose in here itself." : "Empty."}
        </p>
      ) : (
        <ul className="list">
          {location.lots.map((lot) => (
            <li key={lot.id}>
              {/* Taking and returning happen on the lot's own screen, which is
                  where the idempotency key from a scan is spent. */}
              <Link className="list-item" to={`/lots/${lot.id}`}>
                <div className="row">
                  <span className="title">{formatQty(lot.qty_milli)}</span>
                  <span className="spacer" />
                  {lot.status !== "active" && <span className="badge">{lot.status}</span>}
                </div>
                <div className="sub">part {lot.part_id} · take or return →</div>
              </Link>
            </li>
          ))}
        </ul>
      )}

      <div className="row">
        <span className="spacer" />
        {/* The one link off the map, and it is named for what is there rather
            than "open this container": everything this panel does not do —
            editing, the layout, the room plan, the printed id, emptying it — is
            on that page. */}
        <Link to={`/locations/${location.id}`}>Edit or empty this container →</Link>
      </div>
    </aside>
  );
}
