/**
 * Camera lifecycle plus the decode loop, wired to the voter and the hold-off.
 *
 * The pieces this composes are each pure and tested on their own
 * (`voting.ts`, `holdoff.ts`, `roi.ts`); what is left here is the part that needs
 * a real browser — `getUserMedia`, a `<video>`, a `<canvas>` — and is therefore
 * kept as thin as it can be.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { detectCapabilities } from "../capabilities";
import { cropFrame, decodeImageData, FRAME_INTERVAL_MS } from "./decoder";
import { DECODER_HOLD_OFF_MS, PayloadHoldOff } from "./holdoff";
import { FrameVoter } from "./voting";

export type ScannerStatus = "off" | "unavailable" | "starting" | "live" | "error";

export interface ScannerHandle {
  readonly videoRef: React.RefObject<HTMLVideoElement | null>;
  readonly status: ScannerStatus;
  /** Set when `status` is `error`; a denied permission, mostly. */
  readonly message: string | null;
  /** Suppress decoding without tearing the camera down. */
  readonly pause: (paused: boolean) => void;
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

  const onDecodeRef = useRef(onDecode);
  onDecodeRef.current = onDecode;

  const pausedRef = useRef(false);
  const pause = useCallback((paused: boolean) => {
    pausedRef.current = paused;
  }, []);

  useEffect(() => {
    if (!active) {
      setStatus("off");
      return;
    }
    if (!detectCapabilities().camera) {
      // Not an error: over plain HTTP the API does not exist at all, and the
      // caller renders an explanation plus the manual path.
      setStatus("unavailable");
      return;
    }

    const voter = new FrameVoter();
    // The decoder's hold-off pushes its window forward on every sighting, so a
    // label parked in front of the lens fires once rather than once per window.
    const holdOff = new PayloadHoldOff(DECODER_HOLD_OFF_MS, { refreshWhileSuppressed: true });
    const canvas = document.createElement("canvas");

    let stream: MediaStream | null = null;
    let attached: HTMLVideoElement | null = null;
    let live = true;
    // `ReturnType` rather than `number`: this file is type-checked with Node's
    // globals in scope as well as the DOM's, and the two disagree about what
    // `setTimeout` hands back.
    let timer: ReturnType<typeof globalThis.setTimeout> | undefined;

    async function tick(): Promise<void> {
      const video = videoRef.current;
      if (!live) {
        return;
      }
      if (video !== null && !pausedRef.current && video.readyState >= 2) {
        try {
          const frame = cropFrame(video, canvas);
          const decoded = frame === null ? null : await decodeImageData(frame);
          const winner = voter.observe(decoded?.text ?? null);
          if (winner !== null && decoded !== null && holdOff.admit(winner)) {
            onDecodeRef.current(winner, decoded.symbology);
          }
        } catch {
          // A single bad frame is not worth reporting; the wasm module throwing
          // repeatedly would show up as "nothing ever decodes", which is the
          // honest symptom anyway.
        }
      }
      if (live) {
        timer = globalThis.setTimeout(() => void tick(), FRAME_INTERVAL_MS);
      }
    }

    setStatus("starting");
    setMessage(null);
    navigator.mediaDevices
      .getUserMedia({
        video: {
          // The rear camera on a phone; ignored on a laptop with one webcam.
          facingMode: { ideal: "environment" },
          width: { ideal: 1280 },
          height: { ideal: 720 },
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
      for (const track of stream?.getTracks() ?? []) {
        track.stop();
      }
    };
  }, [active]);

  return { videoRef, status, message, pause };
}
