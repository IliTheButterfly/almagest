/**
 * The USB keyboard wedge — a reader that cannot be feature-detected, because it
 * is a keyboard.
 *
 * **This is why `NDEFReader` must never gate a scanning affordance.** Web NFC
 * being absent says one thing only: *this browser has no Web NFC*. It says
 * nothing about whether a tag can be read here. A Flipper Zero running Antlia, a
 * $25 USB barcode scanner and an ACR122U all deliver perfectly good reads to a
 * desktop Chromium that will never have `NDEFReader` — and on Android the
 * converse lies too, since `NDEFReader` exists while the radio is switched off.
 * A capability probe is a fact about an API, not about the desk.
 *
 * **Detected by its terminator, never by typing speed.** A >50 chars/sec
 * heuristic is the obvious design and it is wrong: Antlia types at a
 * deliberately configurable 5-60 ms per key, squarely inside human range, so a
 * speed gate classifies the one NFC reader a laptop has as "a person typing" and
 * never fires. So the rule is a CR/LF-terminated line, and inter-key timing is at
 * most a hint for an affordance — never the gate.
 *
 * **Two payload forms, both accepted**, because different carriers naturally emit
 * different ones: a bare `4K7T-92M8` and a full `https://host/s/4K7T92M8`. Both
 * already resolve at step 1 of the resolver chain, so accepting both costs
 * nothing.
 *
 * What a wedge **cannot** do is give a tag UID: it types what the tag *means*,
 * not what the tag *is*. So it confirms a container all day and cannot bind one —
 * see `TagPresentation`, which keeps the three carriers apart precisely so a
 * screen can say that rather than fail obscurely.
 */

import { normalizeShortId, looksLikeShortId } from "../shortid";
import type { TagPresentation, TagSource } from "./source";

/** Keys that end a wedge read. A scanner sends one; a person pressing Enter also does. */
const TERMINATORS = new Set(["Enter", "Tab"]);

/**
 * Give up on a partial line after this long.
 *
 * Not a speed gate — it is a *staleness* gate, an order of magnitude slower than
 * even the slowest configured Antlia. Without it, three stray keystrokes sit in
 * the buffer forever and prefix the next real read.
 */
export const WEDGE_IDLE_TIMEOUT_MS = 10_000;

export interface WedgeOptions {
  /** Defaults to `document`. Injected by tests. */
  readonly target?: EventTarget;
  readonly idleTimeoutMs?: number;
  readonly now?: () => number;
}

interface KeyLike {
  readonly key: string;
  readonly ctrlKey?: boolean;
  readonly metaKey?: boolean;
  readonly altKey?: boolean;
  readonly target?: EventTarget | null;
  preventDefault?: () => void;
}

/**
 * Should this keystroke be swallowed as part of a scan?
 *
 * Not while the user is typing into a field: a wedge firing mid-sentence in a
 * note box would steal the text and silently resolve a container. A wedge read is
 * fast and self-contained, so requiring the page background to have focus costs a
 * real scanner nothing and protects every form on the page.
 */
function isFormField(target: EventTarget | null | undefined): boolean {
  if (target === null || target === undefined || !("tagName" in target)) {
    return false;
  }
  const element = target as { tagName?: string; isContentEditable?: boolean };
  const tag = (element.tagName ?? "").toUpperCase();
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    element.isContentEditable === true
  );
}

/** The short id carried by `text`, whether bare or inside a `/s/{id}` URL. */
export function parseWedgePayload(text: string): { shortId: string | null; url: string | null } {
  const trimmed = text.trim();
  if (trimmed === "") {
    return { shortId: null, url: null };
  }
  const inUrl = /\/s\/([0-9A-Za-z-]+)\/?$/.exec(trimmed);
  if (inUrl !== null) {
    const code = normalizeShortId(inUrl[1] ?? "");
    // The whole URL is kept as well: it is the NDEF payload verbatim, and
    // `/api/location-tags/resolve` matches it host-agnostically, which is a
    // stronger answer than the code alone when both are available.
    return { shortId: looksLikeShortId(code) ? code : null, url: trimmed };
  }
  const code = normalizeShortId(trimmed);
  return { shortId: looksLikeShortId(code) ? code : null, url: null };
}

/**
 * A reader that is always present, because there is no way to ask whether it is.
 *
 * Subscribing installs a document-level `keydown` listener; unsubscribing removes
 * it. Nothing is claimed, no permission is prompted, and no device is opened —
 * which is also why this can never conflict with the Flipper's own USB state
 * (claiming HID on a Flipper kills its serial link until someone presses a
 * button; a wedge is only ever *listened to*).
 */
export function wedgeTagSource(options: WedgeOptions = {}): TagSource {
  const target = options.target ?? document;
  const idleTimeoutMs = options.idleTimeoutMs ?? WEDGE_IDLE_TIMEOUT_MS;
  const now = options.now ?? (() => Date.now());
  const listeners = new Set<(tap: TagPresentation) => void>();

  let buffer = "";
  let lastKeyAt = 0;

  const onKey = (event: Event): void => {
    const key = event as unknown as KeyLike;
    if (key.ctrlKey === true || key.metaKey === true || key.altKey === true) {
      return;
    }
    if (isFormField(key.target)) {
      return;
    }
    const at = now();
    if (buffer !== "" && at - lastKeyAt > idleTimeoutMs) {
      buffer = "";
    }
    lastKeyAt = at;

    if (TERMINATORS.has(key.key)) {
      const line = buffer;
      buffer = "";
      const { shortId, url } = parseWedgePayload(line);
      if (shortId === null && url === null) {
        return;
      }
      key.preventDefault?.();
      const tap: TagPresentation = { uid: null, url, shortId, carriesNdef: false };
      for (const listener of listeners) {
        listener(tap);
      }
      return;
    }
    if (key.key.length === 1) {
      buffer += key.key;
    }
  };

  return {
    kind: "manual",
    label: "USB reader or barcode wedge",
    canWrite: false,
    subscribe(listener) {
      listeners.add(listener);
      if (listeners.size === 1) {
        target.addEventListener("keydown", onKey);
      }
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) {
          target.removeEventListener("keydown", onKey);
          buffer = "";
        }
      };
    },
  };
}
