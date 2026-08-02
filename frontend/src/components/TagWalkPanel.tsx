/**
 * The two walks along a cabinet: bind every tag, then prove the binding.
 *
 * PLAN.md's target is **2-3 seconds per drawer** — a 44-drawer cabinet in under
 * two minutes, walking-paced rather than software-paced. Everything here is
 * shaped by that:
 *
 * - **One tap does the whole step.** Tap → bind → write the NDEF URI → read it
 *   back → advance. No confirm button, because a confirm button is the entire
 *   budget.
 * - **The cursor is never stored.** Every response carries the next one, derived
 *   server-side as `MIN(sort_order)` among the untagged children. Killing the
 *   browser mid-cabinet costs nothing, and a drawer bound from another phone
 *   drops out of the walk by itself.
 * - **A conflict is a two-button question, not an error.** "Already bound to
 *   {label_path}" with Move here / Cancel, because binding a tag that is already
 *   on another drawer is an ordinary thing to do (you moved the sticker) and an
 *   error dialog would make the normal path read as a failure.
 *
 * **Apply every tag physically first, then walk the cabinet binding them.** The
 * walk confirms whatever tag is *already on that drawer*, so there is no
 * loose-tag hand-off in which two stickers can be swapped without anyone
 * noticing. That ordering is the whole trick, and it is why the screen never
 * offers "take a blank tag and stick it on next".
 *
 * **The verification walk never repairs anything.** It re-reads every tag in
 * order, and a mismatch is recorded with the reverse lookup of where the scanned
 * tag actually belongs ("this tag belongs to B2") and then stops. Auto-fixing
 * would mean choosing between rebinding this drawer and swapping two drawers,
 * which are different physical claims about what happened, and only the person
 * holding the drawers knows which is true.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Dialog } from "./Dialog";
import { ErrorBanner, Loading, Notice } from "./Feedback";
import {
  bindTag,
  checkTag,
  getCurrentProvisioningSession,
  recordTagWriteResult,
  skipSlot,
  startProvisioningSession,
  startVerificationSession,
  undoProvisioningAction,
  type ConflictRead,
  type LocationRead,
  type MismatchRead,
  type ProvisioningState,
  type SlotCursorRead,
  type VerificationState,
} from "../lib/api/client";
import { DecodeFeedback, FEEDBACK_FLASH_MS } from "../lib/scan/feedback";
import { uuid4 } from "../lib/scan/session";
import { TagNotBlankError } from "../lib/scan/nfc";
import type { BridgeConnection, BridgeDevice } from "../lib/tags/bridge";
import { bridgeSource } from "../lib/tags/bridge";
import { useBridgeDevices } from "../lib/tags/useBridge";
import { simulatedTagSource, type SimulatedTag } from "../lib/tags/simulated";
import {
  debounceTaps,
  manualTagSource,
  webNfcTagSource,
  type TagPresentation,
  type TagSource,
} from "../lib/tags/source";
import { detectCapabilities, nfcNotice } from "../lib/capabilities";

export type WalkKind = "provision" | "verify";

/**
 * What the last tap achieved, in the user's terms.
 *
 * A separate thing from the error banner: a mismatch and a degraded write are
 * both *successful* readings that need saying out loud, and rendering them as
 * errors would train the user to ignore the red box that also carries "the
 * server is down".
 */
interface Outcome {
  readonly tone: "ok" | "warn" | "info";
  readonly title: string;
  readonly detail: string;
}

/**
 * A tag whose NDEF write did not take.
 *
 * Worth its own sentence rather than a generic failure, because the recovery is
 * specific and the loss is partial: the UID is in factory-locked pages 0-2 and is
 * untouched, so the drawer still identifies itself at the station and only a
 * phone tap is lost. Nothing needs re-binding; the sticker needs rewriting.
 */
const DEGRADED_DETAIL =
  "The tag answered but its URL did not come back. The binding is fine and the " +
  "station will still recognise this drawer — a phone tap will not open anything " +
  "until the tag is rewritten. The verification walk lists it.";

// --------------------------------------------------------------- readers ----

/**
 * `bridge:<deviceId>` for a reader reached through the device bridge — one choice
 * per attached device, because a bench may have a PN532 under the platform *and*
 * a Flipper on a cable, and "write this tag" is meaningless until the user has
 * been told which field to hold the tag in (ADR 0014).
 */
type ReaderChoice = "webnfc" | "manual" | "simulated" | `bridge:${string}`;

function bridgeChoice(deviceId: string): ReaderChoice {
  return `bridge:${deviceId}`;
}

/** A cabinet of tags that does not exist, for demonstrating a walk with no reader. */
const SIMULATED_TAGS: readonly SimulatedTag[] = [
  { uid: "04A1B2C3D4E580", url: null },
  { uid: "04A1B2C3D4E581", url: null },
  { uid: "04A1B2C3D4E582", url: null },
  // The one whose write silently fails, so the degraded path is reachable in a
  // demo rather than only in a test.
  { uid: "04A1B2C3D4E583", url: null, writeFails: true },
];

function useTagSource(
  choice: ReaderChoice,
  bridge: readonly BridgeDevice[],
  connection: BridgeConnection | null,
): TagSource {
  return useMemo(() => {
    if (choice.startsWith("bridge:") && connection !== null) {
      const deviceId = choice.slice("bridge:".length);
      const device = bridge.find((candidate) => candidate.deviceId === deviceId);
      if (device !== undefined) {
        return bridgeSource(connection, device);
      }
      // Unplugged mid-walk. Falling back to typing keeps the walk usable rather
      // than leaving a dead source subscribed to nothing — but it must be said
      // out loud: the radio has gone from the picker, so nothing is checked, and
      // a walk that has quietly become "type the hex off the sticker" without
      // announcing it is how a person ends up thinking the reader is broken.
      // `lostReader` below renders that.
      return manualTagSource();
    }
    if (choice === "simulated") {
      return simulatedTagSource(SIMULATED_TAGS);
    }
    if (choice === "webnfc") {
      return webNfcTagSource();
    }
    return manualTagSource();
  }, [choice, bridge, connection]);
}

/**
 * What each `not_restored_reason` means at the cabinet, in a sentence that names
 * the next action.
 *
 * The undo response is the one place this walk admits it did less than the word
 * "undone" implies, and it used to render the raw token — "Undone, with a caveat
 * / prior_slot_rebound" names nothing and suggests nothing. A refusal with no
 * path forward is the failure mode this whole feature is built to avoid.
 */
const UNDO_CAVEATS: Record<string, string> = {
  prior_slot_rebound:
    "The tag that used to be here could not be put back: its old slot has been " +
    "given a different tag since. Unbind that one first if you want the original back.",
  prior_tag_bound_elsewhere:
    "The tag that used to be here is now stuck on another container, so putting it " +
    "back would mean one tag answering for two places. Go and unbind it there first.",
  slot_rebound_since:
    "Nothing was removed: something else has already bound this slot to a different " +
    "tag. The sticker on the drawer is still doing its job — leave it where it is.",
};

/** Falls back to the token rather than to silence: an unmapped reason is a bug,
 *  and hiding it would make the undo look clean when it was not. */
function undoCaveat(reason: string): string {
  return UNDO_CAVEATS[reason] ?? reason;
}

// ------------------------------------------------------------- the panel ----

export function TagWalkDialog({
  location,
  kind,
  onClose,
  onChanged,
  /** Switches this dialog to the verification walk once every slot is bound.
   *  Optional because the verify dialog itself has nothing to hand on to. */
  onVerifyNext,
  /** Injected by the tests, which drive a whole cabinet through a fake reader. */
  source: injected,
}: {
  location: LocationRead;
  kind: WalkKind;
  onClose: () => void;
  onChanged: () => void;
  onVerifyNext?: () => void;
  source?: TagSource;
}) {
  return (
    <Dialog
      title={kind === "provision" ? "Bind tags to these slots" : "Verify the tags on these slots"}
      onClose={onClose}
      note={
        kind === "provision"
          ? "Stick every tag on first, then walk the cabinet tapping them in order. " +
            "The cursor advances by itself."
          : "Re-read every tag in order. Nothing is repaired here — a mismatch is " +
            "recorded and named, and you decide what actually happened."
      }
    >
      <TagWalk
        location={location}
        kind={kind}
        onChanged={onChanged}
        {...(onVerifyNext === undefined ? {} : { onVerifyNext })}
        {...(injected === undefined ? {} : { source: injected })}
      />
    </Dialog>
  );
}

export function TagWalk({
  location,
  kind,
  onChanged,
  onVerifyNext,
  source: injected,
}: {
  location: LocationRead;
  kind: WalkKind;
  onChanged: () => void;
  onVerifyNext?: () => void;
  source?: TagSource;
}) {
  const capabilities = useMemo(() => detectCapabilities(), []);
  const simulationAllowed = useMemo(
    () => new URLSearchParams(window.location.search).get("sim") === "1",
    [],
  );
  const [choice, setChoice] = useState<ReaderChoice>(() =>
    capabilities.nfc ? "webnfc" : "manual",
  );
  // Whether the operator has said which reader they want. Until they have, an
  // arriving bridged reader may take over from "Type the UID"; after they have,
  // nothing moves under them mid-walk.
  const [readerPicked, setReaderPicked] = useState(false);
  const { devices: bridgeDevices, connection: bridge } = useBridgeDevices();

  // A bridged reader appearing is the only thing that can change the choice on
  // its own, and only away from the fallback. ADR 0003's rule applied to a
  // chooser: the radio exists because a `device.attached` arrived. On the bench
  // kiosk this is what turns "type the hex off the sticker" into "tap the tag".
  useEffect(() => {
    if (readerPicked || bridgeDevices.length === 0) {
      return;
    }
    if (choice === "manual") {
      // Prefer one that can write. A read-only reader binds perfectly well but
      // leaves every sticker `unverified`, and roster order is arrival order —
      // so with two readers attached the operator would silently get the worse
      // one about half the time.
      const writable = bridgeDevices.find((device) => device.capabilities.writesNdef);
      setChoice(bridgeChoice((writable ?? bridgeDevices[0]!).deviceId));
    }
  }, [bridgeDevices, choice, readerPicked]);

  const detected = useTagSource(choice, bridgeDevices, bridge);

  // The chosen bridged reader is no longer in the roster: it was unplugged, or
  // its app was closed on the device.
  const lostReader =
    choice.startsWith("bridge:") &&
    !bridgeDevices.some((device) => bridgeChoice(device.deviceId) === choice);
  const source = injected ?? detected;

  const [cursor, setCursor] = useState<SlotCursorRead | null>(null);
  const [provision, setProvision] = useState<ProvisioningState | null>(null);
  const [verify, setVerify] = useState<VerificationState | null>(null);
  const [conflict, setConflict] = useState<{ uid: string; conflict: ConflictRead } | null>(null);
  const [overwrite, setOverwrite] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [flash, setFlash] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [starting, setStarting] = useState(true);

  const feedback = useMemo(() => new DecodeFeedback(), []);
  const deviceId = useMemo(() => uuid4(), []);
  // Typing a UID is a path in *every* walk, whichever reader is selected — there
  // is no reader on this setup at all today, and a walk only exercisable with
  // hardware nobody has is a walk nobody can check. It feeds the same listener a
  // real tap does, so it is not a second implementation of the flow.
  const manual = useMemo(() => manualTagSource(), []);

  // Everything the tap handler needs, in a ref rather than a dependency list.
  // The subscription must be set up once — re-subscribing on every state change
  // would drop the reader mid-walk and, with Web NFC, cost a permission prompt.
  /**
   * **Derived, never held in state of its own.** It was a `useState` fed by an
   * effect, and that cost a render: for one commit after the walk loaded, the
   * cursor was on screen and the session id was still null, so the very first tap
   * — the one a person makes the instant the screen appears — was silently
   * dropped. A derivation is available in the same render as the cursor it came
   * with, which is the only ordering that cannot lose a tap.
   */
  const sessionId = (kind === "provision" ? provision?.session?.id : verify?.session?.id) ?? null;

  const live = useRef({ sessionId, cursor, overwrite, busy, source });
  live.current = { sessionId, cursor, overwrite, busy, source };

  const applyProvision = useCallback((next: ProvisioningState) => {
    setProvision(next);
    setCursor(next.cursor);
  }, []);

  const applyVerify = useCallback((next: VerificationState) => {
    setVerify(next);
    setCursor(next.cursor);
  }, []);

  // --- starting or resuming ------------------------------------------------

  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        if (kind === "provision") {
          // Resume first. There is no cursor to restore, so this is free — and it
          // is what makes a killed browser cost nothing.
          const current = await getCurrentProvisioningSession(location.id);
          const state =
            current.session === null
              ? (
                  await startProvisioningSession(location.id, {
                    device_kind: source.kind,
                    client_op_id: uuid4(),
                    device_id: deviceId,
                  })
                ).state
              : current;
          if (live) {
            applyProvision(state);
          }
        } else {
          const started = await startVerificationSession(location.id, {
            device_kind: source.kind,
            client_op_id: uuid4(),
            device_id: deviceId,
          });
          if (live) {
            applyVerify(started.state);
          }
        }
      } catch (cause) {
        if (live) {
          setError(cause);
        }
      } finally {
        if (live) {
          setStarting(false);
        }
      }
    })();
    return () => {
      live = false;
    };
    // Deliberately once per open: restarting a walk because the reader changed
    // would abandon a half-finished cabinet.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.id, kind]);

  // --- one tap -------------------------------------------------------------

  const bindHere = useCallback(
    async (uid: string, options: { move: boolean }) => {
      const walk = live.current.sessionId;
      const slot = live.current.cursor;
      if (walk === null || slot === null) {
        return;
      }
      setBusy(true);
      setError(null);
      try {
        const result = await bindTag(walk, {
          tag_uid: uid,
          location_id: slot.location_id,
          move: options.move,
          client_op_id: uuid4(),
          device_id: deviceId,
        });
        if (result.conflict !== null && result.tag === null) {
          setConflict({ uid, conflict: result.conflict });
          setOutcome(null);
          return;
        }
        applyProvision(result.state);
        setConflict(null);
        onChanged();

        const tag = result.tag;
        if (tag === null) {
          return;
        }
        // The write is the second half of the step, and it is the half the
        // server cannot do. A reader that cannot write leaves the binding
        // `unverified`, which is the honest state, not a failure.
        if (live.current.source.write === undefined) {
          setOutcome({
            tone: "ok",
            title: `Bound ${slot.slot_label ?? slot.name}`,
            detail:
              "This reader cannot write, so the tag itself was not touched. Its " +
              "URL stays whatever is already on it.",
          });
          return;
        }
        try {
          const back = await live.current.source.write(tag.ndef_url, {
            overwrite: live.current.overwrite,
          });
          if (!back.observed) {
            // The tag never left the field, so no fresh reading arrived. That
            // says nothing about the write; reporting it as a failure would send
            // someone rewriting a tag that is almost certainly fine.
            setOutcome({
              tone: "info",
              title: `Bound ${slot.slot_label ?? slot.name}`,
              detail:
                "Written, but the tag did not read back before it left the field. " +
                "The verification walk will settle it.",
            });
            return;
          }
          const written = await recordTagWriteResult(tag.id, {
            read_back_url: back.url,
            client_op_id: uuid4(),
            device_id: deviceId,
          });
          setOutcome(
            written.verified
              ? {
                  tone: "ok",
                  title: `Bound and written: ${slot.slot_label ?? slot.name}`,
                  detail: "Read back and matched. Tapping this drawer opens its page.",
                }
              : { tone: "warn", title: "The tag did not take the write", detail: DEGRADED_DETAIL },
          );
        } catch (cause) {
          if (cause instanceof TagNotBlankError) {
            setOutcome({
              tone: "warn",
              title: "Bound, but the tag already had a record",
              detail:
                "Nothing was written to it. Turn on “replace what is on the tag” " +
                "and tap it again if this really is a tag being reused.",
            });
            return;
          }
          setError(cause);
        }
      } catch (cause) {
        setError(cause);
      } finally {
        setBusy(false);
      }
    },
    [applyProvision, deviceId, onChanged],
  );

  const checkHere = useCallback(
    async (tap: TagPresentation) => {
      const walk = live.current.sessionId;
      const slot = live.current.cursor;
      if (walk === null || slot === null || tap.uid === null) {
        return;
      }
      setBusy(true);
      setError(null);
      try {
        const result = await checkTag(walk, {
          tag_uid: tap.uid,
          location_id: slot.location_id,
          ndef_url: tap.url,
          carries_ndef: tap.carriesNdef,
          client_op_id: uuid4(),
          device_id: deviceId,
        });
        applyVerify(result.state);
        if (result.status === "match") {
          setOutcome(
            result.ndef_state === "degraded"
              ? { tone: "warn", title: `${slot.slot_label ?? slot.name}: right tag, dead URL`, detail: DEGRADED_DETAIL }
              : {
                  tone: "ok",
                  title: `${slot.slot_label ?? slot.name} checks out`,
                  detail: "Same tag as when it was bound.",
                },
          );
          return;
        }
        const found = result.mismatch;
        setOutcome({
          tone: "warn",
          title: `${slot.slot_label ?? slot.name}: wrong tag`,
          detail: describeMismatch(found),
        });
      } catch (cause) {
        setError(cause);
      } finally {
        setBusy(false);
      }
    },
    [applyVerify, deviceId],
  );

  // --- the reader ----------------------------------------------------------

  useEffect(() => {
    const flashTimers = new Set<number>();
    const onTap = debounceTaps((tap) => {
      if (live.current.busy || tap.uid === null) {
        return;
      }
      // Fires before the outcome is known — the case where reassurance matters
      // most is the tap that turns out to resolve to nothing.
      feedback.fire(tap.uid);
      setFlash(true);
      // Tracked so unmounting mid-flash cannot fire `setFlash` into a component
      // that is gone — closing the panel right after a tap is the ordinary way to
      // finish, not an edge case.
      flashTimers.add(window.setTimeout(() => setFlash(false), FEEDBACK_FLASH_MS));
      if (kind === "provision") {
        void bindHere(tap.uid, { move: false });
      } else {
        void checkHere(tap);
      }
    });
    const unsubscribeManual = manual.subscribe(onTap);
    let unsubscribeReader = (): void => undefined;
    try {
      unsubscribeReader = source.subscribe(onTap);
    } catch (cause) {
      // Selecting a reader the browser does not have is the user's mistake to
      // see, not a crash: the typed path above still works.
      setError(cause);
    }
    return () => {
      unsubscribeManual();
      unsubscribeReader();
      for (const timer of flashTimers) {
        window.clearTimeout(timer);
      }
    };
  }, [source, manual, kind, bindHere, checkHere, feedback]);

  // --- rendering -----------------------------------------------------------

  if (starting) {
    return <Loading what="the walk" />;
  }

  const progress = kind === "provision" ? provision?.progress : null;
  const verifyProgress = verify?.progress;
  const notice = choice === "webnfc" ? nfcNotice(capabilities) : null;

  return (
    <div className="stack">
      <ReaderPicker
        choice={choice}
        onChoose={(next) => {
          // An explicit pick pins it: nothing moves under the operator's hand
          // for the rest of the walk.
          setReaderPicked(true);
          setChoice(next);
        }}
        canUseNfc={capabilities.nfc}
        simulationAllowed={simulationAllowed}
        disabled={injected !== undefined}
        bridgeDevices={bridgeDevices}
      />
      {lostReader && (
        <Notice kind="warn" title="That reader has gone">
          <p style={{ margin: 0 }}>
            The reader you were using is no longer attached — a cable, or its app closed on
            the device. Taps will not arrive until you pick another reader above; typing the
            UID still works and the walk keeps its place.
          </p>
        </Notice>
      )}
      {notice !== null && (
        <Notice kind="warn" title="No Web NFC here">
          <p style={{ margin: 0 }}>{notice}</p>
        </Notice>
      )}
      {choice === "simulated" && injected === undefined && (
        <Notice kind="warn" title="Simulated reader — no hardware is involved">
          <p style={{ margin: 0 }}>
            Every tap below invents a UID and binds it to a real slot. Useful for seeing the
            walk work; a mess to unpick if you leave it bound to a drawer that later gets a
            real tag. Unbind what you bind.
          </p>
        </Notice>
      )}

      <div className={flash ? "card flash" : "card"} aria-live="polite">
        <h3>{kind === "provision" ? "Bind" : "Check"}</h3>
        {cursor === null ? (
          <Notice kind="ok" title={kind === "provision" ? "Every slot has a tag" : "Every tag checked"}>
            <p style={{ margin: 0 }}>
              {kind === "provision"
                ? "Nothing left to bind in this container."
                : "The walk has been all the way round."}
            </p>
            {/* PLAN.md: a provisioning pass is "always followed by a verification
                pass", and that one is "not optional busywork". Finishing the binds
                and then offering no route to it left the walk ending on a success
                message — the commonest dead end there is, and the one place the
                next step is not a matter of taste. */}
            {kind === "provision" && onVerifyNext !== undefined && (
              <div className="row">
                <button type="button" className="primary" onClick={onVerifyNext}>
                  Verify these tags now
                </button>
              </div>
            )}
          </Notice>
        ) : (
          <>
            <p className="big-number" style={{ margin: 0 }}>
              {cursor.slot_label ?? cursor.name}
            </p>
            <p className="muted-note" style={{ margin: 0 }}>
              {cursor.label_path}
              {cursor.short_id === null ? "" : ` · ${cursor.short_id}`}
            </p>
            <p style={{ margin: 0 }}>
              {kind === "provision"
                ? "Tap the tag already stuck to this drawer."
                : "Tap this drawer’s tag."}
            </p>
          </>
        )}
      </div>

      {progress !== undefined && progress !== null && (
        <p className="muted-note" data-testid="provision-progress">
          {progress.bound} of {progress.total_slots} bound
          {progress.skipped > 0 ? `, ${progress.skipped} skipped` : ""}
        </p>
      )}
      {verifyProgress !== undefined && (
        <p className="muted-note" data-testid="verify-progress">
          {verifyProgress.checked} of {verifyProgress.total_tagged} checked,{" "}
          {verifyProgress.mismatches} mismatched
        </p>
      )}

      {outcome !== null && (
        <Notice
          kind={outcome.tone === "warn" ? "warn" : outcome.tone === "ok" ? "ok" : "info"}
          title={outcome.title}
        >
          <p style={{ margin: 0 }}>{outcome.detail}</p>
        </Notice>
      )}

      <ErrorBanner error={error} fallback="That tap did not go through." />

      {conflict !== null && (
        <Notice kind="warn" title={`Already bound to ${conflict.conflict.label_path}`}>
          <p style={{ margin: 0 }}>
            That tag is on another drawer’s record. Moving it here is right if you moved the
            sticker; cancel if you tapped the wrong drawer.
          </p>
          <div className="row">
            <button
              type="button"
              className="primary"
              disabled={busy}
              onClick={() => void bindHere(conflict.uid, { move: true })}
            >
              Move here
            </button>
            <button type="button" disabled={busy} onClick={() => setConflict(null)}>
              Cancel
            </button>
          </div>
        </Notice>
      )}

      {kind === "provision" && (
        <ProvisionControls
          source={source}
          overwrite={overwrite}
          onOverwrite={setOverwrite}
          busy={busy}
          canUndo={(provision?.undo_depth ?? 0) > 0}
          undoLabel={provision?.undo_label ?? null}
          canSkip={cursor !== null}
          onSkip={() => {
            const walk = sessionId;
            const slot = cursor;
            if (walk === null || slot === null) {
              return;
            }
            setBusy(true);
            void skipSlot(walk, {
              location_id: slot.location_id,
              client_op_id: uuid4(),
              device_id: deviceId,
            })
              .then((result) => applyProvision(result.state))
              .catch(setError)
              .finally(() => setBusy(false));
          }}
          onUndo={() => {
            const walk = sessionId;
            if (walk === null) {
              return;
            }
            setBusy(true);
            void undoProvisioningAction(walk, { client_op_id: uuid4(), device_id: deviceId })
              .then((result) => {
                applyProvision(result.state);
                onChanged();
                if (result.not_restored_reason !== null) {
                  setOutcome({
                    tone: "info",
                    title: "Undone, with a caveat",
                    detail: undoCaveat(result.not_restored_reason),
                  });
                }
              })
              .catch(setError)
              .finally(() => setBusy(false));
          }}
        />
      )}

      <ManualEntry
        onSubmit={(uid) => manual.present(uid)}
        disabled={busy || cursor === null}
        label={kind === "provision" ? "Bind this UID" : "Check this UID"}
      />

      {verify !== null && verify.mismatches.length > 0 && (
        <MismatchList mismatches={verify.mismatches} />
      )}
    </div>
  );
}

function describeMismatch(found: MismatchRead | null): string {
  if (found === null) {
    return "The tag on this drawer is not the one bound to it.";
  }
  const belongs =
    found.scanned_resolved_label_path === null
      ? "That tag is not bound to anything."
      : `That tag belongs to ${found.scanned_resolved_label_path}.`;
  return (
    `Expected ${found.expected_tag_uid ?? "no tag"}, read ${found.scanned_tag_uid}. ` +
    `${belongs} Nothing has been changed — decide whether the drawers were swapped ` +
    "or the tag was stuck on the wrong one."
  );
}

// ------------------------------------------------------------ sub-panels ----

function ReaderPicker({
  choice,
  onChoose,
  canUseNfc,
  simulationAllowed,
  disabled,
  bridgeDevices,
}: {
  choice: ReaderChoice;
  onChoose: (next: ReaderChoice) => void;
  canUseNfc: boolean;
  simulationAllowed: boolean;
  disabled: boolean;
  bridgeDevices: readonly BridgeDevice[];
}) {
  if (disabled) {
    return null;
  }
  return (
    <fieldset className="row" style={{ border: 0, padding: 0, margin: 0 }}>
      <legend className="muted-note">Reader</legend>
      <label>
        <input
          type="radio"
          name="reader"
          checked={choice === "webnfc"}
          disabled={!canUseNfc}
          onChange={() => onChoose("webnfc")}
        />{" "}
        Phone (Web NFC)
      </label>
      {/* One per attached device, and nothing at all when none are — a bridge
        * that is not running must not leave an empty affordance behind. Labelled
        * with the device's own name, because "Flipper Vyvern" is answerable at a
        * bench and a udev path is not. */}
      {bridgeDevices.map((device) => (
        <label key={device.deviceId}>
          <input
            type="radio"
            name="reader"
            checked={choice === bridgeChoice(device.deviceId)}
            onChange={() => onChoose(bridgeChoice(device.deviceId))}
          />{" "}
          {device.label}
          {device.capabilities.writesNdef ? "" : " (reads only)"}
        </label>
      ))}
      <label>
        <input
          type="radio"
          name="reader"
          checked={choice === "manual"}
          onChange={() => onChoose("manual")}
        />{" "}
        Type the UID
      </label>
      {simulationAllowed && (
        <label>
          <input
            type="radio"
            name="reader"
            checked={choice === "simulated"}
            onChange={() => onChoose("simulated")}
          />{" "}
          Simulated
        </label>
      )}
    </fieldset>
  );
}

function ProvisionControls({
  source,
  overwrite,
  onOverwrite,
  busy,
  canUndo,
  undoLabel,
  canSkip,
  onSkip,
  onUndo,
}: {
  source: TagSource;
  overwrite: boolean;
  onOverwrite: (next: boolean) => void;
  busy: boolean;
  canUndo: boolean;
  undoLabel: string | null;
  canSkip: boolean;
  onSkip: () => void;
  onUndo: () => void;
}) {
  return (
    <div className="row">
      {/* `canSkip` is false at a null cursor: with the walk finished there is no
          slot to skip, and the handler silently returned — a live control that
          does nothing, one-handed, at a cabinet. */}
      <button type="button" disabled={busy || !canSkip} onClick={onSkip}>
        Skip this slot
      </button>
      <button type="button" disabled={busy || !canUndo} onClick={onUndo}>
        {undoLabel === null ? "Undo" : `Undo ${undoLabel}`}
      </button>
      {source.canWrite && (
        <label title="Off by default so a screen left open cannot quietly overwrite a provisioned drawer.">
          <input
            type="checkbox"
            checked={overwrite}
            onChange={(event) => onOverwrite(event.target.checked)}
          />{" "}
          Replace what is on the tag
        </label>
      )}
    </div>
  );
}

/** The typed-UID field. Feeds the walk's own manual source, never a second flow. */
function ManualEntry({
  onSubmit,
  disabled,
  label,
}: {
  onSubmit: (uid: string) => void;
  disabled: boolean;
  label: string;
}) {
  const [uid, setUid] = useState("");

  const present = (): void => {
    const trimmed = uid.trim();
    if (trimmed === "") {
      return;
    }
    onSubmit(trimmed);
    setUid("");
  };

  return (
    <form
      className="row"
      onSubmit={(event) => {
        event.preventDefault();
        present();
      }}
    >
      <label className="field" style={{ flex: 1 }}>
        <span>Tag UID</span>
        <input
          className="mono"
          value={uid}
          onChange={(event) => setUid(event.target.value)}
          placeholder="04:1A:2B:3C:4D:5E:6F"
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      <button type="submit" disabled={disabled || uid.trim() === ""}>
        {label}
      </button>
    </form>
  );
}

function MismatchList({ mismatches }: { mismatches: readonly MismatchRead[] }) {
  return (
    <div className="card">
      <h3>Mismatches</h3>
      <p className="muted-note" style={{ margin: 0 }}>
        Recorded, not repaired. Each one names where the tag you read actually belongs, so a
        straight swap is obvious; nothing is rebound until you say so.
      </p>
      <ul>
        {mismatches.map((found) => (
          <li key={found.id}>
            <strong>{found.label_path}</strong> — read{" "}
            <span className="mono">{found.scanned_tag_uid}</span>,{" "}
            {found.scanned_resolved_label_path === null
              ? "which is bound to nothing"
              : `which belongs to ${found.scanned_resolved_label_path}`}
            {found.resolved_at === null ? "" : " (since resolved)"}
          </li>
        ))}
      </ul>
    </div>
  );
}
