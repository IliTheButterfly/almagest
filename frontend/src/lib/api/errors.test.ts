import { describe, expect, it } from "vitest";

import { ApiError } from "./client";
import { describeError, problemOf, REASON_HINTS } from "./errors";

describe("surfacing the server's reason code", () => {
  it("says megafarads are not a thing for an implausible capacitance", () => {
    // The worked example. `1M` under capacitance is syntactically perfect and
    // physically absurd, and the reason code is what lets the UI say so.
    const error = new ApiError(
      "search failed",
      { detail: { template: "capacitance", reason: "implausible", message: "1e+06 is outside…" } },
      422,
    );
    const report = describeError(error);

    expect(report.reason).toBe("implausible");
    expect(report.template).toBe("capacitance");
    expect(report.status).toBe(422);
    expect(report.headline).toContain("mega");
    // The server's own wording is kept, not discarded.
    expect(report.detail).toBe("1e+06 is outside…");
  });

  it("distinguishes a unit of the wrong quantity from garbage", () => {
    const error = new ApiError("search failed", { detail: { reason: "unit_mismatch" } }, 422);
    expect(describeError(error).headline).toContain("wrong physical quantity");
  });

  it("passes the server's message through for a code it has no hint for", () => {
    // The failure to avoid is a stale friendly message contradicting a backend that
    // has moved on, so an unknown code shows the truth rather than a guess.
    const error = new ApiError(
      "failed",
      { detail: { reason: "some_future_code", message: "the server's own words" } },
      409,
    );
    const report = describeError(error);
    expect(report.headline).toBe("the server's own words");
    expect(report.reason).toBe("some_future_code");
    expect(report.detail).toBeNull();
  });

  it("reads a ledger refusal on a 409", () => {
    const error = new ApiError(
      "could not undo",
      { detail: { reason: "already_reversed", message: "already reversed by seq [7]" } },
      409,
    );
    expect(describeError(error).headline).toBe(REASON_HINTS["already_reversed"]);
  });

  it("handles a bare string detail", () => {
    const error = new ApiError("failed", { detail: "no such template 'wibble'" }, 400);
    expect(describeError(error).headline).toBe("no such template 'wibble'");
  });

  it("handles Pydantic's list-of-errors shape", () => {
    const error = new ApiError(
      "failed",
      { detail: [{ loc: ["body", "qty_milli"], msg: "Input should be greater than 0", type: "x" }] },
      422,
    );
    expect(describeError(error).headline).toBe("Input should be greater than 0");
  });

  it("falls back on a network failure, which carries no detail at all", () => {
    expect(describeError(new TypeError("Failed to fetch")).headline).toBe("Failed to fetch");
    expect(describeError(null, "Nothing was saved.").headline).toBe("Nothing was saved.");
  });

  it("returns no problem for something that is not an ApiError", () => {
    expect(problemOf(new Error("boom"))).toBeNull();
  });
});

describe("the layout change guard's affected-slot list", () => {
  it("extracts every blocked slot from a slots_hold_content 409", () => {
    const error = new ApiError(
      "could not reapply that layout",
      {
        detail: {
          reason: "slots_hold_content",
          message: "some slots hold content",
          affected_slots: [
            { location_id: 41, slot_label: "B2", reasons: ["has_stock"] },
            { location_id: 42, slot_label: "B3", reasons: ["has_tag", "has_children"] },
          ],
        },
      },
      409,
    );
    const report = describeError(error);
    expect(report.reason).toBe("slots_hold_content");
    expect(report.headline).toContain("Move their contents");
    expect(report.affectedSlots).toEqual([
      { locationId: 41, slotLabel: "B2", reasons: ["has_stock"] },
      { locationId: 42, slotLabel: "B3", reasons: ["has_tag", "has_children"] },
    ]);
  });

  it("is null for a refusal that carries no such list", () => {
    const error = new ApiError(
      "refused",
      { detail: { reason: "slot_identity_reinterpreted", message: "would reinterpret A1" } },
      422,
    );
    const report = describeError(error);
    expect(report.reason).toBe("slot_identity_reinterpreted");
    expect(report.affectedSlots).toBeNull();
  });

  it("tolerates a malformed entry rather than throwing", () => {
    const error = new ApiError(
      "could not reapply that layout",
      {
        detail: {
          reason: "slots_hold_content",
          affected_slots: [{ location_id: "not a number", slot_label: "B2" }, null, 42],
        },
      },
      409,
    );
    expect(describeError(error).affectedSlots).toBeNull();
  });
});
