/**
 * Upload, replace or remove one container's own photo.
 *
 * Shared by `ContainerTypeScreen` and `LocationScreen`. The mechanics are
 * identical either way — downscale in the browser (`downscaleForUpload`, since
 * the API deliberately never processes an uploaded image), then upload with
 * `role: "photo"`, and remove by detaching — so this component owns them once;
 * only *which* entity to attach to, and what note to show above the picture,
 * differ between the two callers. A location's photo can fall back to its
 * container type's; a type's photo has nothing to fall back to.
 */

import { useState } from "react";

import type { DocumentRead } from "../lib/api/client";
import { downscaleForUpload } from "../lib/images/resize";
import { ErrorBanner } from "./Feedback";
import { ContainerPhoto } from "./ContainerPhoto";

export interface ContainerPhotoPanelProps {
  /** What is actually shown right now — this container's own photo if it has
   * one, else (for a location) its container type's, else null. */
  readonly displayPhoto: DocumentRead | null;
  /** This entity's *own* photo specifically, so "remove" only ever offers to
   * remove something that was actually attached here. */
  readonly ownPhoto: DocumentRead | null;
  /** The glyph to fall back to if there is no photo to show at all. */
  readonly glyph: string | null;
  readonly note?: string | null;
  readonly onUpload: (file: File) => Promise<void>;
  /** `null` when there is nothing of this entity's own to remove. */
  readonly onRemoveOwn: (() => Promise<void>) | null;
}

export function ContainerPhotoPanel({
  displayPhoto,
  ownPhoto,
  glyph,
  note = null,
  onUpload,
  onRemoveOwn,
}: ContainerPhotoPanelProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function handleFile(file: File): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      // Never blocks on failure to resize — see `downscaleForUpload`'s own
      // docstring. Worst case this uploads exactly what the camera produced.
      const upload = await downscaleForUpload(file);
      await onUpload(upload);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  async function remove(): Promise<void> {
    if (onRemoveOwn === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onRemoveOwn();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <ContainerPhoto photo={displayPhoto} glyph={glyph} alt="" size="card" />
      {note !== null && (
        <p className="muted-note" style={{ margin: 0 }}>
          {note}
        </p>
      )}
      <ErrorBanner error={error} fallback="That photo could not be saved." />
      <label className="field">
        <span>{ownPhoto === null ? "Add a photo" : "Replace this photo"}</span>
        <input
          type="file"
          accept="image/png,image/jpeg"
          disabled={busy}
          onChange={(event) => {
            const file = event.target.files?.[0];
            // Cleared so picking the same file again still fires `onChange`.
            event.target.value = "";
            if (file !== undefined) {
              void handleFile(file);
            }
          }}
        />
      </label>
      {ownPhoto !== null && onRemoveOwn !== null && (
        <button type="button" className="danger" disabled={busy} onClick={() => void remove()}>
          {busy ? "Removing…" : "Remove this photo"}
        </button>
      )}
      {busy && <p className="dim">Saving…</p>}
    </div>
  );
}
