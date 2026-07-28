/**
 * The intake queue — "queue for later", the fast path.
 *
 * `PLAN.md`: *"the fast path is the point"*. At RESOLVING, one tap parks the
 * label and returns straight to scanning with zero further screens, so a box of
 * reels is scanned in under a minute and curated at a desktop afterwards. This is
 * the countermeasure to the thing that actually kills projects in this space —
 * intake that costs a form per item.
 *
 * **It is stored client-side.** The design calls for `POST /api/intake/pending`;
 * that endpoint does not exist yet, so the queue lives in `localStorage` on the
 * device that did the scanning. The consequence is real and the UI says so: the
 * queue does not follow the user from the phone to the desktop until the endpoint
 * lands. Everything about the shape here is chosen so that swapping the storage
 * for the API is a change of transport and nothing else — each entry already
 * carries the `client_op_id` minted at scan time, which is exactly what the
 * endpoint will want in order to be idempotent.
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

  #write(entries: PendingScan[]): void {
    this.#cache = entries;
    try {
      if (entries.length === 0) {
        this.#storage?.removeItem(STORAGE_KEY);
      } else {
        this.#storage?.setItem(STORAGE_KEY, JSON.stringify(entries));
      }
    } catch {
      // In-memory only for the rest of the session.
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
