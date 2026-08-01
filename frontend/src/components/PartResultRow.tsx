/**
 * One part, as it reads in a list of search results.
 *
 * Extracted because two screens show the same thing and had drifted: the search
 * screen printed the stock badge and the BOM's match picker did not, so the same
 * part looked like two different records depending on where you met it — and
 * "what do I have" is precisely the question the badge answers (ADR 0007). One
 * component, so there is one answer.
 *
 * Deliberately renders no wrapper and no action: the caller owns the `<li>`, the
 * link and whatever button sits beside it, because those differ (a whole-row link
 * on the search screen, a "use this" button in the picker, "add to cart" in the
 * cart's shopping view) while the identity of the part does not.
 */

import type { PartSummary } from "../lib/api/client";
import { formatQty } from "../lib/format";

/**
 * How much of this part you have, and how it is packaged.
 *
 * The list is ordered stock-first, so without this the ordering looks arbitrary
 * — the quantity is the one number that explains the sort. `0` is stated rather
 * than left blank: "you have none of this" is most of what a personal inventory
 * is asked, and a blank cell reads as "unknown" instead.
 *
 * Lots and bins are shown only when there is more than one, because "500 in 2
 * lots across 2 bins" is a genuinely different physical situation from "500 on a
 * reel" — and repeating "1 lot, 1 bin" on every row would bury the cases where
 * it matters.
 */
export function StockLine({ part }: { part: PartSummary }) {
  if (part.qty_milli === 0) {
    return <span className="badge">none in stock</span>;
  }

  const detail = [
    part.lot_count > 1 ? `${part.lot_count} lots` : null,
    part.location_count > 1 ? `${part.location_count} bins` : null,
  ].filter((piece) => piece !== null);

  return (
    <span className="badge badge-good">
      {formatQty(part.qty_milli)} in stock
      {detail.length > 0 && ` · ${detail.join(", ")}`}
    </span>
  );
}

/**
 * Which containers, not merely how many.
 *
 * `StockLine` above could always say "in 2 bins" and never which two, so the
 * only way to find out where anything was involved opening the part and reading
 * its lots. That is the wrong shape for the question a search is usually asked —
 * "do I have one of these, and can I go and get it" — and it is the same gap the
 * part screen had before `components/WhereIsIt`.
 *
 * Text, not a drawing. A result list is dozens of rows and a map apiece would be
 * unreadable and slow; naming the drawer is enough to *recognise* it, and the
 * screen that wants the full walk hangs it off these ids. The server caps the
 * list at three and `location_count` still carries the true total, so the row
 * says "and 2 more" rather than quietly implying three is all there is.
 */
export function PlaceLine({ part }: { part: PartSummary }) {
  const places = part.locations ?? [];
  if (places.length === 0) {
    return null;
  }
  const hidden = part.location_count - places.length;

  return (
    <div className="sub place-line">
      {places.map((place) => (
        <span key={place.location_id} className="place-chip">
          {place.label_path}
          {/* The share, but only where the split is the point: one container
              holding all of it is already stated by the stock badge above, and
              repeating it per row is noise.

              Separated by a middot and not a space: a path already ends in a
              slot label, so "… / C2 900" reads as a container *called* "C2 900"
              rather than as nine hundred of them in C2. */}
          {places.length > 1 && (
            <span className="dim"> &middot; {formatQty(place.qty_milli)}</span>
          )}
        </span>
      ))}
      {hidden > 0 && <span className="dim">and {hidden} more</span>}
    </div>
  );
}

export function PartResultRow({ part }: { part: PartSummary }) {
  return (
    <>
      <div className="title">{part.name}</div>
      <div className="sub">
        {part.mpn !== null && <span className="mono">{part.mpn}</span>}
        {part.mpn !== null && part.description !== null && " · "}
        {part.description}
      </div>
      {part.is_stub && <span className="badge badge-warn">stub</span>}
      <StockLine part={part} />
      <PlaceLine part={part} />
    </>
  );
}
