/**
 * Turning the API's machine-readable refusals into something a human can act on.
 *
 * Every non-success body in this API is shaped `{reason, message}` — plus
 * `template` on a search filter failure. The `reason` code is the contract; the
 * `message` is the server's prose. This module maps the codes we can say
 * something genuinely better about, and otherwise passes the server's own message
 * through rather than replacing it with "invalid input".
 *
 * The motivating case is `implausible`: a bare `1M` under capacitance is
 * syntactically fine and physically absurd, and "megafarads aren't a thing" tells
 * the user what to do next in a way no generic validation message can.
 */

import { ApiError } from "./client";

/**
 * One slot a layout change could not delete — `app.services.layout_authoring
 * .AffectedSlot`, carried on the 409 a guarded `reapply-layout` returns.
 * `reasons` is `has_stock` / `has_tag` / `has_children`, plural because a slot
 * can be blocked for more than one of them at once.
 */
export interface AffectedSlotProblem {
  readonly locationId: number;
  readonly slotLabel: string;
  readonly reasons: readonly string[];
}

export interface ProblemDetail {
  readonly reason: string | null;
  readonly message: string | null;
  /** The `parameter_template.name` a search filter failed against. */
  readonly template: string | null;
  /** Only present on the layout change guard's 409 (`reason ===
   * "slots_hold_content"`); `null` for every other refusal. */
  readonly affectedSlots: readonly AffectedSlotProblem[] | null;
}

export interface ErrorReport {
  readonly headline: string;
  /** The server's own wording, when the headline came from our own table. */
  readonly detail: string | null;
  readonly reason: string | null;
  readonly template: string | null;
  readonly status: number | null;
  readonly affectedSlots: readonly AffectedSlotProblem[] | null;
}

/**
 * Reason codes worth rewriting.
 *
 * Deliberately partial. A code missing from here falls through to the server's
 * message, which is always at least accurate — the failure mode to avoid is a
 * stale friendly message that contradicts a backend that has moved on.
 */
export const REASON_HINTS: Readonly<Record<string, string>> = {
  // --- value parser, via the search filter executor -----------------------
  implausible:
    "That value is outside the physically plausible range for this parameter. " +
    "Case matters: a bare `1M` means one mega-, so under capacitance it reads as " +
    "megafarads, which are not a thing — `1u`, `1uF` or `1000n` is probably what " +
    "was meant.",
  unit_mismatch:
    "That is a real unit, but of the wrong physical quantity. Usually the value " +
    "is filed under the wrong parameter rather than mistyped.",
  unknown_unit: "No registered quantity uses that unit symbol.",
  unknown_quantity: "This parameter's base unit is not one the value parser knows.",
  syntax:
    "That does not parse. The shorthand grammar takes a scalar (`4k7`, `0R22`, " +
    "`100nF`), a range (`20-30uF`), or a comparison (`>=50V`).",
  empty: "Enter a value.",
  inverted_range: "The low end of that range is above the high end.",
  ambiguous_range:
    "More than one thing in that text could be the range separator, so it is " +
    "refused rather than guessed at. Write it as `20-30uF`.",
  unknown_choice: "Not one of this parameter's choices, or any of their aliases.",
  empty_choice: "Pick at least one choice.",

  // --- parts / locations ---------------------------------------------------
  duplicate_mpn:
    "A part with that manufacturer part number already exists. Add stock to it " +
    "rather than creating a second row for the same part.",
  unknown_part_kind: "Not a known part kind.",

  // --- authoring container types, and stamping containers out of them ------
  // `duplicate_slug` is checked before the insert rather than caught after it,
  // so it is a clean 409 about one field instead of a 500 — and the slug is the
  // one field with no PATCH counterpart, so it is worth saying that it is
  // permanent while the user is still choosing it.
  duplicate_slug:
    "Another container type already uses that slug. A slug is permanent — it is the " +
    "one field that cannot be changed later — so pick a different one, or clone the " +
    "existing type instead of writing a second like it.",
  bad_naming_pattern:
    "Only {n} can be filled in in a name pattern, and it is replaced with the number " +
    "of each container. Any other braces are refused rather than guessed at, so the " +
    "thirty drawers you asked for do not all end up called the same thing.",
  unknown_container_type: "That container type no longer exists — it may have been renamed or removed.",
  unknown_parent: "The container you were putting these inside no longer exists.",

  // --- the one hard geometric refusal (app.services.capacity) -------------
  // Everything else about capacity in this system is advisory: an over-capacity
  // put-away is accepted and flagged. These three are not, and the wording has
  // to say why they are different rather than reading as another warning.
  pitch_mismatch:
    "The grid pitches do not match, so these physically will not seat — a 42 mm bin " +
    "does not sit on a 50 mm plate. This is refused rather than flagged, unlike being " +
    "over capacity: it would record a world that cannot exist.",
  footprint_too_wide:
    "This takes up more columns than the container you are putting it in offers. " +
    "Refused rather than flagged, because it does not physically fit.",
  footprint_too_deep:
    "This takes up more rows than the container you are putting it in offers. " +
    "Refused rather than flagged, because it does not physically fit.",

  // --- ledger refusals (409) ----------------------------------------------
  same_location: "Source and destination are the same place, so there is nothing to move.",
  same_lot: "A lot cannot be split into itself.",
  zero_delta: "An adjustment of zero records nothing.",
  negative_count: "A physical count cannot be negative.",
  non_positive_quantity: "Enter a quantity above zero.",
  not_found: "There is nothing left to undo here.",
  is_a_reversal: "That row is itself an undo. Undoing an undo is a fresh movement, not a reversal.",
  already_reversed: "That movement has already been undone.",
  moved_since:
    "The lot has moved since that movement, so reversing it now would put stock " +
    "somewhere it no longer is. Move it back by hand instead.",
  no_source: "That movement records no source location, so there is nowhere to move back to.",
  no_lot: "That ledger row has no lot left to compensate.",

  // --- printed ids ---------------------------------------------------------
  short_id_taken:
    "That code is already on another container. It is refused rather than swapped " +
    "for a free one, because the code is already printed — a substitute would " +
    "leave the label and the database permanently disagreeing.",
  check:
    "That code fails its own check symbol, so it was mistyped or misread. The last " +
    "character is a checksum over the other seven, which is what catches a single " +
    "wrong symbol or two swapped ones.",
  length: "A short id is eight symbols, written as four and four.",
  alphabet:
    "That contains a symbol the alphabet excludes. `I`, `L` and `O` are read as " +
    "`1`, `1` and `0`; `U` is not used at all.",

  // --- alias binding -------------------------------------------------------
  empty_code: "That payload normalises to nothing, so there is no key to bind it under.",
  code_too_long: "That payload is too long to use as a binding key.",
  unknown_target: "The thing you are binding this code to does not exist.",

  // --- removing a container (app.services.removal) ------------------------
  // The panel normally shows the *preview*, which carries the same reasons as
  // structured data and names the contents. These cover the 409 the DELETE itself
  // returns when the world changed between the preview and the confirmation —
  // somebody put stock in the drawer while it was open — where the server's own
  // message is the specific one and these say what kind of thing happened.
  holds_stock:
    "There is still stock in there. Move it somewhere else first: nothing here " +
    "relocates stock on its own, because where it goes is a movement in the ledger " +
    "and your decision.",
  has_children:
    "There are containers inside this one. Removing them too is a separate, " +
    "explicit confirmation — a cabinet is never emptied as a side effect of " +
    "removing the cabinet.",
  ancestor_retired:
    "The container this one sits in was removed as well, so restoring this alone " +
    "would bring it back invisible inside something invisible. Restore that one " +
    "first; it brings everything under it back with it.",
  not_retired: "This container has not been removed, so there is nothing to bring back.",

  // --- layout authoring change guard (app.services.layout_authoring) ------
  // `slots_hold_content` is the "guarded" outcome: distinct from a plain
  // refusal because it names exactly what is in the way and is expected to
  // be resolved and retried, not abandoned. The headline states that; the
  // affected-slot list (see AffectedSlotProblem) is what says *which* ones.
  slots_hold_content:
    "Some of the slots this change would remove still hold stock or a bound tag. " +
    "Move their contents to another location first, then save again.",
  slot_identity_reinterpreted:
    "That would reuse an existing slot's label at a different position, which " +
    "would silently redefine what a printed card, a tag or a lot already thinks " +
    "that label means. Delete the old slot and create a new one instead.",
  not_contiguous: "That merge target cuts across an existing slot instead of covering it exactly.",
  gap_in_region: "That merge target is not exactly covered by existing slots — there is a gap.",
  overlap: "Two slots in this layout overlap. Only contiguous, non-overlapping rectangles are legal.",
  duplicate_label: "Two slots in this layout share the same label.",
  invalid_span: "A slot cannot have a zero or negative span.",
  out_of_bounds: "That slot extends past the edge of the grid.",
  missing_slot_label:
    "Every slot needs an explicit label when editing a container's own layout — " +
    "unlike a type's canvas, an instance has no generator to fall back on.",
};

function readString(source: Record<string, unknown>, key: string): string | null {
  const value = source[key];
  return typeof value === "string" && value !== "" ? value : null;
}

/**
 * `detail.affected_slots`, if the shape matches. Tolerant rather than
 * strict — a field missing or mistyped here should read as "no list" rather
 * than throw, since the headline and reason still carry the refusal either
 * way.
 */
function readAffectedSlots(source: Record<string, unknown>): AffectedSlotProblem[] | null {
  const raw = source["affected_slots"];
  if (!Array.isArray(raw)) {
    return null;
  }
  const slots: AffectedSlotProblem[] = [];
  for (const entry of raw) {
    if (entry === null || typeof entry !== "object") {
      continue;
    }
    const record = entry as Record<string, unknown>;
    const locationId = record["location_id"];
    const reasons = record["reasons"];
    if (typeof locationId !== "number" || !Array.isArray(reasons)) {
      continue;
    }
    slots.push({
      locationId,
      slotLabel: readString(record, "slot_label") ?? "",
      reasons: reasons.filter((reason): reason is string => typeof reason === "string"),
    });
  }
  return slots.length > 0 ? slots : null;
}

/**
 * Pull `{reason, message, template}` out of whatever the server sent.
 *
 * Tolerates the two other shapes FastAPI can produce: a bare string `detail`,
 * and the `[{loc, msg, type}]` list a Pydantic validation failure returns.
 */
export function problemOf(error: unknown): ProblemDetail | null {
  if (!(error instanceof ApiError)) {
    return null;
  }
  const body = error.detail;
  if (typeof body === "string") {
    return { reason: null, message: body, template: null, affectedSlots: null };
  }
  if (body === null || typeof body !== "object") {
    return null;
  }

  const detail = (body as { detail?: unknown }).detail ?? body;
  if (typeof detail === "string") {
    return { reason: null, message: detail, template: null, affectedSlots: null };
  }
  if (Array.isArray(detail)) {
    const messages = detail
      .map((entry) =>
        entry !== null && typeof entry === "object"
          ? readString(entry as Record<string, unknown>, "msg")
          : null,
      )
      .filter((message): message is string => message !== null);
    return messages.length === 0
      ? null
      : { reason: null, message: messages.join("; "), template: null, affectedSlots: null };
  }
  if (typeof detail !== "object") {
    return null;
  }

  const record = detail as Record<string, unknown>;
  return {
    reason: readString(record, "reason"),
    message: readString(record, "message"),
    template: readString(record, "template"),
    affectedSlots: readAffectedSlots(record),
  };
}

/** The one function screens call. Always returns something printable. */
export function describeError(error: unknown, fallback = "Something went wrong."): ErrorReport {
  const status = error instanceof ApiError ? error.status : null;
  const problem = problemOf(error);
  const hint = problem?.reason === undefined ? undefined : REASON_HINTS[problem.reason ?? ""];

  if (hint !== undefined) {
    return {
      headline: hint,
      detail: problem?.message ?? null,
      reason: problem?.reason ?? null,
      template: problem?.template ?? null,
      status,
      affectedSlots: problem?.affectedSlots ?? null,
    };
  }

  const headline =
    problem?.message ??
    (error instanceof Error && error.message !== "" ? error.message : null) ??
    fallback;
  return {
    headline,
    detail: null,
    reason: problem?.reason ?? null,
    template: problem?.template ?? null,
    status,
    affectedSlots: problem?.affectedSlots ?? null,
  };
}
