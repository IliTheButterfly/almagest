/**
 * The parameter-field form's draft, its wording, and the translation to the wire.
 *
 * Split out of the form for the same reason `containers/typeDraft` is: this half
 * is where the mistakes are possible, so this is where the tests belong. Three
 * of them are worth naming, because each is a silent failure rather than a typo:
 *
 * - **a numeric field with no unit matches nothing.** `base_unit` is a *quantity
 *   name* (`ohm`, `farad`, `volt`), not a symbol and not a plural, and it is what
 *   makes a bare `1M` read as 1 MΩ under resistance and be refused under
 *   capacitance. The server validates it against the very parser search uses, so
 *   the authoritative check is a round trip — this module only refuses *blank*,
 *   and the screen shows the server's refusal against the same input.
 * - **a list field with no options matches nothing** while looking like a working
 *   filter, which is why the API takes the options in the same request.
 * - **`substitution_direction` has no default**, deliberately. It is what decides
 *   whether a 50 V capacitor may stand in for a 25 V one, and a voltage rating
 *   defaulted to `exact` would make substitution search wrong by construction
 *   with nothing on screen to show it. So the draft starts it empty and the form
 *   cannot be submitted until the user has answered.
 *
 * Every draft field is a string, including the numbers: a controlled numeric
 * input whose state is a `number` cannot represent "the box is empty". The parse
 * happens once, on the way out.
 */

import type {
  NameConflictPolicy,
  ParameterFieldCreate,
  ParameterFieldRead,
  ParameterFieldUpdate,
  SubstitutionDirection,
  ValueType,
} from "../api/client";

/** One option of a list field, as typed. */
export interface ChoiceDraft {
  readonly key: string;
  readonly label: string;
  /** Comma-separated, because that is how it is typed. Split on the way out. */
  readonly aliases: string;
}

export interface FieldDraft {
  readonly name: string;
  readonly displayName: string;
  readonly valueType: ValueType;
  readonly baseUnit: string;
  /** `""` until answered — see the module note: there is no safe default. */
  readonly substitutionDirection: SubstitutionDirection | "";
  readonly plausibleMin: string;
  readonly plausibleMax: string;
  readonly choices: readonly ChoiceDraft[];
  readonly onNameConflict: NameConflictPolicy;
}

export const BLANK_CHOICE: ChoiceDraft = { key: "", label: "", aliases: "" };

export const BLANK_FIELD_DRAFT: FieldDraft = {
  name: "",
  displayName: "",
  valueType: "numeric",
  baseUnit: "",
  substitutionDirection: "",
  plausibleMin: "",
  plausibleMax: "",
  choices: [BLANK_CHOICE, BLANK_CHOICE],
  onNameConflict: "fail",
};

// ------------------------------------------------------------------ wording ----

/** Which part of the form a problem or a server refusal belongs against. */
export type DraftAnchor =
  | "name"
  | "displayName"
  | "valueType"
  | "baseUnit"
  | "substitutionDirection"
  | "plausible"
  | "choices"
  | "category";

export interface DraftProblem {
  readonly anchor: DraftAnchor;
  readonly message: string;
}

/**
 * What each value type *is*, and what it commits the user to.
 *
 * The consequence is in the label rather than in a footnote because it is not
 * advice: text genuinely cannot be filtered by range, and choosing it for a
 * capacitance is a decision that only shows up much later as "why can't I search
 * 20–30 µF".
 */
export const VALUE_TYPE_COPY: Readonly<
  Record<ValueType, { readonly label: string; readonly implication: string }>
> = {
  numeric: {
    label: "A number with a unit — 22 µF, 4k7, 50 V",
    implication:
      "Needs a unit, below. Filterable by range and by substitution, and it is the only " +
      "type that is: 20–30 µF only means something when the field knows it is farads.",
  },
  enum: {
    label: "A list of options — C0G, X7R, X5R",
    implication:
      "Needs at least one option, below. Filtering is ticking the ones you want; a list " +
      "with no options matches nothing while still looking like a working filter.",
  },
  bool: {
    label: "Yes or no — automotive grade, RoHS",
    implication: "Filtered as one or the other. Nothing to configure.",
  },
  text: {
    label: "Free text — a note, a marking",
    implication:
      "Not filterable by range: matching is substring only. If you will ever want " +
      "“between 20 and 30 of these”, this is the wrong type — use a number with a unit.",
  },
};

/**
 * `substitution_direction`, asked as a question about the real world.
 *
 * This is the hardest control on the screen and the most valuable to get right:
 * it is the whole of what "find me a substitute" means, and it is deterministic —
 * the SQL filter uses it directly and never asks a model. The enum value is shown
 * as secondary so a user reading the API docs can line the two up, but nobody has
 * to know the word `higher_ok` to answer "is a bigger number always acceptable?".
 */
export const SUBSTITUTION_COPY: Readonly<
  Record<SubstitutionDirection, { readonly question: string; readonly example: string }>
> = {
  higher_ok: {
    question: "A bigger number is always acceptable",
    example: "A rating. A 50 V capacitor will do where 25 V was asked for — never the reverse.",
  },
  lower_ok: {
    question: "A smaller number is always acceptable",
    example: "A tolerance or a leakage. A 1% resistor will do where 5% was asked for.",
  },
  range_overlap: {
    question: "It has to fall inside the range asked for, either way",
    example: "A value. 22 µF is not a substitute for 10 µF, and 10 µF is not one for 22 µF.",
  },
  exact: {
    question: "It has to match exactly",
    example: "A package or a pin count. An 0805 does not go on an 0603 footprint.",
  },
};

/**
 * Which input a server refusal belongs against.
 *
 * The reason codes are `app.services.parameter_fields`'; anything missing here
 * falls through to the banner, which still shows the server's own message. That
 * is the same deliberately-partial arrangement as `REASON_HINTS`.
 */
const ANCHOR_BY_REASON: Readonly<Record<string, DraftAnchor>> = {
  missing_base_unit: "baseUnit",
  unknown_base_unit: "baseUnit",
  unit_on_non_numeric: "baseUnit",
  base_unit_in_use: "baseUnit",
  no_choices: "choices",
  choices_on_non_enum: "choices",
  duplicate_choice_key: "choices",
  empty_choice_key: "choices",
  choice_in_use: "choices",
  inverted_plausibility: "plausible",
  duplicate_name: "name",
  incompatible_existing_field: "name",
  namespace_needs_category: "category",
  unknown_category: "category",
  value_type_in_use: "valueType",
};

export function anchorForReason(reason: string | null): DraftAnchor | null {
  return reason === null ? null : (ANCHOR_BY_REASON[reason] ?? null);
}

// --------------------------------------------------------------- validation ----

/**
 * A filter key from a display name: `Voltage rating` → `voltage_rating`.
 *
 * Underscores rather than the dashes a container-type slug uses, because this is
 * the string a search request and a shared search URL name — and every field that
 * ships (`voltage_rating`, `mounting_type`) is snake_case. A new field that broke
 * the convention would read as a different sort of thing in the URL.
 */
export function fieldKey(displayName: string): string {
  return displayName
    .toLowerCase()
    .normalize("NFKD")
    // The decomposed accents themselves, dropped rather than left to the class
    // below: NFKD turns "é" into "e" plus a combining mark, and a mark that
    // survives to the next step becomes a separator — `resistance` would go into
    // the search URL as `re_sistance`.
    .replace(/\p{M}+/gu, "")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 128);
}

function filledChoices(draft: FieldDraft): readonly ChoiceDraft[] {
  return draft.choices.filter((choice) => choice.key.trim() !== "");
}

/**
 * The reasons this draft cannot be saved, each anchored to the input that caused
 * it.
 *
 * Checked here *as well as* on the server, and only where the client can be sure:
 * a blank unit is certainly wrong, but whether `ohms` is a unit is the parser's
 * call and asking it is a round trip. Nothing in here is a policy the server does
 * not also enforce.
 */
export function fieldDraftProblems(draft: FieldDraft): readonly DraftProblem[] {
  const problems: DraftProblem[] = [];

  if (draft.displayName.trim() === "") {
    problems.push({ anchor: "displayName", message: "Give the field a name." });
  }
  if (draft.name.trim() === "") {
    problems.push({
      anchor: "name",
      message: "The filter key cannot be empty — it is what a search URL names.",
    });
  }
  if (draft.substitutionDirection === "") {
    problems.push({
      anchor: "substitutionDirection",
      message:
        "Answer the substitution question. There is no safe default: it decides whether a " +
        "50 V part may stand in for a 25 V one.",
    });
  }
  if (draft.valueType === "numeric" && draft.baseUnit.trim() === "") {
    problems.push({
      anchor: "baseUnit",
      message:
        "A number needs a unit, or no value could ever be entered against it and it would " +
        "never match a search.",
    });
  }
  if (draft.valueType !== "numeric" && draft.baseUnit.trim() !== "") {
    problems.push({
      anchor: "baseUnit",
      message: `A unit only means something for a number. Clear it, or make this a number.`,
    });
  }
  if (draft.valueType === "enum") {
    const filled = filledChoices(draft);
    if (filled.length === 0) {
      problems.push({
        anchor: "choices",
        message: "A list needs at least one option — an empty list matches nothing.",
      });
    }
    const keys = filled.map((choice) => choice.key.trim().toLowerCase());
    if (new Set(keys).size !== keys.length) {
      problems.push({ anchor: "choices", message: "Two options share the same key." });
    }
  }
  if (draft.valueType !== "enum" && filledChoices(draft).length > 0) {
    problems.push({
      anchor: "choices",
      message: "Options only mean something for a list. Change the type, or clear them.",
    });
  }

  const low = numberOrNull(draft.plausibleMin);
  const high = numberOrNull(draft.plausibleMax);
  if (draft.plausibleMin.trim() !== "" && low === null) {
    problems.push({ anchor: "plausible", message: "The lower sanity bound is not a number." });
  }
  if (draft.plausibleMax.trim() !== "" && high === null) {
    problems.push({ anchor: "plausible", message: "The upper sanity bound is not a number." });
  }
  if (low !== null && high !== null && low > high) {
    problems.push({
      anchor: "plausible",
      message: "The lower bound is above the upper one, which is a window no value can fall in.",
    });
  }

  return problems;
}

function numberOrNull(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === "") {
    return null;
  }
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : null;
}

/** Comma- or newline-separated, trimmed, blanks dropped. */
export function splitAliases(raw: string): string[] {
  return raw
    .split(/[,\n]/)
    .map((alias) => alias.trim())
    .filter((alias) => alias !== "");
}

/**
 * The request.
 *
 * `applies_to_category` is a **slug**, and the field is then offered on that
 * category *and every descendant of it* — which is why authoring "ESR" on
 * Capacitors is enough for Capacitors > Ceramic, the node parts are actually
 * filed under. `null` means every part has it, like a package.
 */
export function toFieldCreateRequest(
  draft: FieldDraft,
  options: { categorySlug: string | null; clientOpId: string },
): ParameterFieldCreate {
  const numeric = draft.valueType === "numeric";
  return {
    name: draft.name.trim(),
    display_name: draft.displayName.trim(),
    value_type: draft.valueType,
    base_unit: numeric ? draft.baseUnit.trim() : null,
    // Never `""`: the draft's empty string is a not-yet-answered marker, and
    // `fieldDraftProblems` blocks submission while it is one, so this cast is
    // reached only once a real direction has been chosen.
    substitution_direction: (draft.substitutionDirection === ""
      ? "exact"
      : draft.substitutionDirection) as SubstitutionDirection,
    applies_to_category: options.categorySlug === "" ? null : options.categorySlug,
    plausible_min: numberOrNull(draft.plausibleMin),
    plausible_max: numberOrNull(draft.plausibleMax),
    choices:
      draft.valueType === "enum"
        ? filledChoices(draft).map((choice, index) => ({
            key: choice.key.trim(),
            // A blank label is the key, rather than a blank cell in the filter
            // panel: the key is at least always meaningful.
            label: choice.label.trim() === "" ? choice.key.trim() : choice.label.trim(),
            aliases: splitAliases(choice.aliases),
            sort_order: index,
          }))
        : [],
    on_name_conflict: draft.onNameConflict,
    client_op_id: options.clientOpId,
  };
}

// ---------------------------------------------------------------- editing ----

/**
 * A saved field, back as a draft.
 *
 * The options come back as their own rows with their own ids, so they are *not*
 * editable through this draft: adding and removing one are separate requests
 * (`POST`/`DELETE .../choices`) because a delete has a real refusal behind it —
 * parts filed under an option cannot lose it silently. So the draft carries the
 * options only so the form can show what is already there.
 */
export function draftFromField(field: ParameterFieldRead): FieldDraft {
  return {
    name: field.name,
    displayName: field.display_name,
    valueType: field.value_type as ValueType,
    baseUnit: field.base_unit ?? "",
    substitutionDirection: field.substitution_direction as SubstitutionDirection,
    plausibleMin: field.plausible_min === null ? "" : String(field.plausible_min),
    plausibleMax: field.plausible_max === null ? "" : String(field.plausible_max),
    choices: (field.choices ?? []).map((choice) => ({
      key: choice.key,
      label: choice.label,
      aliases: choice.aliases.join(", "),
    })),
    onNameConflict: "fail",
  };
}

/**
 * Which of a saved field's columns this user cannot change, and the reason to
 * show against each.
 *
 * Two independent freezes, and they are worth telling apart on screen because
 * one of them lifts and the other never does: a **seeded** field's identity is
 * frozen forever (the MPN decoders name it), while `value_type` and `base_unit`
 * are frozen only *while parts hold values* — clearing those values makes the
 * field editable again, which is a thing a user can actually do.
 */
export interface FrozenColumns {
  readonly name: string | null;
  readonly valueType: string | null;
  readonly baseUnit: string | null;
}

export function frozenColumns(field: ParameterFieldRead): FrozenColumns {
  const seeded =
    "Part of the shared field library. Its name, type and quantity are frozen — the MPN " +
    "decoders and every saved search name this field.";
  const held =
    field.value_count === 1
      ? "One part holds a value for this field."
      : `${field.value_count} parts hold a value for this field.`;
  const inUse = `${held} Changing this would leave those values in place meaning something else, so it is a data migration rather than an edit.`;
  return {
    name: field.is_seed ? seeded : null,
    valueType: field.is_seed ? seeded : field.value_count > 0 ? inUse : null,
    baseUnit: field.is_seed ? seeded : field.value_count > 0 ? inUse : null,
  };
}

/**
 * The PATCH body: **only what actually changed**.
 *
 * A diff rather than the whole draft, because the server refuses a frozen column
 * the moment it is *assigned* rather than only when it differs — `rename_template`
 * checks the seed freeze before it compares. Sending an unchanged `name` back on a
 * seeded field would therefore be refused as `seed_immutable` for an edit the user
 * never made, against a form they only used to fix a typo in the display name.
 */
export function toFieldUpdateRequest(
  draft: FieldDraft,
  original: ParameterFieldRead,
  options: { categorySlug: string | null; clientOpId: string },
): ParameterFieldUpdate {
  const request: Record<string, unknown> = { client_op_id: options.clientOpId };
  const numeric = draft.valueType === "numeric";

  if (draft.name.trim() !== original.name) {
    request["name"] = draft.name.trim();
  }
  if (draft.displayName.trim() !== original.display_name) {
    request["display_name"] = draft.displayName.trim();
  }
  if (draft.valueType !== original.value_type) {
    request["value_type"] = draft.valueType;
  }
  const unit = numeric ? draft.baseUnit.trim() : null;
  if (unit !== (original.base_unit ?? null)) {
    request["base_unit"] = unit;
  }
  if (draft.substitutionDirection !== original.substitution_direction) {
    request["substitution_direction"] = draft.substitutionDirection;
  }
  const category = options.categorySlug === "" ? null : options.categorySlug;
  if (category !== (original.applies_to_category ?? null)) {
    request["applies_to_category"] = category;
  }
  const low = numberOrNull(draft.plausibleMin);
  const high = numberOrNull(draft.plausibleMax);
  if (low !== original.plausible_min) {
    request["plausible_min"] = low;
  }
  if (high !== original.plausible_max) {
    request["plausible_max"] = high;
  }
  return request as ParameterFieldUpdate;
}

/** True when nothing but the idempotency key would be sent. */
export function isEmptyUpdate(request: ParameterFieldUpdate): boolean {
  return Object.keys(request).filter((key) => key !== "client_op_id").length === 0;
}
