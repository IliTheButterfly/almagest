/**
 * Naming a target, in the same words everywhere.
 *
 * ADR 0010 makes this load-bearing rather than cosmetic: *"a take must never be
 * attributable to a target the user cannot see named at the moment they press the
 * button"*, and with several tabs open the wrong one being focused is a plausible
 * mistake rather than an exotic one. So the take control's own label carries the
 * target's name, and it is built here so the strip, the panel and the button cannot
 * drift into three phrasings of one state.
 */

import type { WorkTarget } from "../projectcontext/target";

/** What the tab shows, and the name every other phrase interpolates. */
export function describeTarget(target: WorkTarget): string {
  return target.label === "" ? fallbackLabel(target) : target.label;
}

function fallbackLabel(target: WorkTarget): string {
  return target.kind === "project" ? `project ${target.projectId}` : `build ${target.buildId}`;
}

/**
 * The take control's label: what pressing it does, and to which target.
 *
 * A return says "from" rather than "for" because it subtracts from that record —
 * the parts are going back on the shelf, and the record it comes off is still the
 * thing that has to be named.
 */
export function takeActionLabel(
  target: WorkTarget | null,
  direction: "take" | "return",
  quantity: string,
): string {
  const verb = direction === "take" ? "Take" : "Return";
  if (target === null) {
    // Nothing open: this writes to the ledger now, and there is no target to
    // name. ADR 0010's other half — a take with no tab is a take.
    return `${verb} ${quantity}`;
  }
  const preposition = direction === "take" ? "for" : "from";
  return `${verb} ${quantity} ${preposition} ${describeTarget(target)}`;
}

/** What committing a tab will do, said before it is done. */
export function describeCommit(target: WorkTarget): string {
  return target.kind === "project"
    ? `Add these lines to the bill of materials of ${describeTarget(target)}`
    : `Reserve these lines against ${describeTarget(target)}`;
}
