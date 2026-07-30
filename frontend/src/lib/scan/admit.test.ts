import { describe, expect, it } from "vitest";

import { admitDecoded } from "./admit";
import type { Decoded } from "./decoder";
import { ESCALATION_LEVELS } from "./decoder";
import { EscalationController, runEscalationAttempt } from "./escalation";
import { DECODER_HOLD_OFF_MS, PayloadHoldOff } from "./holdoff";
import { MultiFrameVoter } from "./voting";

function symbol(text: string, symbology = "QRCode"): Decoded {
  return { text, symbology };
}

interface Loop {
  readonly escalation: EscalationController;
  readonly voter: MultiFrameVoter;
  readonly holdOff: PayloadHoldOff;
  clock: number;
}

function newLoop(): Loop {
  const loop: Loop = {
    escalation: new EscalationController(ESCALATION_LEVELS),
    voter: new MultiFrameVoter(),
    holdOff: new PayloadHoldOff(DECODER_HOLD_OFF_MS, {
      refreshWhileSuppressed: true,
      now: () => loop.clock,
    }),
    clock: 0,
  };
  return loop;
}

/**
 * Drive `frames` ticks of the real loop — the same three objects `useScanner`
 * builds, driven through the same two calls — against a stub camera in which
 * `visibleFrom` is the cheapest decode pass that can see the code at all.
 */
async function run(
  loop: Loop,
  visibleFrom: number,
  frames: number,
): Promise<{ readonly reported: string[]; readonly levels: number[] }> {
  const reported: string[] = [];
  const levels: number[] = [];
  for (let i = 0; i < frames; i += 1) {
    const attempt = await runEscalationAttempt(
      loop.escalation,
      (level) => Promise.resolve(level >= visibleFrom ? [symbol("CODE")] : null),
      () => 0,
    );
    levels.push(attempt.level);
    const admission = admitDecoded(loop.escalation, loop.voter, loop.holdOff, attempt.result ?? []);
    reported.push(...admission.report.map((found) => found.text));
    loop.clock += 100;
  }
  return { reported, levels };
}

describe("a code the cheap pass can read", () => {
  it("is reported on the second frame and never leaves the cheap pass", async () => {
    const loop = newLoop();
    const { reported, levels } = await run(loop, 0, 4);
    expect(reported).toEqual(["CODE"]);
    expect(levels).toEqual([0, 0, 0, 0]);
  });
});

describe("a code only the full-frame pass can read", () => {
  it("is reported, rather than sighted and thrown away", async () => {
    // The regression this module exists for. A QR the user did not centre is
    // invisible to the ROI pass. If the ladder descended on the first *sighting*,
    // the two frames after it would go to a pass that cannot see the code, its
    // single vote would fall out of the three-frame window, and it would never be
    // accepted — escalating would find codes and then discard them.
    const loop = newLoop();
    const { reported } = await run(loop, 1, 8);
    expect(reported).toEqual(["CODE"]);
  });

  it("returns to the cheap pass once it has been accepted", async () => {
    const loop = newLoop();
    const { levels } = await run(loop, 1, 8);
    // Two misses to climb, two frames at the full-frame pass to win the vote,
    // then straight back down to the cheap pass for the next label.
    expect(levels.slice(0, 4)).toEqual([0, 0, 1, 1]);
    expect(levels[4]).toBe(0);
  });
});

describe("a code only the expensive pass can read", () => {
  it("still gets there, one rung at a time", async () => {
    const loop = newLoop();
    const { reported, levels } = await run(loop, 2, 12);
    expect(reported).toEqual(["CODE"]);
    expect(Math.max(...levels)).toBe(2);
  });
});

describe("a label parked in front of the lens", () => {
  it("is reported once, not once per vote", async () => {
    // At ~10 fps a label held up for a second wins the vote repeatedly; the
    // hold-off is what makes that one resolve.
    const loop = newLoop();
    const { reported } = await run(loop, 0, 20);
    expect(reported).toEqual(["CODE"]);
  });

  it("can be read again after it has been out of frame for the window", async () => {
    const loop = newLoop();
    expect((await run(loop, 0, 2)).reported).toEqual(["CODE"]);
    loop.clock += DECODER_HOLD_OFF_MS + 1;
    expect((await run(loop, 0, 2)).reported).toEqual(["CODE"]);
  });
});

describe("one frame carrying two symbols", () => {
  it("reports both, and resets the ladder once", () => {
    const loop = newLoop();
    const frame = [symbol("DM", "DataMatrix"), symbol("C128", "Code128")];
    expect(admitDecoded(loop.escalation, loop.voter, loop.holdOff, frame).report).toEqual([]);

    const second = admitDecoded(loop.escalation, loop.voter, loop.holdOff, frame);
    expect(second.accepted).toBe(true);
    expect(second.report.map((found) => found.text).sort()).toEqual(["C128", "DM"]);
    // The symbology travels with the payload — a reel's DataMatrix and its
    // Code 128 must not be reported as the same kind of thing.
    expect(second.report.find((found) => found.text === "DM")?.symbology).toBe("DataMatrix");
  });
});

describe("a sighting that never becomes a vote", () => {
  it("is not an acceptance, and does not reset the ladder", () => {
    const loop = newLoop();
    loop.escalation.recordResult(false);
    loop.escalation.recordResult(false);
    expect(loop.escalation.level).toBe(1);

    const admission = admitDecoded(loop.escalation, loop.voter, loop.holdOff, [symbol("CODE")]);
    expect(admission.accepted).toBe(false);
    expect(admission.report).toEqual([]);
    expect(loop.escalation.level).toBe(1);
  });
});

describe("a frame that decoded nothing", () => {
  it("reports nothing and accepts nothing", () => {
    const loop = newLoop();
    const admission = admitDecoded(loop.escalation, loop.voter, loop.holdOff, []);
    expect(admission).toEqual({ report: [], accepted: false });
  });
});
