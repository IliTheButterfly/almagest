/**
 * ASSIGN — PLAN.md workflow 1's `... DIMENSIONS → QUANTITY → ASSIGN → PRINT`.
 *
 * This is the step the bug report was actually about: "I set the part as done
 * and then couldn't find it" is not a search bug, it is the three-tier model —
 * `parts` is a definition, and creating one deliberately puts it nowhere.
 * `POST /api/locations/suggest` proposes a destination and touches nothing;
 * only `POST /api/stock/receive`, called from here, writes the ledger row that
 * makes the part findable by "in stock" and visible on a shelf.
 *
 * Shared by the scan screen (right after a stub part is created, so the very
 * next thing on screen is somewhere to put it, not a dead end) and the part
 * detail screen (a part that already exists with nothing anywhere).
 *
 * `autoSuggest` decides how eager this is: a part with zero lots is not
 * "browse a tree when convenient", it is the next required step, so that case
 * fetches a suggestion on mount. A part that already has stock somewhere and
 * is only picking up a second lot gets the quieter, click-to-suggest form —
 * PLAN.md's escalation ladder never errors, but firing it unasked on every
 * part detail view would make the suggest button its own kind of dead end.
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  receiveStock,
  suggestLocation,
  type MovementResponse,
  type SuggestResponse,
} from "../lib/api/client";
import { formatQty } from "../lib/format";
import { uuid4 } from "../lib/scan/session";
import { ErrorBanner, Notice } from "./Feedback";
import { QuantityPad } from "./Quantity";

export interface AssignStockProps {
  readonly partId: number;
  readonly partName: string;
  readonly autoSuggest?: boolean;
  readonly heading?: string | undefined;
  readonly onAssigned: (response: MovementResponse) => void;
}

export function AssignStock({
  partId,
  partName,
  autoSuggest = false,
  heading,
  onAssigned,
}: AssignStockProps) {
  const [started, setStarted] = useState(autoSuggest);
  const [suggestion, setSuggestion] = useState<SuggestResponse | null>(null);
  const [suggestBusy, setSuggestBusy] = useState(false);
  const [suggestError, setSuggestError] = useState<unknown>(null);
  const [overrideId, setOverrideId] = useState("");
  const [qtyMilli, setQtyMilli] = useState(1000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const ask = useCallback(async () => {
    setStarted(true);
    setSuggestBusy(true);
    setSuggestError(null);
    try {
      // Keyed: one rung of the ladder *materialises* an empty grid cell, so a
      // retried suggestion without a key would leave a spare cell behind every
      // time a flaky phone connection dropped the response.
      setSuggestion(await suggestLocation({ part_id: partId, client_op_id: uuid4() }));
    } catch (cause) {
      setSuggestError(cause);
    } finally {
      setSuggestBusy(false);
    }
  }, [partId]);

  useEffect(() => {
    if (autoSuggest) {
      void ask();
    }
    // `ask` is stable per `partId`, and re-running this because `ask` changed
    // identity would re-fire the suggestion on every unrelated re-render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [partId, autoSuggest]);

  const overrideTrimmed = overrideId.trim();
  const overrideNumeric = overrideTrimmed === "" ? null : Number(overrideTrimmed);
  const overrideValid =
    overrideNumeric === null || (Number.isSafeInteger(overrideNumeric) && overrideNumeric > 0);
  const targetLocationId = overrideNumeric ?? suggestion?.location_id ?? null;
  const targetLabel =
    overrideNumeric !== null ? `location ${overrideNumeric}` : suggestion?.label_path ?? null;

  async function commit(): Promise<void> {
    if (targetLocationId === null || qtyMilli <= 0 || !overrideValid) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const response = await receiveStock({
        part_id: partId,
        location_id: targetLocationId,
        qty_milli: qtyMilli,
        client_op_id: uuid4(),
        source: "manual",
      });
      onAssigned(response);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  if (!started) {
    return (
      <div className="card">
        <div className="row">
          <h3 style={{ margin: 0 }}>{heading ?? "Add stock elsewhere"}</h3>
          <span className="spacer" />
          <button type="button" onClick={() => void ask()}>
            Suggest a location
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="card">
      <h3>{heading ?? `Put ${partName} somewhere`}</h3>

      {suggestBusy && <p className="dim">Working out where it should go…</p>}
      <ErrorBanner error={suggestError} fallback="Could not suggest a location." />

      {suggestion !== null && (
        <div className="stack">
          <Link className="list-item" to={`/locations/${suggestion.location_id}`}>
            <div className="title">{suggestion.label_path}</div>
            <div className="sub">
              {suggestion.reason} · <span className="badge">{suggestion.escalation_level}</span>
            </div>
          </Link>
          {suggestion.candidates.length > 1 && (
            <details>
              <summary>Other candidates</summary>
              <ul className="list">
                {suggestion.candidates.slice(1).map((candidate) => (
                  <li key={candidate.location_id}>
                    <Link className="list-item" to={`/locations/${candidate.location_id}`}>
                      <div className="title">{candidate.label_path}</div>
                      <div className="sub">
                        score {candidate.score.toFixed(3)} · {candidate.free_capacity} free
                      </div>
                    </Link>
                  </li>
                ))}
              </ul>
            </details>
          )}
          {suggestion.defrag_plan !== null && suggestion.defrag_plan !== undefined && (
            <Notice kind="info" title="A defrag would free something up">
              {suggestion.defrag_plan.rationale}
            </Notice>
          )}
        </div>
      )}

      <details>
        <summary>Use a different location instead</summary>
        <label className="field">
          <span>Location id</span>
          <input
            inputMode="numeric"
            value={overrideId}
            onChange={(event) => setOverrideId(event.target.value)}
            placeholder={suggestion === null ? "id" : String(suggestion.location_id)}
          />
        </label>
        <p className="muted-note">
          Browse the <Link to="/tree">storage tree</Link> for its id if you do not know it
          offhand.
        </p>
        {!overrideValid && <p className="muted-note">That is not a location id.</p>}
      </details>

      <QuantityPad valueMilli={qtyMilli} onChange={setQtyMilli} caption="how many are going there" />

      <ErrorBanner error={error} fallback="That could not be recorded." />

      <button
        type="button"
        className="primary wide tall"
        onClick={() => void commit()}
        disabled={busy || targetLocationId === null || qtyMilli <= 0 || !overrideValid}
      >
        {busy
          ? "Assigning…"
          : targetLabel === null
            ? "Put stock here"
            : `Put ${formatQty(qtyMilli)} in ${targetLabel}`}
      </button>
    </div>
  );
}
