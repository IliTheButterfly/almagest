/**
 * Captures — the photographs, kept, and readable again later.
 *
 * The whole justification for storing a capture rather than just its decoded
 * values is that the *frame* answers questions the values cannot: what the
 * packaging looked like, what the smudged fourth line said, whether the reel was
 * already open. That is only worth anything if there is a way back to it, which
 * is what this screen is.
 *
 * **The regions come back from the server, not from a re-read.** The outlines
 * were computed once, on the device that held the pixels, and stored with the
 * image; drawing them here is a matter of scaling stored quads by the stored
 * dimensions. Re-running OCR on a saved capture would be slower, would need the
 * model on a desktop that may never have loaded it, and — worst — could produce
 * *different* text than the reading a decision was made from, which would make
 * the evidence disagree with the record it justifies.
 *
 * Barcode payloads *are* re-resolved, though, and that is not a contradiction:
 * the payload is fixed, but what it points at is not. A code that resolved to
 * nothing in the aisle resolves to a real part once someone creates it, and this
 * screen should show what is true now rather than what was true then.
 */

import { useCallback, useEffect, useState } from "react";

import { CaptureOverlay } from "../components/CaptureOverlay";
import { Dialog } from "../components/Dialog";
import { ErrorBanner, Notice } from "../components/Feedback";
import {
  deleteCapture,
  listCaptures,
  resolveScan,
  type CaptureRead,
  type ScanResolveResponse,
} from "../lib/api/client";
import { regionsOf } from "../lib/capture/stored";
import type { TextStatus } from "../lib/capture/types";

const PAGE_SIZE = 24;

export function CapturesScreen() {
  const [captures, setCaptures] = useState<CaptureRead[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<unknown>(null);
  const [open, setOpen] = useState<CaptureRead | null>(null);

  const load = useCallback(async () => {
    try {
      const page = await listCaptures(PAGE_SIZE, 0);
      setCaptures(page.items);
      setTotal(page.total);
    } catch (cause) {
      setError(cause);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="stack">
      <div className="row">
        <h2 style={{ margin: 0 }}>Captures</h2>
        <span className="spacer" />
        {captures !== null && <span className="badge">{total}</span>}
      </div>

      <ErrorBanner error={error} fallback="Could not load your captures." />

      {captures !== null && captures.length === 0 && (
        <Notice kind="info" title="Nothing captured yet">
          <p style={{ margin: 0 }}>
            Tap <b>Capture</b> on the Scan screen to keep a frame. The photograph is
            saved with everything that was read off it, so you can come back to the
            parts of the label no barcode carried.
          </p>
        </Notice>
      )}

      {captures !== null && captures.length > 0 && (
        <ul className="capture-grid">
          {captures.map((capture) => (
            <li key={capture.id}>
              <button type="button" className="capture-card" onClick={() => setOpen(capture)}>
                <img src={capture.document.url} alt="" loading="lazy" />
                <span className="capture-card-meta">
                  <span>{new Date(capture.created_at).toLocaleString()}</span>
                  <span className="muted-note">{summarise(capture)}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {open !== null && (
        <CaptureDialog
          capture={open}
          onClose={() => setOpen(null)}
          onDeleted={() => {
            setOpen(null);
            void load();
          }}
        />
      )}
    </div>
  );
}

/** What was on it, in one line, so the grid is scannable without opening each. */
function summarise(capture: CaptureRead): string {
  const codes = capture.regions.filter((region) => region.kind === "barcode");
  const text = capture.regions.filter((region) => region.kind === "text");
  const parts: string[] = [];
  if (codes.length > 0) {
    parts.push(`${codes.length} code${codes.length === 1 ? "" : "s"}`);
  }
  if (text.length > 0) {
    parts.push(`${text.length} line${text.length === 1 ? "" : "s"}`);
  }
  if (parts.length === 0) {
    // Says which kind of nothing it is, for the same reason the capture screen
    // does: "nobody looked" and "nothing was there" are different facts.
    return capture.text_status === "not_attempted" ? "no text read yet" : "nothing read";
  }
  return parts.join(" · ");
}

function CaptureDialog({
  capture,
  onClose,
  onDeleted,
}: {
  capture: CaptureRead;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [resolved, setResolved] = useState<Record<number, ScanResolveResponse>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const regions = regionsOf(capture);

  // Re-resolved on open: the payload is fixed but what it points at is not, and
  // a code that matched nothing in the aisle may match a part now.
  useEffect(() => {
    let live = true;
    regions.forEach((region, index) => {
      if (region.kind !== "barcode") {
        return;
      }
      void resolveScan({ code: region.text, symbology: region.symbology })
        .then((response) => {
          if (live) {
            setResolved((previous) => ({ ...previous, [index]: response }));
          }
        })
        .catch(() => {
          // Offline, or a payload the resolver refuses. The raw value is still
          // on the chip, which is the useful fallback.
        });
    });
    return () => {
      live = false;
    };
    // Keyed on the capture, not on `regions`, which is a fresh array per render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [capture.id]);

  async function remove(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await deleteCapture(capture.id);
      onDeleted();
    } catch (cause) {
      setError(cause);
      setBusy(false);
    }
  }

  return (
    <Dialog title={new Date(capture.created_at).toLocaleString()} onClose={onClose}>
      <div className="stack">
        <CaptureOverlay
          imageUrl={capture.document.url}
          width={capture.width_px}
          height={capture.height_px}
          regions={regions}
          resolved={resolved}
          textStatus={capture.text_status as TextStatus}
        />
        <div className="row">
          <a className="button" href={capture.document.url} target="_blank" rel="noreferrer">
            Open the full picture
          </a>
          <span className="spacer" />
          <button type="button" onClick={() => void remove()} disabled={busy}>
            {busy ? "Deleting…" : "Delete"}
          </button>
        </div>
        <p className="muted-note" style={{ margin: 0 }}>
          Deleting removes the outlines and the record of this capture. The image file
          itself is shared by content, so another capture of the same label keeps it.
        </p>
        <ErrorBanner error={error} fallback="Could not delete that capture." />
      </div>
    </Dialog>
  );
}
