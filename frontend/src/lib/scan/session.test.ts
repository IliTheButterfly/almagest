import { describe, expect, it } from "vitest";

import { ScanSessionStore, uuid4 } from "./session";

function fixtures(): {
  store: ScanSessionStore;
  advance: (ms: number) => void;
  minted: () => number;
} {
  let at = 1_000;
  let count = 0;
  const store = new ScanSessionStore({
    now: () => at,
    uuid: () => {
      count += 1;
      return `op-${count}`;
    },
  });
  return {
    store,
    advance: (ms) => {
      at += ms;
    },
    minted: () => count,
  };
}

describe("the scan session", () => {
  it("mints one idempotency key at scan time", () => {
    const { store, minted } = fixtures();
    const session = store.scan("REEL-A", "DataMatrix");
    expect(session?.clientOpId).toBe("op-1");
    expect(session?.code).toBe("REEL-A");
    expect(session?.symbology).toBe("DataMatrix");
    expect(minted()).toBe(1);
  });

  it("hands the commit the same key the scan minted, however often it is read", () => {
    // The whole point of minting at scan rather than at commit: a double tap on
    // Commit sends one key and records one movement.
    const { store, minted } = fixtures();
    store.scan("REEL-A");
    expect(store.current()?.clientOpId).toBe("op-1");
    expect(store.current()?.clientOpId).toBe("op-1");
    expect(store.current()?.clientOpId).toBe("op-1");
    expect(minted()).toBe(1);
  });

  it("drops a duplicate scan inside the ~2 s debounce", () => {
    const { store, advance, minted } = fixtures();
    expect(store.scan("REEL-A")).not.toBeNull();
    advance(1_500);
    expect(store.scan("REEL-A")).toBeNull();
    // No second key was minted, so nothing downstream can commit twice.
    expect(minted()).toBe(1);
    expect(store.current()?.clientOpId).toBe("op-1");
  });

  it("admits a deliberate re-scan after the debounce, with a fresh key", () => {
    const { store, advance } = fixtures();
    store.scan("REEL-A");
    advance(2_000);
    const again = store.scan("REEL-A");
    expect(again?.clientOpId).toBe("op-2");
  });

  it("admits a different payload immediately", () => {
    const { store } = fixtures();
    expect(store.scan("REEL-A")).not.toBeNull();
    expect(store.scan("REEL-B")).not.toBeNull();
  });

  it("drops every scan while a commit is in flight", () => {
    const { store } = fixtures();
    store.scan("REEL-A");
    store.beginCommit();
    expect(store.scan("REEL-B")).toBeNull();
    store.endCommit();
    expect(store.scan("REEL-B")).not.toBeNull();
  });

  it("spends the key, so the next movement gets its own", () => {
    // Reusing a spent key would come back `replayed: true` carrying the first
    // movement's numbers, which looks like success and silently loses stock.
    const { store, advance } = fixtures();
    store.scan("REEL-A");
    store.spend();
    expect(store.current()).toBeNull();
    advance(2_000);
    expect(store.scan("REEL-A")?.clientOpId).toBe("op-2");
  });

  it("notifies subscribers on a scan and on spending", () => {
    const { store } = fixtures();
    let notifications = 0;
    const unsubscribe = store.subscribe(() => {
      notifications += 1;
    });
    store.scan("REEL-A");
    store.spend();
    unsubscribe();
    store.scan("REEL-B");
    expect(notifications).toBe(2);
  });
});

describe("uuid4", () => {
  it("produces distinct version-4 uuids", () => {
    const first = uuid4();
    const second = uuid4();
    expect(first).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(first).not.toBe(second);
  });

  it("still works with no crypto.randomUUID, which is the plain-HTTP case", () => {
    // `crypto.randomUUID` is secure-context only, and the app has to stay usable
    // over plain HTTP where the camera and NFC are absent.
    const original = globalThis.crypto;
    Object.defineProperty(globalThis, "crypto", {
      configurable: true,
      value: { getRandomValues: original.getRandomValues.bind(original) },
    });
    try {
      expect(uuid4()).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
      );
    } finally {
      Object.defineProperty(globalThis, "crypto", { configurable: true, value: original });
    }
  });
});
