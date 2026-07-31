/**
 * A cabinet of NTAG213s that does not exist, behaving the way real ones do.
 *
 * **Why this ships in the app and not only in the tests.** There is no NFC reader
 * on this setup and none on order, so without a simulated one the provisioning
 * and verification walks would be code nobody could run until hardware arrives —
 * and "it compiles" is not evidence that a walk works. It is the same argument
 * `deviceagent`'s `agent.fake_tags` already makes on the Python side, and this is
 * deliberately its twin: replay a scripted session, get the whole state machine
 * exercised with no hardware.
 *
 * **It is never reachable by accident.** A simulated tap binds a *real* row to a
 * *made-up* UID, which is a mis-binding the verification walk would later catch
 * and a human would then have to unpick. So it exists only behind an explicit
 * `?sim=1`, and the walk renders a standing warning while it is selected. It is a
 * demo and test harness, not a fallback for missing hardware.
 *
 * What is modelled, because each one is a branch the walk has to handle:
 *
 * - a **blank** factory tag (NDEF-formatted, empty), which is what retail stock is
 * - an **already-written** tag, so `overwrite: false` has something to refuse
 * - a tag whose **write silently fails**, leaving the UID intact and user memory
 *   unreadable — the degraded tag that the whole write-verify path exists for
 */

import type { NfcReadBack } from "../scan/nfc";
import { normalizeUid, type TagPresentation, type TagSource } from "./source";

export interface SimulatedTag {
  readonly uid: string;
  /** What user memory holds now. Null is a blank factory tag. */
  url: string | null;
  /**
   * Model a tag whose user memory cannot be written — a damaged sticker, or a
   * phone pulled away mid-write. The UID keeps reading, exactly as the silicon
   * does: it lives in factory-locked pages 0-2, physically separate from page 4.
   */
  readonly writeFails?: boolean;
}

export interface SimulatedTagSource extends TagSource {
  /** Present tag `uid` to the reader, as if it had been tapped. */
  tap(uid: string): void;
  /** The current contents of the simulated tags, for assertions. */
  readonly tags: readonly SimulatedTag[];
}

export class NoTagInFieldError extends Error {
  constructor() {
    super("No tag is in the field. Tap one first.");
    this.name = "NoTagInFieldError";
  }
}

export function simulatedTagSource(initial: readonly SimulatedTag[]): SimulatedTagSource {
  const tags = initial.map((tag) => ({ ...tag, uid: normalizeUid(tag.uid) }));
  const listeners = new Set<(tap: TagPresentation) => void>();
  let inField: SimulatedTag | null = null;

  const present = (tag: SimulatedTag): void => {
    const reading: TagPresentation = {
      uid: tag.uid,
      url: tag.url,
      shortId: null,
      carriesNdef: true,
    };
    for (const listener of listeners) {
      listener(reading);
    }
  };

  return {
    kind: "phone_webnfc",
    label: "Simulated reader (no hardware)",
    canWrite: true,
    tags,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    tap(uid) {
      const wanted = normalizeUid(uid);
      const tag = tags.find((candidate) => candidate.uid === wanted);
      if (tag === undefined) {
        throw new Error(`no simulated tag with uid ${wanted}`);
      }
      inField = tag;
      present(tag);
    },
    write(url, options): Promise<NfcReadBack> {
      const tag = inField;
      if (tag === null) {
        return Promise.reject(new NoTagInFieldError());
      }
      if (tag.url !== null && options?.overwrite !== true) {
        // Chrome's own refusal, in the shape `openNfcScan` translates.
        return Promise.reject(new DOMException("tag is not blank", "NotAllowedError"));
      }
      if (tag.writeFails === true) {
        // The write reports success and user memory is left unreadable. That
        // asymmetry is the point: the failure is invisible until something reads
        // the tag back, which is exactly why the read-back is not optional.
        tag.url = null;
        return Promise.resolve({ observed: true, url: null });
      }
      tag.url = url;
      return Promise.resolve({ observed: true, url });
    },
  };
}
