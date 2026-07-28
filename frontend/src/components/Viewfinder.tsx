/**
 * The camera panel, and the explanation that replaces it when there is no camera.
 *
 * The rectangle drawn over the preview is the same centre ROI the decoder reads,
 * so aiming is honest: if the label is outside the box it is not being decoded.
 */

import type { RefObject } from "react";

import type { ScannerStatus } from "../lib/scan/useScanner";
import { Notice } from "./Feedback";

export interface ViewfinderProps {
  readonly videoRef: RefObject<HTMLVideoElement | null>;
  readonly status: ScannerStatus;
  readonly message: string | null;
  /** Rendered when the platform has no camera at all. */
  readonly unavailableNotice: string | null;
  readonly hint?: string | undefined;
}

const STATUS_TEXT: Readonly<Record<ScannerStatus, string>> = {
  off: "Camera off",
  unavailable: "No camera available",
  starting: "Opening the camera…",
  live: "Hold one label inside the box",
  error: "Camera error",
};

export function Viewfinder({
  videoRef,
  status,
  message,
  unavailableNotice,
  hint,
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

  return (
    <div className="stack">
      <div className="viewfinder">
        <video ref={videoRef} playsInline muted autoPlay />
        <div className="roi" />
        <div className="status">{hint ?? STATUS_TEXT[status]}</div>
      </div>
      {status === "error" && message !== null && <Notice kind="error">{message}</Notice>}
    </div>
  );
}
