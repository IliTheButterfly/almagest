/**
 * The review queue — the safety valve for every "never auto-accept" rule.
 *
 * Every candidate that lands here is here because a rule refused to guess:
 * one source below the confidence bar, two sources that disagree, or a
 * reading (a marking, a model-read identity) that must never auto-promote at
 * any confidence. That refusal is only worth anything if a human actually
 * works the queue, so this screen's whole design is aimed at making that
 * fast:
 *
 * - **grouped by part, then by field** — fixing a family's five decoded
 *   fields is one pass, not five separate lookups;
 * - **every candidate shows its evidence** — a model or table reading's
 *   `note` carries the quoted line it was read from (see
 *   `cross_check.review_note` on the server); an item with nothing to show is
 *   flagged rather than silently trusted, because a value nobody can judge is
 *   a prompt to guess, not a review;
 * - **a disagreement shows every value, not just the one the priority order
 *   would pick** — that pick is marked, never hidden behind the others;
 * - **bulk accept** for the common case of a whole decoded family being
 *   obviously right, without losing the one-id-at-a-time behaviour for a
 *   single correction.
 *
 * Accepting or correcting a field closes every pending candidate for it, not
 * only the one clicked — see the server route's docstring — so a resolved
 * field simply drops out of the list on the next load rather than lingering
 * with a "still pending" sibling nobody asked about again.
 */

import { useState } from "react";
import { Link } from "react-router-dom";

import { Empty, ErrorBanner, Loading, Notice } from "../components/Feedback";
import {
  acceptEnrichmentCandidate,
  bulkAcceptEnrichmentCandidates,
  correctEnrichmentCandidate,
  dismissEnrichmentCandidate,
  getEnrichmentQueue,
  type EnrichmentBulkAcceptResponse,
  type EnrichmentCandidateRead,
  type EnrichmentFieldGroup,
  type EnrichmentPartGroup,
} from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";

const SOURCE_LABELS: Record<string, string> = {
  manual: "Manual",
  datasheet_table: "Datasheet table",
  mpn_decoder: "Part-number decoder",
  distributor_freetext: "Distributor listing",
  llm_inferred: "Model reading",
};

function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

/** Reasons `accept` is refused server-side, mapped to the badge that says so.
 *
 * `unparseable` carries no value to write at all; `one_sided_limit` parsed but
 * to a bound rather than a value, which `parameters.set_numeric` refuses because
 * a null-bounded row matches no range query. Either way the screen offers
 * Correct/Dismiss and hides Accept and the bulk-select box, rather than offer an
 * action the API will reject — and the badge names *which* refusal, since the
 * fix differs: one is a grammar gap to report, the other is a two-sided value to
 * type. */
const UNACCEPTABLE_REASONS: Record<string, string> = {
  unparseable: "could not be parsed",
  one_sided_limit: "a limit, not a value",
};

function unacceptableBadge(candidate: EnrichmentCandidateRead): string | null {
  if (candidate.review_reason === null) {
    return null;
  }
  return UNACCEPTABLE_REASONS[candidate.review_reason] ?? null;
}

export function ReviewScreen() {
  const queue = useAsync(() => getEnrichmentQueue({ limit: 100 }), []);
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkError, setBulkError] = useState<unknown>(null);
  const [bulkResult, setBulkResult] = useState<EnrichmentBulkAcceptResponse | null>(null);

  function setCandidateSelected(id: number, on: boolean): void {
    setSelected((previous) => {
      const next = new Set(previous);
      if (on) {
        next.add(id);
      } else {
        next.delete(id);
      }
      return next;
    });
  }

  async function acceptSelected(): Promise<void> {
    if (selected.size === 0) {
      return;
    }
    setBulkBusy(true);
    setBulkError(null);
    try {
      const response = await bulkAcceptEnrichmentCandidates([...selected]);
      setBulkResult(response);
      setSelected(new Set());
      queue.reload();
    } catch (cause) {
      setBulkError(cause);
    } finally {
      setBulkBusy(false);
    }
  }

  function onChanged(): void {
    setBulkResult(null);
    queue.reload();
  }

  if (queue.error !== null) {
    return <ErrorBanner error={queue.error} fallback="The review queue could not be loaded." />;
  }
  if (queue.data === null) {
    return <Loading what="the review queue" />;
  }
  const data = queue.data;

  return (
    <div className="stack">
      <div className="card">
        <h1>Review queue</h1>
        <p className="muted-note" style={{ margin: 0 }}>
          {data.total_candidates} pending candidate(s) across {data.total_parts} part(s).
          Nothing shown here is in the catalogue yet — every rule that got a value this
          far refused to guess, on purpose.
        </p>
        {selected.size > 0 && (
          <div className="row" style={{ marginTop: "0.6rem" }}>
            <button
              type="button"
              className="primary"
              onClick={() => void acceptSelected()}
              disabled={bulkBusy}
            >
              {bulkBusy ? "Accepting…" : `Accept ${selected.size} selected`}
            </button>
            <button
              type="button"
              onClick={() => setSelected(new Set())}
              disabled={bulkBusy}
            >
              Clear selection
            </button>
          </div>
        )}
        <ErrorBanner error={bulkError} fallback="That batch could not be accepted." />
        {bulkResult !== null && <BulkResultSummary result={bulkResult} />}
      </div>

      {data.parts.length === 0 ? (
        <Empty>
          Nothing is waiting on a human right now. Every candidate either promoted
          itself already or has not arrived yet.
        </Empty>
      ) : (
        data.parts.map((group) => (
          <PartCard
            key={group.part_id}
            group={group}
            selected={selected}
            onToggle={setCandidateSelected}
            onChanged={onChanged}
          />
        ))
      )}
    </div>
  );
}

function BulkResultSummary({ result }: { result: EnrichmentBulkAcceptResponse }) {
  const failed = result.results.filter((row) => !row.accepted);
  return (
    <Notice kind={failed.length > 0 ? "warn" : "ok"} title="Bulk accept result">
      <p style={{ margin: 0 }}>
        {result.results.length - failed.length} of {result.results.length} accepted.
      </p>
      {failed.length > 0 && (
        <ul>
          {failed.map((row) => (
            <li key={row.candidate_id} className="muted-note">
              candidate #{row.candidate_id}: {row.reason}
            </li>
          ))}
        </ul>
      )}
    </Notice>
  );
}

function PartCard({
  group,
  selected,
  onToggle,
  onChanged,
}: {
  group: EnrichmentPartGroup;
  selected: ReadonlySet<number>;
  onToggle: (id: number, on: boolean) => void;
  onChanged: () => void;
}) {
  return (
    <div className="card">
      <div className="row">
        <Link to={`/parts/${group.part_id}`} className="title" style={{ flex: 1 }}>
          {group.part_name}
        </Link>
        {group.part_mpn !== null && <span className="mono dim">{group.part_mpn}</span>}
      </div>
      <div className="stack">
        {group.fields.map((field) => (
          <FieldCard
            key={field.template_id}
            field={field}
            selected={selected}
            onToggle={onToggle}
            onChanged={onChanged}
          />
        ))}
      </div>
    </div>
  );
}

function FieldCard({
  field,
  selected,
  onToggle,
  onChanged,
}: {
  field: EnrichmentFieldGroup;
  selected: ReadonlySet<number>;
  onToggle: (id: number, on: boolean) => void;
  onChanged: () => void;
}) {
  const disagreement = field.candidates.length > 1;
  return (
    <div className="card">
      <div className="row">
        <span className="title" style={{ flex: 1 }}>
          {field.template_name}
          {field.template_unit !== null && (
            <span className="dim"> ({field.template_unit})</span>
          )}
        </span>
        {disagreement && <span className="badge badge-warn">disagreement</span>}
      </div>
      {field.existing_raw_input !== null && (
        <p className="sub">
          Currently stored: <span className="mono">{field.existing_raw_input}</span>{" "}
          {field.existing_provenance !== null && (
            <span className="badge">{sourceLabel(field.existing_provenance)}</span>
          )}
        </p>
      )}
      <ul className="list">
        {field.candidates.map((candidate) => (
          <CandidateRow
            key={candidate.id}
            candidate={candidate}
            recommended={candidate.id === field.recommended_candidate_id}
            selected={selected.has(candidate.id)}
            onToggle={(on) => onToggle(candidate.id, on)}
            onChanged={onChanged}
          />
        ))}
      </ul>
    </div>
  );
}

function CandidateRow({
  candidate,
  recommended,
  selected,
  onToggle,
  onChanged,
}: {
  candidate: EnrichmentCandidateRead;
  recommended: boolean;
  selected: boolean;
  onToggle: (on: boolean) => void;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [correcting, setCorrecting] = useState(false);
  const refusalBadge = unacceptableBadge(candidate);
  const unacceptable = refusalBadge !== null;
  const isUrl = /^https?:\/\//.test(candidate.source_ref);

  async function accept(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await acceptEnrichmentCandidate(candidate.id);
      onChanged();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  async function dismiss(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await dismissEnrichmentCandidate(candidate.id);
      onChanged();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <li>
      <div className="list-item">
        <div className="row">
          {!unacceptable && (
            <input
              type="checkbox"
              aria-label={`select candidate ${candidate.id}`}
              checked={selected}
              onChange={(event) => onToggle(event.target.checked)}
              disabled={busy}
            />
          )}
          <span className="badge mono">{sourceLabel(candidate.source)}</span>
          {recommended && <span className="badge badge-good">priority pick</span>}
          {candidate.requires_human && (
            <span className="badge badge-warn">needs a human — never auto-accepted</span>
          )}
          {refusalBadge !== null && <span className="badge badge-bad">{refusalBadge}</span>}
          <span className="spacer" />
          <span className="muted-note">{Math.round(candidate.confidence * 100)}% confidence</span>
        </div>
        <div className="title mono">{candidate.choice_key ?? candidate.raw_value}</div>

        {candidate.note !== null ? (
          <blockquote className="sub" style={{ margin: "0.3rem 0", paddingLeft: "0.6rem" }}>
            {candidate.note}
          </blockquote>
        ) : (
          <p className="sub dim" style={{ margin: "0.3rem 0" }}>
            No evidence recorded for this reading — there is nothing here for a
            human to judge it against.
          </p>
        )}
        {candidate.source_ref !== "" && (
          <div className="sub mono">
            {isUrl ? (
              <a href={candidate.source_ref} target="_blank" rel="noreferrer">
                {candidate.source_ref}
              </a>
            ) : (
              candidate.source_ref
            )}
          </div>
        )}

        <div className="row">
          {!unacceptable && (
            <button type="button" className="primary" onClick={() => void accept()} disabled={busy}>
              Accept
            </button>
          )}
          <button type="button" onClick={() => setCorrecting(!correcting)} disabled={busy}>
            {correcting ? "Cancel" : "Correct"}
          </button>
          <button type="button" onClick={() => void dismiss()} disabled={busy}>
            Dismiss
          </button>
        </div>

        <ErrorBanner error={error} fallback="That action did not go through." />

        {correcting && (
          <CorrectForm
            candidate={candidate}
            onDone={() => {
              setCorrecting(false);
              onChanged();
            }}
          />
        )}
      </div>
    </li>
  );
}

/**
 * A human's replacement value. Submitted as a fresh `manual` candidate on the
 * server — never as an edit to the row shown above, which stays exactly what
 * its source said even after being outranked.
 */
function CorrectForm({
  candidate,
  onDone,
}: {
  candidate: EnrichmentCandidateRead;
  onDone: () => void;
}) {
  const [rawValue, setRawValue] = useState(candidate.choice_key ?? candidate.raw_value);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // Pre-filled from the candidate's own reading, so this only goes empty if
  // it is cleared while correcting it — the same silent-disable shape as an
  // untouched required field, just reached from the other direction.
  const rawValueEmpty = rawValue.trim() === "";

  async function submit(): Promise<void> {
    if (rawValueEmpty) {
      // The submit button is disabled for the same reason; this guards a
      // stray Enter-key submit from going nowhere with no explanation.
      setError(new Error("Enter the correct value — an empty correction is not saved."));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await correctEnrichmentCandidate(candidate.id, {
        raw_value: rawValue.trim(),
        note: note.trim() === "" ? null : note.trim(),
      });
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
      style={{ marginTop: "0.4rem" }}
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <label className="field">
        <span>Correct value</span>
        <input
          className="mono"
          value={rawValue}
          onChange={(event) => setRawValue(event.target.value)}
        />
      </label>
      {rawValueEmpty && (
        <p className="muted-note">
          Enter a value — that is why "Save correction" below is disabled.
        </p>
      )}
      <label className="field">
        <span>Why (optional, goes on the record)</span>
        <input
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="e.g. checked the physical part, it is a 475 marking"
        />
      </label>
      <ErrorBanner error={error} fallback="That correction was not saved." />
      <button type="submit" className="primary wide" disabled={busy || rawValueEmpty}>
        {busy ? "Saving…" : "Save correction"}
      </button>
    </form>
  );
}
