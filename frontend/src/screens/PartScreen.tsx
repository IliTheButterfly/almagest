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

import { AssignStock } from "../components/AssignStock";
import { CategorySelect } from "../components/CategorySelect";
import { PartFields } from "../components/PartFields";
import { DocumentsPanel } from "../components/DocumentsPanel";
import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import { WhereIsIt } from "../components/WhereIsIt";
import {
  getPart,
  listPartCategories,
  updatePart,
  type CategoryNode,
  type PartRead,
  type PartUpdate,
} from "../lib/api/client";
import { formatMoneyMicro, formatQty } from "../lib/format";
import { useAsync } from "../lib/hooks/useAsync";
import { formatShortId } from "../lib/shortid";

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

      <DocumentsPanel partId={part.id} />

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
          // This is the fix for "I set the part as done and then couldn't find
          // it" — a part is a definition, not a count. An empty table here
          // says nothing wrong happened, but it needs to say *why* it is empty
          // in the same breath, or the natural reading is that the part is
          // lost rather than merely unassigned.
          <p className="dim">
            None yet — a part is a definition, not a quantity. Stock lives in
            lots at locations, and this part has not been put anywhere.
          </p>
        ) : (
          <ul className="list">
            {part.lots.map((lot) => (
              <LotRow key={lot.id} lot={lot} soleLot={part.lots.length === 1} />
            ))}
          </ul>
        )}
      </div>

      {/* Zero lots: the very next thing on screen is somewhere to put it, not
       * a dead end, so the suggestion fires immediately rather than waiting
       * on a click. Once there is at least one lot, picking up a *second* one
       * elsewhere is a lower-urgency action and gets the quieter click-first
       * form — see AssignStock's `autoSuggest`. */}
      <AssignStock
        partId={part.id}
        partName={part.name}
        autoSuggest={part.lots.length === 0}
        heading={part.lots.length === 0 ? "This part has no stock yet — put some somewhere" : undefined}
        onAssigned={onSaved}
      />

      {/* Beside the stock rather than buried under Details: what a part *is* is
          its values, and this is the only place they can be entered. */}
      <PartFields partId={part.id} />

      <div className="card">
        <h3>Details</h3>
        <dl className="kv">
          <dt>Kind</dt>
          <dd>{part.part_kind}</dd>
          {/* Filed *where*, which is the question the fields on this part depend
              on: a category's fields reach a part by the part being in it. */}
          <dt>Category</dt>
          <dd>
            {part.category_id === null ? (
              <span className="dim">
                not filed under anything — so no category's fields apply to it, and it will not
                appear when browsing by type
              </span>
            ) : (
              <CategoryName categoryId={part.category_id} />
            )}
          </dd>
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

/**
 * One lot, and the way to the drawer it is in.
 *
 * The location used to be one grey line of `label_path` — a correct answer to a
 * question nobody standing in the workshop was asking. `WhereIsIt` draws the
 * walk instead, and it is *behind a press* rather than always open for two
 * reasons, both of them about the common case:
 *
 * - Each open panel fetches the location tree. A part in eleven bins would
 *   otherwise mean eleven fetches of the whole hierarchy on a page nobody had
 *   asked a locational question of yet.
 * - A page of five drawer maps is a page you have to scroll past to reach the
 *   thing you came for. The path string stays visible either way, so the press
 *   only ever *adds*.
 *
 * **Except when there is only one lot**, where it opens by itself: one lot means
 * "where is this part" and "where is this lot" are the same question, the page
 * has nothing else to be about, and making somebody press for the only answer on
 * the screen is a click that exists to protect a fetch that was going to happen.
 */
function LotRow({ lot, soleLot }: { lot: PartRead["lots"][number]; soleLot: boolean }) {
  const [showing, setShowing] = useState(soleLot);
  const path = lot.location_label_path ?? `location ${lot.location_id}`;

  return (
    <li>
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
        <div className="sub">{path}</div>
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

      {/* Outside the link, because a control inside a link is a control nobody
          can reach with a keyboard and a link that sometimes does not navigate.
          Indented under the row it belongs to: a part in five bins is five of
          these, and a flat stack of buttons does not say which lot each answers
          for. */}
      <div className="lot-where">
        <button
          type="button"
          className="quiet-toggle"
          aria-expanded={showing}
          onClick={() => setShowing(!showing)}
        >
          {showing ? "Hide the way there" : "Where is it?"}
        </button>
        {showing && <WhereIsIt locationId={lot.location_id} labelPath={path} />}
      </div>
    </li>
  );
}

/**
 * The category's name from its id, because `PartRead` carries only the id.
 *
 * Resolved here rather than added to the part payload: the tree is already loaded
 * on most screens and cached by the browser, and putting a derived label on the
 * part row is what makes a rename show stale text until something re-saves.
 */
function CategoryName({ categoryId }: { categoryId: number }) {
  const categories = useAsync<CategoryNode[]>(() => listPartCategories(), []);
  const node = (categories.data ?? []).find((candidate) => candidate.id === categoryId) ?? null;
  return node === null ? (
    <span className="dim">#{categoryId}</span>
  ) : (
    <Link to={`/search?category=${node.slug}`}>{node.name} →</Link>
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
    category_id: part.category_id,
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
      <CategorySelect
        value={draft.category_id ?? null}
        onChange={(categoryId) => setDraft({ ...draft, category_id: categoryId })}
        hint={
          "Where it sits in the taxonomy. This is also what decides which fields it can be " +
          "filtered by — a field authored on Capacitors reaches every part filed under " +
          "Capacitors or anything inside it."
        }
      />
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

