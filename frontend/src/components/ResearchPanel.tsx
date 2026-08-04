/**
 * What the datasheet researcher tried for this part, and why it did not work.
 *
 * The screen half of ADR 0017. The backend keeps every candidate URL with its
 * verdict rather than only the winner, and this is the surface that makes that
 * worth having: a part with no datasheet is a **diagnosis**, not a dead end.
 *
 * Four rejections all reading `mpn_absent` says a provider is returning the wrong
 * part. One `not_pdf` says a login wall. No candidates at all says nothing covers
 * this manufacturer yet. Those want three different fixes, and they are
 * indistinguishable from a bare "no datasheet found" — which is exactly what this
 * panel exists not to say.
 *
 * ## Why `exhausted` is styled as information and `failed` as a problem
 *
 * They are different facts and the UI must not flatten them. A genuinely obscure
 * part with no datasheet on the open web reaches `exhausted`, and there is nothing
 * to fix — showing it in a warning colour trains people to ignore the colour.
 * `failed` means the run itself broke, which is worth someone's attention. The
 * backend already keeps them apart (`research_error` is null for `exhausted`); this
 * is that distinction carried through to the pixels.
 *
 * ## The panel renders for a part nobody has researched
 *
 * `state: "pending"` with no candidates is the ordinary case for most of the
 * catalogue and is not an empty state to apologise for. It says the queue has this
 * part and has not got to it, which is true and useful.
 */

import { useState } from "react";

import {
  getPartResearch,
  requeueResearch,
  type PartResearchRead,
  type ResearchCandidateRead,
  type ResearchState,
} from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";
import { ErrorBanner, Loading } from "./Feedback";

/** How each state reads to a person, and how loudly. */
const STATE_COPY: Record<ResearchState, { label: string; badge: string; blurb: string }> = {
  pending: {
    label: "queued",
    badge: "badge",
    blurb: "Waiting for the researcher. Nothing has looked for a datasheet yet.",
  },
  claimed: {
    label: "searching",
    badge: "badge badge-accent",
    blurb: "A worker is looking right now.",
  },
  resolved: {
    label: "found",
    badge: "badge badge-good",
    blurb: "A datasheet was found, checked against this part number, and attached.",
  },
  exhausted: {
    // Deliberately not a warning. Nothing is wrong — see the module docstring.
    label: "nothing found",
    badge: "badge",
    blurb:
      "Every source was tried and none of them had a datasheet for this exact part. " +
      "That is a normal outcome for an obscure part, not a fault.",
  },
  failed: {
    label: "search broke",
    badge: "badge badge-bad",
    blurb: "The search itself failed. This one is worth a look.",
  },
  not_applicable: {
    label: "no datasheet expected",
    badge: "badge",
    blurb: "This part is not the kind of thing that has a datasheet.",
  },
};

/** Why a candidate was refused, in words rather than in the stored slug. */
const REJECT_COPY: Record<string, string> = {
  not_pdf: "not a PDF — usually a login wall or an error page",
  too_large: "too big to be a datasheet",
  parse_failed: "a PDF, but unreadable",
  // The important one, and the wording matters: it is a real datasheet, just not
  // this part's. "Wrong part" is what a person needs to conclude.
  mpn_absent: "a real datasheet, but this part number is not in it",
  fetch_failed: "nothing was served — dead link, timeout or DNS",
};

function rejectText(candidate: ResearchCandidateRead): string {
  const reason = candidate.reject_reason ?? "";
  return REJECT_COPY[reason] ?? reason ?? "refused";
}

export function ResearchPanel({ partId }: { partId: number }) {
  const research = useAsync<PartResearchRead>(() => getPartResearch(partId), [partId]);
  const [queueing, setQueueing] = useState(false);
  const [queueError, setQueueError] = useState<unknown>(null);

  if (research.error !== null) {
    return <ErrorBanner error={research.error} fallback="The research history could not be loaded." />;
  }
  if (research.data === null) {
    return <Loading what="the research history" />;
  }

  const data = research.data;
  const copy = STATE_COPY[data.state];
  const candidates = data.candidates;
  const tried = candidates.length;
  // Counted rather than merely listed: "four sources, all the wrong part" is the
  // sentence that turns a list into a diagnosis.
  const wrongPart = candidates.filter((c) => c.reject_reason === "mpn_absent").length;

  async function requeue() {
    setQueueing(true);
    setQueueError(null);
    try {
      await requeueResearch(partId);
      research.reload();
    } catch (error) {
      setQueueError(error);
    } finally {
      setQueueing(false);
    }
  }

  // Offered for the two terminal states only. Re-queueing a part that is already
  // queued or in flight does nothing useful and invites double-clicking a worker
  // into a second lease.
  const canRequeue = data.state === "exhausted" || data.state === "failed";

  return (
    <div className="card">
      <div className="row">
        <h3 style={{ flex: 1 }}>Datasheet search</h3>
        <span className={copy.badge}>{copy.label}</span>
      </div>

      <p style={{ margin: 0 }}>{copy.blurb}</p>

      {data.error !== null && (
        <p className="mono" style={{ margin: 0, fontSize: "0.85em" }}>
          {data.error}
        </p>
      )}

      {tried > 0 && (
        <p style={{ margin: 0, fontSize: "0.9em", opacity: 0.8 }}>
          {tried} {tried === 1 ? "source" : "sources"} tried
          {wrongPart > 0 &&
            `, ${wrongPart} of them a datasheet for a different part`}
          .
        </p>
      )}

      {tried > 0 && (
        <ul className="stack" style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {candidates.map((candidate) => (
            <li key={candidate.url} className="row" style={{ alignItems: "baseline", gap: "0.5rem" }}>
              <span className={candidate.state === "validated" ? "badge badge-good" : "badge"}>
                {candidate.source}
              </span>
              <span style={{ flex: 1, minWidth: 0 }}>
                <a
                  href={candidate.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="mono"
                  style={{ fontSize: "0.85em", wordBreak: "break-all" }}
                >
                  {candidate.url}
                </a>
                {candidate.state === "rejected" && (
                  <span style={{ display: "block", fontSize: "0.85em", opacity: 0.8 }}>
                    {rejectText(candidate)}
                  </span>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}

      {queueError !== null && (
        <ErrorBanner error={queueError} fallback="That part could not be queued." />
      )}

      {canRequeue && (
        <button type="button" className="wide" disabled={queueing} onClick={requeue}>
          {queueing ? "Queueing…" : "Search again"}
        </button>
      )}
    </div>
  );
}
