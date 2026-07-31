/**
 * "Which iteration did you take these for?" — ADR 0011's one new question.
 *
 * A project is a design; an iteration is a thing being built. Parts you have
 * physically picked up belong to the second, because that is what has a staging
 * location to put them in and an assembly count to measure them against. So a
 * take with a project focused does not attribute to the project — it asks this,
 * once, and from then on the chosen build is the focused tab and takes go
 * straight there.
 *
 * Two deliberate details:
 *
 * - **A closed build is not offered.** Staging into one is refused server-side
 *   (`build_closed`: nothing will ever consume or release the row), so listing it
 *   would be offering a button that cannot work. It is *said* rather than
 *   silently filtered, because "my build is not in this list" is otherwise a
 *   mystery.
 * - **Creating one is part of the question**, not a trip to another screen. The
 *   parts are in your hand; sending you elsewhere to plan a build first is how
 *   they end up on the bench instead of in the record.
 */

import { useState } from "react";

import { createBuild, getProject, type BuildRead } from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";
import { buildTarget, type WorkTarget } from "../lib/projectcontext/target";
import { uuid4 } from "../lib/scan/session";
import { ErrorBanner, Loading, Notice } from "./Feedback";

/** Builds that can still receive parts. Mirrors the server's own refusal. */
const CLOSED = new Set(["completed", "abandoned"]);

export function ChooseIteration({
  project,
  onPick,
  onCancel,
}: {
  project: Extract<WorkTarget, { kind: "project" }>;
  onPick: (target: WorkTarget) => void;
  onCancel: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const loaded = useAsync(() => getProject(project.projectId), [project.projectId]);

  const all: readonly BuildRead[] = loaded.data?.builds ?? [];
  const open = all.filter((build) => !CLOSED.has(build.status));
  const closed = all.length - open.length;

  async function start(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const created = await createBuild(project.projectId, {
        assembly_count: 1,
        client_op_id: uuid4(),
      });
      onPick(buildTarget(created.build));
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <div className="row">
        <h3 style={{ margin: 0, flex: 1, minWidth: 0 }}>Which iteration are these for?</h3>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>

      <p className="muted-note" style={{ margin: 0 }}>
        {project.label} is the design. Parts you have picked up go to the iteration you are
        building, which is what has somewhere to put them — nothing is written until you
        commit that tab.
      </p>

      <ErrorBanner error={loaded.error} fallback="That project's iterations could not be loaded." />
      <ErrorBanner error={error} fallback="That iteration could not be started." />

      {loaded.data === null && loaded.error === null ? (
        <Loading what="this project's iterations" />
      ) : (
        <ul className="list">
          {open.map((build) => (
            <li key={build.id}>
              <button
                type="button"
                className="list-item wide"
                onClick={() => {
                  onPick(buildTarget(build));
                }}
              >
                <div className="row">
                  <span className="title">
                    Build #{build.build_no}
                    {build.label === null ? "" : ` — ${build.label}`}
                  </span>
                  <span className="spacer" />
                  <span className="sub">
                    {build.assembly_count === 1
                      ? "1 assembly"
                      : `${build.assembly_count} assemblies`}
                  </span>
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}

      {loaded.data !== null && open.length === 0 && (
        <Notice kind="info">
          This project has no iteration to build yet. Starting one is the next step
          either way — the parts in your hand are for something.
        </Notice>
      )}

      {closed > 0 && (
        <p className="muted-note" style={{ margin: 0 }}>
          {closed === 1 ? "One iteration is" : `${closed} iterations are`} finished or
          abandoned and cannot take parts. Reopen one from its own page if that is where
          these belong.
        </p>
      )}

      <button type="button" className="primary wide" disabled={busy} onClick={() => void start()}>
        {busy ? "Starting…" : "Start a new iteration for these"}
      </button>
    </div>
  );
}
