/**
 * Projects: the list, and the one form that starts a new one.
 *
 * A project is a board design, not a build of it — `ProjectStatus` is
 * deliberately silent about whether anything has been assembled, since that
 * lives per-build on `BuildStatus`. `ARCHIVED` still shows here rather than
 * being deleted: archiving keeps the BOM, which is the answer to "what was in
 * that board" for as long as anyone might ask.
 */

import { useState } from "react";
import { Link } from "react-router-dom";

import { ErrorBanner, Empty, Loading } from "../components/Feedback";
import {
  createProject,
  listProjects,
  type ProjectCreate,
  type ProjectList,
  type ProjectStatus,
} from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";
import { uuid4 } from "../lib/scan/session";

const STATUS_FILTERS: readonly (ProjectStatus | "all")[] = ["all", "planning", "active", "archived"];

export function ProjectsScreen() {
  const [statusFilter, setStatusFilter] = useState<ProjectStatus | "all">("all");
  const [creating, setCreating] = useState(false);

  const projects = useAsync<ProjectList>(
    () => listProjects(statusFilter === "all" ? {} : { status: [statusFilter] }),
    [statusFilter],
  );

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <h1 style={{ flex: 1 }}>Projects</h1>
          <button type="button" className="primary" onClick={() => setCreating(!creating)}>
            {creating ? "Cancel" : "New project"}
          </button>
        </div>
        <div className="segmented" role="group" aria-label="Status filter">
          {STATUS_FILTERS.map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={statusFilter === value}
              onClick={() => setStatusFilter(value)}
            >
              {value === "all" ? "All" : value.charAt(0).toUpperCase() + value.slice(1)}
            </button>
          ))}
        </div>
      </div>

      {creating && (
        <NewProject
          onDone={() => {
            setCreating(false);
            projects.reload();
          }}
        />
      )}

      <ErrorBanner error={projects.error} fallback="The project list could not be loaded." />
      {projects.data === null ? (
        <Loading what="projects" />
      ) : projects.data.projects.length === 0 ? (
        <Empty>
          {statusFilter === "all"
            ? "No projects yet. Start one above."
            : `No ${statusFilter} projects.`}
        </Empty>
      ) : (
        <ul className="list">
          {projects.data.projects.map((project) => (
            <li key={project.id}>
              <Link className="list-item" to={`/projects/${project.id}`}>
                <div className="row">
                  <span className="title" style={{ flex: 1 }}>
                    {project.name}
                    {project.revision !== null && (
                      <span className="dim mono"> · {project.revision}</span>
                    )}
                  </span>
                  <span className="badge">{project.status}</span>
                </div>
                <div className="sub">
                  {project.builds.length === 0
                    ? "no builds yet"
                    : `${project.builds.length} build(s) — latest #${project.builds[0]?.build_no}, ${project.builds[0]?.status}`}
                </div>
                {project.description !== null && <div className="sub">{project.description}</div>}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function NewProject({ onDone }: { onDone: () => void }) {
  const [draft, setDraft] = useState<ProjectCreate>({ name: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function save(): Promise<void> {
    if (draft.name.trim() === "") {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      // Idempotency guards a doubled tap on a bad connection from filing the
      // same board twice — see `client.createProject`.
      await createProject({ ...draft, client_op_id: uuid4() });
      onDone();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="card"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <h3>New project</h3>
      <label className="field">
        <span>Name (required)</span>
        <input
          value={draft.name}
          onChange={(event) => setDraft({ ...draft, name: event.target.value })}
          autoFocus
        />
      </label>
      <label className="field">
        <span>Revision</span>
        <input
          value={draft.revision ?? ""}
          onChange={(event) => setDraft({ ...draft, revision: event.target.value })}
          placeholder="rev B"
        />
      </label>
      <label className="field">
        <span>Description</span>
        <input
          value={draft.description ?? ""}
          onChange={(event) => setDraft({ ...draft, description: event.target.value })}
        />
      </label>
      <ErrorBanner error={error} fallback="That project could not be created." />
      <button type="submit" className="primary wide" disabled={busy || draft.name.trim() === ""}>
        {busy ? "Creating…" : "Create project"}
      </button>
    </form>
  );
}
