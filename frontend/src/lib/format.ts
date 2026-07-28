/**
 * Display helpers for the integer-scaled fields the API speaks in.
 *
 * Quantities cross the wire as `*_milli` and money as `*_micro`, both integers,
 * so ledger sums stay exact. Nothing here ever feeds a value back into a
 * calculation — these functions are the last step before text, and the integer
 * form is what is stored, sent and compared.
 */

const MILLI = 1000;
const MICRO = 1_000_000;

/** Whole units from thousandths. Lossy by design; display only. */
export function fromMilli(milli: number): number {
  return milli / MILLI;
}

/** Thousandths from whole units, rounded — `2.0005` is not representable. */
export function toMilli(units: number): number {
  return Math.round(units * MILLI);
}

/**
 * A quantity, with the fractional part shown only when there is one.
 *
 * Most parts are counted in whole pieces, so "1200" beats "1200.000" on a phone
 * screen; a part measured in metres of wire still needs its decimals.
 */
export function formatQty(milli: number): string {
  if (milli % MILLI === 0) {
    return (milli / MILLI).toLocaleString();
  }
  return (milli / MILLI).toLocaleString(undefined, {
    minimumFractionDigits: 1,
    maximumFractionDigits: 3,
  });
}

/** A signed quantity, for ledger deltas where the direction is the point. */
export function formatDelta(milli: number): string {
  const sign = milli > 0 ? "+" : "";
  return `${sign}${formatQty(milli)}`;
}

export function formatMoneyMicro(micro: number | null, currency: string | null): string | null {
  if (micro === null) {
    return null;
  }
  const amount = micro / MICRO;
  if (currency === null || currency === "") {
    return amount.toLocaleString(undefined, { maximumFractionDigits: 6 });
  }
  try {
    return amount.toLocaleString(undefined, {
      style: "currency",
      currency,
      maximumFractionDigits: 6,
    });
  } catch {
    // An unknown or malformed currency code must not blank out the price.
    return `${amount.toLocaleString(undefined, { maximumFractionDigits: 6 })} ${currency}`;
  }
}

/**
 * Parse keypad text into thousandths.
 *
 * Returns `null` for anything that is not a non-negative number, so the caller
 * can leave the field alone rather than snapping it to zero mid-typing.
 */
export function parseQtyToMilli(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "" || !/^\d*(\.\d*)?$/.test(trimmed)) {
    return null;
  }
  const value = Number(trimmed);
  return Number.isFinite(value) ? toMilli(value) : null;
}

export function formatFillRatio(ratio: number | null): string {
  return ratio === null ? "—" : `${Math.round(ratio * 100)}%`;
}

/** Local time, seconds included: two ledger rows can be a second apart. */
export function formatTimestamp(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) {
    return iso;
  }
  return at.toLocaleString(undefined, {
    dateStyle: "short",
    timeStyle: "medium",
  });
}
