/**
 * Part detail — identity, where every one of them is, and the curation tail.
 *
 * The quantity shown at the top is a **sum over the part's lots**, and the lots are
 * where quantity actually lives. That is not a presentational nicety: hanging the
 * count on the part is precisely what stopped PartKeepr from ever supporting two
 * locations or a per-batch cost, so the screen shows the lots as the primary fact
 * and the total as derived from them.
 *
 * The edit form is the review-queue tail of intake: a scan that resolved to nothing
 * becomes a legal `is_stub` part in one tap, and this is where it gets a real name
 * later, at a desk, with a keyboard.
 */

import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import {
  getPart,
  suggestLocation,
  updatePart,
  type PartRead,
  type PartUpdate,
  type SuggestResponse,
} from "../lib/api/client";
import { formatMoneyMicro, formatQty } from "../lib/format";
import { useAsync } from "../lib/hooks/useAsync";
import { formatShortId } from "../lib/shortid";
import { uuid4 } from "../lib/scan/session";

export function PartScreen() {
  const { partId: raw } = useParams();
  const partId = Number(raw);
  const valid = Number.isSafeInteger(partId) && partId > 0;

  const part = useAsync<PartRead | null>(
    () => (valid ? getPart(partId) : Promise.resolve(null)),
    [partId, valid],
  );

  if (!valid) {
    return <Notice kind="error" title="That is not a part id" />;
  }
  if (part.error !== null) {
    return <ErrorBanner error={part.error} fallback="That part could not be loaded." />;
  }
  if (part.data === null) {
    return <Loading what="the part" />;
  }
  return <PartDetail part={part.data} onSaved={part.reload} />;
}

function PartDetail({ part, onSaved }: { part: PartRead; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <h1 style={{ flex: 1 }}>{part.name}</h1>
          {part.is_stub && <span className="badge badge-warn">stub</span>}
          {!part.is_active && <span className="badge">inactive</span>}
        </div>
        {part.mpn !== null && <p className="mono" style={{ margin: 0 }}>{part.mpn}</p>}
        {part.description !== null && <p style={{ margin: 0 }}>{part.description}</p>}
        <div className="row">
          <div>
            <div className="big-number">{formatQty(part.total_qty_milli)}</div>
            <div className="muted-note">on hand across {part.lots.length} lot(s)</div>
          </div>
          <span className="spacer" />
          {part.short_id !== null && (
            <span className="badge mono">{formatShortId(part.short_id)}</span>
          )}
        </div>
        <div className="row">
          <button type="button" onClick={() => setEditing(!editing)}>
            {editing ? "Cancel" : "Edit"}
          </button>
        </div>
      </div>

      {part.is_stub && !editing && (
        <Notice kind="warn" title="This part came from a scan that resolved to nothing">
          Only a name was required to record it, which is what kept the scan to one
          tap. Give it a real name and part number when convenient — nothing is
          blocked in the meantime.
        </Notice>
      )}

      {editing && (
        <EditPart
          part={part}
          onDone={() => {
            setEditing(false);
            onSaved();
          }}
        />
      )}

      <div className="card">
        <h3>Stock lots</h3>
        {part.lots.length === 0 ? (
          <p className="dim">None. Nothing of this part is anywhere yet.</p>
        ) : (
          <ul className="list">
            {part.lots.map((lot) => (
              <li key={lot.id}>
                <Link className="list-item" to={`/lots/${lot.id}`}>
                  <div className="row">
                    <span className="title">{formatQty(lot.qty_milli)}</span>
                    <span className="spacer" />
                    {lot.status !== "active" && <span className="badge">{lot.status}</span>}
                    {lot.qty_reserved_milli > 0 && (
                      <span className="badge badge-warn">
                        {formatQty(lot.qty_reserved_milli)} reserved
                      </span>
                    )}
                  </div>
                  <div className="sub">{lot.location_label_path ?? `location ${lot.location_id}`}</div>
                  <div className="sub">
                    {[
                      lot.batch_code === null || lot.batch_code === undefined
                        ? null
                        : `batch ${lot.batch_code}`,
                      lot.date_code === null || lot.date_code === undefined
                        ? null
                        : `date ${lot.date_code}`,
                      formatMoneyMicro(lot.unit_cost_micro ?? null, lot.currency ?? null),
                    ]
                      .filter((piece): piece is string => piece !== null)
                      .join(" · ")}
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>

      <WhereToPut part={part} />

      <div className="card">
        <h3>Details</h3>
        <dl className="kv">
          <dt>Kind</dt>
          <dd>{part.part_kind}</dd>
          <dt>Hot score</dt>
          <dd>{part.hot_score.toFixed(3)}</dd>
          <dt>Unit mass</dt>
          <dd>
            {part.unit_mass_mg === null ? (
              <span className="dim">
                not calibrated — counting by weight is refused for this part rather
                than attempted badly
              </span>
            ) : (
              `${part.unit_mass_mg} mg`
            )}
          </dd>
          <dt>Dimensions</dt>
          <dd>
            {[part.length_mm, part.width_mm, part.height_mm].every((value) => value === null)
              ? "—"
              : `${part.length_mm ?? "?"} × ${part.width_mm ?? "?"} × ${part.height_mm ?? "?"} mm`}
          </dd>
          {part.keywords !== null && (
            <>
              <dt>Keywords</dt>
              <dd>{part.keywords}</dd>
            </>
          )}
          {part.notes !== null && (
            <>
              <dt>Notes</dt>
              <dd>{part.notes}</dd>
            </>
          )}
        </dl>
      </div>
    </div>
  );
}

function EditPart({ part, onDone }: { part: PartRead; onDone: () => void }) {
  const [draft, setDraft] = useState<PartUpdate>({
    name: part.name,
    mpn: part.mpn,
    description: part.description,
    keywords: part.keywords,
    notes: part.notes,
    is_stub: part.is_stub,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function save(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await updatePart(part.id, draft);
      onDone();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="card"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <h3>Edit</h3>
      <label className="field">
        <span>Name (required)</span>
        <input
          value={draft.name ?? ""}
          onChange={(event) => setDraft({ ...draft, name: event.target.value })}
        />
      </label>
      <label className="field">
        <span>Manufacturer part number</span>
        <input
          className="mono"
          value={draft.mpn ?? ""}
          onChange={(event) => setDraft({ ...draft, mpn: event.target.value })}
        />
      </label>
      <label className="field">
        <span>Description</span>
        <input
          value={draft.description ?? ""}
          onChange={(event) => setDraft({ ...draft, description: event.target.value })}
        />
      </label>
      <label className="field">
        <span>Keywords</span>
        <input
          value={draft.keywords ?? ""}
          onChange={(event) => setDraft({ ...draft, keywords: event.target.value })}
        />
      </label>
      <label className="field">
        <span>Notes</span>
        <textarea
          rows={3}
          value={draft.notes ?? ""}
          onChange={(event) => setDraft({ ...draft, notes: event.target.value })}
        />
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={draft.is_stub === true}
          onChange={(event) => setDraft({ ...draft, is_stub: event.target.checked })}
        />
        Still needs review
      </label>
      <ErrorBanner error={error} fallback="Those changes were not saved." />
      <button type="submit" className="primary wide" disabled={busy}>
        {busy ? "Saving…" : "Save"}
      </button>
    </form>
  );
}

/**
 * Where a new lot of this part should go.
 *
 * `POST /api/locations/suggest` never errors — the escalation ladder always ends
 * somewhere concrete, down to the permanent `INBOX` staging row — so this reports
 * which rung answered rather than presenting every answer as equally confident. It
 * also does not touch the ledger: suggesting a destination and putting stock in it
 * are separate steps.
 */
function WhereToPut({ part }: { part: PartRead }) {
  const [suggestion, setSuggestion] = useState<SuggestResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function ask(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      // Keyed, because one rung of the ladder materialises an empty grid cell and a
      // retried suggestion would otherwise leave a spare cell behind each time.
      setSuggestion(await suggestLocation({ part_id: part.id, client_op_id: uuid4() }));
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <div className="row">
        <h3 style={{ margin: 0 }}>Where should this go?</h3>
        <span className="spacer" />
        <button type="button" onClick={() => void ask()} disabled={busy}>
          {busy ? "Thinking…" : "Suggest"}
        </button>
      </div>
      <ErrorBanner error={error} />
      {suggestion !== null && (
        <div className="stack">
          <Link className="list-item" to={`/locations/${suggestion.location_id}`}>
            <div className="title">{suggestion.label_path}</div>
            <div className="sub">{suggestion.reason}</div>
            <div className="sub">
              <span className="badge">{suggestion.escalation_level}</span>
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
    </div>
  );
}
