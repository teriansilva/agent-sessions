/** The pages outside the SPA (#1596) share the app's palette and primitives through a GENERATED
 *  copy in the server's static folder (web/scripts/emit-page-tokens.mjs). A copy that drifts from
 *  its source is the two-palettes failure tokens.css exists to prevent, so every generated file is
 *  regenerated here and compared byte for byte; `pages.css`, the hand-written layout beside them,
 *  holds to tokens only. Fix a failure by running `node scripts/emit-page-tokens.mjs`. */
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";

// @ts-expect-error — a plain .mjs build script, no type declarations.
import { OUT, pageFiles } from "../../scripts/emit-page-tokens.mjs";

const { text, binaries } = pageFiles() as {
  text: Record<string, string>;
  binaries: Record<string, string>;
};

describe("the generated page stylesheets", () => {
  test.each(Object.keys(text))("%s is what the generator makes from its source", (name) => {
    const path = join(OUT, name);
    expect(existsSync(path), `${name} is committed`).toBe(true);
    expect(readFileSync(path, "utf8")).toBe(text[name]);
  });

  test.each(Object.keys(binaries))("%s is the app's own font file", (name) => {
    expect(readFileSync(join(OUT, name)).equals(readFileSync(binaries[name]))).toBe(true);
  });

  test("nothing stale sits beside them", () => {
    const expected = new Set([...Object.keys(text), "pages.css", "fonts"]);
    expect(readdirSync(OUT).filter((f) => !expected.has(f))).toEqual([]);
    expect(readdirSync(join(OUT, "fonts")).sort()).toEqual(
      Object.keys(binaries)
        .map((n) => n.replace("fonts/", ""))
        .sort(),
    );
  });

  test("they follow the device's theme: the light tokens and the light primitives", () => {
    expect(text["tokens.css"]).toMatch(/@media \(prefers-color-scheme: light\) \{\n {2}:root:not\(\[data-theme\]\) \{/);
    // Every light value the app has applies on the device-light page too.
    const light = /:root\[data-theme="light"\]\s*\{([\s\S]*?)\n\}/.exec(text["tokens.css"])![1];
    const names = [...light.matchAll(/(--[a-z0-9-]+):/g)].map((m) => m[1]);
    const device = text["tokens.css"].split("@media (prefers-color-scheme: light)")[1];
    for (const n of names) expect(device, n).toContain(`${n}:`);
    expect(text["nightjar.css"]).toContain(":root:not([data-theme]) .nj-atmo .nj-vig");
    expect(text["nightjar.css"]).toContain(":root:not([data-theme]) .nj-atmo .nj-hex");
  });
});

describe("pages.css (the hand-written layout)", () => {
  const css = readFileSync(join(OUT, "pages.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

  test("tokens only: no literal colour (the QR tile's quiet zone is --qr-ground)", () => {
    expect(css.match(/#[0-9a-fA-F]{3,8}\b/g) ?? []).toEqual([]);
    expect(css).not.toMatch(/\brgba?\(/);
  });

  test("every token it reads exists", () => {
    const defined = new Set(
      [text["tokens.css"], text["motion.css"], text["nightjar.css"]].flatMap((t) =>
        [...t.matchAll(/(--[a-z0-9-]+)\s*:/g)].map((m) => m[1]),
      ),
    );
    const used = [...css.matchAll(/var\((--[a-z0-9-]+)/g)].map((m) => m[1]);
    expect(used.filter((v) => !defined.has(v))).toEqual([]);
  });
});
