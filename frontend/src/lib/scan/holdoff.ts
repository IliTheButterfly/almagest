/**
 * The payload hold-off — one mechanism, used at two windows.
 *
 * `PLAN.md` calls for a **3-second payload-hash hold-off** in the decoder so
 * that one label held in front of the camera does not fire five resolves, and
 * for duplicate scans **within ~2 s, or during an in-flight commit**, to be
 * dropped "by the same debounce as the decoder". This class is that debounce;
 * the two call sites differ only in their window and in whether a repeat sighting
 * pushes the window forward.
 *
 * Keyed per payload rather than on a single most-recent slot, because two labels
 * alternating in view must each be held off independently — a single slot would
 * let A, B, A, B fire four times.
 */

/** Decoder window: a label sitting in the frame resolves once. */
export const DECODER_HOLD_OFF_MS = 3_000;

/** Scan-session window: a deliberate re-scan is dropped if it is this quick. */
export const SCAN_DEBOUNCE_MS = 2_000;

export interface HoldOffOptions {
  /** Injected in tests; defaults to the wall clock. */
  readonly now?: () => number;
  /**
   * Whether a suppressed sighting restarts the window.
   *
   * `false` (default) is the literal reading of "duplicate scans within ~2 s are
   * dropped": the window runs from the sighting that was *admitted*.
   *
   * `true` is what the decoder needs. At ~10 frames a second a label held up for
   * fifteen seconds is sighted 150 times, and with a fixed window that still
   * fires five resolves — exactly what the hold-off exists to prevent. Pushing
   * the window forward on every sighting means the label fires once and cannot
   * fire again until it has been out of frame for the full window.
   */
  readonly refreshWhileSuppressed?: boolean;
}

export class PayloadHoldOff {
  readonly #windowMs: number;
  readonly #now: () => number;
  readonly #refresh: boolean;
  readonly #seen = new Map<string, number>();
  #blocked = false;

  constructor(windowMs: number, options: HoldOffOptions = {}) {
    this.#windowMs = windowMs;
    this.#now = options.now ?? (() => Date.now());
    this.#refresh = options.refreshWhileSuppressed ?? false;
  }

  /**
   * Whether this payload should be acted on now. Records the sighting.
   *
   * Returns `false` while blocked, and `false` for a payload seen inside the
   * window.
   */
  admit(payload: string): boolean {
    const at = this.#now();
    if (this.#blocked) {
      // A sighting during a commit is still a sighting: remember it so that
      // letting go of the block does not immediately fire the label the user is
      // still holding in front of the lens.
      if (this.#refresh) {
        this.#seen.set(payload, at);
      }
      return false;
    }

    this.#prune(at);
    const previous = this.#seen.get(payload);
    if (previous !== undefined && at - previous < this.#windowMs) {
      if (this.#refresh) {
        this.#seen.set(payload, at);
      }
      return false;
    }
    this.#seen.set(payload, at);
    return true;
  }

  /**
   * Suppress everything — used while a commit is in flight, so a double scan
   * cannot queue a second movement behind the first.
   */
  block(): void {
    this.#blocked = true;
  }

  unblock(): void {
    this.#blocked = false;
  }

  get blocked(): boolean {
    return this.#blocked;
  }

  /** Forget every sighting, e.g. when the camera is restarted. */
  forget(): void {
    this.#seen.clear();
  }

  #prune(at: number): void {
    for (const [payload, seenAt] of this.#seen) {
      if (at - seenAt >= this.#windowMs) {
        this.#seen.delete(payload);
      }
    }
  }
}
