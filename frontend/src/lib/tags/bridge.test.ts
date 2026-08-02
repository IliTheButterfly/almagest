/**
 * The bridge client, against a socket made of arrays.
 *
 * The envelopes below are the real ones `agent/events.py` emits — same field
 * names, same shapes — because the two files are the two halves of one protocol
 * and nothing else checks that they agree. A rename on the Python side that this
 * file does not learn about is exactly the failure this is here to catch.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  BridgeWriteFailed,
  BridgeWriteRefused,
  bridgeSource,
  bridgeSources,
  openBridge,
  type BridgeConnection,
} from "./bridge";
import { combineSources, type TagPresentation } from "./source";

const URL_4K7T = "https://almagest.lan/s/4K7T92M8";

/** A `WebSocket` that records what was sent and lets a test push frames back. */
class FakeSocket {
  static last: FakeSocket | null = null;

  readyState = 1;
  sent: string[] = [];
  closes = 0;

  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(readonly url: string) {
    FakeSocket.last = this;
  }

  send(payload: string): void {
    this.sent.push(payload);
  }

  close(): void {
    this.closes += 1;
  }

  open(): void {
    this.onopen?.();
  }

  emit(type: string, data: Record<string, unknown>, seq = 1): void {
    this.onmessage?.({
      data: JSON.stringify({ type, seq, at: "2026-08-01T00:00:00Z", data }),
    } as MessageEvent);
  }

  drop(): void {
    this.readyState = 3;
    this.onclose?.();
  }

  get commands(): Record<string, unknown>[] {
    return this.sent.map((raw) => JSON.parse(raw) as Record<string, unknown>);
  }
}

interface Harness {
  connection: BridgeConnection;
  socket: FakeSocket;
  fire(): void;
}

function harness(options: { writeTimeoutMs?: number } = {}): Harness {
  const timers = new Map<number, () => void>();
  let next = 1;
  const connection = openBridge({
    socket: (url) => new FakeSocket(url) as unknown as WebSocket,
    ...(options.writeTimeoutMs === undefined ? {} : { writeTimeoutMs: options.writeTimeoutMs }),
    setTimeout: (fn) => {
      const handle = next++;
      timers.set(handle, fn);
      return handle;
    },
    clearTimeout: (handle) => {
      timers.delete(handle);
    },
  });
  const socket = FakeSocket.last!;
  socket.open();
  return {
    connection,
    socket,
    fire() {
      for (const [handle, fn] of [...timers]) {
        timers.delete(handle);
        fn();
      }
    },
  };
}

function attach(
  socket: FakeSocket,
  overrides: {
    deviceId?: string;
    kind?: string;
    label?: string;
    writes?: boolean;
    readsNdef?: boolean;
  } = {},
): void {
  socket.emit("device.attached", {
    device_id: overrides.deviceId ?? "flipper-usb:a",
    kind: overrides.kind ?? "flipper_rpc",
    label: overrides.label ?? "Flipper Vyvern",
    capabilities: {
      reads_uid: true,
      reads_ndef: overrides.readsNdef ?? true,
      writes_ndef: overrides.writes ?? true,
    },
  });
}

beforeEach(() => {
  FakeSocket.last = null;
});

describe("absence is the common case", () => {
  it("starts with an empty roster and no error", () => {
    const { connection } = harness();
    expect(connection.devices()).toEqual([]);
  });

  it("survives a WebSocket constructor that throws", () => {
    // A blocked mixed-content request throws outright rather than firing
    // `onerror`. Indistinguishable from "no bridge", and treated the same.
    expect(() =>
      openBridge({
        socket: () => {
          throw new Error("blocked");
        },
        setTimeout: () => 1,
        clearTimeout: () => undefined,
      }),
    ).not.toThrow();
  });

  it("says nothing on the console when the socket errors", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { socket } = harness();
    socket.onerror?.();
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });

  it("reconnects after a drop, and backs off rather than storming", () => {
    const { socket, fire } = harness();
    const first = socket;
    first.drop();
    fire();
    expect(FakeSocket.last).not.toBe(first);
  });

  it("empties the roster when the bridge goes away", () => {
    const { connection, socket } = harness();
    attach(socket);
    expect(connection.devices()).toHaveLength(1);
    socket.drop();
    expect(connection.devices()).toEqual([]);
  });
});

describe("the roster is announced, never probed", () => {
  it("reports a reader with the capability set the bridge sent", () => {
    const { connection, socket } = harness();
    attach(socket, { writes: false });
    expect(connection.devices()).toEqual([
      {
        deviceId: "flipper-usb:a",
        kind: "flipper_rpc",
        label: "Flipper Vyvern",
        capabilities: { readsUid: true, readsNdef: true, writesNdef: false },
      },
    ]);
  });

  it("notifies subscribers on every change, and immediately on subscribe", () => {
    const { connection, socket } = harness();
    const seen: number[] = [];
    connection.onDevices((devices) => seen.push(devices.length));
    attach(socket, { deviceId: "station" });
    attach(socket, { deviceId: "flipper-usb:a" });
    socket.emit("device.detached", { device_id: "station", reason: "unplugged" });
    expect(seen).toEqual([0, 1, 2, 1]);
  });

  it("keeps a reader whose kind it has never heard of", () => {
    // A bridge newer than this page still has a working reader on the end of it.
    const { connection, socket } = harness();
    attach(socket, { kind: "acr122u" });
    expect(connection.devices()[0]!.kind).toBe("manual");
  });

  it("preserves the order readers appeared in", () => {
    const { connection, socket } = harness();
    attach(socket, { deviceId: "station" });
    attach(socket, { deviceId: "flipper-usb:a" });
    expect(connection.devices().map((d) => d.deviceId)).toEqual([
      "station",
      "flipper-usb:a",
    ]);
  });
});

describe("taps", () => {
  it("delivers a tap from the device that saw it", () => {
    const { connection, socket } = harness();
    attach(socket);
    const taps: TagPresentation[] = [];
    connection.onTap("flipper-usb:a", (tap) => taps.push(tap));
    socket.emit("tag.seen", {
      device_id: "flipper-usb:a",
      short_id: "4K7T92M8",
      tag_uid: "04:a2:b3:c4:d5:e6:80",
      ndef_url: URL_4K7T,
      via: "ndef",
    });
    expect(taps).toEqual([
      {
        uid: "04A2B3C4D5E680",
        url: URL_4K7T,
        shortId: "4K7T92M8",
        carriesNdef: true,
      },
    ]);
  });

  it("does not cross-deliver between readers", () => {
    const { connection, socket } = harness();
    attach(socket, { deviceId: "station" });
    attach(socket, { deviceId: "flipper-usb:a" });
    const station: TagPresentation[] = [];
    connection.onTap("station", (tap) => station.push(tap));
    socket.emit("tag.seen", { device_id: "flipper-usb:a", tag_uid: "04AA", ndef_url: null });
    expect(station).toEqual([]);
  });

  it("takes carriesNdef from the reader, not from whether a url arrived", () => {
    // The mistake this prevents: a reader that cannot look at user memory
    // reporting `url: null`, and a walk reading that as "this tag is blank" and
    // marking a perfectly good sticker as needing a rewrite.
    const { connection, socket } = harness();
    attach(socket, { readsNdef: false });
    const taps: TagPresentation[] = [];
    connection.onTap("flipper-usb:a", (tap) => taps.push(tap));
    socket.emit("tag.seen", { device_id: "flipper-usb:a", tag_uid: "04AA", ndef_url: null });
    expect(taps[0]!.carriesNdef).toBe(false);
  });

  it("stops delivering after unsubscribe", () => {
    const { connection, socket } = harness();
    attach(socket);
    const taps: TagPresentation[] = [];
    const stop = connection.onTap("flipper-usb:a", (tap) => taps.push(tap));
    stop();
    socket.emit("tag.seen", { device_id: "flipper-usb:a", tag_uid: "04AA" });
    expect(taps).toEqual([]);
  });
});

describe("writing names a device", () => {
  it("sends a tag.write and resolves with the read-back", async () => {
    const { connection, socket } = harness();
    attach(socket);
    const promise = connection.write("flipper-usb:a", URL_4K7T);
    const [command] = socket.commands;
    expect(command).toMatchObject({
      type: "tag.write",
      device_id: "flipper-usb:a",
      url: URL_4K7T,
      overwrite: false,
    });
    socket.emit("tag.written", {
      request_id: command!["request_id"],
      device_id: "flipper-usb:a",
      url: URL_4K7T,
      read_back_url: URL_4K7T,
    });
    await expect(promise).resolves.toEqual({ observed: true, url: URL_4K7T });
  });

  it("refuses to overwrite unless asked", async () => {
    const { connection, socket } = harness();
    attach(socket);
    const promise = connection.write("flipper-usb:a", URL_4K7T);
    socket.emit("tag.write_refused", {
      request_id: socket.commands[0]!["request_id"],
      device_id: "flipper-usb:a",
      reason: "not_blank",
      message: "the tag already carries something else",
    });
    await expect(promise).rejects.toBeInstanceOf(BridgeWriteRefused);
    await expect(promise).rejects.toMatchObject({ reason: "not_blank" });
  });

  it("passes overwrite through when it is asked for", () => {
    const { connection, socket } = harness();
    attach(socket);
    void connection.write("flipper-usb:a", URL_4K7T, { overwrite: true }).catch(() => undefined);
    expect(socket.commands[0]).toMatchObject({ overwrite: true });
  });

  it("distinguishes a reader that broke from a tag that refused", async () => {
    // The recovery differs, and after a failure whether anything was written is
    // unknown — ADR 0012's `degraded`.
    const { connection, socket } = harness();
    attach(socket);
    const promise = connection.write("flipper-usb:a", URL_4K7T);
    socket.emit("tag.write_failed", {
      request_id: socket.commands[0]!["request_id"],
      device_id: "flipper-usb:a",
      message: "the port vanished",
    });
    await expect(promise).rejects.toBeInstanceOf(BridgeWriteFailed);
  });

  it("reports a write that never came back as a failure, not a refusal", async () => {
    // It may well have happened. Calling it a refusal would tell the user
    // nothing was written when something might have been.
    const { connection, socket, fire } = harness();
    attach(socket);
    const promise = connection.write("flipper-usb:a", URL_4K7T);
    fire();
    await expect(promise).rejects.toBeInstanceOf(BridgeWriteFailed);
  });

  it("refuses a reader that cannot write, without sending anything", async () => {
    const { connection, socket } = harness();
    attach(socket, { writes: false });
    await expect(connection.write("flipper-usb:a", URL_4K7T)).rejects.toMatchObject({
      reason: "unsupported",
    });
    expect(socket.sent).toEqual([]);
  });

  it("refuses an unknown device rather than guessing one", async () => {
    const { connection } = harness();
    await expect(connection.write("flipper-usb:ghost", URL_4K7T)).rejects.toMatchObject({
      reason: "unsupported",
    });
  });

  it("refuses when the agent is too old to have a write path", async () => {
    const { connection, socket } = harness();
    socket.emit("station.hello", { protocol: 1, agent: "almagest-deviceagent/0.1.0" }, 0);
    attach(socket);
    await expect(connection.write("flipper-usb:a", URL_4K7T)).rejects.toMatchObject({
      reason: "unsupported",
    });
  });

  it("ignores another tab's write outcome", () => {
    // The stream is a broadcast by design: two kiosks on one bench must not
    // disagree about whether a tag was written.
    const { connection, socket } = harness();
    attach(socket);
    expect(() =>
      socket.emit("tag.written", {
        request_id: "someone-else",
        device_id: "flipper-usb:a",
        read_back_url: URL_4K7T,
      }),
    ).not.toThrow();
    expect(connection.devices()).toHaveLength(1);
  });

  it("fails every pending write when the bridge disconnects", async () => {
    const { connection, socket } = harness();
    attach(socket);
    const promise = connection.write("flipper-usb:a", URL_4K7T);
    socket.drop();
    await expect(promise).rejects.toBeInstanceOf(BridgeWriteFailed);
  });
});

describe("as a TagSource", () => {
  it("exposes no write property at all when the reader cannot write", () => {
    // Not a stub that rejects: `combineSources` picks a writer by
    // `write !== undefined`, so a stub would let a read-only reader shadow a
    // writing one.
    const { connection, socket } = harness();
    attach(socket, { writes: false });
    const [source] = bridgeSources(connection);
    expect(source!.write).toBeUndefined();
    expect(source!.canWrite).toBe(false);
  });

  it("combines with the other readers rather than replacing them", () => {
    const { connection, socket } = harness();
    attach(socket, { deviceId: "station", kind: "station_pn532", label: "Station PN532" });
    attach(socket, { deviceId: "flipper-usb:a", label: "Flipper Vyvern" });
    const combined = combineSources(...bridgeSources(connection));
    expect(combined.label).toBe("Station PN532 · Flipper Vyvern");
    expect(combined.canWrite).toBe(true);
  });

  it("delivers taps through the combined source", () => {
    const { connection, socket } = harness();
    attach(socket, { deviceId: "station" });
    attach(socket, { deviceId: "flipper-usb:a" });
    const taps: TagPresentation[] = [];
    combineSources(...bridgeSources(connection)).subscribe((tap) => taps.push(tap));
    socket.emit("tag.seen", { device_id: "station", tag_uid: "04AA", ndef_url: null });
    socket.emit("tag.seen", { device_id: "flipper-usb:a", tag_uid: "04BB", ndef_url: null });
    expect(taps.map((tap) => tap.uid)).toEqual(["04AA", "04BB"]);
  });

  it("carries the device kind through, so the server records who bound a tag", () => {
    const { connection, socket } = harness();
    attach(socket, { deviceId: "station", kind: "station_pn532" });
    const [device] = connection.devices();
    expect(bridgeSource(connection, device!).kind).toBe("station_pn532");
  });
});

describe("malformed input", () => {
  it("ignores a non-JSON frame", () => {
    const { connection, socket } = harness();
    socket.onmessage?.({ data: "not json" } as MessageEvent);
    expect(connection.devices()).toEqual([]);
  });

  it("ignores a binary frame", () => {
    const { connection, socket } = harness();
    socket.onmessage?.({ data: new ArrayBuffer(4) } as MessageEvent);
    expect(connection.devices()).toEqual([]);
  });

  it("ignores an event type it has never heard of", () => {
    const { connection, socket } = harness();
    expect(() => socket.emit("weight.stable", { grams: 12 })).not.toThrow();
    expect(connection.devices()).toEqual([]);
  });

  it("ignores a device.attached with no id", () => {
    const { connection, socket } = harness();
    socket.emit("device.attached", { kind: "flipper_rpc", label: "?" });
    expect(connection.devices()).toEqual([]);
  });
});
