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
    </>
  );
}
