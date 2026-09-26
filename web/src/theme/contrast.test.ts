import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "vitest";

// Parses the SHIPPED palette (tokens.css) and enforces WCAG-AA contrast on each theme, so a
// future palette tweak can't silently drop below readable. Dark is the bare `:root` default;
// dark/light also live in `:root[data-theme="…"]`. We check body text/bg per theme, plus the
// CTA contract: since #211 Phase 2 the filled CTAs derive from the brand accent
// (--cta-bg-1 = var(--accent), --cta-text = var(--on-accent)), so their contrast IS the
// on-accent/accent pair already checked per theme.

// vitest runs from web/; the palette under test is web/src/tokens.css, which index.css imports
// (#829 — the docs site imports the same file, so there is one set of values, not two).
// Comments are stripped BEFORE any selector lookup: block() finds the first textual match, and
// tokens.css's header comment names `:root[data-theme="dark"]` — leaving comments in would point
// the dark assertions at the bare :root block and quietly test the wrong palette.
const css = readFileSync(resolve(process.cwd(), "src/tokens.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);

function block(selector: string): string {
  // Grab the first `{ … }` body following the selector.
  const i = css.indexOf(selector);
  expect(i, `selector ${selector} present`).toBeGreaterThanOrEqual(0);
  const open = css.indexOf("{", i);
  const close = css.indexOf("}", open);
  return css.slice(open + 1, close);
}

function token(body: string, name: string): string {
  const m = body.match(new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`));
  expect(m, `--${name} present`).not.toBeNull();
  return (m as RegExpMatchArray)[1];
}

function lum(hex: string): number {
  const n = parseInt(hex.slice(1), 16);
  const ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
}

function ratio(a: string, b: string): number {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const THEMES: Record<string, string> = {
  default: ":root {", // BattleLab dark default lives in the bare :root (#211)
  dark: ':root[data-theme="dark"]',
  light: ':root[data-theme="light"]',
};

for (const [name, selector] of Object.entries(THEMES)) {
  test(`${name}: body text on bg meets WCAG AA (>=4.5:1)`, () => {
    const b = block(selector);
    expect(ratio(token(b, "text"), token(b, "bg"))).toBeGreaterThanOrEqual(4.5);
  });

  // The accent is LIGHT (amber), so text-bearing accent surfaces (the active filter tab
  // `Filters.module.css .tabs button.on`, the takeover button) render dark --on-accent on
  // --accent, never white. Keep that pair AA. (#211)
  test(`${name}: --on-accent on --accent meets WCAG AA (>=4.5:1)`, () => {
    const b = block(selector);
    expect(
      ratio(token(b, "on-accent"), token(b, "accent")),
    ).toBeGreaterThanOrEqual(4.5);
  });
}

// The CTA derives from the brand accent (#211 Phase 2): --cta-bg-1 = var(--accent),
// --cta-text = var(--on-accent). Assert the aliasing is wired (so a custom accent recolours
// the CTA too) and that the resolved on-accent/accent pair — the most contrast-critical part
// of the gradient — stays AA. Default amber is ~11:1 (AAA).
test("CTA derives from the accent and stays AA (>=4.5:1)", () => {
  const root = block(":root {");
  expect(root).toMatch(/--cta-bg-1:\s*var\(--accent\)/);
  expect(root).toMatch(/--cta-text:\s*var\(--on-accent\)/);
  expect(
    ratio(token(root, "on-accent"), token(root, "accent")),
  ).toBeGreaterThanOrEqual(4.5);
});

// ---------------------------------------------------------------- git letters + diff (#784)
//
// These tokens are `color-mix(...)`, not hex, so the helpers above cannot read them — and a test
// that merely asserted "the token exists" would prove nothing. `--status-degraded` (#f59e0b) on
// the light ground measures ~2.15:1, i.e. it FAILED, so the point of this block is to check the
// resolved colour rather than the declaration.

/** Resolve one level of `color-mix(in srgb, A p%, B)` against a token table. */
function resolveMix(expr: string, lookup: (name: string) => string): string {
  const m = expr.match(
    /color-mix\(in srgb,\s*(var\(--[a-z0-9-]+\)|#[0-9a-fA-F]{6})\s*(\d+)%,\s*(var\(--[a-z0-9-]+\)|#[0-9a-fA-F]{6}|transparent)\s*\)/,
  );
  if (!m) return expr.startsWith("#") ? expr : lookup(expr.replace(/var\(--|\)/g, ""));
  const read = (tok: string): string =>
    tok.startsWith("#") ? tok : lookup(tok.replace(/var\(--|\)/g, ""));
  const a = read(m[1]);
  const pct = Number(m[2]) / 100;
  // `transparent` over an opaque surface: treat the surface as the other side.
  const b = m[3] === "transparent" ? null : read(m[3]);
  return mix(a, b, pct);
}

function mix(a: string, b: string | null, pct: number, over = "#000000"): string {
  const base = b ?? over;
  const pa = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16));
  const pb = [1, 3, 5].map((i) => parseInt(base.slice(i, i + 2), 16));
  const out = pa.map((v, i) => Math.round(v * pct + pb[i] * (1 - pct)));
  return `#${out.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

function rawToken(body: string, name: string): string {
  const m = body.match(new RegExp(`--${name}:\\s*([^;]+);`));
  expect(m, `--${name} present`).not.toBeNull();
  return (m as RegExpMatchArray)[1].trim();
}

for (const [name, selector, ground] of [
  ["dark", ":root {", "bg-1"],
  ["light", ':root[data-theme="light"]', "bg-1"],
] as const) {
  test(`${name}: git status letters meet WCAG AA against the panel ground`, () => {
    const root = block(":root {");
    const themed = block(selector);
    const pick = (n: string) => {
      const body = themed.includes(`--${n}:`) ? themed : root;
      return rawToken(body, n);
    };
    const lookup = (n: string) => {
      const v = pick(n);
      return v.startsWith("#") ? v : resolveMix(v, lookup);
    };
    const bg = lookup(ground);
    for (const letter of ["git-add-fg", "git-mod-fg", "git-del-fg", "git-unknown-fg"]) {
      const fg = resolveMix(pick(letter), lookup);
      expect(ratio(fg, bg), `${name} --${letter} on --${ground}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  test(`${name}: failure text meets WCAG AA on the surfaces it is painted on`, () => {
    // #824: the over-cap counter and inline save errors are 10.5-12px text. `--status-down`
    // straight measures 2.8:1 on a dark row — a signal colour is not a text colour, so
    // `--danger-text` exists and is pinned here on BOTH grounds it can land on: a settings row
    // (--surface-2) and the panel itself (--panel).
    const root = block(":root {");
    const themed = block(selector);
    const pick = (n: string) => rawToken(themed.includes(`--${n}:`) ? themed : root, n);
    const lookup = (n: string): string => {
      const v = pick(n);
      return v.startsWith("#") ? v : resolveMix(v, lookup);
    };
    const fg = resolveMix(pick("danger-text"), lookup);
    for (const groundToken of ["surface-2", "panel"]) {
      expect(
        ratio(fg, lookup(groundToken)),
        `${name} --danger-text on --${groundToken}`,
      ).toBeGreaterThanOrEqual(4.5);
    }
  });

  test(`${name}: diff foreground/background pairs meet WCAG AA`, () => {
    const root = block(":root {");
    const themed = block(selector);
    const pick = (n: string) => rawToken(themed.includes(`--${n}:`) ? themed : root, n);
    const lookup = (n: string): string => {
      const v = pick(n);
      return v.startsWith("#") ? v : resolveMix(v, lookup);
    };
    // The diff backgrounds are a % of a status hue over `transparent`, painted on --surface-3.
    const surface = lookup("surface-3");
    for (const [fgTok, bgTok] of [
      ["diff-add-fg", "diff-add-bg"],
      ["diff-del-fg", "diff-del-bg"],
    ] as const) {
      const fg = resolveMix(pick(fgTok), lookup);
      const bgExpr = pick(bgTok).match(/color-mix\(in srgb,\s*var\(--([a-z0-9-]+)\)\s*(\d+)%/);
      expect(bgExpr, `${bgTok} is a color-mix`).not.toBeNull();
      const bg = mix(lookup(bgExpr![1]), surface, Number(bgExpr![2]) / 100);
      expect(ratio(fg, bg), `${name} --${fgTok} on --${bgTok}`).toBeGreaterThanOrEqual(4.5);
    }
  });
}

// ---------------------------------------------------------------- raw status hues as TEXT (#889)
//
// #896's review found `.missionBtnDanger` painting small button text in raw `--status-degraded`
// (#f59e0b): 1.93:1 on `--panel` in the light theme, against AA's 4.5:1. `tokens.css` already
// documents that the raw status hues are unreadable as light-theme text, and `--danger-text` /
// `--git-*-fg` exist precisely so nobody has to rediscover it.
//
// So this asserts the RULE rather than the one instance. Pinning only the fixed colour would let
// the next component reach for `color: var(--status-degraded)` and fail the same way, in a file
// this test does not name.

test("no console stylesheet paints TEXT in a raw status hue", () => {
  const files = [
    "src/components/pulse/mission.module.css",
    "src/components/pulse/missionDrawer.module.css",
  ];
  const offenders: string[] = [];
  for (const rel of files) {
    const css = readFileSync(resolve(process.cwd(), rel), "utf8");
    css.split("\n").forEach((line, i) => {
      // `color:` only. `background`, `border-color` and `fill` are signals rather than text: a
      // 1px edge or a 6px LED has no contrast requirement, and `tokens.css` says so explicitly.
      const m = line.match(/^\s*color:\s*var\(--status-([a-z]+)\)/);
      if (m) offenders.push(`${rel}:${i + 1} → color: var(--status-${m[1]})`);
    });
  }
  expect(
    offenders,
    "use --danger-text (or a color-mix toward --text-1) for text; the raw status hues are " +
      "signal colours and fail AA as small text on the light ground",
  ).toEqual([]);
});

// ---------------------------------------------------------------- editor syntax colours (#950)
//
// The editor paints code on --surface-3. Every syntax token is plain hex so it can be checked
// directly, on every selector that declares a palette (the bare :root is the dark default).

const SYNTAX = [
  "syn-keyword",
  "syn-string",
  "syn-number",
  "syn-comment",
  "syn-function",
  "syn-type",
  "syn-property",
  "syn-punct",
];

for (const [name, selector] of Object.entries(THEMES)) {
  test(`${name}: every editor syntax colour meets WCAG AA on --surface-3`, () => {
    const root = block(":root {");
    const themed = block(selector);
    const pick = (n: string) => token(themed.includes(`--${n}:`) ? themed : root, n);
    const ground = pick("surface-3");
    for (const syn of SYNTAX) {
      expect(ratio(pick(syn), ground), `${name} --${syn} on --surface-3`).toBeGreaterThanOrEqual(
        4.5,
      );
    }
  });

  test(`${name}: no syntax colour is a status hue or the brand accent`, () => {
    // A string painted --status-up reads as "healthy"; a keyword painted --accent reads as a
    // control. Colour is load-bearing in this design system (docs/design.md §3).
    const root = block(":root {");
    const themed = block(selector);
    const pick = (n: string) => token(themed.includes(`--${n}:`) ? themed : root, n);
    const reserved = ["status-up", "status-degraded", "status-down", "status-unknown", "status-draft", "accent"].map(
      (n) => token(root, n).toLowerCase(),
    );
    for (const syn of SYNTAX) {
      expect(reserved, `${name} --${syn}`).not.toContain(pick(syn).toLowerCase());
    }
  });
}

// --- engine accents (#853 P4) ---
// A chip/badge/border colour, not body text: held to >=3:1 (WCAG non-text contrast) on every
// ground a chip sits on, and never equal to a status hue or the brand accent — an agent's colour
// must not read as "healthy", "down" or "clickable".
const ENGINE_ACCENTS = ["amber", "teal", "green", "blue", "lime", "magenta", "slate"];

for (const [name, selector] of Object.entries(THEMES)) {
  test(`${name}: every engine accent reads on the chip grounds (>=3:1)`, () => {
    const root = block(":root {");
    const themed = block(selector);
    const pick = (n: string) => token(themed.includes(`--${n}:`) ? themed : root, n);
    for (const a of ENGINE_ACCENTS) {
      for (const ground of ["bg-1", "bg-2"]) {
        expect(
          ratio(pick(`engine-${a}`), pick(ground)),
          `${name} --engine-${a} on --${ground}`,
        ).toBeGreaterThanOrEqual(3);
      }
    }
  });

  test(`${name}: no engine accent is a status hue or the brand accent`, () => {
    const root = block(":root {");
    const themed = block(selector);
    const pick = (n: string) => token(themed.includes(`--${n}:`) ? themed : root, n);
    const reserved = [
      "status-up",
      "status-degraded",
      "status-down",
      "status-unknown",
      "status-draft",
      "accent",
    ].map((n) => pick(n).toLowerCase());
    for (const a of ENGINE_ACCENTS) {
      expect(reserved, `${name} --engine-${a}`).not.toContain(pick(`engine-${a}`).toLowerCase());
    }
  });
}
