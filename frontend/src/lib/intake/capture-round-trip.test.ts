/**
 * That a parked capture survives the trip to the desk.
 *
 * The queue is a write-behind buffer: entries are written locally at the shelf
 * and pushed later, so the field that carries the photograph has to survive
 * being stored, listed, and serialised into the sync payload. Losing it is
 * silent — the entry still uploads, still looks right in the queue, and simply
 * has no picture when it is finally curated, which is the one thing parking was
 * supposed to preserve.
 */

import { describe, expect, it } from "vitest";

import { IntakeQueue, type PendingScan } from "./queue";
import { buildIntakePayload } from "./sync";

function entry(overrides: Partial<PendingScan> = {}): PendingScan {
  return {
    id: "op-1",
    code: "CF14JT100K",
    symbology: "ocr",
    queuedAt: 1_754_000_000_000,
    decodedKind: null,
    mpn: "CF14JT100K",
    manufacturer: "STACKPOLE ELECTRONICS INC",
    supplierPartNumber: null,
    quantityMilli: 100_000,
    dateCode: "2247",
    lotCode: null,
    partId: null,
    captureId: 26,
    note: null,
    ...overrides,
  };
}

class MemoryStorage {
  #data = new Map<string, string>();
  getItem(key: string): string | null {
    return this.#data.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.#data.set(key, value);
  }
  removeItem(key: string): void {
    this.#data.delete(key);
  }
}

describe("a capture parked with a scan", () => {
  it("survives being stored and read back", () => {
    const storage = new MemoryStorage();
    const queue = new IntakeQueue(storage);
    queue.add(entry());

    // A fresh instance over the same storage: this is a phone that was closed
    // and reopened between the aisle and the desk.
    const reopened = new IntakeQueue(storage);
    expect(reopened.list()[0]?.captureId).toBe(26);
  });

  it("is sent to the server, so the desk pass can load it", () => {
    const payload = buildIntakePayload(entry());
    expect(payload.capture_id).toBe(26);
  });

  it("is omitted rather than sent as null when there is no picture", () => {
    // Parking at a shelf with no signal parks a payload and no capture, which is
    // normal. The wire type takes an optional id, and sending an explicit null
    // for "there wasn't one" is a different statement from not sending it.
    const payload = buildIntakePayload(entry({ captureId: null }));
    expect("capture_id" in payload).toBe(false);
  });

  it("carries what was read off the label alongside the picture", () => {
    // The suggestions computed in the aisle are parked too, so a desk with no
    // network still shows something useful before the capture loads.
    const payload = buildIntakePayload(entry());
    expect(payload.mpn).toBe("CF14JT100K");
    expect(payload.manufacturer).toBe("STACKPOLE ELECTRONICS INC");
    expect(payload.quantity_milli).toBe(100_000);
    expect(payload.date_code).toBe("2247");
  });
});
