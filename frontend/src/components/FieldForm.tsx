/**
 * One form, used for authoring a filterable field and for editing one.
 *
 * Shared for the reason `ContainerTypeForm` is shared: the two hard questions on
 * this screen — what *type* of value it holds, and what a *substitute* for it is —
 * have to be asked the same way in both places, or the create form and the edit
 * form teach two different mental models of the same columns.
 *
 * Three things this form does that a generic CRUD form would not:
 *
 * - **The substitution question has no default and cannot be skipped.** It is what
 *   makes substitution search correct by construction: `higher_ok` is why a 50 V
 *   capacitor satisfies a 25 V requirement and a 25 V one never satisfies 50 V.
 *   Defaulted to `exact`, it would be wrong for every rating in the catalogue with
 *   nothing on screen to show it, so the submit button stays disabled until it is
 *   answered.
 * - **A frozen column is shown, disabled, with the reason.** Not hidden: "why can
 *   I not rename this" is the question, and the answer differs by column — a
 *   seeded field's identity is frozen forever, while the type and quantity are
 *   frozen only while parts hold values, which is a state the user can clear.
 * - **A name collision is a decision, offered as one.** `parameter_template.name`
 *   is globally unique on purpose — one real concept is one field, with one
 *   substitution rule — so the 409 comes back carrying the existing field, and
 *   this form offers the two defensible answers rather than an error to retype
 *   past.
 */

import { useState } from "react";

import {
  SUBSTITUTION_COPY,
  VALUE_TYPE_COPY,
  fieldDraftProblems,
  fieldKey,
  type ChoiceDraft,
  type DraftAnchor,
  type FieldDraft,
  type FrozenColumns,
} from "../lib/parts/fieldDraft";
import type {
  BaseUnitOption,
  NameConflictPolicy,
  ParameterFieldRead,
  SubstitutionDirection,
  ValueType,
} from "../lib/api/client";

export interface FieldFormProps {
  readonly initial: FieldDraft;
  readonly mode: "create" | "edit";
  /** `null` on the create path, where nothing is frozen yet. */
  readonly frozen: FrozenColumns | null;
  readonly baseUnits: readonly BaseUnitOption[];
  /**
   * Where this field will hang, in words — "Capacitors", or "every part" for a
   * global one. Stated rather than implied because it decides which categories
   * offer the field, and the answer includes every descendant.
   */
  readonly appliesTo: string;
  /**
   * The field the server refused a duplicate name against, embedded in its 409.
   * Non-null turns the collision notice on.
   */
  readonly conflict: ParameterFieldRead | null;
  /** Whether `namespace` is even available — it needs a category to prefix with. */
  readonly canNamespace: boolean;
  readonly busy: boolean;
  /** Which control the server's refusal belongs against, and what it said. */
  readonly serverAnchor: DraftAnchor | null;
  readonly serverMessage: string | null;
  readonly onSubmit: (draft: FieldDraft) => void;
  readonly onCancel: (() => void) | null;
}

export function FieldForm({
  initial,
  mode,
  frozen,
  baseUnits,
  appliesTo,
  conflict,
  canNamespace,
  busy,
  serverAnchor,
  serverMessage,
  onSubmit,
  onCancel,
}: FieldFormProps) {
  const [draft, setDraft] = useState<FieldDraft>(initial);
  // Stops deriving the filter key the moment it is typed in, so a suggestion
  // never overwrites a deliberate choice. Always "touched" when editing: the key
  // already exists and is what saved searches name.
  const [keyTouched, setKeyTouched] = useState(mode === "edit");

  function set<K extends keyof FieldDraft>(key: K, value: FieldDraft[K]): void {
    setDraft({ ...draft, [key]: value });
  }

  function setDisplayName(value: string): void {
    setDraft({ ...draft, displayName: value, name: keyTouched ? draft.name : fieldKey(value) });
  }

  function setChoice(index: number, patch: Partial<ChoiceDraft>): void {
    setDraft({
      ...draft,
      choices: draft.choices.map((choice, at) => (at === index ? { ...choice, ...patch } : choice)),
    });
  }

  /** Resubmit under a collision policy, which is the only way this changes. */
  function submitWith(policy: NameConflictPolicy): void {
    const next = { ...draft, onNameConflict: policy };
    setDraft(next);
    onSubmit(next);
  }

  const problems = fieldDraftProblems(draft);
  const numeric = draft.valueType === "numeric";
  const anchored = (anchor: DraftAnchor) =>
    serverAnchor === anchor && serverMessage !== null ? (
      <p className="muted-note" role="alert" style={{ margin: 0 }}>
        {serverMessage}
      </p>
    ) : null;

  return (
    <form
      className="stack"
      onSubmit={(event) => {
        event.preventDefault();
        if (problems.length === 0) {
          onSubmit(draft);
        }
      }}
    >
      <fieldset className="fieldgroup">
        <legend>What is being recorded?</legend>
        <p className="muted-note" style={{ margin: 0 }}>
          This field will be offered on <strong>{appliesTo}</strong>.
        </p>
        <label className="field">
          <span>Name, as it appears in the filter panel</span>
          <input
            value={draft.displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            placeholder="Equivalent series resistance"
          />
        </label>
        {anchored("displayName")}
        {frozen?.name === null || frozen === null ? (
          <label className="field">
            <span>
              Filter key — what a search request and a shared search URL name, so it is worth
              keeping short
            </span>
            <input
              className="mono"
              value={draft.name}
              onChange={(event) => {
                setKeyTouched(true);
                set("name", event.target.value);
              }}
              placeholder="esr"
              autoComplete="off"
              spellCheck={false}
            />
          </label>
        ) : (
          <p className="muted-note" style={{ margin: 0 }}>
            Filter key <span className="mono">{draft.name}</span> — frozen. {frozen.name}
          </p>
        )}
        {anchored("name")}
      </fieldset>

      <fieldset className="fieldgroup">
        <legend>What sort of value is it?</legend>
        {frozen?.valueType !== null && frozen !== null ? (
          <p className="muted-note" style={{ margin: 0 }}>
            {VALUE_TYPE_COPY[draft.valueType].label} — frozen. {frozen.valueType}
          </p>
        ) : (
          <>
            <label className="field">
              <span>Type</span>
              <select
                value={draft.valueType}
                onChange={(event) => set("valueType", event.target.value as ValueType)}
              >
                {(Object.keys(VALUE_TYPE_COPY) as ValueType[]).map((value) => (
                  <option key={value} value={value}>
                    {VALUE_TYPE_COPY[value].label}
                  </option>
                ))}
              </select>
            </label>
            <p className="muted-note" style={{ margin: 0 }}>
              {VALUE_TYPE_COPY[draft.valueType].implication}
            </p>
          </>
        )}
        {anchored("valueType")}

        {numeric &&
          (frozen?.baseUnit !== null && frozen !== null ? (
            <p className="muted-note" style={{ margin: 0 }}>
              Measured in <span className="mono">{draft.baseUnit}</span> — frozen.{" "}
              {frozen.baseUnit}
            </p>
          ) : (
            <>
              <label className="field">
                <span>What it measures</span>
                <select
                  value={draft.baseUnit}
                  onChange={(event) => set("baseUnit", event.target.value)}
                >
                  <option value="">Choose a quantity…</option>
                  {baseUnits.map((unit) => (
                    <option key={unit.name} value={unit.name}>
                      {unit.name} ({unit.symbol})
                    </option>
                  ))}
                </select>
              </label>
              <p className="muted-note" style={{ margin: 0 }}>
                Chosen from the list the value parser itself knows, not typed: it is what makes a
                bare <span className="mono">1M</span> read as 1 MΩ under resistance and be
                refused under capacitance.
              </p>
            </>
          ))}
        {anchored("baseUnit")}
      </fieldset>

      {draft.valueType === "enum" && (
        <fieldset className="fieldgroup">
          <legend>The options</legend>
          <p className="muted-note" style={{ margin: 0 }}>
            Filtering by this field is ticking the ones you want. Aliases are alternative
            spellings that resolve to the same option — <span className="mono">0603</span> and{" "}
            <span className="mono">1608</span> are one package under two conventions, and an
            alias means nobody has to know which one a datasheet used.
          </p>
          {draft.choices.map((choice, index) => (
            <div className="fields" key={index}>
              <label className="field">
                <span>Key</span>
                <input
                  className="mono"
                  value={choice.key}
                  onChange={(event) => setChoice(index, { key: event.target.value })}
                  placeholder="c0g"
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>
              <label className="field">
                <span>Label</span>
                <input
                  value={choice.label}
                  onChange={(event) => setChoice(index, { label: event.target.value })}
                  placeholder="C0G / NP0"
                />
              </label>
              <label className="field">
                <span>Aliases, comma separated</span>
                <input
                  value={choice.aliases}
                  onChange={(event) => setChoice(index, { aliases: event.target.value })}
                  placeholder="np0, cog"
                  autoComplete="off"
                />
              </label>
            </div>
          ))}
          <div className="row">
            <button
              type="button"
              onClick={() => set("choices", [...draft.choices, { key: "", label: "", aliases: "" }])}
            >
              Another option
            </button>
          </div>
          <label className="choice">
            <input
              type="checkbox"
              checked={draft.allowMultiple}
              onChange={(event) => set("allowMultiple", event.target.checked)}
            />
            <span>
              <span className="title">A part can have more than one of these at once</span>
              <span className="sub">
                For an attribute that is genuinely plural — a connector that comes both
                through-hole and surface-mount, a module with two interfaces. Filtering does not
                change: ticking two options still matches a part having either. This can be turned
                on later at any time, but turning it back off is refused while some part holds
                several, because which one to keep is not a decision to make for you.
              </span>
            </span>
          </label>
          {anchored("choices")}
        </fieldset>
      )}

      <fieldset className="fieldgroup">
        <legend>What counts as a substitute?</legend>
        <p className="muted-note" style={{ margin: 0 }}>
          This is the whole of what “find me something else that will do” means for this field,
          and it is answered by a rule rather than by a model — the SQL filter uses it directly.
          There is no default, because the wrong one is invisible: a voltage rating treated as
          “must match exactly” quietly stops a 50 V part standing in for a 25 V one.
        </p>
        <ul className="list">
          {(Object.keys(SUBSTITUTION_COPY) as SubstitutionDirection[]).map((value) => (
            <li key={value} className="list-item">
              <label className="choice">
                <input
                  type="radio"
                  name="substitution-direction"
                  value={value}
                  checked={draft.substitutionDirection === value}
                  onChange={() => set("substitutionDirection", value)}
                />
                <span>
                  <span className="title">{SUBSTITUTION_COPY[value].question}</span>
                  <span className="sub">
                    {SUBSTITUTION_COPY[value].example}{" "}
                    <span className="mono dim">{value}</span>
                  </span>
                </span>
              </label>
            </li>
          ))}
        </ul>
        {anchored("substitutionDirection")}
      </fieldset>

      {numeric && (
        <fieldset className="fieldgroup">
          <legend>Sanity window (optional)</legend>
          <p className="muted-note" style={{ margin: 0 }}>
            In the base unit above, and only a plausibility check: a value outside it is refused
            when entered, which is what catches <span className="mono">1M</span> typed against a
            capacitance and meant as 1 µF. Leave both blank if any number is plausible.
          </p>
          <div className="fields">
            <label className="field">
              <span>Lowest plausible value</span>
              <input
                inputMode="decimal"
                value={draft.plausibleMin}
                onChange={(event) => set("plausibleMin", event.target.value)}
                placeholder="1e-12"
              />
            </label>
            <label className="field">
              <span>Highest plausible value</span>
              <input
                inputMode="decimal"
                value={draft.plausibleMax}
                onChange={(event) => set("plausibleMax", event.target.value)}
                placeholder="1"
              />
            </label>
          </div>
          {anchored("plausible")}
        </fieldset>
      )}

      {anchored("category")}

      {conflict !== null && (
        <div className="notice notice-warn">
          <h3>A field called “{conflict.name}” already exists</h3>
          <p style={{ margin: 0 }}>
            It is “{conflict.display_name}”
            {conflict.applies_to_category === null
              ? ", offered on every part"
              : `, on ${conflict.applies_to_category}`}
            , holds {conflict.value_type === "numeric" ? "a number" : `a ${conflict.value_type}`}
            {conflict.base_unit === null ? "" : ` in ${conflict.base_unit}`}, and{" "}
            {conflict.value_count === 0
              ? "no part uses it yet"
              : `${conflict.value_count} part${conflict.value_count === 1 ? "" : "s"} already use it`}
            .
          </p>
          <p style={{ margin: 0 }}>
            One real-world concept should be one field — that is what gives it a single
            substitution rule — so this is usually the field you wanted. Reusing it also adds any
            options above that it does not have yet.
          </p>
          <div className="row">
            <button type="button" className="primary" disabled={busy} onClick={() => submitWith("reuse")}>
              Use the existing field
            </button>
            {canNamespace && (
              <button type="button" disabled={busy} onClick={() => submitWith("namespace")}>
                Keep mine separate
              </button>
            )}
          </div>
          <p className="muted-note" style={{ margin: 0 }}>
            {canNamespace
              ? "“Keep mine separate” files a differently-named field so the two never collide. Right when the collision is an accident of vocabulary rather than the same measurement; wrong when it is the same measurement, because the filter panel would then offer two of it."
              : "A separate field would need a name of its own, and naming one after a category needs a category — this field is global. Rename it above, or reuse the existing one."}
          </p>
        </div>
      )}

      {problems.length > 0 && (
        <div className="notice notice-warn">
          <h3>Not ready to save</h3>
          <ul className="list">
            {problems.map((problem) => (
              <li key={`${problem.anchor}:${problem.message}`} className="sub">
                {problem.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="row">
        {onCancel !== null && (
          <button type="button" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
        )}
        <span className="spacer" />
        <button type="submit" className="primary" disabled={busy || problems.length > 0}>
          {busy ? "Saving…" : mode === "create" ? "Create this field" : "Save changes"}
        </button>
      </div>
    </form>
  );
}
