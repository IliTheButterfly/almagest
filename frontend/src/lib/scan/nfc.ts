/**
 * Web NFC reads, as a one-shot.
 *
 * Chrome for Android only — not iOS at any URL, not desktop Chromium, and so not
 * the Pi kiosk. That is a permanent per-platform capability, not something to
 * polyfill, so callers feature-detect and render `nfcNotice()` instead of an
 * affordance that cannot work.
 *
 * **NDEF first, UID as the fallback.** The tag's NDEF URI record is the payload
 * (`https://<host>/s/{short_id}`); the serial number is only a secondary handle,
 * useful for a tag whose NDEF was never written or was corrupted. Nothing mutable
 * is ever read from or written to a tag — a tag is a foreign key, not a record.
 *
 * There are no types for Web NFC in `lib.dom`, so the surface used here is
 * declared locally rather than pulling in a types package for four members.
 */

export interface NfcReading {
  /** The NDEF URI record, when the tag had one. */
  readonly url: string | null;
  /** The tag's serial number, as reported by the platform. */
  readonly serialNumber: string | null;
}

interface NdefRecordLike {
  readonly recordType: string;
  readonly data?: DataView;
  readonly encoding?: string;
}

interface NdefMessageLike {
  readonly records: readonly NdefRecordLike[];
}

interface NdefReadingEventLike extends Event {
  readonly serialNumber?: string;
  readonly message?: NdefMessageLike;
}

/** One record in a message being written. Only the URL form is ever used here. */
export interface NdefWriteRecord {
  readonly recordType: "url";
  readonly data: string;
}

interface NdefReaderLike extends EventTarget {
  scan(options?: { signal?: AbortSignal }): Promise<void>;
  write(
    message: { records: readonly NdefWriteRecord[] },
    options?: { overwrite?: boolean; signal?: AbortSignal },
  ): Promise<void>;
}

type NdefReaderConstructor = new () => NdefReaderLike;

function readerConstructor(): NdefReaderConstructor | null {
  const candidate = (globalThis as { NDEFReader?: unknown }).NDEFReader;
  return typeof candidate === "function" ? (candidate as NdefReaderConstructor) : null;
}

function firstUrl(message: NdefMessageLike | undefined): string | null {
  for (const record of message?.records ?? []) {
    if (record.recordType !== "url" && record.recordType !== "absolute-url") {
      continue;
    }
    if (record.data === undefined) {
      continue;
    }
    try {
      return new TextDecoder(record.encoding ?? "utf-8").decode(record.data);
    } catch {
      return null;
    }
  }
  return null;
}

export class NfcUnavailableError extends Error {
  constructor() {
    super("Web NFC is not available in this browser");
    this.name = "NfcUnavailableError";
  }
}

/**
 * A tag was already written and `overwrite` was not asked for.
 *
 * The default is blank-tags-only, and this is what enforcing that feels like.
 * PLAN.md: with no security requirement, the real risk of tag writing is not an
 * attacker, it is *a left-open write screen silently overwriting a provisioned
 * drawer* — so the platform's own `overwrite: false` does the refusing, and the
 * screen offers an explicit toggle rather than making every write destructive.
 */
export class TagNotBlankError extends Error {
  constructor() {
    super("That tag already carries a record. Turn on overwrite to replace it.");
    this.name = "TagNotBlankError";
  }
}

/** How long a write screen stays armed before disarming itself. */
export const WRITE_TIMEOUT_MS = 20_000;

/** How long the same tag is ignored after it has been acted on. */
export const TAG_DEBOUNCE_MS = 400;

function isNotAllowed(cause: unknown): boolean {
  // Chrome rejects a non-blank write with NotAllowedError, the same name it uses
  // for a denied permission. Distinguished by *when*: a permission failure
  // rejects `scan()` before any tag is involved, and this one can only happen
  // after a tag has answered, which is the only call site.
  return cause instanceof DOMException && cause.name === "NotAllowedError";
}

/**
 * An open Web NFC scan, plus the ability to write whatever is in the field.
 *
 * **Why a session rather than a bare `writeTag(url)`.** `NDEFReader.write()` waits
 * for a tag by itself, so a one-shot write is possible — but then the read-back
 * needs a *second* tap, because the tag has to leave and re-enter the field for a
 * fresh `scan()` to fire. "Write the NDEF URI → read back to verify" would cost
 * two taps per drawer against a 2-3 s per drawer budget. Holding one scan open
 * for the whole walk means the same physical tap that triggered the write also
 * delivers the read-back, and the walk stays walking-paced.
 *
 * The session never resolves or rejects on its own; it is stopped by `close()` or
 * by the caller's `AbortSignal`. Errors go to `onError` because a `readingerror`
 * mid-walk is one bad tap, not the end of the walk.
 */
export interface NfcScanSession {
  /**
   * Write `url` to whatever tag is in the field, then read it back.
   *
   * Rejects only when the write itself did not happen. A write that succeeded
   * and a read-back that disagrees is a *result*, not an error — that is the
   * degraded tag the verify screen exists to flag.
   */
  write(url: string, options?: { overwrite?: boolean }): Promise<NfcReadBack>;
  close(): void;
}

/**
 * What came back off the tag after a write.
 *
 * **`observed` is the load-bearing field, and collapsing it into `url: null`
 * would be a bug that cries wolf.** Chrome fires `reading` for a tag entering the
 * field, and a tag that never left it may not fire again — so "no read-back
 * arrived" is common and means nothing about the tag, while "read back, and it is
 * not our URI" means the write really did fail. Reported as *unverified* and
 * *degraded* respectively; only the second sends anyone to rewrite a sticker.
 */
export interface NfcReadBack {
  /** True when a fresh reading arrived after the write, so `url` is evidence. */
  readonly observed: boolean;
  /** The URI that reading carried, or null if it carried none. */
  readonly url: string | null;
}

/** How long to wait for the post-write reading before calling it unobserved. */
export const READ_BACK_TIMEOUT_MS = 1_500;

/**
 * Open a continuous scan. Every tap calls `onReading`.
 *
 * No debounce here on purpose: a raw tap stream is what the platform gives, and
 * *which* repeats matter is a policy that differs per screen — a provisioning
 * walk must ignore the same tag for 400 ms so one tap cannot bind two drawers,
 * while a verification walk re-reading a drawer wants every reading. See
 * `lib/tags/source.ts`, where that policy lives.
 */
export function openNfcScan(options: {
  onReading: (reading: NfcReading) => void;
  onError?: (cause: unknown) => void;
  signal?: AbortSignal;
  /** Overridable so a test does not have to wait out a real timeout. */
  readBackTimeoutMs?: number;
}): NfcScanSession {
  const Reader = readerConstructor();
  if (Reader === null) {
    throw new NfcUnavailableError();
  }

  const controller = new AbortController();
  const reader = new Reader();
  const readBackTimeoutMs = options.readBackTimeoutMs ?? READ_BACK_TIMEOUT_MS;
  let awaitingReadBack: ((reading: NfcReading) => void) | null = null;

  options.signal?.addEventListener("abort", () => controller.abort(), { once: true });

  reader.addEventListener(
    "reading",
    (event: Event) => {
      const source = event as NdefReadingEventLike;
      const reading: NfcReading = {
        url: firstUrl(source.message),
        serialNumber: source.serialNumber ?? null,
      };
      const pending = awaitingReadBack;
      awaitingReadBack = null;
      // A reading that answers a write is consumed by the write, not delivered
      // to the walk: the walk would read it as a second tap on the drawer it has
      // just finished, and auto-advance past the next one.
      if (pending !== null) {
        pending(reading);
        return;
      }
      options.onReading(reading);
    },
    { signal: controller.signal },
  );

  reader.addEventListener("readingerror", () => options.onError?.(new Error("Unreadable tag.")), {
    signal: controller.signal,
  });

  reader.scan({ signal: controller.signal }).catch((cause: unknown) => {
    if (!controller.signal.aborted) {
      options.onError?.(cause);
    }
  });

  return {
    async write(url, writeOptions) {
      // **Armed before the write, not after.** `reader.write()` resolves a
      // microtask before anything downstream of `await` runs, and the platform is
      // free to fire the post-write `reading` in that gap — so installing the
      // handler afterwards loses the read-back *and* hands it to the walk as a
      // fresh tap, which advances the cursor past a drawer nobody touched.
      let deliver: ((reading: NfcReading) => void) | null = null;
      const readBack = new Promise<NfcReadBack>((resolve) => {
        deliver = (reading) => resolve({ observed: true, url: reading.url });
      });
      awaitingReadBack = deliver;

      try {
        await reader.write(
          { records: [{ recordType: "url", data: url }] },
          { overwrite: writeOptions?.overwrite ?? false, signal: controller.signal },
        );
      } catch (cause) {
        // Nothing was written, so nothing will read back. Disarm, or the next
        // ordinary tap is swallowed as an answer to a write that never happened.
        if (awaitingReadBack === deliver) {
          awaitingReadBack = null;
        }
        throw isNotAllowed(cause) ? new TagNotBlankError() : cause;
      }

      const timeout = new Promise<NfcReadBack>((resolve) => {
        setTimeout(() => {
          if (awaitingReadBack === deliver) {
            awaitingReadBack = null;
          }
          resolve({ observed: false, url: null });
        }, readBackTimeoutMs);
      });
      return await Promise.race([readBack, timeout]);
    },
    close() {
      awaitingReadBack = null;
      controller.abort();
    },
  };
}

/**
 * Wait for one tag and return what it carried.
 *
 * Resolves on the first reading and stops scanning. A tag with no NDEF record
 * still resolves — with `url: null` and a serial number — because the UID
 * fallback is the whole reason a blank or damaged tag is recoverable.
 */
export function readOneTag(signal?: AbortSignal): Promise<NfcReading> {
  const Reader = readerConstructor();
  if (Reader === null) {
    return Promise.reject(new NfcUnavailableError());
  }

  return new Promise<NfcReading>((resolve, reject) => {
    const controller = new AbortController();
    const reader = new Reader();

    const stop = (): void => {
      controller.abort();
    };
    signal?.addEventListener("abort", stop, { once: true });

    reader.addEventListener(
      "reading",
      (event: Event) => {
        const reading = event as NdefReadingEventLike;
        stop();
        resolve({
          url: firstUrl(reading.message),
          serialNumber: reading.serialNumber ?? null,
        });
      },
      { signal: controller.signal },
    );

    reader.addEventListener(
      "readingerror",
      () => {
        stop();
        reject(new Error("The tag could not be read. Move the phone and try again."));
      },
      { signal: controller.signal },
    );

    reader.scan({ signal: controller.signal }).catch((cause: unknown) => {
      stop();
      reject(cause instanceof Error ? cause : new Error(String(cause)));
    });
  });
}
