/**
 * The scan session — where the client idempotency key is born.
 *
 * `PLAN.md` workflow 3 is specific about the ordering: the `uuid4` is attached
 * **at scan time**, not at commit time. That is the whole point. If the key were
 * minted when Commit was pressed, a double tap on a flaky connection would send
 * two different keys and record two movements; minting it at the scan means every
 * retry of *that one intent* carries the same key and the server collapses them.
 *
 * The store lives outside React so the key survives navigating from the scan
 * screen to the bin screen to the lot screen, and so it can be tested without a
 * renderer.
 */

import { PayloadHoldOff, SCAN_DEBOUNCE_MS } from "./holdoff";

export interface ScanSession {
  /** Sent as `client_op_id` on the write this scan leads to. */
  readonly clientOpId: string;
  /** The payload as decoded, or what the user typed on the manual path. */
  readonly code: string;
  /** Whatever the decoder called the format. Recorded, never validated. */
  readonly symbology: string | null;
  readonly scannedAt: number;
}

/**
 * A uuid4.
 *
 * `crypto.randomUUID` is **only available in a secure context**, and the app has
 * to stay fully usable over plain HTTP with no camera and no NFC (ADR 0001), so
 * the fallback is not hypothetical — it is the path taken on
 * `http://<lan-ip>:5173` on a phone. `getRandomValues` has no such restriction.
 */
export function uuid4(): string {
  const webcrypto: Crypto | undefined = globalThis.crypto;
  if (typeof webcrypto?.randomUUID === "function") {
    return webcrypto.randomUUID();
  }

  const bytes = new Uint8Array(16);
  if (typeof webcrypto?.getRandomValues === "function") {
    webcrypto.getRandomValues(bytes);
  } else {
    // No CSPRNG at all. Only reachable in an exotic embedded webview; a
    // colliding key would be observed as a spurious "already recorded", never as
    // a lost movement, so degrading is safer than refusing to work.
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  // Version 4, variant 1.
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40;
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80;

  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20, 32),
  ].join("-");
}

export interface ScanSessionOptions {
  readonly uuid?: () => string;
  readonly now?: () => number;
  readonly debounceMs?: number;
}

export class ScanSessionStore {
  readonly #uuid: () => string;
  readonly #now: () => number;
  readonly #holdOff: PayloadHoldOff;
  readonly #listeners = new Set<() => void>();
  #current: ScanSession | null = null;

  constructor(options: ScanSessionOptions = {}) {
    this.#uuid = options.uuid ?? uuid4;
    this.#now = options.now ?? (() => Date.now());
    this.#holdOff = new PayloadHoldOff(options.debounceMs ?? SCAN_DEBOUNCE_MS, {
      ...(options.now === undefined ? {} : { now: options.now }),
    });
  }

  /**
   * Record a scan and mint its idempotency key.
   *
   * Returns `null` when the scan was dropped — the same payload inside the
   * debounce window, or any payload while a commit is in flight. A dropped scan
   * is not an error and must not be reported as one; it is the double-tap this
   * mechanism exists to swallow.
   */
  scan(code: string, symbology: string | null = null): ScanSession | null {
    if (!this.#holdOff.admit(code)) {
      return null;
    }
    this.#current = {
      clientOpId: this.#uuid(),
      code,
      symbology,
      scannedAt: this.#now(),
    };
    this.#emit();
    return this.#current;
  }

  current(): ScanSession | null {
    return this.#current;
  }

  /** Block further scans for the duration of a commit. */
  beginCommit(): void {
    this.#holdOff.block();
  }

  endCommit(): void {
    this.#holdOff.unblock();
  }

  /**
   * Retire the current key after a write has used it.
   *
   * A key names one movement. Reusing it for a second take from the same bin
   * would come back `replayed: true` with the first take's numbers, which looks
   * exactly like a successful second take and silently loses stock — so the key
   * is spent once and the next operation mints a fresh one.
   */
  spend(): void {
    if (this.#current === null) {
      return;
    }
    this.#current = null;
    this.#emit();
  }

  /** Forget the session entirely, e.g. on leaving the scanner. */
  clear(): void {
    this.#holdOff.forget();
    this.#holdOff.unblock();
    if (this.#current !== null) {
      this.#current = null;
      this.#emit();
    }
  }

  subscribe(listener: () => void): () => void {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  }

  #emit(): void {
    for (const listener of this.#listeners) {
      listener();
    }
  }
}

/** The one session the app shares across screens. */
export const scanSession = new ScanSessionStore();
