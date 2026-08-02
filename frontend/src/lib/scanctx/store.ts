/**
 * What was scanned recently, available to every screen.
 *
 * Scanning used to be somewhere you *went*: the scan screen resolved a payload,
 * showed you the answer, and the answer died with the screen. So the two things
 * a person at a cabinet actually wants — "am I holding the drawer this take is
 * from?" and "put this away *here*" — could not be answered by scanning at all,
 * because the field that needed the answer was on a different screen from the
 * reader.
 *
 * This makes a scan an **input method** rather than a destination. A tag read
 * anywhere lands here, the work panel shows it, and any field that has opted in
 * can take it.
 *
 * **Deliberately not persisted.** `localStorage` would carry yesterday's scans
 * into today, and a stale container offered as "just scanned" is exactly the
 * misattribution the whole design guards against — the same reasoning that keeps
 * a cart bound to one target rather than re-aimable. A scan is a statement about
 * where you are standing *now*; when the tab closes, you are not standing there
 * any more.
 *
 * Bounded at `MAX_SCANS`, newest first. A bench session is hours long and a
 * reader fires on every tap; an unbounded list would be a memory leak whose
 * symptom is a panel nobody can read.
 */

import type { ScanTarget } from "../api/client";

/** Ten is a walk down one aisle. Beyond that the panel is a log, not a context. */
export const MAX_SCANS = 10;

export interface ScanRecord {
  /** Client-side identity, so a re-scan of the same tag is a new row. */
  readonly id: string;
  readonly at: number;
  /** The payload as read, verbatim. */
  readonly code: string;
  readonly symbology: string | null;
  /**
   * What the server said it was, or `null` when nothing matched.
   *
   * Kept even when null: "you scanned something and it means nothing" is a
   * useful thing for the panel to be able to say, and dropping it would make an
   * unrecognised tag look like a reader that did not fire.
   */
  readonly target: ScanTarget | null;
  /** `resolved` | `ambiguous` | `unmatched`, from the resolver. */
  readonly status: string;
}

type Listener = () => void;

class ScanContext {
  #scans: readonly ScanRecord[] = [];
  readonly #listeners = new Set<Listener>();

  list(): readonly ScanRecord[] {
    return this.#scans;
  }

  /** The newest scan, or null. What a field offering "use this" reaches for. */
  latest(): ScanRecord | null {
    return this.#scans[0] ?? null;
  }

  /** The newest scan that resolved to a location, or null.
   *
   * A location field wants the drawer you scanned, not the reel you scanned
   * after it — so it asks by *type* rather than taking whatever is on top.
   * Without this, scanning a part to check it would silently disqualify the
   * container you scanned a moment earlier. */
  latestOfType(entityType: string): ScanRecord | null {
    return this.#scans.find((scan) => scan.target?.entity_type === entityType) ?? null;
  }

  add(scan: ScanRecord): void {
    this.#scans = [scan, ...this.#scans].slice(0, MAX_SCANS);
    this.#notify();
  }

  /** Drop one, when a person has dealt with it and wants it out of the way. */
  remove(id: string): void {
    this.#scans = this.#scans.filter((scan) => scan.id !== id);
    this.#notify();
  }

  clear(): void {
    this.#scans = [];
    this.#notify();
  }

  subscribe(listener: Listener): () => void {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  }

  #notify(): void {
    for (const listener of this.#listeners) {
      listener();
    }
  }
}

export const scanContext = new ScanContext();

/** A fresh instance, for tests that must not see another test's scans. */
export function newScanContext(): ScanContext {
  return new ScanContext();
}
