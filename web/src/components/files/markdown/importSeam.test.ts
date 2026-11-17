import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";
import { expect, test } from "vitest";

/** The Markdown preview seam (#2114): only `components/files/markdown/` imports Mermaid or the
 *  raw-HTML pipeline (`rehype-raw`, `rehype-sanitize`). One import site is what keeps them cheap
 *  to maintain and to replace — and keeps raw HTML out of every other Markdown surface (agent
 *  replies render with no HTML at all, #1332). */

const SRC = resolve(process.cwd(), "src");
const SEAM = join(SRC, "components", "files", "markdown") + sep;
const IMPORT = /(?:from\s+|import\s*\(\s*|import\s+)["'](mermaid|rehype-raw|rehype-sanitize)["'/]/;

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) return sources(full);
    return /\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name) ? [full] : [];
  });
}

test("only the markdown preview directory imports mermaid, rehype-raw or rehype-sanitize", () => {
  const offenders = sources(SRC)
    .filter((f) => !f.startsWith(SEAM))
    .filter((f) => IMPORT.test(readFileSync(f, "utf8")))
    .map((f) => relative(SRC, f));
  expect(offenders).toEqual([]);
});

test("the seam is not vacuous: each of the three is imported inside it", () => {
  const inside = sources(SEAM).map((f) => readFileSync(f, "utf8")).join("\n");
  for (const name of ["mermaid", "rehype-raw", "rehype-sanitize"]) {
    expect(inside).toMatch(new RegExp(`["']${name}["']`));
  }
});

test("the file viewer loads the preview lazily, never as a static import", () => {
  const viewer = readFileSync(join(SRC, "components", "files", "FileViewerModal.tsx"), "utf8");
  expect(viewer).toMatch(/lazy\(\(\) => import\("\.\/markdown\/MarkdownDocument"\)\)/);
  expect(viewer).not.toMatch(/from\s+["']\.\/markdown\/MarkdownDocument["']/);
});
