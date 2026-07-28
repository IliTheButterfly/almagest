/**
 * Client-side short-ID tidying for the manual-entry path.
 *
 * Deliberately **not** a reimplementation of `services/shortid.py`. This
 * normalises what a human typed — strips the cosmetic hyphen, upper-cases, folds
 * Crockford's confusable glyphs, drops a display prefix — so the field can echo
 * the canonical form as they type and the request goes out in the shape the
 * server expects.
 *
 * The **mod-37 check symbol is verified server-side only**, on purpose. A second
 * implementation of the check arithmetic would be a second thing to keep in step
 * with the first, for the sake of saving one round trip; and the illustrative
 * codes in the docs (`4K7T-92MQ`) are not check-valid, so a client that enforced
 * the check would refuse to look up the very examples the design uses. The server
 * owns that answer.
 */

const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const DATA_SYMBOLS = 7;
export const SHORT_ID_SYMBOLS = DATA_SYMBOLS + 1;

/** Crockford's canonical confusions. `U` is excluded, never remapped. */
const CONFUSIONS: Readonly<Record<string, string>> = { O: "0", I: "1", L: "1" };

function squash(text: string): string {
  return text
    .replace(/[\s\-_.]+/g, "")
    .toUpperCase()
    .replace(/[OIL]/g, (glyph) => CONFUSIONS[glyph] ?? glyph);
}

/**
 * Canonicalise typed or scanned text to bare symbols.
 *
 * Mirrors the server's tolerance for a display prefix (`BIN 4K7T-92MQ`) by
 * keeping only the last whitespace-separated token, and only when that token is
 * itself full length — otherwise the space was the group separator
 * (`4K7T 92MQ`) and the whole string is meant. The prefix is discarded rather
 * than parsed; it carries no meaning.
 */
export function normalizeShortId(raw: string): string {
  const stripped = raw.trim();
  if (stripped === "") {
    return "";
  }
  const tokens = stripped.split(/\s+/);
  const last = tokens[tokens.length - 1];
  if (tokens.length > 1 && last !== undefined && squash(last).length === SHORT_ID_SYMBOLS) {
    return squash(last);
  }
  return squash(stripped);
}

/**
 * Whether text is the right shape to be a short ID — length and alphabet only.
 *
 * Used to decide whether a manual-entry field should be resolved as a short ID
 * or searched as a part number. A false positive here costs one failed lookup;
 * the resolver chain is also designed to yield on a well-formed-but-unbound code,
 * so nothing gets stuck.
 */
export function looksLikeShortId(raw: string): boolean {
  const text = normalizeShortId(raw);
  return (
    text.length === SHORT_ID_SYMBOLS && [...text].every((symbol) => ALPHABET.includes(symbol))
  );
}

/** The 4 + 4 rendering used on printed labels. Cosmetic; never stored. */
export function formatShortId(shortId: string | null | undefined): string {
  if (shortId === null || shortId === undefined || shortId === "") {
    return "";
  }
  const text = normalizeShortId(shortId);
  if (text.length !== SHORT_ID_SYMBOLS) {
    return shortId;
  }
  return `${text.slice(0, 4)}-${text.slice(4)}`;
}

/**
 * The short ID out of a `/s/{short_id}` URL, which is what an NFC tag's NDEF
 * record and a printed QR both contain. Returns the input untouched when it is
 * not one of our URLs, so an arbitrary vendor payload passes straight through to
 * the resolver chain.
 */
export function shortIdFromPayload(payload: string): string {
  const match = /\/s\/([^/?#\s]+)/.exec(payload.trim());
  return match?.[1] ?? payload.trim();
}
