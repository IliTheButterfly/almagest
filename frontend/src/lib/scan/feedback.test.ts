/**
 * The decode-feedback recipe, in isolation from React.
 *
 * These are the properties `ScanScreen.decode-feedback.test.tsx` cannot pin
 * down through the DOM alone: the exact debounce window, and that a missing
 * `navigator.vibrate` — the iOS Safari case — degrades rather than throwing.
 */

import { describe, expect, it, vi } from "vitest";

import { DecodeFeedback, FEEDBACK_DEBOUNCE_MS } from "./feedback";

function clock(): { now: () => number; advance: (ms: number) => void } {
  let at = 1_000;
  return {
    now: () => at,
    advance: (ms) => {
      at += ms;
    },
  };
}

describe("decode feedback", () => {
  it("admits the first sighting of a payload", () => {
    const time = clock();
    const vibrate = vi.fn(() => true);
    const feedback = new DecodeFeedback({ now: time.now, navigatorScope: { vibrate } });
    expect(feedback.fire("4K7T-92M8")).toBe(true);
    expect(vibrate).toHaveBeenCalledWith(50);
  });

  it("suppresses a second identical decode inside the 400 ms window", () => {
    const time = clock();
    const vibrate = vi.fn(() => true);
    const feedback = new DecodeFeedback({ now: time.now, navigatorScope: { vibrate } });
    expect(feedback.fire("4K7T-92M8")).toBe(true);
    time.advance(FEEDBACK_DEBOUNCE_MS - 1);
    expect(feedback.fire("4K7T-92M8")).toBe(false);
    // The suppressed call must not have re-triggered vibration either — one
    // physical buzz for one admitted decode.
    expect(vibrate).toHaveBeenCalledTimes(1);
  });

  it("admits the same payload again once the window has passed", () => {
    const time = clock();
    const feedback = new DecodeFeedback({ now: time.now, navigatorScope: {} });
    expect(feedback.fire("4K7T-92M8")).toBe(true);
    time.advance(FEEDBACK_DEBOUNCE_MS);
    expect(feedback.fire("4K7T-92M8")).toBe(true);
  });

  it("holds two different payloads off independently", () => {
    const time = clock();
    const feedback = new DecodeFeedback({ now: time.now, navigatorScope: {} });
    expect(feedback.fire("A")).toBe(true);
    expect(feedback.fire("B")).toBe(true);
  });

  it("does not throw when navigator.vibrate is absent — the iOS Safari case", () => {
    const feedback = new DecodeFeedback({ navigatorScope: {} });
    expect(() => feedback.fire("4K7T-92M8")).not.toThrow();
  });

  it("does not throw when navigator.vibrate itself throws", () => {
    const feedback = new DecodeFeedback({
      navigatorScope: {
        vibrate: () => {
          throw new Error("some exotic webview's idea of a joke");
        },
      },
    });
    expect(() => feedback.fire("4K7T-92M8")).not.toThrow();
  });

  it("init() is a no-op, not a throw, when there is no AudioContext at all", () => {
    // jsdom's globalThis has neither AudioContext nor webkitAudioContext, so
    // this exercises exactly the path a plain-HTTP phone takes.
    const feedback = new DecodeFeedback();
    expect(() => feedback.init()).not.toThrow();
    expect(() => feedback.fire("4K7T-92M8")).not.toThrow();
  });

  it("degrades the tone alone when the audio context throws on construction", () => {
    const vibrate = vi.fn(() => true);
    class ThrowingAudioContext {
      constructor() {
        throw new Error("autoplay policy said no");
      }
    }
    const feedback = new DecodeFeedback({
      navigatorScope: { vibrate },
      audioContextCtor: ThrowingAudioContext as unknown as new () => AudioContext,
    });
    expect(() => feedback.init()).not.toThrow();
    expect(feedback.fire("4K7T-92M8")).toBe(true);
    // The tone failed silently, but vibration still happened — one channel
    // degrading must never take the others down with it.
    expect(vibrate).toHaveBeenCalledTimes(1);
  });

  it("plays a short tone through an injected AudioContext without throwing", () => {
    const gain = {
      gain: { setValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() },
      connect: vi.fn(),
    };
    const oscillator = { frequency: { value: 0 }, connect: vi.fn(), start: vi.fn(), stop: vi.fn() };
    class FakeAudioContext {
      currentTime = 0;
      destination = {};
      createOscillator(): typeof oscillator {
        return oscillator;
      }
      createGain(): typeof gain {
        return gain;
      }
      resume(): Promise<void> {
        return Promise.resolve();
      }
    }
    const feedback = new DecodeFeedback({
      audioContextCtor: FakeAudioContext as unknown as new () => AudioContext,
    });
    feedback.init();
    expect(feedback.fire("4K7T-92M8")).toBe(true);
    expect(oscillator.start).toHaveBeenCalledTimes(1);
    expect(oscillator.stop).toHaveBeenCalledTimes(1);
  });
});
