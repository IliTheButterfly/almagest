/**
 * The light/dark control: three states, one of them "follow the OS".
 *
 * Each option carries a glyph *and* a name in its accessible label, and the live
 * one is marked with `aria-pressed` plus a fill and a weight change — the same
 * rule as everywhere else in this UI, that no state is signalled by colour alone.
 */

import { useTheme, type ThemePreference } from "../lib/theme";

const OPTIONS: readonly { value: ThemePreference; glyph: string; label: string }[] = [
  { value: "system", glyph: "◐", label: "follow the system theme" },
  { value: "light", glyph: "☀", label: "light theme" },
  { value: "dark", glyph: "☾", label: "dark theme" },
];

export function ThemeToggle() {
  const { preference, resolved, set } = useTheme();

  return (
    <div className="theme-toggle" role="group" aria-label="Theme">
      {OPTIONS.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={preference === option.value}
          aria-label={
            option.value === "system" ? `${option.label} (currently ${resolved})` : option.label
          }
          title={option.label}
          onClick={() => set(option.value)}
        >
          <span aria-hidden="true">{option.glyph}</span>
        </button>
      ))}
    </div>
  );
}
