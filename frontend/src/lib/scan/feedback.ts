/**
 * Decode feedback — flash, vibration, tone, on every decode.
 *
 * The bug this exists for: "I scanned it and nothing happened." `ScanScreen`
 * had zero decode feedback, so a real decode and a dropped one looked
 * identical from the user's side of the glass, and the honest fix
 * (`PLAN.md`'s tag-provisioning recipe) is reused rather than reinvented: a
 * **150 ms flash**, `navigator.vibrate(50)`, a short tone, and a **400 ms
 * debounce** so one label held in front of the camera cannot double-fire.
 *
 * This must fire on a decode that resolves to *nothing* too — that is the
 * case where reassurance matters most, so the caller triggers this before it
 * knows the outcome, not after.
 *
 * Two gesture facts shape the split between this file and the component that
 * uses it:
 *
 * - `AudioContext` can only be created (or resumed) inside the call stack of
 *   a real user gesture. A camera decode fires from a `setTimeout` tick, not
 *   a click, so `init()` must be invoked from an actual click handler —
 *   Start camera, Read an NFC tag, Look up — and never from the decode path
 *   itself, or the browser silently withholds sound forever.
 * - `navigator.vibrate` does not exist on iOS Safari at all. That is a
 *   missing method, not a permission to catch, so it is feature-detected
 *   rather than merely try/caught, and is wrapped in `try` anyway because an
 *   exotic embedded webview could still throw where Safari is simply absent.
 *
 * Every effect degrades independently — a missing or throwing API disables
 * that one channel and must never stop the others, because the failure mode
 * this whole thing guards against is silence, not a half-strength buzz.
 */

import { PayloadHoldOff } from "./holdoff";

/** Matches the tag-provisioning recipe in `PLAN.md`'s cabinet-binding flow. */
export const FEEDBACK_DEBOUNCE_MS = 400;
export const FEEDBACK_FLASH_MS = 150;
const VIBRATE_MS = 50;
const TONE_HZ = 880;
const TONE_S = 0.12;
const TONE_GAIN = 0.15;

/** The slice of `navigator` this needs, injectable so tests do not need a real one. */
export interface VibrateScope {
  readonly vibrate?: (pattern: number) => boolean;
}

/** Constructor shape of `AudioContext`, injectable for the same reason. */
export type AudioContextCtor = new () => AudioContext;

export interface DecodeFeedbackOptions {
  readonly now?: () => number;
  readonly windowMs?: number;
  readonly navigatorScope?: VibrateScope;
  readonly audioContextCtor?: AudioContextCtor | undefined;
}

export class DecodeFeedback {
  readonly #holdOff: PayloadHoldOff;
  readonly #navigatorScope: VibrateScope | undefined;
  readonly #audioContextCtor: AudioContextCtor | undefined;
  #audioCtx: AudioContext | null = null;

  constructor(options: DecodeFeedbackOptions = {}) {
    this.#holdOff = new PayloadHoldOff(options.windowMs ?? FEEDBACK_DEBOUNCE_MS, {
      ...(options.now === undefined ? {} : { now: options.now }),
    });
    this.#navigatorScope = options.navigatorScope;
    // `undefined` (the default) means "look it up live from `globalThis` on
    // every `init()` call" — a test that never sets one gets jsdom's absence
    // of `AudioContext`, which is exactly what a real plain-HTTP phone gets
    // too, just for a different reason.
    this.#audioContextCtor = options.audioContextCtor;
  }

  /**
   * Create — or resume — the audio context. Call this from an actual click
   * handler, never from the decode path: a decode never runs inside a user
   * gesture, and creating an `AudioContext` outside one produces a context
   * that is permanently suspended and never makes a sound.
   */
  init(): void {
    if (this.#audioCtx !== null) {
      // Browsers suspend the context after e.g. a tab switch; resuming is
      // itself gesture-gated, so this is re-issued every time init() runs.
      void this.#audioCtx.resume().catch(() => undefined);
      return;
    }
    const ctor = this.#audioContextCtor ?? this.#globalAudioContextCtor();
    if (ctor === undefined) {
      // No Web Audio here at all (jsdom in tests; some embedded webviews for
      // real) — the flash and vibration below still fire on every fire().
      return;
    }
    try {
      this.#audioCtx = new ctor();
    } catch {
      this.#audioCtx = null;
    }
  }

  /**
   * Fire on every decode, resolving or not. Returns whether this call passed
   * the debounce, so the caller — which owns the flash, since that needs
   * React state — knows whether to show it.
   */
  fire(payload: string): boolean {
    if (!this.#holdOff.admit(payload)) {
      return false;
    }
    this.#vibrate();
    this.#tone();
    return true;
  }

  #vibrate(): void {
    try {
      const scope = this.#navigatorScope ?? this.#globalNavigator();
      if (typeof scope?.vibrate === "function") {
        scope.vibrate(VIBRATE_MS);
      }
    } catch {
      // iOS Safari has no vibrate; an exotic webview could throw instead of
      // being absent. Either way, the flash and tone must still happen.
    }
  }

  #tone(): void {
    const ctx = this.#audioCtx;
    if (ctx === null) {
      return;
    }
    try {
      const oscillator = ctx.createOscillator();
      const gain = ctx.createGain();
      oscillator.frequency.value = TONE_HZ;
      oscillator.connect(gain);
      gain.connect(ctx.destination);
      const start = ctx.currentTime;
      gain.gain.setValueAtTime(TONE_GAIN, start);
      // Exponential decay reads as a "beep" rather than a click; ramping to
      // exactly zero is disallowed by the Web Audio spec, hence the epsilon.
      gain.gain.exponentialRampToValueAtTime(0.0001, start + TONE_S);
      oscillator.start(start);
      oscillator.stop(start + TONE_S + 0.02);
    } catch {
      // Best effort. A codec or autoplay-policy failure here must not stop
      // the vibration or the flash, which is why this runs last.
    }
  }

  #globalNavigator(): VibrateScope | undefined {
    return typeof navigator === "undefined" ? undefined : navigator;
  }

  #globalAudioContextCtor(): AudioContextCtor | undefined {
    const scope = globalThis as unknown as {
      AudioContext?: AudioContextCtor;
      webkitAudioContext?: AudioContextCtor;
    };
    return scope.AudioContext ?? scope.webkitAudioContext;
  }
}
