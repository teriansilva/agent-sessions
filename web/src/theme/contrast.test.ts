import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "vitest";

// Parses the SHIPPED index.css and enforces WCAG-AA contrast on each theme's palette, so a
// future palette tweak can't silently drop below readable. Royal lives in the bare `:root`;
// dark/light live in `:root[data-theme="…"]`. We check body text/bg per theme, plus the CTA
// contract once (theme-independent): the filled CTAs are gold (--cta-bg-1) with dark text
// (--cta-text), defined only in the bare :root and never overridden per theme.

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
  royal: ":root {",
  dark: ':root[data-theme="dark"]',
  light: ':root[data-theme="light"]',
};

for (const [name, selector] of Object.entries(THEMES)) {
  test(`${name}: body text on bg meets WCAG AA (>=4.5:1)`, () => {
    const b = block(selector);
    expect(ratio(token(b, "text"), token(b, "bg"))).toBeGreaterThanOrEqual(4.5);
  });

  // --accent is still a text-bearing surface after the gold-CTA switch: the active filter
  // tab (Filters.module.css `.tabs button.on`) renders white on --accent. Keep it AA.
  test(`${name}: white on --accent meets WCAG AA (>=4.5:1)`, () => {
    const b = block(selector);
    expect(ratio("#ffffff", token(b, "accent"))).toBeGreaterThanOrEqual(4.5);
  });
}

// The gold CTA is theme-independent (defined only in the bare :root): dark --cta-text on the
// gold --cta-bg-1 base, the most-contrast-critical pair of the gradient. ~11:1, WCAG AAA.
test("CTA: --cta-text on --cta-bg-1 meets WCAG AA (>=4.5:1)", () => {
  const root = block(":root {");
  expect(ratio(token(root, "cta-text"), token(root, "cta-bg-1"))).toBeGreaterThanOrEqual(4.5);
});
