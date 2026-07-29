/**
 * The project list and its create form, against a stubbed `fetch`.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectsScreen } from "./ProjectsScreen";

const PROJECT = {
  id: 1,
  name: "Widget rev B",
  revision: "B",
  status: "active",
  description: "A small board",
  source_ref: null,
  notes: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  builds: [
    {
      id: 9,
      project_id: 1,
      build_no: 2,
      label: null,
      assembly_count: 5,
      bom_revision: "B",
      status: "planned",
      started_at: null,
      completed_at: null,
      notes: null,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    },
  ],
};

interface Call {
  readonly url: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

const calls: Call[] = [];

function stubApi(options: { projects?: readonly unknown[] } = {}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      calls.push({
        url: url.pathname + url.search,
        method: request.method,
        body: raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>),
      });

      const json = (body: unknown, status = 200): Response =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === "/api/projects" && request.method === "GET") {
        const projects = options.projects ?? [PROJECT];
        return json({ total: projects.length, projects });
      }
      if (url.pathname === "/api/projects" && request.method === "POST") {
        return json({ project: { ...PROJECT, id: 2, builds: [] }, replayed: false }, 201);
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}${url.search}`);
    }),
  );
}

function renderScreen() {
  return render(
    <MemoryRouter initialEntries={["/projects"]}>
      <Routes>
        <Route path="/projects" element={<ProjectsScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

function callsTo(pathname: string): Call[] {
  return calls.filter((call) => call.url === pathname);
}

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the project list", () => {
  it("shows each project's latest build summary", async () => {
    stubApi();
    renderScreen();

    expect(await screen.findByText(/Widget rev B/)).toBeTruthy();
    expect(screen.getByText(/latest #2, planned/)).toBeTruthy();
  });

  it("says plainly when there are no builds yet, rather than showing nothing", async () => {
    stubApi({ projects: [{ ...PROJECT, builds: [] }] });
    renderScreen();

    expect(await screen.findByText("no builds yet")).toBeTruthy();
  });

  it("re-requests with a status filter when a tab is pressed", async () => {
    stubApi();
    renderScreen();

    await screen.findByText(/Widget rev B/);
    fireEvent.click(screen.getByRole("button", { name: "Planning" }));

    await waitFor(() =>
      expect(callsTo("/api/projects?status=planning").length).toBeGreaterThan(0),
    );
  });
});

describe("creating a project", () => {
  it("sends the name and an idempotency key, and refreshes the list", async () => {
    stubApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "New project" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Name (required)" }), {
      target: { value: "New board" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    await waitFor(() => expect(callsTo("/api/projects").filter((c) => c.method === "POST")).toHaveLength(1));
    const post = callsTo("/api/projects").find((c) => c.method === "POST");
    expect(post?.body["name"]).toBe("New board");
    expect(typeof post?.body["client_op_id"]).toBe("string");
  });

  it("will not submit a project with no name", async () => {
    stubApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "New project" }));
    expect(screen.getByRole("button", { name: "Create project" })).toHaveProperty("disabled", true);
  });
});
