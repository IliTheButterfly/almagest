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

interface NdefReaderLike extends EventTarget {
  scan(options?: { signal?: AbortSignal }): Promise<void>;
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
