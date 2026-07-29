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
import { Link, useParams, useSearchParams } from "react-router-dom";

import { ErrorBanner, Empty, Loading, Notice } from "../components/Feedback";
import {
  getProject,
  importBom,
  listBomLines,
  searchParts,
  updateBomLines,
  type BomLineEdit,
  type BomLineRead,
  type BomImportResponse,
  type PartSummary,
  type ProjectRead,
} from "../lib/api/client";
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
  const [importing, setImporting] = useState(false);
  const [adding, setAdding] = useState(false);
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
        <div className="row">
          <Link to={`/projects/${project.id}`}>← {project.name}</Link>
        </div>
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
          <button
            type="button"
            onClick={() => {
              setAdding(!adding);
              setImporting(false);
            }}
          >
            {adding ? "Cancel" : "Add a line"}
          </button>
          <button
            type="button"
            className="primary"
            onClick={() => {
              setImporting(!importing);
              setAdding(false);
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
        A KiCad-style CSV/TSV export. Appends to the existing BOM rather than replacing
        it, so import each revision once.
      </p>
      <label className="field">
        <span>Choose a file, or paste below</span>
        <input type="file" accept=".csv,.tsv,text/csv,text/tab-separated-values,text/plain" onChange={onFile} />
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
 */
function ImportResult({ result }: { result: BomImportResponse }) {
  return (
    <Notice kind={result.unmatched_count > 0 ? "warn" : "ok"} title="Import result">
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
        <details>
          <summary>{result.warnings.length} parser warning(s)</summary>
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
 * The one-tap path the design problem calls for: search by the text already
 * on the line (its raw MPN, falling back to the ref value) and confirm a
 * result with a single tap. It searches the whole catalogue, not "parts like
 * this one" — a BOM's `mpn_raw` is exactly the free text this box is for.
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
          placeholder="name, MPN, keywords"
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
                  <div className="title">{part.name}</div>
                  <div className="sub">
                    {part.mpn !== null && <span className="mono">{part.mpn}</span>}
                    {part.mpn !== null && part.description !== null && " · "}
                    {part.description}
                  </div>
                  {part.is_stub && <span className="badge badge-warn">stub</span>}
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
