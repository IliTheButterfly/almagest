/**
 * `naming_pattern`, previewed before it is sent.
 *
 * `POST /api/locations/{id}/instantiate` hands the pattern to Python's
 * `str.format` with exactly one keyword, `n`. Anything else in braces — a typo, a
 * stray `{name}`, an unbalanced brace — raises there, and the route turns that
 * into a 422 `bad_naming_pattern` rather than the bare 500 it used to be. So the
 * server is the authority and its refusal is always surfaced.
 *
 * This module exists because "Drawer {n}" typed into a box gives no clue what
 * thirty containers will end up called, and the answer is cheap to show. It
 * restates the server's two rules:
 *
 * - `{n}` is replaced with the 1-based index;
 * - a count above 1 with **no** `{n}` gets ` {n}` appended, so instances stay
 *   distinguishable rather than thirty rows sharing one name.
 *
 * `namingProblem` is a preview of the same refusal, not a second policy: it
 * reports what the server will reject, and the form still shows the server's own
 * message if one gets through.
 */

/** The one substitution the server performs, and the only brace pair allowed. */
const N_PLACEHOLDER = /\{n\}/g;

/**
 * Why the server will refuse this pattern, or `null` if it will accept it.
 *
 * Deliberately conservative: it only flags braces, because that is the entire
 * class of input `str.format` can fail on. Everything else is a name.
 */
export function namingProblem(pattern: string): string | null {
  const withoutN = pattern.replace(N_PLACEHOLDER, "");
  if (withoutN.includes("{") || withoutN.includes("}")) {
    return (
      "Only {n} can be filled in, and every brace has to be part of it. " +
      "Remove the other braces — the server refuses the whole pattern rather " +
      "than guessing what they meant."
    );
  }
  return null;
}

/**
 * What the containers will actually be called, in order.
 *
 * Returns an empty list for a pattern the server would refuse, so a caller shows
 * the problem instead of a preview built on a rule that will not run.
 */
export function previewNames(pattern: string, count: number): string[] {
  if (namingProblem(pattern) !== null || count < 1) {
    return [];
  }
  const effective =
    pattern.includes("{n}") || count === 1 ? pattern : `${pattern} {n}`;
  const names: string[] = [];
  for (let n = 1; n <= count; n += 1) {
    names.push(effective.replace(N_PLACEHOLDER, String(n)));
  }
  return names;
}

/** The preview, shortened for a one-line hint: first two, an ellipsis, the last. */
export function summariseNames(names: readonly string[]): string {
  if (names.length === 0) {
    return "";
  }
  if (names.length <= 3) {
    return names.join(", ");
  }
  return `${names[0]}, ${names[1]}, … ${names[names.length - 1]}`;
}
