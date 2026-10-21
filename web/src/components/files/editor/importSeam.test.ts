import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";
import { expect, test } from "vitest";

/** The editor seam (#950): only `components/files/editor/` may import CodeMirror or Lezer.
 *
 *  This is what keeps the third-party editor cheap to maintain — and cheap to replace. A second
 *  import site anywhere else in the app is how a library quietly becomes load-bearing. */

const SRC = resolve(process.cwd(), "src");
const SEAM = join(SRC, "components", "files", "editor") + sep;
const IMPORT = /(?:from\s+|import\s*\(\s*|import\s+)["'](@codemirror|@lezer)\//;

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) return sources(full);
    return /\.(ts|tsx)$/.test(name) ? [full] : [];
  });
}

test("only the editor directory imports CodeMirror or Lezer", () => {
  const offenders = sources(SRC)
    .filter((f) => !f.startsWith(SEAM))
    .filter((f) => IMPORT.test(readFileSync(f, "utf8")))
    .map((f) => relative(SRC, f));
  expect(offenders).toEqual([]);
});

test("the seam is not vacuous: the editor directory really does import them", () => {
  const inside = sources(SEAM).filter((f) => IMPORT.test(readFileSync(f, "utf8")));
  expect(inside.length).toBeGreaterThan(0);
});
