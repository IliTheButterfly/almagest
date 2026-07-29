/**
 * The tab strip, as state.
 *
 * These run against the real singletons and jsdom's `localStorage`, because that is
 * the coupling that matters: the store asks the cart registry how many uncommitted
 * lines a tab holds before it will close it, and stubbing that away would test the
 * refusal against a mock of the thing the refusal exists to protect.
 *
 * The subjects are ADR 0010's invariants: exactly one tab focused whenever any is
 * open, `focused === null` meaning "nothing open" and therefore "a take commits
 * immediately", persistence across a reload, and that closing a tab with
 * uncommitted lines refuses rather than discards.
 */

import { beforeEach, describe, expect, it } from "vitest";

import { carts } from "../cart/registry";
import { openTargets } from "./store";
import { targetKey, type WorkTarget } from "./target";

const PROJECT: WorkTarget = { kind: "project", projectId: 12, label: "Bench PSU" };
const BUILD: WorkTarget = { kind: "build", buildId: 5, label: "rev B ×3" };
const OTHER: WorkTarget = { kind: "project", projectId: 13, label: "Curve tracer" };

function line(partId = 42) {
  return { partId, partName: "GRM188R61A106KA73D", qtyMilli: 1_000, lotId: 7 };
}

beforeEach(() => {
  globalThis.localStorage.clear();
  carts.reset();
  openTargets.reset();
});

describe("opening targets", () => {
  it("starts with nothing open, which is the immediate-commit state", () => {
    expect(openTargets.open()).toEqual([]);
    expect(openTargets.focused).toBeNull();
  });

  it("opens a project and a build side by side, in the order opened", () => {
    openTargets.openTarget(PROJECT);
    openTargets.openTarget(BUILD);
    expect(openTargets.open()).toEqual([PROJECT, BUILD]);
  });

  it("focuses whatever was opened last", () => {
    openTargets.openTarget(PROJECT);
    openTargets.openTarget(BUILD);
    expect(openTargets.focused).toEqual(BUILD);
  });

  it("does not duplicate a target, and refreshes its captured label", () => {
    openTargets.openTarget(PROJECT);
    openTargets.openTarget(BUILD);
    openTargets.openTarget({ ...PROJECT, label: "Bench PSU rev C" });

    expect(openTargets.open()).toHaveLength(2);
    // Re-opened, not moved: order is order-of-opening and nothing reorders it.
    expect(openTargets.open()[0]?.label).toBe("Bench PSU rev C");
    expect(openTargets.focused).toEqual({ ...PROJECT, label: "Bench PSU rev C" });
  });

  it("cannot confuse a build with a project of the same number", () => {
    openTargets.openTarget({ kind: "project", projectId: 7, label: "project seven" });
    openTargets.openTarget({ kind: "build", buildId: 7, label: "build seven" });
    expect(openTargets.open()).toHaveLength(2);
  });

  it("moves focus without touching what is open", () => {
    openTargets.openTarget(PROJECT);
    openTargets.openTarget(BUILD);
    openTargets.focus(targetKey(PROJECT));
    expect(openTargets.focused).toEqual(PROJECT);
    expect(openTargets.open()).toEqual([PROJECT, BUILD]);
  });

  it("ignores a request to focus something that is not open", () => {
    openTargets.openTarget(PROJECT);
    openTargets.focus(targetKey(OTHER));
    expect(openTargets.focused).toEqual(PROJECT);
  });
});

describe("focus is never lost while a tab is open", () => {
  it("keeps exactly one focused tab after the focused one closes", () => {
    openTargets.openTarget(PROJECT);
    openTargets.openTarget(BUILD);
    openTargets.close(targetKey(BUILD));
    expect(openTargets.focused).toEqual(PROJECT);
  });

  it("goes back to nothing focused when the last tab closes", () => {
    openTargets.openTarget(PROJECT);
    openTargets.close(targetKey(PROJECT));
    expect(openTargets.open()).toEqual([]);
    expect(openTargets.focused).toBeNull();
  });

  it("repairs a stored focus that names a tab that is not there", () => {
    globalThis.localStorage.setItem(
      "almagest.opentargets.v1",
      JSON.stringify({ open: [PROJECT], focusedKey: "build.999" }),
    );
    openTargets.reset();
    expect(openTargets.focused).toEqual(PROJECT);
  });
});

describe("persistence", () => {
  it("survives a reload, which is the point of the walk to the shelf", () => {
    openTargets.openTarget(PROJECT);
    openTargets.openTarget(BUILD);
    openTargets.focus(targetKey(PROJECT));

    openTargets.reset();

    expect(openTargets.open()).toEqual([PROJECT, BUILD]);
    expect(openTargets.focused).toEqual(PROJECT);
  });

  it("drops a stored tab it cannot interpret rather than guessing one", () => {
    globalThis.localStorage.setItem(
      "almagest.opentargets.v1",
      JSON.stringify({
        open: [{ kind: "workshop", workshopId: 3 }, { kind: "project", label: "no id" }, BUILD],
        focusedKey: null,
      }),
    );
    openTargets.reset();
    expect(openTargets.open()).toEqual([BUILD]);
  });

  it("ignores corrupt stored data", () => {
    globalThis.localStorage.setItem("almagest.opentargets.v1", "{not json");
    openTargets.reset();
    expect(openTargets.open()).toEqual([]);
  });

  it("leaves no key behind once the last tab is closed", () => {
    openTargets.openTarget(PROJECT);
    openTargets.close(targetKey(PROJECT));
    expect(globalThis.localStorage.getItem("almagest.opentargets.v1")).toBeNull();
  });
});

describe("closing a tab that still holds lines", () => {
  it("refuses, and says how many, rather than discarding them", () => {
    openTargets.openTarget(PROJECT);
    carts.for(PROJECT).add(line());
    carts.for(PROJECT).add(line(43));

    const outcome = openTargets.close(targetKey(PROJECT));

    expect(outcome).toEqual({
      closed: false,
      reason: "uncommitted_lines",
      lines: 2,
      target: PROJECT,
    });
    expect(openTargets.open()).toEqual([PROJECT]);
    expect(carts.for(PROJECT).size).toBe(2);
  });

  it("closes and empties it only when the caller has asked and been told yes", () => {
    // Emptied as well as closed: rows left under a closed tab would come back the
    // next time it was opened, long after the user said to throw them away.
    openTargets.openTarget(PROJECT);
    carts.for(PROJECT).add(line());

    expect(openTargets.close(targetKey(PROJECT), { discardLines: true })).toEqual({ closed: true });
    expect(openTargets.open()).toEqual([]);
    expect(carts.for(PROJECT).size).toBe(0);
  });

  it("closes an empty tab without asking", () => {
    openTargets.openTarget(PROJECT);
    expect(openTargets.close(targetKey(PROJECT))).toEqual({ closed: true });
  });

  it("leaves the other tab's lines alone when one is discarded", () => {
    openTargets.openTarget(PROJECT);
    openTargets.openTarget(BUILD);
    carts.for(PROJECT).add(line());
    carts.for(BUILD).add(line(43));

    openTargets.close(targetKey(PROJECT), { discardLines: true });

    expect(carts.for(BUILD).size).toBe(1);
    expect(openTargets.focused).toEqual(BUILD);
  });
});

describe("subscribers", () => {
  it("are notified on every change", () => {
    let notifications = 0;
    const unsubscribe = openTargets.subscribe(() => {
      notifications += 1;
    });
    openTargets.openTarget(PROJECT);
    openTargets.focus(targetKey(PROJECT));
    openTargets.close(targetKey(PROJECT));
    unsubscribe();
    openTargets.openTarget(BUILD);
    expect(notifications).toBe(3);
  });
});
