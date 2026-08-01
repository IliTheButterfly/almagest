/**
 * The photograph, at the desk, beside the entry it was parked with.
 *
 * This is the half of "queue for later" that made the deferral worth anything.
 * Before it, a parked scan carried only what the barcode encoded, so the desk
 * pass — hours later, at a machine with no reel in front of it — had no way to
 * recover the manufacturer, the description or the date code that were printed
 * in ink. Now it has the picture, the outlines, and the same ranked suggestions
 * the aisle had.
 *
 * **Barcodes are re-resolved here, text is not re-read.** The payload is fixed
 * but what it points at is not: a code that matched nothing when it was scanned
 * matches a part once someone creates one, and the desk is exactly where that
 * has usually happened in between. The OCR'd text is left as it was recorded,
 * because a fresh pass could disagree with the reading the entry was parked on,
 * and evidence that argues with the record it justifies is worse than none.
 *
 * Loaded only when opened. An intake queue is a list of dozens; fetching every
 * capture and re-resolving every code to render a collapsed row would make the
 * screen expensive exactly in proportion to how far behind you are.
 */

import { useEffect, useState } from "react";

import {
  readCapture,
  resolveScan,
  type CaptureRead,
  type ScanResolveResponse,
} from "../lib/api/client";
import type { FillField } from "../lib/capture/chips";
import { extractSuggestions, type Suggestions } from "../lib/capture/extract";
import { regionsOf } from "../lib/capture/stored";
import type { Region, TextStatus } from "../lib/capture/types";
import { CaptureOverlay } from "./CaptureOverlay";
import { CaptureToPart, type PartDraft } from "./CaptureToPart";
import { ErrorBanner, Loading } from "./Feedback";

export interface IntakeCaptureProps {
  readonly captureId: number;
  /** Called once a part exists, so the entry can be marked done. */
  readonly onCreated: (part: { id: number; name: string }) => void;
}

export function IntakeCapture({ captureId, onCreated }: IntakeCaptureProps) {
  const [capture, setCapture] = useState<CaptureRead | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [resolved, setResolved] = useState<Record<number, ScanResolveResponse>>({});
  const [armed, setArmed] = useState<FillField | null>(null);
  const [draft, setDraft] = useState<PartDraft>({});

  useEffect(() => {
    let live = true;
    void readCapture(captureId)
      .then((loaded) => {
        if (live) {
          setCapture(loaded);
        }
      })
      .catch((cause: unknown) => {
        if (live) {
          setError(cause);
        }
      });
    return () => {
      live = false;
    };
  }, [captureId]);

  const regions: Region[] = capture === null ? [] : regionsOf(capture);

  useEffect(() => {
    if (capture === null) {
      return;
    }
    let live = true;
    regionsOf(capture).forEach((region, index) => {
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
          // Offline, or a payload the resolver refuses. The text suggestions and
          // the picture are unaffected, which is most of the value.
        });
    });
    return () => {
      live = false;
    };
  }, [capture]);

  if (error !== null) {
    return <ErrorBanner error={error} fallback="That picture could not be loaded." />;
  }
  if (capture === null) {
    return <Loading what="the picture" />;
  }

  const suggestions: Suggestions = extractSuggestions({ regions, resolved });

  return (
    <div className="stack">
      <CaptureOverlay
        imageUrl={capture.document.url}
        width={capture.width_px}
        height={capture.height_px}
        regions={regions}
        resolved={resolved}
        textStatus={capture.text_status as TextStatus}
        {...(armed === null
          ? {}
          : { fillInto: { field: armed, label: armed.replace(/_/g, " ") } })}
        onFill={(field, value) => {
          setDraft((previous) => ({ ...previous, [field]: value }));
          setArmed(null);
        }}
      />
      <CaptureToPart
        draft={draft}
        armed={armed}
        suggestions={suggestions}
        onArm={setArmed}
        onChange={(field, value) => setDraft((previous) => ({ ...previous, [field]: value }))}
        onCreated={onCreated}
      />
    </div>
  );
}
