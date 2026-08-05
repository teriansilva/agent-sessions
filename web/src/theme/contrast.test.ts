import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "vitest";

// Parses the SHIPPED index.css and enforces WCAG-AA contrast on each theme's palette, so a
// future palette tweak can't silently drop below readable. Dark is the bare `:root` default;
// dark/light also live in `:root[data-theme="…"]`. We check body text/bg per theme, plus the
// CTA contract: since #211 Phase 2 the filled CTAs derive from the brand accent
// (--cta-bg-1 = var(--accent), --cta-text = var(--on-accent)), so their contrast IS the
// on-accent/accent pair already checked per theme.

// vitest runs from web/; the stylesheet under test is web/src/index.css.
const css = readFileSync(resolve(process.cwd(), "src/index.css"), "utf8");

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
