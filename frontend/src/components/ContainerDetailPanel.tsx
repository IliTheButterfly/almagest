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
 * **Deliberately not a copy of `LocationScreen`.** It answers "what is this and
 * what is in it", and hands off for everything else: editing, the room plan, the
 * printed id, emptying a bin all live on the container's own page, one link away.
 * A second editor would be the exact duplication this exists to remove — and the
 * one-line rule for what belongs here is: things you want *while comparing
 * containers*, which is what a map is for.
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
  /** Drills the map into this container, without leaving the page. */
  readonly onLookInside?: ((locationId: number) => void) | undefined;
  /** How many containers the map knows are in here. */
  readonly childCount?: number | undefined;
}

export function ContainerDetailPanel({
  locationId,
  onLookInside,
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
  return <Detail location={location.data} onLookInside={onLookInside} childCount={childCount} />;
}

function Detail({
  location,
  onLookInside,
  childCount,
}: {
  location: LocationRead;
  onLookInside: ((locationId: number) => void) | undefined;
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
        {location.is_overfull && <span className="badge badge-warn">over</span>}
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
        <h3 style={{ margin: 0, fontSize: "0.9rem" }}>Contents</h3>
        <span className="spacer" />
        <span className="muted-note">{location.lots.length} lot(s)</span>
      </div>
      {location.lots.length === 0 ? (
        <p className="dim" style={{ margin: 0 }}>
          Empty.
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
        {childCount > 0 && onLookInside !== undefined && (
          <button type="button" onClick={() => onLookInside(location.id)}>
            Look inside ({childCount})
          </button>
        )}
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
