/**
 * One stop on the pick walk, done at the desk with the drawer in your hand.
 *
 * > *I look at the list and go grab all the containers I need. I sit down at the
 * > desk, scan the first container. A confirmation or error message shows if I got
 * > the right container. I then select how many I need to take. [...] and I get a
 * > confirmation on if I took the right amount. Then I keep going through the
 * > list.*
 *
 * Three things that ordering forces, none of them cosmetic:
 *
 * - **The scan comes before the quantity, and gates it.** Not because a wrong
 *   container should be blocked — CLAUDE.md is explicit that a scan is never
 *   rejected, and the take stays possible — but because a quantity entered before
 *   the check is a number the user has already committed to mentally. Asking
 *   "which drawer is this?" afterwards gets the answer "the right one".
 * - **The quantity is pre-filled with what the list asked for**, so the common
 *   case is one press. A pick is a walk against a plan, and the plan already
 *   knows the number.
 * - **The confirmation says what was taken *and* what was wanted.** "Took 40" is
 *   not checkable; "took 40 of 40" is, and "took 38 of 40, 2 short" is the case
 *   the whole flow exists to surface.
 *
 * **The optical counter is offered only where it exists.** Vision counting is a
 * later phase and the station's camera is not built (ADR 0003), so the affordance
 * is rendered as unavailable with the reason, never as a button that does nothing.
 * Overstating what the hardware can do is the specific failure CLAUDE.md's
 * "honest capability limits" section is about.
 *
 * **A picked take stages; it does not consume.** ADR 0011: a take is a
 * *withdrawal*, and the honest record of parts that have physically left a drawer
 * is a ledger move into the build's staging location. Consuming here would claim
 * they had been soldered down the moment they were picked up, and reserving them
 * would leave `stock_lots.qty_milli_cached` insisting they are still in the
 * drawer — the exact lie ADR 0004's staging location exists to prevent. What is
 * built in gets recorded later, from the build screen.
 */

import { useState } from "react";
import { Link } from "react-router-dom";

import { ConfirmScan, type ScanVerdict } from "./ConfirmScan";
import { ErrorBanner, Notice } from "./Feedback";
import { QuantityPad } from "./Quantity";
import { stageStock, type PickStopRead, type PickTakeRead } from "../lib/api/client";
import { formatQty } from "../lib/format";
import { uuid4 } from "../lib/scan/session";
import type { TagSource } from "../lib/tags/source";

/**
 * Whether a part can be counted optically right now, and why not when it cannot.
 *
 * One place to change when the counting tray lands, rather than a `false`
 * scattered through the UI. It returns a *reason* and not a boolean because the
 * reason is what the screen shows: "no camera is attached" and "an 0402 is below
 * the noise floor and cannot be counted by any method here" are different
 * sentences, and the second one will still be true after the hardware exists.
 */
export function opticalCountUnavailable(): string {
  return (
    "No counting camera is attached to this machine. When the station's tray " +
    "exists it will offer a count here; until then the number is yours."
  );
}

interface TakeState {
  readonly qtyMilli: number;
  readonly tookMilli: number | null;
  readonly error: unknown;
  readonly busy: boolean;
}

export function GuidedPickStop({
  stop,
  index,
  buildId,
  source,
  onPicked,
}: {
  stop: PickStopRead;
  index: number;
  /** Whose staging location the picked parts move into. */
  buildId: number;
  /** Injected by tests; production uses every reader at once. */
  source?: TagSource;
  onPicked?: () => void;
}) {
  const [verdict, setVerdict] = useState<ScanVerdict>({ kind: "idle" });
  const [open, setOpen] = useState(false);

  const confirmed = verdict.kind === "right";

  return (
    <li>
      <div className="list-item">
        <div className="row">
          <span className="title" style={{ flex: 1 }}>
            {index}. <Link to={`/locations/${stop.location_id}`}>{stop.label_path}</Link>
          </span>
          <span className="big-number">{formatQty(stop.qty_milli)}</span>
        </div>
        {stop.short_id !== null && <div className="sub mono">{stop.short_id}</div>}

        {/* **The stop stays readable without pressing anything.** The list is
            walked before it is worked: you read it, cross the room, gather the
            drawers, and only then sit down. Folding the contents behind a
            disclosure would turn a walking list into a list you have to expand
            once per stop while holding two drawers. */}
        <ul className="list" style={{ marginTop: "0.4rem" }}>
          {stop.takes.map((take) => (
            <TakeRow
              key={`${take.lot_id}-${take.bom_line_id ?? "none"}-${take.part_id}`}
              take={take}
              buildId={buildId}
              open={open}
              confirmed={confirmed}
              {...(onPicked === undefined ? {} : { onPicked })}
            />
          ))}
        </ul>

        {!open ? (
          <button type="button" onClick={() => setOpen(true)}>
            Pick this stop…
          </button>
        ) : (
          <ConfirmScan
            expected={{
              locationId: stop.location_id,
              labelPath: stop.label_path,
              shortId: stop.short_id,
            }}
            onConfirmed={setVerdict}
            {...(source === undefined ? {} : { source })}
          />
        )}

        {/* Shown whatever the verdict, and that is the point: a scan is never a
            gate that traps someone holding the right parts and the wrong record.
            An unconfirmed take just says so. */}
        {open && !confirmed && (
          <Notice kind="info" title="Not confirmed yet">
            <p style={{ margin: 0 }}>
              Scan the drawer to check it before counting. You can record a take without
              scanning — it is simply not checked against anything.
            </p>
          </Notice>
        )}
      </div>
    </li>
  );
}

function TakeRow({
  take,
  buildId,
  open,
  confirmed,
  onPicked,
}: {
  take: PickTakeRead;
  buildId: number;
  /** Whether this stop is being worked, as opposed to read on the way past. */
  open: boolean;
  confirmed: boolean;
  onPicked?: () => void;
}) {
  const [state, setState] = useState<TakeState>({
    // Pre-filled with what the plan asked for: the common case is one press.
    qtyMilli: take.qty_milli,
    tookMilli: null,
    error: null,
    busy: false,
  });

  async function record(): Promise<void> {
    setState((previous) => ({ ...previous, busy: true, error: null }));
    try {
      await stageStock(buildId, {
        lot_id: take.lot_id,
        qty_milli: state.qtyMilli,
        ...(take.bom_line_id === null ? {} : { bom_line_id: take.bom_line_id }),
        // Minted per take and never reused: a key is spent on use, and replaying
        // one returns the first take's numbers while looking like a second
        // successful take — which silently loses stock.
        client_op_id: uuid4(),
      });
      setState((previous) => ({ ...previous, busy: false, tookMilli: previous.qtyMilli }));
      onPicked?.();
    } catch (cause) {
      setState((previous) => ({ ...previous, busy: false, error: cause }));
    }
  }

  const short = state.tookMilli !== null && state.tookMilli < take.qty_milli;
  const over = state.tookMilli !== null && state.tookMilli > take.qty_milli;

  return (
    <li className="sub">
      <div className="row">
        <span style={{ flex: 1 }}>
          <strong>{formatQty(take.qty_milli)}</strong> of{" "}
          <Link to={`/parts/${take.part_id}`}>{take.part_name}</Link>
          {take.part_mpn !== null && ` (${take.part_mpn})`}
        </span>
        {/* Two badges that change what the hand does, so both are words rather
            than a colour: an emptied lot is carried away whole with no count to
            get wrong, and a substitute is a decision somebody made once that is
            now being acted on blind at a drawer. */}
        {take.whole_lot && <span className="badge badge-good">whole lot</span>}
        {take.is_substitute && <span className="badge badge-info">substitute</span>}
        {take.allocation_id !== null && <span className="badge">already held</span>}
      </div>
      {take.line_no !== null && (
        <div className="sub">
          from <Link to={`/lots/${take.lot_id}`}>lot #{take.lot_id}</Link> · line {take.line_no}
          {take.designators !== null && ` · ${take.designators}`}
        </div>
      )}


      {!open ? null : state.tookMilli === null ? (
        <>
          <QuantityPad
            valueMilli={state.qtyMilli}
            onChange={(next) => setState((previous) => ({ ...previous, qtyMilli: next }))}
          />
          <div className="row">
            <button
              type="button"
              className="primary"
              disabled={state.busy}
              onClick={() => void record()}
            >
              {state.busy ? "Recording…" : `Take ${formatQty(state.qtyMilli)}`}
            </button>
            <button type="button" disabled title={opticalCountUnavailable()}>
              Count optically
            </button>
          </div>
          <p className="muted-note" style={{ margin: 0 }}>
            {opticalCountUnavailable()}
          </p>
          {!confirmed && (
            <p className="muted-note" style={{ margin: 0 }}>
              Recording this without a confirmed scan.
            </p>
          )}
        </>
      ) : (
        <Notice
          kind={short || over ? "warn" : "ok"}
          title={
            short
              ? `Took ${formatQty(state.tookMilli)} of ${formatQty(take.qty_milli)} — short`
              : over
                ? `Took ${formatQty(state.tookMilli)} of ${formatQty(take.qty_milli)} — more than asked`
                : `Took ${formatQty(state.tookMilli)} of ${formatQty(take.qty_milli)}`
          }
        >
          <p style={{ margin: 0 }}>
            {short
              ? "Staged for this build; it is still short by the difference, and the shortage list will show it."
              : over
                ? "Staged for this build. Unstage the surplus if it was a miscount."
                : "Exactly what the list asked for, staged for this build."}
          </p>
        </Notice>
      )}

      <ErrorBanner error={state.error} fallback="That take was not recorded." />
    </li>
  );
}
