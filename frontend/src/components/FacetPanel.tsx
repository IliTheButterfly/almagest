/**
 * The parametric filter panel — the DigiKey question, asked of your own shelves.
 *
 * Everything here is driven by `POST /api/parameter-templates`, which returns
 * every filterable attribute *with counts against the filters already applied*.
 * That is the whole point of the panel: the counts tell you where the parts are
 * **before** you click, so narrowing is informed rather than trial and error.
 * They are therefore re-requested whenever the filters change — see
 * `facetsKey` in lib/search/query.ts.
 *
 * **Zero counts are shown, disabled.** In a personal inventory most facet values
 * legitimately have none, and "you own no tantalums" is more useful than a
 * shorter list; hiding them also makes the panel appear to change shape as stock
 * moves. Disabled-and-struck-through says the same thing without a dead end.
 *
 * Values go to the server as **raw text**. The shorthand grammar is parsed there,
 * with the template's physical quantity as context, because `1M` means megohms
 * under resistance and megafarads — which do not exist — under capacitance. This
 * component never interprets a value; it only joins the two ends of a range.
 */

import type { SearchState } from "../lib/search/query";
import type { FacetsResponse, TemplateFacets } from "../lib/api/client";
import {
  decodeChoices,
  decodeRange,
  encodeRange,
  filterValue,
  withChoice,
  withFilter,
} from "../lib/search/query";

const SUBSTITUTION_NOTE: Readonly<Record<string, string>> = {
  higher_ok: "a higher value substitutes for a lower requirement",
  lower_ok: "a lower value substitutes for a higher requirement",
  range_overlap: "any overlapping range qualifies",
  exact: "the candidate must sit entirely inside the requirement",
};

export interface FacetPanelProps {
  readonly facets: FacetsResponse | null;
  readonly state: SearchState;
  readonly onChange: (next: SearchState, immediate?: boolean) => void;
  /** The template a 422 named, so the offending row can say so. */
  readonly invalidTemplate: string | null;
  readonly invalidMessage: string | null;
}

export function FacetPanel({
  facets,
  state,
  onChange,
  invalidTemplate,
  invalidMessage,
}: FacetPanelProps) {
  if (facets === null) {
    return (
      <div className="card">
        <h3>Filters</h3>
        <p className="dim">Loading the filters…</p>
      </div>
    );
  }

  return (
    <div className="card">
      <div className="row">
        <h3 style={{ margin: 0 }}>Filters</h3>
        <span className="spacer" />
        <span className="muted-note">{facets.total} part(s) match</span>
      </div>

      {facets.templates.length === 0 && (
        <p className="muted-note">
          No parameter templates apply here, so there is nothing to filter on yet.
        </p>
      )}

      {facets.templates.map((template, index) => (
        <Facet
          key={template.name}
          template={template}
          state={state}
          onChange={onChange}
          invalid={invalidTemplate === template.name}
          invalidMessage={invalidTemplate === template.name ? invalidMessage : null}
          // The first few open, plus anything already filtered on. A dozen
          // expanded facets is a screen and a half on a phone.
          startOpen={index < 3}
        />
      ))}

      {state.filters.length > 0 && (
        <button type="button" onClick={() => onChange({ ...state, filters: [], page: 1 }, true)}>
          Clear {state.filters.length} filter(s)
        </button>
      )}
    </div>
  );
}

function Facet({
  template,
  state,
  onChange,
  invalid,
  invalidMessage,
  startOpen,
}: {
  template: TemplateFacets;
  state: SearchState;
  onChange: (next: SearchState, immediate?: boolean) => void;
  invalid: boolean;
  invalidMessage: string | null;
  startOpen: boolean;
}) {
  const current = filterValue(state, template.name);
  const active = current !== null;

  return (
    <details
      className="facet"
      data-active={active}
      data-invalid={invalid}
      open={active || invalid || startOpen}
    >
      <summary>
        <span>{template.display_name}</span>
        <span className="facet-meta">
          {template.populated_count} recorded
          {template.base_unit === null ? "" : ` · ${template.base_unit}`}
        </span>
      </summary>

      {invalid && invalidMessage !== null && (
        <p className="muted-note" role="alert" style={{ color: "var(--bad)" }}>
          {invalidMessage}
        </p>
      )}

      {template.value_type === "enum" ? (
        <Choices template={template} state={state} onChange={onChange} />
      ) : template.value_type === "numeric" ? (
        <NumericRangeInput template={template} state={state} onChange={onChange} />
      ) : (
        <RawValueInput template={template} state={state} onChange={onChange} />
      )}

      {state.mode === "substitute" && template.value_type === "numeric" && (
        <p className="muted-note">
          Substituting: {SUBSTITUTION_NOTE[template.substitution_direction] ?? template.substitution_direction}.
        </p>
      )}
    </details>
  );
}

function Choices({
  template,
  state,
  onChange,
}: {
  template: TemplateFacets;
  state: SearchState;
  onChange: (next: SearchState, immediate?: boolean) => void;
}) {
  const chosen = decodeChoices(filterValue(state, template.name));
  if (template.choices === undefined || template.choices.length === 0) {
    return <p className="muted-note">This attribute has no choices defined.</p>;
  }

  return (
    <ul className="choices">
      {template.choices.map((choice) => {
        const checked = chosen.includes(choice.key);
        const empty = choice.count === 0;
        return (
          <li key={choice.key}>
            <label className={empty ? "zero" : undefined}>
              <input
                type="checkbox"
                checked={checked}
                // Zero means "nothing here matches this" — worth reading, not
                // worth clicking. A ticked box stays clickable so it can be
                // un-ticked even once its count has fallen to zero.
                disabled={empty && !checked}
                onChange={(event) =>
                  // Immediate: a tick is a discrete decision, and debouncing it
                  // would make the tap feel dropped.
                  onChange(withChoice(state, template.name, choice.key, event.target.checked), true)
                }
              />
              <span>{choice.label}</span>
              <span className="count">{choice.count}</span>
            </label>
          </li>
        );
      })}
    </ul>
  );
}

function NumericRangeInput({
  template,
  state,
  onChange,
}: {
  template: TemplateFacets;
  state: SearchState;
  onChange: (next: SearchState, immediate?: boolean) => void;
}) {
  const current = filterValue(state, template.name);
  const range = decodeRange(current);
  const bounds = template.numeric_range ?? null;
  const unit = bounds?.unit_symbol ?? template.base_unit ?? "";

  // A value this panel cannot decompose — a bare scalar like `4k7`, or anything
  // exotic the grammar accepts — is shown as itself rather than rewritten into a
  // range that says something subtly different.
  if (current !== null && range === null) {
    return (
      <>
        <RawValueInput template={template} state={state} onChange={onChange} />
        <p className="muted-note">
          An exact value rather than a range. Clear it to filter by a range instead.
        </p>
      </>
    );
  }

  const set = (next: { min?: string; max?: string }): void => {
    const merged = { min: range?.min ?? "", max: range?.max ?? "", ...next };
    onChange(withFilter(state, template.name, encodeRange(merged)));
  };

  // Nothing in the current result set has a value for this, so there is no range
  // to range over. Disabled rather than hidden, like a zero-count choice.
  const nothingToFilter = bounds === null && current === null;

  return (
    <>
      <div className="range-row">
        <input
          className="mono"
          inputMode="text"
          aria-label={`${template.display_name} minimum`}
          placeholder={bounds === null ? "min" : formatBound(bounds.min, unit)}
          value={range?.min ?? ""}
          disabled={nothingToFilter}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => set({ min: event.target.value })}
        />
        <span className="to" aria-hidden="true">
          to
        </span>
        <input
          className="mono"
          inputMode="text"
          aria-label={`${template.display_name} maximum`}
          placeholder={bounds === null ? "max" : formatBound(bounds.max, unit)}
          value={range?.max ?? ""}
          disabled={nothingToFilter}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => set({ max: event.target.value })}
        />
      </div>
      <p className="muted-note">
        {nothingToFilter ? (
          "No part in this set has a value recorded for this."
        ) : (
          <>
            In stock: {formatBound(bounds?.min ?? 0, unit)} to {formatBound(bounds?.max ?? 0, unit)}.
            Shorthand works — <span className="mono">4k7</span>,{" "}
            <span className="mono">100n</span>, <span className="mono">50V</span>. Case matters:{" "}
            <span className="mono">m</span> is milli, <span className="mono">M</span> is mega.
          </>
        )}
      </p>
    </>
  );
}

function RawValueInput({
  template,
  state,
  onChange,
}: {
  template: TemplateFacets;
  state: SearchState;
  onChange: (next: SearchState, immediate?: boolean) => void;
}) {
  return (
    <input
      className="mono"
      aria-label={template.display_name}
      placeholder={template.value_type === "bool" ? "yes / no" : "value"}
      value={filterValue(state, template.name) ?? ""}
      autoComplete="off"
      spellCheck={false}
      onChange={(event) => onChange(withFilter(state, template.name, event.target.value))}
    />
  );
}

/**
 * The bounds come back in the template's **base unit** as a bare number, so a
 * capacitance reads `0.000022`. Engineering notation is the only form that is
 * legible for a quantity spanning twelve orders of magnitude, and it is display
 * only — what gets sent is whatever the user typed.
 */
function formatBound(value: number, unit: string): string {
  if (value === 0) {
    return `0${unit === "" ? "" : ` ${unit}`}`;
  }
  const prefixes: readonly [number, string][] = [
    [1e9, "G"],
    [1e6, "M"],
    [1e3, "k"],
    [1, ""],
    [1e-3, "m"],
    [1e-6, "µ"],
    [1e-9, "n"],
    [1e-12, "p"],
  ];
  const magnitude = Math.abs(value);
  const [scale, prefix] = prefixes.find(([bound]) => magnitude >= bound) ?? [1e-12, "p"];
  const scaled = value / scale;
  const digits = Math.abs(scaled) >= 100 ? 0 : Math.abs(scaled) >= 10 ? 1 : 2;
  return `${Number(scaled.toFixed(digits))} ${prefix}${unit}`.trim();
}
