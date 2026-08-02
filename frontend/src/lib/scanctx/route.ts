/**
 * Where a scanned thing lives in the app.
 *
 * Shared rather than duplicated: the scan screen and the work panel both turn a
 * `ScanTarget` into a link, and two copies of this switch would drift the first
 * time a new entity type became scannable — the panel would silently render an
 * unclickable row for something the scan screen could open.
 */

import type { ScanTarget } from "../api/client";

export function routeForTarget(target: ScanTarget): string | null {
  switch (target.entity_type) {
    case "location":
      return `/locations/${target.entity_pk}`;
    case "part":
      return `/parts/${target.entity_pk}`;
    case "stock_lot":
      return `/lots/${target.entity_pk}`;
    default:
      return null;
  }
}
