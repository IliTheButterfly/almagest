/**
 * Scan — the front door, and where the fast path lives.
 *
 * Three ways in, in order of speed: the camera (four symbologies, centre-ROI crop,
 * 2-of-3 frame voting, 3-second payload hold-off), Web NFC on Android, and typing a
 * code. The third is not a courtesy: ADR 0001 means the first two are *absent* — no
 * error, no prompt — on any plain-HTTP origin, and Web NFC is absent on iOS and on
 * the kiosk permanently. So each affordance is feature-detected and, when missing,
 * replaced by a sentence explaining why and where to open the app instead.
 *
 * **The idempotency key is minted here**, by `scanSession.scan()`, and travels to
 * whichever screen commits the movement. That is the ordering `PLAN.md` requires:
 * mint at scan, not at commit, so a double tap on Commit cannot record twice.
 *
 * **"Queue for later" is the point of the whole screen.** One tap parks the label
 * and returns straight to scanning, no further screens, so a box of reels goes in
 * under a minute and is curated at a desk afterwards. This is the countermeasure to
 * the thing that actually kills projects in this space, and `parts` is shaped for it:
 * only a name is required, so an unrecognised label becomes a legal stub row in one
 * tap instead of a form somebody abandons.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { AssignStock } from "../components/AssignStock";
import { CaptureOverlay } from "../components/CaptureOverlay";
import { CAPTURE_PART_FIELDS, CaptureToPart, type PartDraft } from "../components/CaptureToPart";
import { CodeEntry } from "../components/CodeEntry";
import { ErrorBanner, Notice } from "../components/Feedback";
import { CategorySelect } from "../components/CategorySelect";
import { PartKindPicker } from "../components/PartKindPicker";
import { Viewfinder } from "../components/Viewfinder";
import {
  bindScanAlias,
  createPart,
  resolveScan,
  type ScanCandidate,
  type ScanResolveResponse,
  type ScanTarget,
} from "../lib/api/client";
import { cameraNotice, detectCapabilities, nfcNotice } from "../lib/capabilities";
import type { FillField } from "../lib/capture/chips";
import { extractSuggestions } from "../lib/capture/extract";
import { useCapture, type CaptureState } from "../lib/capture/useCapture";
import { formatQty } from "../lib/format";
import { intakeQueue, type PendingScan } from "../lib/intake/queue";
import { DecodeFeedback, FEEDBACK_FLASH_MS } from "../lib/scan/feedback";
import { NfcUnavailableError, readOneTag } from "../lib/scan/nfc";
import { scanSession } from "../lib/scan/session";
import { useScanner } from "../lib/scan/useScanner";
import { formatShortId } from "../lib/shortid";

/**
 * The flash/vibrate/tone hook. One `DecodeFeedback` instance lives for the
 * screen's whole life (a ref, not state — it holds a real `AudioContext` once
 * `init()` has run, and re-creating that on every render would drop it).
 * `trigger` is what `handle()` calls on every decode; `init` is what a click
 * handler calls, and only a click handler — see `feedback.ts`.
 */
function useDecodeFeedback(): { readonly flashing: boolean; readonly trigger: (code: string) => void; readonly init: () => void } {
  const feedbackRef = useRef<DecodeFeedback | null>(null);
  feedbackRef.current ??= new DecodeFeedback();
  const [flashing, setFlashing] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(
    () => () => {
      if (timerRef.current !== undefined) {
        clearTimeout(timerRef.current);
      }
    },
    [],
  );

  const trigger = useCallback((code: string) => {
    if (!(feedbackRef.current?.fire(code) ?? false)) {
      return;
    }
    setFlashing(true);
    if (timerRef.current !== undefined) {
      clearTimeout(timerRef.current);
    }
    timerRef.current = setTimeout(() => setFlashing(false), FEEDBACK_FLASH_MS);
  }, []);

  const init = useCallback(() => feedbackRef.current?.init(), []);

  return { flashing, trigger, init };
}

/** Where a resolved target lives in the app. Mirrors the backend's `/s/` map. */
function routeFor(target: ScanTarget): string | null {
  switch (target.entity_type) {
    case "location":
      return `/locations/${target.entity_pk}`;
    case "part":
      return `/parts/${target.entity_pk}`;
    case "stock_lot":
      return `/lots/${target.entity_pk}`;
    default:
      return null;
  }
}

interface Resolution {
  readonly response: ScanResolveResponse;
  readonly clientOpId: string;
  readonly code: string;
  readonly symbology: string | null;
}

export function ScanScreen() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const capabilities = detectCapabilities();

  const [cameraOn, setCameraOn] = useState(capabilities.camera);
  const [resolution, setResolution] = useState<Resolution | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [queued, setQueued] = useState<string | null>(null);
  const { flashing, trigger: triggerFeedback, init: initFeedback } = useDecodeFeedback();

  const handle = useCallback(
    async (code: string, symbology: string | null) => {
      // Fires on *every* decode, before the outcome is known — including one
      // that resolves to nothing, which is the case "I scanned it and nothing
      // happened" is actually about. Its own 400 ms debounce is independent of
      // the scan session's below, so this still confirms a deliberate re-scan
      // that the session drops as a duplicate.
      triggerFeedback(code);

      // The scan session is the debounce: the same payload inside ~2 s, or any
      // payload while a commit is in flight, returns null and is dropped silently.
      // A dropped duplicate is not an error and must not be reported as one.
      const session = scanSession.scan(code, symbology);
      if (session === null) {
        return;
      }
      setBusy(true);
      setError(null);
      setQueued(null);
      try {
        const response = await resolveScan({
          code,
          ...(symbology === null ? {} : { symbology }),
        });
        const next: Resolution = {
          response,
          clientOpId: session.clientOpId,
          code,
          symbology,
        };
        setResolution(next);

        // A container or a lot is unambiguous — go straight there rather than
        // making the user confirm what they just scanned. A part waits, because a
        // part that resolved may be a re-stock and the choice belongs to the user.
        const target = response.target;
        if (
          response.status === "resolved" &&
          target !== null &&
          target !== undefined &&
          (target.entity_type === "location" || target.entity_type === "stock_lot")
        ) {
          const route = routeFor(target);
          if (route !== null) {
            navigate(route);
          }
        }
      } catch (cause) {
        setError(cause);
      } finally {
        setBusy(false);
      }
    },
    [navigate, triggerFeedback],
  );

  const scanner = useScanner({ active: cameraOn, onDecode: (text, symbology) => void handle(text, symbology) });
  const lens = useCapture();

  /**
   * Stop decoding while a capture is on screen.
   *
   * Not a nicety: the live loop would keep firing resolves for whatever the
   * camera is now pointed at — the user's hand, the next reel — while they are
   * reading a *photograph* of something else, and each of those navigates or
   * replaces the resolution panel underneath them. The camera itself keeps
   * running so dismissing the capture is instant.
   */
  useEffect(() => {
    scanner.pause(lens.state.imageUrl !== null);
  }, [scanner, lens.state.imageUrl]);

  // A well-formed code the backend could not resolve arrives as `?unknown=`, and a
  // resolved-but-untyped one as `?code=`. Both are redirects off a physical tag.
  const incoming = params.get("code") ?? params.get("unknown");
  useEffect(() => {
    if (incoming !== null && incoming !== "") {
      void handle(incoming, "manual");
    }
    // Deliberately keyed on the URL only: re-running this on every `handle`
    // identity change would re-resolve the same tag on each render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [incoming]);

  useEffect(() => () => scanSession.clear(), []);

  function queueForLater(): void {
    if (resolution === null) {
      return;
    }
    const parsed = resolution.response.parsed;
    const entry: PendingScan = {
      id: resolution.clientOpId,
      code: resolution.code,
      symbology: resolution.symbology,
      queuedAt: Date.now(),
      decodedKind: resolution.response.decoded_kind,
      mpn: parsed?.mpn ?? null,
      manufacturer: parsed?.manufacturer ?? null,
      supplierPartNumber: parsed?.supplier_part_number ?? null,
      quantityMilli: parsed?.quantity_milli ?? null,
      dateCode: parsed?.date_code ?? null,
      lotCode: parsed?.lot_code ?? null,
      partId:
        resolution.response.target?.entity_type === "part"
          ? (resolution.response.target.entity_pk ?? null)
          : null,
      note: null,
    };
    intakeQueue.add(entry);
    // Straight back to scanning. Zero further screens is the requirement.
    setResolution(null);
    scanSession.spend();
    setQueued(entry.mpn ?? entry.code);
  }

  return (
    <div className="stack">
      {/* Always in the DOM, camera or none: the same confirmation for every
       * way a code gets in, and it must be visible before the network round
       * trip resolves — that immediacy is the whole point. */}
      <div className={`decode-flash${flashing ? " is-active" : ""}`} role="status" aria-live="polite">
        {flashing ? "Scanned" : "Ready to scan"}
      </div>

      {capabilities.camera ? (
        <>
          <Viewfinder
            videoRef={scanner.videoRef}
            status={cameraOn ? scanner.status : "off"}
            message={scanner.message}
            unavailableNotice={cameraNotice(capabilities)}
            hint={busy ? "Resolving…" : undefined}
            camera={scanner}
          />
          <div className="row">
            <button
              type="button"
              onClick={() => {
                // Starting the camera is a real click, so this is where the
                // audio context has to be created — never from the decode
                // path, which runs off a `setTimeout` tick and has no gesture
                // to spend. See lib/scan/feedback.ts.
                initFeedback();
                setCameraOn(!cameraOn);
              }}
            >
              {cameraOn ? "Stop camera" : "Start camera"}
            </button>
            {/* The primary action once the camera is up. A scan reads *one*
             * code and navigates; a capture keeps the frame and reads
             * everything on it, which is what the printed half of a reel label
             * needs. */}
            <button
              type="button"
              className="primary"
              disabled={!cameraOn || scanner.status !== "live"}
              onClick={() => {
                const video = scanner.videoRef.current;
                if (video !== null) {
                  void lens.capture(video);
                }
              }}
            >
              Capture
            </button>
          </div>
        </>
      ) : (
        <>
          <Notice kind="warn" title="No camera here">
            <p style={{ margin: 0 }}>{cameraNotice(capabilities)}</p>
          </Notice>
          {/* Leads with the manual path rather than burying it below a dead
           * viewfinder — this is the fix for "I saw the website the QR led
           * to", not a courtesy. */}
          <div className="card">
            <h3>Type it</h3>
            <CodeEntry
              onSubmit={(code) => {
                initFeedback();
                void handle(code, "manual");
              }}
              busy={busy}
            />
          </div>
        </>
      )}

      <NfcPanel onRead={(payload) => void handle(payload, "nfc")} onInit={initFeedback} />

      {capabilities.camera && (
        <div className="card">
          <h3>Or type it</h3>
          <CodeEntry
            onSubmit={(code) => {
              initFeedback();
              void handle(code, "manual");
            }}
            busy={busy}
          />
        </div>
      )}

      {lens.state.imageUrl !== null && (
        <CapturePanel state={lens.state} onDismiss={lens.clear} />
      )}

      {queued !== null && (
        <Notice kind="ok" title="Parked for later">
          <p style={{ margin: 0 }}>
            <span className="mono">{queued}</span> is in the intake queue. Keep scanning
            — nothing else is needed now.
          </p>
        </Notice>
      )}

      <ErrorBanner error={error} fallback="That code could not be resolved." />

      {resolution !== null && (
        <Resolved
          resolution={resolution}
          onQueue={queueForLater}
          onDismiss={() => setResolution(null)}
          onChanged={() => setResolution(null)}
        />
      )}
    </div>
  );
}

/**
 * The frozen frame, everything read off it, and the form it can fill.
 *
 * Its own component rather than more JSX inside `ScanScreen` because it owns two
 * pieces of state that only make sense together — which field is armed, and what
 * has been picked into the draft so far — and threading those through the
 * screen's already busy body would put them a long way from the overlay they
 * describe.
 */
function CapturePanel({
  state,
  onDismiss,
}: {
  state: CaptureState;
  onDismiss: () => void;
}) {
  const [armed, setArmed] = useState<FillField | null>(null);
  const [draft, setDraft] = useState<PartDraft>({});
  const [created, setCreated] = useState<{ id: number; name: string } | null>(null);

  const armedLabel = CAPTURE_PART_FIELDS.find((entry) => entry.field === armed)?.label ?? "";
  // Recomputed as regions and resolutions arrive: barcodes land first, the OCR
  // pass seconds later, and each makes the suggestions better rather than
  // replacing them.
  const suggestions = useMemo(
    () => extractSuggestions({ regions: state.regions, resolved: state.resolved }),
    [state.regions, state.resolved],
  );

  function fill(field: FillField, value: string): void {
    setDraft((previous) => ({ ...previous, [field]: value }));
    // Disarm after one pick. Leaving it armed makes the *next* tap — often
    // meant as a copy — silently overwrite the field the user just filled.
    setArmed(null);
  }

  if (state.imageUrl === null) {
    return null;
  }

  return (
    <div className="card">
      <div className="row">
        <h3 style={{ margin: 0 }}>What is on this frame</h3>
        <span className="spacer" />
        <button type="button" onClick={onDismiss}>
          Back to scanning
        </button>
      </div>

      <CaptureOverlay
        imageUrl={state.imageUrl}
        width={state.width}
        height={state.height}
        regions={state.regions}
        resolved={state.resolved}
        textStatus={state.textStatus}
        textMessage={state.textMessage}
        readingText={state.readingText}
        {...(armed === null ? {} : { fillInto: { field: armed, label: armedLabel } })}
        onFill={fill}
      />

      <p className="muted-note" style={{ margin: 0 }}>
        Saved with the frame, so this photograph is still here when the queue is
        curated at a desk — see <a href="/captures">Captures</a>. Tap an outline,
        then a value, to copy it anywhere.
      </p>

      {created === null ? (
        <details>
          <summary>Make a part from this</summary>
          <CaptureToPart
            draft={draft}
            armed={armed}
            suggestions={suggestions}
            onArm={setArmed}
            onChange={(field, value) => setDraft((previous) => ({ ...previous, [field]: value }))}
            onCreated={setCreated}
          />
        </details>
      ) : (
        <Notice kind="ok" title="Created — it has no stock yet">
          <p style={{ margin: 0 }}>
            <a href={`/parts/${created.id}`}>Open part {created.id}</a>. A part is a
            definition, not a count: it does not exist anywhere until some quantity of
            it is put in a lot at a location.
          </p>
        </Notice>
      )}

      <ErrorBanner error={state.error} fallback="That capture could not be saved." />
    </div>
  );
}

function NfcPanel({
  onRead,
  onInit,
}: {
  onRead: (payload: string) => void;
  /** Called from the tap, a real click — see lib/scan/feedback.ts. */
  onInit: () => void;
}) {
  const capabilities = detectCapabilities();
  const notice = nfcNotice(capabilities);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState<unknown>(null);

  if (notice !== null) {
    return (
      <details className="card">
        <summary>NFC is not available on this device</summary>
        <p className="muted-note">{notice}</p>
      </details>
    );
  }

  async function tap(): Promise<void> {
    onInit();
    setWaiting(true);
    setError(null);
    try {
      const reading = await readOneTag();
      // NDEF first, UID as the fallback — a tag whose record was never written is
      // still identifiable, which is what makes a blank tag recoverable.
      const payload = reading.url ?? reading.serialNumber;
      if (payload === null) {
        setError(new Error("The tag carried no URL and reported no serial number."));
        return;
      }
      onRead(payload);
    } catch (cause) {
      setError(cause instanceof NfcUnavailableError ? new Error(notice ?? cause.message) : cause);
    } finally {
      setWaiting(false);
    }
  }

  return (
    <div className="card">
      <button type="button" className="wide" onClick={() => void tap()} disabled={waiting}>
        {waiting ? "Hold the phone against the tag…" : "Read an NFC tag"}
      </button>
      <ErrorBanner error={error} />
    </div>
  );
}

function Resolved({
  resolution,
  onQueue,
  onDismiss,
  onChanged,
}: {
  resolution: Resolution;
  onQueue: () => void;
  onDismiss: () => void;
  onChanged: () => void;
}) {
  const { response } = resolution;
  const target = response.target ?? null;
  const parsed = response.parsed ?? null;

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <h3 style={{ margin: 0 }}>
            {response.status === "resolved"
              ? "Resolved"
              : response.status === "ambiguous"
                ? "More than one match"
                : "Nothing matched"}
          </h3>
          <span className="spacer" />
          <span className="badge mono">{response.decoded_kind}</span>
          <span className="badge">{response.latency_ms} ms</span>
        </div>

        {/* The fast path, first and biggest: one tap, back to scanning. */}
        <button type="button" className="primary wide tall" onClick={onQueue}>
          Queue for later
        </button>
        <p className="muted-note" style={{ margin: 0 }}>
          Parks this label and returns to scanning with no further screens. Curate the
          queue later at a desktop.
        </p>

        {target !== null && <TargetLink target={target} />}

        {response.existing_lots !== undefined && response.existing_lots.length > 0 && (
          <div>
            <h3>Already in stock</h3>
            <ul className="list">
              {response.existing_lots.map((lot) => (
                <li key={lot.lot_id}>
                  <a className="list-item" href={`/lots/${lot.lot_id}`}>
                    <div className="title">{formatQty(lot.qty_milli)}</div>
                    <div className="sub">{lot.location_label_path ?? lot.location_name}</div>
                  </a>
                </li>
              ))}
            </ul>
          </div>
        )}

        {parsed !== null && <ParsedFields parsed={parsed} />}

        <div className="row">
          <button type="button" onClick={onDismiss}>
            Dismiss
          </button>
        </div>
      </div>

      {response.candidates !== undefined && response.candidates.length > 0 && (
        <Candidates candidates={response.candidates} />
      )}

      {response.suggest_bind && <BindOrCreate resolution={resolution} onDone={onChanged} />}
    </div>
  );
}

function TargetLink({ target }: { target: ScanTarget }) {
  const route = routeFor(target);
  const label = target.label_path ?? target.label;
  if (route === null) {
    return (
      <p className="muted-note">
        {target.entity_type} {target.entity_pk}: {label}
      </p>
    );
  }
  return (
    <a className="list-item" href={route}>
      <div className="title">{label}</div>
      <div className="sub">
        {target.entity_type}
        {target.short_id === null || target.short_id === undefined
          ? ""
          : ` · ${formatShortId(target.short_id)}`}
      </div>
    </a>
  );
}

function Candidates({ candidates }: { candidates: readonly ScanCandidate[] }) {
  return (
    <div className="card">
      <h3>Candidates</h3>
      <ul className="list">
        {candidates.map((candidate, index) => {
          const route = routeFor(candidate.target);
          const label = candidate.target.label_path ?? candidate.target.label;
          return (
            <li key={`${candidate.target.entity_type}-${candidate.target.entity_pk}-${index}`}>
              {route === null ? (
                <div className="list-item">
                  <div className="title">{label}</div>
                  <div className="sub">{candidate.via}</div>
                </div>
              ) : (
                <a className="list-item" href={route}>
                  <div className="title">{label}</div>
                  <div className="sub">
                    via {candidate.via}
                    {candidate.hit_count === null || candidate.hit_count === undefined
                      ? ""
                      : ` · ${candidate.hit_count} hit(s)`}
                  </div>
                </a>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function ParsedFields({ parsed }: { parsed: NonNullable<ScanResolveResponse["parsed"]> }) {
  const rows: [string, string][] = [];
  const push = (label: string, value: string | number | null | undefined): void => {
    if (value !== null && value !== undefined && value !== "") {
      rows.push([label, String(value)]);
    }
  };
  push("MPN", parsed.mpn);
  push("Supplier PN", parsed.supplier_part_number);
  push("Manufacturer", parsed.manufacturer);
  push("Quantity", parsed.quantity_milli === null ? null : formatQty(parsed.quantity_milli ?? 0));
  push("Lot", parsed.lot_code);
  push("Date code", parsed.date_code);
  push("Country", parsed.country_of_origin);
  push("Serial", parsed.serial);
  push("PO", parsed.purchase_order);

  if (rows.length === 0 && (parsed.warnings ?? []).length === 0) {
    return null;
  }

  return (
    <div>
      <h3>Read off the label</h3>
      <dl className="kv">
        {rows.map(([label, value]) => (
          <div key={label} style={{ display: "contents" }}>
            <dt>{label}</dt>
            <dd className="mono">{value}</dd>
          </div>
        ))}
      </dl>
      {(parsed.warnings ?? []).length > 0 && (
        <Notice kind="warn" title="Parser warnings">
          <ul>
            {(parsed.warnings ?? []).map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </Notice>
      )}
      <p className="muted-note">
        Pre-fills a form; never authority. Nothing here is accepted as a part number
        without a human saying so.
      </p>
    </div>
  );
}

/**
 * The two ways out of an unresolved scan.
 *
 * **Bind** teaches the resolver what this payload means — the alias outranks every
 * parser from then on, because the user knows which reel is in their hand and the
 * parser is reading a label whose conventions we guessed at. **Create a stub** is
 * the one-tap escape: only a name is required, so an unrecognised label never turns
 * into a form somebody abandons.
 */
function BindOrCreate({
  resolution,
  onDone,
}: {
  resolution: Resolution;
  onDone: () => void;
}) {
  const parsed = resolution.response.parsed ?? null;
  const [name, setName] = useState(parsed?.mpn ?? "");
  const [partKind, setPartKind] = useState("component");
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [created, setCreated] = useState<{ id: number; name: string } | null>(null);
  const [assigned, setAssigned] = useState(false);
  const [bindTo, setBindTo] = useState("");

  async function create(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const result = await createPart({
        name: name.trim() === "" ? resolution.code.slice(0, 60) : name.trim(),
        part_kind: partKind.trim() === "" ? "component" : partKind.trim(),
        is_stub: true,
        // Filed now if the user knows where it goes. Optional on purpose: a scan
        // that lands on an unknown label must not turn into a form somebody
        // abandons, and a part can be filed later from its own screen.
        ...(categoryId === null ? {} : { category_id: categoryId }),
        ...(parsed?.mpn === null || parsed?.mpn === undefined ? {} : { mpn: parsed.mpn }),
        // Reuse the key minted at scan time: a retried create must not fork the
        // catalogue into two rows for the same label.
        client_op_id: resolution.clientOpId,
      });
      setCreated({ id: result.part.id, name: result.part.name });
      // Bind the payload to the row it just created, so the next reel of this part
      // resolves on its first scan instead of coming back unknown again.
      await bindScanAlias({
        code: resolution.code,
        symbology: resolution.symbology ?? "unknown",
        entity_type: "part",
        entity_pk: result.part.id,
        alias_kind: "whole_payload",
        ...(parsed?.quantity_milli === null || parsed?.quantity_milli === undefined
          ? {}
          : { hint_qty_milli: parsed.quantity_milli }),
      });
      // Deliberately not `onDone()` here: that clears the resolution and would
      // take the ASSIGN step below with it. A part that just became a row in
      // the catalogue has no stock anywhere yet — see AssignStock's module
      // comment — so the create is not finished until this screen offers
      // somewhere to put it, not a dead end with only a link to "open" a part
      // that is still, physically, nowhere.
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  async function bind(): Promise<void> {
    const partId = Number(bindTo);
    if (!Number.isSafeInteger(partId) || partId <= 0) {
      setError(new Error("Enter the numeric id of the part to bind this code to."));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await bindScanAlias({
        code: resolution.code,
        symbology: resolution.symbology ?? "unknown",
        entity_type: "part",
        entity_pk: partId,
        alias_kind: "whole_payload",
      });
      onDone();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  if (created !== null) {
    return (
      <div className="stack">
        <Notice kind="ok" title="Created — it has no stock yet">
          <p style={{ margin: 0 }}>
            <a href={`/parts/${created.id}`}>Open part {created.id}</a> — this payload is now
            bound to it, so the next one resolves on its first scan. A part is a
            definition, though, not a count: it does not exist anywhere until some
            quantity of it is put in a lot at a location. That is the next step.
          </p>
        </Notice>

        {assigned ? (
          <Notice kind="ok" title="On the shelf">
            <p style={{ margin: 0 }}>
              Stock recorded. <a href={`/parts/${created.id}`}>Open the part</a> to see it,
              or keep scanning.
            </p>
          </Notice>
        ) : (
          <AssignStock
            partId={created.id}
            partName={created.name}
            autoSuggest
            onAssigned={() => setAssigned(true)}
          />
        )}

        <button type="button" className="wide" onClick={onDone}>
          Back to scanning
        </button>
      </div>
    );
  }

  return (
    <div className="card">
      <h3>Nothing matched — teach it</h3>

      <label className="field">
        <span>Name (the only required field)</span>
        <input value={name} onChange={(event) => setName(event.target.value)} placeholder="what it is" />
      </label>
      <PartKindPicker value={partKind} onChange={setPartKind} />
      <CategorySelect
        value={categoryId}
        onChange={setCategoryId}
        hint="Optional, and changeable later — but it is what decides which fields this part can be filtered by."
      />
      <button type="button" className="primary wide" onClick={() => void create()} disabled={busy}>
        {busy ? "Creating…" : "Create a stub part and bind this code"}
      </button>

      <details>
        <summary>Bind to a part that already exists</summary>
        <label className="field">
          <span>Part id</span>
          <input
            inputMode="numeric"
            value={bindTo}
            onChange={(event) => setBindTo(event.target.value)}
            placeholder="42"
          />
        </label>
        <p className="muted-note">
          A binding outranks every parser from then on. Search for the part first if you
          need its id — there is no picker here yet.
        </p>
        <button type="button" className="wide" onClick={() => void bind()} disabled={busy}>
          Bind
        </button>
      </details>

      <ErrorBanner error={error} />
      <p className="muted-note mono">
        normalises to {resolution.response.normalized}
      </p>
    </div>
  );
}
