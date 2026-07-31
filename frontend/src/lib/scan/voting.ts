/**
 * Frame voting for the barcode decoder.
 *
 * A single decoded frame is not trustworthy. `zxing` will occasionally return a
 * checksum-valid read from a partially occluded or motion-blurred DataMatrix,
 * and a wrong-but-confident part number is the most expensive mistake this
 * system can make — so a payload is only accepted once **two of the last three
 * frames agree**, which is the cheapest possible guard that still costs nothing
 * when the label is held still.
 *
 * Frames where nothing decoded are pushed into the window as `null`. They
 * occupy a slot and never match anything, so a flicker between two labels does
 * not accumulate votes across the gap.
 */

/** Frames kept in the sliding window. */
export const VOTE_WINDOW = 3;

/** Identical payloads needed inside that window to accept. */
export const VOTES_NEEDED = 2;

export class FrameVoter {
  readonly #window: number;
  readonly #needed: number;
  #frames: (string | null)[] = [];

  constructor(window: number = VOTE_WINDOW, needed: number = VOTES_NEEDED) {
    if (window < 1) {
      throw new RangeError("the vote window must hold at least one frame");
    }
    if (needed < 1 || needed > window) {
      throw new RangeError("votes needed must be between 1 and the window size");
    }
    this.#window = window;
    this.#needed = needed;
  }

  /**
   * Record one decode attempt and return the payload if it has now won.
   *
   * Returns `null` while a payload is still short of the threshold, and resets
   * the window on acceptance — otherwise the winning frames would stay in the
   * window and a single further sighting would re-fire immediately.
   */
  observe(payload: string | null): string | null {
    this.#frames.push(payload);
    if (this.#frames.length > this.#window) {
      this.#frames.shift();
    }
    if (payload === null) {
      return null;
    }

    let votes = 0;
    for (const frame of this.#frames) {
      if (frame === payload) {
        votes += 1;
      }
    }
    if (votes < this.#needed) {
      return null;
    }
    this.reset();
    return payload;
  }

  /** Current window contents, oldest first. Exposed for tests and diagnostics. */
  get frames(): readonly (string | null)[] {
    return [...this.#frames];
  }

  reset(): void {
    this.#frames = [];
  }
}

/**
 * The same 2-of-3 rule as {@link FrameVoter}, but for a frame that can decode
 * *several* payloads at once — a reel label commonly carries a DataMatrix and
 * a Code 128 of the same MPN, and reading both is more information, not less.
 *
 * Each payload is voted independently against the same sliding window of
 * frames, so a DataMatrix that has been in view for two frames and a Code 128
 * that just appeared do not have to wait for each other — each surfaces the
 * moment *it* has two votes in three frames. An empty frame is still pushed
 * into the window (as `[]`) for the same reason `FrameVoter` pushes `null`:
 * so a flicker occupies a slot rather than leaving stale votes in play
 * forever.
 */
export class MultiFrameVoter {
  readonly #window: number;
  readonly #needed: number;
  #frames: ReadonlySet<string>[] = [];

  constructor(window: number = VOTE_WINDOW, needed: number = VOTES_NEEDED) {
    if (window < 1) {
      throw new RangeError("the vote window must hold at least one frame");
    }
    if (needed < 1 || needed > window) {
      throw new RangeError("votes needed must be between 1 and the window size");
    }
    this.#window = window;
    this.#needed = needed;
  }

  /**
   * Record one decode attempt (zero or more payloads) and return every
   * payload that has now won.
   *
   * Winning payloads are removed from the window on acceptance — the same
   * per-payload reset `FrameVoter` does globally — so a symbol that just won
   * needs fresh votes before it can win again, while a *different* payload
   * still accumulating in the same window is untouched.
   */
  observe(payloads: readonly string[]): string[] {
    const frame = new Set(payloads);
    this.#frames.push(frame);
    if (this.#frames.length > this.#window) {
      this.#frames.shift();
    }

    const winners: string[] = [];
    for (const payload of frame) {
      let votes = 0;
      for (const seen of this.#frames) {
        if (seen.has(payload)) {
          votes += 1;
        }
      }
      if (votes >= this.#needed) {
        winners.push(payload);
      }
    }
    for (const winner of winners) {
      this.#forget(winner);
    }
    return winners;
  }

  /** Current window contents, oldest first. Exposed for tests and diagnostics. */
  get frames(): readonly (readonly string[])[] {
    return this.#frames.map((frame) => [...frame]);
  }

  reset(): void {
    this.#frames = [];
  }

  /** Strip one payload out of every frame in the window, without touching the rest. */
  #forget(payload: string): void {
    this.#frames = this.#frames.map((frame) => {
      if (!frame.has(payload)) {
        return frame;
      }
      const next = new Set(frame);
      next.delete(payload);
      return next;
    });
  }
}
