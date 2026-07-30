/**
 * The escalation ladder — deciding *which* decode pass to run, not how to run it.
 *
 * A single centre-ROI pass at `tryHarder: false` is fast and correct for the
 * label a user has actually centred, but it makes two whole classes of code
 * invisible: anything outside the ROI, and anything the cheap pass is too
 * conservative to read even when centred. Rather than paying for the
 * expensive read on every frame — which is how a phone drops to a crawl —
 * this escalates only after the cheap pass has failed a few times in a row,
 * holds the rung that found something until the vote on it is settled, and
 * drops straight back to the cheap pass the moment a payload is accepted.
 *
 * This module is deliberately decoder-agnostic: it knows nothing about
 * `zxing-wasm`, ROI rectangles, or `ImageData`. `decoder.ts` supplies the
 * passes (what to actually run); this only tracks *which index* is current
 * and *when* to move. That split is what makes the ladder itself testable
 * with a trivial stub decode function instead of a real camera.
 */

/** One rung of the ladder, in the caller's own terms. */
export interface EscalationLevel {
  readonly name: string;
  /**
   * The cadence this level should be driven at, in milliseconds. Cheap levels
   * match the decode loop's base interval; the expensive level is given a much
   * longer floor so a phone that escalates does not also start decoding at the
   * expensive pass's own frame rate.
   */
  readonly minIntervalMs: number;
}

export interface EscalationControllerOptions {
  /** Consecutive misses *at the current level* before moving up one rung. */
  readonly escalateAfter?: number;
}

/**
 * Tracks the current rung of the ladder.
 *
 * Pure and synchronous: `recordResult` is the only mutation, called once per
 * decode attempt with whether that attempt found anything. It is the caller's
 * job to actually run the pass named by `level`/`levelName` — this class never
 * touches a frame.
 */
export class EscalationController {
  readonly #levels: readonly EscalationLevel[];
  readonly #escalateAfter: number;
  #level = 0;
  #misses = 0;

  constructor(levels: readonly EscalationLevel[], options: EscalationControllerOptions = {}) {
    if (levels.length === 0) {
      throw new RangeError("the escalation ladder needs at least one level");
    }
    const escalateAfter = options.escalateAfter ?? 2;
    if (escalateAfter < 1) {
      throw new RangeError("escalateAfter must be at least one consecutive miss");
    }
    this.#levels = levels;
    this.#escalateAfter = escalateAfter;
  }

  /** Index of the level that should run next. */
  get level(): number {
    return this.#level;
  }

  get current(): EscalationLevel {
    // Non-null by construction: the constructor rejects an empty ladder and
    // `#level` never leaves `[0, levels.length - 1]`.
    return this.#levels[this.#level] as EscalationLevel;
  }

  /**
   * Record the outcome of one decode attempt at the current level.
   *
   * A hit **holds** the current rung: it clears the miss count but does not
   * descend. That is what makes escalating useful rather than merely expensive.
   * A payload has to be seen in two of three frames before it is accepted, and
   * dropping to the cheap ROI pass on the *first* sighting hands the next two
   * frames to a pass that cannot see the code at all — a QR the user did not
   * centre would be sighted, dropped, sighted, dropped, and never once reach a
   * second vote. Staying put means the pass that found something gets to
   * confirm it.
   *
   * Descending is {@link reset}'s job, and the decode loop calls it when a
   * payload is actually **accepted** — the point at which "a well-aimed label
   * decodes at full speed again immediately" is true rather than hoped.
   *
   * A miss moves up only after `escalateAfter` consecutive misses, and only if
   * there is a rung above the current one.
   */
  recordResult(found: boolean): void {
    if (found) {
      this.#misses = 0;
      return;
    }
    this.#misses += 1;
    if (this.#misses >= this.#escalateAfter && this.#level < this.#levels.length - 1) {
      this.#level += 1;
      this.#misses = 0;
    }
  }

  /** Back to the cheapest rung. Called when a payload has been accepted. */
  reset(): void {
    this.#level = 0;
    this.#misses = 0;
  }
}

export interface EscalationAttempt<T> {
  readonly result: T | null;
  readonly level: number;
  readonly levelName: string;
  readonly elapsedMs: number;
}

/**
 * Run one decode attempt at the controller's current level and feed the
 * outcome back into it.
 *
 * `decodeAtLevel` is handed the level *index* rather than the `EscalationLevel`
 * itself — the caller (`decoder.ts`) owns what each index actually means (ROI
 * vs. full frame, which formats, `tryHarder` or not); this function only needs
 * to know whether the result was empty.
 *
 * `elapsedMs` is measured here, once, so callers get a consistent timing
 * signal for adaptive scheduling without each needing its own clock.
 */
export async function runEscalationAttempt<T>(
  controller: EscalationController,
  decodeAtLevel: (level: number) => Promise<T | null>,
  now: () => number = () => performance.now(),
): Promise<EscalationAttempt<T>> {
  const level = controller.level;
  const levelName = controller.current.name;
  const startedAt = now();
  const result = await decodeAtLevel(level);
  const elapsedMs = now() - startedAt;
  controller.recordResult(result !== null);
  return { result, level, levelName, elapsedMs };
}

/**
 * The next tick's delay: never faster than the level's own floor, and never
 * faster than the pass that just ran actually took — a slow phone on the
 * expensive pass must not have a second one queued behind it.
 */
export function nextDelayMs(level: EscalationLevel, elapsedMs: number): number {
  return Math.max(level.minIntervalMs, elapsedMs);
}
