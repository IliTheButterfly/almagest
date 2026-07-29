/**
 * Telling the INBOX apart from a project's staging box.
 *
 * **`is_staging` on its own stopped meaning "the inbox" at ADR 0004.** Every
 * project gets a lazily-created box carrying the same flag, and review found the
 * UI treating the two as one thing: a project box rendered a bare "inbox" badge,
 * and the tree screen printed a hardcoded notice calling it "the permanent
 * catch-all … meant to be emptied rather than lived in" — actively false about a
 * box that is holding a board's parts on purpose, and the kind of wrong label
 * that makes someone put stock back in a drawer it was deliberately taken out of.
 *
 * The distinction is `is_placeable === false`, which is not a guess: `staging.py`
 * sets it explicitly on every project box so auto-assignment can never propose
 * one as a *home*, and the backend's own `capacity.get_inbox_location` finds the
 * INBOX with exactly this predicate inverted. Asking the same question here means
 * the screen and the assignment ladder cannot disagree about which box is which.
 *
 * Deliberately **not** a `label_path` prefix test against `"PROJECTS"`: that
 * hardcodes a server-side display constant into the client, and it would start
 * lying the moment anybody renames the root — which is an ordinary location and
 * therefore renameable.
 */

/** Anything with the two flags the distinction needs. */
export interface StagingFlags {
  is_staging: boolean;
  is_placeable?: boolean | null;
}

/** A project's own box or one of its assembly boxes (ADR 0004). */
export function isProjectStagingBox(location: StagingFlags): boolean {
  return location.is_staging && location.is_placeable === false;
}

/** The permanent catch-all that auto-assignment falls back to. */
export function isInbox(location: StagingFlags): boolean {
  return location.is_staging && location.is_placeable !== false;
}
