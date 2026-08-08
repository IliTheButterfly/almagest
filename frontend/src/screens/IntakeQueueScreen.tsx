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
  cancelCaptureDispatch,
  createPart,
  dismissPendingIntake,
  listPendingIntake,
  requestCaptureDispatch,
  resolvePendingIntake,
  type IdentityCandidateRead,
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
/** The prefix `app.scripts.upload_capture` puts in front of an image's sha256. */
const CAPTURE_PAYLOAD = "capture:";

/**
 * What to show when there is no part number yet — the payload, unless it is not
 * one.
 *
 * `raw_payload` is mandatory on an intake entry and is normally the thing that
 * was scanned, which is exactly what a person wants to see. A photograph
 * uploaded rather than scanned has nothing to put there, so the uploader stores
 * the image's hash behind a prefix no symbology produces. Rendering that
 * verbatim gives a row headlined by seventy-one hex characters, which tells the
 * reader nothing and pushes everything useful off the screen.
 *
 * The stored value stays exactly as it is — it is the entry's identity and the
 * hash is how the picture is found. Only the heading changes.
 */
export function headlineFor(rawPayload: string): string {
  return rawPayload.startsWith(CAPTURE_PAYLOAD) ? "Photograph" : rawPayload;
}

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
          {entry.mpn ?? entry.supplier_part_number ?? headlineFor(entry.raw_payload)}
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
      {/* The way in to the whole story: what the browser read, which models ran, what
          each was told and answered, and what became of the part somebody accepted.
          A link rather than an inline panel because it is a diagnostic — read when a
          reading looks wrong, not on every visit — and because the transcripts it
          shows are long enough to bury the queue. */}
      <div className="sub">
        <Link to={`/intake/${entry.id}/activity`}>What happened to this →</Link>
      </div>
      <ErrorBanner error={error} fallback="That entry was not changed." />

      {/* The reason parking is worth anything: the desk pass gets the
          photograph, not just the payload. Collapsed, because loading and
          re-resolving every capture in a long queue would cost most where the
          queue is longest. */}
      {entry.capture_id !== null && entry.capture_id !== undefined && (
        <>
          <details>
            <summary>The picture, and what it says</summary>
            <IntakeCapture
              captureId={entry.capture_id}
              onCreated={(part) => void act("resolve", part.id)}
            />
          </details>
          {/* Not collapsed, unlike the picture above. A proposal is the thing this
              row is *for* once one exists — hiding it behind a disclosure would
              mean the overnight run's whole output needed a click per entry to
              find. The button is cheap to render and the candidates are at most
              three. */}
          <IdentityProposal entry={entry} onChanged={onChanged} onChoose={(partId) => act("resolve", partId)} />
        </>
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

/**
 * What a vision model proposed for this photograph, and the button that asks it to
 * (ADR 0021).
 *
 * ## Three things this deliberately does not do
 *
 * **It never says a part number *is* the answer.** Every row is a proposal with the
 * characters the model quoted printed next to it, because the quote is what a person
 * checks against the picture. ADR 0021 records that catching a fabrication no confidence
 * score would have: the wrong reading quoted `MODEL: MCQ-XBEE3`, a line that is not on
 * the label — which says `MODEL: MICRO` and `FCC ID: MCQ-XBEE3` separately.
 *
 * **It shows the losers.** The second and third readings are not noise to be hidden;
 * they are the alternatives, and one of them having a real datasheet while the others do
 * not is what the overnight research pass is *for*.
 *
 * **Choosing calls the ordinary resolve.** There is no accept-a-candidate endpoint, and
 * there must not be: a machine writing `resolved_part_id` at any confidence is the
 * never-auto-accept rule broken. The button here hands the candidate's already-minted
 * stub id to the same `resolve` a person uses for a barcode.
 *
 * ## Why the confidence is shown as text and not a bar
 *
 * It is **not calibrated** — 0.95 on a wrong answer, measured — and it is clamped below
 * the promotion threshold before it is ever stored. A progress bar reads as a measurement
 * and would invite exactly the trust the number does not deserve; a muted number beside
 * the quote reads as what it is, which is the model's own opinion of its eyesight.
 */
export function IdentityProposal({
  entry,
  onChanged,
  onChoose,
}: {
  entry: PendingIntakeRead;
  onChanged: () => void;
  onChoose: (partId: number) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const candidates = entry.identity_candidates ?? [];
  const state = entry.dispatch_state;

  async function ask(what: "request" | "cancel"): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await (what === "request" ? requestCaptureDispatch(entry.id) : cancelCaptureDispatch(entry.id));
      onChanged();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack" style={{ gap: "0.4rem" }}>
      <div className="row">
        <strong style={{ flex: 1 }}>Read the label</strong>
        <DispatchBadge state={state} />
      </div>

      <ErrorBanner error={error} fallback="That read was not queued." />

      {state === "not_requested" && (
        <>
          <p className="muted-note" style={{ margin: 0 }}>
            Nobody has asked a model to read this photograph. It is off by default because
            it takes the graphics card away from whatever else is using it.
          </p>
          <button type="button" disabled={busy} onClick={() => void ask("request")}>
            {busy ? "Queueing…" : "Ask a model to read it"}
          </button>
        </>
      )}

      {(state === "pending" || state === "claimed") && (
        <>
          <p className="muted-note" style={{ margin: 0 }}>
            {state === "pending"
              ? "Queued. It will be read the next time the reader runs."
              : "Being read now."}
          </p>
          <button type="button" disabled={busy} onClick={() => void ask("cancel")}>
            {busy ? "Cancelling…" : "Take it out of the queue"}
          </button>
        </>
      )}

      {state === "unidentified" && (
        <Notice kind="info" title="Nothing legible">
          <p style={{ margin: 0 }}>
            The model could not name a part from this photograph. That is not a fault —
            take another picture, closer or with less glare, rather than trying again on
            this one.
          </p>
        </Notice>
      )}

      {state === "failed" && (
        <Notice kind="warn" title="The read broke">
          <p style={{ margin: 0 }}>{entry.dispatch_error ?? "No reason was recorded."}</p>
        </Notice>
      )}

      {candidates.length > 0 && (
        <>
          <p className="muted-note" style={{ margin: 0 }}>
            {/* Said in the UI and not only in an ADR: the whole design rests on the
                person understanding that these are readings, not answers. */}
            Suggestions, best first. Check the quoted characters against the picture — the
            model can read a certification number or a URL as confidently as a part number.
          </p>
          <ul className="list">
            {candidates.map((candidate) => (
              <CandidateRow
                key={candidate.mpn}
                candidate={candidate}
                onChoose={onChoose}
              />
            ))}
          </ul>
        </>
      )}

      {(state === "proposed" || state === "unidentified" || state === "failed") && (
        <button type="button" disabled={busy} onClick={() => void ask("request")}>
          {busy ? "Queueing…" : "Read it again"}
        </button>
      )}
    </div>
  );
}

/** One proposal, with what was quoted for it and the one button that accepts it. */
function CandidateRow({
  candidate,
  onChoose,
}: {
  candidate: IdentityCandidateRead;
  onChoose: (partId: number) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const partId = candidate.part_id;

  return (
    <li className="list-item">
      <div className="row">
        <span className="title mono" style={{ flex: 1, overflowWrap: "anywhere" }}>
          {candidate.mpn}
        </span>
        {candidate.manufacturer !== null && candidate.manufacturer !== undefined && (
          <span className="badge">{candidate.manufacturer}</span>
        )}
      </div>
      <div className="sub">
        {/* The quote first, because it is the thing being checked. */}
        read <span className="mono">“{candidate.source_text}”</span>
        {candidate.package === null || candidate.package === undefined
          ? ""
          : ` · ${candidate.package}`}
        {` · the model rated its own reading ${candidate.confidence.toFixed(2)}`}
      </div>
      {candidate.note !== null && candidate.note !== undefined && (
        <div className="sub">{candidate.note}</div>
      )}
      <div className="row">
        {partId === null || partId === undefined ? (
          <span className="muted-note">
            No stub part was created for this reading, so it cannot be chosen here.
          </span>
        ) : (
          <>
            <button
              type="button"
              className="primary"
              disabled={busy}
              onClick={() => {
                setBusy(true);
                void onChoose(partId).finally(() => setBusy(false));
              }}
            >
              {busy ? "Recording…" : "This is it"}
            </button>
            <span className="spacer" />
            <Link to={`/parts/${partId}`}>Part {partId} →</Link>
          </>
        )}
      </div>
    </li>
  );
}

/**
 * Where the read stands, in the same shape `ResearchPanel.STATE_COPY` uses.
 *
 * Copied rather than shared because the two queues' words differ where it matters:
 * research's `exhausted` means "no datasheet exists for this part number" and dispatch's
 * `unidentified` means "nobody can read this photograph", and the fix for one is another
 * provider while the fix for the other is another picture. A shared table would push both
 * toward one bland phrase.
 *
 * **`unidentified` is deliberately not styled as a failure**, exactly as `exhausted` is
 * not: colouring a normal outcome red is how the real failures stop standing out.
 */
const DISPATCH_COPY: Record<string, { label: string; badge: string }> = {
  not_requested: { label: "not read", badge: "badge" },
  pending: { label: "queued", badge: "badge" },
  claimed: { label: "reading", badge: "badge badge-accent" },
  proposed: { label: "suggestions", badge: "badge badge-good" },
  unidentified: { label: "nothing legible", badge: "badge" },
  failed: { label: "read broke", badge: "badge badge-bad" },
};

function DispatchBadge({ state }: { state: PendingIntakeRead["dispatch_state"] }) {
  const chosen = DISPATCH_COPY[state] ?? { label: state, badge: "badge" };
  return <span className={chosen.badge}>{chosen.label}</span>;
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

      {/* The photograph, for an entry that has not synced yet.
       *
       * This used to render only under `ServerRow`, so the picture appeared the
       * moment you pressed Sync and not a second before — which is backwards:
       * the entry you *just* parked is the one you are least likely to remember
       * and most likely to be curating. The capture itself already lives on the
       * server (`captureId` is a server row minted at scan time), so the only
       * thing that was local was the queue entry pointing at it.
       *
       * It carries the same ranked readings as the aisle had, so a part can be
       * built from the label here rather than from the payload alone — and when
       * that happens the entry is dropped from the queue, exactly as `create`
       * below does. */}
      {open && entry.captureId !== null && (
        <IntakeCapture
          captureId={entry.captureId}
          onCreated={(part) => {
            setCreatedId(part.id);
            intakeQueue.remove(entry.id);
          }}
        />
      )}

      {open && (
        <form
          className="stack"
          onSubmit={(event) => {
            event.preventDefault();
            void create();
          }}
        >
          {entry.captureId !== null && (
            <p className="muted-note" style={{ margin: 0 }}>
              Or record it from the payload alone, without the label above.
            </p>
          )}
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
