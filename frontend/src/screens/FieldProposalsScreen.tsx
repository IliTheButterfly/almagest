/**
 * Proposed fields — what a datasheet stated and this install has no field for.
 *
 * The review surface for `parameter_field_proposals`. Everything on it exists to
 * make one decision safe, and the decision is *not* "is this a useful field": it
 * is **"is this a field we already have under another name?"**
 *
 * That is the failure this screen is arranged around. A `parameter_template` is
 * shared by every part in its category, so a second template for one concept —
 * `Rds(on)` beside `on_resistance` — fragments parametric search silently: the
 * filter is a join on `template_id`, so a part with 45 mΩ recorded under one name
 * reads as having no on-resistance under the other, and the substitution rule
 * decides on the half it can see. Nothing reports it. So:
 *
 * - **the nearest existing field is the first thing in a row**, with how it was
 *   matched, and "Merge into it" is offered before the create form. The list comes
 *   back ordered by that likeness, so the rows that must not be waved through are
 *   at the top;
 * - **every sighting shows its verbatim quote**, and how many documents produced
 *   one. A phrase from five unrelated sheets is a real field; the same phrase from
 *   one sheet is that manufacturer's private vocabulary. A proposal nobody can
 *   trace to a line of a datasheet cannot be reviewed at all;
 * - **the create form is the ordinary authoring form**, and pressing Create calls
 *   `POST /api/parameter-fields` — the route that already existed and the only
 *   thing that writes a `parameter_template`. This screen then records the outcome
 *   against the proposal. Two calls on purpose: a single "accept" endpoint that
 *   created the field would be a second door onto that write, which is exactly how
 *   the never-auto-accept rule stops being true. The intake queue keeps the same
 *   shape by routing a chosen identity through its existing `resolve` route;
 * - **`substitution_direction` has no suggested value and must be chosen.** It is
 *   what makes a 50 V part an acceptable stand-in for a 25 V one and not the
 *   reverse; the model is not asked for it and the form does not default it,
 *   because a defaulted one produces a substitution search that is wrong by
 *   construction while looking like it works.
 *
 * Reached from the review queue and from the part-types screen, not from the nav
 * strip — the strip is already too long for a phone, and this is an errand you go
 * on after seeing something rather than a place you start.
 */

import { useState } from "react";
import { Link } from "react-router-dom";

import { Empty, ErrorBanner, Loading, Notice } from "../components/Feedback";
import {
  createParameterField,
  decideFieldProposal,
  listBaseUnits,
  listFieldProposals,
  type BaseUnitOption,
  type FieldProposalRead,
  type SubstitutionDirection,
} from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";

/** How the server matched a proposal to an existing field, said in words.
 *
 * The wording carries the *weight* each rung deserves, because they are not
 * equally strong: an identical name means the field is simply already there, while
 * shared words mean "look at this yourself". Flattening them into one "possible
 * duplicate" badge would make the strong case unremarkable.
 */
const MATCH_LABELS: Record<string, string> = {
  exact_name: "the same name — this field already exists",
  display_name: "the same display name",
  synonym: "a known other name for the same quantity",
  tokens: "similar wording and a compatible unit",
};

function matchLabel(match: string): string {
  return MATCH_LABELS[match] ?? match;
}

const DIRECTIONS: readonly { value: SubstitutionDirection; label: string }[] = [
  { value: "higher_ok", label: "higher is fine — a rating, like voltage" },
  { value: "lower_ok", label: "lower is fine — like tolerance" },
  { value: "range_overlap", label: "must overlap — a value, like capacitance" },
  { value: "exact", label: "must match exactly — like a package" },
];

export function FieldProposalsScreen() {
  const proposals = useAsync(() => listFieldProposals("pending"), []);
  const units = useAsync<BaseUnitOption[]>(() => listBaseUnits(), []);
  const [openId, setOpenId] = useState<number | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [done, setDone] = useState<string | null>(null);

  async function act(id: number, run: () => Promise<string>): Promise<void> {
    setBusyId(id);
    setActionError(null);
    try {
      setDone(await run());
      setOpenId(null);
      proposals.reload();
    } catch (cause) {
      setActionError(cause);
    } finally {
      setBusyId(null);
    }
  }

  function merge(proposal: FieldProposalRead): void {
    const nearest = proposal.nearest;
    if (nearest === null) {
      return;
    }
    void act(proposal.id, async () => {
      // No field is created: the proposal is recorded as having become the field
      // that was already there. This is the whole reason the decision route takes
      // an existing `field_id` rather than a definition.
      await decideFieldProposal(proposal.id, {
        status: "accepted",
        field_id: nearest.template_id,
        note: `merged into ${nearest.name}`,
      });
      return `“${proposal.name}” recorded as ${nearest.name}. Nothing new was created.`;
    });
  }

  function dismiss(proposal: FieldProposalRead): void {
    void act(proposal.id, async () => {
      await decideFieldProposal(proposal.id, { status: "dismissed" });
      // Kept rather than deleted, so the next pass over the same datasheet does
      // not put it back at the top of the list as though it were new.
      return `“${proposal.name}” dismissed. It will not be proposed again.`;
    });
  }

  function create(proposal: FieldProposalRead, form: CreateFields): void {
    void act(proposal.id, async () => {
      // Two calls, deliberately: the field is created through the route that
      // already existed and validates everything, and only then is the proposal
      // marked as what it became.
      const created = await createParameterField({
        name: form.name,
        display_name: form.displayName,
        value_type: form.valueType,
        base_unit: form.valueType === "numeric" ? form.baseUnit : null,
        substitution_direction: form.direction,
        applies_to_category: proposal.suggested_category,
        // A list field is authored with its options in one request, and a
        // proposal carries none — so an enum proposal is created with a single
        // placeholder option the author then edits. An enum template with no
        // choices matches nothing while looking like a working filter, which the
        // API refuses outright.
        choices:
          form.valueType === "enum"
            ? [{ key: "unspecified", label: "Unspecified", aliases: [], sort_order: 10 }]
            : [],
        // If the name is taken, the honest answer is that the field exists — so
        // fail rather than quietly creating `<category>.<name>`.
        on_name_conflict: "fail",
      });
      await decideFieldProposal(proposal.id, {
        status: "accepted",
        field_id: created.field.id,
      });
      return created.reused
        ? `“${form.name}” already existed and was reused.`
        : `“${form.name}” created. Fill in its values from a part's page.`;
    });
  }

  if (proposals.error !== null) {
    return (
      <ErrorBanner error={proposals.error} fallback="The proposed fields could not be loaded." />
    );
  }
  if (proposals.data === null) {
    return <Loading what="the proposed fields" />;
  }

  return (
    <div className="stack">
      <div className="card">
        <h1>Proposed fields</h1>
        <p className="muted-note" style={{ margin: 0 }}>
          Parameters a datasheet stated that there is no field for. Nothing here has been
          created — creating a field is this screen's button, never the reader's, because a
          second field for something you already have makes every search for it quietly
          incomplete.
        </p>
        <p className="muted-note" style={{ margin: "0.4rem 0 0" }}>
          Check the nearest existing field first. <Link to="/part-types">Part types</Link> is
          where fields are edited once they exist.
        </p>
      </div>

      {done !== null && <Notice kind="ok">{done}</Notice>}
      <ErrorBanner error={actionError} fallback="That could not be recorded." />

      {proposals.data.length === 0 ? (
        <Empty>No fields have been proposed. Nothing is waiting on you.</Empty>
      ) : (
        proposals.data.map((proposal) => (
          <ProposalCard
            key={proposal.id}
            proposal={proposal}
            units={units.data ?? []}
            open={openId === proposal.id}
            busy={busyId === proposal.id}
            onToggle={() => setOpenId(openId === proposal.id ? null : proposal.id)}
            onMerge={() => merge(proposal)}
            onDismiss={() => dismiss(proposal)}
            onCreate={(form) => create(proposal, form)}
          />
        ))
      )}
    </div>
  );
}

interface CreateFields {
  readonly name: string;
  readonly displayName: string;
  readonly valueType: "numeric" | "enum";
  readonly baseUnit: string | null;
  readonly direction: SubstitutionDirection;
}

function ProposalCard({
  proposal,
  units,
  open,
  busy,
  onToggle,
  onMerge,
  onDismiss,
  onCreate,
}: {
  proposal: FieldProposalRead;
  units: readonly BaseUnitOption[];
  open: boolean;
  busy: boolean;
  onToggle: () => void;
  onMerge: () => void;
  onDismiss: () => void;
  onCreate: (form: CreateFields) => void;
}) {
  const nearest = proposal.nearest;
  return (
    <div className="card stack">
      <div className="row">
        <h2 style={{ margin: 0 }}>{proposal.display_name ?? proposal.name}</h2>
        <span className="badge mono">{proposal.name}</span>
        <span className="spacer" />
        <span className="badge" title="How many documents stated this field">
          {proposal.seen_count === 1
            ? "1 datasheet"
            : `${String(proposal.seen_count)} datasheets`}
        </span>
      </div>

      {nearest === null ? (
        <p className="muted-note" style={{ margin: 0 }}>
          Nothing you already have resembles this.
        </p>
      ) : (
        <Notice kind="warn" title={`You may already have this: ${nearest.name}`}>
          <p style={{ margin: 0 }}>
            Matched on {matchLabel(nearest.match)}
            {nearest.base_unit === null ? "" : `, measured in ${nearest.base_unit}`}. Creating a
            second field for one quantity makes every search for it incomplete, with nothing
            to say so.
          </p>
          <div className="row" style={{ marginTop: "0.5rem" }}>
            <button type="button" className="primary" onClick={onMerge} disabled={busy}>
              {busy ? "Saving…" : `Merge into ${nearest.name}`}
            </button>
          </div>
        </Notice>
      )}

      <ul className="list">
        {(proposal.evidence ?? []).map((sighting) => (
          <li key={sighting.id} className="list-item">
            <blockquote className="mono" style={{ margin: 0 }}>
              {sighting.quote}
            </blockquote>
            <p className="muted-note" style={{ margin: 0 }}>
              {sighting.stated_value === null ? "no value quoted" : sighting.stated_value}
              {sighting.page === null ? "" : ` · page ${String(sighting.page)}`}
              {sighting.model === null ? "" : ` · ${sighting.model}`}
            </p>
            {sighting.note !== null && <p className="muted-note">{sighting.note}</p>}
          </li>
        ))}
      </ul>

      <div className="row">
        <button type="button" onClick={onToggle} disabled={busy}>
          {open ? "Cancel" : "Create this field…"}
        </button>
        <button type="button" onClick={onDismiss} disabled={busy}>
          Not a field
        </button>
      </div>

      {open && <CreateForm proposal={proposal} units={units} busy={busy} onCreate={onCreate} />}
    </div>
  );
}

/**
 * The ordinary authoring form, pre-filled from the proposal.
 *
 * Pre-filled, never pre-decided. `substitution_direction` starts unset and the
 * submit button stays disabled until it is chosen: it is the one field on here
 * whose wrong value is invisible, since a substitution search with the wrong
 * direction returns plausible parts rather than an error.
 */
function CreateForm({
  proposal,
  units,
  busy,
  onCreate,
}: {
  proposal: FieldProposalRead;
  units: readonly BaseUnitOption[];
  busy: boolean;
  onCreate: (form: CreateFields) => void;
}) {
  const [name, setName] = useState(proposal.name);
  const [displayName, setDisplayName] = useState(proposal.display_name ?? proposal.name);
  const [valueType, setValueType] = useState<"numeric" | "enum">(
    proposal.suggested_value_type === "enum" ? "enum" : "numeric",
  );
  const [baseUnit, setBaseUnit] = useState(proposal.suggested_base_unit ?? "");
  const [direction, setDirection] = useState<SubstitutionDirection | "">("");

  const ready = name.trim() !== "" && direction !== "" && (valueType !== "numeric" || baseUnit);

  return (
    <form
      className="stack"
      onSubmit={(event) => {
        event.preventDefault();
        if (direction === "") {
          return;
        }
        onCreate({
          name: name.trim(),
          displayName: displayName.trim() || name.trim(),
          valueType,
          baseUnit: baseUnit === "" ? null : baseUnit,
          direction,
        });
      }}
    >
      <label>
        Field key
        <input value={name} onChange={(event) => setName(event.target.value)} />
      </label>
      <label>
        Shown as
        <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
      </label>
      <label>
        Kind
        <select
          value={valueType}
          onChange={(event) => setValueType(event.target.value === "enum" ? "enum" : "numeric")}
        >
          <option value="numeric">A number with a unit</option>
          <option value="enum">A list of options</option>
        </select>
      </label>
      {valueType === "numeric" && (
        <label>
          Quantity
          <select value={baseUnit} onChange={(event) => setBaseUnit(event.target.value)}>
            <option value="">Choose the quantity…</option>
            {units.map((unit) => (
              <option key={unit.name} value={unit.name}>
                {unit.name} ({unit.symbol})
              </option>
            ))}
          </select>
        </label>
      )}
      <label>
        When looking for a substitute
        <select
          value={direction}
          onChange={(event) => setDirection(event.target.value as SubstitutionDirection | "")}
        >
          <option value="">Choose — there is no safe default…</option>
          {DIRECTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      {proposal.suggested_category !== null && (
        <p className="muted-note" style={{ margin: 0 }}>
          Offered on <span className="mono">{proposal.suggested_category}</span> and everything
          under it.
        </p>
      )}
      <div className="row">
        <button type="submit" className="primary" disabled={busy || !ready}>
          {busy ? "Creating…" : "Create field"}
        </button>
      </div>
    </form>
  );
}
