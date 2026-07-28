/**
 * The theme override, which is the half of a colour scheme that usually breaks.
 *
 * What matters is not that a class gets toggled but that the *precedence* holds:
 * an explicit choice has to beat the OS preference in **both** directions, and
 * "follow the OS" has to keep following it rather than freezing whatever the OS
 * happened to say at load. Those are the two bugs every hand-rolled theme
 * switcher ships with.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { theme, THEME_STORAGE_KEY } from "./theme";

/** A `matchMedia` whose answer we control, plus a handle on its listeners. */
function stubSystemTheme(dark: boolean): { fire: (nowDark: boolean) => void } {
  const listeners = new Set<() => void>();
  let current = dark;
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query.includes("dark") ? current : !current,
    media: query,
    addEventListener: (_: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_: string, listener: () => void) => listeners.delete(listener),
  }));
  return {
    fire: (nowDark: boolean) => {
      current = nowDark;
      for (const listener of listeners) {
        listener();
      }
    },
  };
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  stubSystemTheme(false);
  theme.reset();
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("with no stored preference", () => {
  it("follows the OS and writes no attribute at all", () => {
    stubSystemTheme(true);
    theme.reset();

    expect(theme.preference).toBe("system");
    expect(theme.resolved).toBe("dark");
    // Crucially *absent*, not set to "dark": the stylesheet's media query is
    // what resolves it, so the choice stays live.
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("keeps following the OS when it changes mid-session", () => {
    const system = stubSystemTheme(false);
    theme.reset();
    const seen: string[] = [];
    const unsubscribe = theme.subscribe(() => seen.push(theme.resolved));

    expect(theme.resolved).toBe("light");
    system.fire(true);

    expect(theme.resolved).toBe("dark");
    expect(seen).toContain("dark");
    unsubscribe();
  });
});

describe("an explicit override", () => {
  it("wins over a dark OS", () => {
    stubSystemTheme(true);
    theme.reset();

    theme.set("light");

    expect(theme.resolved).toBe("light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("wins over a light OS", () => {
    stubSystemTheme(false);
    theme.reset();

    theme.set("dark");

    expect(theme.resolved).toBe("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("persists, and is read back on the next visit", () => {
    theme.set("dark");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");

    // A fresh visit: same stored value, a store that has not seen the set().
    document.documentElement.removeAttribute("data-theme");
    theme.reset();

    expect(theme.preference).toBe("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("is dropped again by going back to system", () => {
    theme.set("dark");
    theme.set("system");

    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("notifies subscribers so the toggle re-renders", () => {
    const listener = vi.fn();
    const unsubscribe = theme.subscribe(listener);

    theme.set("dark");

    expect(listener).toHaveBeenCalled();
    unsubscribe();
  });
});

describe("a corrupt stored value", () => {
  it("is ignored rather than written onto the document", () => {
    localStorage.setItem(THEME_STORAGE_KEY, "solarized");
    theme.reset();

    expect(theme.preference).toBe("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });
});
