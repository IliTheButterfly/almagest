/**
 * Light or dark, and who decides.
 *
 * Three states, not two. `system` follows `prefers-color-scheme` and keeps
 * following it — a workshop moves from a bright bench to a dark room and the OS
 * already knows — while `light` and `dark` are explicit overrides. An app that
 * only offers a boolean has to guess an initial value and then never changes it
 * again, which is the wrong answer at least half the day.
 *
 * The override is written as `data-theme` on `<html>`, because that is the only
 * mechanism that can beat a media query in both directions: `:root[data-theme]`
 * wins over `@media (prefers-color-scheme: …)` on specificity for dark-when-OS-is-
 * light, and the media query itself is scoped `:root:not([data-theme="light"])`
 * for light-when-OS-is-dark. See the token blocks in `styles.css`.
 *
 * `index.html` reads the same key in an inline script before first paint. That
 * duplication is deliberate — a module cannot run early enough to prevent a
 * flash of the wrong theme — and a test asserts the two key strings match.
 */

import { useSyncExternalStore } from "react";

export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

/** Also hardcoded in `index.html`'s pre-paint script. Keep them in step. */
export const THEME_STORAGE_KEY = "almagest.theme";

const DARK_QUERY = "(prefers-color-scheme: dark)";

function isPreference(value: unknown): value is ThemePreference {
  return value === "system" || value === "light" || value === "dark";
}

/**
 * Reading `localStorage` can throw outright — Safari in private mode, or a
 * kiosk with storage disabled — and a theme is never worth a blank screen.
 */
function readStored(): ThemePreference {
  try {
    const raw = globalThis.localStorage?.getItem(THEME_STORAGE_KEY);
    return isPreference(raw) ? raw : "system";
  } catch {
    return "system";
  }
}

function writeStored(preference: ThemePreference): void {
  try {
    if (preference === "system") {
      globalThis.localStorage?.removeItem(THEME_STORAGE_KEY);
    } else {
      globalThis.localStorage?.setItem(THEME_STORAGE_KEY, preference);
    }
  } catch {
    // Persisting failed; the in-memory preference still applies for this visit.
  }
}

function systemPrefersDark(): boolean {
  return globalThis.matchMedia?.(DARK_QUERY).matches ?? false;
}

class ThemeStore {
  #preference: ThemePreference = readStored();
  #listeners = new Set<() => void>();
  #wired = false;

  get preference(): ThemePreference {
    return this.#preference;
  }

  get resolved(): ResolvedTheme {
    if (this.#preference === "system") {
      return systemPrefersDark() ? "dark" : "light";
    }
    return this.#preference;
  }

  set(preference: ThemePreference): void {
    this.#preference = preference;
    writeStored(preference);
    this.apply();
    this.#notify();
  }

  /**
   * Push the preference onto the document.
   *
   * `system` *removes* the attribute rather than writing the resolved value:
   * writing it would freeze the choice at whatever the OS said when the page
   * loaded, which is exactly the bug three-state exists to avoid.
   */
  apply(): void {
    const root = globalThis.document?.documentElement;
    if (root === undefined) {
      return;
    }
    if (this.#preference === "system") {
      root.removeAttribute("data-theme");
    } else {
      root.setAttribute("data-theme", this.#preference);
    }
    this.#paintBrowserChrome(root);
  }

  /**
   * Keep the phone's address bar in step with the page.
   *
   * Read back off the cascade rather than from a table of hexes, so the palette
   * stays defined in exactly one file.
   */
  #paintBrowserChrome(root: Element): void {
    const meta = globalThis.document?.querySelector('meta[name="theme-color"]');
    if (meta === null || meta === undefined || !(meta instanceof HTMLMetaElement)) {
      return;
    }
    const background = globalThis.getComputedStyle?.(root).getPropertyValue("--bg").trim();
    if (background !== undefined && background !== "") {
      meta.content = background;
    }
  }

  /** Re-read the stored preference: another tab changed it, or a test reset. */
  reset(): void {
    this.#preference = readStored();
    this.apply();
    this.#notify();
  }

  subscribe = (listener: () => void): (() => void) => {
    this.#wire();
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };

  #notify(): void {
    for (const listener of this.#listeners) {
      listener();
    }
  }

  /**
   * Wired on first subscription rather than at construction, so importing this
   * module has no side effects on a document that may not exist yet.
   */
  #wire(): void {
    if (this.#wired) {
      return;
    }
    this.#wired = true;
    globalThis.matchMedia?.(DARK_QUERY).addEventListener("change", () => {
      // Only meaningful under `system`, but notifying either way is harmless and
      // avoids a stale render if the preference changed in the same tick.
      this.#notify();
    });
    globalThis.addEventListener?.("storage", (event) => {
      if ((event as StorageEvent).key === THEME_STORAGE_KEY) {
        this.reset();
      }
    });
  }
}

export const theme = new ThemeStore();

export interface ThemeControls {
  readonly preference: ThemePreference;
  readonly resolved: ResolvedTheme;
  readonly set: (preference: ThemePreference) => void;
}

/**
 * Two subscriptions rather than one returning an object: `useSyncExternalStore`
 * compares snapshots by identity, and a fresh object every call is an infinite
 * render loop.
 */
export function useTheme(): ThemeControls {
  const preference = useSyncExternalStore(
    theme.subscribe,
    () => theme.preference,
    () => "system" as ThemePreference,
  );
  const resolved = useSyncExternalStore(
    theme.subscribe,
    () => theme.resolved,
    () => "light" as ResolvedTheme,
  );
  return { preference, resolved, set: (next) => theme.set(next) };
}
