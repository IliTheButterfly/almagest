/**
 * `/part-types` — author the taxonomy and the fields you filter parts by.
 *
 * Until the routes behind this screen existed, every category, every kind and
 * every filterable field came out of a migration: "capacitors also have an ESR"
 * was a code change, and the filter panel could only ever offer what a developer
 * had shipped.
 *
 * **The screen's first job is to stop the user guessing which of two objects they
 * want**, because "part type" is two things in this schema and only one of them
 * carries fields:
 *
 * - a **category** is where a part sits in the taxonomy, and it is what fields
 *   hang off — so it is the one to reach for when the answer is "capacitors need
 *   an ESR". Its rail is the whole left column, and the fields of whatever is
 *   selected are the whole right one.
 * - a **kind** is what something fundamentally *is* — a component is not a tool
 *   is not a consumable. It carries no fields at all, and it is filed at the
 *   bottom, behind its own heading that says so.
 *
 * The detail column always states whether a field is authored *here* or
 * **inherited** from an ancestor, because a field authored on Capacitors is
 * offered on Capacitors > Ceramic — the node parts are really filed under — and
 * editing an inherited one changes it for every sibling category too. That
 * inheritance is computed from the live tree, so reparenting a category changes
 * what it offers immediately.
 *
 * Nothing here navigates away: the selection is `?category=`, and every form
 * opens in place. Same shape as the storage workspace, for the same reason —
 * choosing a category and reading its fields is one task, not two screens.
 */

import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { FieldForm } from "../components/FieldForm";
import { Empty, ErrorBanner, Loading, Notice } from "../components/Feedback";
import {
  addParameterChoice,
  createParameterField,
  createPartCategory,
  createPartKind,
  createParameterQuantity,
  deleteParameterChoice,
  deleteParameterField,
  deleteParameterQuantity,
  listBaseUnits,
  listParameterFields,
  listPartCategories,
  listParameterQuantities,
  listPartKinds,
  movePartCategory,
  updateParameterField,
  type BaseUnitOption,
  type CategoryNode,
  type NameConflictPolicy,
  type ParameterFieldRead,
  type PartKindRead,
  type QuantityRead,
} from "../lib/api/client";
import { existingFieldOf, problemOf } from "../lib/api/errors";
import { useAsync } from "../lib/hooks/useAsync";
import { useMediaQuery } from "../lib/hooks/useMediaQuery";
import { slugify } from "../lib/containers/typeDraft";
import {
  BLANK_FIELD_DRAFT,
  anchorForReason,
  draftFromField,
  frozenColumns,
  isEmptyUpdate,
  fieldKey,
  splitAliases,
  toFieldCreateRequest,
  toFieldUpdateRequest,
  type DraftAnchor,
  type FieldDraft,
} from "../lib/parts/fieldDraft";
import { uuid4 } from "../lib/scan/session";

/** The same breakpoint `.search-layout` uses, whose two columns this reuses. */
const WIDE = "(min-width: 52rem)";

export function PartTypesScreen() {
  const [params, setParams] = useSearchParams();
  const selected = params.get("category") ?? "";
  const wide = useMediaQuery(WIDE);

  const categories = useAsync<CategoryNode[]>(() => listPartCategories(), []);
  const fields = useAsync<ParameterFieldRead[]>(() => listParameterFields(selected), [selected]);
  const kinds = useAsync<PartKindRead[]>(() => listPartKinds(), []);
  // The field form's unit select is built from `base-units`; this is the same set
  // with the custom ones marked and counted, which is what the Units panel needs
  // and what a select does not. Both are reloaded when a unit is defined, or the
  // new unit is missing from the form that sent the user to define it.
  const units = useAsync<BaseUnitOption[]>(() => listBaseUnits(), []);
  const quantities = useAsync<QuantityRead[]>(() => listParameterQuantities(), []);

  function select(slug: string): void {
    const next = new URLSearchParams(params);
    if (slug === "") {
      next.delete("category");
    } else {
      next.set("category", slug);
    }
    setParams(next, { replace: true });
  }

  const category = (categories.data ?? []).find((node) => node.slug === selected) ?? null;

  const rail = (
    <>
      <div className="card">
        <h3>Categories</h3>
        <p className="muted-note" style={{ margin: 0 }}>
          Where a part sits, and the only one of the two that carries fields. A category can go
          inside another — and the deeper one is offered everything authored above it.
        </p>
        <ul className="rail" aria-label="Part categories">
          <li>
            <button type="button" aria-pressed={selected === ""} onClick={() => select("")}>
              <span className="tick" aria-hidden="true" />
              <span>Every part</span>
            </button>
          </li>
          {(categories.data ?? []).map((node) => (
            <li key={node.slug}>
              <button
                type="button"
                aria-pressed={selected === node.slug}
                style={{ paddingLeft: `${0.5 + node.depth * 0.85}rem` }}
                onClick={() => select(selected === node.slug ? "" : node.slug)}
              >
                <span className="tick" aria-hidden="true" />
                <span>{node.name}</span>
                <span className="count" aria-label={`${node.part_count} parts`}>
                  {node.part_count}
                </span>
              </button>
            </li>
          ))}
        </ul>
        {categories.loading && <Loading what="categories" />}
        <ErrorBanner error={categories.error} fallback="The categories could not be loaded." />
        <NewCategory
          categories={categories.data ?? []}
          selected={category}
          onCreated={(slug) => {
            categories.reload();
            select(slug);
          }}
        />
        {category !== null && (
          <MoveCategory
            category={category}
            categories={categories.data ?? []}
            onMoved={() => categories.reload()}
          />
        )}
      </div>
      <Kinds state={kinds} />
      <Units
        quantities={quantities.data}
        loading={quantities.loading}
        error={quantities.error}
        onChanged={() => {
          quantities.reload();
          units.reload();
        }}
      />
    </>
  );

  return (
    <div className="stack">
      <div className="card">
        <h1>Part types</h1>
        <p className="muted-note" style={{ margin: 0 }}>
          Two different things share that name, and only one of them owns fields. A{" "}
          <strong>category</strong> is where a part sits in the taxonomy — Passives &gt;
          Capacitors &gt; Ceramic — and it is what a filterable field hangs off. A{" "}
          <strong>kind</strong> is what something fundamentally is: a component is not a tool.
          Pick a category on the left to see and author the fields it offers.
        </p>
      </div>

      {/* The search screen's two-column grid, reused rather than reinvented: the
          rail-then-detail shape is the same, and a second near-identical set of
          breakpoints would only be free to drift from this one. */}
      <div className="search-layout">
        {wide ? (
          <aside className="search-side stack">{rail}</aside>
        ) : (
          <details className="card">
            <summary>
              Categories and kinds
              {category === null ? "" : ` — ${category.name}`}
            </summary>
            <div className="stack" style={{ marginTop: "0.75rem" }}>
              {rail}
            </div>
          </details>
        )}

        <div className="stack">
          <FieldsPanel
            categorySlug={selected}
            categoryName={category?.name ?? null}
            fields={fields.data}
            loading={fields.loading}
            error={fields.error}
            units={units.data ?? []}
            onChanged={() => fields.reload()}
          />
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ fields ----

function FieldsPanel({
  categorySlug,
  categoryName,
  fields,
  loading,
  error,
  units,
  onChanged,
}: {
  readonly categorySlug: string;
  readonly categoryName: string | null;
  readonly fields: readonly ParameterFieldRead[] | null;
  readonly loading: boolean;
  readonly error: unknown;
  readonly units: readonly BaseUnitOption[];
  readonly onChanged: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const where = categorySlug === "" ? "every part" : (categoryName ?? categorySlug);
  const own = (fields ?? []).filter((field) => !field.inherited);
  const inherited = (fields ?? []).filter((field) => field.inherited);

  return (
    <>
      <div className="card">
        <div className="row">
          <h2 style={{ margin: 0 }}>Fields on {where}</h2>
          <span className="spacer" />
          {!adding && (
            <button type="button" className="primary" onClick={() => setAdding(true)}>
              Add a field
            </button>
          )}
        </div>
        <p className="muted-note" style={{ margin: 0 }}>
          {categorySlug === ""
            ? "With no category selected these are the fields every part has, whatever it is — a package, a mounting type. A field authored here is offered everywhere."
            : `Authored on ${where} and offered on it and every category under it, because a part is filed under the deepest node — a field on Capacitors has to reach Capacitors > Ceramic.`}
        </p>
      </div>

      {adding && (
        <div className="card">
          <h3>A new field on {where}</h3>
          <NewField
            categorySlug={categorySlug}
            appliesTo={where}
            units={units}
            onDone={(created) => {
              setAdding(false);
              if (created) {
                onChanged();
              }
            }}
          />
        </div>
      )}

      {loading && <Loading what="fields" />}
      <ErrorBanner error={error} fallback="The fields could not be loaded." />

      {fields !== null && own.length === 0 && inherited.length === 0 && (
        <Empty>
          Nothing is filterable here yet. A field added now shows up in the filter panel for
          these parts immediately.
        </Empty>
      )}

      {own.length > 0 && (
        <div className="card">
          <h3>Authored here</h3>
          <ul className="list">
            {own.map((field) => (
              <FieldRow key={field.id} field={field} units={units} onChanged={onChanged} />
            ))}
          </ul>
        </div>
      )}

      {inherited.length > 0 && (
        <div className="card">
          <h3>Inherited</h3>
          <p className="muted-note" style={{ margin: 0 }}>
            Offered here but authored further up — on an ancestor category, or on nothing at all,
            which means every part. Editing one of these changes it for every category that
            inherits it, not just this one.
          </p>
          <ul className="list">
            {inherited.map((field) => (
              <FieldRow key={field.id} field={field} units={units} onChanged={onChanged} />
            ))}
          </ul>
        </div>
      )}
    </>
  );
}

/**
 * One field, with the two numbers that decide what may be done to it: how many
 * parts hold a value (which freezes its type and quantity, and refuses a delete)
 * and whether it is part of the shipped library (which freezes its identity for
 * good).
 */
function FieldRow({
  field,
  units,
  onChanged,
}: {
  readonly field: ParameterFieldRead;
  readonly units: readonly BaseUnitOption[];
  readonly onChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const frozen = frozenColumns(field);
  // `choices` is absent rather than empty on a field that has none, because the
  // server omits a default-empty list from the response.
  const options = field.choices ?? [];

  async function save(draft: FieldDraft): Promise<void> {
    const request = toFieldUpdateRequest(draft, field, {
      categorySlug: field.applies_to_category,
      clientOpId: uuid4(),
    });
    if (isEmptyUpdate(request)) {
      setEditing(false);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await updateParameterField(field.id, request);
      setEditing(false);
      onChanged();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  async function remove(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await deleteParameterField(field.id);
      onChanged();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
      setConfirmDelete(false);
    }
  }

  const anchor = anchorForReason(problemOf(error)?.reason ?? null);
  const message = problemOf(error)?.message ?? null;

  return (
    <li className="list-item">
      <div className="row">
        <span className="title">{field.display_name}</span>
        <span className="mono dim">{field.name}</span>
        <span className="spacer" />
        {field.is_seed && <span className="badge badge-info">shipped</span>}
        {field.value_count > 0 && (
          <span className="badge">
            {field.value_count} {field.value_count === 1 ? "part" : "parts"}
          </span>
        )}
      </div>
      <p className="sub" style={{ margin: 0 }}>
        {field.value_type === "numeric"
          ? `a number in ${field.base_unit ?? "no unit"}`
          : field.value_type === "enum"
            ? `${options.length} option${options.length === 1 ? "" : "s"}`
            : field.value_type}
        {" · "}
        <span className="mono">{field.substitution_direction}</span>
        {field.applies_to_category === null ? " · on every part" : ` · on ${field.applies_to_category}`}
      </p>

      {field.value_type === "enum" && (
        /* Collapsed by default: a field's options are its *contents*, and six
           dielectrics unfolded on every list field turn a page about which
           fields exist into a wall of options. The count is already in the line
           above, so the summary says what opening it is for. */
        <details>
          <summary>
            {options.length === 0
              ? "No options yet — this filter matches nothing"
              : `Options — ${options.length}, and where one is added or removed`}
          </summary>
          <Options field={field} onChanged={onChanged} />
        </details>
      )}

      <div className="row">
        <button type="button" onClick={() => setEditing(!editing)} disabled={busy}>
          {editing ? "Stop editing" : "Edit"}
        </button>
        <span className="spacer" />
        {field.is_seed ? (
          <span className="muted-note">Part of the shipped library — it cannot be deleted.</span>
        ) : field.value_count > 0 ? (
          <span className="muted-note">
            {field.value_count} {field.value_count === 1 ? "part holds" : "parts hold"} a value, so
            deleting it would delete {field.value_count === 1 ? "that value" : "those values"} too.
          </span>
        ) : confirmDelete ? (
          <>
            <button type="button" onClick={() => setConfirmDelete(false)} disabled={busy}>
              Keep it
            </button>
            <button type="button" className="danger" onClick={() => void remove()} disabled={busy}>
              Delete “{field.display_name}”
            </button>
          </>
        ) : (
          <button type="button" onClick={() => setConfirmDelete(true)} disabled={busy}>
            Delete
          </button>
        )}
      </div>

      {!editing && <ErrorBanner error={error} fallback="That field could not be changed." />}

      {editing && (
        <FieldForm
          initial={draftFromField(field)}
          mode="edit"
          frozen={frozen}
          baseUnits={units}
          appliesTo={field.applies_to_category ?? "every part"}
          conflict={null}
          canNamespace={false}
          busy={busy}
          serverAnchor={anchor}
          serverMessage={message}
          onSubmit={(draft) => void save(draft)}
          onCancel={() => setEditing(false)}
        />
      )}
    </li>
  );
}

/**
 * A list field's options, added and removed one at a time.
 *
 * Not part of the field form, because these are separate rows with a refusal of
 * their own: `parameter_value.choice_id` is `RESTRICT`, so an option parts are
 * filed under cannot be removed — and the count that says so is per option, not
 * per field.
 */
function Options({
  field,
  onChanged,
}: {
  readonly field: ParameterFieldRead;
  readonly onChanged: () => void;
}) {
  const [key, setKey] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function add(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await addParameterChoice(field.id, {
        key: key.trim(),
        label: label.trim() === "" ? key.trim() : label.trim(),
        client_op_id: uuid4(),
      });
      setKey("");
      setLabel("");
      onChanged();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  async function remove(choiceId: number): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await deleteParameterChoice(field.id, choiceId);
      onChanged();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <ul className="list">
        {(field.choices ?? []).map((choice) => (
          <li key={choice.id} className="list-item">
            <div className="row">
              <span className="mono">{choice.key}</span>
              <span>{choice.label}</span>
              <span className="spacer" />
              {choice.use_count > 0 ? (
                <span className="muted-note">
                  {choice.use_count} {choice.use_count === 1 ? "part" : "parts"} filed under it
                </span>
              ) : (
                <button type="button" disabled={busy} onClick={() => void remove(choice.id)}>
                  Remove
                </button>
              )}
            </div>
            {choice.aliases.length > 0 && (
              <p className="sub" style={{ margin: 0 }}>
                also written {choice.aliases.join(", ")}
              </p>
            )}
          </li>
        ))}
      </ul>
      <div className="fields">
        <label className="field">
          <span>Another option — key</span>
          <input
            className="mono"
            value={key}
            onChange={(event) => setKey(event.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        <label className="field">
          <span>Label</span>
          <input value={label} onChange={(event) => setLabel(event.target.value)} />
        </label>
      </div>
      <div className="row">
        <span className="spacer" />
        <button type="button" disabled={busy || key.trim() === ""} onClick={() => void add()}>
          Add the option
        </button>
      </div>
      <ErrorBanner error={error} fallback="That option could not be changed." />
    </div>
  );
}

/**
 * Authoring a field, including the collision.
 *
 * The 409 is kept in state rather than shown as an error and cleared, because the
 * decision it asks for — reuse the existing field, or file a separate one — is
 * answered by pressing a button in the form that is still holding what was typed.
 */
function NewField({
  categorySlug,
  appliesTo,
  units,
  onDone,
}: {
  readonly categorySlug: string;
  readonly appliesTo: string;
  readonly units: readonly BaseUnitOption[];
  readonly onDone: (created: boolean) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [conflict, setConflict] = useState<ParameterFieldRead | null>(null);
  const [reused, setReused] = useState<string | null>(null);
  /**
   * One key per collision policy, minted once with the form.
   *
   * Not one key for the whole form: answering the collision resubmits with a
   * different `on_name_conflict`, which is a genuinely different request, and
   * replaying the first key would return the 409 the first attempt got. Not a
   * fresh key per attempt either, or a doubled tap on bad wifi files two fields.
   * Keys are a plain uuid because `client_op_id` is capped at 36 characters —
   * exactly a uuid — so there is no room to suffix one.
   */
  const [opIds] = useState<Record<NameConflictPolicy, string>>(() => ({
    fail: uuid4(),
    reuse: uuid4(),
    namespace: uuid4(),
  }));

  async function save(draft: FieldDraft): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const response = await createParameterField(
        toFieldCreateRequest(draft, {
          categorySlug: categorySlug === "" ? null : categorySlug,
          clientOpId: opIds[draft.onNameConflict],
        }),
      );
      setConflict(null);
      if (response.reused) {
        setReused(response.field.display_name);
        return;
      }
      onDone(true);
    } catch (cause) {
      setError(cause);
      setConflict(existingFieldOf(cause));
    } finally {
      setBusy(false);
    }
  }

  const problem = problemOf(error);
  const anchor: DraftAnchor | null = anchorForReason(problem?.reason ?? null);

  if (reused !== null) {
    return (
      <Notice kind="ok" title={`Reusing “${reused}”`}>
        <p style={{ margin: 0 }}>
          Nothing new was created: the existing field now covers this too, and any options you
          named that it did not have were added to it.
        </p>
        <div className="row">
          <button type="button" className="primary" onClick={() => onDone(true)}>
            Done
          </button>
        </div>
      </Notice>
    );
  }

  return (
    <>
      {/* The conflict is rendered inside the form, so this banner deliberately
          stays quiet for it rather than saying the same thing twice. */}
      {conflict === null && (
        <ErrorBanner error={error} fallback="That field could not be created." />
      )}
      <FieldForm
        initial={BLANK_FIELD_DRAFT}
        mode="create"
        frozen={null}
        baseUnits={units}
        appliesTo={appliesTo}
        conflict={conflict}
        canNamespace={categorySlug !== ""}
        busy={busy}
        serverAnchor={conflict === null ? anchor : null}
        serverMessage={conflict === null ? (problem?.message ?? null) : null}
        onSubmit={(draft) => void save(draft)}
        onCancel={() => onDone(false)}
      />
    </>
  );
}

// -------------------------------------------------------------- categories ----

/**
 * Author a category, with the parent as a **control** rather than as a consequence
 * of what happened to be selected.
 *
 * It was the selection: the button read "New category under Capacitors" when a
 * category was highlighted and "New top-level category" when none was, and that is
 * how a sub-category gets filed at the root — the label is the only hint, it is
 * read after the decision to press, and nothing on the form says what will happen.
 * The first thing a real user did with this screen was create a top-level category
 * they meant to nest.
 *
 * So the parent is a select, defaulted to the selection but visible and editable
 * before saving, and it lists the whole tree indented. `MoveCategory` below is the
 * other half: a parent chosen wrong has to be fixable, and "delete it and start
 * again" is not a fix once fields hang off it.
 */
function NewCategory({
  categories,
  selected,
  onCreated,
}: {
  readonly categories: readonly CategoryNode[];
  readonly selected: CategoryNode | null;
  readonly onCreated: (slug: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  // "" is the top level, which is a real answer and not an absent one.
  const [parentId, setParentId] = useState<string>(selected === null ? "" : String(selected.id));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  function start(): void {
    // Re-read the selection on open rather than on render: the rail may have moved
    // since this component mounted, and the default should be what is highlighted
    // *now*.
    setParentId(selected === null ? "" : String(selected.id));
    setOpen(true);
  }

  async function save(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const response = await createPartCategory({
        name: name.trim(),
        slug: slug.trim(),
        ...(parentId === "" ? {} : { parent_id: Number(parentId) }),
        client_op_id: uuid4(),
      });
      setOpen(false);
      setName("");
      setSlug("");
      setSlugTouched(false);
      onCreated(response.part_category.slug);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <div className="row">
        <button type="button" onClick={start}>
          New category…
        </button>
      </div>
    );
  }

  const parent = categories.find((node) => String(node.id) === parentId) ?? null;

  return (
    <form
      className="stack"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <label className="field">
        <span>Name</span>
        <input
          value={name}
          onChange={(event) => {
            setName(event.target.value);
            if (!slugTouched) {
              setSlug(slugify(event.target.value));
            }
          }}
          placeholder="Ceramic"
        />
      </label>
      <label className="field">
        <span>Inside</span>
        <select value={parentId} onChange={(event) => setParentId(event.target.value)}>
          <option value="">Top level — not inside anything</option>
          {categories.map((node) => (
            <option key={node.id} value={String(node.id)}>
              {"\u00a0".repeat(node.depth * 2)}
              {node.name}
            </option>
          ))}
        </select>
      </label>
      <p className="muted-note" style={{ margin: 0 }}>
        {parent === null
          ? "A new branch of the taxonomy. It inherits only the fields that apply to every part."
          : `Inside ${parent.name}, so it inherits every field authored on ${parent.name} and above it.`}
      </p>
      <label className="field">
        <span>Slug — what a search URL names, and permanent</span>
        <input
          className="mono"
          value={slug}
          onChange={(event) => {
            setSlugTouched(true);
            setSlug(event.target.value);
          }}
          placeholder="ceramic"
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      <ErrorBanner error={error} fallback="That category could not be created." />
      <div className="row">
        <button type="button" onClick={() => setOpen(false)} disabled={busy}>
          Cancel
        </button>
        <span className="spacer" />
        <button
          type="submit"
          className="primary"
          disabled={busy || name.trim() === "" || slug.trim() === ""}
        >
          {busy ? "Saving…" : "Create the category"}
        </button>
      </div>
    </form>
  );
}

/**
 * Move a category — the fix for one filed in the wrong place.
 *
 * Its own control rather than a field on an edit form, because it is a different
 * operation: it rebuilds the path cache for the whole table and it takes the
 * category's **whole subtree** with it, and a `parent_id` sitting quietly among
 * renames would not say so. The API refuses a move into the category's own subtree
 * as `would_create_cycle`, walked over `parent_id` rather than the cache, so this
 * offers every category and lets the server be the one that knows.
 */
function MoveCategory({
  category,
  categories,
  onMoved,
}: {
  readonly category: CategoryNode;
  readonly categories: readonly CategoryNode[];
  readonly onMoved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [parentId, setParentId] = useState<string>(
    category.parent_slug === null
      ? ""
      : String(categories.find((node) => node.slug === category.parent_slug)?.id ?? ""),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function save(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await movePartCategory(category.id, {
        parent_id: parentId === "" ? null : Number(parentId),
        client_op_id: uuid4(),
      });
      setOpen(false);
      onMoved();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <div className="row">
        <button type="button" onClick={() => setOpen(true)}>
          Move {category.name}…
        </button>
      </div>
    );
  }

  return (
    <form
      className="stack"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <label className="field">
        <span>Put {category.name} inside</span>
        <select value={parentId} onChange={(event) => setParentId(event.target.value)}>
          <option value="">Top level — not inside anything</option>
          {categories
            .filter((node) => node.id !== category.id)
            .map((node) => (
              <option key={node.id} value={String(node.id)}>
                {"\u00a0".repeat(node.depth * 2)}
                {node.name}
              </option>
            ))}
        </select>
      </label>
      <p className="muted-note" style={{ margin: 0 }}>
        Anything already inside {category.name} moves with it, and what these parts can be
        filtered by changes to match the new place — inheritance is read from the tree as it is
        now, not copied when a field is authored.
      </p>
      <ErrorBanner error={error} fallback="That category could not be moved." />
      <div className="row">
        <button type="button" onClick={() => setOpen(false)} disabled={busy}>
          Cancel
        </button>
        <span className="spacer" />
        <button type="submit" className="primary" disabled={busy}>
          {busy ? "Moving…" : "Move it"}
        </button>
      </div>
    </form>
  );
}

// ------------------------------------------------------------------- units ----

/**
 * The quantities a numeric field can be measured in, and how to add one.
 *
 * Almagest ships the ones an electronics inventory needs — the electrical set plus
 * light, mass, length and a few ratios — and those cannot be redefined here: every
 * value already stored was read under the shipped definition of its quantity, so a
 * local `farad` meaning something else would change what those numbers mean without
 * touching a single one of them. What this panel is for is the unit nobody
 * anticipated: bytes of flash, turns of wire, hours of runtime.
 *
 * A definition is refused if the grammar cannot read a value written in its symbol,
 * checked by actually parsing one. That refusal is the point of the whole control:
 * a unit that stores fine and then reads nothing gives you a field that looks like
 * it works, accepts nothing, and matches nothing.
 */
function Units({
  quantities,
  loading,
  error,
  onChanged,
}: {
  readonly quantities: readonly QuantityRead[] | null;
  readonly loading: boolean;
  readonly error: unknown;
  readonly onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const custom = (quantities ?? []).filter((quantity) => quantity.custom);
  const shipped = (quantities ?? []).filter((quantity) => !quantity.custom);

  return (
    <div className="card">
      <h3>Units</h3>
      <p className="muted-note" style={{ margin: 0 }}>
        What a number field can be measured in. Choosing one is what makes{" "}
        <span className="mono">20–30µF</span> mean something and{" "}
        <span className="mono">1M</span> under capacitance a refusal.
      </p>

      <details>
        <summary>
          {shipped.length} that ship with Almagest — they cannot be changed
        </summary>
        <ul className="list">
          {shipped.map((quantity) => (
            <li key={quantity.name} className="list-item">
              <div className="row">
                <span className="mono">{quantity.symbol}</span>
                <span>{quantity.display_name}</span>
                <span className="spacer" />
                <span className="mono dim">{quantity.name}</span>
              </div>
            </li>
          ))}
        </ul>
        <p className="muted-note" style={{ margin: 0 }}>
          Frozen because every value already recorded was read under these
          definitions — a redefined farad would change what stored numbers mean
          without touching them.
        </p>
      </details>

      {loading && <Loading what="units" />}
      <ErrorBanner error={error} fallback="The units could not be loaded." />

      {custom.length > 0 && (
        <ul className="list">
          {custom.map((quantity) => (
            <CustomUnitRow key={quantity.name} quantity={quantity} onChanged={onChanged} />
          ))}
        </ul>
      )}

      {open ? (
        <NewUnit
          onDone={(created) => {
            setOpen(false);
            if (created) {
              onChanged();
            }
          }}
        />
      ) : (
        <div className="row">
          <button type="button" onClick={() => setOpen(true)}>
            New unit…
          </button>
        </div>
      )}
    </div>
  );
}

function CustomUnitRow({
  quantity,
  onChanged,
}: {
  readonly quantity: QuantityRead;
  readonly onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // Defaulted rather than optional-chained at each use: the count is absent from
  // the response only for a shipped quantity, and this row is only ever a custom
  // one — a shipped one has no id to delete by either.
  const used = quantity.field_count ?? 0;

  async function remove(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await deleteParameterQuantity(quantity.id ?? 0);
      onChanged();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="list-item">
      <div className="row">
        <span className="mono">{quantity.symbol}</span>
        <span className="title">{quantity.display_name}</span>
        <span className="spacer" />
        <span className="mono dim">{quantity.name}</span>
      </div>
      <div className="row">
        {used > 0 ? (
          <span className="muted-note">
            {used} {used === 1 ? "field is" : "fields are"} measured in it, so it cannot be
            removed — those fields could no longer read a value.
          </span>
        ) : (
          <>
            <span className="spacer" />
            <button type="button" disabled={busy} onClick={() => void remove()}>
              Remove
            </button>
          </>
        )}
      </div>
      <ErrorBanner error={error} fallback="That unit could not be removed." />
    </li>
  );
}

/**
 * The form for a unit of your own.
 *
 * Four questions, and the last two are the ones with teeth. **Prefixes** decide
 * whether `10k` of this is ten thousand — right for anything measured, wrong for
 * anything counted, and left on for something counted it makes a stray `k` mean a
 * thousandfold. **Negatives** switch the sanity window to its signed reading,
 * because a window of [-40, 125] compared against magnitudes would accept -200.
 */
function NewUnit({ onDone }: { readonly onDone: (created: boolean) => void }) {
  const [name, setName] = useState("");
  const [symbol, setSymbol] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [low, setLow] = useState("");
  const [high, setHigh] = useState("");
  const [aliases, setAliases] = useState("");
  const [allowPrefix, setAllowPrefix] = useState(true);
  const [allowNegative, setAllowNegative] = useState(false);
  const [allowZero, setAllowZero] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  function numberOrNull(raw: string): number | null {
    const trimmed = raw.trim();
    if (trimmed === "") {
      return null;
    }
    const value = Number(trimmed);
    return Number.isFinite(value) ? value : null;
  }

  async function save(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await createParameterQuantity({
        name: name.trim(),
        symbol: symbol.trim(),
        display_name: displayName.trim() === "" ? name.trim() : displayName.trim(),
        word_aliases: splitAliases(aliases),
        low: numberOrNull(low),
        high: numberOrNull(high),
        allow_zero: allowZero,
        allow_negative: allowNegative,
        allow_prefix: allowPrefix,
      });
      onDone(true);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="stack"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <label className="field">
        {/* Not "what it measures": the field form's unit select already says that,
            and two controls with one label on a screen is ambiguous to read. */}
        <span>What this unit measures</span>
        <input
          value={displayName}
          onChange={(event) => {
            setDisplayName(event.target.value);
            if (!nameTouched) {
              setName(fieldKey(event.target.value));
            }
          }}
          placeholder="Bytes of flash"
        />
      </label>
      <label className="field">
        <span>Symbol — what a value is written and printed with</span>
        <input
          className="mono"
          value={symbol}
          onChange={(event) => setSymbol(event.target.value)}
          placeholder="B"
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      <label className="field">
        <span>Key — what a field stores, and permanent</span>
        <input
          className="mono"
          value={name}
          onChange={(event) => {
            setNameTouched(true);
            setName(event.target.value);
          }}
          placeholder="byte"
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      <label className="field">
        <span>Other spellings, comma separated — matched whatever the case</span>
        <input
          value={aliases}
          onChange={(event) => setAliases(event.target.value)}
          placeholder="byte, bytes"
          autoComplete="off"
        />
      </label>
      <div className="fields">
        <label className="field">
          <span>Lowest plausible value</span>
          <input inputMode="decimal" value={low} onChange={(event) => setLow(event.target.value)} />
        </label>
        <label className="field">
          <span>Highest plausible value</span>
          <input
            inputMode="decimal"
            value={high}
            onChange={(event) => setHigh(event.target.value)}
          />
        </label>
      </div>
      <label className="choice">
        <input
          type="checkbox"
          checked={allowPrefix}
          onChange={(event) => setAllowPrefix(event.target.checked)}
        />
        <span>
          <span className="title">Accepts SI prefixes</span>
          <span className="sub">
            So <span className="mono">10k</span> of this means ten thousand. Right for anything
            measured; turn it off for anything counted, or a stray k silently means a
            thousandfold.
          </span>
        </span>
      </label>
      <label className="choice">
        <input
          type="checkbox"
          checked={allowNegative}
          onChange={(event) => setAllowNegative(event.target.checked)}
        />
        <span>
          <span className="title">Can be negative</span>
          <span className="sub">
            Also makes the window above read as signed rather than as a magnitude, which is the
            only reading that means anything once below zero is allowed.
          </span>
        </span>
      </label>
      <label className="choice">
        <input
          type="checkbox"
          checked={allowZero}
          onChange={(event) => setAllowZero(event.target.checked)}
        />
        <span>
          <span className="title">Can be zero</span>
          <span className="sub">
            A 0 Ω jumper is a real part, and a zero of something counted usually is not.
          </span>
        </span>
      </label>
      <ErrorBanner error={error} fallback="That unit could not be created." />
      <div className="row">
        <button type="button" onClick={() => onDone(false)} disabled={busy}>
          Cancel
        </button>
        <span className="spacer" />
        <button
          type="submit"
          className="primary"
          disabled={busy || name.trim() === "" || symbol.trim() === ""}
        >
          {busy ? "Saving…" : "Create the unit"}
        </button>
      </div>
    </form>
  );
}

// ------------------------------------------------------------------- kinds ----

/**
 * The kinds, and the sentence that keeps them apart from categories.
 *
 * Last on the screen and behind its own heading on purpose: a kind is rarely the
 * answer — there are three or four of them in a lifetime of an inventory — and a
 * user looking for somewhere to put "ESR" must not find this first and conclude
 * the app cannot do it.
 */
function Kinds({ state }: { readonly state: ReturnType<typeof useAsync<PartKindRead[]>> }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function save(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await createPartKind({
        slug: slug.trim(),
        display_name: name.trim(),
        client_op_id: uuid4(),
      });
      setOpen(false);
      setName("");
      setSlug("");
      setSlugTouched(false);
      state.reload();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h3>Kinds</h3>
      <p className="muted-note" style={{ margin: 0 }}>
        What something fundamentally is — a component, a tool, a consumable. A kind carries{" "}
        <strong>no fields</strong>: it is for keeping a screwdriver out of the results when you
        are shopping for capacitors. Fields belong to a category above.
      </p>
      {state.loading && <Loading what="kinds" />}
      <ErrorBanner error={state.error} fallback="The kinds could not be loaded." />
      <ul className="list">
        {(state.data ?? []).map((kind) => (
          <li key={kind.id} className="list-item">
            <div className="row">
              <span className="title">{kind.display_name}</span>
              <span className="mono dim">{kind.slug}</span>
              <span className="spacer" />
              <span className="count">{kind.part_count}</span>
            </div>
          </li>
        ))}
      </ul>
      {open ? (
        <form
          className="stack"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <label className="field">
            <span>Name</span>
            <input
              value={name}
              onChange={(event) => {
                setName(event.target.value);
                if (!slugTouched) {
                  setSlug(slugify(event.target.value));
                }
              }}
              placeholder="Consumable"
            />
          </label>
          <label className="field">
            <span>Slug — what `part_kind=` names in a search, and permanent</span>
            <input
              className="mono"
              value={slug}
              onChange={(event) => {
                setSlugTouched(true);
                setSlug(event.target.value);
              }}
              placeholder="consumable"
              autoComplete="off"
              spellCheck={false}
            />
          </label>
          <ErrorBanner error={error} fallback="That kind could not be created." />
          <div className="row">
            <button type="button" onClick={() => setOpen(false)} disabled={busy}>
              Cancel
            </button>
            <span className="spacer" />
            <button
              type="submit"
              className="primary"
              disabled={busy || name.trim() === "" || slug.trim() === ""}
            >
              {busy ? "Saving…" : "Create the kind"}
            </button>
          </div>
        </form>
      ) : (
        <div className="row">
          <button type="button" onClick={() => setOpen(true)}>
            New kind
          </button>
          <span className="spacer" />
          <Link to="/search">Browse parts →</Link>
        </div>
      )}
    </div>
  );
}
