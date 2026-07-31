/**
 * "Scan the container — is this the right one?"
 *
 * The daily use of a tag, as opposed to the one-off walks in `TagWalkPanel`. Both
 * of Iliana's workflows are the same shape and this is that shape, once:
 *
 * > *I look at the list and go grab all the containers I need. I sit down at the
 * > desk, scan the first container. A confirmation or error message shows if I got
 * > the right container.*
 *
 * > *I got some new parts, I scan them, I see the reference and am directed to take
 * > the right container. I scan it and it confirms if I got the right one.*
 *
 * **The expectation comes first, and that is what makes this different from
 * `/scan`.** The scan screen asks "what is this?" and goes wherever the answer
 * leads. This asks "is this the thing I was sent to get?", which has three
 * answers, and the wrong-container one is the whole reason it exists — a pick
 * from the drawer next to the right one is the error no amount of care prevents
 * and no ledger row records. So a mismatch **names what you actually scanned**,
 * because "wrong drawer" is not actionable and "that is Cabinet B / 03, you want
 * Cabinet A / 07" is.
 *
 * **A scan is never rejected** (CLAUDE.md). A wrong container does not block the
 * step; it warns, and the override stays available, because the user is holding
 * the drawer and the database is not. Blocking teaches people to stop scanning,
 * and then nothing is checked at all.
 *
 * **An unknown tag is an opportunity, not a dead end.** A container with no
 * binding is the ordinary state of a drawer nobody has provisioned yet, so the
 * answer is "bind this tag to the container you were sent to" — the provisioning
 * walk's single-slot form, offered exactly when the person is standing at the
 * right drawer holding the tag.
 */

import { useEffect, useMemo, useState } from "react";

import { ErrorBanner, Notice } from "./Feedback";
import { resolveLocationTag, resolveShortId } from "../lib/api/client";
import { detectCapabilities } from "../lib/capabilities";
import { DecodeFeedback, FEEDBACK_FLASH_MS } from "../lib/scan/feedback";
import { normalizeShortId } from "../lib/shortid";
import {
  combineSources,
  debounceTaps,
  manualTagSource,
  webNfcTagSource,
  type TagPresentation,
  type TagSource,
} from "../lib/tags/source";
import { wedgeTagSource } from "../lib/tags/wedge";

export interface ExpectedContainer {
  readonly locationId: number;
  readonly labelPath: string;
  readonly shortId: string | null;
}

/** What the last scan said about the container in hand. */
export type ScanVerdict =
  | { readonly kind: "idle" }
  | { readonly kind: "right"; readonly labelPath: string }
  | {
      readonly kind: "wrong";
      readonly labelPath: string;
      readonly locationId: number;
    }
  /** A tag nothing is bound to — usually an unprovisioned drawer. */
  | { readonly kind: "unbound"; readonly uid: string | null }
  /** The tag's payload names one container and its UID is bound to another. */
  | { readonly kind: "disagreement"; readonly labelPath: string };

export function ConfirmScan({
  expected,
  onConfirmed,
  source: injected,
  /** Rendered under the verdict — the quantity step, in both workflows. */
  children,
}: {
  expected: ExpectedContainer;
  onConfirmed: (verdict: ScanVerdict) => void;
  source?: TagSource;
  children?: React.ReactNode;
}) {
  const capabilities = useMemo(() => detectCapabilities(), []);
  const manual = useMemo(() => manualTagSource(), []);
  /**
   * Every reader at once, and **never gated on `NDEFReader`**.
   *
   * A USB reader is a keyboard, so its presence cannot be probed for: the wedge
   * listener is always installed, and on a desktop Chromium with a Flipper on the
   * end of a cable it is the reader that actually works. Web NFC joins when the
   * browser has it. Telling someone "this browser has no NFC" while a reader sits
   * plugged into the same machine is worse than saying nothing.
   */
  const detected = useMemo(
    () =>
      combineSources(
        ...(capabilities.nfc ? [webNfcTagSource()] : []),
        wedgeTagSource(),
      ),
    [capabilities.nfc],
  );
  const source = injected ?? detected;

  const [verdict, setVerdict] = useState<ScanVerdict>({ kind: "idle" });
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [flash, setFlash] = useState(false);
  const feedback = useMemo(() => new DecodeFeedback(), []);

  useEffect(() => {
    // A new expectation is a new question; keeping the previous green tick would
    // be the single most dangerous stale state on this screen.
    setVerdict({ kind: "idle" });
  }, [expected.locationId]);

  useEffect(() => {
    const flashTimers = new Set<number>();
    const handle = debounceTaps((tap: TagPresentation) => {
      feedback.fire(tap.uid ?? tap.url ?? "");
      setFlash(true);
      // Tracked so unmounting mid-flash cannot fire `setFlash` into a component
      // that is gone — closing the panel right after a tap is the ordinary way to
      // finish, not an edge case.
      flashTimers.add(window.setTimeout(() => setFlash(false), FEEDBACK_FLASH_MS));
      void judge(tap);
    });
    const stopManual = manual.subscribe(handle);
    let stopReader = (): void => undefined;
    try {
      stopReader = source?.subscribe(handle) ?? (() => undefined);
    } catch (cause) {
      setError(cause);
    }
    return () => {
      stopManual();
      stopReader();
      for (const timer of flashTimers) {
        window.clearTimeout(timer);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source, manual, expected.locationId]);

  async function judge(tap: TagPresentation): Promise<void> {
    if (tap.uid === null && tap.url === null) {
      // A wedge read: it carries what the tag *means*, not what it is, so the
      // short-id lookup is the whole answer rather than a fallback.
      await judgeShortId(tap.shortId);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const resolved = await resolveLocationTag({
        ...(tap.uid === null ? {} : { tag_uid: tap.uid }),
        ...(tap.url === null ? {} : { ndef_url: tap.url }),
      });
      settle(
        resolved.location === null
          ? { kind: "unbound", uid: tap.uid }
          : resolved.disagreement
            ? { kind: "disagreement", labelPath: resolved.location.label_path }
            : resolved.location.location_id === expected.locationId
              ? { kind: "right", labelPath: resolved.location.label_path }
              : {
                  kind: "wrong",
                  labelPath: resolved.location.label_path,
                  locationId: resolved.location.location_id,
                },
      );
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  /** The printed card, and what a USB wedge emits. */
  async function judgeShortId(raw: string | null): Promise<void> {
    const code = normalizeShortId(raw ?? "");
    if (code === "") {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const resolved = await resolveShortId(code);
      const target = resolved.target;
      if (target === null || target === undefined || target.entity_type !== "location") {
        settle({ kind: "unbound", uid: null });
        return;
      }
      settle(
        target.entity_pk === expected.locationId
          ? { kind: "right", labelPath: target.label_path ?? target.label }
          : {
              kind: "wrong",
              labelPath: target.label_path ?? target.label,
              locationId: target.entity_pk,
            },
      );
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  function settle(next: ScanVerdict): void {
    setVerdict(next);
    if (next.kind === "right") {
      onConfirmed(next);
    }
  }


  return (
    <div className={flash ? "card flash" : "card"}>
      <h3>Scan the container</h3>
      <p className="muted-note" style={{ margin: 0 }}>
        Expecting <strong>{expected.labelPath}</strong>
        {expected.shortId === null ? "" : ` · ${expected.shortId}`}
      </p>

      <div aria-live="polite">
        <Verdict verdict={verdict} expected={expected} busy={busy} />
      </div>

      <p className="muted-note" style={{ margin: 0 }}>
        Tap it with the phone, read it with a USB reader, or type what is on the label.
        {capabilities.nfc ? "" : " (This browser has no Web NFC — a plugged-in reader still works.)"}
      </p>

      <form
        className="row"
        onSubmit={(event) => {
          event.preventDefault();
          // One field, two payloads: a tag UID and a printed short ID are both
          // things a person can read off a drawer, and making the user pick which
          // box to type in is a decision they should not have to make. Shape
          // decides — a short ID is eight Crockford symbols, a UID is hex and
          // longer.
          const entered = typed;
          setTyped("");
          if (looksLikeUid(entered)) {
            manual.present(entered);
          } else {
            void judgeShortId(entered);
          }
        }}
      >
        <label className="field" style={{ flex: 1 }}>
          <span>Or type the tag UID or printed code</span>
          <input
            className="mono"
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            placeholder="4K7T-92M8"
            autoComplete="off"
            autoCapitalize="characters"
            spellCheck={false}
          />
        </label>
        <button type="submit" disabled={busy || typed.trim() === ""}>
          Check
        </button>
      </form>

      <ErrorBanner error={error} fallback="That scan did not go through." />

      {children}
    </div>
  );
}

/** Hex and longer than a short ID: a UID, not a printed code. */
function looksLikeUid(raw: string): boolean {
  const squashed = raw.replace(/[\s:-]+/g, "");
  return squashed.length > 8 && /^[0-9a-fA-F]+$/.test(squashed);
}

function Verdict({
  verdict,
  expected,
  busy,
}: {
  verdict: ScanVerdict;
  expected: ExpectedContainer;
  busy: boolean;
}) {
  if (busy) {
    return <p className="dim">Checking…</p>;
  }
  switch (verdict.kind) {
    case "idle":
      return (
        <p className="muted-note" style={{ margin: 0 }}>
          Nothing scanned yet.
        </p>
      );
    case "right":
      return (
        <Notice kind="ok" title="Right container">
          <p style={{ margin: 0 }}>{verdict.labelPath}</p>
        </Notice>
      );
    case "wrong":
      return (
        <Notice kind="warn" title="That is a different container">
          <p style={{ margin: 0 }}>
            You scanned <strong>{verdict.labelPath}</strong>. You want{" "}
            <strong>{expected.labelPath}</strong>.
          </p>
          <p className="muted-note" style={{ margin: 0 }}>
            Nothing is blocked — if you really did take from the one in your hand, carry on
            and the quantity below is recorded against it.
          </p>
        </Notice>
      );
    case "disagreement":
      return (
        <Notice kind="warn" title="That tag contradicts itself">
          <p style={{ margin: 0 }}>
            Its URL names one container and its UID is bound to another ({verdict.labelPath}).
            That is a mis-bound tag: run the verification walk on this cabinet before trusting
            it.
          </p>
        </Notice>
      );
    case "unbound":
      return (
        <Notice kind="info" title="Nothing is bound to that tag">
          <p style={{ margin: 0 }}>
            Normal for a drawer nobody has provisioned. Bind it from the container&apos;s edit
            mode — “Bind NFC tags” — while you are standing at it.
          </p>
        </Notice>
      );
  }
}
