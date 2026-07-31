/**
 * Building a part out of what a photograph says, one deliberate tap per field.
 *
 * This is the other half of the capture feature, and the reason it exists at all:
 * a reel label carries an MPN in its DataMatrix and the manufacturer, the date
 * code and often the real quantity only in *ink*. `BindOrCreate` on the scan
 * screen can prefill a name from a decoded payload and nothing else, so every
 * printed value was previously retyped from a bin the user was still holding.
 *
 * **Arm a field, then tap a value.** The form does not guess. Each field is a
 * target you select first; the next chip you tap in the overlay above lands in
 * it. That interaction is not a stylistic choice — it is the mechanism that lets
 * an OCR'd line fill a part number at all. `chips.ts` refuses to attach a target
 * field to anything a model read, precisely so no heuristic can decide a blurry
 * line "looks like an MPN"; pointing at the field first supplies the human
 * judgement that rule requires. The tap is the acceptance.
 *
 * **A stub, not a finished part.** `is_stub` is set, exactly as the one-tap path
 * does. Only a name is required, so a capture that yielded nothing but a blurry
 * manufacturer still becomes a legal row rather than a form somebody abandons —
 * which is the failure mode this whole project is shaped to avoid.
 */

import { useState } from "react";

import { createPart } from "../lib/api/client";
import type { FillField } from "../lib/capture/chips";
import { CategorySelect } from "./CategorySelect";
import { ErrorBanner, Notice } from "./Feedback";
import { PartKindPicker } from "./PartKindPicker";

/** The fields this form offers as fill targets, in the order they are shown. */
export const CAPTURE_PART_FIELDS: readonly { field: FillField; label: string }[] = [
  { field: "name", label: "Name" },
  { field: "mpn", label: "MPN" },
  { field: "manufacturer", label: "Manufacturer" },
];

export type PartDraft = Partial<Record<FillField, string>>;

export interface CaptureToPartProps {
  readonly draft: PartDraft;
  readonly armed: FillField | null;
  readonly onArm: (field: FillField | null) => void;
  readonly onChange: (field: FillField, value: string) => void;
  readonly onCreated: (part: { id: number; name: string }) => void;
}

export function CaptureToPart({ draft, armed, onArm, onChange, onCreated }: CaptureToPartProps) {
  const [partKind, setPartKind] = useState("component");
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const name = (draft.name ?? "").trim();
  const mpn = (draft.mpn ?? "").trim();
  // The MPN is the better name when only it was picked — it is what the user
  // would have typed anyway, and it keeps "only a name is required" true without
  // making them fill the same value twice.
  const effectiveName = name === "" ? mpn : name;

  async function create(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const result = await createPart({
        name: effectiveName,
        part_kind: partKind.trim() === "" ? "component" : partKind.trim(),
        is_stub: true,
        ...(categoryId === null ? {} : { category_id: categoryId }),
        ...(mpn === "" ? {} : { mpn }),
      });
      onCreated({ id: result.part.id, name: result.part.name });
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <h3 style={{ margin: 0 }}>Make a part from this</h3>
      <p className="muted-note" style={{ margin: 0 }}>
        Pick a field, then tap a value above. Nothing is filled in automatically —
        a value read off a photograph is a suggestion until you say otherwise.
      </p>

      {CAPTURE_PART_FIELDS.map(({ field, label }) => (
        <label className="field" key={field}>
          <span>
            {label}
            {field === "name" ? " (the only required field)" : ""}
          </span>
          <div className="row">
            <input
              value={draft[field] ?? ""}
              onChange={(event) => onChange(field, event.target.value)}
              placeholder={field === "name" ? "what it is" : ""}
            />
            <button
              type="button"
              className={armed === field ? "primary" : ""}
              aria-pressed={armed === field}
              onClick={() => onArm(armed === field ? null : field)}
            >
              {armed === field ? "Tap a value…" : "From capture"}
            </button>
          </div>
        </label>
      ))}

      <PartKindPicker value={partKind} onChange={setPartKind} />
      <CategorySelect
        value={categoryId}
        onChange={setCategoryId}
        hint="Optional, and changeable later — but it is what decides which fields this part can be filtered by."
      />

      {effectiveName === "" && (
        <Notice kind="warn" title="Needs a name">
          <p style={{ margin: 0 }}>
            Tap a value into Name or MPN, or type one. Everything else can wait.
          </p>
        </Notice>
      )}

      <button
        type="button"
        className="primary wide"
        onClick={() => void create()}
        disabled={busy || effectiveName === ""}
      >
        {busy ? "Creating…" : "Create a stub part"}
      </button>
      <ErrorBanner error={error} />
    </div>
  );
}
