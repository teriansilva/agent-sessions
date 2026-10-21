import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "vitest";

// tokens.css carries THREE selectors, and two of them describe the same theme: the bare `:root`
// IS the dark default, and `:root[data-theme="dark"]` restates the dark palette so an explicit
// choice beats an inherited one. Two copies of one palette is a standing invitation to change one
// and not the other — a tweak to the bare `:root` that the explicit block doesn't get would leave
// users who picked dark on a stale palette while everyone on the default sees the new one, which
// looks like a rendering bug rather than a token bug. These tests pin the relationship (#829).
// Comments stripped before any selector lookup — the header comment above names
// `:root[data-theme="dark"]`, and a raw indexOf would match THAT and return the bare :root body.
const css = readFileSync(resolve(process.cwd(), "src/tokens.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);

function block(selector: string): string {
  const i = css.indexOf(selector);
  expect(i, `selector ${selector} present`).toBeGreaterThanOrEqual(0);
  const open = css.indexOf("{", i);
  const close = css.indexOf("}", open);
  return css.slice(open + 1, close);
}

// Split on `;` at paren-depth 0 so a color-mix(...) argument list stays one declaration.
function decls(body: string): Map<string, string> {
  const out = new Map<string, string>();
  const clean = body;
  let depth = 0;
  let cur = "";
  const flush = () => {
    const d = cur.trim();
    cur = "";
    if (!d.includes(":")) return;
    const k = d.slice(0, d.indexOf(":")).trim();
    const v = d
      .slice(d.indexOf(":") + 1)
      .trim()
      .replace(/\s+/g, " ");
    out.set(k, v);
  };
  for (const ch of clean) {
    if (ch === "(") depth++;
    else if (ch === ")") depth--;
    if (ch === ";" && depth === 0) flush();
    else cur += ch;
  }
  flush();
  return out;
}

const bare = decls(block(":root {"));
const dark = decls(block(':root[data-theme="dark"]'));
const light = decls(block(':root[data-theme="light"]'));

test("tokens.css declares all three selectors", () => {
  expect(bare.size).toBeGreaterThan(0);
  expect(dark.size).toBeGreaterThan(0);
  expect(light.size).toBeGreaterThan(0);
});

// The explicit-dark block is deliberately a SUBSET of the bare :root — it restates the palette,
// not the theme-shared tokens (status hues, fonts, derived color-mix values) that both themes
// share. What it must never do is disagree.
test("explicit dark never diverges from the bare :root default", () => {
  const diverged = [...dark]
    .filter(([k, v]) => bare.get(k) !== v)
    .map(([k, v]) => `${k}: ${v} (bare: ${bare.get(k) ?? "absent"})`);
  expect(diverged, "every :root[data-theme=dark] declaration matches the bare :root").toEqual([]);
});

// A token that only light declares would have no dark value at all: on dark it would resolve to
// whatever the browser inherits (usually nothing), so the surface using it renders unstyled.
test("every token light overrides exists in the bare :root", () => {
  const orphans = [...light.keys()].filter((k) => k.startsWith("--") && !bare.has(k));
  expect(orphans, "light overrides only tokens the dark default defines").toEqual([]);
});

// index.css must keep importing the palette, and the @import must stay the first rule — CSS
// drops an @import that follows any other rule, which would silently unstyle the whole app.
test("index.css imports tokens.css as its first rule", () => {
  const index = readFileSync(resolve(process.cwd(), "src/index.css"), "utf8");
  const withoutComments = index.replace(/\/\*[\s\S]*?\*\//g, "").trim();
  expect(withoutComments.startsWith('@import "./tokens.css";')).toBe(true);
});
