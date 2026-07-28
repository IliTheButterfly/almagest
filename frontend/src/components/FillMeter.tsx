/**
 * Fill state, drawn honestly.
 *
 * Four states, and the two that are usually conflated are the ones that matter:
 *
 * - **`ratio === null` is "not measured", not "empty".** It means the container
 *   has no capacity model — no slot count, no volume, no grid — so there is
 *   nothing to be a fraction of. Drawing that as a 0% bar would invent a fact.
 * - **`is_overfull` is a real state.** Capacity in this system is advisory: an
 *   over-capacity put-away is accepted and flagged, never refused, because a
 *   rejected scan teaches the user to stop scanning. So "over" has to be
 *   renderable, and loudly.
 *
 * Each state carries a shape *and* a word as well as a colour — a dashed empty
 * track, a solid bar, a diagonal hatch — because the palette's blue and pink are
 * a luminance match and nothing here may depend on hue alone.
 */

export type FillLevel = "unknown" | "normal" | "high" | "over";

/** Where "nearly full" starts. Advisory, like everything about capacity. */
const HIGH_WATER = 0.9;

export function fillLevel(ratio: number | null, overfull: boolean): FillLevel {
  if (overfull) {
    return "over";
  }
  if (ratio === null) {
    return "unknown";
  }
  return ratio >= HIGH_WATER ? "high" : "normal";
}

export function FillMeter({
  ratio,
  overfull,
  showText = true,
}: {
  ratio: number | null;
  overfull: boolean;
  showText?: boolean | undefined;
}) {
  const level = fillLevel(ratio, overfull);
  const percent = ratio === null ? null : Math.round(ratio * 100);
  const width = ratio === null ? 0 : Math.min(100, Math.max(0, ratio * 100));

  const label =
    level === "over"
      ? `over capacity${percent === null ? "" : ` — ${percent}% of it`}`
      : level === "unknown"
        ? "no capacity model for this container, so fill is not measured"
        : `${percent}% full`;

  return (
    <span className="fill-meter" title={label}>
      <span className={`fill-bar ${level}`} role="img" aria-label={label}>
        {level !== "unknown" && <span style={{ width: `${level === "over" ? 100 : width}%` }} />}
      </span>
      {showText && (
        <span className={`fill-text ${level}`} aria-hidden="true">
          {level === "over" ? "! over" : level === "unknown" ? "n/m" : `${percent}%`}
        </span>
      )}
    </span>
  );
}
