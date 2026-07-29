/**
 * A project's detail — its builds, and planning a new one — against a
 * stubbed `fetch`.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectScreen } from "./ProjectScreen";

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
      label: "Prototype",
      assembly_count: 5,
      bom_revision: "B",
      status: "abandoned",
      started_at: "2026-01-01T00:00:00Z",
      completed_at: "2026-01-02T00:00:00Z",
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

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      calls.push({
        url: url.pathname,
        method: request.method,
        body: raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>),
      });

      const json = (body: unknown, status = 200): Response =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === "/api/projects/1" && request.method === "GET") {
        return json(PROJECT);
      }
      if (url.pathname === "/api/projects/1/builds") {
        return json(
          {
            build: { ...PROJECT.builds[0], id: 10, build_no: 3, status: "planned", label: "Run 2" },
            replayed: false,
          },
          201,
        );
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderScreen() {
  return render(
    <MemoryRouter initialEntries={["/projects/1"]}>
      <Routes>
        <Route path="/projects/:projectId" element={<ProjectScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("build status", () => {
  it("shows an abandoned build distinctly from a plain planned one", async () => {
    stubApi();
    renderScreen();

    const badge = await screen.findByText("abandoned");
    expect(badge.className).toContain("badge-bad");
  });
});

describe("planning a new build", () => {
  it("lets the client set label and assembly count but never build_no or bom_revision", async () => {
    stubApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Plan a build" }));
    fireEvent.change(screen.getByPlaceholderText("prototype run"), {
      target: { value: "Run 2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Plan this build" }));

    await waitFor(() =>
      expect(calls.some((call) => call.url === "/api/projects/1/builds")).toBe(true),
    );
    const post = calls.find((call) => call.url === "/api/projects/1/builds");
    expect(post?.body).toEqual(
      expect.objectContaining({ label: "Run 2", assembly_count: 1 }),
    );
    expect(post?.body["build_no"]).toBeUndefined();
    expect(post?.body["bom_revision"]).toBeUndefined();
  });
});
