import { beforeEach, describe, expect, it } from "vitest";

import { IntakeQueue, type PendingScan, type QueueStorage } from "./queue";

function memoryStorage(): QueueStorage & { readonly map: Map<string, string> } {
  const map = new Map<string, string>();
  return {
    map,
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => {
      map.set(key, value);
    },
    removeItem: (key) => {
      map.delete(key);
    },
  };
}

function entry(id: string, code = `code-${id}`): PendingScan {
  return {
    id,
    code,
    symbology: "DataMatrix",
    queuedAt: 1_700_000_000_000,
    decodedKind: "ecia",
    mpn: "GRM188R61A106KA73D",
    manufacturer: "Murata",
    supplierPartNumber: "490-1691-1-ND",
    quantityMilli: 5_000_000,
    dateCode: "2412",
    lotCode: "L1",
    partId: null,
    note: null,
  };
}

describe("the intake queue", () => {
  let storage: QueueStorage;

  beforeEach(() => {
    storage = memoryStorage();
  });

  it("starts empty", () => {
    expect(new IntakeQueue(storage).list()).toEqual([]);
  });

  it("keeps parked scans in the order they were parked", () => {
    const queue = new IntakeQueue(storage);
    queue.add(entry("op-1"));
    queue.add(entry("op-2"));
    expect(queue.list().map((parked) => parked.id)).toEqual(["op-1", "op-2"]);
  });

  it("keys on the scan's client_op_id, so re-parking replaces rather than doubles", () => {
    const queue = new IntakeQueue(storage);
    queue.add(entry("op-1", "first"));
    queue.add(entry("op-1", "second"));
    expect(queue.size).toBe(1);
    expect(queue.list()[0]?.code).toBe("second");
  });

  it("survives a reload, which is the whole point of parking", () => {
    const first = new IntakeQueue(storage);
    first.add(entry("op-1"));
    expect(new IntakeQueue(storage).list().map((parked) => parked.id)).toEqual(["op-1"]);
  });

  it("removes and clears", () => {
    const queue = new IntakeQueue(storage);
    queue.add(entry("op-1"));
    queue.add(entry("op-2"));
    queue.remove("op-1");
    expect(queue.list().map((parked) => parked.id)).toEqual(["op-2"]);
    queue.clear();
    expect(queue.size).toBe(0);
  });

  it("notifies subscribers", () => {
    const queue = new IntakeQueue(storage);
    let notifications = 0;
    const unsubscribe = queue.subscribe(() => {
      notifications += 1;
    });
    queue.add(entry("op-1"));
    queue.remove("op-1");
    unsubscribe();
    queue.add(entry("op-2"));
    expect(notifications).toBe(2);
  });

  it("ignores corrupt stored data instead of taking the scanner down with it", () => {
    storage.setItem("almagest.intake.pending.v1", "{not json");
    expect(new IntakeQueue(storage).list()).toEqual([]);
  });

  it("drops entries that are not parked scans", () => {
    storage.setItem("almagest.intake.pending.v1", JSON.stringify([{ nope: true }, entry("op-1")]));
    expect(new IntakeQueue(storage).list().map((parked) => parked.id)).toEqual(["op-1"]);
  });

  it("degrades to memory when storage refuses to write", () => {
    // Safari private mode, a full quota, a locked-down kiosk profile. Losing the
    // parked queue is bad; refusing to scan is worse.
    const hostile: QueueStorage = {
      getItem: () => null,
      setItem: () => {
        throw new DOMException("QuotaExceededError");
      },
      removeItem: () => undefined,
    };
    const queue = new IntakeQueue(hostile);
    expect(() => queue.add(entry("op-1"))).not.toThrow();
    expect(queue.size).toBe(1);
  });

  it("works with no storage at all", () => {
    const queue = new IntakeQueue(null);
    queue.add(entry("op-1"));
    expect(queue.size).toBe(1);
  });
});
