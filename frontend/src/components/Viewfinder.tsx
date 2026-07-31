/**
 * The camera panel, and the explanation that replaces it when there is no camera.
 *
 * Two things this has to be honest about, now that the decoder is no longer a
 * single fixed pass:
 *
 * - **The rectangle is only a promise while the cheap ROI pass is running.**
 *   The escalation ladder falls back to the full frame when the ROI pass keeps
 *   missing, so a box that always claimed "outside this is not decoded" would be
 *   wrong exactly when it matters most — when the user has failed to centre the
 *   label and is being helped anyway. The overlay therefore follows
 *   `camera.pass`, and the caption says which of the two is true right now.
 * - **The resolution actually granted.** A silent `getUserMedia` fallback to
 *   640×480 is the difference between a dense QR that reads and one that never
 *   will, and it is invisible from the picture. It is read back from
 *   `getSettings()` and printed.
 *
 * Torch and zoom are offered only where the track reports them, which on a
 * laptop webcam is neither.
 */

import type { RefObject } from "react";

import { DECODE_PASSES } from "../lib/scan/decoder";
import { roiOverlayInset } from "../lib/scan/roi";
import type { ScannerHandle, ScannerStatus } from "../lib/scan/useScanner";
import { Notice } from "./Feedback";

/**
 * The tuning half of {@link ScannerHandle}, kept as one prop so a caller passes
 * the handle it already holds (`camera={scanner}`) rather than being edited
 * again every time the hook learns a new knob.
 */
export type CameraControls = Pick<ScannerHandle, "resolution" | "torch" | "zoom" | "pass">;

export interface ViewfinderProps {
  readonly videoRef: RefObject<HTMLVideoElement | null>;
  readonly status: ScannerStatus;
  readonly message: string | null;
  /** Rendered when the platform has no camera at all. */
  readonly unavailableNotice: string | null;
  readonly hint?: string | undefined;
  /** Omit to render the preview with no controls and no diagnostics. */
  readonly camera?: CameraControls | undefined;
}

const STATUS_TEXT: Readonly<Record<ScannerStatus, string>> = {
  off: "Camera off",
  unavailable: "No camera available",
  starting: "Opening the camera…",
  live: "Hold one label inside the box",
  error: "Camera error",
};

/** What the box means, per rung of the ladder. */
const PASS_TEXT: Readonly<Record<string, string>> = {
  roi: "Hold one label inside the box",
  "full-frame": "Reading the whole frame — the box is only a suggestion",
  hard: "Reading the whole frame slowly, every symbology",
};

function passText(pass: string | null): string | null {
  if (pass === null) {
    return null;
  }
  return PASS_TEXT[pass] ?? null;
}

export function Viewfinder({
  videoRef,
  status,
  message,
  unavailableNotice,
  hint,
  camera,
}: ViewfinderProps) {
  if (status === "unavailable") {
    return (
      <Notice kind="warn" title="Scanning is not available here">
        <p style={{ margin: 0 }}>
          {unavailableNotice ??
            "This browser exposes no camera. Type the code instead — every scanning path has a manual equivalent."}
        </p>
      </Notice>
    );
  }

  const live = status === "live";
  const pass = live ? (camera?.pass ?? null) : null;
  // Only the cheap pass is confined to the box. Once the ladder has escalated,
  // the rectangle is a suggestion, so it is drawn as one.
  const roiBinding = pass === null || pass === "roi";
  const caption = hint ?? passText(pass) ?? STATUS_TEXT[status];
  // Drawn from the resolution the camera actually granted, because `object-fit:
  // cover` crops the frame's long axis out of the picture: a fixed inset marks
  // the wrong region on every camera whose aspect ratio is not the box's.
  const inset = roiOverlayInset(
    camera?.resolution?.width ?? 0,
    camera?.resolution?.height ?? 0,
    DECODE_PASSES[0]?.roiFraction,
  );

  return (
    <div className="stack">
      <div className="viewfinder">
        <video ref={videoRef} playsInline muted autoPlay />
        <div
          className={`roi${roiBinding ? "" : " is-advisory"}`}
          style={{ inset: `${(inset.y * 100).toFixed(2)}% ${(inset.x * 100).toFixed(2)}%` }}
        />
        <div className="status">{caption}</div>
      </div>
      {status === "error" && message !== null && <Notice kind="error">{message}</Notice>}
      {live && camera !== undefined && <CameraTuning camera={camera} />}
    </div>
  );
}

function CameraTuning({ camera }: { readonly camera: CameraControls }) {
  const { resolution, torch, zoom } = camera;
  if (resolution === null && !torch.available && !zoom.available) {
    return null;
  }

  return (
    <div className="camera-tuning">
      {resolution !== null && (
        // Spelled out rather than badged: "1920×1080" and "640×480" have to be
        // told apart at a glance by someone wondering why nothing decodes.
        <span className="camera-tuning-readout">
          Camera {resolution.width}×{resolution.height}
        </span>
      )}
      {torch.available && (
        <button type="button" aria-pressed={torch.enabled} onClick={torch.toggle}>
          {torch.enabled ? "Torch on" : "Torch off"}
        </button>
      )}
      {zoom.available && (
        <label className="camera-tuning-zoom">
          Zoom
          <input
            type="range"
            min={zoom.min}
            max={zoom.max}
            step={zoom.step}
            value={zoom.value}
            onChange={(event) => zoom.set(Number(event.target.value))}
          />
        </label>
      )}
    </div>
  );
}
