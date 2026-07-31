/**
 * What a tab on the right-hand panel *is* — the thing a take gets attributed to.
 *
 * ADR 0010: a target is a **project or a build**, and the strip is heterogeneous
 * on purpose, because "finishing rev B" is a project and "kitting rev C" is a
 * build, and forcing one to be expressed as the other would mean inventing a
 * build nobody wanted.
 *
 * It is a discriminated union rather than `{type: string, id: number}` for one
 * reason that is worth the extra keystrokes: a build id read as a project id
 * would attribute stock to whichever project happens to share that number, and
 * that mistake is silent — the request succeeds. With `projectId` and `buildId`
 * as *different fields*, the mix-up is not merely unlikely, it does not typecheck.
 * `targetKey` inherits the same property: the two id spaces cannot collide in a
 * storage key because the kind is part of it.
 *
 * The label is a **capture**, exactly as the cart's part names are: it is what was
 * on screen when the tab was opened, refreshed whenever the target is opened
 * again. It is never compared — a renamed project is the same project.
 */

export type WorkTarget =
  | { readonly kind: "project"; readonly projectId: number; readonly label: string }
  | { readonly kind: "build"; readonly buildId: number; readonly label: string };

/**
 * The two constructors, so a target's captured label is written once.
 *
 * Three screens now open tabs — a project, a build, and the iteration chooser the
 * take screen puts up — and a build named "Build #2" in one place and "rev C" in
 * another is two tabs as far as a reader is concerned, even though `targetKey`
 * would tell them apart correctly. Structural parameters rather than the generated
 * `ProjectRead`/`BuildRead`: this module is deliberately free of API imports.
 */
export function projectTarget(project: {
  readonly id: number;
  readonly name: string;
}): WorkTarget {
  return { kind: "project", projectId: project.id, label: project.name };
}

export function buildTarget(build: {
  readonly id: number;
  readonly build_no: number;
  readonly label: string | null;
}): WorkTarget {
  return {
    kind: "build",
    buildId: build.id,
    label: `Build #${build.build_no}${build.label === null ? "" : ` — ${build.label}`}`,
  };
}

/** The tab's identity, and the suffix of its cart's `localStorage` key. */
export type TargetKey = string;

export function targetKey(target: WorkTarget): TargetKey {
  return target.kind === "project" ? `project.${target.projectId}` : `build.${target.buildId}`;
}

/** Same destination? The label is deliberately not part of the comparison. */
export function sameTarget(a: WorkTarget, b: WorkTarget): boolean {
  return targetKey(a) === targetKey(b);
}

/**
 * A stored target, or `null` when it cannot be read.
 *
 * An unrecognised `kind` — written by a newer build of the app, or hand-edited —
 * is dropped rather than guessed at. Losing a tab costs one tap; guessing it
 * wrong attributes stock to the wrong job.
 */
export function readTarget(value: unknown): WorkTarget | null {
  if (value === null || typeof value !== "object") {
    return null;
  }
  const record = value as Record<string, unknown>;
  const label = typeof record["label"] === "string" ? record["label"] : "";
  switch (record["kind"]) {
    case "project":
      return typeof record["projectId"] === "number"
        ? { kind: "project", projectId: record["projectId"], label }
        : null;
    case "build":
      return typeof record["buildId"] === "number"
        ? { kind: "build", buildId: record["buildId"], label }
        : null;
    default:
      return null;
  }
}
