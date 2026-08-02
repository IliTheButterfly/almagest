/**
 * "Carry on with this on your phone."
 *
 * > *it would be cool to be able to transfer a session to a phone by scanning a
 * > qr code. That way I can go off with my phone and scan the containers as I pick
 * > them up to verify if they are correct*
 *
 * **Nothing is transferred, because there is nothing to transfer.** A pick walk's
 * progress lives in the ledger as each take is recorded, and its position is
 * derived from what is left — the same principle as the provisioning cursor being
 * `MIN(sort_order)` among untagged slots rather than a stored number. So the
 * handoff is a deep link and no more: scan it, the phone opens the same walk, and
 * both screens stay correct because both are reading the same rows. There is no
 * token to expire and no state to reconcile if the desktop is left open.
 *
 * The QR is rendered server-side (`/api/handoff/qr.svg`) rather than by a
 * JavaScript encoder, because the backend already carries `segno` for label cards
 * and the base URL is a server-side setting (ADR 0001) that the browser should not
 * be reconstructing.
 *
 * **The phone must be on the LAN and trust the private CA** — the same
 * prerequisite the tags themselves have, since `https://almagest.lan` is what is
 * written to every one of them. Said out loud here, because "the QR does nothing"
 * on an untrusted phone is otherwise a mystery.
 */

import { useState } from "react";

import { Notice } from "./Feedback";

export function HandoffQr({ path, what }: { path: string; what: string }) {
  const [open, setOpen] = useState(false);
  const [failed, setFailed] = useState(false);

  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)}>
        Continue on my phone…
      </button>
    );
  }

  return (
    <div className="card">
      <div className="row">
        <h3 style={{ flex: 1 }}>Continue on your phone</h3>
        <button type="button" onClick={() => setOpen(false)}>
          Hide
        </button>
      </div>
      <p className="muted-note" style={{ margin: 0 }}>
        Scan with the phone&apos;s camera to open {what} there. Nothing moves — both screens
        show the same walk, and either can record a take.
      </p>
      {/* `onError` because an <img> whose src answers 4xx/5xx renders a broken
       * icon and nothing else: the body is unreachable from an image load, so the
       * API's carefully worded refusal — 503 naming the missing `labels` extra and
       * the command that installs it — would land in a network tab nobody at a
       * bench opens. The failure has to be visible on the screen the operator is
       * looking at, even when the exact reason cannot be. */}
      {failed ? (
        <Notice kind="warn" title="The QR code could not be drawn">
          <p style={{ margin: 0 }}>
            This install may be missing the optional <code>labels</code> extra
            (<code>uv sync --extra labels</code>); <code>/api/handoff/qr.svg</code> answers with
            the reason. You can still open <code>{path}</code> on the phone by typing it.
          </p>
        </Notice>
      ) : (
        <img
          src={`/api/handoff/qr.svg?path=${encodeURIComponent(path)}`}
          alt={`QR code opening ${path}`}
          width={220}
          height={220}
          onError={() => setFailed(true)}
          style={{ imageRendering: "pixelated", maxWidth: "100%", height: "auto" }}
        />
      )}
      <p className="muted-note" style={{ margin: 0 }}>
        The phone has to be on the same network and trust this site&apos;s certificate — the
        same requirement the tags have, since that host is what is written on them.
      </p>
    </div>
  );
}
