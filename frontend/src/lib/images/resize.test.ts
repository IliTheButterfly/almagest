/**
 * `jsdom` has no `createImageBitmap` and no real 2D canvas context, so this
 * suite is exercising exactly the fallback path a genuinely old or stripped-down
 * browser would take too — not a mock standing in for a capability the test
 * pretends exists. That is deliberate: the one property that matters most,
 * "resizing never blocks the upload", is best proven by an environment that
 * cannot resize at all.
 */

import { describe, expect, it } from "vitest";

import { downscaleForUpload } from "./resize";

function jpeg(bytes: number): File {
  return new File([new Uint8Array(bytes)], "photo.jpg", { type: "image/jpeg" });
}

describe("downscaleForUpload", () => {
  it("passes a non-image file through untouched", async () => {
    const pdf = new File([new Uint8Array(10)], "note.pdf", { type: "application/pdf" });
    const result = await downscaleForUpload(pdf);
    expect(result).toBe(pdf);
  });

  it("passes an image through untouched when resizing is unavailable", async () => {
    // The real assertion here is behavioural, not environmental: whatever the
    // reason resizing cannot happen, the upload must still be able to proceed
    // with the original bytes rather than failing.
    expect(typeof createImageBitmap).not.toBe("function");
    const photo = jpeg(12_000_000);
    const result = await downscaleForUpload(photo);
    expect(result).toBe(photo);
    expect(result.size).toBe(12_000_000);
  });

  it("never throws even when given something that is not really an image", async () => {
    const mislabelled = new File([new Uint8Array([1, 2, 3])], "not-a-photo.png", {
      type: "image/png",
    });
    await expect(downscaleForUpload(mislabelled)).resolves.toBe(mislabelled);
  });
});
