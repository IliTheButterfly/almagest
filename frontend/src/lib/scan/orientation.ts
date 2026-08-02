/**
 * Which way up the camera is bolted, remembered per device.
 *
 * A phone's camera is the right way up because a person is holding it. A camera
 * screwed to a bench arm above a tray is whichever way up the bracket allowed,
 * and on the Jetson station it is upside down — the sensor is inverted relative
 * to the person looking at the screen. Nothing in `getUserMedia` reports this:
 * the frame is what the sensor saw, and only the fixture knows it is mounted
 * head-down.
 *
 * **The decode path is left alone, and that is deliberate.** (The still-capture
 * path is not — see below.) Two facts make decoding already correct under a
 * 180° mount:
 *
 * - The decoder's crop is a *centred* rectangle ({@link centreRoi}), and a
 *   centred rectangle maps onto itself under a half turn. So the pixels read at
 *   180° are exactly the pixels read at 0°, and {@link roiOverlayInset} keeps
 *   drawing the truth with no adjustment.
 * - `decodeImageData` already passes `tryRotate: true`, because nobody holds a
 *   reel square to the lens. The symbologies that are not rotation-invariant —
 *   Code 128, EAN — are therefore already read upside down.
 *
 * Rotating the decoded frame as well would cost a full-resolution redraw per
 * pass, on the slowest machine in the fleet, to change no outcome. What is
 * genuinely broken by an inverted mount is a person trying to aim, so that is
 * what is fixed.
 *
 * **Never seen against a physically inverted camera.** Every test here and in
 * `Viewfinder.test.tsx` runs in jsdom, which asserts a class name and a stored
 * value — not a picture. The bench webcam was disconnected before this could be
 * looked at on the station's own display. The reasoning above is the argument;
 * it is not a photograph, and the first person to put a camera back on that
 * bracket should confirm it before trusting it.
 *
 * **The still-capture path.**
 * PR #60 (`lib/capture/grab.ts`) draws the raw video frame to a canvas and
 * encodes it, then runs OCR over the result. Both of those arguments above stop
 * applying there: a captured still is *looked at* by a person, and OCR of
 * upside-down text does not degrade the way a rotation-invariant QR does — it
 * returns nothing. `grabStill` therefore draws through this same rotation —
 * see `lib/capture/grab.ts` and `grab.rotation.test.ts`. It is the one place
 * the mounting reaches the pixels rather than only the preview, and it shares
 * this setting rather than introducing a second one.
 *
 * **Only a half turn is offered.** A quarter turn swaps the frame's axes, which
 * `roiOverlayInset` reasons about explicitly — it compares the frame's aspect
 * ratio against the viewfinder box's to undo `object-fit: cover` — so a 90°
 * setting would silently draw the aiming rectangle in the wrong place. That is
 * the one failure this module exists to prevent, so the type does not admit it.
 */

/** Degrees the preview is turned before it is shown. */
export type CameraRotation = 0 | 180;

/**
 * Where the choice is kept.
 *
 * `localStorage`, not the database: this is a property of one physical fixture,
 * not of the inventory. The same account on a phone must not inherit the bench
 * station's bracket.
 */
export const CAMERA_ROTATION_KEY = "almagest.camera-rotation";

/**
 * A `localStorage`-shaped thing. Narrowed to the two methods used so tests can
 * pass a plain object, and so a caller in a context with no storage at all can
 * pass `null` instead of being forced to stub one.
 */
export interface RotationStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/**
 * Read the stored rotation, defaulting to upright.
 *
 * Anything unrecognised — a hand-edited value, a key left by an older build,
 * a storage that throws because the browser is in a locked-down mode — reads as
 * `0`. A preview that is the wrong way up is a nuisance; one that fails to
 * render because a string did not parse is a broken scanner.
 */
export function readCameraRotation(store: RotationStore | null): CameraRotation {
  if (store === null) {
    return 0;
  }
  let raw: string | null;
  try {
    raw = store.getItem(CAMERA_ROTATION_KEY);
  } catch {
    return 0;
  }
  return raw === "180" ? 180 : 0;
}

/** Persist the rotation. A storage that refuses the write is not an error worth surfacing. */
export function writeCameraRotation(store: RotationStore | null, rotation: CameraRotation): void {
  if (store === null) {
    return;
  }
  try {
    store.setItem(CAMERA_ROTATION_KEY, String(rotation));
  } catch {
    // Private mode, a full quota, a policy-disabled storage. The rotation still
    // applies for this session; it just will not survive a reload.
  }
}

/** The other one. Exported so the toggle has no arithmetic in it. */
export function flipRotation(rotation: CameraRotation): CameraRotation {
  return rotation === 0 ? 180 : 0;
}

/**
 * The store to use in a browser, or `null` where there is none.
 *
 * `localStorage` throws on access — not on use — in some embedded webviews, so
 * this is a try/catch rather than a truthiness check.
 */
export function defaultRotationStore(): RotationStore | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}
