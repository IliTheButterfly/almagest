/**
 * "What does this container look like" — the picture half, drawn from one of
 * two sources depending on how much of it there is to draw.
 *
 * **A photo** is a real image: what a drawer actually looks like, shot from a
 * phone standing in front of it (`DocumentRole.PHOTO`, `docs/PLAN.md`'s content
 * store). It is the right thing to show once — on that one container's own
 * detail screen (`LocationScreen`, `ContainerTypeScreen`) — and the wrong thing
 * to load ninety-six of. **A glyph** is a single pictogram, cheap enough for
 * every node of `ContainerLayout`'s dense recursive map, where a baseplate can
 * lay out a dozen cells and a cabinet a few dozen drawer fronts in one screen.
 *
 * This component draws whichever one it is given, falling back from a photo to
 * a glyph, and never renders a broken-image icon: `onError` demotes a photo
 * whose bytes 404 (a stale reference, a scrubbed blob) to the same fallback a
 * container with no photo at all gets. What happens when *neither* is
 * available depends on `size`: a container's own screen (`"card"`) gets a
 * visible, dashed placeholder — the natural place to add a picture — while a
 * tile in the dense tree (`"tile"`, the default) renders nothing at all, since
 * a dashed box in every one of ninety-six otherwise-unpictured cells would be
 * noise rather than information.
 */

import { useState } from "react";

import type { DocumentRead } from "../lib/api/client";
import { glyphLabel, glyphSymbol } from "../lib/locations/glyphs";

export interface ContainerPhotoProps {
  /** A real photo, if one is attached — pass `null` to draw the glyph instead. */
  readonly photo?: DocumentRead | null;
  /** The resolved glyph name (`effective_glyph`), or `null` for "none chosen". */
  readonly glyph: string | null;
  readonly alt: string;
  /** `"tile"` is the small size used inline in a cell; `"card"` is the large
   * size used on a container's own detail screen. */
  readonly size?: "tile" | "card";
}

export function ContainerPhoto({ photo = null, glyph, alt, size = "tile" }: ContainerPhotoProps) {
  const [broken, setBroken] = useState(false);

  if (photo !== null && !broken) {
    return (
      <img
        className="container-photo"
        src={photo.url}
        alt={alt}
        loading="lazy"
        onError={() => setBroken(true)}
      />
    );
  }

  const symbol = glyphSymbol(glyph);
  if (symbol !== null) {
    const label = glyphLabel(glyph) ?? alt;
    if (size === "card") {
      return (
        <span className="container-photo-placeholder" role="img" aria-label={label} title={label}>
          {symbol}
        </span>
      );
    }
    return (
      <span className="cell-glyph" role="img" aria-label={label} title={label}>
        {symbol}
      </span>
    );
  }

  // Neither a usable photo nor a recognised glyph. On a container's own
  // detail screen (`size="card"`) that is worth a visible, dashed placeholder
  // — it is where a picture would be *added*. In the dense tree, drawing that
  // same box in every one of ninety-six otherwise-unpictured cells would be
  // noise rather than information, so a tile with nothing to show renders
  // nothing at all — a real, clean absence rather than a hole standing in for
  // a missing image.
  if (size === "card") {
    return (
      <span className="container-photo-placeholder" aria-hidden="true" title="No picture set">
        ?
      </span>
    );
  }
  return null;
}
