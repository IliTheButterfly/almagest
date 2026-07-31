/**
 * The part-kind control on a create-a-part form.
 *
 * It was a free-text box, which is the one shape this column cannot be: a kind is
 * a `part_kinds.slug` and `POST /api/parts` refuses anything else with
 * `unknown_part_kind`, so every typo was a refusal *after* the form was filled in
 * — and nothing on screen said what the accepted answers were. Now the accepted
 * answers are the options.
 *
 * **The escape hatch is a link, not an inline form.** Authoring a kind is a
 * decision about the whole inventory ("a consumable is a different sort of thing
 * from a component"), it is rare, and the form for it belongs beside the
 * categories it is so easily confused with — a kind carries no fields, and a user
 * who wanted somewhere to hang "ESR" has come to the wrong control. Leaving the
 * scan half-finished to make that decision is worse than the link, so the link
 * opens in a new tab and this form keeps what was typed.
 *
 * Falls back to the free-text input when the list cannot be loaded, rather than
 * leaving no way to answer at all: offline, the old behaviour is still better than
 * a disabled select.
 */

import { Link } from "react-router-dom";

import { listPartKinds, type PartKindRead } from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";

export function PartKindPicker({
  value,
  onChange,
}: {
  readonly value: string;
  readonly onChange: (slug: string) => void;
}) {
  const kinds = useAsync<PartKindRead[]>(() => listPartKinds(), []);
  const available = kinds.data ?? [];

  if (kinds.error !== null || (!kinds.loading && available.length === 0)) {
    return (
      <label className="field">
        <span>Part kind</span>
        <input
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="component"
          autoComplete="off"
        />
      </label>
    );
  }

  return (
    <>
      <label className="field">
        <span>Part kind</span>
        <select value={value} onChange={(event) => onChange(event.target.value)}>
          {/* Kept as an option even when the list has loaded: it is what the
              value starts as, and a select whose current value is absent from its
              options silently shows the first one instead. */}
          {available.some((kind) => kind.slug === value) ? null : (
            <option value={value}>{value}</option>
          )}
          {available.map((kind) => (
            <option key={kind.id} value={kind.slug}>
              {kind.display_name}
            </option>
          ))}
        </select>
      </label>
      <p className="muted-note" style={{ margin: 0 }}>
        What it fundamentally is, not what it does — a kind carries no filterable fields.{" "}
        <Link to="/part-types" target="_blank" rel="noreferrer">
          Add a kind, or a category with its own fields →
        </Link>
      </p>
    </>
  );
}
