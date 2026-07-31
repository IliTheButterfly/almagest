/**
 * Opening a project or a build as a tab, and closing it again — the one control
 * that turns the mode on.
 *
 * Shared by the project and the build screen because ADR 0010's strip is
 * heterogeneous: the two screens open different kinds of target and the gesture,
 * the wording and the refusal are the same, and two copies would be two places for
 * the close rule to drift.
 *
 * The close rule is the part worth reading: **a tab holding uncommitted lines is
 * not closed silently.** The store refuses and says how many lines are in it; this
 * asks, and only a deliberate second press discards them. Those lines are a
 * statement about parts that physically moved, so losing them to a stray tap is the
 * failure this whole feature exists to prevent.
 */

import { useState } from "react";

import { describeTarget } from "../lib/cart/describe";
import { useOpenTargets } from "../lib/projectcontext/hooks";
import { openTargets } from "../lib/projectcontext/store";
import { sameTarget, targetKey, type WorkTarget } from "../lib/projectcontext/target";
import { Notice } from "./Feedback";

export function OpenTargetButton({ target }: { target: WorkTarget }) {
  const open = useOpenTargets();
  const [asking, setAsking] = useState<number | null>(null);
  const isOpen = open.some((candidate) => sameTarget(candidate, target));

  function close(discardLines: boolean): void {
    const outcome = openTargets.close(targetKey(target), { discardLines });
    setAsking(outcome.closed ? null : outcome.lines);
  }

  if (!isOpen) {
    return (
      <button
        type="button"
        onClick={() => {
          openTargets.openTarget(target);
        }}
      >
        Work on this
      </button>
    );
  }

  return (
    <>
      <button type="button" onClick={() => close(false)}>
        Stop working on this
      </button>
      {asking !== null && (
        <Notice kind="warn" title="That tab still has lines nobody has committed">
          <p style={{ margin: 0 }}>
            {asking === 1 ? "One line" : `${asking} lines`} in {describeTarget(target)} have not
            been committed, so nothing has been written to the ledger for them. Commit them from
            the panel, or discard them here.
          </p>
          <div className="row">
            <button
              type="button"
              className="danger"
              onClick={() => {
                close(true);
              }}
            >
              Discard {asking === 1 ? "it" : "them"} and close
            </button>
            <button
              type="button"
              onClick={() => {
                setAsking(null);
              }}
            >
              Keep the tab
            </button>
          </div>
        </Notice>
      )}
    </>
  );
}
