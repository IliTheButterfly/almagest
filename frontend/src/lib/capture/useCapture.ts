/**
 * One capture, from tapping the button to having something worth tapping back.
 *
 * The ordering here is the whole user experience, so it is worth stating plainly.
 * Five things happen, and only the first two are on the critical path:
 *
 * 1. **Grab and freeze.** The still is drawn and shown immediately. From this
 *    moment the user is looking at a photograph rather than a live preview,
 *    which is what makes outlines meaningful — a box over a moving image would
 *    point at nothing.
 * 2. **Barcodes.** A few hundred milliseconds, and it cannot fail to be
 *    available. Outlines appear.
 * 3. **Save.** Upload the image, create the capture row with the barcode regions
 *    already on it. Deliberately *not* awaited before showing anything: a slow
 *    network must not delay outlines that have already been computed.
 * 4. **Resolve.** Each barcode payload goes through `/api/scan/resolve`, which is
 *    what expands one outline into MPN / quantity / date-code chips. Per region,
 *    so a slow one does not hold up the rest.
 * 5. **Text.** Seconds, and allowed to be unavailable entirely. Appended when it
 *    lands.
 *
 * **Nothing waits for step 3 except step 5**, which genuinely cannot append
 * regions to a row that does not exist yet — so it awaits the save promise
 * rather than racing it.
 *
 * The capture is saved without being asked to be. That is the requested
 * behaviour and it is also the right default: the image is the asset, it is
 * content-addressed so re-capturing the same unchanged label costs no extra
 * bytes, and a photo the user did not want is one tap to delete.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  appendCaptureRegions,
  createCapture,
  resolveScan,
  uploadDocument,
  type CaptureRegionIn,
  type ScanResolveResponse,
} from "../api/client";
import { detectRegions } from "./detect";
import { CAPTURE_MEDIA_TYPE, grabStill, toBitmap, type CaptureSource } from "./grab";
import type { BarcodeRegion, Region, Still, TextStatus } from "./types";

export type CaptureStatus = "idle" | "working" | "ready" | "error";

export interface CaptureState {
  readonly status: CaptureStatus;
  /** Object URL of the frozen still. Null until one has been taken. */
  readonly imageUrl: string | null;
  readonly width: number;
  readonly height: number;
  readonly regions: readonly Region[];
  /** Keyed by region index. Absent while the round trip is still in flight. */
  readonly resolved: Readonly<Record<number, ScanResolveResponse>>;
  readonly textStatus: TextStatus;
  /** Why text could not be read, when it could not. Safe to show. */
  readonly textMessage: string | null;
  /** Set once the row exists, so the capture can be attached to something. */
  readonly captureId: number | null;
  readonly error: unknown;
  /** True while the text pass is still running. */
  readonly readingText: boolean;
}

const EMPTY: CaptureState = {
  status: "idle",
  imageUrl: null,
  width: 0,
  height: 0,
  regions: [],
  resolved: {},
  textStatus: "not_attempted",
  textMessage: null,
  captureId: null,
  error: null,
  readingText: false,
};

function toRegionIn(region: Region): CaptureRegionIn {
  return {
    kind: region.kind,
    text: region.text,
    corners: region.quad.map((point) => ({ x: point.x, y: point.y })),
    ...(region.kind === "barcode"
      ? { symbology: region.symbology }
      : { confidence: region.confidence }),
  };
}

export interface UseCapture {
  readonly state: CaptureState;
  /** Take one. Safe to call again; the previous capture is discarded. */
  readonly capture: (source: CaptureSource & CanvasImageSource) => Promise<void>;
  /** Back to the live preview. Does not delete anything already saved. */
  readonly clear: () => void;
}

export function useCapture(): UseCapture {
  const [state, setState] = useState<CaptureState>(EMPTY);

  // Every async step checks this before setting state. A capture that is
  // cleared, or superseded by a second one, must not have its late-arriving OCR
  // result painted over whatever is on screen now.
  const generation = useRef(0);
  // Revoked on replacement and on unmount: an object URL holds the whole decoded
  // image alive, and a session of scanning a box of reels would otherwise
  // accumulate every frame ever captured.
  const urlRef = useRef<string | null>(null);

  const replaceUrl = useCallback((next: string | null) => {
    if (urlRef.current !== null) {
      URL.revokeObjectURL(urlRef.current);
    }
    urlRef.current = next;
  }, []);

  useEffect(() => () => replaceUrl(null), [replaceUrl]);

  const clear = useCallback(() => {
    generation.current += 1;
    replaceUrl(null);
    setState(EMPTY);
  }, [replaceUrl]);

  const capture = useCallback(
    async (source: CaptureSource & CanvasImageSource) => {
      const mine = ++generation.current;
      const current = () => generation.current === mine;

      let still: Still;
      try {
        still = await grabStill(source);
      } catch (cause) {
        setState({ ...EMPTY, status: "error", error: cause });
        return;
      }
      if (!current()) {
        return;
      }

      const url = URL.createObjectURL(still.blob);
      replaceUrl(url);
      setState({
        ...EMPTY,
        status: "working",
        imageUrl: url,
        width: still.width,
        height: still.height,
        readingText: true,
      });

      let bitmap: ImageBitmap;
      try {
        bitmap = await toBitmap(still);
      } catch (cause) {
        if (current()) {
          setState((previous) => ({ ...previous, status: "error", error: cause, readingText: false }));
        }
        return;
      }

      // Started here and awaited only by the text append below, so a slow upload
      // never delays an outline that has already been computed.
      let save: Promise<number | null> = Promise.resolve(null);

      const onBarcodes = (barcodes: readonly BarcodeRegion[]): void => {
        if (!current()) {
          return;
        }
        setState((previous) => ({ ...previous, regions: barcodes, status: "ready" }));

        save = (async () => {
          const upload = await uploadDocument(still.blob, {
            mediaType: CAPTURE_MEDIA_TYPE,
            kind: "photo",
            filename: "capture.jpg",
          });
          const saved = await createCapture({
            sha256: upload.document.sha256,
            width_px: still.width,
            height_px: still.height,
            regions: barcodes.map(toRegionIn),
          });
          if (current()) {
            setState((previous) => ({ ...previous, captureId: saved.id }));
          }
          return saved.id;
        })().catch((cause: unknown) => {
          // A capture that could not be saved is still perfectly usable on
          // screen — the outlines and chips are already computed locally. So
          // this surfaces as an error without tearing down what is displayed.
          if (current()) {
            setState((previous) => ({ ...previous, error: cause }));
          }
          return null;
        });

        // Per region rather than in one batch: a slow resolve for one symbol
        // must not hold back the chips for the others.
        barcodes.forEach((barcode, index) => {
          void resolveScan({ code: barcode.text, symbology: barcode.symbology })
            .then((response) => {
              if (current()) {
                setState((previous) => ({
                  ...previous,
                  resolved: { ...previous.resolved, [index]: response },
                }));
              }
            })
            .catch(() => {
              // Offline, or a payload the resolver refused. The raw value is
              // still on the chip list, which is the useful fallback — so this
              // is not worth an error banner over a working capture.
            });
        });
      };

      let detection;
      try {
        detection = await detectRegions(bitmap, still.blob, { onBarcodes });
      } catch (cause) {
        if (current()) {
          setState((previous) => ({ ...previous, status: "error", error: cause, readingText: false }));
        }
        return;
      } finally {
        bitmap.close();
      }

      if (!current()) {
        return;
      }
      setState((previous) => ({
        ...previous,
        status: "ready",
        regions: detection.regions,
        textStatus: detection.textStatus,
        textMessage: detection.textMessage ?? null,
        readingText: false,
      }));

      const captureId = await save;
      if (captureId === null || !current()) {
        return;
      }
      const text = detection.regions.filter((region) => region.kind === "text");
      try {
        await appendCaptureRegions(captureId, text.map(toRegionIn), detection.textStatus);
      } catch {
        // Same reasoning as the save failure above: what is on screen is intact.
      }
    },
    [replaceUrl],
  );

  return { state, capture, clear };
}
