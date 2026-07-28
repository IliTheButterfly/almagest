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
