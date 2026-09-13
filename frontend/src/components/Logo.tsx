/**
 * The mark: a chest, with the star held in its latch.
 *
 * What makes a chest legible at icon sizes is a lid seam across the upper third
 * and a latch straddling it — not the wood grain, which is gone by 24px. The
 * latch is also the natural home for the star, being the one element already on
 * the centre line.
 *
 * There are deliberately two copies of this drawing. `public/favicon.svg` is
 * loaded by the browser as its own document, so it cannot see our custom
 * properties and has to name its colours literally. This copy is inline in the
 * page, so it can do the right thing instead: the frame and the seam take
 * `currentColor` — which makes the mark track whatever text it sits beside,
 * through both themes, for free — and only the latch names a token. Edits to the
 * geometry have to be made in both places.
 *
 * The star is *cut out* of the latch with `fill-rule="evenodd"` rather than
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
        d="M28,13.5 H36 A3.5,3.5 0 0 1 39.5,17 V31 A3.5,3.5 0 0 1 36,34.5
           H28 A3.5,3.5 0 0 1 24.5,31 V17 A3.5,3.5 0 0 1 28,13.5 Z
           M32 18.1 33.48 22.52 37.9 24 33.48 25.48 32 29.9 30.52 25.48 26.1 24 30.52 22.52Z"
      />
      <g fill="none" stroke="currentColor">
        {/* The lid seam, in two segments that stop either side of the latch. One
            line running underneath would show through the star's hole. Lighter
            than the frame on purpose: weight is the second signal, so the mark
            still reads with hue removed. Lands on straight wall, not on a corner
            curve, so no clip path is needed. */}
        <path strokeWidth="4" d="M5 24H24.5M39.5 24H59" />
        <rect x="5" y="5" width="54" height="54" rx="13" strokeWidth="5" />
      </g>
    </svg>
  );
}
