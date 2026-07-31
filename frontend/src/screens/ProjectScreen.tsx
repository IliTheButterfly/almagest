/**
 * One project: its own fields, and every build ever planned against it.
 *
 * Builds are shown newest first, exactly as `ProjectRead.builds` already
 * orders them server-side — the same "definition plus its dependents" shape
 * `PartScreen` uses for lots, and for the same reason: the build list is read
 * here in one round trip rather than fetched per-row.
 *
 * `bom_revision` is copied onto a build at the instant it is planned, not a
 * live reference to `projects.revision` — so a build already in progress
 * keeps pointing at the BOM it was actually planned against even if the
 * project's revision field changes under it later. That is why a build's
 * badge here can show a revision the project card no longer does.
 */

import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import { PathBar } from "../components/PathBar";
import {
  createBuild,
  getProject,
  updateProject,
  type BuildCreate,
  type ProjectRead,
  type ProjectStatus,
  type ProjectUpdate,
} from "../lib/api/client";
import { formatTimestamp } from "../lib/format";
import { useAsync } from "../lib/hooks/useAsync";
import { uuid4 } from "../lib/scan/session";

export function ProjectScreen() {
  const { projectId: raw } = useParams();
  const projectId = Number(raw);
  const valid = Number.isSafeInteger(projectId) && projectId > 0;

  const project = useAsync<ProjectRead | null>(
    () => (valid ? getProject(projectId) : Promise.resolve(null)),
    [projectId, valid],
  );

  if (!valid) {
    return <Notice kind="error" title="That is not a project id" />;
  }
  if (project.error !== null) {
    return <ErrorBanner error={project.error} fallback="That project could not be loaded." />;
  }
  if (project.data === null) {
    return <Loading what="the project" />;
  }
  return <ProjectDetail project={project.data} onChanged={project.reload} />;
}

function BuildStatusBadge({ status }: { status: string }) {
  if (status === "completed") {
    return <span className="badge badge-good">completed</span>;
  }
  if (status === "abandoned") {
    return <span className="badge badge-bad">abandoned</span>;
  }
  // Neither is a colour-only distinction — the word carries it — so "planned"
  // and "in progress" stay the plain neutral badge rather than reaching for a
  // hue that would suggest a problem neither state has.
  return <span className="badge">{status === "in_progress" ? "in progress" : status}</span>;
}

function ProjectDetail({
  project,
  onChanged,
}: {
  project: ProjectRead;
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [planning, setPlanning] = useState(false);

  return (
    <div className="stack">
      <div className="card">
        <PathBar
          trail={[
            { key: "projects", label: "Projects", to: "/projects" },
            { key: `project-${project.id}`, label: project.name },
          ]}
          label="Project path"
        />
        <div className="row">
          <h1 style={{ flex: 1 }}>
            {project.name}
            {project.revision !== null && <span className="dim mono"> · {project.revision}</span>}
          </h1>
          <span className="badge">{project.status}</span>
        </div>
        {project.description !== null && <p style={{ margin: 0 }}>{project.description}</p>}
        {project.notes !== null && <p className="muted-note">{project.notes}</p>}
        <div className="row">
          <Link to={`/projects/${project.id}/bom`}>Bill of materials →</Link>
          <span className="spacer" />
          <button type="button" onClick={() => setEditing(!editing)}>
            {editing ? "Cancel" : "Edit"}
          </button>
        </div>
      </div>

      {editing && (
        <EditProject
          project={project}
          onDone={() => {
            setEditing(false);
            onChanged();
          }}
        />
      )}

      <div className="card">
        <div className="row">
          <h3 style={{ margin: 0, flex: 1 }}>Builds</h3>
          <button type="button" className="primary" onClick={() => setPlanning(!planning)}>
            {planning ? "Cancel" : "Plan a build"}
          </button>
        </div>

        {planning && (
          <NewBuild
            projectId={project.id}
            onDone={() => {
              setPlanning(false);
              onChanged();
            }}
          />
        )}

        {project.builds.length === 0 ? (
          <p className="dim">No builds planned yet.</p>
        ) : (
          <ul className="list">
            {project.builds.map((build) => (
              <li key={build.id}>
                <Link className="list-item" to={`/builds/${build.id}`}>
                  <div className="row">
                    <span className="title" style={{ flex: 1 }}>
                      Build #{build.build_no}
                      {build.label !== null && ` — ${build.label}`}
                    </span>
                    <BuildStatusBadge status={build.status} />
                  </div>
                  <div className="sub">
                    {build.assembly_count} assembl{build.assembly_count === 1 ? "y" : "ies"}
                    {build.bom_revision !== null && ` · BOM ${build.bom_revision}`}
                  </div>
                  <div className="sub">
                    {build.started_at !== null && `started ${formatTimestamp(build.started_at)}`}
                    {build.completed_at !== null &&
                      ` · closed ${formatTimestamp(build.completed_at)}`}
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function EditProject({
  project,
  onDone,
}: {
  project: ProjectRead;
  onDone: () => void;
}) {
  const [draft, setDraft] = useState<ProjectUpdate>({
    name: project.name,
    revision: project.revision,
    status: project.status as ProjectStatus,
    description: project.description,
    source_ref: project.source_ref,
    notes: project.notes,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function save(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      // Unguarded, like `parts.update_part`: replaying this sets the same
      // fields to the same values, so there is nothing a key would protect.
      await updateProject(project.id, draft);
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
      <h3>Edit project</h3>
      <label className="field">
        <span>Name</span>
        <input
          value={draft.name ?? ""}
          onChange={(event) => setDraft({ ...draft, name: event.target.value })}
        />
      </label>
      <label className="field">
        <span>Revision</span>
        <input
          value={draft.revision ?? ""}
          onChange={(event) => setDraft({ ...draft, revision: event.target.value })}
        />
      </label>
      <label className="field">
        <span>Status</span>
        <select
          value={draft.status ?? project.status}
          onChange={(event) =>
            setDraft({ ...draft, status: event.target.value as ProjectStatus })
          }
        >
          <option value="planning">Planning</option>
          <option value="active">Active</option>
          <option value="archived">Archived</option>
        </select>
      </label>
      <label className="field">
        <span>Description</span>
        <input
          value={draft.description ?? ""}
          onChange={(event) => setDraft({ ...draft, description: event.target.value })}
        />
      </label>
      <label className="field">
        <span>Notes</span>
        <textarea
          rows={3}
          value={draft.notes ?? ""}
          onChange={(event) => setDraft({ ...draft, notes: event.target.value })}
        />
      </label>
      <ErrorBanner error={error} fallback="Those changes were not saved." />
      <button type="submit" className="primary wide" disabled={busy}>
        {busy ? "Saving…" : "Save"}
      </button>
    </form>
  );
}

function NewBuild({ projectId, onDone }: { projectId: number; onDone: () => void }) {
  const [draft, setDraft] = useState<BuildCreate>({ assembly_count: 1 });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function save(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      // `build_no` and `bom_revision` are assigned server-side — never sent
      // from here — so two desks planning a build at once cannot collide.
      await createBuild(projectId, { ...draft, client_op_id: uuid4() });
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
      <label className="field">
        <span>Label</span>
        <input
          value={draft.label ?? ""}
          onChange={(event) => setDraft({ ...draft, label: event.target.value })}
          placeholder="prototype run"
        />
      </label>
      <label className="field">
        <span>Assemblies</span>
        <input
          type="number"
          min={1}
          value={draft.assembly_count ?? 1}
          onChange={(event) =>
            setDraft({ ...draft, assembly_count: Math.max(1, Number(event.target.value) || 1) })
          }
        />
      </label>
      <ErrorBanner error={error} fallback="That build could not be planned." />
      <button type="submit" className="primary wide" disabled={busy}>
        {busy ? "Planning…" : "Plan this build"}
      </button>
    </form>
  );
}
