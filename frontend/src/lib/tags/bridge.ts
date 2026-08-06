/**
 * Readers that are not in this browser: the device bridge's WebSocket client.
 *
 * `deviceagent` runs on the same machine as the browser and reaches hardware a
 * page cannot — a PN532 on a Pi's UART, a Flipper Zero on a USB cable or over
 * Bluetooth. It has always published a loopback event stream; until ADR 0014
 * nothing in this app had ever opened it, and `station_pn532` was a
 * `TagDeviceKind` with no implementation behind it.
 *
 * Four rules shape everything here, and three of them are about *absence*:
 *
 * **A bridge that is not running is not an error. It is Tuesday.** Almost every
 * page load is a phone with no bridge anywhere. So: no warning, no error toast,
 * no console noise, no retry storm, and nothing on the critical path to first
 * paint. The connection is opened lazily and its failure is silence.
 *
 * **Nothing is probed.** ADR 0012 is scathing about capability probes for
 * readers — `typeof NDEFReader === "function"` answers only "does this browser
 * implement Web NFC", and is true on an Android with the radio switched off.
 * The bridge announces each reader with a capability set on connect and
 * whenever one appears, so this module never guesses what a device can do; it
 * is told, or it has no device.
 *
 * **One socket, several readers, and the client does not choose between them.**
 * A bench can have a PN532 under the platform and a Flipper on a cable. Reading
 * from all of them at once is right — whichever is in your hand is the one you
 * will use — but a *write* must name one, because a write aimed at the wrong
 * reader either fails confusingly or succeeds against whatever tag happened to
 * be in that other reader's field. So `bridgeSources` hands back one `TagSource`
 * per device and lets the walk decide.
 *
 * **The event stream is a broadcast, not a reply channel.** Two kiosk tabs on
 * one bench see the same events, by design, so a write is correlated by a
 * `request_id` this client mints rather than by "the next message".
 */

import type { NfcReadBack } from "../scan/nfc";
import { normalizeUid, type TagDeviceKind, type TagPresentation, type TagSource } from "./source";

/** Where `deviceagent` listens. Loopback, and it refuses to be bound elsewhere. */
export const DEFAULT_BRIDGE_URL = "ws://127.0.0.1:8765";

/**
 * The protocol version this client understands, from `agent/events.py`.
 *
 * A bridge announcing something older cannot write — that half of the protocol
 * did not exist — so it is used read-only rather than refused. Refusing would
 * throw away a perfectly good reader over a feature the walk might not need.
 */
export const BRIDGE_PROTOCOL = 2;

/** How long to wait for a write to come back before giving up on it. */
export const WRITE_TIMEOUT_MS = 25_000;

/**
 * Reconnection backoff. Deliberately unhurried and capped: the common case is
 * that there is no bridge and never will be on this device, and a client
 * retrying briskly for the lifetime of a phone's browser tab is a battery bug.
 */
const RECONNECT_MIN_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

/**
 * How many times to try before concluding there is no bridge on this device.
 *
 * **A failed WebSocket always logs to the console, and the page cannot suppress
 * it** — the error is emitted by the browser, not by us, so it is reachable by no
 * `try`, no handler and no flag. Retrying forever therefore produces an endless
 * `ERR_CONNECTION_REFUSED` scroll in devtools on every page load of every machine
 * without the agent, which is nearly all of them. That is not "degrading in
 * silence", which is what this file claims to do.
 *
 * So a client that has **never** connected gives up after a few attempts: no
 * bridge answered, and on a phone or a desktop without the agent none ever will.
 * One that *has* connected keeps reconnecting for as long as the page lives,
 * because there the agent demonstrably exists and has merely restarted.
 */
const MAX_ATTEMPTS_BEFORE_FIRST_CONNECT = 3;

export interface BridgeCapabilities {
  readonly readsUid: boolean;
  readonly readsNdef: boolean;
  readonly writesNdef: boolean;
}

export interface BridgeDevice {
  readonly deviceId: string;
  readonly kind: TagDeviceKind;
  readonly label: string;
  readonly capabilities: BridgeCapabilities;
}

export interface BridgeConnection {
  /** Attached readers, in the order they appeared. Empty until something is. */
  devices(): readonly BridgeDevice[];
  /** Called whenever the roster changes. Returns an unsubscribe. */
  onDevices(listener: (devices: readonly BridgeDevice[]) => void): () => void;
  /** Taps from one device. Returns an unsubscribe. */
  onTap(deviceId: string, listener: (tap: TagPresentation) => void): () => void;
  /**
   * The tag on one device has been lifted off it. Returns an unsubscribe.
   *
   * Separate from `onTap` rather than a `TagPresentation | null` through it,
   * because most consumers only care about arrivals: a walk binds what it is
   * given and has nothing to do when a tag leaves. Making every one of them
   * handle a null would be a null check per call site to express something two
   * of them want.
   */
  onGone(deviceId: string, listener: () => void): () => void;
  /** Write through one named device, and resolve with what it read back. */
  write(deviceId: string, url: string, options?: { overwrite?: boolean }): Promise<NfcReadBack>;
  /** True once a socket has been open at least once. For diagnostics only. */
  connected(): boolean;
  close(): void;
}

interface Envelope {
  readonly type: string;
  readonly seq: number;
  readonly at: string;
  readonly data: Record<string, unknown>;
}

type SocketFactory = (url: string) => WebSocket;

export interface BridgeOptions {
  readonly url?: string;
  /** Injected in tests; production uses the global. */
  readonly socket?: SocketFactory;
  readonly writeTimeoutMs?: number;
  /** Injected so the reconnect backoff is assertable without real timers. */
  readonly setTimeout?: (fn: () => void, ms: number) => number;
  readonly clearTimeout?: (handle: number) => void;
}

interface PendingWrite {
  resolve(value: NfcReadBack): void;
  reject(error: Error): void;
  timer: number;
}

/**
 * A write the reader refused. **Nothing was written.**
 *
 * `reason` comes from the closed vocabulary `agent/tags.py` defines and both a
 * PN532 and a Flipper draw from, so a screen has one table of answers rather
 * than one per reader:
 *
 * - `no_tag` — the field is empty;
 * - `not_blank` — the tag already carries a URI and `overwrite` was not asked
 *   for. The default everywhere, because the two mistakes are not symmetrical:
 *   refusing costs a toggle, overwriting costs a drawer that answers to another
 *   drawer's short id;
 * - `too_long` — the payload does not fit, refused before a byte was written;
 * - `unsupported` — this reader cannot write, or is already writing;
 * - `read_back_failed` — the write was attempted and the tag did not read back.
 *   **This one did touch the tag** and is ADR 0012's `degraded`.
 */
export class BridgeWriteRefused extends Error {
  readonly reason: string;

  constructor(reason: string, message: string) {
    super(message);
    this.name = "BridgeWriteRefused";
    this.reason = reason;
  }
}

/**
 * The reader broke mid-write, or stopped answering.
 *
 * Kept apart from a refusal because the recovery differs and, critically,
 * **whether anything was written is unknown**. The UID lives in factory-locked
 * pages 0-2, so the tag still identifies itself; the honest next step is to read
 * it back, never to assume either way.
 */
export class BridgeWriteFailed extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BridgeWriteFailed";
  }
}

function readCapabilities(raw: unknown): BridgeCapabilities {
  const source = (raw ?? {}) as Record<string, unknown>;
  return {
    readsUid: source["reads_uid"] === true,
    readsNdef: source["reads_ndef"] === true,
    writesNdef: source["writes_ndef"] === true,
  };
}

const DEVICE_KINDS: readonly TagDeviceKind[] = [
  "phone_webnfc",
  "station_pn532",
  "station_rc522",
  "flipper_rpc",
  "manual",
];

function readKind(raw: unknown): TagDeviceKind {
  // An unknown kind falls back to `manual` rather than being dropped: a bridge
  // newer than this page still has a working reader on the end of it, and the
  // kind only decides what the server records about who bound a tag.
  return DEVICE_KINDS.includes(raw as TagDeviceKind) ? (raw as TagDeviceKind) : "manual";
}

/**
 * Open a connection to the bridge, reconnecting quietly for as long as it lives.
 *
 * Returns immediately with an empty roster; readers appear if and when the
 * bridge answers. There is no "is a bridge available" call, and there must not
 * be: the answer changes when someone plugs a cable in, so anything that
 * resolved once would be stale by the time it was read.
 */
export function openBridge(options: BridgeOptions = {}): BridgeConnection {
  const url = options.url ?? DEFAULT_BRIDGE_URL;
  const makeSocket: SocketFactory = options.socket ?? ((target) => new WebSocket(target));
  const writeTimeoutMs = options.writeTimeoutMs ?? WRITE_TIMEOUT_MS;
  const schedule = options.setTimeout ?? ((fn, ms) => globalThis.setTimeout(fn, ms) as unknown as number);
  const unschedule = options.clearTimeout ?? ((handle) => globalThis.clearTimeout(handle));

  const devices = new Map<string, BridgeDevice>();
  const deviceListeners = new Set<(devices: readonly BridgeDevice[]) => void>();
  const tapListeners = new Map<string, Set<(tap: TagPresentation) => void>>();
  const goneListeners = new Map<string, Set<() => void>>();
  const pending = new Map<string, PendingWrite>();

  let socket: WebSocket | null = null;
  let closed = false;
  let everConnected = false;
  let protocol = BRIDGE_PROTOCOL;
  let backoffMs = RECONNECT_MIN_MS;
  let reconnectTimer: number | null = null;
  /** Consecutive failures since the last success. `everConnected` above is the
   *  other half of the decision — see MAX_ATTEMPTS_BEFORE_FIRST_CONNECT. */
  let failedAttempts = 0;
  let requestCounter = 0;

  const announceDevices = (): void => {
    const snapshot = [...devices.values()];
    for (const listener of deviceListeners) {
      listener(snapshot);
    }
  };

  const failEveryPendingWrite = (message: string): void => {
    for (const [, entry] of pending) {
      unschedule(entry.timer);
      entry.reject(new BridgeWriteFailed(message));
    }
    pending.clear();
  };

  const settle = (
    requestId: unknown,
    settlement: (entry: PendingWrite) => void,
  ): void => {
    if (typeof requestId !== "string") {
      return;
    }
    const entry = pending.get(requestId);
    if (entry === undefined) {
      // Another tab's write. The stream is a broadcast on purpose — two kiosks
      // on one bench must not disagree about whether a tag was written — so
      // seeing someone else's outcome is normal, not an error.
      return;
    }
    pending.delete(requestId);
    unschedule(entry.timer);
    settlement(entry);
  };

  const handle = (envelope: Envelope): void => {
    const data = envelope.data ?? {};
    switch (envelope.type) {
      case "station.hello": {
        const announced = data["protocol"];
        protocol = typeof announced === "number" ? announced : 1;
        break;
      }
      case "device.attached": {
        const deviceId = String(data["device_id"] ?? "");
        if (deviceId === "") {
          break;
        }
        devices.set(deviceId, {
          deviceId,
          kind: readKind(data["kind"]),
          label: String(data["label"] ?? deviceId),
          capabilities: readCapabilities(data["capabilities"]),
        });
        announceDevices();
        break;
      }
      case "device.detached": {
        if (devices.delete(String(data["device_id"] ?? ""))) {
          announceDevices();
        }
        break;
      }
      case "tag.seen": {
        const deviceId = String(data["device_id"] ?? "");
        const listeners = tapListeners.get(deviceId);
        if (listeners === undefined || listeners.size === 0) {
          break;
        }
        const uid = data["tag_uid"];
        const device = devices.get(deviceId);
        const tap: TagPresentation = {
          uid: typeof uid === "string" ? normalizeUid(uid) : null,
          url: typeof data["ndef_url"] === "string" ? (data["ndef_url"] as string) : null,
          shortId: typeof data["short_id"] === "string" ? (data["short_id"] as string) : null,
          // Whether user memory was looked at is a property of the *reader*, not
          // of what came back — so it comes from the capability set. A `null`
          // url from a reader that cannot read NDEF must never be read as "this
          // tag is blank", which is the mistake that marks a good sticker as
          // needing a rewrite.
          carriesNdef: device?.capabilities.readsNdef ?? false,
        };
        for (const listener of [...listeners]) {
          listener(tap);
        }
        break;
      }
      case "tag.gone": {
        // The field is empty again. Presence is stated by its edges now: one
        // `tag.seen` when a tag arrives, one of these when it leaves, silence in
        // between meaning "nothing changed" rather than "nobody is looking".
        // Before this the agent re-published `tag.seen` for as long as a tag lay
        // in the field, so every client had to infer presence from a drumbeat
        // and none could tell the difference between a tag lifted and a bridge
        // that had stopped talking.
        const deviceId = String(data["device_id"] ?? "");
        for (const listener of [...(goneListeners.get(deviceId) ?? [])]) {
          listener();
        }
        break;
      }
      case "tag.written": {
        const readBack = data["read_back_url"];
        settle(data["request_id"], (entry) => {
          entry.resolve({
            observed: typeof readBack === "string",
            url: typeof readBack === "string" ? readBack : null,
          });
        });
        break;
      }
      case "tag.write_refused": {
        settle(data["request_id"], (entry) => {
          entry.reject(
            new BridgeWriteRefused(
              String(data["reason"] ?? "unsupported"),
              String(data["message"] ?? "the reader refused the write"),
            ),
          );
        });
        break;
      }
      case "tag.write_failed": {
        settle(data["request_id"], (entry) => {
          entry.reject(new BridgeWriteFailed(String(data["message"] ?? "the reader failed")));
        });
        break;
      }
      default:
        // Unknown types are ignored, which is what the envelope is for. A bridge
        // newer than this page must keep working.
        break;
    }
  };

  const connect = (): void => {
    if (closed) {
      return;
    }
    let next: WebSocket;
    try {
      next = makeSocket(url);
    } catch {
      // Constructing a WebSocket can throw outright — a blocked mixed-content
      // request, a disallowed URL. Indistinguishable from "no bridge", and
      // treated the same.
      scheduleReconnect();
      return;
    }
    socket = next;

    next.onopen = () => {
      everConnected = true;
      backoffMs = RECONNECT_MIN_MS;
    };
    next.onmessage = (event: MessageEvent) => {
      if (typeof event.data !== "string") {
        return;
      }
      let envelope: Envelope;
      try {
        envelope = JSON.parse(event.data) as Envelope;
      } catch {
        return;
      }
      if (typeof envelope?.type !== "string") {
        return;
      }
      handle(envelope);
    };
    next.onerror = () => {
      // Swallowed. A failed connection to a bridge that is not running is the
      // overwhelmingly common case and must not reach the console.
    };
    next.onclose = () => {
      socket = null;
      if (devices.size > 0) {
        devices.clear();
        announceDevices();
      }
      failedAttempts += 1;
      failEveryPendingWrite("the bridge disconnected");
      scheduleReconnect();
    };
  };

  const scheduleReconnect = (): void => {
    if (closed || reconnectTimer !== null) {
      return;
    }
    // See MAX_ATTEMPTS_BEFORE_FIRST_CONNECT: nothing has ever answered here, so
    // stop rather than log a refused connection every thirty seconds forever.
    if (!everConnected && failedAttempts >= MAX_ATTEMPTS_BEFORE_FIRST_CONNECT) {
      return;
    }
    const delay = backoffMs;
    backoffMs = Math.min(backoffMs * 2, RECONNECT_MAX_MS);
    reconnectTimer = schedule(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  };

  connect();

  return {
    devices: () => [...devices.values()],
    onDevices(listener) {
      deviceListeners.add(listener);
      listener([...devices.values()]);
      return () => {
        deviceListeners.delete(listener);
      };
    },
    onTap(deviceId, listener) {
      const listeners = tapListeners.get(deviceId) ?? new Set();
      listeners.add(listener);
      tapListeners.set(deviceId, listeners);
      return () => {
        listeners.delete(listener);
      };
    },
    onGone(deviceId, listener) {
      const listeners = goneListeners.get(deviceId) ?? new Set();
      listeners.add(listener);
      goneListeners.set(deviceId, listeners);
      return () => {
        listeners.delete(listener);
      };
    },
    write(deviceId, writeUrl, writeOptions) {
      const device = devices.get(deviceId);
      if (device === undefined) {
        return Promise.reject(
          new BridgeWriteRefused("unsupported", `no reader called ${deviceId}`),
        );
      }
      if (!device.capabilities.writesNdef) {
        return Promise.reject(
          new BridgeWriteRefused("unsupported", `${device.label} cannot write`),
        );
      }
      if (protocol < BRIDGE_PROTOCOL) {
        return Promise.reject(
          new BridgeWriteRefused(
            "unsupported",
            "this device agent is too old to write tags; update it",
          ),
        );
      }
      const live = socket;
      if (live === null || live.readyState !== 1) {
        return Promise.reject(new BridgeWriteFailed("the bridge is not connected"));
      }

      requestCounter += 1;
      const requestId = `w${requestCounter}-${deviceId}`;
      return new Promise<NfcReadBack>((resolve, reject) => {
        const timer = schedule(() => {
          pending.delete(requestId);
          // A write that never came back is a *failure*, not a refusal: it may
          // well have happened. Saying otherwise would tell the user nothing was
          // written when something might have been.
          reject(new BridgeWriteFailed("the reader did not answer in time"));
        }, writeTimeoutMs);
        pending.set(requestId, { resolve, reject, timer });
        live.send(
          JSON.stringify({
            type: "tag.write",
            request_id: requestId,
            device_id: deviceId,
            url: writeUrl,
            overwrite: writeOptions?.overwrite ?? false,
          }),
        );
      });
    },
    connected: () => everConnected && socket !== null,
    close() {
      closed = true;
      if (reconnectTimer !== null) {
        unschedule(reconnectTimer);
        reconnectTimer = null;
      }
      failEveryPendingWrite("the bridge was closed");
      socket?.close();
      socket = null;
    },
  };
}

/**
 * One `TagSource` per attached reader, ready for `combineSources`.
 *
 * A snapshot: call it again when the roster changes (`onDevices`). Returning
 * live-updating sources instead would hide the one thing a walk needs to react
 * to — a reader appearing or vanishing mid-walk is exactly when the screen must
 * say something.
 */
export function bridgeSources(connection: BridgeConnection): TagSource[] {
  return connection.devices().map((device) => bridgeSource(connection, device));
}

/** The `TagSource` for one bridged reader. */
export function bridgeSource(connection: BridgeConnection, device: BridgeDevice): TagSource {
  const source: TagSource = {
    kind: device.kind,
    label: device.label,
    canWrite: device.capabilities.writesNdef,
    subscribe(listener) {
      return connection.onTap(device.deviceId, listener);
    },
  };
  if (!device.capabilities.writesNdef) {
    // No `write` property at all, rather than one that rejects: `combineSources`
    // picks a writer by `write !== undefined`, so a stub would make a reader
    // that cannot write shadow one that can.
    return source;
  }
  return {
    ...source,
    write: (url, options) => connection.write(device.deviceId, url, options),
  };
}
