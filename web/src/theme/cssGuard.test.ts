/** No undefined tokens, and no hard-coded purple, in the app's styles (#948).
 *
 * The report was a purple dialog. The cause was a stylesheet written against tokens that tokens.css
 * never defined (`--surface-1`, `--mono`, `--focus`). `var(--surface-1, #1b1226)` looks themed in a
 * diff and paints its fallback on every install, in both themes, because the token it names does not
 * exist. Copies of that sheet reached three dialogs and a Settings form before anyone saw it, because
 * nothing broke. These checks are the part of that review a machine can do.
 *
 * What counts as defined: a name tokens.css declares, a custom property some other sheet declares
 * (`--rec` in Compose, `--app-header-height` in App.css), or one the app sets from code
 * (`style={{ "--proj": colour }}`, `setProperty("--sidebar-w", …)`). It is a name-level check, not a
 * cascade check: a local property declared in one sheet and read in another would pass. The failure
 * it exists for is a name that nothing sets at all.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, test } from "vitest";

const SRC = resolve(process.cwd(), "src");

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(join(dir, e.name)) : [join(dir, e.name)],
  );
}

const FILES = walk(SRC).map((f) => relative(SRC, f).split("\\").join("/"));
const read = (f: string) => readFileSync(join(SRC, f), "utf8");

/** Comments blanked with their line breaks kept, so a finding still names its line. */
const blank = (c: string) => c.replace(/[^\n]/g, " ");
const cssSource = (f: string) => read(f).replace(/\/\*[\s\S]*?\*\//g, blank);
// A `//` comment only where it cannot be the middle of a URL (`https://…` has a colon before it).
const codeSource = (f: string) =>
  read(f)
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/(^|[\s;{}])\/\/[^\n]*/g, (m, lead: string) => lead + blank(m.slice(lead.length)));

const ALL_CSS = FILES.filter((f) => f.endsWith(".css"));
const CSS = ALL_CSS.filter((f) => f !== "tokens.css");
const CODE = FILES.filter((f) => /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f));

function findings(files: string[], source: (f: string) => string, re: RegExp) {
  return files.flatMap((f) =>
    source(f)
      .split("\n")
      .flatMap((line, i) => [...line.matchAll(re)].map((m) => ({ at: `${f}:${i + 1}`, m }))),
  );
}

// ---------------------------------------------------------------------------------------------------
// Undefined tokens
// ---------------------------------------------------------------------------------------------------

/** Custom properties code SETS: an object key (`{ "--proj": c }`, `{ ["--proj" as string]: c }`) or a
 *  `setProperty("--name", …)` call. A quoted name anywhere else, such as `getPropertyValue("--x")`, is
 *  a read, and counting it as a definition would let an undefined token pass (#961 review 4813). */
function codeDefinitions(src: string): string[] {
  const out: string[] = [];
  for (const m of src.matchAll(/["'`](--[\w-]+)["'`]\s*(?:as\s+[^\]:,}]+)?\]?\s*:/g)) out.push(m[1]);
  for (const m of src.matchAll(/setProperty\(\s*["'`](--[\w-]+)["'`]/g)) out.push(m[1]);
  return out;
}

describe("every var(--name) the app reads is defined somewhere (#948)", () => {
  const defined = new Set<string>();
  for (const f of ALL_CSS) for (const m of cssSource(f).matchAll(/(--[\w-]+)\s*:/g)) defined.add(m[1]);
  for (const f of CODE) for (const name of codeDefinitions(codeSource(f))) defined.add(name);

  test("a READ in code is not a definition (#961 review 4813)", () => {
    expect(codeDefinitions('el.style.getPropertyValue("--missing")')).toEqual([]);
    expect(codeDefinitions('getComputedStyle(el).getPropertyValue("--missing")')).toEqual([]);
    expect(codeDefinitions('style={{ "--proj": colour }}')).toEqual(["--proj"]);
    expect(codeDefinitions('style={{ ["--proj" as string]: colour }}')).toEqual(["--proj"]);
    expect(codeDefinitions('document.documentElement.style.setProperty("--sidebar-w", w)')).toEqual(["--sidebar-w"]);
  });

  test("in stylesheets", () => {
    const missing = findings(ALL_CSS, cssSource, /var\(\s*(--[\w-]+)/g)
      .filter(({ m }) => !defined.has(m[1]))
      .map(({ at, m }) => `${at} ${m[1]}`);
    expect(missing).toEqual([]);
  });

  test("in inline styles set from code", () => {
    const missing = findings(CODE, codeSource, /var\(\s*(--[\w-]+)/g)
      .filter(({ m }) => !defined.has(m[1]))
      .map(({ at, m }) => `${at} ${m[1]}`);
    expect(missing).toEqual([]);
  });
});

// ---------------------------------------------------------------------------------------------------
// Hard-coded purple
// ---------------------------------------------------------------------------------------------------

/** HSL hue (degrees) and saturation (0–1) of `#rgb`, `#rgba`, `#rrggbb` or `#rrggbbaa`. Alpha is ignored. */
function hueSat(hex: string): { hue: number; sat: number } {
  let h = hex.replace(/^#/, "");
  if (h.length <= 4) h = [...h.slice(0, 3)].map((c) => c + c).join("");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  if (d === 0) return { hue: 0, sat: 0 };
  const l = (max + min) / 2;
  const sat = d / (1 - Math.abs(2 * l - 1));
  let hue = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  hue *= 60;
  if (hue < 0) hue += 360;
  return { hue, sat };
}

/** Indigo through violet. It starts at 240° because the retired panel blue-violet (`#14102e`) sits
 *  at 248°. It stops at 320° so the magenta and pink the app does use stay out of it: the Kimi engine
 *  colour is at 329° and the operator's Magenta accent preset at 322°. Under 15% saturation a hex
 *  is a tinted grey, and the HUD palette is made of those. */
const isPurple = (hex: string) => {
  const { hue, sat } = hueSat(hex);
  return hue >= 240 && hue <= 320 && sat >= 0.15;
};

/** The only purples allowed, each for a reason that is not chrome. Keyed by file AND value, so a new
 *  purple in the same file still fails. */
const PURPLE_ALLOWED: Record<string, readonly string[]> = {
  // The terminal's ANSI palette. Magenta is one of the sixteen colours a program asks for by number,
  // and swapping in another hue would misrender agent output. These paint only the xterm canvas.
  "theme/themes.ts": ["#75507b", "#ad7fa8", "#8f2f96", "#7a3fd4"],
  // One of six colours the operator can pick for a project (#361). It shows only where somebody chose
  // it, so it is their content, not app chrome.
  // It moved to `lib/projectColors.ts` with the shared picker (#1187).
  "lib/projectColors.ts": ["#c792ea"],
};

const allowed = (at: string, hex: string) =>
  (PURPLE_ALLOWED[at.slice(0, at.lastIndexOf(":"))] ?? []).includes(hex.toLowerCase());

describe("no hard-coded purple outside tokens.css (#948)", () => {
  test("the classifier is right about the colours this was written for", () => {
    // Without these, a broken hue function would pass the scans below by finding nothing.
    for (const hex of ["#1b1226", "#2a223f", "#3b3357", "#aea3c7", "#ece7f7", "#0b0617", "#2f2658", "#14102e", "#a78bfa"])
      expect(isPurple(hex), hex).toBe(true);
    // The amber accent, the draft blue, the Gemini blue, the Kimi pink, the Magenta accent preset,
    // the HUD greys.
    for (const hex of ["#ffb000", "#3b9eff", "#7aa2ff", "#f472b6", "#d6409f", "#25282e", "#6f747d", "#131418"])
      expect(isPurple(hex), hex).toBe(false);
  });

  test("in stylesheets", () => {
    const hits = findings(CSS, cssSource, /#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\w-])/g)
      .filter(({ at, m }) => isPurple(m[0]) && !allowed(at, m[0]))
      .map(({ at, m }) => `${at} ${m[0]}`);
    expect(hits).toEqual([]);
  });

  test("in code", () => {
    // Six- and eight-digit only, plus a fully quoted short form. `#335` in a string is an issue
    // number far more often than a colour.
    const hits = findings(CODE, codeSource, /#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6})(?![\w-])|(?<=["'`])#[0-9a-fA-F]{3,4}(?=["'`])/g)
      .filter(({ at, m }) => isPurple(m[0]) && !allowed(at, m[0]))
      .map(({ at, m }) => `${at} ${m[0]}`);
    expect(hits).toEqual([]);
  });
});
