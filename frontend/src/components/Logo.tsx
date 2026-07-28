/**
 * The mark: a drawer of identical compartments, and the one that was found.
 *
 * There are deliberately two copies of this drawing. `public/favicon.svg` is
 * loaded by the browser as its own document, so it cannot see our custom
 * properties and has to name its colours literally. This copy is inline in the
 * page, so it can do the right thing instead: the frame and the divisions take
 * `currentColor` — which makes the mark track whatever text it sits beside,
 * through both themes, for free — and only the lit cell names a token. Edits to
 * the geometry have to be made in both places.
 *
 * The star is *cut out* of the lit cell with `fill-rule="evenodd"` rather than
 * painted in the background colour. The header behind it is a blue-to-pink
 * gradient wash, so there is no single background colour available to paint
 * with; a hole is correct on any ground, including a one-ink label print.
 *
 * Sized by `.app-header .brand svg` in styles.css, following the rule that this
 * project keeps its dimensions in the stylesheet and not in the components.
 *
 * `aria-hidden` is correct here: every current use sits next to the word
 * "Almagest", so naming the image too would make a screen reader say it twice.
 */
export function Logo() {
  return (
    <svg viewBox="0 0 64 64" aria-hidden="true" focusable="false">
      <path
        fill="var(--accent-2)"
        fillRule="evenodd"
        d="M23 23H41V41H23Z
           M32 24.8 33.77 30.23 39.2 32 33.77 33.77 32 39.2 30.23 33.77 24.8 32 30.23 30.23Z"
      />
      <g fill="none" stroke="currentColor">
        {/* Divisions, thinner than the frame: weight is the second signal, so
            the mark still reads when hue is removed. They end on the frame's
            straight runs, so no clip path is needed. */}
        <path strokeWidth="2.5" d="M23 5V59M41 5V59M5 23H59M5 41H59" />
        <rect x="5" y="5" width="54" height="54" rx="13" strokeWidth="5" />
      </g>
    </svg>
  );
}
