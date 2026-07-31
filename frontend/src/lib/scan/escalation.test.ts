import { describe, expect, it } from "vitest";

import type { EscalationLevel } from "./escalation";
import { EscalationController, nextDelayMs, runEscalationAttempt } from "./escalation";

const LEVELS: readonly EscalationLevel[] = [
  { name: "roi", minIntervalMs: 100 },
  { name: "full-frame", minIntervalMs: 100 },
  { name: "hard", minIntervalMs: 500 },
];

describe("the escalation ladder", () => {
  it("starts on the cheapest rung", () => {
    const controller = new EscalationController(LEVELS);
    expect(controller.level).toBe(0);
    expect(controller.current.name).toBe("roi");
  });

  it("stays put until the miss threshold is reached", () => {
    const controller = new EscalationController(LEVELS);
    controller.recordResult(false);
    expect(controller.level).toBe(0);
    controller.recordResult(false);
    expect(controller.level).toBe(1);
  });

  it("does not accumulate misses across a hit", () => {
    // The label was found, so the next miss starts a fresh count. Without this,
    // a scanner that reads one code per second would drift up the ladder anyway.
    const controller = new EscalationController(LEVELS);
    controller.recordResult(false);
    controller.recordResult(true);
    controller.recordResult(false);
    expect(controller.level).toBe(0);
  });

  it("escalates again after a sighting that never became a vote", () => {
    // A hit holds the rung but does not make it permanent: two more misses and
    // the ladder carries on up.
    const controller = new EscalationController(LEVELS);
    controller.recordResult(false);
    controller.recordResult(false);
    expect(controller.level).toBe(1);
    controller.recordResult(true);
    controller.recordResult(false);
    controller.recordResult(false);
    expect(controller.level).toBe(2);
  });

  it("holds the rung that found something instead of dropping on first sight", () => {
    // The bug this rule exists to prevent: a QR the user did not centre is only
    // visible to the full-frame pass, and a payload needs two of three frames to
    // be accepted. Descending on the first sighting hands the next two frames to
    // the ROI pass, which cannot see it — so it is sighted, dropped, sighted,
    // dropped, and never once reaches a second vote. Escalating would find codes
    // and then throw them away.
    const controller = new EscalationController(LEVELS);
    controller.recordResult(false);
    controller.recordResult(false);
    expect(controller.current.name).toBe("full-frame");
    controller.recordResult(true);
    expect(controller.current.name).toBe("full-frame");
    controller.recordResult(true);
    expect(controller.current.name).toBe("full-frame");
  });

  it("drops straight to the bottom when the caller accepts a payload", () => {
    // Acceptance, not sighting, is where "a well-aimed label decodes at full
    // speed again" becomes true. `useScanner` calls `reset()` on a vote winner.
    const controller = new EscalationController(LEVELS);
    for (let i = 0; i < 6; i += 1) {
      controller.recordResult(false);
    }
    expect(controller.current.name).toBe("hard");
    controller.reset();
    expect(controller.level).toBe(0);
  });

  it("never climbs past the top rung", () => {
    const controller = new EscalationController(LEVELS);
    for (let i = 0; i < 40; i += 1) {
      controller.recordResult(false);
    }
    expect(controller.level).toBe(LEVELS.length - 1);
  });

  it("honours a custom miss threshold", () => {
    const controller = new EscalationController(LEVELS, { escalateAfter: 1 });
    controller.recordResult(false);
    expect(controller.level).toBe(1);
  });

  it("resets to the bottom", () => {
    const controller = new EscalationController(LEVELS);
    controller.recordResult(false);
    controller.recordResult(false);
    controller.reset();
    expect(controller.level).toBe(0);
  });

  it("refuses a ladder or a threshold that cannot work", () => {
    expect(() => new EscalationController([])).toThrow(RangeError);
    expect(() => new EscalationController(LEVELS, { escalateAfter: 0 })).toThrow(RangeError);
  });
});

describe("running one attempt", () => {
  it("reports the level it ran at, not the level it moved to", async () => {
    // The caller needs the level that produced this result so it can schedule
    // the next tick against the cadence that was actually paid for.
    const controller = new EscalationController(LEVELS, { escalateAfter: 1 });
    let clock = 0;
    const attempt = await runEscalationAttempt(
      controller,
      () => Promise.resolve(null),
      () => {
        clock += 7;
        return clock;
      },
    );
    expect(attempt.level).toBe(0);
    expect(attempt.levelName).toBe("roi");
    expect(attempt.elapsedMs).toBe(7);
    expect(controller.level).toBe(1);
  });

  it("hands the decode function the level index to run", async () => {
    const controller = new EscalationController(LEVELS, { escalateAfter: 1 });
    const seen: number[] = [];
    const decode = (level: number): Promise<string | null> => {
      seen.push(level);
      return Promise.resolve(null);
    };
    await runEscalationAttempt(controller, decode, () => 0);
    await runEscalationAttempt(controller, decode, () => 0);
    expect(seen).toEqual([0, 1]);
  });

  it("treats a result as a hit and feeds it back", async () => {
    const controller = new EscalationController(LEVELS, { escalateAfter: 1 });
    controller.recordResult(false);
    expect(controller.level).toBe(1);
    const attempt = await runEscalationAttempt(controller, () => Promise.resolve(["CODE"]), () => 0);
    expect(attempt.result).toEqual(["CODE"]);
    // Held, not descended: the rung that found this has to be given the chance
    // to find it again, or the vote never completes.
    expect(controller.level).toBe(1);
    // Still free to carry on up once it starts missing again.
    controller.recordResult(false);
    expect(controller.level).toBe(2);
  });

  it("counts an empty array as a hit, because only null means nothing found", async () => {
    // `decodeAtLevel` is documented as returning `null` for a miss; the decode
    // loop maps an empty symbol list to `null` itself. Asserting the boundary
    // here is what stops the two halves drifting apart.
    const controller = new EscalationController(LEVELS, { escalateAfter: 1 });
    await runEscalationAttempt(controller, () => Promise.resolve([]), () => 0);
    expect(controller.level).toBe(0);
  });
});

describe("the next tick's delay", () => {
  it("never runs faster than the level's own floor", () => {
    expect(nextDelayMs({ name: "hard", minIntervalMs: 500 }, 12)).toBe(500);
  });

  it("never queues a second pass behind a slow one", () => {
    // A phone that took 800 ms on the expensive pass gets 800 ms, not 500.
    expect(nextDelayMs({ name: "hard", minIntervalMs: 500 }, 800)).toBe(800);
  });
});
