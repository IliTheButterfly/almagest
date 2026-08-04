/**
 * Writing a tag, against a fake `NDEFReader`.
 *
 * The two behaviours worth pinning are both about *not crying wolf*, because the
 * cost of a false "this tag is broken" is somebody peeling a working sticker off a
 * drawer:
 *
 * - a read-back that never arrived is `observed: false`, which is unverified, not
 *   degraded — Chrome does not re-fire `reading` for a tag that never left the
 *   field, so no read-back is the *common* case;
 * - a read-back that arrived and disagrees is the real failure, and it must be
 *   distinguishable from the above by more than a null.
 *
 * The third is that the reading which answers a write is consumed by the write. A
 * walk that also saw it would read it as a second tap on the drawer it just
 * finished and auto-advance past the next one.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { openNfcScan, TagNotBlankError, type NdefWriteRecord } from "./nfc";

interface WriteCall {
  readonly records: readonly NdefWriteRecord[];
  readonly overwrite: boolean | undefined;
}

class FakeReader extends EventTarget {
  readonly writes: WriteCall[] = [];
  scanned = false;
  /** Set to make `write()` reject the way Chrome rejects a non-blank tag. */
  refuseNonBlank = false;

  scan(): Promise<void> {
    this.scanned = true;
    return Promise.resolve();
  }

  write(
    message: { records: readonly NdefWriteRecord[] },
    options?: { overwrite?: boolean },
  ): Promise<void> {
    if (this.refuseNonBlank && options?.overwrite !== true) {
      return Promise.reject(new DOMException("not blank", "NotAllowedError"));
    }
    this.writes.push({ records: message.records, overwrite: options?.overwrite });
    return Promise.resolve();
  }

  /** Fire what Chrome fires when a tag enters the field. */
  present(url: string | null, serialNumber = "04:1a:2b:3c:4d:5e:6f"): void {
    const event = new Event("reading") as Event & {
      serialNumber?: string;
      message?: { records: unknown[] };
    };
    event.serialNumber = serialNumber;
    event.message = {
      records:
        url === null
          ? []
          : [{ recordType: "url", encoding: "utf-8", data: new TextEncoder().encode(url) }],
    };
    this.dispatchEvent(event);
  }
}

function install(): FakeReader {
  const reader = new FakeReader();
  vi.stubGlobal(
    "NDEFReader",
    function NDEFReader(this: unknown) {
      return reader;
    } as unknown as new () => FakeReader,
  );
  return reader;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("writing and reading back", () => {
  it("writes the URL as a URL record and refuses a non-blank tag by default", async () => {
    const reader = install();
    const session = openNfcScan({ onReading: () => undefined, readBackTimeoutMs: 5 });

    await session.write("https://almagest.aether.lan/s/4K7T92M8");
    expect(reader.writes[0]?.records[0]).toEqual({
      recordType: "url",
      data: "https://almagest.aether.lan/s/4K7T92M8",
    });
    // Blank-tags-only is the default: the real risk is a write screen left open
    // quietly overwriting a provisioned drawer, not an attacker.
    expect(reader.writes[0]?.overwrite).toBe(false);

    reader.refuseNonBlank = true;
    await expect(session.write("https://almagest.aether.lan/s/4K7T92M8")).rejects.toBeInstanceOf(
      TagNotBlankError,
    );

    // ...and the explicit toggle gets through.
    await session.write("https://almagest.aether.lan/s/4K7T92M8", { overwrite: true });
    expect(reader.writes[1]?.overwrite).toBe(true);
    session.close();
  });

  it("reports a missing read-back as unobserved, not as a bad tag", async () => {
    install();
    const session = openNfcScan({ onReading: () => undefined, readBackTimeoutMs: 5 });

    const back = await session.write("https://almagest.aether.lan/s/4K7T92M8");

    expect(back).toEqual({ observed: false, url: null });
    session.close();
  });

  it("reports a read-back that disagrees, which is the real failure", async () => {
    const reader = install();
    const session = openNfcScan({ onReading: () => undefined, readBackTimeoutMs: 500 });

    const pending = session.write("https://almagest.aether.lan/s/4K7T92M8");
    // The tag answered, and user memory came back empty — a half-written sticker.
    reader.present(null);
    await expect(pending).resolves.toEqual({ observed: true, url: null });
    session.close();
  });

  it("does not deliver the post-write reading to the walk", async () => {
    const reader = install();
    const seen: (string | null)[] = [];
    const session = openNfcScan({
      onReading: (reading) => seen.push(reading.url),
      readBackTimeoutMs: 500,
    });

    reader.present("https://almagest.aether.lan/s/AAAAAAAA");
    const pending = session.write("https://almagest.aether.lan/s/4K7T92M8");
    reader.present("https://almagest.aether.lan/s/4K7T92M8");
    await pending;

    // Only the first tap. The second answered the write; a walk that also saw it
    // would treat it as a fresh tap and advance past the next drawer.
    expect(seen).toEqual(["https://almagest.aether.lan/s/AAAAAAAA"]);
    session.close();
  });
});
