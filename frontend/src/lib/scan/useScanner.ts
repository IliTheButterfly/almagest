/**
 * Camera lifecycle plus the decode loop, wired to the escalation ladder, the
 * multi-symbol voter and the hold-off.
 *
 * The pieces this composes are each pure and tested on their own
 * (`escalation.ts`, `decoder.ts`, `voting.ts`, `holdoff.ts`, `trackControls.ts`,
 * and `admit.ts` for the way the first four interact per tick);
 * what is left here is the part that needs a real browser —
 * `getUserMedia`, a `<video>`, a `<canvas>`, `MediaStreamTrack` — and is
 * therefore kept as thin as it can be.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { detectCapabilities } from "../capabilities";
import { admitDecoded } from "./admit";
import { cropFrame, decodeImageData, DECODE_PASSES, ESCALATION_LEVELS, FRAME_INTERVAL_MS } from "./decoder";
import { EscalationController, nextDelayMs, runEscalationAttempt } from "./escalation";
import { DECODER_HOLD_OFF_MS, PayloadHoldOff } from "./holdoff";
import {
  applyContinuousFocus,
  safeCapabilities,
  safeSettings,
  setTorch,
  setZoom,
  supportsContinuousFocus,
  torchAvailable as trackHasTorch,
  zoomRange as trackZoomRange,
} from "./trackControls";
import { MultiFrameVoter } from "./voting";

export type ScannerStatus = "off" | "unavailable" | "starting" | "live" | "error";

export interface TorchControl {
  readonly available: boolean;
  readonly enabled: boolean;
  readonly toggle: () => void;
}

export interface ZoomControl {
  readonly available: boolean;
  readonly min: number;
  readonly max: number;
  readonly step: number;
  readonly value: number;
  readonly set: (value: number) => void;
}

export interface Resolution {
  readonly width: number;
  readonly height: number;
}

const TORCH_UNAVAILABLE: TorchControl = { available: false, enabled: false, toggle: () => undefined };
const ZOOM_UNAVAILABLE: ZoomControl = { available: false, min: 0, max: 0, step: 1, value: 0, set: () => undefined };

export interface ScannerHandle {
  readonly videoRef: React.RefObject<HTMLVideoElement | null>;
  readonly status: ScannerStatus;
  /** Set when `status` is `error`; a denied permission, mostly. */
  readonly message: string | null;
  /** Suppress decoding without tearing the camera down. */
  readonly pause: (paused: boolean) => void;
  /**
   * The resolution actually granted by `getUserMedia`, read back from
   * `getSettings()` rather than assumed — a silent fallback to 640×480 must be
   * visible, not just hoped against.
   */
  readonly resolution: Resolution | null;
  readonly torch: TorchControl;
  readonly zoom: ZoomControl;
  /**
   * Name of the decode pass that ran most recently — `roi`, `full-frame` or
   * `hard`, and `null` before the first attempt.
   *
   * Published so the viewfinder can stop drawing an ROI rectangle it is no
   * longer honouring. The overlay used to mean "outside this box is not
   * decoded", which was true of the single-pass decoder and is a lie the
   * moment the ladder escalates to the full frame.
   */
  readonly pass: string | null;
}

export interface ScannerOptions {
  /** Whether the camera should be running at all. */
  readonly active: boolean;
  /** Called once per accepted payload — after voting *and* the hold-off. */
  readonly onDecode: (text: string, symbology: string) => void;
}

export function useScanner({ active, onDecode }: ScannerOptions): ScannerHandle {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [status, setStatus] = useState<ScannerStatus>("off");
  const [message, setMessage] = useState<string | null>(null);
  const [resolution, setResolution] = useState<Resolution | null>(null);
  const [torchAvailableState, setTorchAvailableState] = useState(false);
  const [torchEnabled, setTorchEnabled] = useState(false);
  const [zoomRangeState, setZoomRangeState] = useState<{
    readonly min: number;
    readonly max: number;
    readonly step: number;
  } | null>(null);
  const [zoomValue, setZoomValue] = useState(0);
  // Set once per pass *change*, not once per frame: `useState` bails out of the
  // re-render when the value is identical, and the ladder changes rungs rarely,
  // so this costs nothing at the 10 fps the cheap passes run at.
  const [pass, setPass] = useState<string | null>(null);

  const onDecodeRef = useRef(onDecode);
  onDecodeRef.current = onDecode;

  const trackRef = useRef<MediaStreamTrack | null>(null);

  const pausedRef = useRef(false);
  const pause = useCallback((paused: boolean) => {
    pausedRef.current = paused;
  }, []);

  const toggleTorch = useCallback(() => {
    const track = trackRef.current;
    if (track === null) {
      return;
    }
    const next = !torchEnabled;
    void setTorch(track, next).then((applied) => {
      if (applied) {
        setTorchEnabled(next);
      }
    });
  }, [torchEnabled]);

  const setZoomLevel = useCallback((value: number) => {
    const track = trackRef.current;
    if (track === null) {
      return;
    }
    void setZoom(track, value).then((applied) => {
      if (applied) {
        setZoomValue(value);
      }
    });
  }, []);

  const torch: TorchControl = torchAvailableState
    ? { available: true, enabled: torchEnabled, toggle: toggleTorch }
    : TORCH_UNAVAILABLE;

  const zoom: ZoomControl =
    zoomRangeState === null
      ? ZOOM_UNAVAILABLE
      : {
          available: true,
          min: zoomRangeState.min,
          max: zoomRangeState.max,
          step: zoomRangeState.step,
          value: zoomValue,
          set: setZoomLevel,
        };

  useEffect(() => {
    if (!active) {
      setStatus("off");
      setResolution(null);
      setTorchAvailableState(false);
      setTorchEnabled(false);
      setZoomRangeState(null);
      setPass(null);
      trackRef.current = null;
      return;
    }
    if (!detectCapabilities().camera) {
      // Not an error: over plain HTTP the API does not exist at all, and the
      // caller renders an explanation plus the manual path.
      setStatus("unavailable");
      return;
    }

    const escalation = new EscalationController(ESCALATION_LEVELS);
    const voter = new MultiFrameVoter();
    // The decoder's hold-off pushes its window forward on every sighting, so a
    // label parked in front of the lens fires once rather than once per window.
    const holdOff = new PayloadHoldOff(DECODER_HOLD_OFF_MS, { refreshWhileSuppressed: true });
    const canvas = document.createElement("canvas");

    let stream: MediaStream | null = null;
    let attached: HTMLVideoElement | null = null;
    let track: MediaStreamTrack | null = null;
    let live = true;
    // `ReturnType` rather than `number`: this file is type-checked with Node's
    // globals in scope as well as the DOM's, and the two disagree about what
    // `setTimeout` hands back.
    let timer: ReturnType<typeof globalThis.setTimeout> | undefined;

    async function tick(): Promise<void> {
      if (!live) {
        return;
      }
      const video = videoRef.current;
      let delayMs: number = FRAME_INTERVAL_MS;

      if (video !== null && !pausedRef.current && video.readyState >= 2) {
        try {
          const attempt = await runEscalationAttempt(escalation, async (level) => {
            const pass = DECODE_PASSES[level];
            if (pass === undefined) {
              return null;
            }
            const frame = cropFrame(video, canvas, pass.roiFraction);
            if (frame === null) {
              return null;
            }
            const decoded = await decodeImageData(frame, pass);
            return decoded.length > 0 ? decoded : null;
          });

          const admission = admitDecoded(escalation, voter, holdOff, attempt.result ?? []);
          for (const symbol of admission.report) {
            onDecodeRef.current(symbol.text, symbol.symbology);
          }

          setPass(attempt.levelName);

          const level = ESCALATION_LEVELS[attempt.level];
          if (level !== undefined) {
            delayMs = nextDelayMs(level, attempt.elapsedMs);
          }
        } catch {
          // A single bad frame is not worth reporting; the wasm module throwing
          // repeatedly would show up as "nothing ever decodes", which is the
          // honest symptom anyway.
        }
      }

      if (live) {
        timer = globalThis.setTimeout(() => void tick(), delayMs);
      }
    }

    setStatus("starting");
    setMessage(null);
    setResolution(null);
    setTorchAvailableState(false);
    setTorchEnabled(false);
    setZoomRangeState(null);
    setPass(null);

    navigator.mediaDevices
      .getUserMedia({
        video: {
          // The rear camera on a phone; ignored on a laptop with one webcam.
          facingMode: { ideal: "environment" },
          // A dense QR needs real pixels: the previous 1280×720 ideal, cropped
          // to a 60% ROI, decoded from roughly 768×432 — below what ZXing
          // needs once there is any motion blur. `ideal` degrades gracefully
          // on hardware that cannot deliver it.
          width: { ideal: 1920 },
          height: { ideal: 1080 },
        },
      })
      .then(async (opened) => {
        stream = opened;
        if (!live) {
          return;
        }
        const video = videoRef.current;
        if (video === null) {
          return;
        }
        video.srcObject = opened;
        // Held for the cleanup, which must detach the stream from the element it
        // was actually attached to rather than from whatever the ref points at by
        // then.
        attached = video;
        await video.play();
        if (!live) {
          return;
        }
        setStatus("live");

        track = opened.getVideoTracks()[0] ?? null;
        trackRef.current = track;
        if (track !== null) {
          const settings = safeSettings(track);
          if (typeof settings.width === "number" && typeof settings.height === "number") {
            // A scanner that silently fell back to a low resolution is the bug
            // this whole pass exists to fix, so the granted resolution is read
            // back from `getSettings()` and rendered — not logged to a console
            // nobody has open on a phone.
            setResolution({ width: settings.width, height: settings.height });
          }

          const capabilities = safeCapabilities(track);
          if (supportsContinuousFocus(capabilities)) {
            void applyContinuousFocus(track);
          }

          if (trackHasTorch(capabilities)) {
            setTorchAvailableState(true);
            setTorchEnabled(false);
          }

          const range = trackZoomRange(capabilities);
          if (range !== null) {
            setZoomRangeState(range);
            setZoomValue(typeof settings.zoom === "number" ? settings.zoom : range.min);
          }
        }

        void tick();
      })
      .catch((cause: unknown) => {
        if (!live) {
          return;
        }
        setStatus("error");
        setMessage(
          cause instanceof Error && cause.name === "NotAllowedError"
            ? "Camera permission was refused. Grant it in the browser's site settings, or type the code instead."
            : `The camera could not be opened: ${cause instanceof Error ? cause.message : String(cause)}`,
        );
      });

    return () => {
      live = false;
      if (timer !== undefined) {
        globalThis.clearTimeout(timer);
      }
      if (attached !== null) {
        attached.srcObject = null;
      }
      for (const streamTrack of stream?.getTracks() ?? []) {
        streamTrack.stop();
      }
    };
  }, [active]);

  return { videoRef, status, message, pause, resolution, torch, zoom, pass };
}
