/**
 * The container-type form's draft, and the translation to and from the wire.
 *
 * Split out of the form component because this half is where the mistakes are
 * possible and therefore where the tests belong: every numeric column here is
 * nullable, an empty input has to become `null` rather than `0` or `NaN`, and
 * **the two groups must not be conflated** — which is ADR 0002's whole point and
 * the reason the draft names them apart (`presents*` versus `occupies*`) instead
 * of holding one flat bag of dimensions.
 *
 * > | Question | Expressed as |
 * > |---|---|
 * > | What grid do I present to my children? | `grid_rows`, `grid_cols`, `grid_pitch_mm`, `grid_height_unit_mm` |
 * > | What footprint do I occupy in my parent's grid? | `footprint_cols`, `footprint_rows`, `footprint_height_u` |
 *
 * A Gridfinity bin answers both at once and the answers are unrelated: it takes
 * up 2x1 units of the baseplate it sits on **and** offers its own 1x3 grid of
 * dividers. A form that let those two blur into one "size" is a form that gets
 * filled in wrong, so the labels in `ContainerTypeForm` are phrased as the two
 * questions verbatim.
 *
 * Every field is a string, including the numbers. That is deliberate: a
 * controlled numeric input whose state is a `number` cannot represent "the user
 * has cleared the box", so it either snaps to 0 or fights the cursor. The parse
 * happens once, on the way out.
 */

import type {
  CapacityModel,
  ChildLayout,
  ChildView,
  ContainerGlyph,
  ContainerTypeCreate,
  ContainerTypeRead,
  ContainerTypeUpdate,
  SlotLabelScheme,
} from "../api/client";

export interface TypeDraft {
  // --- what it is ---------------------------------------------------------
  readonly slug: string;
  readonly displayName: string;
  readonly description: string;
  readonly glyph: string;

  // --- ADR 0002, question one: the grid it offers its children ------------
  readonly presentsLayout: ChildLayout;
  readonly presentsRows: string;
  readonly presentsCols: string;
  readonly presentsPitchMm: string;
  readonly presentsHeightUnitMm: string;
  readonly slotLabelScheme: SlotLabelScheme;
  /** The `sequential` scheme's zero-pad width, the one `slot_label_params` key
   * this form exposes — a blank leaves the params untouched at null. */
  readonly slotLabelZeroPad: string;

  // --- ADR 0002, question two: the space it takes up in its parent --------
  readonly occupiesCols: string;
  readonly occupiesRows: string;
  readonly occupiesHeightU: string;

  // --- how full counts as full --------------------------------------------
  readonly capacityModel: CapacityModel;
  readonly capacitySlots: string;
  /** The drawer front a label goes on, in mm. Without both of these
   *  `POST /api/labels/sheets` refuses with `missing_front_dimensions` and no
   *  card can be printed for anything of this type — the seeds shipped without
   *  them and there was no way to supply them from here. */
  readonly frontWidthMm: string;
  readonly frontHeightMm: string;
  readonly innerLengthMm: string;
  readonly innerWidthMm: string;
  readonly innerHeightMm: string;

  // --- presentation and placement -----------------------------------------
  /** `""` means "derive it from the geometry above" — a real choice, not an
   * absence, and the only way to say it is an explicit null on the wire. */
  readonly childView: string;
  readonly isPlaceable: boolean;
}

export const BLANK_DRAFT: TypeDraft = {
  slug: "",
  displayName: "",
  description: "",
  glyph: "",
  presentsLayout: "grid",
  presentsRows: "",
  presentsCols: "",
  presentsPitchMm: "",
  presentsHeightUnitMm: "",
  slotLabelScheme: "row_alpha_col_num",
  slotLabelZeroPad: "",
  occupiesCols: "",
  occupiesRows: "",
  occupiesHeightU: "",
  capacityModel: "slots",
  capacitySlots: "",
  frontWidthMm: "",
  frontHeightMm: "",
  innerLengthMm: "",
  innerWidthMm: "",
  innerHeightMm: "",
  childView: "",
  isPlaceable: true,
};

function text(value: number | null | undefined): string {
  return value === null || value === undefined ? "" : String(value);
}

/** The zero-pad width out of `slot_label_params`, tolerantly: the column is free
 * JSON, so anything else in there is left alone rather than coerced. */
export function zeroPadOf(params: Record<string, unknown> | null | undefined): string {
  const raw = params?.["zero_pad"];
  return typeof raw === "number" ? String(raw) : "";
}

export function draftFromType(type: ContainerTypeRead): TypeDraft {
  return {
    slug: type.slug,
    displayName: type.display_name,
    description: type.description ?? "",
    glyph: type.glyph ?? "",
    presentsLayout: type.child_layout as ChildLayout,
    presentsRows: text(type.grid_rows),
    presentsCols: text(type.grid_cols),
    presentsPitchMm: text(type.grid_pitch_mm),
    presentsHeightUnitMm: text(type.grid_height_unit_mm),
    slotLabelScheme: type.slot_label_scheme as SlotLabelScheme,
    slotLabelZeroPad: zeroPadOf(type.slot_label_params),
    occupiesCols: text(type.footprint_cols),
    occupiesRows: text(type.footprint_rows),
    occupiesHeightU: text(type.footprint_height_u),
    capacityModel: type.capacity_model as CapacityModel,
    capacitySlots: text(type.capacity_slots),
    frontWidthMm: text(type.front_width_mm),
    frontHeightMm: text(type.front_height_mm),
    innerLengthMm: text(type.inner_length_mm),
    innerWidthMm: text(type.inner_width_mm),
    innerHeightMm: text(type.inner_height_mm),
    // The raw override, not `effective_child_view`: "" has to mean "derived",
    // and seeding the picker with the derived answer would turn every save into
    // a pin of whatever it happened to derive to at the time.
    childView: type.child_view ?? "",
    isPlaceable: type.is_placeable,
  };
}

/** A blank, `NaN`, or non-finite entry is `null` — never a smuggled zero. */
export function numberOf(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") {
    return null;
  }
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * `Display Name` → `display-name`, bounded to the column's 128 characters.
 *
 * Only a starting suggestion. The slug is the one field with no `PATCH`
 * counterpart, so it is chosen once and lives forever; the form keeps it
 * editable and stops deriving it the moment it is touched.
 */
export function slugify(name: string): string {
  return name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 128);
}

/**
 * The reasons this draft cannot be saved, in the user's words.
 *
 * Checked here as well as by the server, because the server's own answer to a
 * `grid_rows` of `0` is a Pydantic 422 against `GridSpan` — accurate, and about
 * a field name rather than a thing in the world. Nothing in here is a *policy*
 * the server does not also enforce: this is the same bound said earlier.
 */
export function draftProblems(draft: TypeDraft, options: { requireSlug: boolean }): string[] {
  const problems: string[] = [];
  if (draft.displayName.trim() === "") {
    problems.push("Give it a name.");
  }
  if (options.requireSlug && draft.slug.trim() === "") {
    problems.push("Give it a slug — the short, permanent id used in URLs.");
  }

  const spans: readonly (readonly [string, string])[] = [
    ["the grid it offers (rows)", draft.presentsRows],
    ["the grid it offers (columns)", draft.presentsCols],
    ["the space it takes up (columns)", draft.occupiesCols],
    ["the space it takes up (rows)", draft.occupiesRows],
    ["the space it takes up (height)", draft.occupiesHeightU],
    ["how many compartments", draft.capacitySlots],
    ["the label zero-padding", draft.slotLabelZeroPad],
  ];
  for (const [what, raw] of spans) {
    const value = numberOf(raw);
    if (raw.trim() !== "" && (value === null || !Number.isInteger(value) || value < 1)) {
      problems.push(`${what} must be a whole number of 1 or more, or left blank.`);
    }
  }

  const measures: readonly (readonly [string, string])[] = [
    ["the grid pitch", draft.presentsPitchMm],
    ["the height unit", draft.presentsHeightUnitMm],
    ["the inner length", draft.innerLengthMm],
    ["the inner width", draft.innerWidthMm],
    ["the inner height", draft.innerHeightMm],
  ];
  for (const [what, raw] of measures) {
    const value = numberOf(raw);
    if (raw.trim() !== "" && (value === null || value <= 0)) {
      problems.push(`${what} must be a measurement above zero, or left blank.`);
    }
  }
  return problems;
}

/**
 * The fields both `POST` and `PATCH` carry.
 *
 * **Every one is sent explicitly, including the nulls.** `PATCH` keys off
 * Pydantic's `model_fields_set`, so an omitted field means "leave it alone" and
 * an explicit null means "clear it" — and this is a whole form the user has just
 * read, so a box they emptied has to clear the column rather than silently keep
 * the old value. `child_view` is the sharpest case: null is what hands the
 * drawing back to being derived, and omitting it would make that unsayable.
 */
function commonFields(draft: TypeDraft): Omit<ContainerTypeUpdate, "client_op_id" | "device_id"> {
  const zeroPad = numberOf(draft.slotLabelZeroPad);
  return {
    display_name: draft.displayName.trim(),
    description: draft.description.trim() === "" ? null : draft.description.trim(),
    glyph: draft.glyph === "" ? null : (draft.glyph as ContainerGlyph),
    child_layout: draft.presentsLayout,
    child_view: draft.childView === "" ? null : (draft.childView as ChildView),
    grid_rows: numberOf(draft.presentsRows),
    grid_cols: numberOf(draft.presentsCols),
    grid_pitch_mm: numberOf(draft.presentsPitchMm),
    grid_height_unit_mm: numberOf(draft.presentsHeightUnitMm),
    footprint_cols: numberOf(draft.occupiesCols),
    footprint_rows: numberOf(draft.occupiesRows),
    footprint_height_u: numberOf(draft.occupiesHeightU),
    slot_label_scheme: draft.slotLabelScheme,
    // Only ever the one key this form knows about. Sending `{}` would be a
    // claim about the whole blob, which is free JSON the seeds already use.
    slot_label_params: zeroPad === null ? null : { zero_pad: zeroPad },
    capacity_model: draft.capacityModel,
    capacity_slots: numberOf(draft.capacitySlots),
    front_width_mm: numberOf(draft.frontWidthMm),
    front_height_mm: numberOf(draft.frontHeightMm),
    inner_length_mm: numberOf(draft.innerLengthMm),
    inner_width_mm: numberOf(draft.innerWidthMm),
    inner_height_mm: numberOf(draft.innerHeightMm),
    is_placeable: draft.isPlaceable,
  };
}

export function toCreateRequest(draft: TypeDraft, clientOpId: string): ContainerTypeCreate {
  return {
    ...commonFields(draft),
    slug: draft.slug.trim(),
    display_name: draft.displayName.trim(),
    client_op_id: clientOpId,
  };
}

export function toUpdateRequest(draft: TypeDraft, clientOpId: string): ContainerTypeUpdate {
  return { ...commonFields(draft), client_op_id: clientOpId };
}

/** One line summarising what grid a type offers, for a list row. */
export function describePresents(type: ContainerTypeRead): string {
  if (type.grid_rows === null || type.grid_cols === null) {
    return type.child_layout === "none" ? "holds nothing" : "no grid declared";
  }
  const pitch = type.grid_pitch_mm === null ? "" : ` at ${type.grid_pitch_mm} mm pitch`;
  return `offers ${type.grid_rows} x ${type.grid_cols}${pitch}`;
}

/** One line summarising the space a type takes up in its parent. */
export function describeOccupies(type: ContainerTypeRead): string {
  if (type.footprint_cols === null && type.footprint_rows === null) {
    return "takes up nothing measured";
  }
  const cols = type.footprint_cols ?? 1;
  const rows = type.footprint_rows ?? 1;
  const height = type.footprint_height_u === null ? "" : `, ${type.footprint_height_u}u tall`;
  return `takes up ${cols} x ${rows}${height}`;
}
