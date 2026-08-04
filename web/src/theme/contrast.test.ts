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
