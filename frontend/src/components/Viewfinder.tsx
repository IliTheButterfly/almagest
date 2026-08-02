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
 * laptop webcam is neither. The third knob, the half turn, is offered
 * everywhere: no camera reports which way up its bracket holds it, so it is the
 * one setting the user has to tell us — see `lib/scan/orientation.ts`.
 */

import type { RefObject } from "react";
import { useCallback, useState } from "react";

import { DECODE_PASSES } from "../lib/scan/decoder";
import type { CameraRotation, RotationStore } from "../lib/scan/orientation";
import {
  defaultRotationStore,
  flipRotation,
  readCameraRotation,
  writeCameraRotation,
} from "../lib/scan/orientation";
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
  /**
   * Where the half-turn setting is remembered. Defaults to `localStorage`;
   * injectable so a test does not have to reach for a real one.
   */
  readonly rotationStore?: RotationStore | null | undefined;
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
  rotationStore,
}: ViewfinderProps) {
  const rotation = useCameraRotation(rotationStore);

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
        <video
          ref={videoRef}
          playsInline
          muted
          autoPlay
          className={rotation.value === 180 ? "is-half-turned" : undefined}
        />
        <div
          className={`roi${roiBinding ? "" : " is-advisory"}`}
          style={{ inset: `${(inset.y * 100).toFixed(2)}% ${(inset.x * 100).toFixed(2)}%` }}
        />
        <div className="status">{caption}</div>
      </div>
      {status === "error" && message !== null && <Notice kind="error">{message}</Notice>}
      {live && camera !== undefined && <CameraTuning camera={camera} rotation={rotation} />}
    </div>
  );
}

/**
 * The remembered half turn, plus the toggle.
 *
 * State lives here rather than in a prop the callers thread through, because it
 * is a property of the machine the browser is running on and every caller would
 * pass the same thing. `useState`'s lazy initialiser reads storage once per
 * mount, not once per render.
 */
interface RotationControl {
  readonly value: CameraRotation;
  readonly flip: () => void;
}

function useCameraRotation(store: RotationStore | null | undefined): RotationControl {
  const [resolved] = useState<RotationStore | null>(() =>
    store === undefined ? defaultRotationStore() : store,
  );
  const [value, setValue] = useState<CameraRotation>(() => readCameraRotation(resolved));

  const flip = useCallback(() => {
    setValue((current) => {
      const next = flipRotation(current);
      writeCameraRotation(resolved, next);
      return next;
    });
  }, [resolved]);

  return { value, flip };
}

function CameraTuning({
  camera,
  rotation,
}: {
  readonly camera: CameraControls;
  readonly rotation: RotationControl;
}) {
  const { resolution, torch, zoom } = camera;

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
      {/* Always offered, unlike torch and zoom: those are advertised by the
       * track, and which way up a bracket holds the camera is not something any
       * track can advertise. The station's webcam is mounted head-down, so this
       * is the control that makes it usable at all. */}
      <button
        type="button"
        aria-pressed={rotation.value === 180}
        onClick={rotation.flip}
        title="Turn the preview half way round, for a camera mounted upside down"
      >
        {rotation.value === 180 ? "Upside-down mount" : "Upright mount"}
      </button>
    </div>
  );
}
