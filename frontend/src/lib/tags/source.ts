/**
 * What a tag reader is, on the browser side — the sibling of `deviceagent`'s
 * `agent.tags.TagSource`, deliberately the same shape.
 *
 * The two walks (provisioning, verification) must not know *how* a tag arrived.
 * There are three ways and they are not interchangeable:
 *
 * - **Web NFC**, Chrome for Android only. Reads both carriers and can write.
 * - **The station's PN532**, reached over the agent's WebSocket. Reads both
 *   carriers, cannot write (no write path exists on the agent yet).
 * - **A UID typed in by hand.** No hardware at all, which is the state of this
 *   setup today: there is no reader on the bench, and the walks still have to be
 *   usable and testable. It reads *neither* carrier — a human copying the number
 *   off a tag is not evidence about what user memory holds.
 *
 * That last distinction is why `carriesNdef` exists and why it is not simply
 * `url !== null`. A typed UID and a genuinely blank tag both arrive with no URL,
 * and treating them the same would let a keyboard mark a perfectly good sticker
 * as needing a rewrite. The server enforces the same rule from the other side
 * (`CheckRequest.carries_ndef`); this is where the honest answer is produced.
 */

import { PayloadHoldOff } from "../scan/holdoff";
import {
  openNfcScan,
  TAG_DEBOUNCE_MS,
  type NfcReadBack,
  type NfcScanSession,
} from "../scan/nfc";

/**
 * One tap: what a single reading saw.
 *
 * **Three carriers, and which ones a reader produces is the whole difference
 * between readers.** A PN532 or Web NFC hands back the UID and the URI record. A
 * USB wedge — a Flipper running Antlia, a barcode scanner — hands back only the
 * *short id*, because it types what the tag means rather than what the tag is. A
 * human types whichever they can read.
 *
 * That matters because the two walks need a `tag_uid` and cannot work from a
 * short id: binding a tag is a claim about a specific piece of silicon. Confirming
 * *which container is in my hand* works from any of the three. So a reader is
 * never "supported" or not; it is capable of some steps and not others, and the
 * screens say which.
 */
export interface TagPresentation {
  /** The anticollision UID, upper-case hex. Null when this reader cannot give one. */
  readonly uid: string | null;
  /** The NDEF URI record, verbatim. Null when the tag carries none. */
  readonly url: string | null;
  /** A bare short id (`4K7T92M8`), when that is all the reader emits. */
  readonly shortId: string | null;
  /**
   * Did this reader look at user memory at all? False for a typed UID and for a
   * wedge, so a `url: null` from a keyboard never means "this tag is blank".
   */
  readonly carriesNdef: boolean;
}

/**
 * Matches the server's `ProvisioningDevice`, which records who bound a tag.
 *
 * `station_pn532` and `flipper_rpc` both arrive over the device bridge's
 * WebSocket (ADR 0013) rather than from anything in this browser — which is the
 * point of them. Web NFC is Chromium-on-Android only, so without the bridge a
 * desktop, an iPhone and the Pi kiosk can all read a tag by some means and none
 * of them can write one.
 */
export type TagDeviceKind =
  | "phone_webnfc"
  | "station_pn532"
  | "flipper_rpc"
  | "manual";

export interface TagSource {
  readonly kind: TagDeviceKind;
  /** Human sentence for the walk's status line. */
  readonly label: string;
  /** Whether `write` exists. A walk hides the write affordance when it does not. */
  readonly canWrite: boolean;
  /** Listen for taps. Returns an unsubscribe that must be safe to call twice. */
  subscribe(listener: (tap: TagPresentation) => void): () => void;
  /**
   * Write the payload to the tag in the field and read it back.
   *
   * Present only when `canWrite`. Rejects when the write did not happen (no tag,
   * a non-blank tag without `overwrite`, a platform refusal); a read-back that
   * disagrees resolves instead, because that is a result.
   */
  write?(url: string, options?: { overwrite?: boolean }): Promise<NfcReadBack>;
}

/**
 * Ignore the same tag for `windowMs` after it is acted on.
 *
 * PLAN.md's 400 ms debounce, and it is not cosmetic: a tag sitting in the field
 * fires `reading` repeatedly, so without this one physical tap binds the cursor
 * slot, auto-advances, and binds the *next* slot to the same tag — quietly, and
 * discovered only by a verification walk much later.
 *
 * `PayloadHoldOff` rather than a fresh timer, because it is the same debounce the
 * camera decoder already uses at a different window, keyed per payload so two
 * drawers tapped in quick succession are never mistaken for one bouncing. Here
 * the payload is the UID. `refreshWhileSuppressed` is on for the same reason the
 * decoder sets it: a tag left lying on the reader is seen continuously, and a
 * fixed window would let it fire again every 400 ms.
 */
export function debounceTaps(
  listener: (tap: TagPresentation) => void,
  options?: { windowMs?: number; now?: () => number },
): (tap: TagPresentation) => void {
  const holdOff = new PayloadHoldOff(options?.windowMs ?? TAG_DEBOUNCE_MS, {
    ...(options?.now === undefined ? {} : { now: options.now }),
    refreshWhileSuppressed: true,
  });
  return (tap) => {
    // Keyed on whichever carrier this reader produced, so a wedge emitting short
    // ids debounces per container rather than every wedge read colliding on "".
    if (holdOff.admit(tap.uid ?? tap.url ?? tap.shortId ?? "")) {
      listener(tap);
    }
  };
}

/**
 * Canonicalise a UID the way the server will.
 *
 * Readers render the same seven bytes as `04:1A:2B…`, `04-1a-2b…` or bare hex,
 * and `idcodec.tagpayload.normalize_tag_uid` folds all of them to the same
 * string. Doing it here too is not a second implementation of a *rule* — the
 * server still normalises what it receives — it is so the walk can compare a tap
 * against what it already displayed without a round trip.
 */
export function normalizeUid(raw: string): string {
  return raw.replace(/[\s:-]+/g, "").toUpperCase();
}

/** Web NFC, when the browser has it. Throws `NfcUnavailableError` when it does not. */
export function webNfcTagSource(options?: { readBackTimeoutMs?: number }): TagSource {
  let session: NfcScanSession | null = null;
  const listeners = new Set<(tap: TagPresentation) => void>();

  const ensure = (): NfcScanSession => {
    session ??= openNfcScan({
      ...(options?.readBackTimeoutMs === undefined
        ? {}
        : { readBackTimeoutMs: options.readBackTimeoutMs }),
      onReading: (reading) => {
        const tap: TagPresentation = {
          uid: reading.serialNumber === null ? null : normalizeUid(reading.serialNumber),
          url: reading.url,
          shortId: null,
          carriesNdef: true,
        };
        for (const listener of listeners) {
          listener(tap);
        }
      },
    });
    return session;
  };

  return {
    kind: "phone_webnfc",
    label: "Phone (Web NFC)",
    canWrite: true,
    subscribe(listener) {
      listeners.add(listener);
      ensure();
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) {
          session?.close();
          session = null;
        }
      };
    },
    write(url, writeOptions) {
      return ensure().write(url, writeOptions);
    },
  };
}

/** A hand-typed UID. Every walk keeps this path, because every reader can be absent. */
export interface ManualTagSource extends TagSource {
  /** Feed one UID, as if a reader had just seen it. */
  present(uid: string): void;
}

export function manualTagSource(): ManualTagSource {
  const listeners = new Set<(tap: TagPresentation) => void>();
  return {
    kind: "manual",
    label: "Typed by hand",
    canWrite: false,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    present(uid) {
      // `carriesNdef: false` — a human reading a number off a sticker has said
      // nothing about what is in its user memory, and must not be able to mark a
      // working tag degraded.
      const tap: TagPresentation = {
        uid: normalizeUid(uid),
        url: null,
        shortId: null,
        carriesNdef: false,
      };
      for (const listener of listeners) {
        listener(tap);
      }
    },
  };
}

/**
 * Listen to several readers at once.
 *
 * The right default for every screen that *confirms* a container, because the
 * readers are not alternatives the user should have to choose between: a phone
 * has Web NFC, the same laptop has a Flipper on USB, and both have a keyboard.
 * Asking "which reader?" before a scan is a question with no useful answer —
 * whichever one is in your hand is the one you will use.
 *
 * `canWrite` is true when *any* member can write, and `write` goes to the first
 * that can. Writing is only ever the provisioning walk, which selects one reader
 * deliberately, so this is a convenience rather than a routing decision.
 */
export function combineSources(...sources: readonly TagSource[]): TagSource {
  const writable = sources.find((candidate) => candidate.write !== undefined);
  const combined: TagSource = {
    kind: sources[0]?.kind ?? "manual",
    label: sources.map((candidate) => candidate.label).join(" · "),
    canWrite: writable !== undefined,
    subscribe(listener) {
      const stops = sources.map((candidate) => {
        try {
          return candidate.subscribe(listener);
        } catch {
          // One reader being unavailable must never silence the others — that is
          // the entire failure this function exists to prevent.
          return () => undefined;
        }
      });
      return () => {
        for (const stop of stops) {
          stop();
        }
      };
    },
  };
  if (writable?.write !== undefined) {
    const write = writable.write.bind(writable);
    return { ...combined, write };
  }
  return combined;
}
