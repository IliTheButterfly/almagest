/**
 * What a parked entry is headlined by when it has no part number yet.
 *
 * `raw_payload` is mandatory on an intake entry and is normally the scanned
 * string, which is exactly what somebody wants to read. A photograph uploaded
 * rather than scanned has nothing to put there, so `app.scripts.upload_capture`
 * stores the image's sha256 behind a prefix no symbology produces — and
 * rendering that verbatim gave a row headlined by seventy-one hex characters,
 * which is what prompted this.
 *
 * The stored value is deliberately left alone. It is the entry's identity and
 * the hash is how the picture is found again; only the heading changes.
 */

import { describe, expect, it } from "vitest";

import { headlineFor } from "./IntakeQueueScreen";

describe("headlineFor", () => {
  it("names an uploaded photograph instead of printing its hash", () => {
    const sha = "ec12cd38add3e2a6e2a0ddf95dc1786d0577f9d7100e649586cda3aa7cea3d69";
    expect(headlineFor(`capture:${sha}`)).toBe("Photograph");
  });

  it("leaves a scanned payload exactly as it was scanned", () => {
    // Verbatim, control characters and all: the bytes are the asset, and this is
    // the case the heading exists for in the first place.
    const ecia = "[)>06PCF14JT100KCT-ND1PCF14JT100K";
    expect(headlineFor(ecia)).toBe(ecia);
  });

  it("does not swallow a payload that merely mentions the word", () => {
    // The prefix is anchored, so a barcode whose content happens to contain
    // "capture:" somewhere is still shown as itself.
    expect(headlineFor("SN-capture:1234")).toBe("SN-capture:1234");
  });
});
