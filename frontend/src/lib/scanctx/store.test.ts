/**
 * The scan context: what the reader saw, for whoever needs it next.
 *
 * The behaviours worth pinning are the ones that protect against a *stale*
 * scan being treated as a fresh statement about where somebody is standing.
 */

import { describe, expect, it } from "vitest";

import { MAX_SCANS, newScanContext, type ScanRecord } from "./store";

function scan(
  id: string,
  entityType: string | null = "location",
  presentOn: string | null = "flipper-usb:a",
): ScanRecord {
  return {
    id,
    presentOn,
    at: 1_700_000_000_000,
    code: `https://almagest.aether.lan/s/${id}`,
    symbology: "nfc",
    target:
      entityType === null
        ? null
        : ({
            entity_type: entityType,
            entity_pk: 1,
            label: id,
            label_path: null,
            short_id: null,
          } as ScanRecord["target"]),
    status: entityType === null ? "unmatched" : "resolved",
  };
}

describe("the scan context", () => {
  it("puts the newest scan first, because that is where you are standing", () => {
    const context = newScanContext();
    context.add(scan("first"));
    context.add(scan("second"));

    expect(context.list().map((row) => row.id)).toEqual(["second", "first"]);
    expect(context.latest()?.id).toBe("second");
  });

  it("answers by type, so a part scanned after a drawer does not hide the drawer", () => {
    const context = newScanContext();
    context.add(scan("drawer", "location"));
    context.add(scan("reel", "part"));

    // The newest overall is the reel; a location field still wants the drawer.
    expect(context.latest()?.id).toBe("reel");
    expect(context.latestOfType("location")?.id).toBe("drawer");
    expect(context.latestOfType("stock_lot")).toBeNull();
  });

  it("keeps an unmatched scan rather than dropping it", () => {
    const context = newScanContext();
    context.add(scan("unknown", null));

    // "You scanned something and it means nothing" is a useful thing to be able
    // to say. Dropping it would make an unrecognised tag look identical to a
    // reader that never fired.
    expect(context.list()).toHaveLength(1);
    expect(context.latest()?.target).toBeNull();
    expect(context.latestOfType("location")).toBeNull();
  });

  it("is bounded, because a bench session is hours and a reader fires on every tap", () => {
    const context = newScanContext();
    for (let index = 0; index < MAX_SCANS + 5; index += 1) {
      context.add(scan(`s${index}`));
    }

    expect(context.list()).toHaveLength(MAX_SCANS);
    expect(context.latest()?.id).toBe(`s${MAX_SCANS + 4}`);
  });

  it("tells its subscribers on every change, including removal", () => {
    const context = newScanContext();
    let notifications = 0;
    const unsubscribe = context.subscribe(() => {
      notifications += 1;
    });

    context.add(scan("one"));
    context.remove("one");
    unsubscribe();
    context.add(scan("two"));

    expect(notifications).toBe(2);
    expect(context.list().map((row) => row.id)).toEqual(["two"]);
  });
});

describe("presence, as opposed to a log of sightings", () => {
  it("marks the previous tag on that reader as no longer there", () => {
    const context = newScanContext();
    context.add(scan("drawer"));
    context.add(scan("reel", "part"));

    // One reader holds one tag: the arrival of the second means the first is
    // off it, even if the tag was swapped fast enough that no empty poll — and
    // so no `tag.gone` — landed between them.
    expect(context.present().map((row) => row.id)).toEqual(["reel"]);
    expect(context.list().map((row) => row.id)).toEqual(["reel", "drawer"]);
  });

  it("keeps a lifted tag in the list but stops calling it present", () => {
    const context = newScanContext();
    context.add(scan("drawer"));

    context.lifted("flipper-usb:a");

    // Still the last thing you scanned; no longer the thing in your hand.
    expect(context.list().map((row) => row.id)).toEqual(["drawer"]);
    expect(context.present()).toEqual([]);
  });

  it("leaves another reader's tag alone", () => {
    const context = newScanContext();
    context.add(scan("on-a", "location", "flipper-usb:a"));
    context.add(scan("on-b", "location", "flipper-usb:b"));

    context.lifted("flipper-usb:a");

    expect(context.present().map((row) => row.id)).toEqual(["on-b"]);
  });

  it("says nothing when a reader with no tag reports one lifted", () => {
    const context = newScanContext();
    context.add(scan("drawer"));
    let notifications = 0;
    context.subscribe(() => {
      notifications += 1;
    });

    context.lifted("flipper-usb:b");

    // A subscriber re-rendering on an event that changed nothing is how a
    // panel flickers for no reason.
    expect(notifications).toBe(0);
  });
});
