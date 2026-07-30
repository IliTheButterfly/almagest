/**
 * Draining the INBOX — the flow that was missing rather than broken.
 *
 * `INBOX` is where auto-assignment puts stock when every rung of the escalation
 * ladder is exhausted, because a rejected put-away teaches the user to stop
 * scanning (CLAUDE.md, "Capacity is advisory and a scan is never rejected"). That
 * is deliberate, and it leaves a debt nothing was paying: intake put lots in
 * staging and no screen moved them onward, so the only way out was to know a
 * container's numeric row id.
 *
 * So this is a worklist, not a new mechanism: `POST /api/stock/lots/{id}/move`
 * already exists and is the right primitive. What it adds is **one destination for
 * many lots** — pick the drawer once, tick the lots that belong in it, move them in
 * one pass. Somebody standing at a bench with a handful of parts sorts by
 * destination, not by lot.
 *
 * Each lot is its own request with its own idempotency key, and **a lot that fails
 * fails alone**: the others still move and the failure is named. That is the rule
 * the batch movement endpoint already follows, and the opposite — losing eleven
 * successful moves because the twelfth lot had been emptied by somebody else — is
 * how a screen like this stops being trusted.
 *
 * A **project's** staging box is not drained here. It carries the same `is_staging`
 * flag but is a deliberate holding place for a board's parts (ADR 0004), so it is
 * counted and pointed at rather than offered up for emptying — see
 * `lib/locations/staging` for why the two are told apart by `is_placeable`.
 */

import { useState } from "react";
import { Link } from "react-router-dom";

import { ContainerPicker, type PickedContainer } from "../components/ContainerPicker";
import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import {
  getLocation,
  getLocationTree,
  getPart,
  moveLot,
  type LocationRead,
} from "../lib/api/client";
import { formatQty } from "../lib/format";
import { useAsync } from "../lib/hooks/useAsync";
import { isInbox, isProjectStagingBox } from "../lib/locations/staging";
import { uuid4 } from "../lib/scan/session";

interface StagingContents {
  /** Inbox-kind staging locations, contents included. */
  readonly bins: readonly LocationRead[];
  /** How many project boxes exist, so the screen can say it is not ignoring them. */
  readonly projectBoxes: number;
  /** Part names for everything in those bins, so a row is not "part 41". */
  readonly partNames: ReadonlyMap<number, string>;
}

/**
 * One tree request, then one request per staging bin, then one per distinct part.
 *
 * Bounded by what is *in staging*, which is a handful by construction — the INBOX
 * is the exception path. `LotRead` carries `part_id` but no name, and a row that
 * says "part 41" cannot be sorted by hand, so the names are worth the fan-out.
 */
async function loadStaging(): Promise<StagingContents> {
  const tree = await getLocationTree();
  const projectBoxes = tree.nodes.filter(isProjectStagingBox).length;
  const inboxes = tree.nodes.filter(isInbox);
  const bins = await Promise.all(inboxes.map((node) => getLocation(node.id)));
  const partIds = [...new Set(bins.flatMap((bin) => bin.lots.map((lot) => lot.part_id)))];
  const parts = await Promise.all(
    partIds.map(async (id) => {
      try {
        return [id, (await getPart(id)).name] as const;
      } catch {
        // A part that will not load must not blank the worklist; the lot is still
        // movable, and "part 41" is a worse row than a name but a better one than
        // an error screen.
        return [id, `part ${id}`] as const;
      }
    }),
  );
  return { bins, projectBoxes, partNames: new Map(parts) };
}

export function StagingScreen() {
  const staging = useAsync<StagingContents>(loadStaging, []);

  if (staging.error !== null) {
    return <ErrorBanner error={staging.error} fallback="Staging could not be loaded." />;
  }
  if (staging.data === null) {
    return <Loading what="what is in staging" />;
  }
  return <Drain contents={staging.data} onMoved={staging.reload} />;
}

interface Outcome {
  readonly moved: number;
  readonly destination: string;
  readonly failures: readonly string[];
}

function Drain({
  contents,
  onMoved,
}: {
  contents: StagingContents;
  onMoved: () => void;
}) {
  const [chosen, setChosen] = useState<ReadonlySet<number>>(() => new Set());
  const [destination, setDestination] = useState<PickedContainer | null>(null);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  const lots = contents.bins.flatMap((bin) => bin.lots.map((lot) => ({ lot, bin })));
  const selected = lots.filter(({ lot }) => chosen.has(lot.id));

  function toggle(lotId: number): void {
    const next = new Set(chosen);
    if (next.has(lotId)) {
      next.delete(lotId);
    } else {
      next.add(lotId);
    }
    setChosen(next);
    setOutcome(null);
  }

  async function move(): Promise<void> {
    if (destination === null || selected.length === 0) {
      return;
    }
    setBusy(true);
    setOutcome(null);
    const failures: string[] = [];
    let moved = 0;
    for (const { lot } of selected) {
      try {
        await moveLot(lot.id, {
          to_location_id: destination.id,
          // Per lot, not per press: two lots in one pass are two movements, and one
          // key across both would make the server replay the first lot's answer for
          // the second.
          client_op_id: uuid4(),
          source: "manual",
        });
        moved += 1;
      } catch (cause) {
        failures.push(
          `lot ${lot.id} (${cause instanceof Error ? cause.message : "could not be moved"})`,
        );
      }
    }
    setOutcome({ moved, destination: destination.label, failures });
    setChosen(new Set());
    setBusy(false);
    onMoved();
  }

  return (
    <div className="stack">
      <div className="card">
        <h1>Staging</h1>
        <p className="muted-note" style={{ margin: 0 }}>
          Stock that landed in the inbox because auto-assignment ran out of options —
          nothing here is lost, it just has no home yet. Give it one.
        </p>
        {contents.projectBoxes > 0 && (
          <p className="muted-note" style={{ margin: 0 }}>
            {contents.projectBoxes} project parts box(es) also carry the staging flag and
            are deliberately left alone: those parts were set aside for a build on
            purpose. Put them back from the <Link to="/projects">build&apos;s roster</Link>.
          </p>
        )}
      </div>

      {outcome !== null && (
        <Notice
          kind={outcome.failures.length === 0 ? "ok" : "warn"}
          title={`Moved ${outcome.moved} lot(s) to ${outcome.destination}`}
        >
          {outcome.failures.length === 0 ? (
            <p style={{ margin: 0 }}>Staging is that much emptier.</p>
          ) : (
            <p style={{ margin: 0 }}>
              {outcome.failures.length} could not move, and the rest did:{" "}
              {outcome.failures.join(", ")}.
            </p>
          )}
        </Notice>
      )}

      {lots.length === 0 ? (
        <div className="card">
          <Notice kind="ok" title="Nothing is in staging">
            <p style={{ margin: 0 }}>
              Every lot has a real home. <Link to="/tree">Storage</Link> shows where.
            </p>
          </Notice>
        </div>
      ) : (
        <>
          <div className="card">
            <div className="row">
              <h3 style={{ margin: 0 }}>Waiting for a home ({lots.length})</h3>
              <span className="spacer" />
              <button
                type="button"
                onClick={() => {
                  setOutcome(null);
                  setChosen(
                    chosen.size === lots.length
                      ? new Set()
                      : new Set(lots.map(({ lot }) => lot.id)),
                  );
                }}
              >
                {chosen.size === lots.length ? "Select none" : "Select all"}
              </button>
            </div>
            <ul className="list">
              {lots.map(({ lot, bin }) => (
                <li key={lot.id} className="list-item">
                  <div className="row">
                    <label className="row-tight" style={{ flex: 1, minWidth: 0 }}>
                      <input
                        type="checkbox"
                        checked={chosen.has(lot.id)}
                        onChange={() => toggle(lot.id)}
                      />
                      <span style={{ minWidth: 0 }}>
                        <div className="title">
                          {contents.partNames.get(lot.part_id) ?? `part ${lot.part_id}`}
                        </div>
                        <div className="sub">
                          {formatQty(lot.qty_milli)}
                          {lot.batch_code === null || lot.batch_code === undefined
                            ? ""
                            : ` · batch ${lot.batch_code}`}
                          {contents.bins.length > 1 ? ` · in ${bin.name}` : ""}
                        </div>
                      </span>
                    </label>
                    <Link to={`/lots/${lot.id}`}>Open</Link>
                  </div>
                </li>
              ))}
            </ul>
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>Send them to</h3>
            <p className="muted-note" style={{ margin: 0 }}>
              {selected.length === 0
                ? "Tick the lots that belong in the same container first."
                : `${selected.length} lot(s) selected. One destination for all of them — ` +
                  "sort by drawer, not by lot."}
            </p>
            {/* Every staging bin is excluded: "send it back where it already is"
                is not a destination, it is the state being drained. */}
            <ContainerPicker
              onPick={setDestination}
              pickedId={destination?.id ?? null}
              excludeIds={contents.bins.map((bin) => bin.id)}
              actionLabel="Send here"
            />
            <button
              type="button"
              className="primary wide tall"
              disabled={busy || destination === null || selected.length === 0}
              onClick={() => void move()}
            >
              {busy
                ? "Moving…"
                : destination === null
                  ? "Choose where they go"
                  : `Move ${selected.length} lot(s) to ${destination.label}`}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
