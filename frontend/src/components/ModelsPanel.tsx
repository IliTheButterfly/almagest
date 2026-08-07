/**
 * Which models are running, and the switch for each.
 *
 * ## Why this screen exists at all
 *
 * The GPU is shared with everything else on the machine, so Almagest's model
 * servers default to zero replicas and a reaper scales them back down when chat
 * goes idle. That is the right default and it has one cost: **at any moment, most
 * of the model list is not running**, and until now the only way to find out was
 * to send a message and read the failure — or to leave the app and run
 * `make k8s-model` from a terminal.
 *
 * ## A server is the unit, not a model
 *
 * The small and medium models share one Ollama deployment. Starting either is the
 * same cluster action and stopping it takes both, so the control is per server and
 * says which models it holds. Offering a per-model switch would have to lie about
 * one of them.
 *
 * ## Three states, not two
 *
 * `starting` is a first-class state and the reason this polls: a 27B binds its
 * port and *then* spends minutes loading weights, so "the pod exists" and "you can
 * ask it something" are minutes apart. A two-state view would flip to Running and
 * then fail every question asked of it.
 *
 * ## It stays useful with no cluster
 *
 * On a dev box the API cannot scale anything. Then `controllable` is false: the
 * states still render (they come from asking the servers, which needs no cluster
 * rights) and the buttons are replaced by the command that does work there. A
 * disabled button with no explanation is how somebody concludes the feature is
 * broken.
 */

import { useCallback, useEffect, useState } from "react";

import {
  listModelServers,
  startModelServer,
  stopModelServer,
  type ModelServerList,
  type ModelServerRead,
} from "../lib/api/client";
import { ErrorBanner, Notice } from "./Feedback";

/** How often to re-ask while something is on its way up. */
const POLL_MS = 4000;

function StateBadge({ state }: { state: ModelServerRead["state"] }) {
  // `starting` is deliberately the warn colour rather than the good one: it means
  // "not yet", and a green pill next to a model that cannot answer is the exact
  // misreading this panel exists to prevent.
  const kind =
    state === "running"
      ? "badge-good"
      : state === "starting"
        ? "badge-warn"
        : state === "unknown"
          ? "badge-info"
          : "badge";
  const label =
    state === "running"
      ? "Running"
      : state === "starting"
        ? "Starting"
        : state === "stopped"
          ? "Stopped"
          : "Unknown";
  return <span className={`badge ${kind}`}>{label}</span>;
}

function Replicas({ server }: { server: ModelServerRead }) {
  if (server.desired_replicas === null) {
    // Not zero — nobody here can see the cluster. Saying "0 replicas" would be a
    // confident wrong answer.
    return <span className="dim">replica count unavailable from here</span>;
  }
  return (
    <span className="dim">
      {server.ready_replicas ?? 0} of {server.desired_replicas} ready
    </span>
  );
}

export function ModelsPanel() {
  const [list, setList] = useState<ModelServerList | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [detail, setDetail] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  const load = useCallback(async () => {
    try {
      setList(await listModelServers());
      setError(null);
    } catch (cause) {
      setError(cause);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll only while something is starting. A model loading weights is the one
  // state that changes on its own, and polling a settled list would put a cluster
  // read and two HTTP probes on a timer for no new information.
  const starting = (list?.servers ?? []).some((server) => server.state === "starting");
  useEffect(() => {
    if (!starting) {
      return;
    }
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [starting, load]);

  const act = async (serverId: string, what: "start" | "stop") => {
    setBusy(serverId);
    setDetail(null);
    try {
      const result = what === "start" ? await startModelServer(serverId) : await stopModelServer(serverId);
      // The reply carries the state after itself, so one round trip both acts and
      // refreshes. Re-fetching separately would show the state from before the
      // click about as often as not.
      setList((current) => (current === null ? current : { ...current, servers: result.servers }));
      setDetail(result.detail);
      setFailed(!result.ok);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="stack">
      <ErrorBanner error={error} fallback="Could not load the models." />

      {list !== null && !list.controllable && (
        <Notice kind="info" title="Read-only from here">
          <p style={{ margin: 0 }}>
            This install can see what is running but cannot change it — it is not
            running in the cluster, or has no permission to scale. From a terminal:{" "}
            <code className="mono">{list.hint}</code>
          </p>
        </Notice>
      )}

      {detail !== null && (
        <Notice kind={failed ? "warn" : "ok"} title={failed ? "Nothing changed" : "Working on it"}>
          <p style={{ margin: 0 }}>{detail}</p>
        </Notice>
      )}

      {(list?.servers ?? []).map((server) => (
        <div className="card stack" key={server.id}>
          <div className="row">
            <b>{server.label}</b>
            <StateBadge state={server.state} />
            {/* Which one has the card. At most one can, so this is also the answer
                to "why is the other one not coming up". */}
            {server.holds_gpu && <span className="badge badge-accent">Holding the GPU</span>}
            <span className="spacer" />
            <Replicas server={server} />
          </div>

          <ul className="list">
            {server.models.map((model) => (
              <li className="list-item" key={model.id}>
                {/* The row is inside the item rather than on it: `.list-item` sets
                    `display: block` and is declared after `.row`, so both classes
                    on one element loses the flex layout and the badge ends up
                    jammed against the label. */}
                <div className="row">
                  <span>{model.label}</span>
                  <span className="spacer" />
                  {/* Per model, because the two Ollama models share a server and a
                      model that was never pulled would 404 at generation time even
                      with its server up and healthy. */}
                  <span className={model.loaded ? "badge badge-good" : "badge"}>
                    {model.loaded ? "Loaded" : "Not loaded"}
                  </span>
                </div>
              </li>
            ))}
          </ul>

          {list?.controllable === true && (
            <div className="row">
              <button
                type="button"
                onClick={() => void act(server.id, "start")}
                disabled={busy !== null || server.state === "running"}
              >
                Start
              </button>
              <button
                type="button"
                onClick={() => void act(server.id, "stop")}
                disabled={busy !== null || server.state === "stopped"}
              >
                Stop
              </button>
              <span className="spacer" />
              {server.deployment !== null && <span className="mono dim">{server.deployment}</span>}
            </div>
          )}
        </div>
      ))}

      <p className="muted-note">
        There is one graphics card, so starting a model stops the other one. A large
        model takes a few minutes to load its weights before it can answer, and the
        idle reaper stops whichever is running once nobody has asked anything for a
        while — which is why most of this list is usually stopped.
      </p>
    </div>
  );
}
