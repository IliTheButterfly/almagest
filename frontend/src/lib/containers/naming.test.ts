/**
 * The naming-pattern preview, against the server's two rules.
 *
 * These are a restatement of `app.services.layout_authoring.instantiate`, not a
 * second policy: `{n}` is the only substitution, a count above one with no `{n}`
 * gets ` {n}` appended, and anything else in braces is refused with
 * `bad_naming_pattern` rather than guessed at. If this module and that function
 * ever disagree, the server wins and the form still shows its 422 — but the point
 * of the preview is that the disagreement should not happen.
 */

import { describe, expect, it } from "vitest";

import { namingProblem, previewNames, summariseNames } from "./naming";

describe("namingProblem", () => {
  it("accepts a plain name and a name with the one placeholder", () => {
    expect(namingProblem("Drawer")).toBeNull();
    expect(namingProblem("Drawer {n}")).toBeNull();
    expect(namingProblem("{n}")).toBeNull();
  });

  it("refuses any other placeholder, which is what the server does", () => {
    // "Cabinet {n} {oops}" was an uncaught KeyError and a bare 500 before the
    // route caught it; the honest answer is that the pattern is malformed.
    expect(namingProblem("Cabinet {n} {oops}")).toContain("Only {n}");
    expect(namingProblem("Cabinet {")).toContain("Only {n}");
    expect(namingProblem("Cabinet }")).toContain("Only {n}");
  });
});

describe("previewNames", () => {
  it("substitutes the 1-based index", () => {
    expect(previewNames("Drawer {n}", 3)).toEqual(["Drawer 1", "Drawer 2", "Drawer 3"]);
  });

  it("leaves a single container's name exactly as typed", () => {
    expect(previewNames("Bench cabinet", 1)).toEqual(["Bench cabinet"]);
  });

  it("appends a number when more than one is asked for without a placeholder", () => {
    // Otherwise thirty drawers all end up called the same thing, which is the
    // reason the server does this rather than refusing.
    expect(previewNames("Drawer", 2)).toEqual(["Drawer 1", "Drawer 2"]);
  });

  it("substitutes every occurrence, as str.format does", () => {
    expect(previewNames("{n} of {n}", 2)).toEqual(["1 of 1", "2 of 2"]);
  });

  it("previews nothing for a pattern the server would refuse", () => {
    expect(previewNames("Drawer {x}", 3)).toEqual([]);
  });
});

describe("summariseNames", () => {
  it("shows them all when there are few", () => {
    expect(summariseNames(["A", "B", "C"])).toBe("A, B, C");
  });

  it("shows the ends when there are many, so the last name is visible", () => {
    // The last one is the one worth checking: it is where an off-by-one in the
    // count shows up.
    expect(summariseNames(["A", "B", "C", "D"])).toBe("A, B, … D");
  });

  it("says nothing about an empty list", () => {
    expect(summariseNames([])).toBe("");
  });
});
