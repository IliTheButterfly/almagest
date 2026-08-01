/**
 * Turning an outline into the values a person would actually want.
 *
 * A region is not a value. Tapping a DataMatrix on a reel label and getting
 * `[)>\x1e06\x1dP...` on the clipboard is useless — what the user wants is the
 * MPN inside it, or the quantity, or the date code. The resolver already takes a
 * payload apart into exactly those fields (`ScanParsed`, filled by the
 * ECIA/MH10.8.2 parser), so **one barcode region expands into several chips** and
 * none of that parsing is reimplemented here.
 *
 * ## Provenance is carried, not flattened
 *
 * Three sources, kept apart in the type because they warrant different trust:
 *
 * - `verified` — decoded from a checksummed symbology, or a field the resolver
 *   parsed out of one. Correct unless the label itself is wrong.
 * - `read` — a line an OCR pass guessed at, with its confidence attached.
 * - `resolved` — this payload is a short ID we minted, and it points at a real
 *   row. Worth its own kind because the useful action is *going there*, not
 *   copying it.
 *
 * ## Why only `verified` chips carry a target field
 *
 * A chip with a `field` can be dropped straight into a form. That is only ever
 * populated from the resolver's parse, where an ECIA data identifier says
 * unambiguously that `1P` is the supplier part number — a rule, not a guess.
 *
 * An OCR'd line never gets one, deliberately. `CLAUDE.md` and `docs/PLAN.md` both
 * state that an OCR'd or model-read part number is never auto-accepted, and the
 * cheap way to violate that is a helpful heuristic that decides a line "looks
 * like an MPN" and offers to fill the MPN box. So a text chip can still fill a
 * field — but only the field the user was already pointing at when they opened
 * the capture. The tap is the acceptance, and it has to be aimed.
 */

import { formatQty } from "../format";
import type { ScanResolveResponse } from "../api/client";
import type { Region } from "./types";

/** Fields a chip can be dropped into. Mirrors the intake form's own names. */
export type FillField =
  | "name"
  | "mpn"
  | "manufacturer"
  | "supplier_part_number"
  | "quantity"
  | "date_code"
  | "lot_code";

export type ChipKind = "verified" | "read" | "resolved";

export interface Chip {
  /** Stable within one capture, so React keys and "just copied" state behave. */
  readonly id: string;
  /** What this value is — "MPN", "Quantity", "Read text". */
  readonly label: string;
  /** Exactly what lands on the clipboard or in a field. */
  readonly value: string;
  readonly kind: ChipKind;
  /** 0-100, `read` chips only. */
  readonly confidence?: number;
  /** Only ever set on `verified` chips — see the module comment. */
  readonly field?: FillField;
  /** Set on a `resolved` chip: where tapping it should go. */
  readonly href?: string;
}

/** Where a resolved target lives in the app. Mirrors ScanScreen's own map. */
function routeFor(entityType: string, entityPk: number): string | null {
  switch (entityType) {
    case "location":
      return `/locations/${entityPk}`;
    case "part":
      return `/parts/${entityPk}`;
    case "stock_lot":
      return `/lots/${entityPk}`;
    default:
      return null;
  }
}

/**
 * Printable payloads only.
 *
 * An ECIA payload *is* its GS/RS/EOT separators and must be stored verbatim, but
 * a "copy the whole payload" chip carrying raw control characters pastes
 * invisible junk into a text box. So the whole-payload chip is offered only when
 * the payload is something a person could meaningfully paste; the verbatim
 * bytes are already safe in `scan_events` either way.
 */
function isPrintable(text: string): boolean {
  // C0 controls plus DEL. `\x1d` (GS), `\x1e` (RS) and `\x04` (EOT) are the ones
  // that actually turn up — they are the ECIA envelope's own separators.
  // eslint-disable-next-line no-control-regex
  return !/[\x00-\x1f\x7f]/.test(text);
}

function push(
  chips: Chip[],
  id: string,
  label: string,
  value: string | null | undefined,
  field?: FillField,
): void {
  if (value === null || value === undefined || value === "") {
    return;
  }
  chips.push({
    id,
    label,
    value,
    kind: "verified",
    ...(field === undefined ? {} : { field }),
  });
}

/**
 * Every value worth offering for one region.
 *
 * `resolved` is what `/api/scan/resolve` said about this region's payload, when
 * it has been asked. Absent for a text region, and absent for a barcode until
 * the round trip lands — the outline is drawn before that, so this has to
 * produce something useful with nothing but the raw text.
 */
export function chipsForRegion(
  region: Region,
  index: number,
  resolved?: ScanResolveResponse | null,
): Chip[] {
  if (region.kind === "text") {
    return [
      {
        id: `r${index}-text`,
        label: "Read text",
        value: region.text,
        kind: "read",
        ...(region.confidence === undefined ? {} : { confidence: region.confidence }),
      },
    ];
  }

  const chips: Chip[] = [];
  const target = resolved?.target ?? null;
  if (target !== null && target !== undefined) {
    const href = routeFor(target.entity_type, target.entity_pk);
    chips.push({
      id: `r${index}-target`,
      label: target.retired ? "Removed container" : "Open",
      value: target.label_path ?? target.label,
      kind: "resolved",
      ...(href === null ? {} : { href }),
    });
  }

  const parsed = resolved?.parsed ?? null;
  if (parsed !== null && parsed !== undefined) {
    // **Two part numbers, and no way to tell which is the manufacturer's.**
    // `app.services.scanning.ecia.mpn_candidates` states the problem exactly:
    // distributors disagree about whether DI `P` or DI `1P` carries the MPN, and
    // nothing on the label says which convention was used. On a DigiKey reel
    // `1P` is the manufacturer's part and `P` is an order code; on other labels
    // it is the other way round.
    //
    // So neither is labelled "MPN" outright. Both are offered, both can fill the
    // MPN field, and the DI they came from is on the chip — which is the piece
    // of information that actually lets a person decide, since it is printed on
    // the label in front of them. `1P` leads because it is the more common
    // carrier, not because it is known to be right here.
    const bothPartNumbers =
      Boolean(parsed.mpn) &&
      Boolean(parsed.supplier_part_number) &&
      parsed.mpn !== parsed.supplier_part_number;

    if (bothPartNumbers) {
      push(chips, `r${index}-spn`, "Part number · 1P", parsed.supplier_part_number, "mpn");
      push(chips, `r${index}-mpn`, "Part number · P", parsed.mpn, "mpn");
    } else {
      push(chips, `r${index}-mpn`, "MPN", parsed.mpn, "mpn");
      push(
        chips,
        `r${index}-spn`,
        "Supplier PN",
        parsed.supplier_part_number,
        "supplier_part_number",
      );
    }
    push(chips, `r${index}-mfr`, "Manufacturer", parsed.manufacturer, "manufacturer");
    if (parsed.quantity_milli !== null && parsed.quantity_milli !== undefined) {
      push(
        chips,
        `r${index}-qty`,
        "Quantity",
        formatQty(parsed.quantity_milli),
        "quantity",
      );
    }
    push(chips, `r${index}-date`, "Date code", parsed.date_code, "date_code");
    push(chips, `r${index}-lot`, "Lot", parsed.lot_code, "lot_code");
    push(chips, `r${index}-po`, "PO", parsed.purchase_order);
    push(chips, `r${index}-serial`, "Serial", parsed.serial);
    push(chips, `r${index}-country`, "Country", parsed.country_of_origin);
  }

  // Always last, and only when it is something a person could paste. It is the
  // fallback that makes an unparsed vendor format still useful — which is the
  // same reason `scan_events` keeps the bytes.
  if (isPrintable(region.text)) {
    chips.push({
      id: `r${index}-raw`,
      label: chips.length === 0 ? region.symbology : "Whole payload",
      value: region.text,
      kind: "verified",
    });
  }
  return chips;
}
