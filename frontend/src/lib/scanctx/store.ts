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
  /**
   * Which reader has this tag against it *now*, or null once it was lifted off.
   *
   * Presence, not history. A tag held against the reader is one row that stays
   * current, not a row per poll — the agent states presence by its edges
   * (`tag.seen` on arrival, `tag.gone` on departure) and this mirrors that. A
   * field offering "use the drawer you are holding" wants the one that is still
   * there; the rest are what you scanned earlier, and a panel that could not
   * tell them apart would offer a drawer you put down five minutes ago.
   */
  readonly presentOn: string | null;
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

  /** What is against a reader right now, newest first. Usually none or one. */
  present(): readonly ScanRecord[] {
    return this.#scans.filter((scan) => scan.presentOn !== null);
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
    // One reader holds one tag. A new arrival on a device therefore means
    // whatever it was holding is no longer there, even if no `tag.gone` reached
    // us — a tag swapped fast enough that no empty poll landed between them.
    this.#scans = [
      scan,
      ...this.#scans.map((row) =>
        row.presentOn === scan.presentOn ? { ...row, presentOn: null } : row,
      ),
    ].slice(0, MAX_SCANS);
    this.#notify();
  }

  /**
   * The tag on `deviceId` has been lifted off.
   *
   * The row stays — it is still the last thing you scanned, and dropping it
   * would make "I just scanned that" untrue the moment you took the drawer
   * away. What changes is that nothing may now claim you are holding it.
   */
  lifted(deviceId: string): void {
    let changed = false;
    this.#scans = this.#scans.map((row) => {
      if (row.presentOn !== deviceId) {
        return row;
      }
      changed = true;
      return { ...row, presentOn: null };
    });
    if (changed) {
      this.#notify();
    }
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
