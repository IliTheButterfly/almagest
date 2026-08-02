/**
 * A saved capture, read back as the same shape a fresh one has.
 *
 * The wire type and the in-memory type describe the same thing in two different
 * dialects: the server stores a region's quad as four `{x, y}` points in a list
 * and its kind as a plain string, because that is what a schema can express;
 * the client wants a four-element tuple and a discriminated union, because that
 * is what lets `CaptureOverlay` and `chipsForRegion` be written once and used
 * for both a live capture and a remembered one.
 *
 * This is the seam. It exists so the gallery is not a second, subtly different
 * renderer of the same data — which is how a "view saved capture" screen ends up
 * drawing outlines a pixel out or labelling a confidence differently from the
 * screen the value was originally taken on.
 */

import type { CaptureRead, CaptureRegionRead } from "../api/client";
import type { Quad, Region } from "./types";
import { boxToQuad } from "./types";

/**
 * Rebuild a region's quad from the four stored corners.
 *
 * Falls back to a degenerate box rather than throwing if a row somehow carries
 * the wrong number of points: a capture with one odd region should still render
 * the other nine, and a saved photograph is evidence that must not become
 * unviewable because of a display detail.
 */
function quadOf(region: CaptureRegionRead): Quad {
  const corners = region.corners;
  if (corners.length !== 4) {
    return boxToQuad(0, 0, 0, 0);
  }
  return [
    { x: corners[0]!.x, y: corners[0]!.y },
    { x: corners[1]!.x, y: corners[1]!.y },
    { x: corners[2]!.x, y: corners[2]!.y },
    { x: corners[3]!.x, y: corners[3]!.y },
  ];
}

export function regionsOf(capture: CaptureRead): Region[] {
  const regions: Region[] = [];
  for (const region of capture.regions) {
    if (region.kind === "barcode") {
      regions.push({
        kind: "barcode",
        text: region.text,
        quad: quadOf(region),
        // A stored barcode always has one; the fallback keeps a hand-written row
        // from rendering as `undefined` in the chip label.
        symbology: region.symbology ?? "barcode",
      });
    } else {
      regions.push({
        kind: "text",
        text: region.text,
        quad: quadOf(region),
        // Omitted rather than defaulted when the row has none: `0%` on a chip
        // reads as "certainly wrong", which is a far stronger claim than
        // "nobody recorded how sure it was". The chip simply shows no score.
        ...(region.confidence === null || region.confidence === undefined
          ? {}
          : { confidence: region.confidence }),
      });
    }
  }
  return regions;
}
