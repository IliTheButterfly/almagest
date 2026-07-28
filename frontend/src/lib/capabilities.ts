/**
 * Feature detection for the two capabilities that are gated behind a secure
 * context, and the honest explanation for each when it is missing.
 *
 * ADR 0001 is the constraint. `getUserMedia` and `NDEFReader` exist on
 * `localhost` in development and on `https://almagest.lan` in production, and are
 * **simply absent** over plain HTTP — no error, no permission prompt, the API is
 * just not there. That includes `http://<lan-ip>:5173`, which is the obvious way
 * to open the dev server on a phone and the one that silently has no camera.
 *
 * So: never render an affordance that cannot work. A button that does nothing
 * when tapped teaches the user that the app is broken; a sentence saying why the
 * camera is unavailable and where to open the app instead teaches them how to fix
 * it. And there is always a manual path, so both being absent costs speed and
 * nothing else.
 */

export interface CapabilityScope {
  readonly isSecureContext?: boolean;
  readonly navigator?: { readonly mediaDevices?: { readonly getUserMedia?: unknown } };
  readonly NDEFReader?: unknown;
}

export interface BrowserCapabilities {
  /** `https:`, or the `localhost`/`127.0.0.1` exemption. */
  readonly secureContext: boolean;
  readonly camera: boolean;
  readonly nfc: boolean;
}

function scopeOf(scope?: CapabilityScope): CapabilityScope {
  return scope ?? (globalThis as unknown as CapabilityScope);
}

/**
 * Probe for the APIs themselves, not for the conditions that usually imply them.
 *
 * `isSecureContext` is recorded because it is the *explanation*, but the decision
 * is made on whether the function is actually callable — a browser could withhold
 * either API for its own reasons (iOS has no Web NFC at any URL) and inferring
 * presence from the scheme would then be wrong.
 */
export function detectCapabilities(scope?: CapabilityScope): BrowserCapabilities {
  const target = scopeOf(scope);
  return {
    secureContext: target.isSecureContext === true,
    camera: typeof target.navigator?.mediaDevices?.getUserMedia === "function",
    nfc: typeof target.NDEFReader === "function",
  };
}

/** `null` when the camera is available. Otherwise a sentence to render. */
export function cameraNotice(capabilities: BrowserCapabilities): string | null {
  if (capabilities.camera) {
    return null;
  }
  if (!capabilities.secureContext) {
    return (
      "This page is not a secure context, so the browser does not expose the " +
      "camera at all — there is no permission to grant. Open the app at " +
      "https://almagest.lan (or at http://localhost during development); an " +
      "http:// address on the LAN will never have a camera. Type the code or " +
      "part number instead in the meantime."
    );
  }
  return (
    "This browser exposes no camera. Type the short ID or part number instead — " +
    "every scanning path here has a manual equivalent."
  );
}

/** `null` when Web NFC is available. Otherwise a sentence to render. */
export function nfcNotice(capabilities: BrowserCapabilities): string | null {
  if (capabilities.nfc) {
    return null;
  }
  if (!capabilities.secureContext) {
    return (
      "Web NFC needs a secure context. Open the app at https://almagest.lan on " +
      "Chrome for Android; over http:// the API does not exist."
    );
  }
  return (
    "Web NFC is Chrome-for-Android only — not iOS, not desktop, and so not the " +
    "kiosk. This is a permanent platform limit, not a setting. A tag still works " +
    "here: tapping it opens its /s/ URL in the phone's browser, which lands on the " +
    "same screens. Reading a tag from inside the app needs an Android phone."
  );
}
