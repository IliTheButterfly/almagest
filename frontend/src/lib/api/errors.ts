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

export interface ProblemDetail {
  readonly reason: string | null;
  readonly message: string | null;
  /** The `parameter_template.name` a search filter failed against. */
  readonly template: string | null;
}

export interface ErrorReport {
  readonly headline: string;
  /** The server's own wording, when the headline came from our own table. */
  readonly detail: string | null;
  readonly reason: string | null;
  readonly template: string | null;
  readonly status: number | null;
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
};

function readString(source: Record<string, unknown>, key: string): string | null {
  const value = source[key];
  return typeof value === "string" && value !== "" ? value : null;
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
    return { reason: null, message: body, template: null };
  }
  if (body === null || typeof body !== "object") {
    return null;
  }

  const detail = (body as { detail?: unknown }).detail ?? body;
  if (typeof detail === "string") {
    return { reason: null, message: detail, template: null };
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
      : { reason: null, message: messages.join("; "), template: null };
  }
  if (typeof detail !== "object") {
    return null;
  }

  const record = detail as Record<string, unknown>;
  return {
    reason: readString(record, "reason"),
    message: readString(record, "message"),
    template: readString(record, "template"),
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
  };
}
