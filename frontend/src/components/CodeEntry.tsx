/**
 * The manual path — a short ID or part number, typed.
 *
 * This is not a fallback bolted on for completeness. ADR 0001 means the camera and
 * NFC are *absent*, not merely unpermitted, on any plain-HTTP origin — including
 * the dev server opened by IP on a phone — and Web NFC is absent on iOS and on the
 * kiosk permanently. A build that only works with a camera would be unusable on
 * half the devices it is meant to run on, so typing a code is a first-class entry
 * point everywhere a scan is offered.
 *
 * The field echoes the canonical short-ID form as it is typed, which catches the
 * common transcription slips before a round trip. The mod-37 check symbol is left
 * to the server — see `lib/shortid.ts`.
 */

import { useState } from "react";

import { formatShortId, looksLikeShortId, normalizeShortId } from "../lib/shortid";

export interface CodeEntryProps {
  readonly onSubmit: (code: string) => void;
  readonly busy?: boolean;
  readonly label?: string;
  readonly placeholder?: string;
  readonly initialValue?: string;
}

export function CodeEntry({
  onSubmit,
  busy,
  label = "Short ID or part number",
  placeholder = "4K7T-92M8, or an MPN",
  initialValue = "",
}: CodeEntryProps) {
  const [text, setText] = useState(initialValue);
  const canonical = looksLikeShortId(text) ? formatShortId(normalizeShortId(text)) : null;

  return (
    <form
      className="stack"
      onSubmit={(event) => {
        event.preventDefault();
        const trimmed = text.trim();
        if (trimmed !== "") {
          onSubmit(trimmed);
        }
      }}
    >
      <label className="field">
        <span>{label}</span>
        <input
          className="mono"
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={placeholder}
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
          enterKeyHint="search"
        />
      </label>
      {canonical !== null && (
        <p className="muted-note">
          Reads as short ID <span className="mono">{canonical}</span>
        </p>
      )}
      <button type="submit" className="primary wide" disabled={busy === true || text.trim() === ""}>
        {busy === true ? "Looking up…" : "Look up"}
      </button>
    </form>
  );
}
