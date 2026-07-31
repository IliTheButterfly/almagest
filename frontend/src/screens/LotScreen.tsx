/**
 * Take / return — workflow 3, and the screen that carries this whole build.
 *
 * One screen, usable one-handed: what and where, a big ±1 stepper over a keypad, a
 * big Commit, then an eight-second one-tap undo. It is also the landing page for a
 * scanned lot tag (`/lots/:id` is a `/s/{short_id}` redirect target), so it has to
 * work from a cold page load with no prior state.
 *
 * **Where a take goes depends on whether a tab is open** (ADR 0010). With a target
 * focused, taking adds a line to *that target's* record and writes **nothing**;
 * with nothing open it commits to the ledger immediately, exactly as it always
 * did — a take with no tab is a take, and that path is a request in its own right
 * ("just pick a container, scan it and say how many parts you took or put back"),
 * not an edge case of the other one. Return is symmetric on both paths: while a tab
 * is focused it is a negative line in the same record, because "I took four and put
 * one back" is one activity and has to read as one.
 *
 * Four details are load-bearing rather than cosmetic:
 *
 * 1. **The idempotency key comes from the scan, not from the commit.** If the key
 *    were minted when Commit was pressed, a double tap on a bad connection would
 *    send two keys and record two takes. `scanSession` mints it at scan time; this
 *    screen reuses whatever is already there and only mints its own when it was
 *    opened cold (a tag tap *is* the scan in that case).
 *
 * 2. **`replayed: true` suppresses the undo.** It means the server answered from
 *    its idempotency store and wrote nothing new — the movement happened earlier,
 *    so the eight-second window closed earlier too. Offering an undo there would
 *    be a lie about what is being reversed.
 *
 * 3. **The key is spent on use.** A second take from the same bin mints a fresh
 *    key, because replaying the first key would return the first take's numbers and
 *    look exactly like a successful second take — silently losing stock.
 *
 * 4. **On the record path the key is scoped to the *line*, not to this screen or
 *    this scan.** Deferring the write moves where the key must live: one visit to
 *    this screen can now feed several distinct future writes — one per tab, and one
 *    more each time a row is edited — so a key held here would make the second of
 *    them a replay of the first, which is rule 3's silent stock loss with extra
 *    steps. `ShoppingCart.add` therefore mints and re-mints the key per row, and
 *    this screen's own key stays **unspent**: nothing was written, so there is
 *    nothing for it to have been spent on. It is still there for the take that
 *    commits.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ContainerPicker, type PickedContainer } from "../components/ContainerPicker";
import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import { PathBar } from "../components/PathBar";
import { QuantityPad } from "../components/Quantity";
import {
  consumeLot,
  getLot,
  getLotHistory,
  getPart,
  moveLot,
  returnLot,
  undoMovement,
  type LedgerEntry,
  type LotRead,
  type MovementResponse,
  type PartRead,
} from "../lib/api/client";
import { netQtyMilli } from "../lib/cart/cart";
import { describeTarget, takeActionLabel } from "../lib/cart/describe";
import { carts } from "../lib/cart/registry";
import { useCartLines } from "../lib/cart/useCart";
import { formatDelta, formatQty, formatTimestamp } from "../lib/format";
import { useAsync } from "../lib/hooks/useAsync";
import { ALL_STORAGE } from "../lib/locations/trail";
import { useFocusedTarget } from "../lib/projectcontext/hooks";
import type { WorkTarget } from "../lib/projectcontext/target";
import { scanSession, uuid4 } from "../lib/scan/session";
import { offersUndo, undoSecondsLeft, UNDO_WINDOW_MS } from "../lib/stock/undo";

type Direction = "take" | "return";

interface Committed {
  readonly clientOpId: string;
  readonly direction: Direction;
  readonly qtyMilli: number;
  readonly at: number;
  readonly response: MovementResponse;
  readonly undoable: boolean;
}

/**
 * A take that went into a record instead of the ledger.
 *
 * `lineId` is `null` when the addition cancelled a row out exactly — "I put back
 * the four I had just taken" — which is a legitimate outcome with nothing left to
 * point at.
 */
interface Staged {
  readonly target: WorkTarget;
  readonly direction: Direction;
  readonly qtyMilli: number;
  readonly lineId: string | null;
  readonly at: number;
}

export function LotScreen() {
  const { lotId: rawLotId } = useParams();
  const lotId = Number(rawLotId);
  const valid = Number.isSafeInteger(lotId) && lotId > 0;

  const lot = useAsync<LotRead | null>(
    () => (valid ? getLot(lotId) : Promise.resolve(null)),
    [lotId, valid],
  );
  const partId = lot.data?.part_id ?? null;
  const part = useAsync<PartRead | null>(
    () => (partId === null ? Promise.resolve(null) : getPart(partId)),
    [partId],
  );

  if (!valid) {
    return <Notice kind="error" title="That is not a lot id" />;
  }
  if (lot.error !== null) {
    return <ErrorBanner error={lot.error} fallback="That lot could not be loaded." />;
  }
  if (lot.data === null) {
    return <Loading what="the lot" />;
  }

  return (
    <TakeReturn
      lot={lot.data}
      part={part.data}
      onCommitted={() => {
        lot.reload();
      }}
    />
  );
}

function TakeReturn({
  lot,
  part,
  onCommitted,
}: {
  lot: LotRead;
  part: PartRead | null;
  onCommitted: () => void;
}) {
  const [direction, setDirection] = useState<Direction>("take");
  const [qtyMilli, setQtyMilli] = useState(1000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [committed, setCommitted] = useState<Committed | null>(null);
  const [staged, setStaged] = useState<Staged | null>(null);
  const [undone, setUndone] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  /** Bumped whenever a movement is recorded; see `QuantityPad`'s `entryKey`. */
  const [entryKey, setEntryKey] = useState(0);

  /**
   * The tab this take will be attributed to, or `null` for "nothing is open".
   *
   * Read on every render rather than captured, so a tab opened or focused in the
   * panel while this screen is up changes what the button says and does — the
   * alternative is a control that names a target that stopped being the focused
   * one, which is the one thing ADR 0010 forbids outright.
   */
  const focused = useFocusedTarget();
  const cart = focused === null ? null : carts.for(focused);
  const lines = useCartLines(cart);
  /** What this record already says about *this* lot, netted. Signed. */
  const inRecordMilli = netQtyMilli(lines.filter((line) => line.lotId === lot.id));

  /**
   * The key for the movement about to be committed.
   *
   * Taken from the scan session when there is one, so the key really was minted at
   * scan time; minted here only when this screen was opened cold from a tag tap or
   * a pasted link, where the tap *is* the scan.
   */
  const opIdRef = useRef<string>(scanSession.current()?.clientOpId ?? uuid4());

  /**
   * Put the real name on anything taken before the part had loaded.
   *
   * The lot resolves before the part does, and the take control is live in
   * between — deliberately, because a bench screen that makes you wait to say you
   * picked something up is the wrong trade. So the row captures `Lot 7`, and this
   * corrects it the moment the name exists. Display only: `relabel` does not
   * re-key the row.
   *
   * The focused cart only. A take can only have landed in the cart that was
   * focused when the button was pressed, and the sub-second window in which that
   * take was captured nameless is not one a tab switch fits inside.
   */
  useEffect(() => {
    if (cart === null || part === null) {
      return;
    }
    cart.relabel(part.id, { partName: part.name, mpn: part.mpn ?? null });
  }, [cart, part]);

  // Drives the countdown, and only while there is one to draw.
  const counting = committed !== null && committed.undoable && !undone;
  useEffect(() => {
    if (!counting) {
      return;
    }
    const timer = globalThis.setInterval(() => setNow(Date.now()), 250);
    return () => globalThis.clearInterval(timer);
  }, [counting]);

  const elapsed = committed === null ? 0 : now - committed.at;
  const undoOpen = counting && elapsed < UNDO_WINDOW_MS;

  const commitToLedger = useCallback(async () => {
    if (qtyMilli <= 0) {
      return;
    }
    setBusy(true);
    setError(null);
    setUndone(false);
    setStaged(null);
    // Duplicate scans during an in-flight commit are dropped by the same debounce
    // as the decoder — the scanner must not queue a second movement behind this.
    scanSession.beginCommit();
    const clientOpId = opIdRef.current;
    try {
      const body = { qty_milli: qtyMilli, client_op_id: clientOpId, source: "scan" as const };
      const response =
        direction === "take" ? await consumeLot(lot.id, body) : await returnLot(lot.id, body);
      setCommitted({
        clientOpId,
        direction,
        qtyMilli,
        at: Date.now(),
        response,
        undoable: offersUndo(response),
      });
      setNow(Date.now());
      setEntryKey((key) => key + 1);
      // Spent. The next movement gets its own key.
      scanSession.spend();
      opIdRef.current = uuid4();
      onCommitted();
    } catch (cause) {
      setError(cause);
    } finally {
      scanSession.endCommit();
      setBusy(false);
    }
  }, [direction, lot.id, onCommitted, qtyMilli]);

  /**
   * The other half: put this into the focused target's record and write nothing.
   *
   * No `scanSession.beginCommit()`/`spend()` here, and that is the point of rule 4
   * in the module comment — no request is made, so there is no key to spend and no
   * commit for the scanner debounce to protect. The line carries its own key,
   * minted by `add`, and a second take of this same bin nets into the same row with
   * a *fresh* key rather than reusing the one the server may already have seen.
   */
  const addToRecord = useCallback(() => {
    if (cart === null || focused === null || qtyMilli <= 0) {
      return;
    }
    setError(null);
    setCommitted(null);
    const line = cart.add({
      partId: lot.part_id,
      partName: part?.name ?? `Lot ${lot.id}`,
      mpn: part?.mpn ?? null,
      qtyMilli,
      lotId: lot.id,
      locationId: lot.location_id,
      locationLabel: lot.location_label_path ?? null,
      direction,
    });
    setStaged({
      target: focused,
      direction,
      qtyMilli,
      lineId: line?.id ?? null,
      at: Date.now(),
    });
    setEntryKey((key) => key + 1);
  }, [cart, direction, focused, lot, part, qtyMilli]);

  /**
   * Take back the line that was just added.
   *
   * Adding the opposite direction rather than deleting the row: the row may have
   * existed before this screen touched it, in which case removing it would throw
   * away an earlier take as well. The netting in `add` is the same arithmetic
   * either way, so this restores exactly the state before the press — and if the
   * row only existed because of that press, netting to zero removes it.
   */
  const undoStaged = useCallback(() => {
    if (cart === null || staged === null) {
      return;
    }
    cart.add({
      partId: lot.part_id,
      partName: part?.name ?? `Lot ${lot.id}`,
      mpn: part?.mpn ?? null,
      qtyMilli: staged.qtyMilli,
      lotId: lot.id,
      locationId: lot.location_id,
      locationLabel: lot.location_label_path ?? null,
      direction: staged.direction === "take" ? "return" : "take",
    });
    setStaged(null);
  }, [cart, lot, part, staged]);

  const undo = useCallback(async () => {
    if (committed === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await undoMovement({
        client_op_id_to_undo: committed.clientOpId,
        // The undo is itself a write, so it carries its own key: a double tap on
        // the undo button must not append two compensating rows.
        client_op_id: uuid4(),
        source: "scan",
      });
      setUndone(true);
      onCommitted();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }, [committed, onCommitted]);

  const onHand = lot.qty_milli;
  const projected = direction === "take" ? onHand - qtyMilli : onHand + qtyMilli;
  /**
   * What the button says, and it says the target.
   *
   * ADR 0010: *"a take must never be attributable to a target the user cannot see
   * named at the moment they press the button"*. With nothing open the label is
   * exactly what it always was.
   */
  const actionLabel = takeActionLabel(focused, direction, formatQty(qtyMilli));
  const mpn = part?.mpn ?? null;
  const batch = lot.batch_code ?? null;

  return (
    <div className="stack">
      <div className="card">
        {/* A lot's place is its container's place. Only the container itself can
            be linked from here: `LotRead` carries `location_label_path` but no
            `id_path`, so the intermediate levels have no ids to link to and are
            shown as the one crumb they can honestly be. */}
        <PathBar
          trail={[
            { key: "root", label: ALL_STORAGE, to: "/tree" },
            {
              key: `loc-${lot.location_id}`,
              label: lot.location_label_path ?? `location ${lot.location_id}`,
              to: `/locations/${lot.location_id}`,
            },
            { key: `lot-${lot.id}`, label: part?.name ?? `Lot ${lot.id}` },
          ]}
          label="Lot path"
        />
        <h1>{part?.name ?? `Lot ${lot.id}`}</h1>
        {mpn !== null && (
          <p className="mono dim" style={{ margin: 0 }}>
            {mpn}
          </p>
        )}
        <p className="muted-note" style={{ margin: 0 }}>
          {batch !== null && (
            <>
              {" · batch "}
              <span className="mono">{batch}</span>
            </>
          )}
        </p>
        <div className="row">
          <div>
            <div className="big-number">{formatQty(onHand)}</div>
            <div className="muted-note">on hand</div>
          </div>
          <span className="spacer" />
          {lot.qty_reserved_milli > 0 && (
            <span className="badge badge-warn">{formatQty(lot.qty_reserved_milli)} reserved</span>
          )}
          {part !== null && <Link to={`/parts/${part.id}`}>Part detail</Link>}
        </div>
      </div>

      <div className="segmented" role="group" aria-label="Direction">
        <button
          type="button"
          aria-pressed={direction === "take"}
          onClick={() => setDirection("take")}
        >
          Take
        </button>
        <button
          type="button"
          aria-pressed={direction === "return"}
          onClick={() => setDirection("return")}
        >
          Return
        </button>
      </div>

      {focused !== null && (
        <Notice kind="info" title={`Working on ${describeTarget(focused)}`}>
          <p style={{ margin: 0 }}>
            This goes into that record — nothing is written to the ledger until you
            commit it.
            {inRecordMilli !== 0 &&
              ` It already holds ${formatQty(Math.abs(inRecordMilli))} of this lot${
                inRecordMilli < 0 ? ", going back" : ""
              }.`}
          </p>
        </Notice>
      )}

      <div className="card">
        <QuantityPad
          valueMilli={qtyMilli}
          onChange={setQtyMilli}
          caption={
            focused === null
              ? `${direction === "take" ? "taking" : "returning"} — leaves ${formatQty(projected)}`
              : `${direction === "take" ? "taking" : "putting back"} — for ${describeTarget(focused)}`
          }
          disabled={busy}
          entryKey={entryKey}
        />
      </div>

      {focused === null && projected < 0 && (
        <Notice kind="warn">
          That takes the balance below zero. It is recorded anyway — a negative
          balance is a bookkeeping error to be reconciled, not a reason to refuse
          something that physically happened.
        </Notice>
      )}

      {focused !== null && direction === "take" && inRecordMilli + qtyMilli > onHand && (
        <Notice kind="warn">
          That is more than this lot holds. It goes into the record anyway — the
          record is what you say you did — but the line will be refused when the
          record is committed unless the stock is there by then.
        </Notice>
      )}

      <ErrorBanner error={error} fallback="That movement was not recorded." />

      <div className="commit-bar">
        <button
          type="button"
          className="primary wide tall"
          onClick={() => (focused === null ? void commitToLedger() : addToRecord())}
          disabled={busy || qtyMilli <= 0}
        >
          {busy ? "Recording…" : actionLabel}
        </button>
      </div>

      {staged !== null && (
        <StagedOutcome staged={staged} netMilli={inRecordMilli} onUndo={undoStaged} />
      )}

      {committed !== null && (
        <CommitOutcome
          committed={committed}
          undone={undone}
          undoOpen={undoOpen}
          secondsLeft={undoSecondsLeft(elapsed)}
          busy={busy}
          onUndo={() => void undo()}
        />
      )}

      <MoveLot lot={lot} onMoved={onCommitted} />

      <LotHistory lotId={lot.id} refreshKey={committed?.at ?? 0} undone={undone} />
    </div>
  );
}

/**
 * What just went into a record — and the free undo that comes with it.
 *
 * ADR 0010 names this as the compensating gain for the fidelity the deferred write
 * costs: undoing an *uncommitted* line writes nothing and reverses nothing, so it
 * needs no eight-second window and no compensating ledger row. There is
 * deliberately no countdown here for that reason.
 */
function StagedOutcome({
  staged,
  netMilli,
  onUndo,
}: {
  staged: Staged;
  netMilli: number;
  onUndo: () => void;
}) {
  const verb = staged.direction === "take" ? "Took" : "Put back";
  return (
    <div className="undo-bar" role="status">
      <div style={{ flex: 1 }}>
        <strong>
          {verb} {formatQty(staged.qtyMilli)} for {describeTarget(staged.target)}
        </strong>
        <div className="muted-note">
          {netMilli === 0
            ? "That record now says nothing about this lot. Nothing has been written."
            : `${formatQty(Math.abs(netMilli))} of this lot in that record${
                netMilli < 0 ? ", going back" : ""
              }. Nothing has been written to the ledger.`}
        </div>
      </div>
      <button type="button" onClick={onUndo}>
        Undo
      </button>
    </div>
  );
}

/**
 * "This is in the wrong drawer" — the whole lot, to a container chosen by hand.
 *
 * Its own key, minted per attempt and never `opIdRef`: that one belongs to the
 * take/return above and is *spent on use*, so borrowing it would let a move replay
 * a take's stored response — the exact silent-stock-loss the module comment warns
 * about. A move is a different operation and gets a different key.
 *
 * Whole-lot only. A partial move would split the lot, and "some of these go
 * elsewhere" is take-then-receive, which already exists and keeps both halves'
 * provenance straight.
 */
function MoveLot({ lot, onMoved }: { lot: LotRead; onMoved: () => void }) {
  const [open, setOpen] = useState(false);
  const [destination, setDestination] = useState<PickedContainer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<string | null>(null);

  async function run(): Promise<void> {
    if (destination === null) {
      return;
    }
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      await moveLot(lot.id, {
        to_location_id: destination.id,
        client_op_id: uuid4(),
        source: "manual",
      });
      setResult(`Moved to ${destination.label}.`);
      setDestination(null);
      setOpen(false);
      onMoved();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <div className="row">
        <h3 style={{ margin: 0 }}>Where it lives</h3>
        <span className="spacer" />
        <button type="button" onClick={() => setOpen(!open)}>
          {open ? "Cancel" : "Move this lot…"}
        </button>
      </div>
      {result !== null && <Notice kind="ok">{result}</Notice>}
      {open && (
        <>
          <p className="muted-note" style={{ margin: 0 }}>
            The whole lot goes to one container. Nothing is taken out of stock — this
            only changes where it is.
          </p>
          <ContainerPicker
            onPick={setDestination}
            pickedId={destination?.id ?? null}
            excludeIds={[lot.location_id]}
            actionLabel="Move here"
          />
          <ErrorBanner error={error} fallback="The lot was not moved." />
          <button
            type="button"
            className="primary wide"
            disabled={busy || destination === null}
            onClick={() => void run()}
          >
            {busy
              ? "Moving…"
              : destination === null
                ? "Choose where it goes"
                : `Move to ${destination.label}`}
          </button>
        </>
      )}
    </div>
  );
}

function CommitOutcome({
  committed,
  undone,
  undoOpen,
  secondsLeft,
  busy,
  onUndo,
}: {
  committed: Committed;
  undone: boolean;
  undoOpen: boolean;
  secondsLeft: number;
  busy: boolean;
  onUndo: () => void;
}) {
  const verb = committed.direction === "take" ? "Took" : "Returned";
  const summary = `${verb} ${formatQty(committed.qtyMilli)} · now ${formatQty(
    committed.response.lot.qty_milli,
  )} on hand`;

  if (undone) {
    return (
      <Notice kind="ok" title="Undone">
        <p style={{ margin: 0 }}>
          A compensating row was appended — the original movement is still in the
          ledger, which is what makes the history honest.
        </p>
      </Notice>
    );
  }

  if (!committed.undoable) {
    // `replayed: true`. See the module comment: the movement landed earlier, so
    // there is no fresh eight-second window and nothing here to offer.
    return (
      <Notice kind="info" title="Already recorded">
        <p style={{ margin: 0 }}>
          {summary}. This exact operation had already been recorded, so nothing new
          was written and the undo window for it has closed. Undo it from the lot's
          history if it was a mistake.
        </p>
      </Notice>
    );
  }

  if (!undoOpen) {
    return (
      <Notice kind="ok" title="Recorded">
        <p style={{ margin: 0 }}>{summary}.</p>
      </Notice>
    );
  }

  return (
    <div className="undo-bar" role="status">
      <div style={{ flex: 1 }}>
        <strong>{summary}</strong>
      </div>
      <span className="countdown">{secondsLeft}s</span>
      <button type="button" onClick={onUndo} disabled={busy}>
        Undo
      </button>
    </div>
  );
}

function LotHistory({
  lotId,
  refreshKey,
  undone,
}: {
  lotId: number;
  refreshKey: number;
  undone: boolean;
}) {
  const [open, setOpen] = useState(false);
  const history = useAsync<LedgerEntry[] | null>(
    () => (open ? getLotHistory(lotId, 25) : Promise.resolve(null)),
    [lotId, open, refreshKey, undone],
  );

  return (
    <div className="card">
      <div className="row">
        <h3 style={{ margin: 0 }}>History</h3>
        <span className="spacer" />
        <button type="button" onClick={() => setOpen(!open)}>
          {open ? "Hide" : "Show"}
        </button>
      </div>
      {open && history.data === null && <Loading what="the ledger" />}
      {open && history.error !== null && <ErrorBanner error={history.error} />}
      {open && history.data !== null && history.data.length === 0 && (
        <p className="dim">No movements recorded.</p>
      )}
      {open && history.data !== null && history.data.length > 0 && (
        <div className="scroll-x">
          <table className="history">
            <thead>
              <tr>
                <th>When</th>
                <th>Kind</th>
                <th>Δ</th>
                <th>After</th>
                <th>Src</th>
              </tr>
            </thead>
            <tbody>
              {history.data.map((entry) => (
                <tr key={entry.seq}>
                  <td>{formatTimestamp(entry.ts)}</td>
                  <td>
                    {entry.kind}
                    {entry.reversal_of_seq !== null && (
                      <span className="badge"> undo of {entry.reversal_of_seq}</span>
                    )}
                  </td>
                  <td>{formatDelta(entry.delta_milli)}</td>
                  <td>{formatQty(entry.qty_after_milli)}</td>
                  <td className="dim">{entry.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
