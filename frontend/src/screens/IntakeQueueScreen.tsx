/**
 * The intake queue — the desk end of the fast path.
 *
 * Scanning parks a label in one tap; this is where a box of parked reels gets walked
 * through afterwards, with a keyboard, at whatever pace suits. Each entry keeps the
 * `client_op_id` minted at its scan, so creating the part from here is idempotent
 * against the scan that produced it.
 *
 * **`localStorage` is now a write-behind buffer, not the record.** Parking still
 * writes locally first, because the fast path has to work with no network — that is
 * most of its value. Sync then pushes to `POST /api/intake/pending`, after which the
 * queue follows the user from the phone that scanned to the desktop that curates.
 *
 * Both lists are shown, and shown *separately*, on purpose. Merging them into one
 * "queue" would hide the only thing worth knowing: whether a scan has left the
 * device yet. Something still local is one cleared cache from gone, and the user is
 * the only one who can decide to press Sync before walking away from the shelf.
 */

import { useState, useSyncExternalStore } from "react";
import { Link } from "react-router-dom";

import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import { CategorySelect } from "../components/CategorySelect";
import { PartKindPicker } from "../components/PartKindPicker";
import {
  bindScanAlias,
  createPart,
  dismissPendingIntake,
  listPendingIntake,
  resolvePendingIntake,
  type PendingIntakeList,
  type PendingIntakeRead,
} from "../lib/api/client";
import { formatQty, formatTimestamp } from "../lib/format";
import { useAsync } from "../lib/hooks/useAsync";
import { IntakeCapture } from "../components/IntakeCapture";
import { intakeQueue, type PendingScan } from "../lib/intake/queue";
import { syncIntakeQueue, type SyncOutcome } from "../lib/intake/sync";

function usePending(): readonly PendingScan[] {
  return useSyncExternalStore(
    (listener) => intakeQueue.subscribe(listener),
    () => intakeQueue.list(),
    () => [],
  );
}

/** Whether this device could not write the queue to disk. Same subscription the
 *  list uses, so the warning appears on the change that caused it. */
function useDegraded(): boolean {
  return useSyncExternalStore(
    (listener) => intakeQueue.subscribe(listener),
    () => intakeQueue.degraded,
    () => false,
  );
}

export function IntakeQueueScreen() {
  const local = usePending();
  const degraded = useDegraded();
  const [syncing, setSyncing] = useState(false);
  const [outcome, setOutcome] = useState<SyncOutcome | null>(null);
  const [reload, setReload] = useState(0);

  const server = useAsync<PendingIntakeList>(() => listPendingIntake(), [reload]);

  async function sync(): Promise<void> {
    setSyncing(true);
    setOutcome(null);
    try {
      setOutcome(await syncIntakeQueue());
    } finally {
      setSyncing(false);
      setReload((tick) => tick + 1);
    }
  }

  const onServer = server.data?.entries ?? [];

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <h1 style={{ flex: 1 }}>Intake queue</h1>
          <span className="badge">{local.length + onServer.length}</span>
        </div>
        {degraded && (
          <Notice kind="warn" title="These scans are only in this tab">
            <p style={{ margin: 0 }}>
              This device would not let the queue be saved to disk — storage is full, or
              the browser is in private mode. Scanning carries on, but closing the tab
              loses whatever has not been synced. Sync now.
            </p>
          </Notice>
        )}
        <p className="muted-note" style={{ margin: 0 }}>
          Labels parked at scan time, curated here. Parking writes to this device first
          so scanning never waits for the network; syncing sends them to the server,
          after which the queue follows you to any other device.
        </p>
        {/* Where resolved intake lands when auto-assignment ran out of options.
            Named here because that is where somebody notices the parts are not in
            the drawer they expected. */}
        <p className="muted-note" style={{ margin: 0 }}>
          Stock that could not be placed automatically waits in{" "}
          <Link to="/staging">staging</Link> until it is given a home.
        </p>

        <div className="row">
          <button
            type="button"
            className="primary"
            disabled={syncing || local.length === 0}
            onClick={() => void sync()}
          >
            {syncing
              ? "Syncing…"
              : local.length === 0
                ? "Nothing to sync"
                : `Sync ${local.length} to the server`}
          </button>
          <span className="spacer" />
          {local.length > 0 && (
            <button type="button" className="danger" onClick={() => intakeQueue.clear()}>
              Discard {local.length} unsynced
            </button>
          )}
        </div>

        {outcome !== null && <SyncReport outcome={outcome} />}
        <ErrorBanner error={server.error} fallback="The server queue could not be loaded." />
      </div>

      {local.length + onServer.length === 0 && !server.loading ? (
        <Notice kind="info" title="Nothing parked">
          <p style={{ margin: 0 }}>
            Scan a distributor label and tap <strong>Queue for later</strong> to park it
            here. That path exists so a box of reels can be scanned in under a minute
            with no forms — the forms happen here, afterwards.
          </p>
          <p style={{ margin: 0 }}>
            <Link to="/scan">Go to the scanner →</Link>
          </p>
        </Notice>
      ) : null}

      {/* Local and synced are listed separately rather than merged. The one thing
          worth knowing about a parked scan is whether it has left the device yet:
          something still local is one cleared cache from gone. */}
      {local.length > 0 && (
        <div className="card">
          <div className="row">
            <h3 style={{ margin: 0 }}>On this device only ({local.length})</h3>
            <span className="spacer" />
            <span className="badge badge-warn">not synced</span>
          </div>
          <p className="muted-note" style={{ margin: 0 }}>
            Not on the server yet, so clearing this browser&apos;s storage would lose
            them.
          </p>
          <ul className="list">
            {local.map((entry) => (
              <PendingRow key={entry.id} entry={entry} />
            ))}
          </ul>
        </div>
      )}

      {server.loading && onServer.length === 0 ? (
        <Loading what="the server queue" />
      ) : (
        onServer.length > 0 && (
          <div className="card">
            <div className="row">
              <h3 style={{ margin: 0 }}>On the server ({server.data?.pending_total ?? 0})</h3>
              <span className="spacer" />
              <span className="badge badge-good">synced</span>
            </div>
            <ul className="list">
              {onServer.map((entry) => (
                <ServerRow
                  key={entry.id}
                  entry={entry}
                  onChanged={() => setReload((tick) => tick + 1)}
                />
              ))}
            </ul>
          </div>
        )
      )}
    </div>
  );
}

/**
 * What sync did, itemised.
 *
 * Failures are listed individually rather than summarised as a count, because the
 * two kinds mean opposite things: a 4xx entry will never upload and needs a human,
 * while an unreachable server will clear on its own. "3 failed" hides that
 * difference, and hiding it is how a permanently stuck entry gets ignored forever.
 */
function SyncReport({ outcome }: { outcome: SyncOutcome }) {
  const moved = outcome.uploaded + outcome.alreadyThere;

  if (outcome.failed.length === 0) {
    return (
      <Notice kind="ok">
        {moved === 0
          ? "Nothing to send."
          : `${outcome.uploaded} sent to the server` +
            (outcome.alreadyThere === 0
              ? "."
              : `, ${outcome.alreadyThere} were already there (a previous sync got through).`)}
      </Notice>
    );
  }

  return (
    <Notice kind="warn" title={`${outcome.failed.length} could not be sent`}>
      {moved > 0 && <p style={{ margin: 0 }}>{moved} went up; these stayed here:</p>}
      <ul style={{ margin: "0.4rem 0 0", paddingLeft: "1.2rem" }}>
        {outcome.failed.map((failure) => (
          <li key={failure.id}>{failure.message}</li>
        ))}
      </ul>
    </Notice>
  );
}

/**
 * One entry that has made it to the server.
 *
 * Resolve and dismiss stay distinguishable here for the same reason they are in the
 * schema: a pile of dismissed unknowns is noise, a pile of resolved ones is a
 * vendor format worth writing a parser for.
 */
function ServerRow({ entry, onChanged }: { entry: PendingIntakeRead; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  /**
   * `partId` names what the entry *became*, which the schema keeps separate from
   * the `part_id` the resolver guessed at scan time — "the scan looked like
   * this" and "this is what it is" are different claims, and conflating them
   * turns a guess into a record.
   */
  async function act(what: "resolve" | "dismiss", partId?: number): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await (what === "resolve"
        ? resolvePendingIntake(entry.id, partId === undefined ? {} : { resolved_part_id: partId })
        : dismissPendingIntake(entry.id));
      onChanged();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="list-item">
      <div className="row">
        <span className="title mono" style={{ flex: 1, overflowWrap: "anywhere" }}>
          {entry.mpn ?? entry.supplier_part_number ?? entry.raw_payload}
        </span>
        {entry.decoded_kind !== null && <span className="badge">{entry.decoded_kind}</span>}
      </div>
      <div className="sub">
        {/* `queued_at` is the device's scan time and may be absent or wrong; the
            server's `created_at` is always there. Preferring the former is right —
            it is what the user remembers — with a fallback rather than a blank. */}
        {formatTimestamp(entry.queued_at ?? entry.created_at)}
        {entry.quantity_milli === null ? "" : ` · ${formatQty(entry.quantity_milli)}`}
        {entry.lot_code === null ? "" : ` · lot ${entry.lot_code}`}
      </div>
      <ErrorBanner error={error} fallback="That entry was not changed." />

      {/* The reason parking is worth anything: the desk pass gets the
          photograph, not just the payload. Collapsed, because loading and
          re-resolving every capture in a long queue would cost most where the
          queue is longest. */}
      {entry.capture_id !== null && entry.capture_id !== undefined && (
        <details>
          <summary>The picture, and what it says</summary>
          <IntakeCapture
            captureId={entry.capture_id}
            onCreated={(part) => void act("resolve", part.id)}
          />
        </details>
      )}

      <div className="row">
        <button type="button" disabled={busy} onClick={() => void act("resolve")}>
          Mark done
        </button>
        <span className="spacer" />
        <button type="button" disabled={busy} onClick={() => void act("dismiss")}>
          Not an intake
        </button>
      </div>
    </li>
  );
}

function PendingRow({ entry }: { entry: PendingScan }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(entry.mpn ?? "");
  const [partKind, setPartKind] = useState("component");
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [createdId, setCreatedId] = useState<number | null>(entry.partId);

  async function create(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const result = await createPart({
        name: name.trim() === "" ? entry.code.slice(0, 60) : name.trim(),
        part_kind: partKind.trim() === "" ? "component" : partKind.trim(),
        is_stub: true,
        ...(categoryId === null ? {} : { category_id: categoryId }),
        ...(entry.mpn === null ? {} : { mpn: entry.mpn }),
        // The key from the original scan. Curating the same parked label twice must
        // not fork the catalogue.
        client_op_id: entry.id,
      });
      setCreatedId(result.part.id);
      await bindScanAlias({
        code: entry.code,
        symbology: entry.symbology ?? "unknown",
        entity_type: "part",
        entity_pk: result.part.id,
        alias_kind: "whole_payload",
        ...(entry.quantityMilli === null ? {} : { hint_qty_milli: entry.quantityMilli }),
      });
      intakeQueue.remove(entry.id);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  const summary = entry.mpn ?? entry.supplierPartNumber ?? entry.code;

  return (
    <li className="list-item">
      <div className="row">
        <span className="title mono" style={{ flex: 1, overflowWrap: "anywhere" }}>
          {summary}
        </span>
        {entry.decodedKind !== null && <span className="badge mono">{entry.decodedKind}</span>}
      </div>
      <div className="sub">
        {formatTimestamp(new Date(entry.queuedAt).toISOString())}
        {entry.symbology === null ? "" : ` · ${entry.symbology}`}
        {entry.quantityMilli === null ? "" : ` · qty ${formatQty(entry.quantityMilli)}`}
        {entry.dateCode === null ? "" : ` · date ${entry.dateCode}`}
        {entry.lotCode === null ? "" : ` · lot ${entry.lotCode}`}
      </div>

      {createdId !== null && (
        <div className="sub">
          <Link to={`/parts/${createdId}`}>Part {createdId} →</Link>
        </div>
      )}

      <div className="row">
        <button type="button" onClick={() => setOpen(!open)}>
          {open ? "Close" : "Curate"}
        </button>
        <span className="spacer" />
        <button type="button" className="danger" onClick={() => intakeQueue.remove(entry.id)}>
          Discard
        </button>
      </div>

      {open && (
        <form
          className="stack"
          onSubmit={(event) => {
            event.preventDefault();
            void create();
          }}
        >
          <label className="field">
            <span>Name (the only required field)</span>
            <input value={name} onChange={(event) => setName(event.target.value)} />
          </label>
          <PartKindPicker value={partKind} onChange={setPartKind} />
          <CategorySelect value={categoryId} onChange={setCategoryId} />
          <details>
            <summary>Raw payload</summary>
            <p className="muted-note mono" style={{ overflowWrap: "anywhere" }}>
              {entry.code}
            </p>
          </details>
          <ErrorBanner error={error} fallback="That part was not created." />
          <button type="submit" className="primary wide" disabled={busy}>
            {busy ? "Creating…" : "Create as a stub part"}
          </button>
        </form>
      )}
    </li>
  );
}
