/**
 * What one decode attempt's symbols become — the decision `useScanner` makes on
 * every tick, lifted out of the hook so it can be tested without a camera.
 *
 * Three rules meet here, and the interaction between them is the part that is
 * easy to get wrong:
 *
 * 1. **Voting.** A payload is reported only once two of three frames agree, per
 *    `voting.ts`. A wrong-but-confident part number is the most expensive
 *    mistake this system can make.
 * 2. **The ladder holds until the vote settles.** A rung that sighted something
 *    keeps running; only an *accepted* payload sends the ladder back to the
 *    cheap ROI pass. Descending on the first sighting is the bug this module's
 *    tests pin down: a code visible only to the full-frame pass would be
 *    sighted, dropped to a pass that cannot see it, and never reach a second
 *    vote.
 * 3. **The hold-off is last.** A label parked in front of the lens wins the
 *    vote on nearly every frame; the hold-off is what turns that into one
 *    report. It runs *after* the ladder reset, because "is this label being
 *    read well here" and "have I already told the caller about it" are
 *    different questions.
 */

import type { Decoded } from "./decoder";
import type { EscalationController } from "./escalation";
import type { PayloadHoldOff } from "./holdoff";
import type { MultiFrameVoter } from "./voting";

export interface Admission {
  /** Symbols to hand to the caller, in the order they won. */
  readonly report: readonly Decoded[];
  /** Whether any payload won its vote — a sighting alone is not one. */
  readonly accepted: boolean;
}

/**
 * Feed one attempt's symbols through the voter, the ladder and the hold-off.
 *
 * Mutates all three, which is the point: they are the loop's state. `decoded`
 * being empty is a normal miss and is still pushed into the vote window, so a
 * flicker occupies a slot rather than leaving stale votes in play.
 */
export function admitDecoded(
  escalation: EscalationController,
  voter: MultiFrameVoter,
  holdOff: PayloadHoldOff,
  decoded: readonly Decoded[],
): Admission {
  const winners = voter.observe(decoded.map((symbol) => symbol.text));
  if (winners.length > 0) {
    // This rung has done its job, so the next label gets the cheap pass again.
    // Reset even for a winner the hold-off goes on to suppress: the label is
    // being read fine at this level, which is the only question being asked.
    escalation.reset();
  }

  const report: Decoded[] = [];
  for (const winner of winners) {
    const symbol = decoded.find((candidate) => candidate.text === winner);
    if (symbol !== undefined && holdOff.admit(winner)) {
      report.push(symbol);
    }
  }
  return { report, accepted: winners.length > 0 };
}
