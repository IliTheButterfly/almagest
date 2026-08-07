/**
 * The models panel must not overstate what is running.
 *
 * Every case here is a place where the convenient rendering and the honest one
 * differ: a pod that exists is not a model that can answer, a cluster nobody can
 * read is not a model that is off, and a server that is up does not mean every
 * model it holds was ever pulled.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ModelsPanel } from "./ModelsPanel";
import type { ModelServerList, ModelServerRead } from "../lib/api/client";

const { listModelServers, startModelServer, stopModelServer } = vi.hoisted(() => ({
  listModelServers: vi.fn(),
  startModelServer: vi.fn(),
  stopModelServer: vi.fn(),
}));

vi.mock("../lib/api/client", () => ({ listModelServers, startModelServer, stopModelServer }));

function server(overrides: Partial<ModelServerRead> = {}): ModelServerRead {
  return {
    id: "ollama",
    label: "Ollama — small and medium models",
    deployment: "almagest-llm",
    state: "stopped",
    desired_replicas: 0,
    ready_replicas: 0,
    holds_gpu: false,
    models: [
      { id: "qwen3-4b", label: "Qwen3 4B — fast", size_b: 4, loaded: false },
      { id: "qwen3-8b", label: "Qwen3 8B — balanced", size_b: 8, loaded: false },
    ],
    ...overrides,
  };
}

function list(overrides: Partial<ModelServerList> = {}): ModelServerList {
  return { servers: [server()], controllable: true, hint: null, ...overrides };
}

function draw() {
  return render(
    <MemoryRouter>
      <ModelsPanel />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  listModelServers.mockResolvedValue(list());
});

describe("what it says is running", () => {
  it("calls a loading model Starting, never Running", async () => {
    // A 27B binds its port minutes before it can answer. Green here is a lie that
    // costs somebody a question and a wait.
    listModelServers.mockResolvedValue(
      list({
        servers: [server({ state: "starting", desired_replicas: 1, ready_replicas: 0, holds_gpu: true })],
      }),
    );
    draw();

    expect(await screen.findByText("Starting")).toBeTruthy();
    expect(screen.queryByText("Running")).toBeNull();
    // Already holding the card, which is why the other one will not start.
    expect(screen.getByText("Holding the GPU")).toBeTruthy();
  });

  it("does not report an unreadable cluster as a stopped model", async () => {
    listModelServers.mockResolvedValue(
      list({
        servers: [server({ state: "unknown", desired_replicas: null, ready_replicas: null })],
      }),
    );
    draw();

    expect(await screen.findByText("Unknown")).toBeTruthy();
    expect(screen.getByText(/replica count unavailable/i)).toBeTruthy();
    expect(screen.queryByText("Stopped")).toBeNull();
  });

  it("marks each held model separately, because one server holds two", async () => {
    // The 8B was pulled, the 4B never was. With the server up, an "everything on
    // this server is available" reading would offer a model that 404s.
    listModelServers.mockResolvedValue(
      list({
        servers: [
          server({
            state: "running",
            desired_replicas: 1,
            ready_replicas: 1,
            holds_gpu: true,
            models: [
              { id: "qwen3-4b", label: "Qwen3 4B — fast", size_b: 4, loaded: false },
              { id: "qwen3-8b", label: "Qwen3 8B — balanced", size_b: 8, loaded: true },
            ],
          }),
        ],
      }),
    );
    draw();

    expect(await screen.findByText("Loaded")).toBeTruthy();
    expect(screen.getByText("Not loaded")).toBeTruthy();
  });
});

describe("starting and stopping", () => {
  it("renders the state the action returned, not the one before it", async () => {
    startModelServer.mockResolvedValue({
      ok: true,
      detail: "Ollama is starting.",
      released: [],
      servers: [server({ state: "starting", desired_replicas: 1, ready_replicas: 0, holds_gpu: true })],
    });
    draw();

    fireEvent.click(await screen.findByRole("button", { name: "Start" }));

    expect(startModelServer).toHaveBeenCalledWith("ollama");
    expect(await screen.findByText("Starting")).toBeTruthy();
    expect(screen.getByText("Ollama is starting.")).toBeTruthy();
  });

  it("says so when the release of the other model is part of the answer", async () => {
    startModelServer.mockResolvedValue({
      ok: true,
      detail: "vLLM is starting. The other model server is being stopped to free the GPU.",
      released: ["ollama"],
      servers: [server({ state: "stopped" })],
    });
    draw();

    fireEvent.click(await screen.findByRole("button", { name: "Start" }));
    expect(await screen.findByText(/free the GPU/)).toBeTruthy();
  });

  it("reports a refused start as a failure rather than a success", async () => {
    startModelServer.mockResolvedValue({
      ok: false,
      detail: "Ollama could not be started — the cluster refused the request.",
      released: [],
      servers: [server()],
    });
    draw();

    fireEvent.click(await screen.findByRole("button", { name: "Start" }));

    expect(await screen.findByText("Nothing changed")).toBeTruthy();
    expect(screen.queryByText("Working on it")).toBeNull();
  });

  it("stops a running server", async () => {
    listModelServers.mockResolvedValue(
      list({ servers: [server({ state: "running", desired_replicas: 1, ready_replicas: 1 })] }),
    );
    stopModelServer.mockResolvedValue({
      ok: true,
      detail: "Ollama is stopping.",
      released: ["ollama"],
      servers: [server()],
    });
    draw();

    fireEvent.click(await screen.findByRole("button", { name: "Stop" }));

    expect(stopModelServer).toHaveBeenCalledWith("ollama");
    expect(await screen.findByText("Stopped")).toBeTruthy();
  });

  it("does not offer Start for something already running, or Stop for something off", async () => {
    listModelServers.mockResolvedValue(
      list({ servers: [server({ state: "running", desired_replicas: 1, ready_replicas: 1 })] }),
    );
    draw();

    expect((await screen.findByRole<HTMLButtonElement>("button", { name: "Start" })).disabled).toBe(true);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Stop" }).disabled).toBe(false);
  });
});

describe("no cluster", () => {
  it("hides the buttons and names the command that works instead", async () => {
    // A disabled button with no explanation is how somebody decides the feature is
    // broken.
    listModelServers.mockResolvedValue(
      list({ controllable: false, hint: "make k8s-model M=8b|27b|off" }),
    );
    draw();

    expect(await screen.findByText("Read-only from here")).toBeTruthy();
    expect(screen.getByText("make k8s-model M=8b|27b|off")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Start" })).toBeNull();
  });

  it("still shows what is running", async () => {
    listModelServers.mockResolvedValue(
      list({
        controllable: false,
        hint: "make k8s-model M=8b|27b|off",
        servers: [server({ state: "running", desired_replicas: null, ready_replicas: null })],
      }),
    );
    draw();

    expect(await screen.findByText("Running")).toBeTruthy();
  });
});

describe("polling", () => {
  it("re-asks while a model is starting, and stops once it is up", async () => {
    vi.useFakeTimers();
    try {
      listModelServers.mockResolvedValue(
        list({ servers: [server({ state: "starting", desired_replicas: 1, ready_replicas: 0 })] }),
      );
      draw();
      // Waits for the *rendered* state, not just the call: the timer only starts
      // once "starting" has landed in state, so asserting on the call count would
      // race the commit that arms it.
      await vi.waitFor(() => expect(screen.getByText("Starting")).toBeTruthy());

      // Weights land: the next poll answers "running", and polling should stop.
      listModelServers.mockResolvedValue(
        list({ servers: [server({ state: "running", desired_replicas: 1, ready_replicas: 1 })] }),
      );
      await vi.advanceTimersByTimeAsync(5000);
      await vi.waitFor(() => expect(screen.getByText("Running")).toBeTruthy());
      expect(listModelServers).toHaveBeenCalledTimes(2);

      const settled = listModelServers.mock.calls.length;
      await vi.advanceTimersByTimeAsync(20000);
      expect(listModelServers).toHaveBeenCalledTimes(settled);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not poll a settled list at all", async () => {
    vi.useFakeTimers();
    try {
      listModelServers.mockResolvedValue(list());
      draw();
      await vi.waitFor(() => expect(listModelServers).toHaveBeenCalledTimes(1));
      await vi.advanceTimersByTimeAsync(30000);
      expect(listModelServers).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});

it("waits for the first load before drawing a state", async () => {
  listModelServers.mockReturnValue(new Promise(() => {}));
  draw();
  await waitFor(() => expect(listModelServers).toHaveBeenCalled());
  expect(screen.queryByText("Stopped")).toBeNull();
});
