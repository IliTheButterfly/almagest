/**
 * That a saved capture reads back as the same thing a fresh one is.
 *
 * The point of this seam is that the gallery is not a second renderer: the
 * outlines drawn over a remembered photograph must sit exactly where they sat
 * when the value was taken, or the evidence stops matching the record.
 */

import { describe, expect, it } from "vitest";

import type { CaptureRead } from "../api/client";
import { regionsOf } from "./stored";

function capture(regions: CaptureRead["regions"]): CaptureRead {
  return {
    id: 1,
    created_at: "2026-08-01T10:00:00Z",
    width_px: 1600,
    height_px: 1200,
    text_status: "ok",
    device_id: null,
    note: null,
    document: {
      id: 1,
      sha256: "a".repeat(64),
      kind: "photo",
      media_type: "image/jpeg",
      byte_size: 1,
      page_count: null,
      source_url: null,
      original_filename: null,
      created_at: "2026-08-01T10:00:00Z",
      url: "/api/documents/" + "a".repeat(64),
    },
    regions,
  } as CaptureRead;
}

const CORNERS = [
  { x: 10, y: 20 },
  { x: 60, y: 20 },
  { x: 60, y: 50 },
  { x: 10, y: 50 },
];

describe("regionsOf", () => {
  it("rebuilds a barcode region with its corners in order", () => {
    const [region] = regionsOf(
      capture([
        {
          id: 1,
          kind: "barcode",
          text: "RC0805FR-0710KL",
          corners: CORNERS,
          symbology: "DataMatrix",
          confidence: null,
          scan_event_id: 7,
          order_index: 0,
        },
      ]),
    );
    expect(region?.kind).toBe("barcode");
    expect(region?.quad).toEqual(CORNERS);
    expect(region?.kind === "barcode" && region.symbology).toBe("DataMatrix");
  });

  it("rebuilds a text region with its confidence", () => {
    const [region] = regionsOf(
      capture([
        {
          id: 2,
          kind: "text",
          text: "Murata Electronics",
          corners: CORNERS,
          symbology: null,
          confidence: 74,
          scan_event_id: null,
          order_index: 1,
        },
      ]),
    );
    expect(region?.kind === "text" && region.confidence).toBe(74);
  });

  it("survives a region with the wrong number of corners", () => {
    // A saved photograph is evidence; one malformed row must not make the whole
    // capture unviewable.
    const regions = regionsOf(
      capture([
        {
          id: 3,
          kind: "text",
          text: "odd",
          corners: [{ x: 1, y: 1 }],
          symbology: null,
          confidence: 60,
          scan_event_id: null,
          order_index: 0,
        },
        {
          id: 4,
          kind: "text",
          text: "fine",
          corners: CORNERS,
          symbology: null,
          confidence: 80,
          scan_event_id: null,
          order_index: 1,
        },
      ]),
    );
    expect(regions).toHaveLength(2);
    expect(regions[1]?.text).toBe("fine");
  });

  it("does not report a missing confidence as zero-confidence", () => {
    // "Not recorded" and "certainly wrong" are different claims.
    const [region] = regionsOf(
      capture([
        {
          id: 5,
          kind: "text",
          text: "x",
          corners: CORNERS,
          symbology: null,
          confidence: null,
          scan_event_id: null,
          order_index: 0,
        },
      ]),
    );
    expect(region?.kind === "text" && region.confidence).toBeUndefined();
  });
});
