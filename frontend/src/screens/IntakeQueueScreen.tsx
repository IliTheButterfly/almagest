/**
 * The intake queue — the desk end of the fast path.
 *
 * Scanning parks a label in one tap; this is where a box of parked reels gets walked
 * through afterwards, with a keyboard, at whatever pace suits. Each entry keeps the
 * `client_op_id` minted at its scan, so creating the part from here is idempotent
 * against the scan that produced it.
 *
 * **The queue is stored in this browser.** `POST /api/intake/pending` is in the
 * design but not in the API yet, so the queue does not follow the user from the
 * phone that scanned to the desktop that curates. That is a real limitation and the
 * screen says so rather than letting someone discover it after scanning a box.
 */

import { useState, useSyncExternalStore } from "react";
import { Link } from "react-router-dom";

import { ErrorBanner, Notice } from "../components/Feedback";
import { bindScanAlias, createPart } from "../lib/api/client";
import { formatQty, formatTimestamp } from "../lib/format";
import { intakeQueue, type PendingScan } from "../lib/intake/queue";

function usePending(): readonly PendingScan[] {
  return useSyncExternalStore(
    (listener) => intakeQueue.subscribe(listener),
    () => intakeQueue.list(),
    () => [],
  );
}

export function IntakeQueueScreen() {
  const pending = usePending();

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <h1 style={{ flex: 1 }}>Intake queue</h1>
          <span className="badge">{pending.length}</span>
        </div>
        <p className="muted-note" style={{ margin: 0 }}>
          Labels parked at scan time, to be curated here. Held in this browser only —
          the server-side pending queue is not built yet, so what was scanned on the
          phone is curated on the phone.
        </p>
        {pending.length > 0 && (
          <button type="button" className="danger" onClick={() => intakeQueue.clear()}>
            Discard all {pending.length}
          </button>
        )}
      </div>

      {pending.length === 0 ? (
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
      ) : (
        <ul className="list">
          {pending.map((entry) => (
            <PendingRow key={entry.id} entry={entry} />
          ))}
        </ul>
      )}
    </div>
  );
}

function PendingRow({ entry }: { entry: PendingScan }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(entry.mpn ?? "");
  const [partKind, setPartKind] = useState("component");
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
          <label className="field">
            <span>Part kind</span>
            <input
              value={partKind}
              onChange={(event) => setPartKind(event.target.value)}
              autoComplete="off"
            />
          </label>
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
