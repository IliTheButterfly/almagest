/**
 * The one-handed quantity control: a ±1 stepper over a keypad.
 *
 * Both, not either. The stepper is what "took two of these" actually looks like
 * and it works with a thumb while the other hand holds the part; the keypad is for
 * "took 250 off a reel", where tapping + a hundred times is absurd. The presets
 * cover the middle ground.
 *
 * Every target is at least 44 px, and the digits are large because this is read at
 * arm's length over a bench.
 */

import { useState } from "react";

import { formatQty, toMilli } from "../lib/format";

const PRESETS = [5, 10, 25, 100] as const;
const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "00", "⌫"] as const;

export interface QuantityPadProps {
  /** Current value, in thousandths, matching the wire format. */
  readonly valueMilli: number;
  readonly onChange: (valueMilli: number) => void;
  /** Text under the big number, e.g. "of 1 200 on hand". */
  readonly caption?: string;
  readonly disabled?: boolean;
  /**
   * Change this to say "whatever was being typed is finished".
   *
   * The buffer below survives anything the pad itself cannot see, and recording a
   * movement is exactly that: after a take of 4 the digits `4` are still in the
   * buffer, so the next tap of `1` reads as `41`. That is the same surprise the
   * buffer exists to prevent, one step later — and on a take it is wrong in the
   * expensive direction. The owning screen bumps this when it records something,
   * because it is the only party that knows a statement was completed.
   */
  readonly entryKey?: number;
}

export function QuantityPad({
  valueMilli,
  onChange,
  caption,
  disabled,
  entryKey,
}: QuantityPadProps) {
  /**
   * The keypad is an entry buffer, not an increment.
   *
   * Without it, a screen that opens on 1 turns a tap of "2" into 12 — which is
   * both surprising and, on a take, wrong in the expensive direction. So typing
   * starts a fresh number, while the stepper, the presets and Clear all discard
   * the buffer and go back to acting on the value.
   *
   * Entry works in whole units. Sub-unit quantities exist (wire by the metre) but
   * are not what a bench keypad is for, and every value it can produce is exactly
   * representable in thousandths.
   */
  const [entry, setEntry] = useState<string | null>(null);
  // Adjusting state on a changed prop during render rather than in an effect: an
  // effect would let one render draw the stale buffer, and the tap that lands in
  // that render is the one this is here to get right.
  const [seenEntryKey, setSeenEntryKey] = useState(entryKey);
  if (entryKey !== seenEntryKey) {
    setSeenEntryKey(entryKey);
    setEntry(null);
  }
  const whole = Math.max(0, Math.round(valueMilli / 1000));

  function set(nextMilli: number): void {
    setEntry(null);
    onChange(nextMilli);
  }

  function press(key: string): void {
    if (key === "⌫") {
      const trimmed = (entry ?? String(whole)).slice(0, -1);
      setEntry(trimmed);
      onChange(toMilli(trimmed === "" ? 0 : Number(trimmed)));
      return;
    }
    const next = `${entry ?? ""}${key}`.replace(/^0+(?=\d)/, "");
    if (next.length > 7) {
      return;
    }
    setEntry(next);
    onChange(toMilli(Number(next)));
  }

  return (
    <div className="stack">
      <div className="stepper">
        <button
          type="button"
          onClick={() => set(Math.max(0, valueMilli - 1000))}
          disabled={disabled === true || valueMilli <= 0}
          aria-label="one fewer"
        >
          −
        </button>
        <div className="value">
          <div className="big-number" aria-live="polite">
            {formatQty(valueMilli)}
          </div>
          {caption !== undefined && <div className="muted-note">{caption}</div>}
        </div>
        <button
          type="button"
          onClick={() => set(valueMilli + 1000)}
          disabled={disabled}
          aria-label="one more"
        >
          +
        </button>
      </div>

      <div className="row">
        {PRESETS.map((preset) => (
          <button
            key={preset}
            type="button"
            onClick={() => set(toMilli(preset))}
            disabled={disabled}
          >
            {preset}
          </button>
        ))}
        <button type="button" onClick={() => set(0)} disabled={disabled}>
          Clear
        </button>
      </div>

      <div className="keypad">
        {KEYS.map((key) => (
          <button
            key={key}
            type="button"
            onClick={() => press(key)}
            disabled={disabled}
            aria-label={key === "⌫" ? "backspace" : key}
          >
            {key}
          </button>
        ))}
      </div>
    </div>
  );
}
