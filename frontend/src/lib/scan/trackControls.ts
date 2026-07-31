/**
 * `MediaStreamTrack` capability probing and tuning, kept apart from
 * `useScanner.ts` so it can be unit-tested with a plain stub object instead of
 * a real camera.
 *
 * Every one of `getCapabilities`, `getSettings` and `applyConstraints` is
 * optional in the spec, and a browser that has the method can still throw
 * from it (Firefox's `getCapabilities` is a documented no-op on some
 * versions). Nothing here may throw back at the caller — a missing or
 * misbehaving capability must degrade to "not offered", never to a crashed
 * scanner.
 */

/** The subset of `MediaTrackCapabilities` this module reads. Everything optional. */
export interface TrackCapabilitiesLike {
  readonly focusMode?: readonly string[];
  readonly torch?: boolean;
  readonly zoom?: { readonly min?: number; readonly max?: number; readonly step?: number };
}

/** The subset of `MediaTrackSettings` this module reads. */
export interface TrackSettingsLike {
  readonly width?: number;
  readonly height?: number;
  readonly zoom?: number;
}

/**
 * The subset of `MediaStreamTrack` this module needs — narrowed for testing.
 *
 * `applyConstraints` is declared with **method syntax** and an `advanced` of
 * `unknown[]`, and both halves of that are load-bearing. `focusMode`, `torch`
 * and `zoom` are not in `lib.dom`'s `MediaTrackConstraintSet` at all, so the
 * constraint bag cannot be typed as the DOM's own type without a cast at every
 * call site. Method syntax then makes the parameter check bivariant, which is
 * what lets a real `MediaStreamTrack` satisfy this interface directly —
 * declared as a property, `strictFunctionTypes` compares the parameters
 * contravariantly and a genuine track is rejected.
 */
export interface VideoTrackLike {
  getCapabilities?: () => unknown;
  getSettings?: () => unknown;
  applyConstraints?(constraints?: { advanced?: unknown[] }): Promise<void>;
}

/** `getCapabilities()`, or `{}` if the method is absent, throws, or returns garbage. */
export function safeCapabilities(track: VideoTrackLike): TrackCapabilitiesLike {
  if (typeof track.getCapabilities !== "function") {
    return {};
  }
  try {
    const raw = track.getCapabilities();
    return raw !== null && typeof raw === "object" ? (raw as TrackCapabilitiesLike) : {};
  } catch {
    return {};
  }
}

/** `getSettings()`, or `{}` if the method is absent, throws, or returns garbage. */
export function safeSettings(track: VideoTrackLike): TrackSettingsLike {
  if (typeof track.getSettings !== "function") {
    return {};
  }
  try {
    const raw = track.getSettings();
    return raw !== null && typeof raw === "object" ? (raw as TrackSettingsLike) : {};
  } catch {
    return {};
  }
}

export function supportsContinuousFocus(capabilities: TrackCapabilitiesLike): boolean {
  return Array.isArray(capabilities.focusMode) && capabilities.focusMode.includes("continuous");
}

export function torchAvailable(capabilities: TrackCapabilitiesLike): boolean {
  return capabilities.torch === true;
}

export interface ZoomRange {
  readonly min: number;
  readonly max: number;
  readonly step: number;
}

/** `null` when the track does not report a usable zoom range. */
export function zoomRange(capabilities: TrackCapabilitiesLike): ZoomRange | null {
  const zoom = capabilities.zoom;
  if (
    zoom === undefined ||
    typeof zoom.min !== "number" ||
    typeof zoom.max !== "number" ||
    zoom.max <= zoom.min
  ) {
    return null;
  }
  return { min: zoom.min, max: zoom.max, step: typeof zoom.step === "number" && zoom.step > 0 ? zoom.step : 1 };
}

/**
 * One `applyConstraints` call, reduced to "did it take" — never throws.
 *
 * Every tuning knob (focus, torch, zoom) goes through this so the failure
 * mode is uniform: a browser that advertises a capability and then rejects
 * the constraint anyway is treated exactly like a browser that never
 * advertised it.
 */
async function applyAdvanced(
  track: VideoTrackLike,
  constraint: Record<string, unknown>,
): Promise<boolean> {
  if (typeof track.applyConstraints !== "function") {
    return false;
  }
  try {
    await track.applyConstraints({ advanced: [constraint] });
    return true;
  } catch {
    return false;
  }
}

/** Best-effort continuous autofocus. Silent no-op where unsupported. */
export function applyContinuousFocus(track: VideoTrackLike): Promise<boolean> {
  return applyAdvanced(track, { focusMode: "continuous" });
}

export function setTorch(track: VideoTrackLike, enabled: boolean): Promise<boolean> {
  return applyAdvanced(track, { torch: enabled });
}

export function setZoom(track: VideoTrackLike, value: number): Promise<boolean> {
  return applyAdvanced(track, { zoom: value });
}
