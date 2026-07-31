/**
 * The BOM table for one project.
 *
 * **The load-bearing design problem**: a fresh import lands with most lines
 * `part_id IS NULL`, and that is the *normal* state of a new BOM, not an
 * error — the whole import strategy in `app.services.bom_import` assumes a
 * human curates matches here afterward. So this screen treats three states as
 * genuinely different rows, not shades of one "needs attention" grey:
 *
 * - **unmatched** (`part_id IS NULL`) — amber, `!`, and the one-tap "Match"
 *   action is right on the row, because burying it behind a second screen is
 *   exactly the friction that would make curation not happen;
 * - **auto-matched** (`part_id` set by an exact-MPN import hit, unconfirmed)
 *   — a distinct accent colour and glyph, because `bom_import` deliberately
 *   never sets `is_match_confirmed`: an exact string match is strong evidence,
 *   not a human's agreement, and conflating the two is the bug CLAUDE.md's
 *   "never auto-accept" rule exists to prevent one layer up;
 * - **matched** (a human has confirmed it) — green, `✓`.
 *
 * DNP lines are reported, never hidden — dropping them would make the BOM the
 * user sees not the BOM that was imported.
 */

import { useState, type ChangeEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ErrorBanner, Empty, Loading, Notice } from "../components/Feedback";
import { PartResultRow } from "../components/PartResultRow";
import { PathBar } from "../components/PathBar";
import {
  getProject,
  importBom,
  listBomLines,
  searchParts,
  suggestRequirements,
  updateBomLines,
  type BomLineEdit,
  type BomLineRead,
  type BomImportResponse,
  type PartCandidateRead,
  type PartSummary,
  type ProjectRead,
  type RequirementRead,
  type SuggestionLineRead,
} from "../lib/api/client";
import { openTargets } from "../lib/projectcontext/store";
import { formatQty } from "../lib/format";
import { useAsync } from "../lib/hooks/useAsync";
import { uuid4 } from "../lib/scan/session";

// Whole units, not milli — the same `value / 1000` convention
// `BuildScreen.ReserveStock`'s quantity field already uses, so a per-assembly
// quantity reads and types the same way everywhere it appears.
const MILLI = 1000;

type LineState = "dnp" | "unmatched" | "unconfirmed" | "matched";

function stateOf(line: BomLineRead): LineState {
  if (line.is_dnp) {
    return "dnp";
  }
  if (line.part_id === null) {
    return "unmatched";
  }
  return line.is_match_confirmed ? "matched" : "unconfirmed";
}

export function BomScreen() {
  const { projectId: raw } = useParams();
  const projectId = Number(raw);
  const valid = Number.isSafeInteger(projectId) && projectId > 0;

  const project = useAsync<ProjectRead | null>(
    () => (valid ? getProject(projectId) : Promise.resolve(null)),
    [projectId, valid],
  );

  if (!valid) {
    return <Notice kind="error" title="That is not a project id" />;
  }
  if (project.error !== null) {
    return <ErrorBanner error={project.error} fallback="That project could not be loaded." />;
  }
  if (project.data === null) {
    return <Loading what="the project" />;
  }
  return <Bom project={project.data} />;
}

function Bom({ project }: { project: ProjectRead }) {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const [importing, setImporting] = useState(false);
  const [adding, setAdding] = useState(false);
  const [pasting, setPasting] = useState(false);
  const unmatchedOnly = params.get("unmatched") === "1";

  const lines = useAsync(() => listBomLines(project.id, { unmatchedOnly, limit: 1000 }), [
    project.id,
    unmatchedOnly,
  ]);

  function setUnmatchedOnly(value: boolean): void {
    const next = new URLSearchParams(params);
    if (value) {
      next.set("unmatched", "1");
    } else {
      next.delete("unmatched");
    }
    setParams(next);
  }

  const unmatchedCount =
    lines.data?.lines.filter((line) => stateOf(line) === "unmatched").length ?? 0;
  const unconfirmedCount =
    lines.data?.lines.filter((line) => stateOf(line) === "unconfirmed").length ?? 0;

  return (
    <div className="stack">
      <div className="card">
        <PathBar
          trail={[
            { key: "projects", label: "Projects", to: "/projects" },
            { key: `project-${project.id}`, label: project.name, to: `/projects/${project.id}` },
            { key: "bom", label: "Bill of materials" },
          ]}
          label="BOM path"
        />
        <h1>Bill of materials</h1>
        <div className="row">
          <div className="segmented" role="group" aria-label="Filter">
            <button
              type="button"
              aria-pressed={!unmatchedOnly}
              onClick={() => setUnmatchedOnly(false)}
            >
              All lines
            </button>
            <button
              type="button"
              aria-pressed={unmatchedOnly}
              onClick={() => setUnmatchedOnly(true)}
            >
              Unmatched only
            </button>
          </div>
          <span className="spacer" />
          {/* A BOM you are *deciding* rather than transcribing is built by walking
              the shelves, which is what ADR 0010 turned into a mode: this opens the
              project as a tab and drops you in search, so every take from here on
              is attributed to it until the tab is closed. */}
          <button
            type="button"
            onClick={() => {
              openTargets.openTarget({
                kind: "project",
                projectId: project.id,
                label: project.name,
              });
              navigate("/search");
            }}
          >
            Choose from what you have
          </button>
          <button
            type="button"
            onClick={() => {
              setAdding(!adding);
              setImporting(false);
              setPasting(false);
            }}
          >
            {adding ? "Cancel" : "Add a line"}
          </button>
          <button
            type="button"
            onClick={() => {
              setPasting(!pasting);
              setImporting(false);
              setAdding(false);
            }}
          >
            {pasting ? "Cancel" : "Paste requirements"}
          </button>
          <button
            type="button"
            className="primary"
            onClick={() => {
              setImporting(!importing);
              setAdding(false);
              setPasting(false);
            }}
          >
            {importing ? "Cancel" : "Import CSV"}
          </button>
        </div>
        {!unmatchedOnly && lines.data !== null && (unmatchedCount > 0 || unconfirmedCount > 0) && (
          <p className="muted-note" style={{ margin: 0 }}>
            {unmatchedCount > 0 && `${unmatchedCount} line(s) unmatched. `}
            {unconfirmedCount > 0 && `${unconfirmedCount} auto-matched line(s) await confirmation.`}
          </p>
        )}
      </div>

      {adding && (
        <AddBomLine
          projectId={project.id}
          onAdded={() => {
            setAdding(false);
            lines.reload();
          }}
        />
      )}

      {pasting && (
        <PasteRequirements projectId={project.id} onAccepted={() => lines.reload()} />
      )}

      {importing && (
        <ImportBom projectId={project.id} onImported={() => lines.reload()} />
      )}

      <ErrorBanner error={lines.error} fallback="The BOM could not be loaded." />
      {lines.data === null ? (
        <Loading what="the BOM" />
      ) : lines.data.lines.length === 0 ? (
        <Empty>
          {unmatchedOnly
            ? "Nothing unmatched — every line either has a part or is marked DNP."
            : "No BOM lines yet. Import a CSV/TSV export above."}
        </Empty>
      ) : (
        <ul className="list">
          {lines.data.lines.map((line) => (
            <BomLineRow key={line.id} line={line} onChanged={lines.reload} />
          ))}
        </ul>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ import --

function ImportBom({
  projectId,
  onImported,
}: {
  projectId: number;
  onImported: () => void;
}) {
  const [content, setContent] = useState("");
  const [match, setMatch] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<BomImportResponse | null>(null);

  function onFile(event: ChangeEvent<HTMLInputElement>): void {
    const file = event.target.files?.[0];
    if (file === undefined) {
      return;
    }
    const reader = new FileReader();
    reader.onload = () => setContent(typeof reader.result === "string" ? reader.result : "");
    reader.readAsText(file);
  }

  async function submit(): Promise<void> {
    if (content.trim() === "") {
      return;
    }
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      // Idempotency-guarded on the server, but that only covers a *retry* of
      // this exact request — importing lands new lines every time it is
      // pressed, so nothing here re-imports the same file automatically.
      const response = await importBom(projectId, { content, match, client_op_id: uuid4() });
      setResult(response);
      // The lines list reloads under the still-open form, so the honest
      // count above stays on screen next to the table it describes — closing
      // immediately is what would hide the very report this step exists to
      // show.
      onImported();
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
        void submit();
      }}
    >
      <h3>Import a BOM</h3>
      <p className="muted-note">
        A CSV or tab-separated export from KiCad, Altium or CircuitMaker — the columns
        are worked out from the file. A spreadsheet (<code>.xlsx</code>, <code>.xls</code>)
        cannot be read; re-export it as CSV. Appends to the existing BOM rather than
        replacing it, so import each revision once.
      </p>
      <label className="field">
        <span>Choose a file, or paste below</span>
        <input
          type="file"
          accept=".csv,.tsv,.txt,text/csv,text/tab-separated-values,text/plain"
          onChange={onFile}
        />
      </label>
      <label className="field">
        <span>File contents</span>
        <textarea
          rows={6}
          className="mono"
          value={content}
          onChange={(event) => setContent(event.target.value)}
          placeholder="Reference,Value,Footprint,Qty,DNP,MPN..."
        />
      </label>
      <label className="check">
        <input type="checkbox" checked={match} onChange={(event) => setMatch(event.target.checked)} />
        Match exact part numbers as it lands
      </label>
      <ErrorBanner error={error} fallback="That file could not be imported." />
      <button type="submit" className="primary wide" disabled={busy || content.trim() === ""}>
        {busy ? "Importing…" : "Import"}
      </button>
      {result !== null && <ImportResult result={result} />}
    </form>
  );
}

/**
 * The honest accounting the task calls for: how many lines landed, how many
 * were matched automatically, and how many are now this project's curation
 * worklist — never just "import complete".
 *
 * **Nothing landing is the loudest case, not the quietest.** A file the reader
 * refuses — an `.xlsx` someone exported, an empty sheet — comes back with zero
 * lines, zero unmatched, and the reason in `warnings`. Read naively that is a
 * green "0 line(s) landed" with the only useful sentence collapsed behind a
 * `<details>` labelled "1 parser warning(s)", which is exactly the silent failure
 * the importer's own refusal exists to avoid. So an empty import is flagged and
 * its warnings are shown open: when there are no lines, the warnings *are* the
 * result.
 */
function ImportResult({ result }: { result: BomImportResponse }) {
  const landedNothing = result.lines.length === 0;
  return (
    <Notice
      kind={landedNothing || result.unmatched_count > 0 ? "warn" : "ok"}
      title={landedNothing ? "Nothing was imported" : "Import result"}
    >
      <p style={{ margin: 0 }}>
        {result.lines.length} line(s) landed — {result.matched_count} matched by exact part
        number, {result.dnp_count} marked DNP, and {result.unmatched_count} need a human to
        say what the part is.
      </p>
      {result.ambiguous_keys.length > 0 && (
        <p className="muted-note">
          {result.ambiguous_keys.length} part number(s) matched more than one active part and
          were left unmatched rather than guessed at:{" "}
          <span className="mono">{result.ambiguous_keys.join(", ")}</span>
        </p>
      )}
      {result.warnings.length > 0 && (
        <details open={landedNothing}>
          <summary>
            {landedNothing ? "Why" : `${result.warnings.length} parser warning(s)`}
          </summary>
          <ul>
            {result.warnings.map((warning, index) => (
              <li key={index} className="muted-note">
                {warning}
              </li>
            ))}
          </ul>
        </details>
      )}
    </Notice>
  );
}

// --------------------------------------------------------- paste-requirements --

/**
 * The paste-many box: prose in, one requirement per line — the shape an agent
 * hands back when asked "what parts do I need for this circuit". Each line
 * goes through `POST /api/requirements/suggest` in one batched call, which
 * translates it with the deterministic grammar (never a model — see
 * `app.services.requirements.parser`) and then runs the **existing** search
 * executor to say what already satisfies it. Nothing is written until a row
 * is accepted.
 *
 * A line the parser could not fully read is not an error: `residue` (the
 * words nothing accounted for) and `confidence`/`provenance` (how much of the
 * line was an exact lookup versus a model's guess) are shown on every row, not
 * folded away — the same "the honest column must not be hidden" rule
 * `ImportResult` follows for a refused file.
 */
function PasteRequirements({
  projectId,
  onAccepted,
}: {
  projectId: number;
  onAccepted: () => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [lines, setLines] = useState<SuggestionLineRead[] | null>(null);
  const [accepted, setAccepted] = useState<ReadonlySet<number>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkError, setBulkError] = useState<unknown>(null);

  async function suggest(): Promise<void> {
    const requestLines = text
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line !== "");
    if (requestLines.length === 0) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const response = await suggestRequirements({
        lines: requestLines.map((line) => ({ text: line })),
      });
      setLines(response.lines);
      setAccepted(new Set());
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  // A pasted line carries no designator — there is nowhere on this screen for
  // one — so every accepted row lands in the same "no designator" state
  // `BomLineRow` already flags for cleanup, per the "speed first" call: the
  // fast path may create an undesignated line, but it must not look silent.
  //
  // `qty_per_assembly_milli` is not optional: `BomLineEdit`'s own
  // `_create_needs_a_quantity_delete_needs_an_id` validator rejects an edit with
  // no `id` and no quantity, so every accept in this panel used to 422. It comes
  // from `required_milli` — what the *line* said (`3x 10k` says three) — and falls
  // back to one per assembly, which is the same `FALLBACK` an import writes for a
  // file with neither a quantity column nor designators.
  async function acceptLine(line: SuggestionLineRead, partId: number | null): Promise<void> {
    await updateBomLines(projectId, {
      edits: [
        { note: line.text, part_id: partId, qty_per_assembly_milli: qtyMilliFor(line) },
      ],
      client_op_id: uuid4(),
    });
    setAccepted((previous) => new Set(previous).add(line.index));
    onAccepted();
  }

  // **Writes no part, on purpose.** This used to send the rank-1 candidate's
  // `part_id` for every line, and `PUT .../bom` sets `is_match_confirmed = true`
  // whenever an edit names a part without saying otherwise — so one click marked a
  // *substitute* nobody had looked at, and parts the user does not own, as "a
  // human agreed". A rank is not a confirmation: `bom_import` refuses to set that
  // flag even for an exact MPN equality, and a ranking is a far weaker claim than
  // that. So the bulk action does the part of the job that needs no judgement —
  // landing every line with its text intact — and choosing a part stays a
  // deliberate per-row "Use this", which *is* the human agreement the column
  // exists to record.
  async function acceptAllWithoutParts(): Promise<void> {
    if (lines === null) {
      return;
    }
    const pending = lines.filter((line) => !accepted.has(line.index));
    if (pending.length === 0) {
      return;
    }
    setBulkBusy(true);
    setBulkError(null);
    try {
      await updateBomLines(projectId, {
        edits: pending.map((line) => ({
          note: line.text,
          part_id: null,
          qty_per_assembly_milli: qtyMilliFor(line),
        })),
        client_op_id: uuid4(),
      });
      setAccepted(new Set(lines.map((line) => line.index)));
      onAccepted();
    } catch (cause) {
      setBulkError(cause);
    } finally {
      setBulkBusy(false);
    }
  }

  // "You own none of these" has to be the loudest thing on the panel, not a
  // count buried in a per-row badge — it is the answer that turns into a
  // purchase, per the task's own framing.
  const ownsNothingCount =
    lines?.filter((line) => line.outcome === "order" || line.outcome === "no_match").length ?? 0;
  const allAccepted = lines !== null && accepted.size === lines.length;

  return (
    <div className="card">
      <h3>Paste requirements</h3>
      <p className="muted-note">
        One requirement per line — the kind of text an agent hands back when asked
        what a circuit needs. Each line is read, then checked against what you own;
        nothing lands on the BOM until you accept a row.
      </p>
      <label className="field">
        <span>Requirements, one per line</span>
        <textarea
          rows={6}
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={
            "3x 10k 1% 0603 resistor\n100nF 50V X7R 0603\na dual op-amp, rail-to-rail, SOIC-8"
          }
        />
      </label>
      <ErrorBanner error={error} fallback="Those lines could not be read." />
      <button
        type="button"
        className="primary wide"
        onClick={() => void suggest()}
        disabled={busy || text.trim() === ""}
      >
        {busy ? "Reading…" : "Suggest parts"}
      </button>

      {lines !== null && (
        <>
          {ownsNothingCount > 0 && (
            <Notice kind="warn" title="You own none of these">
              <p style={{ margin: 0 }}>
                {ownsNothingCount} of {lines.length} line(s) have nothing on the shelf that
                satisfies them. Their "not stocked" list below is what the catalogue offers
                instead — the list that turns into an order.
              </p>
            </Notice>
          )}
          <div className="row">
            <span className="muted-note" style={{ flex: 1 }}>
              Adding every line at once lands the text and the quantity, but no
              part — picking one is a decision, and the top-ranked candidate is
              only a ranking. Use a row's "Use this" to attach a part, which is
              what records that you agreed.
            </span>
            <button
              type="button"
              onClick={() => void acceptAllWithoutParts()}
              disabled={bulkBusy || allAccepted}
            >
              {bulkBusy ? "Adding…" : "Add all without parts"}
            </button>
          </div>
          <ErrorBanner error={bulkError} fallback="Those lines could not be added." />
          <ul className="list">
            {lines.map((line) => (
              <SuggestionRow
                key={line.index}
                line={line}
                added={accepted.has(line.index)}
                onAccept={(partId) => acceptLine(line, partId)}
              />
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

/**
 * What one accepted line demands per assembly, in milli-units.
 *
 * `required_milli` is what the line itself said — `3x 10k` says three — and null
 * when it said nothing, because the parser refuses to invent a quantity the user
 * never gave. `bom_lines.qty_per_assembly_milli` is NOT NULL, so something has to
 * be written: one per assembly, the same fallback `bom_import` uses for a file
 * with neither a quantity column nor designators.
 */
function qtyMilliFor(line: SuggestionLineRead): number {
  return line.required_milli ?? 1000;
}

/**
 * Confidence, legible without colour: a percentage plus a word for
 * `provenance` (`deterministic` / `interpreted` / `mixed` / `none`), each
 * paired with its own badge glyph — the same rule `.badge`'s three status
 * variants already follow, so this introduces no fourth channel, just a
 * fourth label on the existing ones. A guessed field is `mixed`, never
 * `deterministic`, even at high confidence: the two are different claims.
 */
function ConfidenceBadge({ requirement }: { requirement: RequirementRead }) {
  const pct = Math.round(requirement.confidence * 100);
  switch (requirement.provenance) {
    case "deterministic":
      return <span className="badge badge-good">{pct}% deterministic</span>;
    case "interpreted":
      return <span className="badge badge-info">{pct}% model-read</span>;
    case "mixed":
      return <span className="badge badge-warn">{pct}% mixed</span>;
    default:
      return <span className="badge">{pct}% unread</span>;
  }
}

function OutcomeBadge({ outcome }: { outcome: string }) {
  switch (outcome) {
    case "stocked":
      return <span className="badge badge-good">in stock</span>;
    case "order":
      return <span className="badge badge-warn">order</span>;
    case "no_match":
      return <span className="badge badge-bad">no match</span>;
    default:
      return <span className="badge">not actionable</span>;
  }
}

/**
 * One requirement, one row. Shows what was understood (filters, category,
 * quantity), what was not (`residue` — the whole signal for "a model would
 * help here", never hidden), and the ranked candidates in the same two lists
 * the wire carries: `in_stock` before `not_stocked`, because owning nothing
 * that satisfies a line is a different, more important answer than owning
 * something.
 *
 * Accepting reads a specific candidate's `part_id` off its own "Use this", or
 * `null` from the row's own "Accept without a part" — either way it is one
 * `updateBomLines` edit, same as a hand-typed line, and the row's text
 * survives in `note` even when nothing was matched.
 *
 * **"Use this" is the only thing on this screen that attaches a part**, and that
 * is why it is the only thing that may set `is_match_confirmed` (which `PUT
 * .../bom` does for any edit naming a part). The bulk button deliberately writes
 * no part at all: see `acceptAllWithoutParts`.
 */
function SuggestionRow({
  line,
  added,
  onAccept,
}: {
  line: SuggestionLineRead;
  added: boolean;
  onAccept: (partId: number | null) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const requirement = line.requirement;

  async function accept(partId: number | null): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await onAccept(partId);
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
          <span className="title" style={{ flex: 1 }}>
            {line.text}
          </span>
          <OutcomeBadge outcome={line.outcome} />
          <ConfidenceBadge requirement={requirement} />
        </div>

        <p className="sub">{line.message}</p>

        {/*
          The parser leaves `quantity` null when the line did not say one, rather
          than defaulting to 1 — but `bom_lines.qty_per_assembly_milli` is NOT
          NULL, so accepting the row has to write something. Saying which number
          it will be is the difference between a documented assumption and a BOM
          figure that appeared out of nowhere.
        */}
        {line.required_milli === null && (
          <p className="sub muted-note">
            This line does not say how many, so accepting it assumes one per
            assembly. Edit the quantity on the row afterwards if it is not.
          </p>
        )}

        {requirement.filters.length > 0 && (
          <p className="sub">
            Understood as:{" "}
            <span className="mono">
              {requirement.filters.map((filter) => `${filter.template}=${filter.value}`).join(", ")}
            </span>
            {requirement.category !== null && <> · {requirement.category}</>}
            {requirement.quantity !== null && <> · qty {requirement.quantity}</>}
          </p>
        )}

        {requirement.residue.length > 0 && (
          <p className="sub">
            <span className="badge badge-warn">not understood</span>{" "}
            <span className="mono">{requirement.residue.join(" ")}</span>
          </p>
        )}

        {requirement.rejections.length > 0 && (
          <p className="sub muted-note">
            {requirement.rejections.map((rejection) => rejection.message).join("; ")}
          </p>
        )}

        {line.in_stock.length > 0 && (
          <CandidateList
            title="In stock"
            candidates={line.in_stock}
            busy={busy || added}
            onUse={(partId) => void accept(partId)}
          />
        )}
        {line.not_stocked.length > 0 && (
          <CandidateList
            title="Not stocked — would need to be ordered"
            candidates={line.not_stocked}
            busy={busy || added}
            onUse={(partId) => void accept(partId)}
          />
        )}

        <ErrorBanner error={error} fallback="That line could not be added." />

        <div className="row">
          {added ? (
            <span className="badge badge-good">added to BOM</span>
          ) : (
            <button type="button" onClick={() => void accept(null)} disabled={busy}>
              Accept without a part
            </button>
          )}
        </div>
      </div>
    </li>
  );
}

function CandidateList({
  title,
  candidates,
  busy,
  onUse,
}: {
  title: string;
  candidates: PartCandidateRead[];
  busy: boolean;
  onUse: (partId: number) => void;
}) {
  return (
    <div className="sub">
      <span className="dim">{title}:</span>
      <ul className="list">
        {candidates.map((candidate) => (
          <li key={candidate.part_id}>
            <div className="list-item">
              <div className="row">
                <div style={{ flex: 1, minWidth: 0 }}>
                  <Link to={`/parts/${candidate.part_id}`} className="title">
                    {candidate.name}
                  </Link>{" "}
                  {candidate.is_substitute && (
                    <span className="badge badge-info">substitute</span>
                  )}
                  {candidate.is_stub && <span className="badge badge-warn">stub</span>}
                  <div className="sub">
                    {candidate.mpn !== null && <span className="mono">{candidate.mpn}</span>}
                    {candidate.mpn !== null && " · "}
                    {formatQty(candidate.qty_milli)} on hand
                  </div>
                  {candidate.reasons.length > 0 && (
                    <ul>
                      {candidate.reasons.map((reason, index) => (
                        <li key={index} className="muted-note">
                          {reason.explanation}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                <button type="button" onClick={() => onUse(candidate.part_id)} disabled={busy}>
                  Use this
                </button>
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------- add-line --

/**
 * "I can't add parts to a project" — a hand-typed line, landing exactly the
 * way an imported-but-unmatched one does: `part_id` is optional here on
 * purpose, because an unmatched line is a legal, ordinary row (`stateOf`
 * already treats it that way), not something this form has to refuse.
 *
 * Goes through the same `PUT .../bom` batch `BomLineRow`'s edits do — one
 * write path for the whole screen, per `BomLineEdit`'s server-side docstring.
 */
function AddBomLine({ projectId, onAdded }: { projectId: number; onAdded: () => void }) {
  const [designators, setDesignators] = useState("");
  const [qtyMilli, setQtyMilli] = useState(MILLI);
  const [partId, setPartId] = useState<number | null>(null);
  const [partLabel, setPartLabel] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // The server refuses a non-positive quantity outright (422,
  // `non_positive_quantity`), and it is right to — a zero-quantity line
  // records nothing. Computed once so the disabled button and the inline
  // hint below always agree on why.
  const qtyInvalid = qtyMilli <= 0;

  async function submit(): Promise<void> {
    if (qtyInvalid) {
      // Belt and suspenders: the submit button is disabled for the same
      // reason, but a stray Enter-key submit must not vanish silently either.
      setError(new Error("Enter a quantity above zero — a zero-quantity line records nothing."));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await updateBomLines(projectId, {
        edits: [
          {
            qty_per_assembly_milli: qtyMilli,
            designators: designators.trim() === "" ? null : designators.trim(),
            part_id: partId,
          },
        ],
        client_op_id: uuid4(),
      });
      onAdded();
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
        void submit();
      }}
    >
      <h3>Add a line by hand</h3>
      <p className="muted-note">
        Leaving the part unset is normal — the same "needs a human to say what it is"
        state an import leaves an unmatched line in.
      </p>
      <label className="field">
        <span>Designators</span>
        <input
          value={designators}
          onChange={(event) => setDesignators(event.target.value)}
          placeholder="R7, or R7,R8"
        />
      </label>
      <label className="field">
        <span>Quantity per assembly</span>
        <input
          type="number"
          min={0}
          value={qtyMilli / MILLI}
          onChange={(event) =>
            setQtyMilli(Math.max(0, Number(event.target.value) || 0) * MILLI)
          }
        />
      </label>
      {qtyInvalid && (
        <p className="muted-note">
          Quantity must be greater than zero — that is why "Add line" below is disabled.
        </p>
      )}
      <div className="row">
        <span style={{ flex: 1 }}>
          {partId === null ? (
            <span className="dim">No part matched yet</span>
          ) : (
            <>
              Part: <Link to={`/parts/${partId}`}>{partLabel ?? `#${partId}`}</Link>
            </>
          )}
        </span>
        <button type="button" onClick={() => setPicking(!picking)}>
          {picking ? "Cancel" : partId === null ? "Pick a part" : "Change part"}
        </button>
        {partId !== null && (
          <button
            type="button"
            onClick={() => {
              setPartId(null);
              setPartLabel(null);
            }}
          >
            Clear part
          </button>
        )}
      </div>
      {picking && (
        <MatchPicker
          seedText={designators}
          onPick={(id, label) => {
            setPartId(id);
            setPartLabel(label);
            setPicking(false);
          }}
          busy={busy}
        />
      )}
      <ErrorBanner error={error} fallback="That line could not be added." />
      <button type="submit" className="primary wide" disabled={busy || qtyInvalid}>
        {busy ? "Adding…" : "Add line"}
      </button>
    </form>
  );
}

// -------------------------------------------------------------------- rows --

function StateBadge({ state }: { state: LineState }) {
  switch (state) {
    case "dnp":
      return <span className="badge">DNP</span>;
    case "unmatched":
      return <span className="badge badge-warn">unmatched</span>;
    case "unconfirmed":
      return <span className="badge badge-info">auto-matched</span>;
    case "matched":
      return <span className="badge badge-good">matched</span>;
  }
}

function BomLineRow({ line, onChanged }: { line: BomLineRead; onChanged: () => void }) {
  const state = stateOf(line);
  const [picking, setPicking] = useState(false);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function apply(edit: Omit<BomLineEdit, "id">): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      // Idempotency-guarded: the default for a bare `part_id` (confirm on
      // set, clear confirmation on null) is request-shape-dependent, so a
      // retried tap and a genuinely repeated one must be distinguishable.
      await updateBomLines(line.project_id, {
        edits: [{ id: line.id, ...edit }],
        client_op_id: uuid4(),
      });
      setPicking(false);
      setEditing(false);
      onChanged();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <li>
      <div className={`list-item${state === "dnp" ? " dim" : ""}`}>
        <div className="row">
          <span className="title" style={{ flex: 1 }}>
            {line.designators ?? `line ${line.line_no}`}
            {line.ref_value !== null && <span className="dim mono"> — {line.ref_value}</span>}
          </span>
          {line.designators === null && (
            // Speed-first intake (a paste, or a hand-added line) is allowed to
            // land with no designator — but it still needs desk cleanup later,
            // so it gets the same glyph-plus-word-plus-colour treatment
            // `unmatched` does, not a silent gap in the row.
            <span className="badge badge-warn">no designator</span>
          )}
          <StateBadge state={state} />
        </div>
        <div className="sub">
          {line.mpn_raw !== null && <span className="mono">{line.mpn_raw}</span>}
          {line.mpn_raw !== null && line.manufacturer_raw !== null && " · "}
          {line.manufacturer_raw}
          {line.footprint !== null && ` · ${line.footprint}`}
        </div>
        <div className="sub">
          {formatQty(line.qty_per_assembly_milli)} per assembly
          {line.part_id !== null && (
            <>
              {" · "}
              <Link to={`/parts/${line.part_id}`}>part #{line.part_id}</Link>
            </>
          )}
        </div>
        {line.note !== null && <div className="sub">{line.note}</div>}

        <div className="row">
          {state === "unmatched" && (
            <button type="button" onClick={() => setPicking(!picking)} disabled={busy}>
              {picking ? "Cancel" : "Match"}
            </button>
          )}
          {state === "unconfirmed" && (
            <>
              <button
                type="button"
                className="primary"
                onClick={() => void apply({ is_match_confirmed: true })}
                disabled={busy}
              >
                Confirm match
              </button>
              <button type="button" onClick={() => setPicking(!picking)} disabled={busy}>
                {picking ? "Cancel" : "Change"}
              </button>
            </>
          )}
          {state === "matched" && (
            <button type="button" onClick={() => setPicking(!picking)} disabled={busy}>
              {picking ? "Cancel" : "Change match"}
            </button>
          )}
          {state !== "dnp" && (
            <button type="button" onClick={() => void apply({ is_dnp: true })} disabled={busy}>
              Mark DNP
            </button>
          )}
          {state === "dnp" && (
            <button type="button" onClick={() => void apply({ is_dnp: false })} disabled={busy}>
              Not DNP after all
            </button>
          )}
          <button type="button" onClick={() => setEditing(!editing)} disabled={busy}>
            {editing ? "Cancel" : "Edit"}
          </button>
          <button
            type="button"
            className="danger"
            onClick={() => void apply({ delete: true })}
            disabled={busy}
          >
            Remove line
          </button>
        </div>

        <ErrorBanner error={error} fallback="That edit was not saved." />

        {editing && (
          <EditBomLineFields line={line} busy={busy} onSave={(edit) => void apply(edit)} />
        )}

        {picking && (
          <MatchPicker
            seedText={line.mpn_raw ?? line.ref_value ?? ""}
            onPick={(partId) => void apply({ part_id: partId })}
            onClear={line.part_id === null ? undefined : () => void apply({ part_id: null })}
            busy={busy}
          />
        )}
      </div>
    </li>
  );
}

/**
 * Designators, ref value and per-assembly quantity — the fields `bom_import`
 * fills from a CSV and this is the only other way to correct. Part matching
 * stays `MatchPicker`'s job, not this form's: mixing "pick a part" into a
 * text-field form would duplicate the search UI for no benefit, when the row
 * already has a dedicated Match/Change-match button right beside this one.
 */
function EditBomLineFields({
  line,
  busy,
  onSave,
}: {
  line: BomLineRead;
  busy: boolean;
  onSave: (edit: Omit<BomLineEdit, "id">) => void;
}) {
  const [designators, setDesignators] = useState(line.designators ?? "");
  const [refValue, setRefValue] = useState(line.ref_value ?? "");
  const [qtyMilli, setQtyMilli] = useState(line.qty_per_assembly_milli);
  // Pre-filled from an existing line, so this can only go invalid if the
  // field is cleared while editing — silently disabling "Save" then would be
  // exactly as confusing as the same field was on the add form.
  const qtyInvalid = qtyMilli <= 0;

  return (
    <div className="card" style={{ marginTop: "0.4rem" }}>
      <label className="field">
        <span>Designators</span>
        <input value={designators} onChange={(event) => setDesignators(event.target.value)} />
      </label>
      <label className="field">
        <span>Value</span>
        <input value={refValue} onChange={(event) => setRefValue(event.target.value)} />
      </label>
      <label className="field">
        <span>Quantity per assembly</span>
        <input
          type="number"
          min={0}
          value={qtyMilli / MILLI}
          onChange={(event) =>
            setQtyMilli(Math.max(0, Number(event.target.value) || 0) * MILLI)
          }
        />
      </label>
      {qtyInvalid && (
        <p className="muted-note">
          Quantity must be greater than zero — that is why "Save" below is disabled.
        </p>
      )}
      <button
        type="button"
        className="primary wide"
        disabled={busy || qtyInvalid}
        onClick={() =>
          onSave({
            designators: designators.trim() === "" ? null : designators.trim(),
            ref_value: refValue.trim() === "" ? null : refValue.trim(),
            qty_per_assembly_milli: qtyMilli,
          })
        }
      >
        {busy ? "Saving…" : "Save"}
      </button>
    </div>
  );
}

/**
 * Match **one existing line** to a part, seeded with the text already on it.
 *
 * Deliberately narrow after ADR 0007, and this is the whole of what survives of
 * the old "pick a part" flow. A BOM line that arrived from an import already
 * carries the string to search with (`mpn_raw`, falling back to `ref_value`), so
 * the useful gesture here is *confirm this row*: one seeded query, one tap. That
 * is not the same task as **choosing** parts, which is a browsing session and
 * belongs on the real search screen with its facets, category rail and
 * stock-per-row — "it's still not the same view as the search tab" was a correct
 * complaint about using this box for that job. Choosing is now the cart's, fed
 * from the real search screen; this stays only as the seeded confirm-a-row
 * convenience, and both places on this screen that offer it say which is which.
 *
 * Every result row renders through `PartResultRow`, the same component the search
 * screen's list uses, so a part reads identically in both places.
 *
 * **Enter searches.** This box sits inside `AddBomLine`'s `<form>`, so a bare
 * `<input>` made Enter submit the *form* — "pressing enter on the pick a part for
 * project BOM adds the line instead of searching", as reported. The key is
 * therefore handled on the field itself rather than by moving buttons around:
 * every button here is already `type="button"`, so the submission came from the
 * implicit-submit rule, which only the keypress can intercept.
 */
function MatchPicker({
  seedText,
  onPick,
  onClear,
  busy,
}: {
  seedText: string;
  // `partLabel` lets a caller show what was picked without a second fetch —
  // `AddBomLine` has nothing else to render a name from until the new line
  // comes back from `listBomLines`.
  onPick: (partId: number, partLabel: string) => void;
  onClear?: (() => void) | undefined;
  busy: boolean;
}) {
  const [text, setText] = useState(seedText);
  const [results, setResults] = useState<PartSummary[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function search(): Promise<void> {
    setSearching(true);
    setError(null);
    try {
      const response = await searchParts({ text: text.trim() === "" ? null : text, limit: 8 });
      setResults(response.results);
    } catch (cause) {
      setError(cause);
    } finally {
      setSearching(false);
    }
  }

  return (
    <div className="card" style={{ marginTop: "0.4rem" }}>
      <div className="row">
        <input
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== "Enter") {
              return;
            }
            // `preventDefault` stops the implicit submit; `stopPropagation` stops
            // the enclosing form's `onSubmit` from ever being consulted. Both,
            // because the two mechanisms are independent and only the pair of
            // them makes Enter mean "search" wherever this box is embedded.
            event.preventDefault();
            event.stopPropagation();
            if (!searching) {
              void search();
            }
          }}
          placeholder="name, MPN, keywords"
          enterKeyHint="search"
          type="search"
          autoComplete="off"
          aria-label="Search parts to match"
          style={{ flex: 1 }}
        />
        <button type="button" onClick={() => void search()} disabled={searching}>
          {searching ? "Searching…" : "Search"}
        </button>
      </div>
      <ErrorBanner error={error} />
      {results !== null && (
        <ul className="list">
          {results.length === 0 && <li className="dim">No parts matched.</li>}
          {results.map((part) => (
            <li key={part.id} className="list-item">
              <div className="row">
                <div style={{ flex: 1, minWidth: 0 }}>
                  <PartResultRow part={part} />
                </div>
                <button type="button" onClick={() => onPick(part.id, part.name)} disabled={busy}>
                  Use this
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {onClear !== undefined && (
        <button type="button" onClick={onClear} disabled={busy}>
          Clear match
        </button>
      )}
    </div>
  );
}
