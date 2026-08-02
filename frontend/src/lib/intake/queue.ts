/**
 * The intake queue — "queue for later", the fast path.
 *
 * `PLAN.md`: *"the fast path is the point"*. At RESOLVING, one tap parks the
 * label and returns straight to scanning with zero further screens, so a box of
 * reels is scanned in under a minute and curated at a desktop afterwards. This is
 * the countermeasure to the thing that actually kills projects in this space —
 * intake that costs a form per item.
 *
 * **This is a write-behind buffer, not the record.** `POST /api/intake/pending`
 * is where a parked scan ends up; this holds it until then. Writing locally first
 * is not a fallback — it is the design. The fast path's value is that it never
 * stops to talk to anything, and a shelf in a basement is exactly where the wifi
 * is worst, so an intake path that needed the network would fail at the only
 * moment it matters.
 *
 * `lib/intake/sync.ts` drains it. The handover works because each entry already
 * carries the `client_op_id` minted at scan time: that is the server's
 * idempotency key, so pushing the same entry twice converges on one row, and an
 * entry is only dropped from here once the server has confirmed it.
 */

const STORAGE_KEY = "almagest.intake.pending.v1";

export interface PendingScan {
  /** The `client_op_id` minted at scan time. Also this entry's identity. */
  readonly id: string;
  /** The payload verbatim, control characters included. */
  readonly code: string;
  readonly symbology: string | null;
  readonly queuedAt: number;
  /** Which resolver handler claimed the payload, if any. Display only. */
  readonly decodedKind: string | null;
  readonly mpn: string | null;
  readonly manufacturer: string | null;
  readonly supplierPartNumber: string | null;
  readonly quantityMilli: number | null;
  readonly dateCode: string | null;
  readonly lotCode: string | null;
  /** A resolved part, when the scan matched something and was parked anyway. */
  readonly partId: number | null;
  /**
   * The still taken alongside this scan, once the server has stored it.
   *
   * Null is normal and not a failure: a capture is saved over the network and
   * the queue deliberately is not, so parking at a shelf with no signal parks a
   * payload with no picture. The desk pass then has exactly what the aisle had.
   */
  readonly captureId: number | null;
  readonly note: string | null;
}

export interface QueueStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function isPendingScan(value: unknown): value is PendingScan {
  if (value === null || typeof value !== "object") {
    return false;
  }
  const record = value as Record<string, unknown>;
  return typeof record["id"] === "string" && typeof record["code"] === "string";
}

/**
 * A queue that never throws at the caller.
 *
 * Storage can fail for reasons that have nothing to do with this app — Safari's
 * private mode, a full quota, a locked-down kiosk profile. Losing the parked
 * queue is bad; taking the scanner down with it is worse, so every failure
 * degrades to an in-memory queue for the session.
 */
export class IntakeQueue {
  readonly #storage: QueueStorage | null;
  readonly #listeners = new Set<() => void>();
  #cache: PendingScan[];

  constructor(storage: QueueStorage | null = defaultStorage()) {
    this.#storage = storage;
    this.#cache = this.#load();
  }

  list(): readonly PendingScan[] {
    return this.#cache;
  }

  get size(): number {
    return this.#cache.length;
  }

  /** Newest last. Re-queuing the same `id` replaces the earlier entry. */
  add(entry: PendingScan): void {
    this.#write([...this.#cache.filter((existing) => existing.id !== entry.id), entry]);
  }

  remove(id: string): void {
    this.#write(this.#cache.filter((entry) => entry.id !== id));
  }

  clear(): void {
    this.#write([]);
  }

  subscribe(listener: () => void): () => void {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  }

  #load(): PendingScan[] {
    try {
      const raw = this.#storage?.getItem(STORAGE_KEY);
      if (raw === null || raw === undefined) {
        return [];
      }
      const parsed: unknown = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed.filter(isPendingScan) : [];
    } catch {
      return [];
    }
  }

  /**
   * True once a write to `localStorage` has failed — quota, Safari private mode,
   * a locked-down kiosk profile.
   *
   * **The queue keeps working in memory, and the user has to be told.** Staying
   * up is right: stopping the scanner because storage is full would cost the
   * whole box of reels rather than the tab. Staying up *quietly* is not, and
   * `sync.ts` states the rule this used to break — "the failure mode of this
   * feature is losing intake data, which the user must be told about rather than
   * have hidden by a spinner." Closing the tab is then an ordinary thing to do
   * and thirty parked scans go with it.
   *
   * Sticky by design: it stays true for the rest of the session even if a later
   * write succeeds, because the entries lost from the earlier failure are not
   * coming back and a warning that flickers off is worse than none.
   */
  get degraded(): boolean {
    return this.#degraded;
  }

  #degraded = false;

  #write(entries: PendingScan[]): void {
    this.#cache = entries;
    try {
      if (entries.length === 0) {
        this.#storage?.removeItem(STORAGE_KEY);
      } else {
        this.#storage?.setItem(STORAGE_KEY, JSON.stringify(entries));
      }
    } catch {
      // In-memory only for the rest of the session — and said out loud, through
      // the same notification every other change goes through, so no screen has
      // to poll for it.
      this.#degraded = true;
    }
    for (const listener of this.#listeners) {
      listener();
    }
  }
}

function defaultStorage(): QueueStorage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export const intakeQueue = new IntakeQueue();
