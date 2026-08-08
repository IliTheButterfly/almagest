/**
 * `/intake/:entryId/activity` — one parked scan's whole story, in one place.
 *
 * The pipeline is three workers that deliberately do not know about each other, and
 * the cost of that is this screen's reason to exist: **nothing else can say what
 * happened to a photograph.** The intake queue shows the proposals, the part screen
 * shows the datasheet, the review queue shows the fields, and the question a person
 * actually asks — *I photographed a resistor, why did it come out as `CFI4JT100K`* —
 * is answered by none of them because the answer spans all three.
 *
 * ## Three rules this screen is written around
 *
 * **The transcript is what makes the never-auto-accept rule reviewable.** ADR 0021's
 * `source_text` gives a reviewer the characters the model claims it read. That answers
 * *what it said*. The next question is always *what was it told* — did the browser's
 * OCR hand it the typo and did it copy it, was the barcode anchor there, did the
 * reasoning budget run out before the answer began. So the prompt is here, not only the
 * answer.
 *
 * **The prompt and the raw response live behind `<details>`.** They are long, and they
 * are for when something looks wrong rather than for every visit. Everything a person
 * skims — which model, what it cost, which candidates — is open.
 *
 * **"Not recorded" is never written as "0".** A server that omits `usage` is ordinary,
 * and a zero would read as "the prompt was empty". Same rule at the other end: where no
 * worker has run, the section says so in words rather than rendering an empty list,
 * because an empty list reads as a failure.
 *
 * ## The confidence shown is the stored, clamped number
 *
 * `identity_candidates[].confidence` was clamped strictly below the promotion threshold
 * on the way into the database, because reading characters off a photograph and trusting
 * a datasheet's statement of a value are different quantities that happen to share a
 * range. The model's own self-report survives only inside the raw response, and where
 * that is shown it is shown as part of a transcript and labelled as one. The two are
 * never printed as the same number: ADR 0021 measured 0.95 self-reported on an answer
 * that was the item's FCC ID.
 */

import { Link, useParams } from "react-router-dom";

import { ErrorBanner, Loading, Notice } from "../components/Feedback";
import {
  getIntakeActivity,
  type IntakeActivityRead,
  type ModelRunRead,
  type ResolvedPartActivity,
} from "../lib/api/client";
import { formatTimestamp } from "../lib/format";
import { useAsync } from "../lib/hooks/useAsync";
import { headlineFor } from "./IntakeQueueScreen";

/** What a missing number is called. Never "0" — see the module docstring. */
const UNRECORDED = "not recorded";

function count(value: number | null | undefined): string {
  return value === null || value === undefined ? UNRECORDED : String(value);
}

/** Seconds, to one decimal. `latency_ms` is the provider's own measurement. */
function duration(ms: number | null | undefined): string {
  return ms === null || ms === undefined ? UNRECORDED : `${(ms / 1000).toFixed(1)}s`;
}

export function IntakeActivityScreen() {
  const { entryId } = useParams<{ entryId: string }>();
  const id = Number(entryId);
  const activity = useAsync<IntakeActivityRead>(() => getIntakeActivity(id), [id]);

  if (activity.loading && activity.data === null) {
    return <Loading what="this entry's activity" />;
  }
  if (activity.data === null) {
    return (
      <div className="stack">
        <ErrorBanner error={activity.error} fallback="That entry's activity could not be read." />
        <p>
          <Link to="/intake">← Back to the intake queue</Link>
        </p>
      </div>
    );
  }

  const { entry, capture, dispatch, model_runs: runs, identity_candidates: candidates } =
    activity.data;

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <h1 style={{ flex: 1 }}>
            Intake #{entry.id} — {entry.mpn ?? headlineFor(entry.raw_payload)}
          </h1>
          <span className="badge">{entry.status}</span>
        </div>
        <div className="sub">
          {formatTimestamp(entry.queued_at ?? entry.created_at)}
          {entry.device_id === null ? "" : ` · ${entry.device_id}`}
          {entry.symbology === null ? "" : ` · ${entry.symbology}`}
        </div>
        <p className="muted-note" style={{ margin: 0 }}>
          Everything that has happened to this scan, oldest first. The three stages after
          it — reading the label, finding a datasheet, reading that datasheet — run as
          separate workers on their own schedules, so a stage with nothing under it is
          usually one that has not run rather than one that failed.
        </p>
        <p>
          <Link to="/intake">← Back to the intake queue</Link>
        </p>
      </div>

      <CaptureSection capture={capture} />

      <div className="card">
        <div className="row">
          <h3 style={{ margin: 0, flex: 1 }}>Read the label</h3>
          <span className="badge">{dispatch.state}</span>
        </div>
        <div className="sub">
          attempt {dispatch.attempts} of {dispatch.max_attempts}
          {dispatch.label_kind === null ? "" : ` · looks like a ${dispatch.label_kind}`}
        </div>
        {dispatch.error !== null && (
          <Notice kind="warn" title="The run broke">
            <p style={{ margin: 0 }}>{dispatch.error}</p>
          </Notice>
        )}
        {dispatch.state === "unidentified" && (
          <Notice kind="info" title="Nothing legible">
            <p style={{ margin: 0 }}>
              A model looked and could not name a part. That is not a fault — the fix is
              another photograph, closer or with less glare.
            </p>
          </Notice>
        )}

        {runs.length === 0 ? (
          <Notice kind="info" title="No worker has run">
            <p style={{ margin: 0 }}>
              {dispatch.state === "not_requested"
                ? "Nobody has asked a model to read this photograph. Reading one costs the graphics card, so it is off until somebody asks."
                : "This entry is queued, but no reader has picked it up yet. Nothing has been recorded against it."}
            </p>
          </Notice>
        ) : (
          <ol className="list">
            {runs.map((run, index) => (
              <RunRow key={run.id} run={run} index={index + 1} />
            ))}
          </ol>
        )}
      </div>

      <div className="card">
        <h3 style={{ margin: 0 }}>What it proposed</h3>
        {candidates.length === 0 ? (
          <p className="muted-note" style={{ margin: 0 }}>
            {runs.length === 0
              ? "Nothing yet, because nothing has read it."
              : "The run finished and named nothing. An empty answer is a real answer here — a model pushed to guess is a model inventing a part number."}
          </p>
        ) : (
          <>
            <p className="muted-note" style={{ margin: 0 }}>
              Best first, losers kept. Check the quoted characters against the picture: a
              model can read a certification number as confidently as a part number.
            </p>
            <ul className="list">
              {candidates.map((candidate) => (
                <li className="list-item" key={candidate.mpn}>
                  <div className="row">
                    <span className="title mono" style={{ flex: 1, overflowWrap: "anywhere" }}>
                      {candidate.mpn}
                    </span>
                    {candidate.manufacturer !== null && (
                      <span className="badge">{candidate.manufacturer}</span>
                    )}
                  </div>
                  <div className="sub">
                    quoted <span className="mono">“{candidate.source_text}”</span>
                    {candidate.package === null ? "" : ` · ${candidate.package}`}
                    {/* The *stored* number, clamped below the promotion threshold on the
                        way in. Said in words so it cannot be mistaken for a measurement,
                        and so a reader who also opens the raw response above knows which
                        of the two numbers they are looking at. */}
                    {` · stored confidence ${candidate.confidence.toFixed(2)} (clamped; the model's own self-report is in the raw response above)`}
                  </div>
                  {candidate.note !== null && <div className="sub">{candidate.note}</div>}
                  {candidate.part_id !== null && (
                    <div className="sub">
                      <Link to={`/parts/${candidate.part_id}`}>Stub part {candidate.part_id} →</Link>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>

      <AcceptedSection entry={activity.data.entry} part={activity.data.resolved_part} />
    </div>
  );
}

/** The photograph and what the browser read off it, before any model saw it. */
function CaptureSection({ capture }: { capture: IntakeActivityRead["capture"] }) {
  if (capture === null) {
    return (
      <div className="card">
        <h3 style={{ margin: 0 }}>The photograph</h3>
        <p className="muted-note" style={{ margin: 0 }}>
          There is none. This was a bare barcode scan, which is the ordinary fast path —
          nothing was lost.
        </p>
      </div>
    );
  }

  const barcodes = capture.regions.filter((region) => region.kind === "barcode");
  const lines = capture.regions.filter((region) => region.kind !== "barcode");

  return (
    <div className="card">
      <div className="row">
        <h3 style={{ margin: 0, flex: 1 }}>The photograph, and what the browser read</h3>
        <span className="badge">{capture.text_status}</span>
      </div>
      <div className="sub">
        {formatTimestamp(capture.created_at)} · {capture.width_px}×{capture.height_px} ·{" "}
        {barcodes.length} barcode{barcodes.length === 1 ? "" : "s"}, {lines.length} text line
        {lines.length === 1 ? "" : "s"}
      </div>
      <p className="muted-note" style={{ margin: 0 }}>
        Both passes ran in the browser. A decoded barcode is checksummed and is the
        strongest evidence anywhere in this chain; an OCR line is the weakest, and is
        exactly what the model was given a chance to repair.
      </p>
      <ul className="list">
        {capture.regions.map((region, index) => (
          <li className="list-item" key={`${region.kind}-${index}`}>
            <div className="row">
              <span className="title mono" style={{ flex: 1, overflowWrap: "anywhere" }}>
                {region.text}
              </span>
              <span className="badge">{region.symbology ?? region.kind}</span>
            </div>
            <div className="sub">
              {region.kind === "barcode"
                ? "decoded — checksummed"
                : /* A barcode has no meaningful confidence: it checksummed or it did
                     not. Only a text region carries one, and a missing one says the
                     reader did not report it rather than that it read nothing. */
                  `read by OCR · confidence ${count(region.confidence)}`}
            </div>
          </li>
        ))}
      </ul>
      <p className="sub mono" style={{ overflowWrap: "anywhere" }}>
        <a href={`/api/documents/${capture.document_sha256}`}>the image →</a>
      </p>
    </div>
  );
}

/**
 * One model call: which model, what it cost, and the conversation behind a disclosure.
 *
 * The cost is open and the transcript is collapsed, which is the split the docstring at
 * the top argues: the numbers are what a person skims, and the prompt is what they open
 * when a reading looks wrong.
 */
function RunRow({ run, index }: { run: ModelRunRead; index: number }) {
  return (
    <li className="list-item">
      <div className="row">
        <span className="title" style={{ flex: 1 }}>
          Run {index} — <span className="mono">{run.model}</span> via {run.provider}
        </span>
        {run.error === null ? (
          <span className="badge badge-good">answered</span>
        ) : (
          <span className="badge badge-bad">broke</span>
        )}
      </div>
      <div className="sub">
        {formatTimestamp(run.started_at)} · {duration(run.latency_ms)} ·{" "}
        {count(run.prompt_tokens)} prompt / {count(run.completion_tokens)} completion tokens
        {` · finish_reason: ${run.finish_reason ?? UNRECORDED}`}
      </div>
      {run.finish_reason === "length" && (
        <Notice kind="warn" title="It ran out of room">
          <p style={{ margin: 0 }}>
            The answer stopped at the token ceiling rather than finishing. That is a budget
            set too low, not a broken model — and on a thinking model the reasoning is
            spent from the same budget before the answer starts.
          </p>
        </Notice>
      )}
      {run.truncated && (
        <p className="muted-note" style={{ margin: 0 }}>
          One of the two texts below was longer than the column and was cut. What is shown
          is the beginning of it.
        </p>
      )}
      {run.error !== null && (
        <Notice kind="warn" title="What broke">
          <p style={{ margin: 0 }}>{run.error}</p>
        </Notice>
      )}

      {run.request_json === null ? (
        <p className="muted-note" style={{ margin: 0 }}>
          The prompt was not recorded — the call broke before there was one to record.
        </p>
      ) : (
        <details>
          <summary>Show the prompt</summary>
          <p className="muted-note" style={{ margin: "0.4rem 0" }}>
            Exactly what was sent, with the image replaced by its hash — the picture is
            already stored once and a copy here would be megabytes per run. This is the
            half that says whether the model repeated a mistake it was handed.
          </p>
          <pre className="mono" style={{ overflowX: "auto", whiteSpace: "pre-wrap" }}>
            {run.request_json}
          </pre>
        </details>
      )}

      {run.response_text === null ? (
        <p className="muted-note" style={{ margin: 0 }}>
          No completion came back at all.
        </p>
      ) : (
        <details>
          <summary>Show the raw response</summary>
          <p className="muted-note" style={{ margin: "0.4rem 0" }}>
            The completion string as returned, before anything parsed it. Any confidence
            number in here is <strong>the model's own claim about itself</strong>, not the
            value stored against a candidate — those are different quantities and the
            stored one is clamped.
          </p>
          <pre className="mono" style={{ overflowX: "auto", whiteSpace: "pre-wrap" }}>
            {run.response_text}
          </pre>
        </details>
      )}
    </li>
  );
}

/**
 * The part a **person** accepted, and what the later workers made of it.
 *
 * Reached through `resolved_part_id` only. A candidate's stub `part_id` is a machine's
 * proposal, and following it would present three unaccepted stubs' research as though it
 * were this entry's outcome.
 */
function AcceptedSection({
  entry,
  part,
}: {
  entry: IntakeActivityRead["entry"];
  part: ResolvedPartActivity | null;
}) {
  if (part === null) {
    return (
      <div className="card">
        <h3 style={{ margin: 0 }}>Accepted</h3>
        <p className="muted-note" style={{ margin: 0 }}>
          Nobody has chosen yet. A reading is never accepted automatically, at any
          confidence — that is the one rule the rest of this screen exists to make
          checkable. Choose one back on the <Link to="/intake">intake queue</Link>.
        </p>
      </div>
    );
  }

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <h3 style={{ margin: 0, flex: 1 }}>Accepted</h3>
          {part.is_stub && <span className="badge">stub</span>}
        </div>
        <div className="sub">
          {entry.resolved_at === null ? "" : `${formatTimestamp(entry.resolved_at)} · `}
          <Link to={`/parts/${part.id}`}>
            {part.name} (part {part.id}) →
          </Link>
          {part.mpn === null ? "" : ` · ${part.mpn}`}
        </div>
      </div>

      <div className="card">
        <div className="row">
          <h3 style={{ margin: 0, flex: 1 }}>Find a datasheet</h3>
          <span className="badge">{part.research_state}</span>
        </div>
        <div className="sub">attempt {part.research_attempts}</div>
        {part.research_error !== null && (
          <Notice kind="warn" title="The search broke">
            <p style={{ margin: 0 }}>{part.research_error}</p>
          </Notice>
        )}
        {part.research_candidates.length === 0 ? (
          <p className="muted-note" style={{ margin: 0 }}>
            {part.research_state === "pending"
              ? "Queued. No worker has run yet, which is the normal state — the searcher runs on its own schedule."
              : "Nothing was proposed. A part with no candidate rows was never looked at, which is a different problem from one whose every candidate was refused."}
          </p>
        ) : (
          <ul className="list">
            {part.research_candidates.map((candidate) => (
              <li className="list-item" key={candidate.url}>
                <div className="row">
                  <span className="title mono" style={{ flex: 1, overflowWrap: "anywhere" }}>
                    {candidate.url}
                  </span>
                  <span className="badge">{candidate.state}</span>
                </div>
                <div className="sub">
                  from {candidate.source}
                  {candidate.reject_reason === null ? "" : ` · refused: ${candidate.reject_reason}`}
                </div>
                {candidate.note !== null && <div className="sub">{candidate.note}</div>}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="card">
        <h3 style={{ margin: 0 }}>Read the datasheet</h3>
        {part.documents.length === 0 ? (
          <p className="muted-note" style={{ margin: 0 }}>
            No document is attached, so there is nothing to read yet.
          </p>
        ) : (
          <ul className="list">
            {part.documents.map((document) => (
              <li className="list-item" key={document.sha256}>
                <div className="row">
                  <span className="title mono" style={{ flex: 1, overflowWrap: "anywhere" }}>
                    {document.sha256.slice(0, 16)}…
                  </span>
                  <span className="badge">{document.extraction_state}</span>
                </div>
                <div className="sub">
                  {document.media_type} · {document.byte_size} bytes · attempt{" "}
                  {document.extraction_attempts}
                </div>
                {/* `pending` is normal, not broken: a stored PDF whose text nobody has
                    read yet. Said here because a state badge alone reads as a warning. */}
                {document.extraction_state === "pending" && (
                  <div className="sub">
                    Stored and served; its text has not been read. That is normal, not a
                    failure — only search over the contents waits on it.
                  </div>
                )}
                {document.extraction_error !== null && (
                  <div className="sub">{document.extraction_error}</div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="card">
        <h3 style={{ margin: 0 }}>Proposed fields</h3>
        {part.field_candidates.length === 0 ? (
          <p className="muted-note" style={{ margin: 0 }}>
            None. Nothing has proposed a value for this part yet.
          </p>
        ) : (
          <ul className="list">
            {part.field_candidates.map((candidate, index) => (
              <li className="list-item" key={`${candidate.template_name}-${index}`}>
                <div className="row">
                  <span className="title" style={{ flex: 1 }}>
                    {candidate.template_label}
                  </span>
                  <span className="badge">{candidate.status}</span>
                </div>
                <div className="sub">
                  <span className="mono">{candidate.raw_value}</span> · from {candidate.source}
                  {` · confidence ${candidate.confidence.toFixed(2)}`}
                  {candidate.review_reason === null ? "" : ` · ${candidate.review_reason}`}
                </div>
                {candidate.requires_human && (
                  <div className="sub">Waiting on a person in the review queue.</div>
                )}
              </li>
            ))}
          </ul>
        )}
        <p className="muted-note" style={{ margin: 0 }}>
          Whatever is still waiting is on the <Link to="/review">review queue</Link>.
        </p>
      </div>
    </div>
  );
}
