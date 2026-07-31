/**
 * The fields a part can have values for, and the editor for them.
 *
 * This is the piece that made the rest reachable. A category could be authored,
 * fields hung off it, a unit invented and a list field told to hold several
 * options — and there was no way to fill any of it in by hand. Values only ever
 * arrived because a *source* proposed one and somebody accepted it in the review
 * queue, so for a part no decoder recognised the whole apparatus was decorative.
 *
 * One control per type, and each one is the shape that type actually is:
 *
 * - **a number** is a text box, because the value is *shorthand* — `22uF`, `4k7`,
 *   `20-30uF`, and the server parses it with the same grammar search uses. A
 *   numeric input with a separate unit dropdown would throw away `4k7` and the
 *   ranges, which is most of why the grammar exists.
 * - **a list** is a select, or **checkboxes when the field allows several** —
 *   which is the only place in the app where that distinction is visible, and the
 *   reason a multi-valued field is worth declaring.
 * - **yes/no** is a checkbox with three states in practice: yes, no, and *no value
 *   recorded*, which is not the same as no. Clearing is how you say the third.
 *
 * Every row saves on its own. Refusals land against the field that caused them,
 * with the server's own words — `1M` under capacitance is the most useful error
 * this system produces, and burying it in a banner about six fields would waste it.
 */

import { useState } from "react";
import { Link } from "react-router-dom";

import { Empty, ErrorBanner, Loading, Notice } from "./Feedback";
import {
  clearPartParameter,
  listPartParameters,
  setPartParameter,
  type PartParameterRead,
  type PartParametersResponse,
} from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";

export function PartFields({ partId }: { readonly partId: number }) {
  const state = useAsync<PartParametersResponse>(() => listPartParameters(partId), [partId]);
  const data = state.data;
  const own = (data?.parameters ?? []).filter((field) => !field.inherited);
  const inherited = (data?.parameters ?? []).filter((field) => field.inherited);

  return (
    <div className="card">
      <h3>Fields</h3>
      {state.loading && <Loading what="fields" />}
      <ErrorBanner error={state.error} fallback="This part's fields could not be loaded." />

      {data !== undefined && data !== null && !data.filed && (
        <Notice kind="info" title="This part is not filed under a category">
          <p style={{ margin: 0 }}>
            So the only fields it has are the ones every part has. A field reaches a part
            through the category it sits in — file it above, and this list becomes whatever that
            category offers.
          </p>
        </Notice>
      )}

      {data !== undefined && data !== null && data.parameters.length === 0 && (
        <Empty>
          Nothing to record yet. Fields are authored on categories, in{" "}
          <Link to="/part-types">Part types</Link>.
        </Empty>
      )}

      {own.length > 0 && (
        <ul className="list">
          {own.map((field) => (
            <FieldRow
              key={field.name}
              partId={partId}
              field={field}
              onSaved={() => state.reload()}
            />
          ))}
        </ul>
      )}

      {inherited.length > 0 && (
        <details>
          <summary>
            {inherited.length} field{inherited.length === 1 ? "" : "s"} every part of this sort has
          </summary>
          <ul className="list">
            {inherited.map((field) => (
              <FieldRow
                key={field.name}
                partId={partId}
                field={field}
                onSaved={() => state.reload()}
              />
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function FieldRow({
  partId,
  field,
  onSaved,
}: {
  readonly partId: number;
  readonly field: PartParameterRead;
  readonly onSaved: () => void;
}) {
  const [text, setText] = useState(field.raw_input ?? "");
  const [picked, setPicked] = useState<readonly string[]>(
    (field.choices ?? []).map((choice) => choice.key),
  );
  const [checked, setChecked] = useState(field.value_bool === true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);

  const hasValue = field.raw_input !== null && field.raw_input !== undefined;

  async function save(): Promise<void> {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      if (field.value_type === "enum") {
        await setPartParameter(partId, field.name, { choices: [...picked] });
      } else if (field.value_type === "bool") {
        await setPartParameter(partId, field.name, { checked });
      } else {
        await setPartParameter(partId, field.name, { value: text });
      }
      setSaved(true);
      onSaved();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  async function clear(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await clearPartParameter(partId, field.name);
      setText("");
      setPicked([]);
      setChecked(false);
      setSaved(false);
      onSaved();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  function toggle(key: string): void {
    setPicked((current) =>
      current.includes(key) ? current.filter((held) => held !== key) : [...current, key],
    );
  }

  return (
    <li className="list-item">
      <div className="row">
        <span className="title">{field.display_name}</span>
        {field.base_unit !== null && <span className="mono dim">{field.base_unit}</span>}
        <span className="spacer" />
        {hasValue ? (
          <span className="badge badge-good">{field.display ?? field.raw_input}</span>
        ) : (
          <span className="muted-note">not recorded</span>
        )}
      </div>

      {field.value_type === "numeric" && (
        <label className="field">
          {/* The examples are unit-free on purpose. Naming µF here printed "22uF,
              4k7, 20-30uF" under a field measured in bytes — the same mistake the
              `implausible` hint used to make, and units being authorable means any
              hard-coded example is wrong for somebody's field. What is worth saying
              is the grammar (a prefix, an infix, a range) and which quantity the
              number is read as. */}
          <span>
            Read as {field.base_unit}. Shorthand works — a prefix (
            <span className="mono">22u</span>), an infix (<span className="mono">4k7</span>) or a
            range (<span className="mono">20-30</span>) — and the unit may be written or left
            off
          </span>
          <input
            value={text}
            onChange={(event) => setText(event.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </label>
      )}

      {field.value_type === "text" && (
        <label className="field">
          <span>Text — matched as a substring, never as a range</span>
          <input value={text} onChange={(event) => setText(event.target.value)} />
        </label>
      )}

      {field.value_type === "bool" && (
        <label className="choice">
          <input
            type="checkbox"
            checked={checked}
            onChange={(event) => setChecked(event.target.checked)}
          />
          <span>
            <span className="title">Yes</span>
            <span className="sub">
              Unticked saves a definite “no”. To say nothing is recorded at all, clear it.
            </span>
          </span>
        </label>
      )}

      {field.value_type === "enum" &&
        (field.allow_multiple ? (
          <>
            <p className="muted-note" style={{ margin: 0 }}>
              This one can hold several at once — tick every one that applies.
            </p>
            {(field.options ?? []).map((option) => (
              <label className="choice" key={option.id}>
                <input
                  type="checkbox"
                  checked={picked.includes(option.key)}
                  onChange={() => toggle(option.key)}
                />
                <span>
                  <span className="title">{option.label}</span>
                  <span className="sub mono">{option.key}</span>
                </span>
              </label>
            ))}
          </>
        ) : (
          <label className="field">
            <span>One of</span>
            <select
              value={picked[0] ?? ""}
              onChange={(event) =>
                setPicked(event.target.value === "" ? [] : [event.target.value])
              }
            >
              <option value="">Choose…</option>
              {(field.options ?? []).map((option) => (
                <option key={option.id} value={option.key}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        ))}

      {/* The server's own message, against the field that caused it. */}
      <ErrorBanner error={error} fallback="That value was not saved." />
      {saved && error === null && (
        <p className="muted-note" role="status" style={{ margin: 0 }}>
          Saved.
        </p>
      )}

      <div className="row">
        {hasValue && (
          <button type="button" onClick={() => void clear()} disabled={busy}>
            Clear
          </button>
        )}
        <span className="spacer" />
        <button
          type="button"
          className="primary"
          disabled={
            busy ||
            (field.value_type === "enum" ? picked.length === 0 : false) ||
            (field.value_type !== "enum" && field.value_type !== "bool" && text.trim() === "")
          }
          onClick={() => void save()}
        >
          {busy ? "Saving…" : hasValue ? "Update" : "Record it"}
        </button>
      </div>
    </li>
  );
}
