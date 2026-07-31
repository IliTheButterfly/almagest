/**
 * The wedge, and the one design decision in it that matters.
 *
 * A USB reader is a keyboard, so it cannot be feature-detected — which is why
 * `NDEFReader` being absent must never disable a scanning affordance. The trap is
 * the obvious way to tell a scanner from a person: typing speed. Antlia on a
 * Flipper types at a configurable 5-60 ms per key, squarely inside human range, so
 * a speed gate would classify the only NFC reader a laptop has as "someone typing"
 * and never fire. Hence the terminator rule, and hence the first test here.
 */

import { describe, expect, it, vi } from "vitest";

import { parseWedgePayload, wedgeTagSource } from "./wedge";
import type { TagPresentation } from "./source";

/** Types `text` then Enter, at whatever pace `perKeyMs` says. */
function type(target: EventTarget, text: string, options: { perKeyMs?: number } = {}): void {
  for (const character of text) {
    target.dispatchEvent(new KeyboardEvent("keydown", { key: character, bubbles: true }));
  }
  target.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  void options;
}

function collect(source: ReturnType<typeof wedgeTagSource>): TagPresentation[] {
  const taps: TagPresentation[] = [];
  source.subscribe((tap) => taps.push(tap));
  return taps;
}

describe("detecting a wedge", () => {
  it("fires on the terminator however slowly the characters arrive", () => {
    const target = new EventTarget();
    // A clock that advances 60 ms per key — Antlia's slowest configured pace, and
    // well inside what a person can type. A speed gate fails this test; that is
    // the entire point of it.
    let clock = 0;
    const source = wedgeTagSource({ target, now: () => (clock += 60) });
    const taps = collect(source);

    type(target, "4K7T92M8");

    expect(taps).toHaveLength(1);
    expect(taps[0]?.shortId).toBe("4K7T92M8");
  });

  it("accepts the full /s/ URL as well as the bare code", () => {
    const target = new EventTarget();
    const taps = collect(wedgeTagSource({ target }));

    type(target, "https://almagest.lan/s/4K7T92M8");

    expect(taps[0]?.shortId).toBe("4K7T92M8");
    // The URL is kept verbatim too: `/api/location-tags/resolve` matches it
    // host-agnostically, which is a stronger answer than the code alone.
    expect(taps[0]?.url).toBe("https://almagest.lan/s/4K7T92M8");
  });

  it("never claims to have read the tag's user memory", () => {
    const target = new EventTarget();
    const taps = collect(wedgeTagSource({ target }));

    type(target, "4K7T92M8");

    // A wedge types what the tag *means*. It has not looked at page 4, so it must
    // not be able to mark a working sticker as needing a rewrite.
    expect(taps[0]?.carriesNdef).toBe(false);
    // Nor can it give a UID, which is why it cannot drive a provisioning walk.
    expect(taps[0]?.uid).toBeNull();
  });

  it("ignores keystrokes aimed at a form field", () => {
    const input = document.createElement("input");
    document.body.append(input);
    const taps = collect(wedgeTagSource({ target: document }));

    input.focus();
    for (const character of "4K7T92M8") {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: character, bubbles: true }));
    }
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));

    // Otherwise a wedge fires mid-sentence in a note box, steals the text and
    // silently resolves a container.
    expect(taps).toHaveLength(0);
    input.remove();
  });

  it("drops a stale partial line rather than prefixing the next read", () => {
    const target = new EventTarget();
    let clock = 0;
    const source = wedgeTagSource({ target, idleTimeoutMs: 1_000, now: () => clock });
    const taps = collect(source);

    target.dispatchEvent(new KeyboardEvent("keydown", { key: "X" }));
    clock += 5_000;
    type(target, "4K7T92M8");

    expect(taps).toHaveLength(1);
    expect(taps[0]?.shortId).toBe("4K7T92M8");
  });

  it("stops listening once every subscriber has gone", () => {
    const target = new EventTarget();
    const remove = vi.spyOn(target, "removeEventListener");
    const source = wedgeTagSource({ target });
    const stop = source.subscribe(() => undefined);

    stop();

    expect(remove).toHaveBeenCalledWith("keydown", expect.any(Function));
  });
});

describe("parsing what a wedge typed", () => {
  it("tolerates the hyphen a printed label carries", () => {
    expect(parseWedgePayload("4K7T-92M8").shortId).toBe("4K7T92M8");
  });

  it("returns nothing for text that is not a code, rather than guessing", () => {
    expect(parseWedgePayload("hello").shortId).toBeNull();
    expect(parseWedgePayload("").shortId).toBeNull();
  });
});
