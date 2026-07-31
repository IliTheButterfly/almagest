/**
 * The field draft's rules and its translation to the wire.
 *
 * Every case here is a *silent* failure if it goes wrong, which is why they are
 * tests rather than manual checks:
 *
 * - a numeric field with a blank unit is creatable, appears in the filter panel,
 *   and then refuses every value forever;
 * - a list field with no options matches nothing while looking like a working
 *   filter;
 * - `substitution_direction` defaulted rather than answered makes substitution
 *   search wrong for every rating in the catalogue, with nothing on screen to
 *   show it;
 * - and the PATCH body has to be a *diff*, because the server refuses a frozen
 *   column the moment it is assigned — so echoing an unchanged `name` back would
 *   be refused as `seed_immutable` for an edit nobody made.
 */

import { describe, expect, it } from "vitest";

import {
  BLANK_FIELD_DRAFT,
  anchorForReason,
  draftFromField,
  fieldDraftProblems,
  fieldKey,
  frozenColumns,
  isEmptyUpdate,
  splitAliases,
  toFieldCreateRequest,
  toFieldUpdateRequest,
  type FieldDraft,
} from "./fieldDraft";
import type { ParameterFieldRead } from "../api/client";

function field(overrides: Partial<ParameterFieldRead> = {}): ParameterFieldRead {
  return {
    id: 12,
    name: "capacitance",
    display_name: "Capacitance",
    value_type: "numeric",
    base_unit: "farad",
    substitution_direction: "range_overlap",
    applies_to_category: "capacitors",
    sort_order: 10,
    plausible_min: 1e-12,
    plausible_max: 1,
    inherited: false,
    is_seed: false,
    value_count: 0,
    choices: [],
    ...overrides,
  };
}

/** A valid numeric draft, so each test can break exactly one thing. */
const ESR: FieldDraft = {
  ...BLANK_FIELD_DRAFT,
  name: "esr",
  displayName: "ESR",
  valueType: "numeric",
  baseUnit: "ohm",
  substitutionDirection: "lower_ok",
  choices: [],
};

describe("fieldKey", () => {
  it("makes a snake_case filter key, because that is what every shipped field is", () => {
    expect(fieldKey("Voltage rating")).toBe("voltage_rating");
    expect(fieldKey("  ESR (100 kHz) ")).toBe("esr_100_khz");
  });

  it("strips accents rather than emitting them into a search URL", () => {
    expect(fieldKey("Résistance")).toBe("resistance");
  });
});

describe("fieldDraftProblems", () => {
  it("accepts a complete numeric field", () => {
    expect(fieldDraftProblems(ESR)).toEqual([]);
  });

  it("refuses a numeric field with no unit, anchored to the unit control", () => {
    const problems = fieldDraftProblems({ ...ESR, baseUnit: "" });
    expect(problems.map((problem) => problem.anchor)).toContain("baseUnit");
  });

  it("refuses a unit on a field that is not a number", () => {
    const problems = fieldDraftProblems({
      ...ESR,
      valueType: "text",
      baseUnit: "ohm",
    });
    expect(problems.map((problem) => problem.anchor)).toContain("baseUnit");
  });

  it("refuses an unanswered substitution question — there is no safe default", () => {
    const problems = fieldDraftProblems({ ...ESR, substitutionDirection: "" });
    expect(problems.map((problem) => problem.anchor)).toContain("substitutionDirection");
  });

  it("refuses a list field with no options, which would match nothing", () => {
    const problems = fieldDraftProblems({
      ...ESR,
      valueType: "enum",
      baseUnit: "",
      choices: [{ key: "", label: "", aliases: "" }],
    });
    expect(problems.map((problem) => problem.anchor)).toContain("choices");
  });

  it("refuses two options sharing a key", () => {
    const problems = fieldDraftProblems({
      ...ESR,
      valueType: "enum",
      baseUnit: "",
      choices: [
        { key: "c0g", label: "C0G", aliases: "" },
        { key: "C0G", label: "again", aliases: "" },
      ],
    });
    expect(problems.map((problem) => problem.message)).toContain("Two options share the same key.");
  });

  it("refuses a sanity window that no value can fall in", () => {
    const problems = fieldDraftProblems({ ...ESR, plausibleMin: "10", plausibleMax: "1" });
    expect(problems.map((problem) => problem.anchor)).toContain("plausible");
  });
});

describe("toFieldCreateRequest", () => {
  it("sends the unit only for a numeric field, and the category as a slug", () => {
    const request = toFieldCreateRequest(ESR, {
      categorySlug: "capacitors",
      clientOpId: "op-1",
    });
    expect(request).toMatchObject({
      name: "esr",
      display_name: "ESR",
      value_type: "numeric",
      base_unit: "ohm",
      substitution_direction: "lower_ok",
      applies_to_category: "capacitors",
      choices: [],
      on_name_conflict: "fail",
      client_op_id: "op-1",
    });
  });

  it("drops the unit when the type is not numeric, which the API refuses outright", () => {
    const request = toFieldCreateRequest(
      { ...ESR, valueType: "bool", baseUnit: "ohm" },
      { categorySlug: null, clientOpId: "op-2" },
    );
    expect(request.base_unit).toBeNull();
    expect(request.applies_to_category).toBeNull();
  });

  it("keeps only filled options, labels a blank one by its key, and orders them as typed", () => {
    const request = toFieldCreateRequest(
      {
        ...ESR,
        valueType: "enum",
        baseUnit: "",
        choices: [
          { key: "c0g", label: "", aliases: "np0, cog" },
          { key: "", label: "ignored", aliases: "" },
          { key: "x7r", label: "X7R", aliases: "" },
        ],
      },
      { categorySlug: "capacitors", clientOpId: "op-3" },
    );
    expect(request.choices).toEqual([
      { key: "c0g", label: "c0g", aliases: ["np0", "cog"], sort_order: 0 },
      { key: "x7r", label: "X7R", aliases: [], sort_order: 1 },
    ]);
  });

  it("sends an empty sanity window as null rather than as zero", () => {
    const request = toFieldCreateRequest(ESR, { categorySlug: null, clientOpId: "op-4" });
    expect(request.plausible_min).toBeNull();
    expect(request.plausible_max).toBeNull();
  });
});

describe("splitAliases", () => {
  it("splits on commas and newlines, trims, and drops blanks", () => {
    expect(splitAliases("0603, 1608\n\n , RC0603")).toEqual(["0603", "1608", "RC0603"]);
  });
});

describe("draftFromField", () => {
  it("round-trips a saved field, with the numbers back as strings", () => {
    const draft = draftFromField(field());
    expect(draft).toMatchObject({
      name: "capacitance",
      displayName: "Capacitance",
      valueType: "numeric",
      baseUnit: "farad",
      substitutionDirection: "range_overlap",
      plausibleMax: "1",
    });
    // Not "" — an absent window and a window of zero are different answers.
    expect(draft.plausibleMin).not.toBe("");
  });

  it("leaves an unset sanity bound as an empty box rather than as a zero", () => {
    const draft = draftFromField(field({ plausible_min: null, plausible_max: null }));
    expect(draft.plausibleMin).toBe("");
    expect(draft.plausibleMax).toBe("");
  });
});

describe("frozenColumns", () => {
  it("freezes nothing on a field of your own that nothing uses yet", () => {
    expect(frozenColumns(field())).toEqual({ name: null, valueType: null, baseUnit: null });
  });

  it("freezes the type and quantity — but not the name — once parts hold values", () => {
    const frozen = frozenColumns(field({ value_count: 4 }));
    expect(frozen.name).toBeNull();
    expect(frozen.valueType).toContain("4 parts");
    expect(frozen.baseUnit).toContain("data migration");
  });

  it("freezes all three identity columns on a shipped field, whatever its use count", () => {
    const frozen = frozenColumns(field({ is_seed: true, value_count: 0 }));
    expect(frozen.name).toContain("shared field library");
    expect(frozen.valueType).toContain("shared field library");
    expect(frozen.baseUnit).toContain("shared field library");
  });
});

describe("toFieldUpdateRequest", () => {
  const original = field({ is_seed: true, value_count: 9 });

  it("sends only what changed, so an untouched frozen column is never assigned", () => {
    const draft = { ...draftFromField(original), displayName: "Capacitance (nominal)" };
    const request = toFieldUpdateRequest(draft, original, {
      categorySlug: original.applies_to_category,
      clientOpId: "op-5",
    });
    expect(request).toEqual({
      client_op_id: "op-5",
      display_name: "Capacitance (nominal)",
    });
    expect(request).not.toHaveProperty("name");
    expect(request).not.toHaveProperty("value_type");
    expect(request).not.toHaveProperty("base_unit");
  });

  it("reports a no-op edit as empty rather than sending a bare idempotency key", () => {
    const request = toFieldUpdateRequest(draftFromField(original), original, {
      categorySlug: original.applies_to_category,
      clientOpId: "op-6",
    });
    expect(isEmptyUpdate(request)).toBe(true);
  });

  it("clears a sanity bound as an explicit null, which is a real edit", () => {
    const draft = { ...draftFromField(original), plausibleMax: "" };
    const request = toFieldUpdateRequest(draft, original, {
      categorySlug: original.applies_to_category,
      clientOpId: "op-7",
    });
    expect(request.plausible_max).toBeNull();
    expect(isEmptyUpdate(request)).toBe(false);
  });

  it("sends a null category when a field is made global", () => {
    const request = toFieldUpdateRequest(draftFromField(original), original, {
      categorySlug: null,
      clientOpId: "op-8",
    });
    expect(request.applies_to_category).toBeNull();
  });
});

describe("anchorForReason", () => {
  it("places the server's refusals against the control that caused them", () => {
    expect(anchorForReason("unknown_base_unit")).toBe("baseUnit");
    expect(anchorForReason("duplicate_name")).toBe("name");
    expect(anchorForReason("namespace_needs_category")).toBe("category");
  });

  it("falls through to the banner for a code this build has never heard of", () => {
    expect(anchorForReason("something_new")).toBeNull();
    expect(anchorForReason(null)).toBeNull();
  });
});
