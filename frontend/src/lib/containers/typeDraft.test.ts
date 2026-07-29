/**
 * The container-type draft's translation to the wire.
 *
 * The interesting cases are all about *absence*: a nullable numeric column whose
 * box the user emptied has to reach the API as `null`, not as `0` and not as
 * `NaN`, and `child_view` has to be sent as an explicit null rather than omitted
 * — the server keys off `model_fields_set`, so an omitted field means "leave it
 * alone" and the choice "go back to deriving it" would be unsayable.
 */

import { describe, expect, it } from "vitest";

import {
  BLANK_DRAFT,
  describeOccupies,
  describePresents,
  draftFromType,
  draftProblems,
  numberOf,
  slugify,
  toCreateRequest,
  toUpdateRequest,
  zeroPadOf,
  type TypeDraft,
} from "./typeDraft";
import type { ContainerTypeRead } from "../api/client";

function type(overrides: Partial<ContainerTypeRead> = {}): ContainerTypeRead {
  return {
    id: 3,
    slug: "gridfinity-bin-2x1x6",
    display_name: "Gridfinity bin 2x1x6u",
    description: null,
    child_layout: "list",
    child_view: null,
    effective_child_view: "list",
    glyph: null,
    photo: null,
    grid_rows: null,
    grid_cols: null,
    grid_pitch_mm: 42,
    grid_height_unit_mm: 7,
    footprint_cols: 2,
    footprint_rows: 1,
    footprint_height_u: 6,
    slot_label_scheme: "sequential",
    slot_label_params: { zero_pad: 2 },
    materialize_slots: false,
    capacity_model: "volume",
    capacity_slots: null,
    max_parts_per_slot: null,
    inner_length_mm: 80,
    inner_width_mm: 38.5,
    inner_height_mm: 37,
    default_fill_factor: 0.85,
    full_threshold: 0.9,
    esd_safe: null,
    is_placeable: true,
    max_item_dimension_mm: null,
    allowed_part_kinds: null,
    front_width_mm: null,
    front_height_mm: null,
    is_seed: true,
    ...overrides,
  };
}

describe("numberOf", () => {
  it("reads a blank box as null rather than zero", () => {
    // The whole reason the draft holds strings: a nullable column and a numeric
    // input disagree about what "empty" is, and `0` is a legal grid pitch-adjacent
    // value in enough places that guessing it would be silent corruption.
    expect(numberOf("")).toBeNull();
    expect(numberOf("   ")).toBeNull();
  });

  it("reads nonsense as null rather than NaN", () => {
    expect(numberOf("four")).toBeNull();
    expect(numberOf("4.2.1")).toBeNull();
  });

  it("keeps a real measurement, decimals included", () => {
    expect(numberOf("42")).toBe(42);
    expect(numberOf(" 41.5 ")).toBe(41.5);
  });
});

describe("slugify", () => {
  it("suggests a slug from a name", () => {
    expect(slugify("Raaco C8-30")).toBe("raaco-c8-30");
    expect(slugify("  Akro-Mils 10144  ")).toBe("akro-mils-10144");
  });

  it("stays inside the column's 128 characters", () => {
    expect(slugify("x".repeat(200))).toHaveLength(128);
  });
});

describe("draftFromType", () => {
  it("keeps the two ADR 0002 answers apart", () => {
    const draft = draftFromType(type());
    // A bin presents no grid of its own and occupies 2x1 of its parent's. If
    // these ever crossed over, a bin would claim to offer two cells it has not got.
    expect(draft.presentsRows).toBe("");
    expect(draft.presentsCols).toBe("");
    expect(draft.occupiesCols).toBe("2");
    expect(draft.occupiesRows).toBe("1");
    expect(draft.occupiesHeightU).toBe("6");
  });

  it("seeds the view picker from the override, never from the resolved value", () => {
    // Seeding it from `effective_child_view` would turn every save into a pin of
    // whatever the geometry happened to derive to, quietly ending the derivation.
    const draft = draftFromType(type({ child_view: null, effective_child_view: "list" }));
    expect(draft.childView).toBe("");

    const pinned = draftFromType(type({ child_view: "grid_cells", effective_child_view: "grid_cells" }));
    expect(pinned.childView).toBe("grid_cells");
  });

  it("reads the zero-pad width out of the free-JSON label params", () => {
    expect(zeroPadOf({ zero_pad: 2 })).toBe("2");
    expect(zeroPadOf(null)).toBe("");
    expect(zeroPadOf({ something_else: true })).toBe("");
  });
});

describe("toUpdateRequest", () => {
  it("writes each of ADR 0002's two answers to its own columns", () => {
    // The mutation this catches is the one the ADR was written against: a
    // "size" that feeds both the grid a container offers and the footprint it
    // occupies. A bin that offers 3 dividers does not take up 3 plate cells.
    const body = toUpdateRequest(
      {
        ...BLANK_DRAFT,
        displayName: "Bin",
        presentsRows: "1",
        presentsCols: "3",
        occupiesCols: "2",
        occupiesRows: "1",
        occupiesHeightU: "6",
      },
      "op-1",
    );
    expect(body.grid_rows).toBe(1);
    expect(body.grid_cols).toBe(3);
    expect(body.footprint_cols).toBe(2);
    expect(body.footprint_rows).toBe(1);
    expect(body.footprint_height_u).toBe(6);
  });

  it("sends an emptied box as null, so the column is actually cleared", () => {
    const draft: TypeDraft = { ...draftFromType(type()), occupiesHeightU: "", innerWidthMm: "" };
    const body = toUpdateRequest(draft, "op-1");
    expect(body.footprint_height_u).toBeNull();
    expect(body.inner_width_mm).toBeNull();
  });

  it("sends child_view as an explicit null rather than omitting it", () => {
    const body = toUpdateRequest({ ...BLANK_DRAFT, childView: "" }, "op-1");
    expect("child_view" in body).toBe(true);
    expect(body.child_view).toBeNull();
  });

  it("sends the glyph as an explicit null too — there is no derivation under it", () => {
    const body = toUpdateRequest({ ...BLANK_DRAFT, glyph: "" }, "op-1");
    expect("glyph" in body).toBe(true);
    expect(body.glyph).toBeNull();
  });

  it("writes only the one label param it knows about", () => {
    expect(toUpdateRequest({ ...BLANK_DRAFT, slotLabelZeroPad: "3" }, "op").slot_label_params).toEqual({
      zero_pad: 3,
    });
    expect(toUpdateRequest({ ...BLANK_DRAFT, slotLabelZeroPad: "" }, "op").slot_label_params).toBeNull();
  });
});

describe("toCreateRequest", () => {
  it("carries the slug, which has no PATCH counterpart at all", () => {
    const body = toCreateRequest({ ...BLANK_DRAFT, displayName: "  My box ", slug: " my-box " }, "op");
    expect(body.slug).toBe("my-box");
    expect(body.display_name).toBe("My box");
    expect(body.client_op_id).toBe("op");
  });
});

describe("draftProblems", () => {
  it("wants a name, and a slug only where one can be chosen", () => {
    expect(draftProblems(BLANK_DRAFT, { requireSlug: true })).toEqual([
      "Give it a name.",
      "Give it a slug — the short, permanent id used in URLs.",
    ]);
    expect(draftProblems({ ...BLANK_DRAFT, displayName: "x" }, { requireSlug: false })).toEqual([]);
  });

  it("catches a span of zero, which the server bounds at one", () => {
    const problems = draftProblems(
      { ...BLANK_DRAFT, displayName: "x", slug: "x", presentsRows: "0" },
      { requireSlug: true },
    );
    expect(problems).toHaveLength(1);
    expect(problems[0]).toContain("must be a whole number of 1 or more");
  });

  it("catches a fractional cell count, and allows a fractional millimetre", () => {
    const fractionalCells = draftProblems(
      { ...BLANK_DRAFT, displayName: "x", slug: "x", presentsCols: "2.5" },
      { requireSlug: true },
    );
    expect(fractionalCells).toHaveLength(1);

    const fractionalPitch = draftProblems(
      { ...BLANK_DRAFT, displayName: "x", slug: "x", presentsPitchMm: "41.5" },
      { requireSlug: true },
    );
    expect(fractionalPitch).toEqual([]);
  });

  it("treats a blank as fine everywhere — every one of these columns is nullable", () => {
    expect(draftProblems({ ...BLANK_DRAFT, displayName: "x", slug: "x" }, { requireSlug: true })).toEqual(
      [],
    );
  });

  it("rejects a measurement of zero, which the server bounds above zero", () => {
    const problems = draftProblems(
      { ...BLANK_DRAFT, displayName: "x", slug: "x", innerHeightMm: "0" },
      { requireSlug: true },
    );
    expect(problems).toHaveLength(1);
    expect(problems[0]).toContain("above zero");
  });
});

describe("the list summaries", () => {
  it("says what a type offers and what it takes up as two separate sentences", () => {
    expect(describeOccupies(type())).toBe("takes up 2 x 1, 6u tall");
    expect(describePresents(type())).toBe("no grid declared");

    const baseplate = type({
      grid_rows: 4,
      grid_cols: 6,
      grid_pitch_mm: 42,
      footprint_cols: null,
      footprint_rows: null,
      footprint_height_u: null,
      child_layout: "grid",
    });
    expect(describePresents(baseplate)).toBe("offers 4 x 6 at 42 mm pitch");
    expect(describeOccupies(baseplate)).toBe("takes up nothing measured");
  });

  it("does not call a container that holds nothing a missing grid", () => {
    expect(describePresents(type({ child_layout: "none" }))).toBe("holds nothing");
  });
});
