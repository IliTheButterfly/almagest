/**
 * The frozen still, its outlines, and the values you can tap out of them.
 *
 * **Percentages, not pixels.** Every outline is positioned as a percentage of
 * the capture's own dimensions, which is why `width_px`/`height_px` are stored
 * alongside the image. The same overlay then draws correctly on a phone in
 * portrait, on a desktop panel, and on a re-opened capture whose image is being
 * served at whatever size the layout gives it — with no measurement, no resize
 * observer, and nothing to go stale when the container changes.
 *
 * **Selection is explicit and the tap is the acceptance.** Tapping an outline
 * selects it and reveals its chips; tapping a chip copies that value (or fills
 * the field the caller asked for). Nothing is ever copied or filled by merely
 * detecting it. That is not only good manners — for an OCR'd value it is the
 * rule: `CLAUDE.md` forbids auto-accepting a model-read part number, and a
 * deliberate tap on a labelled value is exactly the human decision it asks for.
 *
 * **`read` chips look different from `verified` ones**, and say their
 * confidence. A guessed word and a checksummed payload must never be presented
 * as equally trustworthy, because the user's whole job here is deciding which of
 * the two they are looking at.
 */

import { useEffect, useState } from "react";

import type { ScanResolveResponse } from "../lib/api/client";
import { chipsForRegion, type Chip, type FillField } from "../lib/capture/chips";
import { copyText } from "../lib/capture/copy";
import type { Region, TextStatus } from "../lib/capture/types";
import { bounds } from "../lib/capture/types";

/** How long a chip says "Copied" before going back to its label. */
const COPIED_MS = 1400;

export interface CaptureOverlayProps {
  readonly imageUrl: string;
  readonly width: number;
  readonly height: number;
  readonly regions: readonly Region[];
  readonly resolved: Readonly<Record<number, ScanResolveResponse>>;
  readonly textStatus: TextStatus;
  readonly textMessage?: string | null;
  readonly readingText?: boolean;
  /**
   * When set, chips offer "Use" instead of only copying — this is the capture
   * opened *from* a form field. The field name is what the caller is pointing
   * at, which is what lets an OCR'd line fill it without any guessing on our
   * part.
   */
  readonly fillInto?: { readonly field: FillField; readonly label: string } | undefined;
  readonly onFill?: ((field: FillField, value: string) => void) | undefined;
}

export function CaptureOverlay({
  imageUrl,
  width,
  height,
  regions,
  resolved,
  textStatus,
  textMessage,
  readingText = false,
  fillInto,
  onFill,
}: CaptureOverlayProps) {
  const [selected, setSelected] = useState<number | null>(null);

  // A capture that gains OCR regions seconds after its barcodes must not move
  // the user's selection out from under them — but a *new* capture must clear
  // it, or the selection points at a region from the previous photograph.
  useEffect(() => setSelected(null), [imageUrl]);

  // Clamped rather than trusted: the OCR pass appends regions after a selection
  // may already exist, and a capture can be replaced entirely while one is held.
  const activeRegion = selected === null ? undefined : regions[selected];
  const active = activeRegion === undefined ? null : selected;

  return (
    <div className="capture">
      <div
        className="capture-frame"
        // The intrinsic ratio, so the box is the right shape before the image
        // has loaded and never reflows under the outlines once it has.
        style={{ aspectRatio: `${width} / ${height}` }}
      >
        <img src={imageUrl} alt="The frame you captured" />
        {regions.map((region, index) => {
          const box = bounds(region.quad);
          return (
            <button
              key={`${region.kind}-${index}-${region.text.slice(0, 24)}`}
              type="button"
              className={`capture-region is-${region.kind}${active === index ? " is-selected" : ""}`}
              style={{
                left: `${(box.x / width) * 100}%`,
                top: `${(box.y / height) * 100}%`,
                width: `${(box.width / width) * 100}%`,
                height: `${(box.height / height) * 100}%`,
              }}
              onClick={() => setSelected(active === index ? null : index)}
              aria-pressed={active === index}
              aria-label={
                region.kind === "barcode"
                  ? `${region.symbology} code: ${region.text}`
                  : `Read text: ${region.text}`
              }
            />
          );
        })}
      </div>

      <TextStatusNote status={textStatus} message={textMessage ?? null} reading={readingText} />

      {activeRegion === undefined ? (
        <p className="muted-note" style={{ margin: 0 }}>
          {regions.length === 0
            ? "Nothing was read off this frame yet."
            : `${regions.length} thing${regions.length === 1 ? "" : "s"} read. Tap one to use it.`}
        </p>
      ) : (
        <ChipList
          chips={chipsForRegion(activeRegion, active ?? 0, resolved[active ?? 0])}
          {...(fillInto === undefined ? {} : { fillInto })}
          {...(onFill === undefined ? {} : { onFill })}
        />
      )}
    </div>
  );
}

/**
 * Why there is (or is not) any text.
 *
 * The four states read very differently to a user and collapsing them would be
 * a lie in at least one direction — "no text found" on a phone that could not
 * load the reader is a statement about the label that nobody checked.
 */
function TextStatusNote({
  status,
  message,
  reading,
}: {
  status: TextStatus;
  message: string | null;
  reading: boolean;
}) {
  if (reading) {
    return (
      <p className="muted-note" style={{ margin: 0 }}>
        Reading the text… the codes above are ready now.
      </p>
    );
  }
  if (status === "ok" || status === "not_attempted") {
    return null;
  }
  if (status === "empty") {
    return (
      <p className="muted-note" style={{ margin: 0 }}>
        No readable text in this frame — only what is outlined above.
      </p>
    );
  }
  return (
    <p className="muted-note" style={{ margin: 0 }}>
      {status === "unavailable"
        ? (message ?? "The text reader is not available on this device.")
        : (message ?? "Reading the text failed.")}{" "}
      Any codes above were still read normally.
    </p>
  );
}

function ChipList({
  chips,
  fillInto,
  onFill,
}: {
  chips: readonly Chip[];
  fillInto?: { readonly field: FillField; readonly label: string } | undefined;
  onFill?: ((field: FillField, value: string) => void) | undefined;
}) {
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    if (copied === null) {
      return;
    }
    const timer = setTimeout(() => setCopied(null), COPIED_MS);
    return () => clearTimeout(timer);
  }, [copied]);

  if (chips.length === 0) {
    return (
      <p className="muted-note" style={{ margin: 0 }}>
        Nothing usable in that one.
      </p>
    );
  }

  return (
    <ul className="capture-chips">
      {chips.map((chip) => {
        // A chip fills a field when the caller pointed at one — for a `read`
        // chip that is the *only* way it ever fills anything, since it carries
        // no field of its own. See `chips.ts`.
        const field = fillInto?.field ?? chip.field;
        const canFill = onFill !== undefined && field !== undefined;
        return (
          <li key={chip.id}>
            <div className={`capture-chip is-${chip.kind}`}>
              <span className="capture-chip-label">
                {chip.label}
                {chip.confidence === undefined ? "" : ` · ${chip.confidence}%`}
              </span>
              <span className="capture-chip-value mono">{chip.value}</span>
              <span className="row">
                {chip.href !== undefined && (
                  <a className="capture-chip-action" href={chip.href}>
                    Open
                  </a>
                )}
                {canFill && (
                  <button
                    type="button"
                    className="capture-chip-action"
                    onClick={() => onFill(field, chip.value)}
                  >
                    Use{fillInto === undefined ? "" : ` as ${fillInto.label}`}
                  </button>
                )}
                <button
                  type="button"
                  className="capture-chip-action"
                  onClick={() => {
                    void copyText(chip.value).then((ok) => setCopied(ok ? chip.id : null));
                  }}
                >
                  {copied === chip.id ? "Copied" : "Copy"}
                </button>
              </span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
