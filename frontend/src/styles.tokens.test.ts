/**
 * The palette, audited from the stylesheet itself.
 *
 * A colour scheme's accessibility claim rots the moment somebody nudges a hex,
 * and "it looked fine" is not a measurement. So this reads `styles.css`,
 * resolves every semantic token through the scale, and recomputes the WCAG 2.x
 * contrast ratios — both that the required pairs pass AA (4.5:1 body, 3:1 UI)
 * **and** that the ratio written in the comment beside each token is the ratio
 * that colour actually has. A stale comment fails here rather than misleading
 * the next person to touch the file.
 *
 * It also holds the two structural properties CSS cannot enforce on its own:
 * that the dark mapping applied by the media query and the one applied by
 * `data-theme="dark"` are the same text, and that reduced motion switches
 * everything off rather than a hand-maintained list of properties.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { THEME_STORAGE_KEY } from "./lib/theme";

const HERE = dirname(fileURLToPath(import.meta.url));
const CSS = readFileSync(join(HERE, "styles.css"), "utf8");
const HTML = readFileSync(join(HERE, "..", "index.html"), "utf8");

// --------------------------------------------------------------- contrast ----

function channel(value: number): number {
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
}

/** WCAG 2.x relative luminance of a `#rrggbb` string. */
function luminance(hex: string): number {
  const int = Number.parseInt(hex.slice(1), 16);
  const r = channel(((int >> 16) & 0xff) / 255);
  const g = channel(((int >> 8) & 0xff) / 255);
  const b = channel((int & 0xff) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (high + 0.05) / (low + 0.05);
}

// ------------------------------------------------------------- extraction ----

/** The text between the opening and closing `@tokens:NAME` sentinel comments. */
function section(name: string): string {
  const open = CSS.indexOf(`/* @tokens:${name} */`);
  const close = CSS.indexOf(`/* @/tokens:${name} */`);
  expect(open, `sentinel @tokens:${name} is missing`).toBeGreaterThan(-1);
  expect(close, `sentinel @/tokens:${name} is missing`).toBeGreaterThan(open);
  return CSS.slice(open, close);
}

interface Declaration {
  readonly name: string;
  readonly value: string;
  /** The trailing comment on the same line, where the ratios are recorded. */
  readonly note: string;
}

function declarations(text: string): Declaration[] {
  const out: Declaration[] = [];
  for (const line of text.split("\n")) {
    const match = /^\s*(--[\w-]+):\s*([^;]+);(?:\s*\/\*(.*?)\*\/)?\s*$/.exec(line);
    if (match !== null) {
      out.push({ name: match[1] ?? "", value: (match[2] ?? "").trim(), note: match[3] ?? "" });
    }
  }
  return out;
}

/** The raw ramp: the one place in the project where a hex literal may appear. */
const SCALE: Record<string, string> = {};
for (const declaration of declarations(CSS.slice(0, CSS.indexOf("/* @tokens:light */")))) {
  if (declaration.name.startsWith("--pal-") && declaration.value.startsWith("#")) {
    SCALE[declaration.name] = declaration.value;
  }
}

/** A theme mapping: token name → resolved hex, following one `var()` hop. */
function theme(sectionName: string): Record<string, string> {
  const resolved: Record<string, string> = {};
  for (const declaration of declarations(section(sectionName))) {
    const reference = /^var\((--pal-[\w-]+)\)$/.exec(declaration.value);
    if (reference !== null) {
      const hex = SCALE[reference[1] ?? ""];
      expect(hex, `${declaration.name} points at unknown ${reference[1]}`).toBeDefined();
      resolved[declaration.name] = hex ?? "";
    } else if (declaration.value.startsWith("#")) {
      resolved[declaration.name] = declaration.value;
    }
  }
  return resolved;
}

const THEMES = {
  light: theme("light"),
  dark: theme("dark-attr"),
} as const;

// The pairs the design owes a minimum to. Everything on this list is a place
// where the two colours genuinely meet in the rendered UI — meter fill against
// its own track, ink against the button it sits on — rather than every
// combination the tokens permit.
const BODY_TEXT = 4.5;
const LARGE_OR_UI = 3;

const REQUIRED: readonly (readonly [string, string, number])[] = [
  ["--text", "--bg", BODY_TEXT],
  ["--text", "--bg-raised", BODY_TEXT],
  ["--text", "--bg-sunken", BODY_TEXT],
  ["--text-dim", "--bg", BODY_TEXT],
  ["--text-dim", "--bg-raised", BODY_TEXT],
  ["--text-dim", "--bg-sunken", BODY_TEXT],
  ["--accent", "--bg", BODY_TEXT],
  ["--accent", "--bg-raised", BODY_TEXT],
  ["--accent", "--bg-sunken", BODY_TEXT],
  ["--accent-ink", "--accent", BODY_TEXT],
  ["--accent-2", "--bg", BODY_TEXT],
  ["--accent-2", "--bg-raised", BODY_TEXT],
  ["--accent-2", "--bg-sunken", BODY_TEXT],
  ["--accent-2-ink", "--accent-2", BODY_TEXT],
  ["--good", "--bg", BODY_TEXT],
  ["--good", "--bg-raised", BODY_TEXT],
  ["--warn", "--bg", BODY_TEXT],
  ["--warn", "--bg-raised", BODY_TEXT],
  ["--bad", "--bg", BODY_TEXT],
  ["--bad", "--bg-raised", BODY_TEXT],
  // Text on the soft status fills: the notices and the selected rows.
  ["--text", "--accent-soft", BODY_TEXT],
  ["--text", "--accent-2-soft", BODY_TEXT],
  ["--text", "--good-soft", BODY_TEXT],
  ["--text", "--warn-soft", BODY_TEXT],
  ["--text", "--bad-soft", BODY_TEXT],
  // UI boundaries and graphical objects: control edges, the focus ring, and
  // each fill-meter colour against the track it is drawn in.
  ["--line-strong", "--bg", LARGE_OR_UI],
  ["--line-strong", "--bg-raised", LARGE_OR_UI],
  ["--line-strong", "--bg-sunken", LARGE_OR_UI],
  ["--focus", "--bg", LARGE_OR_UI],
  ["--focus", "--bg-raised", LARGE_OR_UI],
  ["--focus", "--bg-sunken", LARGE_OR_UI],
  ["--accent", "--bg-inset", LARGE_OR_UI],
  ["--warn", "--bg-inset", LARGE_OR_UI],
  ["--bad", "--bg-inset", LARGE_OR_UI],
  ["--good", "--bg-inset", LARGE_OR_UI],
];

describe.each(["light", "dark"] as const)("the %s theme", (name) => {
  const tokens: Record<string, string> = THEMES[name];

  it("defines every token the other theme defines", () => {
    const other = name === "light" ? THEMES.dark : THEMES.light;
    expect(Object.keys(tokens).sort()).toEqual(Object.keys(other).sort());
  });

  it.each(REQUIRED)("%s on %s clears %d:1", (foreground, background, minimum) => {
    const fg = tokens[foreground];
    const bg = tokens[background];
    expect(fg, `${foreground} is not defined in the ${name} theme`).toBeDefined();
    expect(bg, `${background} is not defined in the ${name} theme`).toBeDefined();

    const ratio = contrast(fg ?? "", bg ?? "");
    expect(
      ratio,
      `${foreground} (${fg}) on ${background} (${bg}) is ${ratio.toFixed(2)}:1, under ${minimum}:1`,
    ).toBeGreaterThanOrEqual(minimum);
  });

  it("records ratios in the comments that match the colours", () => {
    const sectionName = name === "light" ? "light" : "dark-attr";
    const surfaces: Record<string, string> = {
      raised: "--bg-raised",
      bg: "--bg",
      sunken: "--bg-sunken",
    };
    let checked = 0;

    for (const declaration of declarations(section(sectionName))) {
      const self = tokens[declaration.name];
      if (self === undefined) {
        continue;
      }

      // The hex written in the comment must be what the var() resolves to.
      const literal = /#[0-9a-f]{6}/.exec(declaration.note);
      if (literal !== null) {
        expect(literal[0], `${declaration.name}: comment hex is stale`).toBe(self);
      }

      // "5.36:1 raised / 4.99:1 bg" — this token against a named surface.
      for (const [, claimed, surface] of declaration.note.matchAll(
        /([\d.]+):1 (raised|bg|sunken)\b/g,
      )) {
        const against = tokens[surfaces[surface ?? ""] ?? ""] ?? "";
        expect(
          contrast(self, against),
          `${declaration.name} vs ${surface}: comment claims ${claimed}:1`,
        ).toBeCloseTo(Number(claimed), 1);
        checked += 1;
      }

      // "5.36:1 on --accent" — this token against another token.
      for (const [, claimed, other] of declaration.note.matchAll(
        /([\d.]+):1 on (--[\w-]+)/g,
      )) {
        const against = tokens[other ?? ""];
        expect(against, `${declaration.name} names unknown ${other}`).toBeDefined();
        expect(
          contrast(self, against ?? ""),
          `${declaration.name} vs ${other}: comment claims ${claimed}:1`,
        ).toBeCloseTo(Number(claimed), 1);
        checked += 1;
      }

      // "--text on it is 15.88:1" — another token against this one.
      for (const [, other, claimed] of declaration.note.matchAll(
        /(--[\w-]+) on it is ([\d.]+):1/g,
      )) {
        const over = tokens[other ?? ""];
        expect(over, `${declaration.name} names unknown ${other}`).toBeDefined();
        expect(
          contrast(over ?? "", self),
          `${other} on ${declaration.name}: comment claims ${claimed}:1`,
        ).toBeCloseTo(Number(claimed), 1);
        checked += 1;
      }
    }

    // Guards against the regexes silently matching nothing after a reformat.
    expect(checked).toBeGreaterThan(25);
  });
});

describe("the flag it is derived from", () => {
  it("keeps both flag hues in the scale, at the one step they are usable", () => {
    expect(SCALE["--pal-blue-400"]).toBe("#5bcefa");
    expect(SCALE["--pal-pink-400"]).toBe("#f5a9b8");
  });

  it("does not use them for anything with a contrast duty", () => {
    // They are a luminance match for each other (0.53 vs 0.51) and close to
    // white, so they are identity fills only. Both themes point --identity-*
    // at them and nothing else does.
    for (const tokens of [THEMES.light, THEMES.dark]) {
      const users = Object.entries(tokens)
        .filter(([, hex]) => hex === "#5bcefa" || hex === "#f5a9b8")
        .map(([token]) => token);
      expect(users.sort()).toEqual(["--identity-a", "--identity-b"]);
    }
  });
});

describe("the stylesheet as a whole", () => {
  it("keeps every colour literal in the scale", () => {
    const body = CSS.slice(CSS.indexOf("/* @tokens:light */")).replace(/\/\*[\s\S]*?\*\//g, "");
    const literals = [...body.matchAll(/#[0-9a-fA-F]{3,8}\b/g)].map((match) => match[0]);
    // The viewfinder's letterbox and caption are the two deliberate absolutes:
    // they sit over a live camera frame, not over a themed surface.
    expect(literals.filter((literal) => literal !== "#000" && literal !== "#fff")).toEqual([]);
  });

  it("applies the dark mapping identically however it is triggered", () => {
    // `data-theme="dark"` has to beat a light OS, and the media query has to
    // apply when nothing is stored. CSS has no way to share one body between
    // the two without `light-dark()`, so they are duplicated — and drift
    // between them would be a theme that is subtly wrong in one of the paths.
    const normalise = (text: string): string =>
      text
        .split("\n")
        .map((line) => line.trim())
        .filter((line) => line !== "" && !line.startsWith("/*") && !line.includes("{"))
        .join("\n");

    expect(normalise(section("dark-media"))).toBe(normalise(section("dark-attr")));
  });

  it("disables all motion under prefers-reduced-motion", () => {
    const query = CSS.slice(CSS.indexOf("@media (prefers-reduced-motion: reduce)"));
    const block = query.slice(0, query.indexOf("\n}\n"));
    // A universal selector rather than a list of the properties in use today,
    // so a transition added later is covered without anyone remembering to.
    expect(block).toMatch(/\*,\s*\n\s*\*::before,\s*\n\s*\*::after/);
    expect(block).toMatch(/transition-duration:\s*0\.01ms\s*!important/);
    expect(block).toMatch(/animation-duration:\s*0\.01ms\s*!important/);
  });

  it("routes every transition through the --dur token", () => {
    // The reduced-motion block also zeroes --dur, so anything using it is
    // switched off twice over. A hardcoded duration would escape one of those.
    const body = CSS.replace(/\/\*[\s\S]*?\*\//g, "");
    for (const [declaration] of body.matchAll(/transition:[^;]+;/g)) {
      expect(declaration, "a transition bypasses --dur").toContain("var(--dur)");
    }
  });
});

describe("the pre-paint theme script", () => {
  it("reads the same storage key the module writes", () => {
    // index.html has to apply the override before first paint, which means it
    // cannot import theme.ts. The key is therefore written twice, and this is
    // the only thing keeping the two copies honest.
    expect(HTML).toContain(`localStorage.getItem("${THEME_STORAGE_KEY}")`);
    expect(HTML).toContain('setAttribute("data-theme"');
  });
});
