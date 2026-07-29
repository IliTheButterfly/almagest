/**
 * The pictogram a container is drawn with — the cheap half of "what does this
 * look like", the other half being the photo `ContainerPhoto` renders (see that
 * component's docstring for the split and why the dense tree never loads one).
 *
 * Deliberately the *sibling* of `lib/locations/views.ts`, not a copy of it: this
 * module only ever answers "what character represents this glyph name", because
 * `effective_glyph` — the instance override, else the container type's, else
 * `null` — is already resolved server-side (`app.services.glyphs`) for exactly
 * the reason `effective_child_view` is: the fallback would otherwise need
 * `container_types` fetched and joined again on this side.
 *
 * `null`/unknown both mean "no glyph" here, and that is a real answer rather
 * than a loading state: unlike `child_view`, there is no derivation underneath
 * a glyph, so a container with neither its own nor its type's is a container
 * nobody has picked a picture for yet.
 */

import type { ContainerGlyph } from "../api/client";

/** A single character per glyph — cheap enough to sit inside a slot label's
 * own cell in a dense grid without a second network fetch. */
export const GLYPH_SYMBOLS: Readonly<Record<ContainerGlyph, string>> = {
  box: "📦",
  bin: "🗑",
  drawer: "🗄",
  cabinet: "🚪",
  shelf: "📚",
  tray: "🍱",
  bag: "👜",
  reel: "🧵",
  room: "🏠",
  rack: "🎛",
};

export const GLYPH_LABELS: Readonly<Record<ContainerGlyph, string>> = {
  box: "Box",
  bin: "Bin",
  drawer: "Drawer",
  cabinet: "Cabinet",
  shelf: "Shelf",
  tray: "Tray",
  bag: "Bag",
  reel: "Reel",
  room: "Room",
  rack: "Rack",
};

/**
 * The character to render for a resolved glyph name, or `null` for "draw the
 * neutral placeholder instead" — which covers both "no glyph was ever chosen"
 * and "a newer build named one this bundle has never heard of". The second
 * case is the other half of the no-`CHECK` promise: `container_types.glyph`
 * and `locations.glyph` carry no constraint, so a row can legally hold a name
 * this bundle predates, and the honest response is the same placeholder an
 * unset glyph gets — never a crash.
 */
export function glyphSymbol(glyph: string | null): string | null {
  if (glyph === null || !(glyph in GLYPH_SYMBOLS)) {
    return null;
  }
  return GLYPH_SYMBOLS[glyph as ContainerGlyph];
}

export function glyphLabel(glyph: string | null): string | null {
  if (glyph === null || !(glyph in GLYPH_LABELS)) {
    return null;
  }
  return GLYPH_LABELS[glyph as ContainerGlyph];
}

/** Every glyph a picker can offer, in the fixed display order above. */
export const ALL_GLYPHS: readonly ContainerGlyph[] = Object.keys(
  GLYPH_LABELS,
) as ContainerGlyph[];
