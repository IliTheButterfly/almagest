/**
 * Building a part out of what a photograph says.
 *
 * A reel label carries an MPN in its Data Matrix and the manufacturer, the
 * description and the date code only in *ink*. `extract.ts` pairs each printed
 * heading with the value under it and ranks what it finds; this form is where
 * that lands.
 *
 * **The fields start filled, and that is not a contradiction of the
 * never-auto-accept rule.** Nothing is written until Create is pressed, every
 * value shows where it came from, and any value with a rival is one tap from
 * being swapped. What the rule forbids is a machine-read part number becoming a
 * record *without a person looking*; a pre-filled form that a person reviews and
 * submits is the person looking. The alternative — an empty form beside a list
 * of values to retype — is the data-entry friction this whole project exists to
 * remove.
 *
 * Two things are deliberately visible rather than smoothed over:
 *
 * - **Where each value came from.** A value decoded from a checksummed symbol
 *   and a value guessed at by OCR are both offered, and they are not the same
 *   kind of thing. The badge says which, and an OCR'd one carries its score.
 * - **That there were alternatives.** When the extractor found more than one
 *   candidate for a field, the others stay on screen. Hiding them would present
 *   a coin-flip as a determination — which is exactly what happens on a label
 *   whose `Part Number` row was misread.
 */

import { useEffect, useState } from "react";

import { createPart } from "../lib/api/client";
import type { FillField } from "../lib/capture/chips";
import type { Suggestion, Suggestions } from "../lib/capture/extract";
import { CategorySelect } from "./CategorySelect";
import { ErrorBanner, Notice } from "./Feedback";
import { PartKindPicker } from "./PartKindPicker";

/** The fields this form offers as fill targets, in the order they are shown. */
export const CAPTURE_PART_FIELDS: readonly { field: FillField; label: string }[] = [
  { field: "name", label: "Name" },
  { field: "mpn", label: "MPN" },
  { field: "manufacturer", label: "Manufacturer" },
];

/**
 * Extracted values that describe a *shipment* rather than a part definition —
 * how many arrived, when they were made, which reel. They belong to a lot, which
 * this form does not create, so they are shown to be copied rather than filed.
 */
const LOT_FIELDS: readonly { field: FillField; label: string }[] = [
  { field: "quantity", label: "Quantity" },
  { field: "date_code", label: "Date code" },
  { field: "lot_code", label: "Lot code" },
];

export type PartDraft = Partial<Record<FillField, string>>;

export interface CaptureToPartProps {
  readonly draft: PartDraft;
  readonly armed: FillField | null;
  readonly suggestions: Suggestions;
  readonly onArm: (field: FillField | null) => void;
  readonly onChange: (field: FillField, value: string) => void;
  readonly onCreated: (part: { id: number; name: string }) => void;
}

function SourceBadge({ suggestion }: { suggestion: Suggestion }) {
  return (
    <span className={`suggest-badge is-${suggestion.source}`}>
      {suggestion.source === "barcode" ? "code" : "read"}
      {suggestion.confidence === undefined ? "" : ` ${suggestion.confidence}%`}
    </span>
  );
}

export function CaptureToPart({
  draft,
  armed,
  suggestions,
  onArm,
  onChange,
  onCreated,
}: CaptureToPartProps) {
  const [partKind, setPartKind] = useState("component");
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [prefilled, setPrefilled] = useState(false);

  // Fill once, when suggestions first arrive — the OCR pass lands seconds after
  // the form is on screen, and a second fill would overwrite whatever the user
  // has typed in the meantime.
  useEffect(() => {
    if (prefilled || Object.keys(suggestions).length === 0) {
      return;
    }
    setPrefilled(true);
    for (const { field } of CAPTURE_PART_FIELDS) {
      const best = suggestions[field]?.[0];
      if (best !== undefined && (draft[field] ?? "") === "") {
        onChange(field, best.value);
      }
    }
    // Deliberately not keyed on `draft`/`onChange`: this runs on the transition
    // from "no suggestions" to "some", and nothing else.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [suggestions, prefilled]);

  const name = (draft.name ?? "").trim();
  const mpn = (draft.mpn ?? "").trim();
  // The MPN is the better name when only it was picked — it is what the user
  // would have typed anyway, and keeps "only a name is required" true without
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

  const lotFound = LOT_FIELDS.filter(({ field }) => (suggestions[field] ?? []).length > 0);

  return (
    <div className="stack">
      <h3 style={{ margin: 0 }}>Make a part from this</h3>
      <p className="muted-note" style={{ margin: 0 }}>
        Filled in from the label. Check each one — a value read off a photograph is a
        suggestion until you submit it. Tap an alternative to swap, or <b>From capture</b>
        {" "}to pick a value off the picture yourself.
      </p>

      {CAPTURE_PART_FIELDS.map(({ field, label }) => {
        const options = suggestions[field] ?? [];
        const current = draft[field] ?? "";
        const chosen = options.find((option) => option.value === current);
        return (
          <label className="field" key={field}>
            <span>
              {label}
              {field === "name" ? " (the only required field)" : ""}
              {chosen !== undefined && <SourceBadge suggestion={chosen} />}
            </span>
            <div className="row">
              <input
                value={current}
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
            {options.length > 1 && (
              <ul className="suggest-list">
                {options.map((option) => (
                  <li key={`${option.source}-${option.via}-${option.value}`}>
                    <button
                      type="button"
                      className={`suggest-option${option.value === current ? " is-current" : ""}`}
                      onClick={() => onChange(field, option.value)}
                    >
                      <span className="mono">{option.value}</span>
                      <SourceBadge suggestion={option} />
                      <span className="muted-note">{option.via}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </label>
        );
      })}

      <PartKindPicker value={partKind} onChange={setPartKind} />
      <CategorySelect
        value={categoryId}
        onChange={setCategoryId}
        hint="Optional, and changeable later — but it is what decides which fields this part can be filtered by."
      />

      {lotFound.length > 0 && (
        <div>
          <h3 style={{ margin: "0 0 4px" }}>Also on the label</h3>
          <p className="muted-note" style={{ margin: "0 0 6px" }}>
            These describe the shipment, not the part — they belong to a lot, which you
            record when you put it somewhere.
          </p>
          <dl className="kv">
            {lotFound.map(({ field, label }) => (
              <div key={field} style={{ display: "contents" }}>
                <dt>{label}</dt>
                <dd className="mono">
                  {suggestions[field]?.[0]?.value}
                  {suggestions[field]?.[0] !== undefined && (
                    <SourceBadge suggestion={suggestions[field]![0]!} />
                  )}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      {effectiveName === "" && (
        <Notice kind="warn" title="Needs a name">
          <p style={{ margin: 0 }}>
            Nothing on the label read as a name. Tap a value into Name or MPN, or type
            one. Everything else can wait.
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
