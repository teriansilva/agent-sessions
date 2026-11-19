import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { describe, expect, test } from "vitest";

/** The hosted Markdown renderer (#2210) runs inside the artifact sandbox, an opaque origin, from
 *  ONE classic script. This walks every local import from `artifact-md/main.tsx` and holds it to
 *  an allowlist: the Markdown seam's pure renderer and nothing of the app (no API client, no
 *  worker, no Mermaid, no highlighter), and no dynamic import (a chunk would be a CORS request). */

const SRC = resolve(__dirname, "..");
const WEB = resolve(SRC, "..");
const ENTRY = join(SRC, "artifact-md", "main.tsx");

const ALLOWED_OUTSIDE = new Set([
  "components/files/markdown/ArtifactMarkdown.tsx",
  "components/files/markdown/markdownPipeline.ts",
  "components/files/markdown/RemoteImage.tsx",
  "components/files/markdown/markdownView.ts",
  "components/files/markdown/markdownDocument.module.css",
]);
const ALLOWED_PACKAGES = new Set(["react", "react-dom/client", "react-markdown", "rehype-sanitize", "remark-gfm"]);

const SPEC = /(?:import|export)\s[^'"]*?from\s*["']([^"']+)["']|import\s*["']([^"']+)["']/g;
const DYNAMIC = /\bimport\(/;

function resolveLocal(from: string, spec: string): string | null {
  if (!spec.startsWith(".")) return null;
  const base = resolve(dirname(from), spec);
  for (const cand of [base, `${base}.ts`, `${base}.tsx`]) {
    if (existsSync(cand) && statSync(cand).isFile()) return cand;
  }
  throw new Error(`unresolved import ${spec} from ${relative(SRC, from)}`);
}

const packages = new Set<string>();
const dynamic: string[] = [];
function walk(): string[] {
  const seen = new Set<string>();
  const todo = [ENTRY];
  while (todo.length) {
    const file = todo.pop()!;
    if (seen.has(file)) continue;
    seen.add(file);
    if (file.endsWith(".css")) continue;
    const text = readFileSync(file, "utf8").replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
    if (DYNAMIC.test(text)) dynamic.push(relative(SRC, file));
    for (const m of text.matchAll(SPEC)) {
      const spec = m[1] ?? m[2];
      if (!spec) continue;
      const next = resolveLocal(file, spec);
      if (next) todo.push(next);
      else packages.add(spec);
    }
  }
  return [...seen].map((f) => relative(SRC, f).split("\\").join("/"));
}

describe("the hosted Markdown renderer bundle (#2210)", () => {
  const reached = walk();

  test("reaches only its own modules and the Markdown seam's pure renderer", () => {
    expect(reached.filter((f) => !f.startsWith("artifact-md/") && !ALLOWED_OUTSIDE.has(f))).toEqual([]);
    expect(reached).toContain("components/files/markdown/ArtifactMarkdown.tsx");
  });

  test("pulls in only the allowlisted packages: no rehype-raw, no Mermaid, no app code", () => {
    expect([...packages].filter((p) => !ALLOWED_PACKAGES.has(p))).toEqual([]);
    expect(packages.has("rehype-sanitize")).toBe(true);
  });

  test("has no dynamic import: everything is in the one classic script", () => {
    expect(dynamic).toEqual([]);
  });

  test("is built as ONE classic script (IIFE) with fixed names beside the app's assets", () => {
    const config = readFileSync(join(WEB, "vite.config.artifact-md.ts"), "utf8");
    expect(config).toMatch(/formats:\s*\["iife"\]/);
    expect(config).toMatch(/fileName:\s*\(\)\s*=>\s*"artifact-md\.js"/);
    expect(config).toMatch(/cssFileName:\s*"artifact-md"/);
    expect(config).toMatch(/outDir:\s*"dist\/assets"/);
    expect(config).toMatch(/emptyOutDir:\s*false/);
    // `npm run build` builds it AFTER the app (whose build empties dist/).
    const build = JSON.parse(readFileSync(join(WEB, "package.json"), "utf8")).scripts.build as string;
    expect(build).toMatch(/vite build && vite build --config vite\.config\.artifact-md\.ts$/);
  });
});
