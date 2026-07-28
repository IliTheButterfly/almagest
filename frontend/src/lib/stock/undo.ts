/**
 * The eight-second undo window, and the one case where it must not be offered.
 *
 * Every write response carries `replayed`. It is `true` when the server answered
 * from its idempotency store — the movement was recorded by an *earlier* request
 * with the same key, and this response is a copy of that earlier answer. Nothing
 * new was written.
 *
 * That distinction is the whole reason the field exists. "Your take was recorded
 * just now" and "your take was already recorded a minute ago" are otherwise
 * byte-identical responses, and showing an eight-second undo for the second one
 * is a lie: the window closed whenever the original commit happened, which may
 * have been long enough ago that undoing now would reverse a movement the user
 * has since forgotten about. So on a replay the UI says what happened and offers
 * nothing to press.
 */

/** How long the one-tap undo stays on screen. `PLAN.md` workflow 3. */
export const UNDO_WINDOW_MS = 8_000;

export interface Replayable {
  readonly replayed?: boolean;
}

/**
 * Whether a write's response earns an undo affordance.
 *
 * Defaults to offering one when the field is absent, since an older server that
 * predates the field only ever did the work.
 */
export function offersUndo(response: Replayable): boolean {
  return response.replayed !== true;
}

/** Whole seconds left in the window, for the countdown next to the button. */
export function undoSecondsLeft(elapsedMs: number, windowMs: number = UNDO_WINDOW_MS): number {
  return Math.max(0, Math.ceil((windowMs - elapsedMs) / 1000));
}
