/**
 * Put a value on the clipboard, or say plainly that it could not be done.
 *
 * `navigator.clipboard` is gated behind a secure context — the same gate that
 * removes `getUserMedia` and `NDEFReader` over plain HTTP, and the reason ADR
 * 0001 settled on `https://almagest.aether.lan` behind a private CA. On the surfaces
 * this feature is used from that gate is normally satisfied, since a capture
 * needs a camera and a camera needs the same secure context.
 *
 * But not always, and the exception is the interesting one: a capture can be
 * *reopened* from the intake queue at a desk, on a machine that never had a
 * camera and may be on plain HTTP. Copying has to keep working there, so there
 * is a fallback, and it is the deprecated one:
 *
 * `document.execCommand("copy")` over a hidden, selected `<textarea>`. It is
 * deprecated, it is ugly, and it is still the only thing that works in a
 * non-secure context in every current browser. The alternative is a
 * "copy failed" toast next to a value the user can plainly see, which is worse
 * than a deprecated API.
 *
 * Returns whether it worked rather than throwing. The caller shows a confirmation
 * either way — "copied" or "select and copy it yourself" — and neither is an
 * error worth an error banner.
 */

export async function copyText(value: string): Promise<boolean> {
  // Feature-detected rather than assumed: in a non-secure context
  // `navigator.clipboard` is `undefined` outright, not a method that rejects.
  if (globalThis.navigator?.clipboard !== undefined) {
    try {
      await globalThis.navigator.clipboard.writeText(value);
      return true;
    } catch {
      // Permission denied, or a browser that exposes the API and refuses it
      // without a user gesture it recognises. Fall through rather than give up:
      // the fallback below asks nothing of the permission system.
    }
  }
  return legacyCopy(value);
}

function legacyCopy(value: string): boolean {
  if (typeof document === "undefined" || typeof document.execCommand !== "function") {
    return false;
  }
  const area = document.createElement("textarea");
  area.value = value;
  // Off-screen rather than `display: none` — a hidden element cannot hold a
  // selection, and an unselected textarea copies nothing.
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.top = "-1000px";
  area.style.opacity = "0";
  document.body.appendChild(area);
  try {
    area.select();
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    document.body.removeChild(area);
  }
}
